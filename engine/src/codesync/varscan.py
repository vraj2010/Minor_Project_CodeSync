"""Scanning a project for every variable and every read and write of it.

The scan runs in two passes over the whole project. The first builds the scopes
of every file and declares the variables that belong to them, following
Python's own rules: a name assigned in a function is local to it unless
declared `global` or `nonlocal`, a class body does not leak into its methods,
and a comprehension has a scope of its own. The second pass walks the code
again and attributes each read and write to the variable it reaches, including
variables imported from other modules and attributes reached through `self`."""

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

from .index import ProjectIndex
from .resolve import Resolver
from .symbols import ModuleInfo, Symbol, SymbolKind
from .typeinfer import combine_types, infer_type, operator_symbol
from .variables import Access, AccessMode, Variable, VariableKind, VariableTable

_TEXT_LIMIT = 200
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_CLASS_RECEIVERS = frozenset({"__new__", "__init_subclass__", "__class_getitem__"})
_COMPREHENSIONS = {
    ast.ListComp: "listcomp",
    ast.SetComp: "setcomp",
    ast.DictComp: "dictcomp",
    ast.GeneratorExp: "genexpr",
}
_SHARED_KINDS = (VariableKind.MODULE, VariableKind.CLASS, VariableKind.INSTANCE)


class _ScopeKind(Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    LAMBDA = "lambda"
    COMPREHENSION = "comprehension"


@dataclass(eq=False)
class _Builder:
    qualname: str
    name: str
    kind: VariableKind
    scope: str
    container: str
    module: str
    relpath: str
    lineno: int
    col_offset: int
    annotation: str | None = None
    default: str | None = None
    parameter_kind: str | None = None
    receiver_type: str | None = None
    is_receiver: bool = False
    evidence: list[str | None] = field(default_factory=list)


@dataclass(eq=False)
class _Scope:
    kind: _ScopeKind
    qualname: str
    parent: "_Scope | None"
    relpath: str
    module: str
    symbol: Symbol | None = None
    builders: dict[str, _Builder] = field(default_factory=dict)
    defs: set[str] = field(default_factory=set)
    globals: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)
    receiver: str | None = None
    receiver_class: str | None = None
    receiver_is_class: bool = False

    @property
    def container(self) -> str:
        scope = self
        while scope.kind in (_ScopeKind.LAMBDA, _ScopeKind.COMPREHENSION):
            scope = scope.parent
        return scope.qualname

    def module_scope(self) -> "_Scope":
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def symbol_scope(self) -> Symbol | None:
        scope: _Scope | None = self
        while scope is not None:
            if scope.symbol is not None:
                return scope.symbol
            scope = scope.parent
        return None

    def receiver_info(self, name: str) -> tuple[str, bool] | None:
        """The class `name` stands for when it is the `self` or `cls` of the
        method this scope is, or is nested in."""
        scope: _Scope | None = self
        while scope is not None:
            if scope.receiver == name:
                return scope.receiver_class, scope.receiver_is_class
            if name in scope.builders:
                return None
            if scope.kind in (_ScopeKind.CLASS, _ScopeKind.MODULE):
                return None
            scope = scope.parent
        return None


class _Project:
    def __init__(self, index: ProjectIndex, resolver: Resolver) -> None:
        self.index = index
        self.resolver = resolver
        self.symbols = index.table
        self.builders: dict[str, _Builder] = {}
        self.accesses: list[Access] = []
        self.scopes: dict[str, dict[int, _Scope]] = {}
        self._walrus: set[int] = set()
        self._imported: dict[tuple[str, str], _Builder | None] = {}
        self._subclasses: dict[str, list[str]] | None = None

    def module_label(self, info: ModuleInfo) -> str:
        kept = self.symbols.module(info.name)
        if kept is None or kept.relpath == info.relpath:
            return info.name
        return f"{info.name}@{info.relpath}"

    def declare_module(self, info: ModuleInfo, tree: ast.Module) -> _Scope:
        label = self.module_label(info)
        scope = _Scope(_ScopeKind.MODULE, label, None, info.relpath, label)
        self.scopes[info.relpath] = {}
        self._descend(scope, tree)
        return scope

    def declare(
        self,
        scope: _Scope,
        name: str,
        node: ast.AST,
        kind: VariableKind | None = None,
    ) -> _Builder:
        qualname = f"{scope.qualname}.{name}"
        builder = self.builders.get(qualname)
        if builder is None:
            builder = _Builder(
                qualname=qualname,
                name=name,
                kind=kind or _kind_of(scope),
                scope=scope.qualname,
                container=scope.container,
                module=scope.module,
                relpath=scope.relpath,
                lineno=node.lineno,
                col_offset=node.col_offset,
            )
            self.builders[qualname] = builder
        scope.builders[name] = builder
        return builder

    def declare_attribute(
        self,
        class_qualname: str,
        name: str,
        node: ast.AST,
        scope: _Scope,
        *,
        on_class: bool,
    ) -> _Builder:
        qualname = f"{class_qualname}.{name}"
        builder = self.builders.get(qualname)
        if builder is None:
            builder = _Builder(
                qualname=qualname,
                name=name,
                kind=VariableKind.CLASS if on_class else VariableKind.INSTANCE,
                scope=class_qualname,
                container=class_qualname,
                module=scope.module,
                relpath=scope.relpath,
                lineno=node.lineno,
                col_offset=node.col_offset,
            )
            self.builders[qualname] = builder
        return builder

    def call_type(self, scope: _Scope, func: ast.expr) -> str | None:
        path = self.resolver.dotted(scope.relpath, func, scope.symbol_scope())
        symbol = self.resolver.canonical(path) if path else None
        if symbol is None:
            return None
        if symbol.kind is SymbolKind.CLASS:
            return symbol.qualname
        node = self.resolver.node_for(symbol)
        if isinstance(node, _FUNCTIONS) and node.returns is not None:
            return ast.unparse(node.returns)
        return None

    def class_attribute(
        self, class_qualname: str, name: str, seen: set[str] | None = None
    ) -> _Builder | None:
        found = self.builders.get(f"{class_qualname}.{name}")
        if found is not None and found.kind in (
            VariableKind.CLASS,
            VariableKind.INSTANCE,
        ):
            return found

        seen = set() if seen is None else seen
        if class_qualname in seen:
            return None
        seen.add(class_qualname)

        cls = self.symbols.lookup(class_qualname)
        if cls is None or cls.kind is not SymbolKind.CLASS:
            return None
        for base in self.resolver.bases(cls):
            base_class = self.resolver.canonical(base)
            if base_class is not None and base_class.kind is SymbolKind.CLASS:
                found = self.class_attribute(base_class.qualname, name, seen)
                if found is not None:
                    return found
        return None

    def attribute_candidates(self, class_qualname: str, name: str) -> list[_Builder]:
        """The variables `self.<name>` can refer to inside a class: the one the
        class or its bases define, or else those its subclasses define, as when
        a mixin reads an attribute set by the class it is mixed into."""
        found = self.class_attribute(class_qualname, name)
        if found is not None:
            return [found]

        candidates: list[_Builder] = []
        subclasses = self.subclasses()
        pending = list(subclasses.get(class_qualname, ()))
        seen = {class_qualname}
        while pending:
            subclass = pending.pop(0)
            if subclass in seen:
                continue
            seen.add(subclass)
            builder = self.builders.get(f"{subclass}.{name}")
            if builder is not None and builder.kind in (
                VariableKind.CLASS,
                VariableKind.INSTANCE,
            ):
                candidates.append(builder)
            else:
                pending.extend(subclasses.get(subclass, ()))
        return candidates

    def subclasses(self) -> dict[str, list[str]]:
        if self._subclasses is None:
            found: dict[str, list[str]] = defaultdict(list)
            for symbol in self.symbols.symbols:
                if symbol.kind is not SymbolKind.CLASS:
                    continue
                for base in self.resolver.bases(symbol):
                    base_class = self.resolver.canonical(base)
                    if base_class is not None and base_class.kind is SymbolKind.CLASS:
                        found[base_class.qualname].append(symbol.qualname)
            self._subclasses = dict(found)
        return self._subclasses

    def variable_at(self, path: str) -> _Builder | None:
        found = self.builders.get(path)
        if found is not None and found.kind in _SHARED_KINDS:
            return found

        symbol = self.resolver.canonical(path)
        if symbol is not None:
            found = self.builders.get(symbol.qualname)
            return found if found is not None and found.kind in _SHARED_KINDS else None

        owner, _, attribute = path.rpartition(".")
        owner_symbol = self.resolver.canonical(owner) if owner else None
        if owner_symbol is not None and owner_symbol.kind is SymbolKind.CLASS:
            return self.class_attribute(owner_symbol.qualname, attribute)
        return None

    def imported(self, relpath: str, name: str) -> _Builder | None:
        key = (relpath, name)
        if key not in self._imported:
            path = self.resolver.imports(relpath).bindings.get(name)
            self._imported[key] = self.variable_at(path) if path else None
        return self._imported[key]

    def first_walrus(self, node: ast.NamedExpr) -> bool:
        if id(node) in self._walrus:
            return False
        self._walrus.add(id(node))
        return True

    def _descend(self, scope: _Scope, node: ast.AST) -> None:
        declarer = _Declarer(self, scope)
        declarer.run(node)
        for nested in declarer.nested:
            child = self._child(scope, nested)
            self.scopes[scope.relpath][id(nested)] = child
            self._descend(child, nested)

    def _child(self, parent: _Scope, node: ast.AST) -> _Scope:
        if isinstance(node, _FUNCTIONS):
            scope = self._scope(parent, _ScopeKind.FUNCTION, node.name, node)
            self._parameters(scope, node.args)
            if parent.kind is _ScopeKind.CLASS:
                self._receiver(scope, parent, node)
            return scope
        if isinstance(node, ast.ClassDef):
            return self._scope(parent, _ScopeKind.CLASS, node.name, node)
        if isinstance(node, ast.Lambda):
            scope = self._scope(
                parent, _ScopeKind.LAMBDA, _anonymous("lambda", node), node
            )
            self._parameters(scope, node.args)
            return scope
        label = _COMPREHENSIONS[type(node)]
        return self._scope(
            parent, _ScopeKind.COMPREHENSION, _anonymous(label, node), node
        )

    def _scope(
        self, parent: _Scope, kind: _ScopeKind, name: str, node: ast.AST
    ) -> _Scope:
        defined = isinstance(node, (*_FUNCTIONS, ast.ClassDef))
        return _Scope(
            kind,
            f"{parent.qualname}.{name}",
            parent,
            parent.relpath,
            parent.module,
            symbol=self.resolver.symbol_for(parent.relpath, node) if defined else None,
        )

    def _parameters(self, scope: _Scope, args: ast.arguments) -> None:
        positional = [*args.posonlyargs, *args.args]
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for index, (arg, default) in enumerate(zip(positional, defaults, strict=True)):
            kind = "positional-only" if index < len(args.posonlyargs) else "positional"
            self._parameter(scope, arg, kind, default)
        if args.vararg is not None:
            self._parameter(scope, args.vararg, "var-positional", None, "tuple")
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            self._parameter(scope, arg, "keyword-only", default)
        if args.kwarg is not None:
            self._parameter(scope, args.kwarg, "var-keyword", None, "dict")

    def _parameter(
        self,
        scope: _Scope,
        arg: ast.arg,
        kind: str,
        default: ast.expr | None,
        implied: str | None = None,
    ) -> None:
        builder = self.declare(scope, arg.arg, arg, VariableKind.PARAMETER)
        builder.parameter_kind = kind
        if arg.annotation is not None:
            builder.annotation = ast.unparse(arg.annotation)
        if implied is not None:
            builder.evidence.append(implied)
        if default is not None:
            builder.default = _text(default)
            outer = scope.parent or scope
            builder.evidence.append(
                infer_type(default, lambda func: self.call_type(outer, func))
            )

    def _receiver(self, scope: _Scope, cls: _Scope, node: ast.AST) -> None:
        decorators = {
            item.id for item in node.decorator_list if isinstance(item, ast.Name)
        }
        if "staticmethod" in decorators:
            return
        first = [*node.args.posonlyargs, *node.args.args]
        if not first:
            return

        on_class = "classmethod" in decorators or node.name in _CLASS_RECEIVERS
        scope.receiver = first[0].arg
        scope.receiver_class = cls.qualname
        scope.receiver_is_class = on_class
        builder = scope.builders[first[0].arg]
        builder.is_receiver = True
        builder.receiver_type = f"type[{cls.qualname}]" if on_class else cls.qualname

    def freeze(self) -> VariableTable:
        table = VariableTable()
        for builder in self.builders.values():
            table.add(self._variable(builder))
        for access in self.accesses:
            table.add_access(access)
        return table

    def _variable(self, builder: _Builder) -> Variable:
        static_type, source = None, None
        if builder.receiver_type is not None:
            static_type, source = builder.receiver_type, "inferred"
        elif builder.annotation is not None:
            static_type, source = builder.annotation, "annotation"
        else:
            static_type = combine_types(builder.evidence)
            source = "inferred" if static_type else None

        return Variable(
            qualname=builder.qualname,
            name=builder.name,
            kind=builder.kind,
            scope=builder.scope,
            container=builder.container,
            module=builder.module,
            relpath=builder.relpath,
            lineno=builder.lineno,
            col_offset=builder.col_offset,
            annotation=builder.annotation,
            default=builder.default,
            parameter_kind=builder.parameter_kind,
            static_type=static_type,
            type_source=source,
            overrides=self._overrides(builder),
            receiver=builder.is_receiver,
        )

    def _overrides(self, builder: _Builder) -> str | None:
        if builder.kind not in (VariableKind.CLASS, VariableKind.INSTANCE):
            return None
        cls = self.symbols.lookup(builder.scope)
        if cls is None or cls.kind is not SymbolKind.CLASS:
            return None
        for base in self.resolver.bases(cls):
            base_class = self.resolver.canonical(base)
            if base_class is None or base_class.kind is not SymbolKind.CLASS:
                continue
            found = self.class_attribute(base_class.qualname, builder.name)
            if found is not None:
                return found.qualname
        return None


class _Declarer(ast.NodeVisitor):
    """First pass over one scope: declares the variables bound in it and
    collects the scopes nested in it, without entering them."""

    def __init__(self, project: _Project, scope: _Scope) -> None:
        self.project = project
        self.scope = scope
        self.nested: list[ast.AST] = []

    def run(self, node: ast.AST) -> None:
        if self.scope.kind is _ScopeKind.COMPREHENSION:
            self._comprehension_body(node)
        elif isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            for statement in node.body:
                self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.defs.add(node.name)
        for expression in (*node.decorator_list, *_defaults(node.args)):
            self.visit(expression)
        for expression in _annotations(node):
            self.visit(expression)
        self.nested.append(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.defs.add(node.name)
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.nested.append(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in _defaults(node.args):
            self.visit(expression)
        self.nested.append(node)

    def _comprehension(self, node: ast.expr) -> None:
        self.visit(node.generators[0].iter)
        for inner in ast.walk(node):
            if isinstance(inner, ast.NamedExpr) and self.project.first_walrus(inner):
                self._bind_walrus(inner)
        self.nested.append(node)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def _comprehension_body(self, node: ast.expr) -> None:
        for index, generator in enumerate(node.generators):
            self._target(generator.target, None)
            if index > 0:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        if (
            self.scope.kind is not _ScopeKind.COMPREHENSION
            and self.project.first_walrus(node)
        ):
            self._bind_walrus(node)

    def _bind_walrus(self, node: ast.NamedExpr) -> None:
        scope = self.scope
        while scope.kind is _ScopeKind.COMPREHENSION:
            scope = scope.parent
        self._bind(node.target.id, node.target, node.value, scope=scope)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.scope.defs.add(alias.asname or alias.name.partition(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.scope.defs.add(alias.asname or alias.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)
        declared = (
            self.scope.kind is _ScopeKind.CLASS
            and node.value is None
            and isinstance(node.target, ast.Name)
        )
        self._target(
            node.target,
            node.value,
            ast.unparse(node.annotation),
            VariableKind.INSTANCE if declared else None,
        )

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Attribute):
            self.visit(node.target.value)
        else:
            self._target(node.target, None)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._target(node.target, None)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._target(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._bind(node.name, node, None, hint=_exception_type(node.type))
        for statement in node.body:
            self.visit(statement)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, target, None)
            else:
                self.visit(target)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._bind(node.name, node, None)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name, node, None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind(node.rest, node, None)
        self.generic_visit(node)

    def _target(
        self,
        target: ast.expr,
        value: ast.expr | None,
        annotation: str | None = None,
        kind: VariableKind | None = None,
    ) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, target, value, annotation, kind=kind)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element, item in zip(
                target.elts, _unpacked(value, len(target.elts)), strict=True
            ):
                self._target(element, item)
        elif isinstance(target, ast.Starred):
            self._target(target.value, None)
        elif isinstance(target, ast.Attribute):
            self._bind_attribute(target, value, annotation)
            self.visit(target.value)
        else:
            self.visit(target)

    def _bind(
        self,
        name: str,
        node: ast.AST,
        value: ast.expr | None,
        annotation: str | None = None,
        *,
        scope: _Scope | None = None,
        hint: str | None = None,
        kind: VariableKind | None = None,
    ) -> None:
        owner = scope or self._owner(name)
        builder = self.project.declare(owner, name, node, kind)
        self._remember(builder, value, annotation, hint)

    def _bind_attribute(
        self, target: ast.Attribute, value: ast.expr | None, annotation: str | None
    ) -> None:
        if not isinstance(target.value, ast.Name):
            return
        receiver = self.scope.receiver_info(target.value.id)
        if receiver is None:
            return
        class_qualname, on_class = receiver
        builder = self.project.declare_attribute(
            class_qualname, target.attr, target, self.scope, on_class=on_class
        )
        self._remember(builder, value, annotation, None)

    def _remember(
        self,
        builder: _Builder,
        value: ast.expr | None,
        annotation: str | None,
        hint: str | None,
    ) -> None:
        if annotation and builder.annotation is None:
            builder.annotation = annotation
        if value is not None:
            builder.evidence.append(
                infer_type(value, lambda func: self.project.call_type(self.scope, func))
            )
        if hint:
            builder.evidence.append(hint)

    def _owner(self, name: str) -> _Scope:
        scope = self.scope
        if name in scope.globals:
            return scope.module_scope()
        if name in scope.nonlocals:
            enclosing = scope.parent
            while enclosing is not None:
                if enclosing.kind is _ScopeKind.FUNCTION and name in enclosing.builders:
                    return enclosing
                enclosing = enclosing.parent
        return scope


class _Recorder(ast.NodeVisitor):
    """Second pass over one file: attributes every read and write to the
    variable it reaches."""

    def __init__(self, project: _Project, relpath: str, module: _Scope) -> None:
        self.project = project
        self.resolver = project.resolver
        self.relpath = relpath
        self.scopes = project.scopes[relpath]
        self.scope = module

    def run(self, tree: ast.Module) -> None:
        for statement in tree.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for expression in (*node.decorator_list, *_defaults(node.args)):
            self.visit(expression)
        for expression in _annotations(node):
            self.visit(expression)
        self._enter(node, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._enter(node, node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in _defaults(node.args):
            self.visit(expression)
        self._enter(node, [node.body])

    def _comprehension(self, node: ast.expr) -> None:
        self.visit(node.generators[0].iter)
        outer = self.scope
        self.scope = self.scopes.get(id(node), outer)
        for index, generator in enumerate(node.generators):
            origin = f"loop over {_text(generator.iter)}"
            self._store(generator.target, origin)
            if index > 0:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scope = outer

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._store(target, _text(node.value), node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)
        if node.value is not None:
            self._store(node.target, _text(node.value), node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._read(node.target)
        text = f"{operator_symbol(node.op)}= {_text(node.value)}"
        self._store(node.target, text)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._store(node.target, _text(node.value), node.value)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._store(node.target, f"loop over {_text(node.iter)}")
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._store(
                    item.optional_vars, f"context of {_text(item.context_expr)}"
                )
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            caught = _text(node.type) if node.type is not None else "any exception"
            self._write_name(node.name, node, f"caught {caught}")
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._write_name(node.name, node, "match capture")
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._write_name(node.name, node, "match capture")

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._write_name(node.rest, node, "match capture")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        builder = self._lookup(node.id)
        if builder is not None:
            self._record(builder, _mode(node), node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        for builder in self._attribute_variables(node):
            self._record(builder, _mode(node), node)
        self.visit(node.value)

    def _enter(self, node: ast.AST, body: list) -> None:
        outer = self.scope
        self.scope = self.scopes.get(id(node), outer)
        for item in body:
            self.visit(item)
        self.scope = outer

    def _read(self, target: ast.expr) -> None:
        builders: list[_Builder] = []
        if isinstance(target, ast.Name):
            found = self._lookup(target.id)
            builders = [found] if found is not None else []
        elif isinstance(target, ast.Attribute):
            builders = self._attribute_variables(target)
        for builder in builders:
            self._record(builder, AccessMode.READ, target)

    def _store(
        self, target: ast.expr, text: str | None, value: ast.expr | None = None
    ) -> None:
        if isinstance(target, ast.Name):
            self._write_name(target.id, target, text)
        elif isinstance(target, (ast.Tuple, ast.List)):
            unpacked = _unpacked(value, len(target.elts))
            for element, item in zip(target.elts, unpacked, strict=True):
                origin = _text(item) if item is not None else f"unpacked from {text}"
                self._store(element, origin, item)
        elif isinstance(target, ast.Starred):
            self._store(target.value, f"unpacked from {text}")
        elif isinstance(target, ast.Attribute):
            for builder in self._attribute_variables(target):
                self._record(builder, AccessMode.WRITE, target, text)
            self.visit(target.value)
        else:
            self.visit(target)

    def _write_name(self, name: str, node: ast.AST, text: str | None) -> None:
        builder = self._resolve(name)
        if builder is not None:
            self._record(builder, AccessMode.WRITE, node, text)

    def _lookup(self, name: str) -> _Builder | None:
        builder = self._resolve(name)
        if builder is None:
            builder = self.project.imported(self.relpath, name)
        return builder

    def _resolve(self, name: str) -> _Builder | None:
        scope = self.scope
        if name in scope.globals:
            return scope.module_scope().builders.get(name)
        if name in scope.nonlocals:
            return self._enclosing(scope.parent, name)
        if name in scope.builders:
            return scope.builders[name]
        if name in scope.defs:
            return None
        return self._enclosing(scope.parent, name)

    def _enclosing(self, scope: _Scope | None, name: str) -> _Builder | None:
        while scope is not None:
            if scope.kind is not _ScopeKind.CLASS:
                found = scope.builders.get(name)
                if found is not None:
                    return found
                if name in scope.defs:
                    return None
            scope = scope.parent
        return None

    def _attribute_variables(self, node: ast.Attribute) -> list[_Builder]:
        base = node.value
        if isinstance(base, ast.Name):
            receiver = self.scope.receiver_info(base.id)
            if receiver is not None:
                return self.project.attribute_candidates(receiver[0], node.attr)
            if self._lookup(base.id) is not None:
                return []
        elif isinstance(base, ast.Attribute):
            root = _root(base)
            if root is None:
                return []
            if self.scope.receiver_info(root.id) or self._lookup(root.id) is not None:
                return []
        else:
            return []

        path = self.resolver.dotted(self.relpath, node, self.scope.symbol_scope())
        found = self.project.variable_at(path) if path else None
        return [found] if found is not None else []

    def _record(
        self,
        builder: _Builder,
        mode: AccessMode,
        node: ast.AST,
        value: str | None = None,
    ) -> None:
        if builder.is_receiver and mode is AccessMode.READ:
            return
        self.project.accesses.append(
            Access(
                variable=builder.qualname,
                mode=mode,
                relpath=self.relpath,
                lineno=node.lineno,
                col_offset=node.col_offset,
                scope=self.scope.container,
                value=value,
            )
        )


def build_variable_table(
    index: ProjectIndex, resolver: Resolver | None = None
) -> VariableTable:
    resolver = resolver or Resolver(index)
    project = _Project(index, resolver)

    modules: list[tuple[str, ast.Module, _Scope]] = []
    for info in index.table.files:
        tree = resolver.tree(info.relpath)
        if tree is not None:
            modules.append((info.relpath, tree, project.declare_module(info, tree)))

    for relpath, tree, scope in modules:
        _Recorder(project, relpath, scope).run(tree)
    return project.freeze()


def _kind_of(scope: _Scope) -> VariableKind:
    if scope.kind is _ScopeKind.MODULE:
        return VariableKind.MODULE
    if scope.kind is _ScopeKind.CLASS:
        return VariableKind.CLASS
    return VariableKind.LOCAL


def _anonymous(label: str, node: ast.AST) -> str:
    return f"<{label}@{node.lineno}:{node.col_offset}>"


def _defaults(args: ast.arguments) -> list[ast.expr]:
    return [*args.defaults, *(item for item in args.kw_defaults if item is not None)]


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    args = node.args
    every = [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        *filter(None, (args.vararg, args.kwarg)),
    ]
    found = [arg.annotation for arg in every if arg.annotation is not None]
    if node.returns is not None:
        found.append(node.returns)
    return found


def _unpacked(value: ast.expr | None, count: int) -> list[ast.expr | None]:
    if (
        isinstance(value, (ast.Tuple, ast.List))
        and len(value.elts) == count
        and not any(isinstance(item, ast.Starred) for item in value.elts)
    ):
        return list(value.elts)
    return [None] * count


def _exception_type(node: ast.expr | None) -> str | None:
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.unparse(node)
    if isinstance(node, ast.Tuple):
        names = [ast.unparse(item) for item in node.elts]
        return " | ".join(names)
    return None


def _root(node: ast.expr) -> ast.Name | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node if isinstance(node, ast.Name) else None


def _mode(node: ast.Name | ast.Attribute) -> AccessMode:
    return AccessMode.READ if isinstance(node.ctx, ast.Load) else AccessMode.WRITE


def _text(node: ast.expr) -> str:
    flat = " ".join(ast.unparse(node).split())
    return flat if len(flat) <= _TEXT_LIMIT else flat[: _TEXT_LIMIT - 3] + "..."

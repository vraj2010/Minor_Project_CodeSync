"""Resolving names in project code to the symbols they refer to."""

import ast
from dataclasses import dataclass, field

from .index import ProjectIndex
from .parsing import read_source
from .symbols import ModuleInfo, Symbol, SymbolKind

_Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
_MAX_ALIAS_DEPTH = 20


@dataclass
class ImportTable:
    bindings: dict[str, str] = field(default_factory=dict)
    star_modules: list[str] = field(default_factory=list)


class Resolver:
    """Answers "what does this expression refer to?" for code in an indexed
    project. Imports and re-exports are followed, `self`, `cls` and `super()`
    are understood inside a class, and anything that depends on a runtime value
    is left unresolved rather than guessed."""

    def __init__(self, index: ProjectIndex) -> None:
        self.index = index
        self.table = index.table
        self._sources: dict[str, str] = {}
        self._trees: dict[str, ast.Module | None] = {}
        self._imports: dict[str, ImportTable] = {}
        self._nodes: dict[str, dict[tuple[int, str], _Definition]] = {}
        self._symbols: dict[str, dict[tuple[int, str], Symbol]] = {}
        self._bases: dict[Symbol, list[str]] = {}

    def source(self, relpath: str) -> str:
        if relpath not in self._sources:
            try:
                self._sources[relpath] = read_source(self.index.root / relpath)
            except (OSError, UnicodeDecodeError, SyntaxError, LookupError):
                self._sources[relpath] = ""
        return self._sources[relpath]

    def tree(self, relpath: str) -> ast.Module | None:
        if relpath not in self._trees:
            try:
                self._trees[relpath] = ast.parse(self.source(relpath), filename=relpath)
            except (SyntaxError, ValueError, RecursionError):
                self._trees[relpath] = None
        return self._trees[relpath]

    def node_for(self, symbol: Symbol) -> _Definition | None:
        if symbol.relpath not in self._nodes:
            tree = self.tree(symbol.relpath)
            self._nodes[symbol.relpath] = {
                (node.lineno, node.name): node
                for node in (ast.walk(tree) if tree is not None else ())
                if isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
            }
        return self._nodes[symbol.relpath].get((symbol.position.lineno, symbol.name))

    def symbol_for(self, relpath: str, node: _Definition) -> Symbol | None:
        if relpath not in self._symbols:
            info = self.table.module_at(relpath)
            symbols = self.table.symbols_in(info.name) if info is not None else []
            self._symbols[relpath] = {
                (symbol.position.lineno, symbol.name): symbol
                for symbol in symbols
                if symbol.relpath == relpath and symbol.kind is not SymbolKind.VARIABLE
            }
        return self._symbols[relpath].get((node.lineno, node.name))

    def imports(self, relpath: str) -> ImportTable:
        if relpath not in self._imports:
            self._imports[relpath] = self._read_imports(relpath)
        return self._imports[relpath]

    def dotted(
        self, relpath: str, expr: ast.expr, scope: Symbol | None = None
    ) -> str | None:
        """The project-wide dotted path `expr` names, or None when it cannot be
        known without running the code."""
        if isinstance(expr, ast.Name):
            return self._name(relpath, expr.id, scope)
        if isinstance(expr, ast.Attribute):
            owner = self._owner(relpath, expr.value, scope)
            return f"{owner}.{expr.attr}" if owner else None
        return None

    def canonical(self, dotted: str) -> Symbol | None:
        return self._canonical(dotted, 0)

    def member(
        self, cls: Symbol, name: str, seen: set[str] | None = None
    ) -> Symbol | None:
        """Look `name` up on a class the way attribute access would: on the
        class itself first, then along its base classes."""
        seen = set() if seen is None else seen
        if cls.qualname in seen:
            return None
        seen.add(cls.qualname)

        found = self.table.lookup(f"{cls.qualname}.{name}")
        if found is not None:
            return found
        for base in self.bases(cls):
            base_class = self.canonical(base)
            if base_class is not None and base_class.kind is SymbolKind.CLASS:
                found = self.member(base_class, name, seen)
                if found is not None:
                    return found
        return None

    def bases(self, cls: Symbol) -> list[str]:
        if cls not in self._bases:
            node = self.node_for(cls)
            ancestors = self.table.ancestors(cls)
            outer = ancestors[-1] if ancestors else None
            self._bases[cls] = [
                path
                for base in (node.bases if isinstance(node, ast.ClassDef) else ())
                if (path := self.dotted(cls.relpath, base, outer)) is not None
            ]
        return self._bases[cls]

    def enclosing_class(self, scope: Symbol | None) -> Symbol | None:
        for symbol in self._chain(scope):
            if symbol.kind is SymbolKind.CLASS:
                return symbol
        return None

    def _canonical(self, dotted: str, depth: int) -> Symbol | None:
        if depth > _MAX_ALIAS_DEPTH:
            return None
        symbol = self.table.lookup(dotted)
        if symbol is not None:
            return symbol

        rewritten = self._follow_import(dotted)
        if rewritten is not None and rewritten != dotted:
            return self._canonical(rewritten, depth + 1)

        owner_path, _, attribute = dotted.rpartition(".")
        if not owner_path:
            return None
        owner = self._canonical(owner_path, depth + 1)
        if owner is None or owner.kind is not SymbolKind.CLASS:
            return None
        return self.member(owner, attribute)

    def _follow_import(self, dotted: str) -> str | None:
        """Rewrite a path that passes through a name some module imported, so
        that `inventory.Stock` becomes `inventory.models.Stock`."""
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            info = self.table.module(".".join(parts[:cut]))
            if info is None:
                continue
            imports = self.imports(info.relpath)
            name, rest = parts[cut], parts[cut + 1 :]
            target = imports.bindings.get(name)
            if target is not None:
                return ".".join([target, *rest])
            for module in imports.star_modules:
                candidate = f"{module}.{name}"
                if self.table.lookup(candidate) or self.table.module(candidate):
                    return ".".join([candidate, *rest])
            return None
        return None

    def _name(self, relpath: str, name: str, scope: Symbol | None) -> str | None:
        for symbol in self._chain(scope):
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                local = f"{symbol.qualname}.{name}"
                if self.table.lookup(local) is not None:
                    return local

        imports = self.imports(relpath)
        if name in imports.bindings:
            return imports.bindings[name]

        info = self.table.module_at(relpath)
        if info is not None and self.table.lookup(f"{info.name}.{name}") is not None:
            return f"{info.name}.{name}"

        for module in imports.star_modules:
            if self.canonical(f"{module}.{name}") is not None:
                return f"{module}.{name}"
        return None

    def _owner(self, relpath: str, value: ast.expr, scope: Symbol | None) -> str | None:
        if isinstance(value, ast.Name) and value.id in ("self", "cls"):
            cls = self.enclosing_class(scope)
            if cls is not None:
                return cls.qualname

        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and value.func.id == "super":
                cls = self.enclosing_class(scope)
                bases = self.bases(cls) if cls is not None else []
                return bases[0] if bases else None
            created = self.dotted(relpath, value.func, scope)
            symbol = self.canonical(created) if created else None
            if symbol is not None and symbol.kind is SymbolKind.CLASS:
                return symbol.qualname
            return None

        return self.dotted(relpath, value, scope)

    def _chain(self, scope: Symbol | None) -> list[Symbol]:
        """`scope` and the symbols around it, innermost first."""
        if scope is None:
            return []
        return [scope, *reversed(self.table.ancestors(scope))]

    def _read_imports(self, relpath: str) -> ImportTable:
        table = ImportTable()
        info = self.table.module_at(relpath)
        tree = self.tree(relpath)
        if info is None or tree is None:
            return table

        statements = sorted(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for statement in statements:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.asname:
                        table.bindings[alias.asname] = alias.name
                    else:
                        top = alias.name.partition(".")[0]
                        table.bindings[top] = top
                continue

            base = absolute_module(info, statement.module, statement.level)
            if base is None:
                continue
            for alias in statement.names:
                if alias.name == "*":
                    table.star_modules.append(base)
                else:
                    table.bindings[alias.asname or alias.name] = f"{base}.{alias.name}"
        return table


def absolute_module(info: ModuleInfo, module: str | None, level: int) -> str | None:
    """The absolute module a `from` import names, or None when a relative
    import climbs above the top-level package."""
    if level == 0:
        return module

    package = info.name.split(".")
    if not info.is_package:
        package = package[:-1]
    climb = level - 1
    if climb >= len(package):
        return None

    parts = package[: len(package) - climb]
    if module:
        parts.append(module)
    return ".".join(parts)

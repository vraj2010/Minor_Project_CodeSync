"""Does everything a rewrite refers to actually exist?

A rewrite that parses can still use a name nobody defined, read an attribute the
class does not have, or call a project function the wrong way. All of that fails
only when the line runs, which may be nowhere near a test. Each of these checks
looks only at what the rewrite newly introduced, so a problem the original code
already had is not blamed on it."""

import ast
import builtins
import symtable
import textwrap

from .context import instance_attributes
from .contract import bind, own_nodes, snapshot
from .findings import Check, Finding
from .resolve import Resolver, absolute_module
from .symbols import Symbol, SymbolKind

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_MODULE_NAMES = frozenset(
    {"__name__", "__file__", "__doc__", "__package__", "__spec__", "__path__"}
)


def check_names(
    resolver: Resolver,
    target: Symbol,
    before_node: ast.FunctionDef | ast.AsyncFunctionDef,
    after_node: ast.FunctionDef | ast.AsyncFunctionDef,
    rewrite_source: str,
    patched: str,
    added_imports: tuple[str, ...],
) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_undefined_names(target, before_node, rewrite_source, patched))
    findings.extend(_missing_self_attributes(resolver, target, before_node, after_node))
    findings.extend(_missing_project_names(resolver, target, before_node, after_node))
    findings.extend(_calls_to_project_code(resolver, target, before_node, after_node))
    findings.extend(_bad_imports(resolver, target, added_imports))
    return findings


def _finding(target: Symbol, code: str, message: str, text: str | None = None) -> Finding:
    return Finding(
        Check.NAMES, code, message, target.relpath, target.position.lineno, text
    )


def _undefined_names(
    target: Symbol,
    before_node: ast.AST,
    rewrite_source: str,
    patched: str,
) -> list[Finding]:
    module = ast.parse(patched)
    if any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(module)
    ):
        # A star import brings in names that cannot be listed from here.
        return []

    defined = _module_names(patched)
    known = defined | _MODULE_NAMES | set(dir(builtins))
    already_used = _free_names(textwrap.dedent(ast.unparse(before_node)))

    return [
        _finding(
            target,
            "undefined-name",
            f"`{name}` is not defined or imported in {target.relpath}",
        )
        for name in sorted(_free_names(rewrite_source) - known - already_used)
    ]


def _module_names(source: str) -> set[str]:
    table = symtable.symtable(source, "patched", "exec")
    return {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }


def _free_names(source: str) -> set[str]:
    """Names a function reads from outside itself, nested scopes included."""
    names: set[str] = set()
    pending = list(symtable.symtable(source, "rewrite", "exec").get_children())
    while pending:
        table = pending.pop()
        pending.extend(table.get_children())
        for symbol in table.get_symbols():
            if symbol.is_referenced() and symbol.is_global():
                names.add(symbol.get_name())
    return names


def _missing_self_attributes(
    resolver: Resolver,
    target: Symbol,
    before_node: ast.AST,
    after_node: ast.AST,
) -> list[Finding]:
    if target.kind is not SymbolKind.METHOD:
        return []
    cls = resolver.enclosing_class(target)
    if cls is None:
        return []
    known = _class_attributes(resolver, cls)
    if known is None:
        return []

    used_before = _self_reads(before_node)
    written_by_rewrite = _self_writes(after_node)
    findings = []
    for name, text in sorted(_self_reads(after_node).items()):
        if name in known or name in used_before or name in written_by_rewrite:
            continue
        findings.append(
            _finding(
                target,
                "missing-attribute",
                f"`self.{name}` is not an attribute of `{cls.name}`",
                text,
            )
        )
    return findings


def _class_attributes(resolver: Resolver, cls: Symbol) -> set[str] | None:
    """Every attribute a class and its bases have, or None if a base lies
    outside the project, since then nothing can be said about what is missing."""
    names: set[str] = set(dir(object))
    pending = [cls]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current.qualname in seen:
            continue
        seen.add(current.qualname)

        node = resolver.node_for(current)
        if not isinstance(node, ast.ClassDef):
            return None
        if any(_is_dynamic(item) for item in node.body):
            return None
        if node.bases and len(resolver.bases(current)) != len(node.bases):
            return None

        names.update(item.name for item in resolver.table.children_of(current.qualname))
        names.update(instance_attributes(node))
        for path in resolver.bases(current):
            base = resolver.canonical(path)
            if base is None or base.kind is not SymbolKind.CLASS:
                return None
            pending.append(base)
    return names


def _is_dynamic(statement: ast.stmt) -> bool:
    return isinstance(statement, _FUNCTIONS) and statement.name in (
        "__getattr__",
        "__getattribute__",
    )


def _self_reads(node: ast.AST) -> dict[str, str]:
    found = {}
    for item in ast.walk(node):
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.ctx, ast.Load)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
        ):
            found[item.attr] = f"self.{item.attr}"
    return found


def _self_writes(node: ast.AST) -> set[str]:
    return {
        item.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Attribute)
        and isinstance(item.ctx, ast.Store)
        and isinstance(item.value, ast.Name)
        and item.value.id == "self"
    }


def _missing_project_names(
    resolver: Resolver,
    target: Symbol,
    before_node: ast.AST,
    after_node: ast.AST,
) -> list[Finding]:
    """`helpers.missing()` where `helpers` is a project module."""
    used_before = {
        ast.unparse(item)
        for item in ast.walk(before_node)
        if isinstance(item, ast.Attribute)
    }
    findings = []
    seen = set()
    for item in ast.walk(after_node):
        if not isinstance(item, ast.Attribute) or not isinstance(item.ctx, ast.Load):
            continue
        text = ast.unparse(item)
        if text in used_before or text in seen:
            continue
        seen.add(text)

        path = resolver.dotted(target.relpath, item, target)
        if path is None:
            continue
        owner = path.rpartition(".")[0]
        if resolver.table.module(owner) is None:
            continue
        if resolver.canonical(path) is None and resolver.table.module(path) is None:
            findings.append(
                _finding(
                    target,
                    "missing-project-name",
                    f"`{owner}` has nothing called `{item.attr}`",
                    text,
                )
            )
    return findings


def _calls_to_project_code(
    resolver: Resolver,
    target: Symbol,
    before_node: ast.AST,
    after_node: ast.AST,
) -> list[Finding]:
    """A call the rewrite added to a project function must fit that function."""
    used_before = {
        ast.unparse(item) for item in own_nodes(before_node) if isinstance(item, ast.Call)
    }
    findings = []
    for call in own_nodes(after_node):
        if not isinstance(call, ast.Call) or ast.unparse(call) in used_before:
            continue
        path = resolver.dotted(target.relpath, call.func, target)
        symbol = resolver.canonical(path) if path else None
        if symbol is None or symbol == target:
            continue

        skip_first = symbol.kind is SymbolKind.CLASS or (
            symbol.kind is SymbolKind.METHOD
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in ("self", "cls")
        )
        definition = symbol
        if symbol.kind is SymbolKind.CLASS:
            found = resolver.member(symbol, "__init__")
            if found is None:
                continue
            definition = found
        elif symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            continue
        if definition.kind is SymbolKind.METHOD and not skip_first:
            continue

        node = resolver.node_for(definition)
        if not isinstance(node, _FUNCTIONS):
            continue
        problem = bind(snapshot(node), call, skip_first=skip_first)
        if problem is not None:
            findings.append(
                _finding(
                    target,
                    "bad-call",
                    f"the call to `{symbol.qualname}` {problem}",
                    ast.unparse(call),
                )
            )
    return findings


def _bad_imports(
    resolver: Resolver, target: Symbol, added_imports: tuple[str, ...]
) -> list[Finding]:
    info = resolver.table.module_at(target.relpath)
    if info is None:
        return []

    findings = []
    for text in added_imports:
        statement = ast.parse(text).body[0]
        if not isinstance(statement, ast.ImportFrom):
            continue
        base = absolute_module(info, statement.module, statement.level)
        if base is None or resolver.table.module(base) is None:
            continue
        for alias in statement.names:
            path = f"{base}.{alias.name}"
            if (
                alias.name != "*"
                and resolver.canonical(path) is None
                and resolver.table.module(path) is None
            ):
                findings.append(
                    _finding(
                        target,
                        "bad-import",
                        f"`{base}` has nothing called `{alias.name}` to import",
                        text,
                    )
                )
    return findings

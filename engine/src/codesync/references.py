"""Finding where a symbol is used, and what a symbol itself calls."""

import ast
from dataclasses import dataclass
from enum import Enum

from .resolve import Resolver, absolute_module
from .symbols import Symbol, SymbolKind


class Confidence(str, Enum):
    RESOLVED = "resolved"
    NAME_ONLY = "name-only"


class UsageKind(str, Enum):
    CALL = "call"
    REFERENCE = "reference"
    IMPORT = "import"


@dataclass(frozen=True)
class Reference:
    relpath: str
    lineno: int
    end_lineno: int
    col_offset: int
    scope: Symbol | None
    kind: UsageKind
    confidence: Confidence
    text: str

    @property
    def location(self) -> str:
        return f"{self.relpath}:{self.lineno}"

    @property
    def summary(self) -> str:
        """The referencing expression on one line, shortened for listings."""
        flat = " ".join(self.text.split())
        return flat if len(flat) <= 100 else flat[:97] + "..."

    def to_dict(self) -> dict:
        return {
            "path": self.relpath,
            "lineno": self.lineno,
            "end_lineno": self.end_lineno,
            "scope": self.scope.qualname if self.scope else None,
            "kind": self.kind.value,
            "confidence": self.confidence.value,
            "text": self.text,
        }


@dataclass(frozen=True)
class Callee:
    symbol: Symbol
    count: int


def find_references(resolver: Resolver, target: Symbol) -> list[Reference]:
    """Every place in the project that refers to `target`.

    A reference that resolves statically is reported as such. For a method,
    a call on a value whose type is unknown (`stock.value()`) cannot be
    resolved; it is reported as a name-only match rather than dropped, because
    for safety checking a false alarm is cheaper than a missed caller."""
    wanted = {target} | _constructed_classes(resolver, target)
    names = _names_for(resolver, wanted)

    found: list[Reference] = []
    for info in resolver.table.files:
        if not _mentions(resolver.source(info.relpath), names):
            continue
        tree = resolver.tree(info.relpath)
        if tree is None:
            continue
        scan = _ReferenceScan(resolver, info.relpath, target, wanted, names)
        scan.visit(tree)
        found.extend(scan.found)
    return sorted(found, key=lambda item: (item.relpath, item.lineno, item.col_offset))


def find_callees(resolver: Resolver, target: Symbol) -> list[Callee]:
    """Project symbols called from inside `target`, most called first. Calls to
    functions nested in `target` are left out, since they are part of it."""
    node = resolver.node_for(target)
    if node is None:
        return []

    counts: dict[Symbol, int] = {}
    for statement in node.body:
        for call in ast.walk(statement):
            if not isinstance(call, ast.Call):
                continue
            path = resolver.dotted(target.relpath, call.func, target)
            symbol = resolver.canonical(path) if path else None
            if (
                symbol is None
                or symbol == target
                or symbol.kind is SymbolKind.VARIABLE
                or symbol.qualname.startswith(target.qualname + ".")
            ):
                continue
            counts[symbol] = counts.get(symbol, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: -item[1])
    return [Callee(symbol, count) for symbol, count in ranked]


def _constructed_classes(resolver: Resolver, target: Symbol) -> set[Symbol]:
    """For an `__init__`, the classes whose instantiation runs it: the class it
    is defined on and any subclass that does not define its own."""
    if target.name != "__init__" or target.kind is not SymbolKind.METHOD:
        return set()
    return {
        symbol
        for symbol in resolver.table.symbols
        if symbol.kind is SymbolKind.CLASS
        and resolver.member(symbol, "__init__") == target
    }


def _names_for(resolver: Resolver, wanted: set[Symbol]) -> set[str]:
    """Names that may refer to a wanted symbol: its own name plus any alias an
    import gives it, followed through chains of re-exports."""
    names = {symbol.name for symbol in wanted}
    checked: set[str] = set()
    grew = True
    while grew:
        grew = False
        for info in resolver.table.files:
            if info.relpath in checked or not _mentions(
                resolver.source(info.relpath), names
            ):
                continue
            checked.add(info.relpath)
            for local, path in resolver.imports(info.relpath).bindings.items():
                if local not in names and resolver.canonical(path) in wanted:
                    names.add(local)
                    grew = True
    return names


def _mentions(source: str, names: set[str]) -> bool:
    return any(name in source for name in names)


class _ReferenceScan(ast.NodeVisitor):
    def __init__(
        self,
        resolver: Resolver,
        relpath: str,
        target: Symbol,
        wanted: set[Symbol],
        names: set[str],
    ) -> None:
        self.resolver = resolver
        self.relpath = relpath
        self.source = resolver.source(relpath)
        self.target = target
        self.classes = wanted - {target}
        self.names = names
        self.found: list[Reference] = []
        self._scope: list[Symbol] = []
        self._calls: dict[int, ast.Call] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._enter(node, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._calls[id(node.func)] = node
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.names and isinstance(node.ctx, ast.Load):
            self._consider(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.names and isinstance(node.ctx, ast.Load):
            self._consider(node)
        self.visit(node.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        info = self.resolver.table.module_at(self.relpath)
        base = absolute_module(info, node.module, node.level) if info else None
        if base is None:
            return
        for alias in node.names:
            if alias.name not in self.names:
                continue
            if self.resolver.canonical(f"{base}.{alias.name}") == self.target:
                self._record(node, UsageKind.IMPORT, Confidence.RESOLVED)
                return

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._enter(node, node.body)

    def _enter(self, node: ast.AST, body: list[ast.stmt]) -> None:
        symbol = self.resolver.symbol_for(self.relpath, node)
        if symbol is not None:
            self._scope.append(symbol)
        for statement in body:
            self.visit(statement)
        if symbol is not None:
            self._scope.pop()

    def _consider(self, node: ast.Name | ast.Attribute) -> None:
        scope = self._scope[-1] if self._scope else None
        call = self._calls.get(id(node))
        path = self.resolver.dotted(self.relpath, node, scope)
        symbol = self.resolver.canonical(path) if path else None

        if symbol == self.target or (symbol in self.classes and call is not None):
            confidence = Confidence.RESOLVED
        elif (
            path is None
            and isinstance(node, ast.Attribute)
            and node.attr == self.target.name
            and self.target.kind is SymbolKind.METHOD
        ):
            confidence = Confidence.NAME_ONLY
        else:
            return

        kind = UsageKind.CALL if call is not None else UsageKind.REFERENCE
        self._record(call or node, kind, confidence)

    def _record(self, node: ast.AST, kind: UsageKind, confidence: Confidence) -> None:
        self.found.append(
            Reference(
                relpath=self.relpath,
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                col_offset=node.col_offset,
                scope=self._scope[-1] if self._scope else None,
                kind=kind,
                confidence=confidence,
                text=ast.get_source_segment(self.source, node) or "",
            )
        )

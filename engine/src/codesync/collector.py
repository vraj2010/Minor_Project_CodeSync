"""Walking a parsed module into a flat list of symbols."""

import ast

from .symbols import Position, Symbol, SymbolKind

_Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def collect_symbols(tree: ast.Module, module: str, relpath: str) -> list[Symbol]:
    collector = _Collector(module, relpath)
    collector.visit(tree)
    return collector.symbols


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, relpath: str) -> None:
        self.module = module
        self.relpath = relpath
        self.symbols: list[Symbol] = []
        self._scope: list[Symbol] = []
        self._function_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self._definition(node, SymbolKind.CLASS)
        self._descend(symbol, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node, is_async=True)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self._binds_names():
            return
        for target in node.targets:
            self._variables(target, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._binds_names():
            self._variables(node.target, node)

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        parent = self._parent()
        kind = (
            SymbolKind.METHOD
            if parent is not None and parent.kind is SymbolKind.CLASS
            else SymbolKind.FUNCTION
        )
        symbol = self._definition(node, kind, is_async=is_async)
        self._function_depth += 1
        self._descend(symbol, node.body)
        self._function_depth -= 1

    def _descend(self, symbol: Symbol, body: list[ast.stmt]) -> None:
        self._scope.append(symbol)
        for statement in body:
            self.visit(statement)
        self._scope.pop()

    def _definition(
        self, node: _Definition, kind: SymbolKind, *, is_async: bool = False
    ) -> Symbol:
        parent = self._parent()
        decorators = tuple(ast.unparse(item) for item in node.decorator_list)
        symbol = Symbol(
            qualname=self._qualname(node.name),
            name=node.name,
            kind=kind,
            module=self.module,
            relpath=self.relpath,
            position=_position(node),
            parent=parent.qualname if parent is not None else None,
            decorators=decorators,
            decorator_lineno=(
                min(item.lineno for item in node.decorator_list)
                if node.decorator_list
                else None
            ),
            is_async=is_async,
            is_nested=self._function_depth > 0,
        )
        self.symbols.append(symbol)
        return symbol

    def _variables(self, target: ast.expr, statement: ast.stmt) -> None:
        if isinstance(target, ast.Name):
            parent = self._parent()
            self.symbols.append(
                Symbol(
                    qualname=self._qualname(target.id),
                    name=target.id,
                    kind=SymbolKind.VARIABLE,
                    module=self.module,
                    relpath=self.relpath,
                    position=_position(statement),
                    parent=parent.qualname if parent is not None else None,
                )
            )
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._variables(element, statement)
        elif isinstance(target, ast.Starred):
            self._variables(target.value, statement)

    def _qualname(self, name: str) -> str:
        return ".".join([self.module, *(item.name for item in self._scope), name])

    def _parent(self) -> Symbol | None:
        return self._scope[-1] if self._scope else None

    def _binds_names(self) -> bool:
        """Assignments are recorded where they create a namespace entry: at
        module level and in a class body, but not inside a function."""
        parent = self._parent()
        return parent is None or parent.kind is SymbolKind.CLASS


def _position(node: ast.stmt) -> Position:
    return Position(
        lineno=node.lineno,
        end_lineno=node.lineno if node.end_lineno is None else node.end_lineno,
        col_offset=node.col_offset,
        end_col_offset=(
            node.col_offset if node.end_col_offset is None else node.end_col_offset
        ),
    )

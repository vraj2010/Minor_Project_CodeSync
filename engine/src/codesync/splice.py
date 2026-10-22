"""Putting a rewritten definition back into its file.

The edit is made on the file's lines, not by regenerating it from a syntax
tree, so everything outside the definition (comments, spacing, line endings)
stays exactly as it was and the diff shows only what the model changed."""

import ast
import difflib
import io
import textwrap
from dataclasses import dataclass

from .response import Rewrite
from .symbols import Symbol

_NO_NEWLINE = "\\ No newline at end of file\n"


class SpliceError(Exception):
    pass


@dataclass(frozen=True)
class Patch:
    relpath: str
    original: str
    patched: str
    added_imports: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.original != self.patched

    def diff(self, context: int = 3) -> str:
        lines = difflib.unified_diff(
            _lines(self.original),
            _lines(self.patched),
            f"a/{self.relpath}",
            f"b/{self.relpath}",
            n=context,
        )
        out = []
        for line in lines:
            out.append(line)
            if not line.endswith(("\n", "\r")):
                out.append("\n" + _NO_NEWLINE)
        return "".join(out)

    def line_counts(self) -> tuple[int, int]:
        """Lines added and removed."""
        added = removed = 0
        for line in self.diff(context=0).splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return added, removed


def splice(source: str, symbol: Symbol, rewrite: Rewrite) -> Patch:
    """Replace `symbol`'s definition in `source` with `rewrite`, adding any
    import the rewrite needs that the module does not already have."""
    lines = _lines(source)
    newline = "\r\n" if "\r\n" in source else "\n"
    start, end = symbol.start_lineno, symbol.position.end_lineno
    _check_definition(lines, symbol)

    first = lines[start - 1]
    indent = first[: len(first) - len(first.lstrip(" \t"))]
    replacement = [
        (indent + line if line.strip() else "") + newline
        for line in rewrite.source.split("\n")
    ]

    module = _parse(source, symbol.relpath)
    imports = _new_imports(module, rewrite.imports)
    edits = [(start - 1, end, replacement)]
    if imports:
        at = _import_line(module)
        edits.append((at, at, [text + newline for text in imports]))

    for begin, stop, new_lines in sorted(edits, key=lambda edit: edit[0], reverse=True):
        lines[begin:stop] = new_lines

    patched = "".join(lines)
    try:
        ast.parse(patched)
    except SyntaxError as error:
        raise SpliceError(
            f"{symbol.relpath} does not parse after the edit "
            f"(line {error.lineno}: {error.msg})"
        ) from error
    return Patch(symbol.relpath, source, patched, tuple(imports))


def _lines(text: str) -> list[str]:
    return io.StringIO(text, newline="").readlines()


def _parse(source: str, relpath: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise SpliceError(f"{relpath} does not parse as it is") from error


def _check_definition(lines: list[str], symbol: Symbol) -> None:
    """Make sure the lines the index points at still hold the definition, so a
    file edited since it was indexed is refused instead of corrupted."""
    start, end = symbol.start_lineno, symbol.position.end_lineno
    if end > len(lines):
        raise SpliceError(f"{symbol.relpath} is shorter than the index expects")

    block = "".join(lines[start - 1 : end]).replace("\r\n", "\n")
    try:
        body = ast.parse(textwrap.dedent(block)).body
    except SyntaxError:
        body = []
    if not (
        len(body) == 1
        and isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
        and body[0].name == symbol.name
    ):
        raise SpliceError(
            f"lines {start}-{end} of {symbol.relpath} no longer hold `{symbol.name}`"
        )


def _new_imports(module: ast.Module, imports: tuple[str, ...]) -> list[str]:
    """The import statements from a rewrite that bind something the module
    does not already import."""
    existing = {
        binding
        for statement in module.body
        if isinstance(statement, (ast.Import, ast.ImportFrom))
        for binding in _bindings(statement)
    }
    needed = []
    for text in imports:
        statement = ast.parse(text).body[0]
        bindings = _bindings(statement)
        if not bindings <= existing:
            needed.append(text)
            existing |= bindings
    return needed


def _bindings(statement: ast.Import | ast.ImportFrom) -> set[tuple]:
    if isinstance(statement, ast.Import):
        return {("import", alias.name, alias.asname) for alias in statement.names}
    return {
        ("from", statement.level, statement.module, alias.name, alias.asname)
        for alias in statement.names
    }


def _import_line(module: ast.Module) -> int:
    """Index of the line a new import goes before: after the imports at the
    top of the module, or after its docstring when it has none."""
    position = 0
    for index, statement in enumerate(module.body):
        is_import = isinstance(statement, (ast.Import, ast.ImportFrom))
        is_docstring = (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
        )
        if not (is_import or is_docstring):
            break
        position = statement.end_lineno or statement.lineno
    return position

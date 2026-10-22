"""Turning a model reply into a definition that can be spliced in."""

import ast
import re
import textwrap
from dataclasses import dataclass

_FENCE = re.compile(
    r"^[ \t]*```[ \t]*([\w+.-]*)[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE
)
_PYTHON_TAGS = frozenset({"", "python", "py", "python3"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class ResponseError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Rewrite:
    name: str
    source: str
    imports: tuple[str, ...] = ()
    is_async: bool = False


def parse_rewrite(reply: str, *, name: str, owner: str | None = None) -> Rewrite:
    """Find the rewritten definition of `name` in a model reply.

    A reply may hold several code blocks, such as the old code, the new code
    and an example; the last block that defines `name` is the answer. A method
    sent back inside its class, named by `owner`, is taken out of the class.
    A block whose only function has another name is accepted as a rename, which
    is for the safety checks to judge, not the parser."""
    text = reply.replace("\r\n", "\n")
    blocks = code_blocks(text)
    if not blocks:
        raise ResponseError("no-code", "the reply has no code block")

    parsed: list[tuple[str, ast.Module]] = []
    problems: list[str] = []
    for block in blocks:
        try:
            parsed.append((block, ast.parse(block)))
        except SyntaxError as error:
            problems.append(f"line {error.lineno}: {error.msg}")
    if not parsed:
        if "```" not in text:
            raise ResponseError("no-code", "the reply has no code block")
        raise ResponseError(
            "syntax", f"the code in the reply does not parse ({problems[-1]})"
        )

    matches = [
        (block, tree, found)
        for block, tree in parsed
        if (found := _locate(tree, name, owner)) is not None
    ]
    if not matches:
        matches = [
            (block, tree, found)
            for block, tree in parsed
            if (found := _only_function(tree)) is not None
        ]
        if len(matches) != 1:
            raise ResponseError(
                "no-definition", f"the reply does not contain a definition of `{name}`"
            )

    block, tree, (others, definition) = matches[-1]
    _reject_extra_code(others)
    return Rewrite(
        name=definition.name,
        source=_definition_text(block, definition),
        imports=tuple(
            ast.get_source_segment(block, statement) or ""
            for statement in tree.body
            if isinstance(statement, (ast.Import, ast.ImportFrom))
        ),
        is_async=isinstance(definition, ast.AsyncFunctionDef),
    )


def code_blocks(text: str) -> list[str]:
    """The Python code blocks in a reply. A reply with no fences at all is
    taken to be bare code, and a block left open because the reply was cut
    off still counts."""
    fenced = _FENCE.findall(text)
    if fenced:
        return [
            textwrap.dedent(body)
            for language, body in fenced
            if language.lower() in _PYTHON_TAGS
        ]
    if "```" in text:
        _, _, rest = text.partition("```")
        _, _, body = rest.partition("\n")
        return [textwrap.dedent(body)] if body.strip() else []
    return [text] if text.strip() else []


def _locate(
    tree: ast.Module, name: str, owner: str | None
) -> tuple[list[ast.stmt], ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """The definition of `name` and the statements that came with it."""
    for statement in tree.body:
        if isinstance(statement, _FUNCTIONS) and statement.name == name:
            return [other for other in tree.body if other is not statement], statement

    if owner is None:
        return None
    for statement in tree.body:
        if not (isinstance(statement, ast.ClassDef) and statement.name == owner):
            continue
        for member in statement.body:
            if isinstance(member, _FUNCTIONS) and member.name == name:
                others = [other for other in tree.body if other is not statement]
                others += [other for other in statement.body if other is not member]
                return others, member
    return None


def _only_function(
    tree: ast.Module,
) -> tuple[list[ast.stmt], ast.FunctionDef | ast.AsyncFunctionDef] | None:
    functions = [
        statement for statement in tree.body if isinstance(statement, _FUNCTIONS)
    ]
    if len(functions) != 1:
        return None
    return [other for other in tree.body if other is not functions[0]], functions[0]


def _reject_extra_code(statements: list[ast.stmt]) -> None:
    extra = []
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom)) or _is_string(statement):
            continue
        if isinstance(statement, (*_FUNCTIONS, ast.ClassDef)):
            extra.append(f"`{statement.name}` on line {statement.lineno}")
        else:
            extra.append(f"a statement on line {statement.lineno}")
    if extra:
        raise ResponseError(
            "extra-code",
            "the code block holds more than the definition: " + ", ".join(extra),
        )


def _is_string(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _definition_text(
    block: str, definition: ast.FunctionDef | ast.AsyncFunctionDef
) -> str:
    lines = block.split("\n")
    start = min(
        [definition.lineno, *(item.lineno for item in definition.decorator_list)]
    )
    end = definition.end_lineno or definition.lineno
    return textwrap.dedent("\n".join(lines[start - 1 : end])).strip("\n")

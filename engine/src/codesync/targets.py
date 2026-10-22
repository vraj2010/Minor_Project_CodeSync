"""Turning what the user typed into the symbol they meant."""

import re
from pathlib import Path

from .index import ProjectIndex
from .symbols import Symbol, SymbolKind

_LOCATION = re.compile(r"^.+\.py:\d+$")
_REFINABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD)


class TargetError(ValueError):
    pass


def parse_location(value: str, root: Path) -> tuple[str, int]:
    text, separator, line = value.rpartition(":")
    if not separator or not line.isdigit():
        raise TargetError(f"expected PATH:LINE, got {value!r}")

    path = Path(text.replace("\\", "/"))
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root)
        except ValueError:
            raise TargetError(f"{text} is outside {root}") from None
    return path.as_posix(), int(line)


def is_location(spec: str) -> bool:
    return bool(_LOCATION.match(spec.replace("\\", "/")))


def find_symbol(index: ProjectIndex, spec: str) -> Symbol:
    """The symbol a qualified name or a `path:line` location points at."""
    if is_location(spec):
        relpath, lineno = parse_location(spec, index.root)
        if index.table.module_at(relpath) is None:
            raise TargetError(f"{relpath} is not part of the index")
        symbol = index.table.enclosing(relpath, lineno)
        if symbol is None:
            raise TargetError(f"{relpath}:{lineno} is not inside any definition")
        return symbol

    symbol = index.table.lookup(spec)
    if symbol is None:
        raise TargetError(_not_found(index, spec))
    return symbol


def find_refinable(index: ProjectIndex, spec: str) -> Symbol:
    """The function or method to rewrite. A location inside a nested function
    selects the function around it, since that is the unit that is rewritten."""
    symbol = find_symbol(index, spec)

    if is_location(spec):
        chain = [*index.table.ancestors(symbol), symbol]
        for candidate in reversed(chain):
            if candidate.kind in _REFINABLE and not candidate.is_nested:
                return candidate
        raise TargetError(f"{spec} is not inside a function or method")

    if symbol.kind not in _REFINABLE:
        raise TargetError(
            f"`{symbol.qualname}` is a {symbol.kind.value}; "
            "only a function or a method can be refined"
        )
    if symbol.is_nested:
        raise TargetError(
            f"`{symbol.qualname}` is nested inside `{symbol.parent}`; "
            "refine the enclosing function instead"
        )
    return symbol


def _not_found(index: ProjectIndex, spec: str) -> str:
    message = f"nothing in the index is called `{spec}`"
    similar = [symbol.qualname for symbol in index.table.find(spec.rpartition(".")[2])]
    if similar:
        message += (
            "; did you mean " + ", ".join(f"`{name}`" for name in similar[:5]) + "?"
        )
    return message

"""Reading and parsing a single source file."""

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParseFailure:
    relpath: str
    reason: str
    message: str
    lineno: int | None = None

    def describe(self) -> str:
        location = f"{self.relpath}:{self.lineno}" if self.lineno else self.relpath
        return f"{location}  {self.reason}: {self.message}"


class SourceError(Exception):
    def __init__(self, failure: ParseFailure) -> None:
        super().__init__(f"{failure.relpath}: {failure.message}")
        self.failure = failure


def read_source(path: Path) -> str:
    with tokenize.open(path) as handle:
        return handle.read()


def read_exact_source(path: Path) -> str:
    """Like read_source, but line endings are left as they are on disk so an
    edit can be written back without converting the whole file."""
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding)


def load_module(path: Path, relpath: str) -> tuple[ast.Module, str]:
    """Parse `path`, raising SourceError instead of letting one unreadable file
    abort the indexing of a whole repository."""
    try:
        source = read_source(path)
    except (UnicodeDecodeError, SyntaxError, LookupError) as exc:
        raise SourceError(ParseFailure(relpath, "decode", str(exc))) from exc
    except OSError as exc:
        raise SourceError(ParseFailure(relpath, "read", str(exc))) from exc

    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError as exc:
        raise SourceError(
            ParseFailure(relpath, "syntax", exc.msg or "invalid syntax", exc.lineno)
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise SourceError(ParseFailure(relpath, "syntax", str(exc))) from exc

    return tree, source

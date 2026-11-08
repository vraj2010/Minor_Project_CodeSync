"""The vocabulary the safety checks report in."""

from dataclasses import dataclass
from enum import Enum


class Check(str, Enum):
    CONTRACT = "contract"
    CALL_SITES = "call-sites"
    NAMES = "names"
    TYPES = "types"
    TESTS = "tests"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Finding:
    """One thing a check found wrong, precise enough for a person to look at
    and for a model to act on."""

    check: Check
    code: str
    message: str
    relpath: str | None = None
    lineno: int | None = None
    code_text: str | None = None

    @property
    def location(self) -> str | None:
        if self.relpath is None:
            return None
        return f"{self.relpath}:{self.lineno}" if self.lineno else self.relpath

    def to_dict(self) -> dict:
        return {
            "check": self.check.value,
            "code": self.code,
            "message": self.message,
            "path": self.relpath,
            "lineno": self.lineno,
            "text": self.code_text,
        }

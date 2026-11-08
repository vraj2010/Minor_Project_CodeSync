"""Writing a kept change to the project, in a way that can be undone exactly.

Before a file is touched, its exact bytes go into a journal under
`.codesync/applied`. Undoing puts those bytes back, so the file ends up identical
to what it was, line endings, encoding and all. A file that was edited after the
change was applied is left alone, because putting the old bytes back would throw
that edit away."""

import base64
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .parsing import encode_like, read_exact_source
from .splice import Patch

JOURNAL = Path(".codesync") / "applied"


class ApplyError(Exception):
    pass


@dataclass(frozen=True)
class AppliedChange:
    id: str
    relpath: str
    target: str
    applied_at: str
    original: bytes
    patched_digest: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.relpath,
            "target": self.target,
            "applied_at": self.applied_at,
            "original": base64.b64encode(self.original).decode("ascii"),
            "patched_digest": self.patched_digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppliedChange":
        return cls(
            id=data["id"],
            relpath=data["path"],
            target=data["target"],
            applied_at=data["applied_at"],
            original=base64.b64decode(data["original"]),
            patched_digest=data["patched_digest"],
        )


def apply_patch(root: Path, patch: Patch, target: str) -> AppliedChange:
    """Write `patch` to the project, refusing if the file is no longer what the
    patch was made from."""
    path = _inside(root, patch.relpath)
    try:
        if read_exact_source(path) != patch.original:
            raise ApplyError(
                f"{patch.relpath} changed after this rewrite was checked; "
                "nothing was written"
            )
        original = path.read_bytes()
    except OSError as error:
        raise ApplyError(f"could not read {patch.relpath}: {error}") from error

    patched = encode_like(patch.patched, original)
    change = AppliedChange(
        id=f"{_stamp()}-{secrets.token_hex(3)}",
        relpath=patch.relpath,
        target=target,
        applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        original=original,
        patched_digest=_digest(patched),
    )

    journal = root / JOURNAL
    journal.mkdir(parents=True, exist_ok=True)
    entry = journal / f"{change.id}.json"
    entry.write_text(json.dumps(change.to_dict()), encoding="utf-8")
    try:
        _write(path, patched)
    except OSError as error:
        entry.unlink(missing_ok=True)
        raise ApplyError(f"could not write {patch.relpath}: {error}") from error
    return change


def undo(root: Path, change_id: str | None = None) -> AppliedChange:
    """Put a file back exactly as it was. Without an id, the newest change is
    the one undone."""
    changes = list_applied(root)
    if not changes:
        raise ApplyError("no applied change to undo")
    if change_id is None:
        change = changes[-1]
    else:
        matches = [item for item in changes if item.id == change_id]
        if not matches:
            raise ApplyError(f"no applied change has the id {change_id}")
        change = matches[0]

    later = [
        item
        for item in changes
        if item.relpath == change.relpath and item.id > change.id
    ]
    if later:
        raise ApplyError(
            f"{later[-1].id} changed {change.relpath} after this; undo that first"
        )

    path = _inside(root, change.relpath)
    if not path.is_file() or _digest(path.read_bytes()) != change.patched_digest:
        raise ApplyError(
            f"{change.relpath} was edited after the change was applied, so "
            "undoing it would lose that edit; nothing was changed"
        )
    _write(path, change.original)
    (root / JOURNAL / f"{change.id}.json").unlink(missing_ok=True)
    return change


def list_applied(root: Path) -> list[AppliedChange]:
    """Applied changes that can still be undone, oldest first."""
    journal = root / JOURNAL
    if not journal.is_dir():
        return []
    return sorted(
        (
            AppliedChange.from_dict(json.loads(item.read_text(encoding="utf-8")))
            for item in journal.glob("*.json")
        ),
        key=lambda item: item.id,
    )


def _inside(root: Path, relpath: str) -> Path:
    path = (root / relpath).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ApplyError(f"{relpath} is outside the project")
    return path


def _write(path: Path, data: bytes) -> None:
    """Replace a file in one step, so a crash cannot leave half of it."""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".codesync-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")

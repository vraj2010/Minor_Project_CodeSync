"""A throwaway copy of a project, where a suggestion can be tried out.

Nothing a check does touches the real project. The rewrite goes into the copy,
the slow checks run there, and the copy is deleted at the end, whatever the
outcome."""

import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .discovery import DEFAULT_EXCLUDES, discover
from .parsing import encode_like

_SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|GEMINI|DEEPSEEK|CODESYNC)", re.IGNORECASE
)
_VENV_FOLDERS = (".venv", "venv", "env")


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path: Path | None = None
        self._originals: dict[str, bytes] = {}

    def __enter__(self) -> "Sandbox":
        self.path = Path(tempfile.mkdtemp(prefix="codesync-")) / "project"
        shutil.copytree(
            self.root,
            self.path,
            ignore=shutil.ignore_patterns(*DEFAULT_EXCLUDES, ".codesync"),
            symlinks=True,
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self.path is not None:
            shutil.rmtree(self.path.parent, ignore_errors=True)
            self.path = None

    def write(self, relpath: str, text: str) -> None:
        """Put a new version of a file in the copy, keeping its encoding."""
        path = self._path(relpath)
        original = self._originals.setdefault(relpath, path.read_bytes())
        path.write_bytes(encode_like(text, original))

    def restore(self) -> None:
        """Put every changed file back, ready for the next suggestion."""
        for relpath, original in self._originals.items():
            self._path(relpath).write_bytes(original)
        self._originals.clear()

    def _path(self, relpath: str) -> Path:
        if self.path is None:
            raise RuntimeError("the sandbox is not open")
        return self.path / relpath


def clean_environment(
    root: Path, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment a project's own code runs in. Running tests means running
    that code, so anything that looks like a secret is left behind."""
    source = os.environ if source is None else source
    env = {
        name: value
        for name, value in source.items()
        if not _SECRET_NAME.search(name)
    }
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in import_roots(root))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def import_roots(root: Path) -> list[Path]:
    """Folders the project's code is imported from, most specific first.

    A file's import root is the folder above its topmost package, so a library
    kept at `requests/src/requests/` is imported from `requests/src`, wherever
    that sits in the upload. Placing these ahead of site-packages also means an
    installed copy of the library can never stand in for the project's own."""
    root = root.resolve()
    found: dict[Path, None] = {}
    try:
        files = discover(root)
    except OSError:
        files = []
    for source in files:
        directory = source.path.parent
        while directory != root and (directory / "__init__.py").is_file():
            directory = directory.parent
        found[directory] = None
    if (root / "src").is_dir():
        found[root / "src"] = None
    found.pop(root, None)
    ordered = sorted(found, key=lambda path: (-len(path.parts), str(path)))
    return [*ordered, root]


def find_python(root: Path) -> str:
    """The interpreter a project's tests should run under: its own virtual
    environment if it has one, otherwise the one running this."""
    for folder in _VENV_FOLDERS:
        for candidate in (
            root / folder / "Scripts" / "python.exe",
            root / folder / "bin" / "python",
        ):
            if candidate.is_file():
                return str(candidate)
    return sys.executable

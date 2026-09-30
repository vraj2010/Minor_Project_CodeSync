"""Locating the Python source files that make up a project."""

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pathspec

DEFAULT_EXCLUDES = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "site-packages",
    "node_modules",
    "build",
    "dist",
    ".eggs",
    "*.egg-info",
    ".idea",
    ".vscode",
)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relpath: str
    module: str
    is_package: bool


def discover(
    root: Path | str,
    *,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    use_gitignore: bool = True,
    follow_symlinks: bool = False,
) -> list[SourceFile]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    excludes = tuple(excludes)
    ignore_spec = _load_gitignore(root) if use_gitignore else None
    found: list[SourceFile] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        directory = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not _matches(name, excludes)
            and not _is_ignored(ignore_spec, directory / name, root, is_dir=True)
        )

        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = directory / filename
            if _is_ignored(ignore_spec, path, root, is_dir=False):
                continue
            found.append(
                SourceFile(
                    path=path,
                    relpath=relative_path(path, root),
                    module=module_name(path, root),
                    is_package=filename == "__init__.py",
                )
            )

    return found


def module_name(path: Path, root: Path) -> str:
    """Dotted name of `path`, following packages upwards until the first
    directory without an `__init__.py`. That directory is the import root, so a
    `src/` layout yields `mypkg.mod` rather than `src.mypkg.mod`."""
    parts: list[str] = []
    directory = path.parent
    while directory != root and (directory / "__init__.py").is_file():
        parts.append(directory.name)
        directory = directory.parent
    parts.reverse()

    if path.name == "__init__.py":
        return ".".join(parts) if parts else path.parent.name
    parts.append(path.stem)
    return ".".join(parts)


def relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _load_gitignore(root: Path) -> pathspec.GitIgnoreSpec | None:
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return None
    lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    return pathspec.GitIgnoreSpec.from_lines(lines)


def _is_ignored(
    spec: pathspec.GitIgnoreSpec | None, path: Path, root: Path, *, is_dir: bool
) -> bool:
    if spec is None:
        return False
    candidate = relative_path(path, root)
    return spec.match_file(candidate + "/" if is_dir else candidate)

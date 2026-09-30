"""Building the symbol table for a whole project."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .collector import collect_symbols
from .discovery import DEFAULT_EXCLUDES, discover
from .parsing import ParseFailure, SourceError, load_module
from .symbols import ModuleInfo, SymbolTable


@dataclass(frozen=True)
class DuplicateModule:
    name: str
    kept: str
    shadowed: str


@dataclass(frozen=True)
class ProjectIndex:
    root: Path
    table: SymbolTable
    file_count: int
    failures: tuple[ParseFailure, ...] = ()
    duplicates: tuple[DuplicateModule, ...] = ()

    def summary(self) -> dict[str, int]:
        return {
            "files": self.file_count,
            "modules": len(self.table.modules),
            **self.table.counts(),
            "failures": len(self.failures),
        }

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "summary": self.summary(),
            "modules": [info.to_dict() for info in self.table.modules],
            "symbols": [symbol.to_dict() for symbol in self.table.symbols],
            "failures": [
                {
                    "path": failure.relpath,
                    "reason": failure.reason,
                    "message": failure.message,
                    "lineno": failure.lineno,
                }
                for failure in self.failures
            ],
            "duplicate_modules": [
                {"name": item.name, "kept": item.kept, "shadowed": item.shadowed}
                for item in self.duplicates
            ],
        }


def build_index(
    root: Path | str,
    *,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    use_gitignore: bool = True,
    follow_symlinks: bool = False,
) -> ProjectIndex:
    root = Path(root).resolve()
    sources = discover(
        root,
        excludes=excludes,
        use_gitignore=use_gitignore,
        follow_symlinks=follow_symlinks,
    )

    table = SymbolTable()
    failures: list[ParseFailure] = []
    duplicates: list[DuplicateModule] = []

    for source in sources:
        try:
            tree, text = load_module(source.path, source.relpath)
        except SourceError as error:
            failures.append(error.failure)
            continue

        existing = table.module(source.module)
        if existing is not None:
            duplicates.append(
                DuplicateModule(source.module, existing.relpath, source.relpath)
            )

        table.add_module(
            ModuleInfo(
                name=source.module,
                relpath=source.relpath,
                is_package=source.is_package,
                line_count=len(text.splitlines()),
            )
        )
        for symbol in collect_symbols(tree, source.module, source.relpath):
            table.add_symbol(symbol)

    return ProjectIndex(
        root=root,
        table=table,
        file_count=len(sources),
        failures=tuple(failures),
        duplicates=tuple(duplicates),
    )

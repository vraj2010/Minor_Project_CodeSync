"""Command line entry point for the CodeSync engine."""

import argparse
import json
import sys
from pathlib import Path

from .index import ProjectIndex, build_index
from .symbols import Symbol, SymbolKind

_SUMMARY_LABELS = (
    ("files", "files"),
    ("modules", "modules"),
    ("class", "classes"),
    ("function", "functions"),
    ("method", "methods"),
    ("variable", "variables"),
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codesync", description="Project-aware analysis for safe code refinement"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    index_parser = subcommands.add_parser(
        "index", help="index a project and report what was found"
    )
    _add_common_arguments(index_parser)
    index_parser.add_argument(
        "--json", action="store_true", help="emit the full index as JSON"
    )
    index_parser.set_defaults(handler=_cmd_index)

    symbols_parser = subcommands.add_parser("symbols", help="list the indexed symbols")
    _add_common_arguments(symbols_parser)
    symbols_parser.add_argument("--module", help="restrict to one dotted module name")
    symbols_parser.add_argument(
        "--kind",
        choices=[kind.value for kind in SymbolKind],
        help="restrict to one kind",
    )
    symbols_parser.add_argument(
        "--name", help="restrict to symbols with this short name"
    )
    symbols_parser.add_argument(
        "--json", action="store_true", help="emit the symbols as JSON"
    )
    symbols_parser.set_defaults(handler=_cmd_symbols)

    at_parser = subcommands.add_parser(
        "at", help="name the symbol that encloses a line, given as PATH:LINE"
    )
    _add_common_arguments(at_parser)
    at_parser.add_argument(
        "location", help="source location, for example app/models.py:42"
    )
    at_parser.set_defaults(handler=_cmd_at)

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", help="path to the project")
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="index files that the project's .gitignore excludes",
    )


def _cmd_index(args: argparse.Namespace) -> int:
    index = _load(args)
    if args.json:
        print(json.dumps(index.to_dict(), indent=2))
        return 0

    summary = index.summary()
    rows = [("root", str(index.root))]
    rows += [(label, str(summary[key])) for key, label in _SUMMARY_LABELS]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")

    if index.duplicates:
        print(f"\nduplicate module names ({len(index.duplicates)})")
        for item in index.duplicates:
            print(f"  {item.name}: kept {item.kept}, shadowed {item.shadowed}")

    if index.failures:
        print(f"\nparse failures ({len(index.failures)})")
        for failure in index.failures:
            print(f"  {failure.describe()}")
    return 0


def _cmd_symbols(args: argparse.Namespace) -> int:
    index = _load(args)
    selected = [
        symbol
        for symbol in index.table.symbols
        if (args.module is None or symbol.module == args.module)
        and (args.kind is None or symbol.kind.value == args.kind)
        and (args.name is None or symbol.name == args.name)
    ]

    if args.json:
        print(json.dumps([symbol.to_dict() for symbol in selected], indent=2))
        return 0

    if not selected:
        print("no symbols matched")
        return 1

    _print_symbols(selected)
    return 0


def _cmd_at(args: argparse.Namespace) -> int:
    index = _load(args)
    relpath, lineno = _parse_location(args.location, index.root)

    if index.table.module_at(relpath) is None:
        print(f"{relpath} is not part of the index")
        return 1

    symbol = index.table.enclosing(relpath, lineno)
    if symbol is None:
        print(f"{relpath}:{lineno} is not inside any definition")
        return 1

    print(f"{relpath}:{lineno}  in module {symbol.module}\n")
    _print_symbols([*index.table.ancestors(symbol), symbol], indent="  ")
    return 0


def _load(args: argparse.Namespace) -> ProjectIndex:
    return build_index(Path(args.root), use_gitignore=not args.no_gitignore)


def _print_symbols(symbols: list[Symbol], indent: str = "") -> None:
    kind_width = max(len(symbol.kind.value) for symbol in symbols)
    name_width = max(len(symbol.qualname) for symbol in symbols)
    for symbol in symbols:
        print(
            f"{indent}{symbol.kind.value:<{kind_width}}  "
            f"{symbol.qualname:<{name_width}}  {symbol.location}"
        )


def _parse_location(value: str, root: Path) -> tuple[str, int]:
    text, separator, line = value.rpartition(":")
    if not separator or not line.isdigit():
        raise ValueError(f"expected PATH:LINE, got {value!r}")

    path = Path(text.replace("\\", "/"))
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root)
        except ValueError:
            raise ValueError(f"{text} is outside {root}") from None
    return path.as_posix(), int(line)


if __name__ == "__main__":
    raise SystemExit(main())

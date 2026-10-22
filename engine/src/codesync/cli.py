"""Command line entry point for the CodeSync engine."""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from .batch import load_tasks
from .context import DEFAULT_BUDGET, DEFAULT_GOAL, ContextMode, Section, build_context
from .index import ProjectIndex, build_index
from .llm import PROVIDERS, ChatClient, ProviderError, create_client
from .prompts import build_messages
from .references import Confidence, find_references
from .refine import RefineResult, append_record, refine
from .resolve import Resolver
from .symbols import Symbol, SymbolKind
from .targets import TargetError, find_refinable, find_symbol, parse_location

DEFAULT_LOG = Path(".codesync") / "runs.jsonl"

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
    except (FileNotFoundError, NotADirectoryError, ValueError, ProviderError) as error:
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
    _add_project_arguments(index_parser)
    index_parser.add_argument(
        "--json", action="store_true", help="emit the full index as JSON"
    )
    index_parser.set_defaults(handler=_cmd_index)

    symbols_parser = subcommands.add_parser("symbols", help="list the indexed symbols")
    _add_project_arguments(symbols_parser)
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
    _add_project_arguments(at_parser)
    at_parser.add_argument(
        "location", help="source location, for example app/models.py:42"
    )
    at_parser.set_defaults(handler=_cmd_at)

    usages_parser = subcommands.add_parser(
        "usages", help="list every place in the project that uses a symbol"
    )
    _add_project_arguments(usages_parser)
    usages_parser.add_argument("target", help="qualified name or PATH:LINE")
    usages_parser.add_argument(
        "--json", action="store_true", help="emit the usages as JSON"
    )
    usages_parser.set_defaults(handler=_cmd_usages)

    context_parser = subcommands.add_parser(
        "context", help="show the prompt a refinement would send, without sending it"
    )
    _add_project_arguments(context_parser)
    context_parser.add_argument("target", help="qualified name or PATH:LINE")
    _add_context_arguments(context_parser)
    context_parser.set_defaults(handler=_cmd_context)

    refine_parser = subcommands.add_parser(
        "refine", help="ask a model to rewrite a function and show the result as a diff"
    )
    _add_project_arguments(refine_parser)
    refine_parser.add_argument("target", help="qualified name or PATH:LINE")
    _add_context_arguments(refine_parser)
    _add_model_arguments(refine_parser)
    _add_log_arguments(refine_parser)
    refine_parser.add_argument(
        "--output", type=Path, help="also write the diff to this file"
    )
    refine_parser.add_argument(
        "--show-reply", action="store_true", help="print the model's reply as well"
    )
    refine_parser.set_defaults(handler=_cmd_refine)

    batch_parser = subcommands.add_parser(
        "batch", help="run every refinement in a task file"
    )
    batch_parser.add_argument("tasks", type=Path, help="JSON file listing the tasks")
    _add_context_arguments(batch_parser)
    _add_model_arguments(batch_parser)
    _add_log_arguments(batch_parser)
    batch_parser.add_argument(
        "--patches", type=Path, help="write each diff into this directory"
    )
    batch_parser.set_defaults(handler=_cmd_batch)

    return parser


def _add_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", help="path to the project")
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="index files that the project's .gitignore excludes",
    )


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--goal", default=DEFAULT_GOAL, help="what the rewrite should achieve"
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in ContextMode],
        default=ContextMode.PROJECT.value,
        help="project sends the contract, callers and callees; "
        "baseline sends the function alone",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"token budget for the context (default {DEFAULT_BUDGET})",
    )


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        default="deepseek",
        help=f"one of {', '.join(PROVIDERS)}, or any name together with --base-url",
    )
    parser.add_argument("--model", help="model name, if not the provider's default")
    parser.add_argument("--base-url", help="URL of an OpenAI-compatible API")
    parser.add_argument(
        "--api-key-env", help="environment variable that holds the API key"
    )
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for a reply"
    )


def _add_log_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"append a JSON record of each run here (default {DEFAULT_LOG})",
    )
    parser.add_argument("--no-log", action="store_true", help="do not record the run")


def _cmd_index(args: argparse.Namespace) -> int:
    index = _load(args)
    if args.json:
        print(json.dumps(index.to_dict(), indent=2))
        return 0

    summary = index.summary()
    rows = [("root", str(index.root))]
    rows += [(label, str(summary[key])) for key, label in _SUMMARY_LABELS]
    _print_rows(rows)

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
    relpath, lineno = parse_location(args.location, index.root)

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


def _cmd_usages(args: argparse.Namespace) -> int:
    index = _load(args)
    target = find_symbol(index, args.target)
    references = find_references(Resolver(index), target)

    if args.json:
        print(json.dumps([ref.to_dict() for ref in references], indent=2))
        return 0

    if not references:
        print(f"nothing else in the project uses {target.qualname}")
        return 0

    noun = "place" if len(references) == 1 else "places"
    print(f"{target.qualname} is used in {len(references)} {noun}\n")
    rows = [
        (
            ref.kind.value,
            ref.location,
            ref.scope.qualname if ref.scope else "(module level)",
            ref.summary
            + ("   [name only]" if ref.confidence is Confidence.NAME_ONLY else ""),
        )
        for ref in references
    ]
    kind_width, location_width, scope_width = (
        max(len(row[column]) for row in rows) for column in range(3)
    )
    for kind, location, scope, text in rows:
        print(
            f"  {kind:<{kind_width}}  {location:<{location_width}}  "
            f"{scope:<{scope_width}}  {text}"
        )
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    index = _load(args)
    target = find_refinable(index, args.target)
    context = build_context(
        Resolver(index),
        target,
        goal=args.goal,
        mode=ContextMode(args.mode),
        budget=args.budget,
    )

    _print_rows(
        [
            ("target", f"{target.qualname} ({target.location})"),
            ("mode", context.mode.value),
            ("budget", f"{context.tokens} of {context.budget} tokens"),
            ("included", ", ".join(_count_sections(context.sections)) or "-"),
            ("omitted", ", ".join(_count_sections(context.omitted)) or "-"),
        ]
    )
    for message in build_messages(context):
        print(f"\n----- {message['role']} -----\n")
        print(message["content"])
    return 0


def _cmd_refine(args: argparse.Namespace) -> int:
    client = _client(args)
    index = _load(args)
    target = find_refinable(index, args.target)
    try:
        result = refine(
            index,
            target,
            client,
            goal=args.goal,
            mode=ContextMode(args.mode),
            budget=args.budget,
        )
    finally:
        client.close()

    if not args.no_log:
        append_record(args.log, result.to_record())

    _report(result)
    if args.show_reply and result.completion is not None:
        print("\n----- reply -----\n")
        print(result.completion.text)
    if result.patch is not None and result.patch.changed:
        diff = result.patch.diff()
        print()
        print(diff, end="")
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(diff, encoding="utf-8", newline="")
    return 0 if result.outcome.has_patch else 1


def _cmd_batch(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks)
    client = _client(args)
    projects: dict[Path, tuple[ProjectIndex, Resolver]] = {}
    outcomes: Counter[str] = Counter()
    width = max((len(task.target) for task in tasks), default=0)

    try:
        for number, task in enumerate(tasks, start=1):
            if task.root not in projects:
                index = build_index(task.root)
                projects[task.root] = (index, Resolver(index))
            index, resolver = projects[task.root]

            try:
                target = find_refinable(index, task.target)
            except TargetError as error:
                outcomes["bad-target"] += 1
                print(
                    f"{number:>3}  {'bad-target':<15}  {task.target:<{width}}  {error}"
                )
                continue

            result = refine(
                index,
                target,
                client,
                goal=task.goal or args.goal,
                mode=ContextMode(args.mode),
                budget=args.budget,
                resolver=resolver,
            )
            outcomes[result.outcome.value] += 1
            if not args.no_log:
                append_record(args.log, result.to_record())
            if (
                args.patches is not None
                and result.patch is not None
                and result.patch.changed
            ):
                args.patches.mkdir(parents=True, exist_ok=True)
                path = args.patches / f"{number:03}-{target.qualname}.patch"
                path.write_text(result.patch.diff(), encoding="utf-8", newline="")
            print(
                f"{number:>3}  {result.outcome.value:<15}  "
                f"{target.qualname:<{width}}  {_detail(result)}"
            )
    finally:
        client.close()

    totals = ", ".join(f"{name} {count}" for name, count in outcomes.most_common())
    print(f"\n{len(tasks)} tasks: {totals}")
    return 0


def _client(args: argparse.Namespace) -> ChatClient:
    return create_client(
        args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )


def _report(result: RefineResult) -> None:
    context = result.context
    rows = [
        ("target", f"{result.target.qualname} ({result.target.location})"),
        (
            "context",
            f"{context.mode.value}, {context.tokens} of {context.budget} tokens, "
            f"{len(context.omitted)} sections left out",
        ),
    ]
    if result.completion is not None:
        completion = result.completion
        usage = ""
        if completion.prompt_tokens is not None:
            usage = (
                f", {completion.prompt_tokens} prompt + "
                f"{completion.completion_tokens} reply tokens"
            )
        rows.append(("model", f"{completion.model}, {completion.latency:.1f}s{usage}"))
    rows.append(("outcome", f"{result.outcome.value}  {_detail(result)}".rstrip()))
    _print_rows(rows)


def _detail(result: RefineResult) -> str:
    if result.error:
        return result.error
    if result.patch is None or not result.patch.changed:
        return ""
    added, removed = result.patch.line_counts()
    detail = f"+{added} -{removed} lines"
    if result.rewrite is not None and result.rewrite.name != result.target.name:
        detail += f", renamed to {result.rewrite.name}"
    if result.patch.added_imports:
        detail += f", {len(result.patch.added_imports)} import(s) added"
    return detail


def _count_sections(sections: Iterable[Section]) -> list[str]:
    counts = Counter(section.kind.value for section in sections)
    return [
        kind if count == 1 else f"{kind} ({count})" for kind, count in counts.items()
    ]


def _load(args: argparse.Namespace) -> ProjectIndex:
    return build_index(Path(args.root), use_gitignore=not args.no_gitignore)


def _print_rows(rows: list[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")


def _print_symbols(symbols: list[Symbol], indent: str = "") -> None:
    kind_width = max(len(symbol.kind.value) for symbol in symbols)
    name_width = max(len(symbol.qualname) for symbol in symbols)
    for symbol in symbols:
        print(
            f"{indent}{symbol.kind.value:<{kind_width}}  "
            f"{symbol.qualname:<{name_width}}  {symbol.location}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line commands for the variable map, the model check and the web
service."""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from .annotate import (
    Annotation,
    AnnotationCache,
    AnnotationOptions,
    AnnotationStatus,
    plan_annotations,
    run_annotations,
)
from .cliargs import (
    add_model_arguments,
    add_project_arguments,
    create_model_client,
    explain_error,
    load_index,
    print_rows,
    print_table,
)
from .llm import KEY_ENV, MODEL_ENV, ProviderError
from .resolve import Resolver
from .text import plural
from .variables import AccessMode, VariableKind
from .varmap import VariableMap, parse_kinds
from .varscan import build_variable_table

DEFAULT_MAP = Path(".codesync") / "variable-map.json"
DEFAULT_CACHE = Path(".codesync") / "annotations.jsonl"
_SAMPLE_ROWS = 12


def register(subcommands: argparse._SubParsersAction) -> None:
    variables = subcommands.add_parser(
        "variables", help="list every variable in a project"
    )
    add_project_arguments(variables)
    variables.add_argument("--kind", choices=[kind.value for kind in VariableKind])
    variables.add_argument("--module", help="restrict to one dotted module name")
    variables.add_argument(
        "--container", help="restrict to one function, class or module"
    )
    variables.add_argument("--name", help="restrict to variables with this short name")
    variables.add_argument(
        "--query", help="restrict to qualified names containing this text"
    )
    variables.add_argument(
        "--cross-file",
        action="store_true",
        help="only variables used in a file other than the one that defines them",
    )
    variables.add_argument("--limit", type=int, help="show at most this many rows")
    variables.add_argument(
        "--json", action="store_true", help="emit the variables as JSON"
    )
    variables.set_defaults(handler=_cmd_variables)

    variable = subcommands.add_parser(
        "variable", help="show one variable with every place it is written and read"
    )
    add_project_arguments(variable)
    variable.add_argument("name", help="qualified name of the variable")
    variable.add_argument(
        "--json", action="store_true", help="emit the variable as JSON"
    )
    variable.set_defaults(handler=_cmd_variable)

    annotate = subcommands.add_parser(
        "annotate",
        help="describe every variable with a hosted model and write the variable map",
    )
    add_project_arguments(annotate)
    add_model_arguments(annotate, max_tokens=4096)
    annotate.add_argument(
        "--kinds",
        help="comma-separated kinds to describe: "
        "module, class, instance, parameter, local",
    )
    annotate.add_argument(
        "--max-requests", type=int, help="stop planning after this many requests"
    )
    annotate.add_argument(
        "--max-variables", type=int, default=20, help="most variables in one request"
    )
    annotate.add_argument(
        "--workers", type=int, default=4, help="requests in flight at once"
    )
    annotate.add_argument(
        "--cache", type=Path, default=DEFAULT_CACHE, help="answer cache file"
    )
    annotate.add_argument(
        "--no-cache", action="store_true", help="do not read or write the cache"
    )
    annotate.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MAP,
        help="where to write the variable map",
    )
    annotate.add_argument(
        "--accesses",
        action="store_true",
        help="include every read and write in the map",
    )
    annotate.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent, without sending it",
    )
    annotate.add_argument("--quiet", action="store_true", help="no progress output")
    annotate.set_defaults(handler=_cmd_annotate)

    check = subcommands.add_parser(
        "check", help="test the model key and connection with one tiny request"
    )
    add_model_arguments(check, max_tokens=512, retries=1)
    check.set_defaults(handler=_cmd_check)

    models = subcommands.add_parser(
        "models", help="list the Gemini models currently on offer"
    )
    add_model_arguments(models)
    models.add_argument("--contains", help="only models whose name contains this text")
    models.set_defaults(handler=_cmd_models)

    serve = subcommands.add_parser("serve", help="run the web API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    serve.set_defaults(handler=_cmd_serve)


def _cmd_variables(args: argparse.Namespace) -> int:
    index = load_index(args)
    variable_map = VariableMap(index.root, build_variable_table(index))
    selected = variable_map.select(
        kind=args.kind,
        module=args.module,
        container=args.container,
        name=args.name,
        query=args.query,
        cross_file=True if args.cross_file else None,
    )

    if args.json:
        entries = [variable_map.entry(item.qualname) for item in selected]
        print(json.dumps(entries, indent=2))
        return 0
    if not selected:
        print("no variables matched")
        return 1

    shown = selected[: args.limit] if args.limit else selected
    rows = []
    for item in shown:
        usage = variable_map.table.usage(item.qualname)
        rows.append(
            (
                item.kind.value,
                item.qualname,
                item.static_type or "-",
                f"{usage.reads}/{usage.writes}",
                item.location,
            )
        )
    print_table(("kind", "variable", "type", "r/w", "location"), rows)
    hidden = len(selected) - len(shown)
    more = f", {hidden} more not shown (see --limit)" if hidden else ""
    print(f"\n{plural(len(selected), 'variable')}{more}")
    return 0


def _cmd_variable(args: argparse.Namespace) -> int:
    index = load_index(args)
    resolver = Resolver(index)
    variable_map = VariableMap(index.root, build_variable_table(index, resolver))
    table = variable_map.table

    variable = table.lookup(args.name)
    if variable is None:
        similar = [item.qualname for item in table.find(args.name.rpartition(".")[2])]
        hint = (
            "; did you mean " + ", ".join(f"`{name}`" for name in similar[:5]) + "?"
            if similar
            else ""
        )
        raise ValueError(f"nothing in the index is called `{args.name}`{hint}")

    if args.json:
        print(json.dumps(variable_map.entry(args.name, accesses=True), indent=2))
        return 0

    usage = table.usage(args.name)
    kind = (
        f"{variable.parameter_kind} parameter"
        if variable.parameter_kind
        else f"{variable.kind.value} variable"
    )
    rows = [
        ("variable", variable.qualname),
        ("kind", kind),
        ("declared", variable.location),
        ("scope", variable.scope),
    ]
    if variable.static_type:
        rows.append(("type", f"{variable.static_type} ({variable.type_source})"))
    if variable.default is not None:
        rows.append(("default", variable.default))
    if variable.overrides:
        rows.append(("overrides", variable.overrides))
    rows.append(
        (
            "used",
            f"{plural(usage.reads, 'read')} and {plural(usage.writes, 'write')} "
            f"in {plural(len(usage.files), 'file')}",
        )
    )
    print_rows(rows)

    accesses = table.accesses(args.name)
    for mode, title in ((AccessMode.WRITE, "written"), (AccessMode.READ, "read")):
        chosen = [item for item in accesses if item.mode is mode]
        if not chosen:
            continue
        print(f"\n{title} ({len(chosen)})")
        lines = [
            (
                item.location,
                item.value or _source_line(resolver, item.relpath, item.lineno),
            )
            for item in chosen
        ]
        _print_places(lines)
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    index = load_index(args)
    resolver = Resolver(index)
    table = build_variable_table(index, resolver)
    options = AnnotationOptions(
        kinds=parse_kinds(_split(args.kinds)),
        max_requests=args.max_requests,
        max_variables=args.max_variables,
    )
    plan = plan_annotations(resolver, table, options)

    summary = plan.summary()
    print_rows(
        [
            ("project", str(index.root)),
            ("variables", str(len(table))),
            ("described by the model", str(summary["variables"])),
            ("described statically", f"{summary['static']} (self and cls)"),
            ("skipped", str(summary["skipped"])),
            ("requests", str(summary["requests"])),
            ("estimated prompt tokens", f"{summary['estimated_prompt_tokens']:,}"),
        ]
    )
    if args.dry_run:
        return 0

    client = create_model_client(args)
    cache = None if args.no_cache else AnnotationCache(args.cache)
    progress = None if args.quiet else _progress
    try:
        result = run_annotations(
            plan, client, cache=cache, workers=args.workers, progress=progress
        )
    finally:
        client.close()
    if progress is not None and plan.requests:
        print(file=sys.stderr)

    variable_map = VariableMap(
        index.root, table, result.annotations, result.usage, client.model
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(variable_map.to_dict(accesses=args.accesses), indent=2)
    args.output.write_text(document, encoding="utf-8")

    statuses = variable_map.summary()["annotations"]
    usage = result.usage
    print()
    print_rows(
        [
            ("model", client.model),
            ("described", str(statuses[AnnotationStatus.OK.value])),
            (
                "not described",
                f"{statuses['missing']} missing, {statuses['failed']} failed",
            ),
            ("requests", f"{usage.requests} sent, {usage.cached} from cache"),
            (
                "tokens",
                f"{usage.prompt_tokens:,} prompt, {usage.completion_tokens:,} reply",
            ),
            ("variable map", str(args.output)),
        ]
    )
    _print_problems(result.annotations, resumable=cache is not None)
    _print_sample(variable_map)
    return (
        1
        if plan.requests and (result.stopped or usage.failed == len(plan.requests))
        else 0
    )


def _cmd_check(args: argparse.Namespace) -> int:
    client = create_model_client(args)
    key = os.environ.get(KEY_ENV, "")

    rows = [
        ("model", client.model),
        ("address", client.base_url),
        ("key", f"{KEY_ENV}, {plural(len(key), 'character')}"),
    ]
    if any(character.isspace() for character in key):
        rows.append(("warning", "the key contains spaces, so it is not a real key"))

    try:
        completion = client.complete(
            [{"role": "user", "content": "Reply with the single word OK."}]
        )
    except ProviderError as error:
        print_rows(rows)
        print()
        return _report_failure(error)
    finally:
        client.close()

    reply = " ".join(completion.text.split())[:40]
    rows.append(("reply", f'"{reply}" in {completion.latency:.1f}s'))
    print_rows(rows)
    print("\nthe key works")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    client = create_model_client(args)
    try:
        names = client.list_models()
    except ProviderError as error:
        return _report_failure(error)
    finally:
        client.close()

    needle = args.contains.lower() if args.contains else None
    shown = [name for name in names if needle is None or needle in name.lower()]
    if not shown:
        print("no models matched")
        return 1

    print("\n".join(shown))
    print(f"\n{plural(len(shown), 'model')}. Use one with --model, or set {MODEL_ENV}.")
    return 0


def _report_failure(error: ProviderError) -> int:
    print(f"failed: {error}")
    hint = explain_error(str(error))
    if hint:
        print(f"-> {hint}")
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .server.app import create_app
    except ImportError:
        raise ValueError(
            'the web API needs its extra dependencies: pip install -e ".[server]"'
        ) from None
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


def _print_problems(annotations: dict[str, Annotation], *, resumable: bool) -> None:
    reasons: Counter[str] = Counter()
    for item in annotations.values():
        if item.status is AnnotationStatus.OK or not item.error:
            continue
        if not item.error.startswith("skipped:"):
            reasons[_flatten(item.error)] += 1
    if not reasons:
        return

    print("\nwhat went wrong")
    for reason, count in reasons.most_common(3):
        print(f"  {plural(count, 'variable')}: {reason}")
        hint = explain_error(reason)
        if hint:
            print(f"  -> {hint}")
    if resumable:
        print(
            "  Run the same command again: what was described is kept, so only "
            "the failed requests are sent."
        )
    print("  Run `codesync check` to test the key and the connection.")


def _flatten(error: str) -> str:
    """One line, without the request id that makes every error look different."""
    flat = " ".join(error.split())
    return re.sub(r"\s*\(request_id: [^)]*\)", "", flat)


def _print_sample(variable_map: VariableMap) -> None:
    described = [
        (variable, variable_map.annotations[variable.qualname])
        for variable in variable_map.table.variables
        if variable.qualname in variable_map.annotations
        and variable_map.annotations[variable.qualname].status is AnnotationStatus.OK
        and not variable.receiver
    ]
    if not described:
        return
    rows = [
        (
            item.qualname.removeprefix(item.module + "."),
            note.type or "-",
            note.role or "-",
            _shorten(note.purpose or "-", 60),
        )
        for item, note in described[:_SAMPLE_ROWS]
    ]
    print()
    print_table(("variable", "type", "role", "purpose"), rows)
    if len(described) > _SAMPLE_ROWS:
        print(f"... and {len(described) - _SAMPLE_ROWS} more in the variable map")


def _print_places(rows: list[tuple[str, str]]) -> None:
    width = max(len(place) for place, _ in rows)
    for place, text in rows:
        print(f"  {place:<{width}}  {text}")


def _progress(done: int, total: int) -> None:
    print(f"\r  {done}/{total} requests", end="", file=sys.stderr, flush=True)


def _source_line(resolver: Resolver, relpath: str, lineno: int) -> str:
    lines = resolver.source(relpath).split("\n")
    return _shorten(lines[lineno - 1].strip(), 100) if lineno <= len(lines) else ""


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]

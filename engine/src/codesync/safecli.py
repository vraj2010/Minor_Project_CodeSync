"""The commands for impact analysis and for checking and keeping changes."""

import argparse
import json
import sys
from pathlib import Path

from .apply import ApplyError, apply_patch, list_applied, undo
from .comparison import ARMS, Comparison, load_tasks, plan, run_comparison
from .cliargs import (
    add_model_arguments,
    add_project_arguments,
    create_model_client,
    load_index,
    print_rows,
    print_table,
)
from .context import DEFAULT_BUDGET, DEFAULT_GOAL, ContextMode
from .findings import CheckStatus
from .impact import DEFAULT_MAX_DEPTH, Impact, analyze_impact, dependency_map
from .parsing import read_exact_source
from .references import Confidence
from .refine import append_record
from .resolve import Resolver
from .response import ResponseError, parse_rewrite
from .safe import DEFAULT_FIXES, Decision, SafeResult, refine_safely
from .splice import SpliceError, splice
from .symbols import SymbolKind
from .targets import find_refinable, find_symbol
from .trapset import DEFAULT_FOLDER, Score, TrapResult, load_trapset, run_traps, validate
from .verify import Report, Verdict, Verifier, VerifyOptions

DEFAULT_LOG = Path(".codesync") / "runs.jsonl"

_MARKS = {
    CheckStatus.PASSED: "pass",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.SKIPPED: "skip",
}


def register(subcommands) -> None:
    deps = subcommands.add_parser(
        "deps", help="show which modules of a project import which"
    )
    add_project_arguments(deps)
    deps.add_argument("--module", help="show only this module")
    deps.add_argument("--json", action="store_true", help="emit the map as JSON")
    deps.set_defaults(handler=_cmd_deps)

    impact = subcommands.add_parser(
        "impact", help="what breaks if this function changes, and which tests notice"
    )
    add_project_arguments(impact)
    impact.add_argument("target", help="qualified name or PATH:LINE")
    impact.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"how many calls away to follow (default {DEFAULT_MAX_DEPTH})",
    )
    impact.add_argument("--json", action="store_true", help="emit the result as JSON")
    impact.set_defaults(handler=_cmd_impact)

    verify = subcommands.add_parser(
        "verify", help="check a rewrite you supply, without any model"
    )
    add_project_arguments(verify)
    verify.add_argument("target", help="qualified name or PATH:LINE")
    verify.add_argument(
        "rewrite", type=Path, help="file holding the new definition of the function"
    )
    _add_check_arguments(verify)
    verify.add_argument("--json", action="store_true", help="emit the report as JSON")
    verify.add_argument(
        "--apply", action="store_true", help="write the change if it is safe"
    )
    verify.set_defaults(handler=_cmd_verify)

    improve = subcommands.add_parser(
        "improve", help="have a model rewrite a function, keeping it only if it is safe"
    )
    add_project_arguments(improve)
    improve.add_argument("target", help="qualified name or PATH:LINE")
    improve.add_argument(
        "--goal", default=DEFAULT_GOAL, help="what the rewrite should achieve"
    )
    improve.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"token budget for the context (default {DEFAULT_BUDGET})",
    )
    improve.add_argument(
        "--fixes",
        type=int,
        default=DEFAULT_FIXES,
        help=f"further tries after a failed check (default {DEFAULT_FIXES})",
    )
    _add_check_arguments(improve)
    add_model_arguments(improve)
    improve.add_argument(
        "--apply", action="store_true", help="write the change if it is kept"
    )
    improve.add_argument(
        "--output", type=Path, help="also write a kept change to this file as a diff"
    )
    improve.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"append a JSON record of the run here (default {DEFAULT_LOG})",
    )
    improve.add_argument("--no-log", action="store_true", help="do not record the run")
    improve.set_defaults(handler=_cmd_improve)

    undo_parser = subcommands.add_parser(
        "undo", help="put a file back exactly as it was before a change was applied"
    )
    undo_parser.add_argument("root", help="path to the project")
    undo_parser.add_argument("id", nargs="?", help="which change; default the newest")
    undo_parser.add_argument(
        "--list", action="store_true", help="list what can be undone"
    )
    undo_parser.set_defaults(handler=_cmd_undo)

    traps = subcommands.add_parser(
        "traps", help="score the checks against the set of hand-written traps"
    )
    traps.add_argument(
        "--set", type=Path, default=DEFAULT_FOLDER, help="folder holding the trap set"
    )
    traps.add_argument("--only", help="comma-separated ids to run, for example T01,C02")
    traps.add_argument(
        "--validate",
        action="store_true",
        help="prove the set itself with the hidden suite, instead of scoring",
    )
    traps.add_argument("--no-types", action="store_true", help="skip the type check")
    traps.add_argument("--output", type=Path, help="also write the results as JSON")
    traps.add_argument("--json", action="store_true", help="emit the results as JSON")
    traps.set_defaults(handler=_cmd_traps)

    compare = subcommands.add_parser(
        "compare", help="run the same tasks four ways and count what quietly breaks"
    )
    compare.add_argument(
        "--set", type=Path, default=DEFAULT_FOLDER, help="folder holding the trap set"
    )
    compare.add_argument("--tasks", type=Path, help="task file (default: the set's own)")
    compare.add_argument(
        "--repeats", type=int, default=1, help="runs of each task in each arm"
    )
    compare.add_argument(
        "--dry-run", action="store_true", help="show what a run would cost and stop"
    )
    compare.add_argument("--output", type=Path, help="also write the results as JSON")
    add_model_arguments(compare)
    compare.set_defaults(handler=_cmd_compare)


def _add_check_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="do not run the tests; nothing can then be confirmed safe",
    )
    parser.add_argument("--no-types", action="store_true", help="skip the type check")
    parser.add_argument(
        "--all-tests",
        action="store_true",
        help="run every test, not only the ones found to reach the function",
    )
    parser.add_argument(
        "--allow-untested",
        action="store_true",
        help="accept a change when no test reaches the function",
    )
    parser.add_argument(
        "--python", help="interpreter for the project's tests (default: its own .venv)"
    )
    parser.add_argument(
        "--check-timeout",
        type=float,
        default=120.0,
        help="seconds each slow check may take (default 120)",
    )


def _options(args: argparse.Namespace) -> VerifyOptions:
    return VerifyOptions(
        run_types=not args.no_types,
        run_tests=not args.no_tests,
        all_tests=args.all_tests,
        allow_untested=args.allow_untested,
        python=args.python,
        timeout=args.check_timeout,
    )


def _cmd_deps(args: argparse.Namespace) -> int:
    modules = dependency_map(Resolver(load_index(args)))
    if args.module is not None:
        modules = [item for item in modules if item.module == args.module]
        if not modules:
            print(f"no module is called {args.module}")
            return 1

    if args.json:
        print(json.dumps([item.to_dict() for item in modules], indent=2))
        return 0

    print_table(
        ("module", "imports", "imported by"),
        [
            (
                item.module,
                ", ".join(item.imports) or "-",
                ", ".join(item.imported_by) or "-",
            )
            for item in modules
        ],
    )
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    index = load_index(args)
    target = find_symbol(index, args.target)
    if target.kind is SymbolKind.VARIABLE:
        raise ValueError(
            f"`{target.qualname}` is a variable; `codesync variable` shows its uses"
        )
    impact = analyze_impact(Resolver(index), target, max_depth=args.depth)

    if args.json:
        print(json.dumps(impact.to_dict(), indent=2))
        return 0
    _print_impact(impact)
    return 0


def _print_impact(impact: Impact) -> None:
    print(f"if {impact.target.qualname} changes\n")

    if not impact.direct and not impact.tests:
        print("nothing else in the project reaches it")
        return

    print(f"used directly ({len(impact.direct)})")
    for ref in impact.direct:
        print(f"  {ref.kind.value:<9}  {ref.location}  {_tagged(ref.summary, ref)}")

    further = [item for item in impact.affected if item.depth > 1]
    if further:
        print(f"\nreached through other code ({len(further)})")
        for item in further:
            route = " <- ".join(reversed(item.via[1:]))
            print(f"  {item.symbol.qualname}  (via {route})")

    print(f"\nfiles affected ({len(impact.files)})")
    for relpath in impact.files:
        print(f"  {relpath}")

    print(f"\ntests that cover it ({len(impact.tests)})")
    for test in impact.tests:
        print(f"  {test.nodeid}")
    if not impact.tests:
        print("  none: a change here cannot be confirmed by running tests")

    if impact.truncated:
        print("\nthe search stopped early; raise --depth to follow it further")


def _tagged(text: str, ref) -> str:
    suffix = "   [name only]" if ref.confidence is Confidence.NAME_ONLY else ""
    return text + suffix


def _cmd_verify(args: argparse.Namespace) -> int:
    index = load_index(args)
    target = find_refinable(index, args.target)
    resolver = Resolver(index)
    owner = resolver.enclosing_class(target) if target.kind is SymbolKind.METHOD else None
    text = args.rewrite.read_text(encoding="utf-8")

    try:
        rewrite = parse_rewrite(text, name=target.name, owner=owner.name if owner else None)
        patch = splice(read_exact_source(index.root / target.relpath), target, rewrite)
    except (ResponseError, SpliceError) as error:
        raise ValueError(f"the rewrite cannot be used: {error}") from error

    if not patch.changed:
        print("the rewrite is the same as the function")
        return 0

    with Verifier(index, target, _options(args), resolver) as verifier:
        report = verifier.check(rewrite, patch)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)

    if report.verdict is not Verdict.SAFE:
        return 1
    if args.apply:
        _apply(index.root, patch, target.qualname)
    return 0


def _cmd_improve(args: argparse.Namespace) -> int:
    client = create_model_client(args)
    index = load_index(args)
    target = find_refinable(index, args.target)
    try:
        result = refine_safely(
            index,
            target,
            client,
            goal=args.goal,
            mode=ContextMode.PROJECT,
            budget=args.budget,
            options=_options(args),
            fixes=args.fixes,
        )
    finally:
        client.close()

    if not args.no_log:
        append_record(args.log, result.to_record())
    _print_result(result)

    if result.decision is Decision.KEPT and result.patch is not None:
        diff = result.patch.diff()
        print()
        print(diff, end="")
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(diff, encoding="utf-8", newline="")
        if args.apply:
            _apply(index.root, result.patch, target.qualname)
    return 0 if result.decision in (Decision.KEPT, Decision.UNCHANGED) else 1


def _cmd_undo(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if args.list:
        changes = list_applied(root)
        if not changes:
            print("nothing to undo")
            return 0
        print_table(
            ("id", "file", "function"),
            [(item.id, item.relpath, item.target) for item in changes],
        )
        return 0

    try:
        change = undo(root, args.id)
    except ApplyError as error:
        raise ValueError(str(error)) from error
    print(f"{change.relpath} is back as it was before {change.target} was changed")
    return 0


def _cmd_traps(args: argparse.Namespace) -> int:
    trapset = load_trapset(args.set)

    if args.validate:
        problems = validate(trapset, _validation_progress)
        print()
        if problems:
            print(f"{len(problems)} problems with the trap set")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print(
            f"the hidden suite passes on the original, fails on all "
            f"{len(trapset.traps)} traps and passes on all "
            f"{len(trapset.controls)} controls"
        )
        return 0

    options = VerifyOptions(run_types=not args.no_types, python=sys.executable)
    only = set(args.only.split(",")) if args.only else None
    score = run_traps(
        trapset,
        options=options,
        only=only,
        progress=None if args.json else _trap_progress,
    )

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(score.to_dict(), indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(score.to_dict(), indent=2))
    else:
        _print_score(score)
    return 0


def _validation_progress(trap_id: str, failures: list[str]) -> None:
    print(f"{trap_id}  hidden failures: {len(failures)}")


def _trap_progress(result: TrapResult) -> None:
    by = f" by {result.caught_by}" if result.caught_by else ""
    print(f"{result.trap.id}  {result.outcome}{by}  {result.trap.target}")


def _print_score(score: Score) -> None:
    summary = score.summary()
    total = summary["traps"]
    print()
    print_rows(
        [
            ("traps", str(total)),
            ("caught", f"{summary['caught']} of {total}"),
            ("unconfirmed", f"{summary['unconfirmed']} (refused, cause not found)"),
            ("missed", str(summary["missed"])),
        ]
    )

    print("\nby category")
    print_table(
        ("category", "caught", "unconfirmed", "missed"),
        [
            (name, str(item["caught"]), str(item["unconfirmed"]), str(item["kept"]))
            for name, item in summary["by_category"].items()
        ],
    )

    print("\ncaught by")
    print_table(
        ("check", "traps"),
        [(name, str(count)) for name, count in summary["caught_by_check"].items()],
    )

    print()
    print_rows(
        [
            ("controls", str(summary["controls"])),
            ("kept", str(summary["controls_kept"])),
            ("wrongly refused", str(summary["controls_refused"])),
            ("unconfirmed", str(summary["controls_unconfirmed"])),
        ]
    )

    missed = [item for item in score.traps if item.outcome == "kept"]
    if missed:
        print("\nmissed traps")
        for item in missed:
            print(f"  {item.trap.id}  {item.trap.target}: {item.trap.title}")


def _cmd_compare(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks or args.set / "tasks.json")
    if args.dry_run:
        cost = plan(tasks, ARMS, args.repeats)
        print_rows([(key.replace("_", " "), str(value)) for key, value in cost.items()])
        return 0

    client = create_model_client(args)
    try:
        comparison = run_comparison(
            client,
            tasks,
            trapset=load_trapset(args.set),
            repeats=args.repeats,
            progress=_comparison_progress,
        )
    finally:
        client.close()

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(comparison.to_dict(), indent=2), encoding="utf-8"
        )
    _print_comparison(comparison)
    return 0


def _comparison_progress(run: dict) -> None:
    broken = "  BROKEN" if run["hidden_failures"] else ""
    print(f"{run['arm']:<7} {run['outcome']:<10} {run['target']}{broken}")


def _print_comparison(comparison: Comparison) -> None:
    print(f"\nmodel {comparison.model}\n")
    print_table(
        ("arm", "tasks", "kept", "kept and broken", "refused", "requests", "broken rate"),
        [
            (
                name,
                str(item.tasks),
                str(item.kept),
                str(item.kept_and_broken),
                str(item.refused),
                str(item.requests),
                f"{item.broken_rate:.0%}",
            )
            for name, item in comparison.tallies.items()
        ],
    )


def _apply(root: Path, patch, target: str) -> None:
    try:
        change = apply_patch(root, patch, target)
    except ApplyError as error:
        raise ValueError(str(error)) from error
    print(f"\napplied to {change.relpath}; `codesync undo` reverses it exactly")


def _print_result(result: SafeResult) -> None:
    for attempt in result.attempts:
        print(f"attempt {attempt.number}: ", end="")
        if attempt.report is None:
            print(attempt.result.outcome.value, attempt.result.error or "")
        elif attempt.report.caught_by:
            print(f"rejected by the {attempt.report.caught_by} check")
        else:
            print(attempt.report.verdict.value)

    print()
    print_rows(
        [
            ("function", result.target.qualname),
            ("decision", result.decision.value),
            ("reason", result.reason),
        ]
    )
    report = result.report
    if report is not None:
        print()
        _print_report(report, heading=False)


def _print_report(report: Report, *, heading: bool = True) -> None:
    if heading:
        print(f"{report.target}: {report.verdict.value}\n")

    for item in report.checks:
        extra = f"  {item.seconds * 1000:.0f} ms" if item.seconds else ""
        reason = f"  ({item.detail})" if item.detail else ""
        print(f"  {_MARKS[item.status]}  {item.check.value}{extra}{reason}")

    problems = report.findings
    if problems:
        print(f"\nwhat is wrong ({len(problems)})")
        for finding in problems:
            where = f"{finding.location}: " if finding.location else ""
            print(f"  {where}{finding.message}")

    if report.places:
        noun = "place" if len(report.places) == 1 else "places"
        print(f"\nrefused: {len(report.places)} {noun} would have stopped working")
        for place in report.places:
            print(f"  {place}")

    if report.notes:
        print("\nworth knowing")
        for note in report.notes:
            print(f"  {note}")

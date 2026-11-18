"""Checking, describing and applying rewrites for the web API."""

import ast
import sys

from ..apply import ApplyError, apply_patch, list_applied, undo
from ..context import definition_source
from ..contract import snapshot
from ..parsing import read_exact_source
from ..resolve import Resolver
from ..response import parse_rewrite
from ..safe import SafeResult
from ..splice import splice
from ..symbols import Symbol, SymbolKind
from ..verify import Report, Verdict, Verifier, VerifyOptions
from .settings import Settings
from .store import CheckedChange, ProjectState, Snapshot

_NOT_RUN = "the hosted service never runs uploaded code, so behaviour was not checked"
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def options_for(settings: Settings) -> VerifyOptions:
    """What a server may do with a project. Tests stay off unless it was started
    for local use, and the report then says they did not run."""
    return VerifyOptions(
        run_tests=settings.run_tests,
        tests_note=None if settings.run_tests else _NOT_RUN,
        python=sys.executable,
        timeout=float(settings.check_timeout),
    )


def describe_symbol(resolver: Resolver, target: Symbol) -> dict:
    node = resolver.node_for(target)
    return {
        **target.to_dict(),
        "source": definition_source(resolver, target),
        "contract": snapshot(node).to_dict() if isinstance(node, _FUNCTIONS) else None,
    }


def check_rewrite(
    project: ProjectState,
    view: Snapshot,
    target: Symbol,
    code: str,
    options: VerifyOptions,
) -> dict:
    """Check a rewrite against one view of the project. The change is kept on
    the project so it can be applied or downloaded by its id."""
    resolver = view.resolver
    owner = (
        resolver.enclosing_class(target) if target.kind is SymbolKind.METHOD else None
    )
    rewrite = parse_rewrite(code, name=target.name, owner=owner.name if owner else None)
    source = read_exact_source(view.index.root / target.relpath)
    patch = splice(source, target, rewrite)
    if not patch.changed:
        return {"changed": False, "diff": "", "report": None}

    with Verifier(view.index, target, options, resolver) as verifier:
        report: Report = verifier.check(rewrite, patch)
    change = project.remember(
        target.qualname, patch, report.verdict.value, view.revision
    )
    return {
        "changed": True,
        "change": change.id,
        "path": patch.relpath,
        "diff": patch.diff(),
        "report": report.to_dict(),
    }


def improvement_payload(
    result: SafeResult, resolver: Resolver, change: CheckedChange | None = None
) -> dict:
    """A finished rewrite, for a client to show. The last attempt's code is
    included even when it was refused, because seeing what was turned down is
    the point of showing a refusal."""
    last = result.last.result
    report = result.report
    return {
        "target": result.target.qualname,
        "change": change.id if change else None,
        "path": last.patch.relpath if last.patch else None,
        "decision": result.decision.value,
        "reason": result.reason,
        "before": definition_source(resolver, result.target),
        "after": last.rewrite.source if last.rewrite else None,
        "diff": last.patch.diff() if last.patch else None,
        "report": report.to_dict() if report else None,
        "attempts": [
            {
                "number": item.number,
                "outcome": item.result.outcome.value,
                "error": item.result.error,
                "verdict": item.report.verdict.value if item.report else None,
                "caught_by": item.report.caught_by if item.report else None,
            }
            for item in result.attempts
        ],
    }


def remember_improvement(
    project: ProjectState, result: SafeResult, revision: int
) -> CheckedChange | None:
    """Keep the last attempt's patch, judged by that attempt's own report and
    never by an earlier one, so a verdict cannot be borrowed from other code."""
    last = result.last
    if last.result.patch is None or not last.result.patch.changed:
        return None
    verdict = last.report.verdict.value if last.report else None
    return project.remember(
        result.target.qualname, last.result.patch, verdict, revision
    )


class ChangeRefused(Exception):
    pass


def apply_change(project: ProjectState, change: CheckedChange) -> dict:
    """Write a checked change into the project's files. Only a change every
    check passed, checked against the project as it is now, is written."""
    if change.verdict != Verdict.SAFE.value:
        raise ChangeRefused(
            f"only a rewrite every check passed can be applied; this one is "
            f"{change.verdict or 'not checked'}"
        )
    with project.lock:
        if change.revision != project.revision:
            raise ChangeRefused(
                "the project has changed since this rewrite was checked; "
                "check it again first"
            )
        try:
            applied = apply_patch(
                project.index.root,
                change.patch,
                change.target,
                journal=project.journal,
            )
        except ApplyError as error:
            raise ChangeRefused(str(error)) from None
        project.reload()
    return {
        "applied": applied.summary(),
        "message": f"Changes saved to {applied.relpath}",
        "history": applied_changes(project),
    }


def undo_change(project: ProjectState, change_id: str | None) -> dict:
    """Put a file back byte for byte as it was before a change was applied."""
    with project.lock:
        try:
            undone = undo(project.index.root, change_id, journal=project.journal)
        except ApplyError as error:
            raise ChangeRefused(str(error)) from None
        project.reload()
    return {
        "undone": undone.summary(),
        "message": f"{undone.relpath} is back to how it was",
        "history": applied_changes(project),
    }


def applied_changes(project: ProjectState) -> list[dict]:
    """Changes that can still be undone, oldest first."""
    return [
        item.summary()
        for item in list_applied(project.index.root, journal=project.journal)
    ]


def patch_file(change: CheckedChange) -> tuple[str, str]:
    """A unified diff that `git apply` takes from the project's root folder,
    with a file name for it."""
    name = change.target.replace("/", ".") + ".patch"
    return name, change.patch.diff()

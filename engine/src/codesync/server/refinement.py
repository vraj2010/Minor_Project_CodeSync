"""Checking and describing rewrites for the web API."""

import ast
import sys

from ..context import definition_source
from ..contract import snapshot
from ..parsing import read_exact_source
from ..resolve import Resolver
from ..response import parse_rewrite
from ..safe import SafeResult
from ..splice import splice
from ..symbols import Symbol, SymbolKind
from ..verify import Report, Verifier, VerifyOptions
from .settings import Settings

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


def check_rewrite(project, target: Symbol, code: str, options: VerifyOptions) -> dict:
    resolver = project.resolver
    owner = (
        resolver.enclosing_class(target) if target.kind is SymbolKind.METHOD else None
    )
    rewrite = parse_rewrite(code, name=target.name, owner=owner.name if owner else None)
    source = read_exact_source(project.index.root / target.relpath)
    patch = splice(source, target, rewrite)
    if not patch.changed:
        return {"changed": False, "diff": "", "report": None}

    with Verifier(project.index, target, options, resolver) as verifier:
        report: Report = verifier.check(rewrite, patch)
    return {"changed": True, "diff": patch.diff(), "report": report.to_dict()}


def improvement_payload(result: SafeResult, resolver: Resolver) -> dict:
    """A finished rewrite, for a client to show. The last attempt's code is
    included even when it was refused, because seeing what was turned down is
    the point of showing a refusal."""
    last = result.last.result
    report = result.report
    return {
        "target": result.target.qualname,
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

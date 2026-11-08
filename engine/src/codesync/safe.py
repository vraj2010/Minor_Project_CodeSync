"""The retry loop: rewrite, check, and either keep, send back or refuse.

When a check fails the model is told exactly what broke and gets up to three
more tries. A rewrite that cannot be confirmed safe is refused rather than
retried, since no amount of rewriting supplies a missing test."""

from dataclasses import dataclass
from enum import Enum

from .context import DEFAULT_BUDGET, DEFAULT_GOAL, ContextMode
from .index import ProjectIndex
from .llm import ModelClient
from .refine import Outcome, RefineResult, refine
from .resolve import Resolver
from .splice import Patch
from .symbols import Symbol
from .verify import Report, Verdict, Verifier, VerifyOptions

DEFAULT_FIXES = 3


class Decision(str, Enum):
    KEPT = "kept"
    REFUSED = "refused"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True)
class Attempt:
    number: int
    result: RefineResult
    report: Report | None = None

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "outcome": self.result.outcome.value,
            "error": self.result.error,
            "diff": self.result.patch.diff() if self.result.patch else None,
            "report": self.report.to_dict() if self.report else None,
        }


@dataclass(frozen=True)
class SafeResult:
    target: Symbol
    decision: Decision
    attempts: tuple[Attempt, ...]
    reason: str

    @property
    def last(self) -> Attempt:
        return self.attempts[-1]

    @property
    def patch(self) -> Patch | None:
        """The change to keep. Only a kept rewrite has one."""
        return self.last.result.patch if self.decision is Decision.KEPT else None

    @property
    def report(self) -> Report | None:
        for attempt in reversed(self.attempts):
            if attempt.report is not None:
                return attempt.report
        return None

    def to_record(self) -> dict:
        return {
            "target": self.target.qualname,
            "path": self.target.relpath,
            "decision": self.decision.value,
            "reason": self.reason,
            "attempts": [item.to_dict() for item in self.attempts],
        }


def refine_safely(
    index: ProjectIndex,
    target: Symbol,
    client: ModelClient,
    *,
    goal: str = DEFAULT_GOAL,
    mode: ContextMode = ContextMode.PROJECT,
    budget: int = DEFAULT_BUDGET,
    resolver: Resolver | None = None,
    options: VerifyOptions | None = None,
    fixes: int = DEFAULT_FIXES,
) -> SafeResult:
    """Rewrite `target`, keeping the result only if the checks say it is safe.
    `fixes` is how many further tries the model gets after the first."""
    resolver = resolver or Resolver(index)
    attempts: list[Attempt] = []
    prior: list[dict[str, str]] = []

    with Verifier(index, target, options, resolver) as verifier:
        for number in range(1, fixes + 2):
            result = refine(
                index,
                target,
                client,
                goal=goal,
                mode=mode,
                budget=budget,
                resolver=resolver,
                prior=prior,
            )

            if result.outcome is Outcome.PROVIDER_FAILED:
                attempts.append(Attempt(number, result))
                return SafeResult(
                    target,
                    Decision.FAILED,
                    tuple(attempts),
                    result.error or "the model could not be reached",
                )
            if result.outcome is Outcome.UNCHANGED:
                attempts.append(Attempt(number, result))
                return SafeResult(
                    target,
                    Decision.UNCHANGED,
                    tuple(attempts),
                    "the model returned the function as it was",
                )
            if not result.outcome.has_patch:
                attempts.append(Attempt(number, result))
                prior = _extended(prior, result, _unusable_feedback(result))
                continue

            assert result.patch is not None and result.rewrite is not None
            report = verifier.check(result.rewrite, result.patch)
            attempts.append(Attempt(number, result, report))

            if report.verdict is Verdict.SAFE:
                return SafeResult(
                    target, Decision.KEPT, tuple(attempts), "every check passed"
                )
            if report.verdict is Verdict.UNCONFIRMED:
                return SafeResult(
                    target,
                    Decision.REFUSED,
                    tuple(attempts),
                    "the change could not be confirmed safe: "
                    + "; ".join(report.notes or ("a required check did not run",)),
                )
            prior = _extended(prior, result, report.feedback())

    return SafeResult(
        target,
        Decision.REFUSED,
        tuple(attempts),
        f"still not safe after {len(attempts)} attempts",
    )


def _extended(
    prior: list[dict[str, str]], result: RefineResult, feedback: str
) -> list[dict[str, str]]:
    reply = result.completion.text if result.completion else ""
    return [
        *prior,
        {"role": "assistant", "content": reply},
        {"role": "user", "content": feedback},
    ]


def _unusable_feedback(result: RefineResult) -> str:
    return (
        f"That reply could not be used: {result.error}. Reply with one ```python "
        "code block holding the complete definition, and nothing else in the block."
    )

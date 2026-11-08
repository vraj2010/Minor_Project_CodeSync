"""The fair comparison: the same tasks, run four ways.

    plain    a model with no project awareness, nothing checked
    map      a model shown the project's contract and callers, nothing checked
    checks   the same, with the safety checks
    retry    the same, with the checks and the retry loop

For each arm the question is how often a change gets kept that quietly breaks
the project. That is settled by the hidden suite, which no arm ever sees."""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .context import ContextMode
from .index import build_index
from .llm import ModelClient
from .refine import Outcome, refine
from .resolve import Resolver
from .safe import Decision, refine_safely
from .sandbox import Sandbox
from .splice import Patch
from .trapset import DEFAULT_FOLDER, TrapSet, load_trapset, oracle_failures
from .verify import VerifyOptions


@dataclass(frozen=True)
class Arm:
    name: str
    mode: ContextMode
    checks: bool = False
    fixes: int = 0


ARMS = (
    Arm("plain", ContextMode.BASELINE),
    Arm("map", ContextMode.PROJECT),
    Arm("checks", ContextMode.PROJECT, checks=True),
    Arm("retry", ContextMode.PROJECT, checks=True, fixes=3),
)


@dataclass(frozen=True)
class Task:
    target: str
    goal: str


@dataclass
class Tally:
    tasks: int = 0
    kept: int = 0
    kept_and_broken: int = 0
    refused: int = 0
    unchanged: int = 0
    unusable: int = 0
    failed: int = 0
    requests: int = 0

    @property
    def broken_rate(self) -> float:
        return self.kept_and_broken / self.tasks if self.tasks else 0.0

    def to_dict(self) -> dict:
        return {
            "tasks": self.tasks,
            "kept": self.kept,
            "kept_and_broken": self.kept_and_broken,
            "kept_and_sound": self.kept - self.kept_and_broken,
            "refused": self.refused,
            "unchanged": self.unchanged,
            "unusable": self.unusable,
            "failed": self.failed,
            "model_requests": self.requests,
            "broken_rate": round(self.broken_rate, 4),
        }


@dataclass
class Comparison:
    model: str
    tallies: dict[str, Tally] = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "arms": {name: item.to_dict() for name, item in self.tallies.items()},
            "runs": self.runs,
        }


def load_tasks(path: Path) -> list[Task]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Task(item["target"], item["goal"]) for item in data["tasks"]]


def plan(tasks: list[Task], arms: tuple[Arm, ...], repeats: int = 1) -> dict:
    """What a run would cost at most, before anything is sent."""
    least = len(tasks) * len(arms) * repeats
    most = sum((arm.fixes + 1) * len(tasks) * repeats for arm in arms)
    return {
        "tasks": len(tasks),
        "arms": [arm.name for arm in arms],
        "repeats": repeats,
        "fewest_model_requests": least,
        "most_model_requests": most,
    }


def run_comparison(
    client: ModelClient,
    tasks: list[Task],
    *,
    trapset: TrapSet | None = None,
    arms: tuple[Arm, ...] = ARMS,
    repeats: int = 1,
    options: VerifyOptions | None = None,
    progress: Callable[[dict], None] | None = None,
) -> Comparison:
    trapset = trapset or load_trapset(DEFAULT_FOLDER)
    options = options or VerifyOptions(python=sys.executable)
    index = build_index(trapset.project)
    resolver = Resolver(index)
    comparison = Comparison(client.model, {arm.name: Tally() for arm in arms})
    verdicts: dict[str, list[str]] = {}

    with Sandbox(trapset.project) as sandbox:
        for round_number in range(1, repeats + 1):
            for task in tasks:
                target = index.table.lookup(task.target)
                if target is None:
                    raise ValueError(f"nothing is called {task.target}")
                for arm in arms:
                    tally = comparison.tallies[arm.name]
                    tally.tasks += 1
                    outcome, patch, requests = _run_arm(
                        arm, index, resolver, target, client, task.goal, options
                    )
                    tally.requests += requests
                    _count(tally, outcome)

                    failures: list[str] = []
                    if patch is not None:
                        key = patch.patched
                        if key not in verdicts:
                            verdicts[key] = oracle_failures(trapset, sandbox, patch)
                        failures = verdicts[key]
                        if failures:
                            tally.kept_and_broken += 1

                    run = {
                        "round": round_number,
                        "arm": arm.name,
                        "target": task.target,
                        "outcome": outcome,
                        "requests": requests,
                        "hidden_failures": failures,
                        "diff": patch.diff() if patch is not None else None,
                    }
                    comparison.runs.append(run)
                    if progress is not None:
                        progress(run)
    return comparison


def _run_arm(
    arm: Arm, index, resolver, target, client, goal, options
) -> tuple[str, Patch | None, int]:
    if not arm.checks:
        result = refine(
            index, target, client, goal=goal, mode=arm.mode, resolver=resolver
        )
        if result.outcome is Outcome.CHANGED:
            return "kept", result.patch, 1
        if result.outcome is Outcome.UNCHANGED:
            return "unchanged", None, 1
        if result.outcome is Outcome.PROVIDER_FAILED:
            return "failed", None, 1
        return "unusable", None, 1

    result = refine_safely(
        index,
        target,
        client,
        goal=goal,
        mode=arm.mode,
        resolver=resolver,
        options=options,
        fixes=arm.fixes,
    )
    requests = len(result.attempts)
    if result.decision is Decision.KEPT:
        return "kept", result.patch, requests
    if result.decision is Decision.UNCHANGED:
        return "unchanged", None, requests
    if result.decision is Decision.FAILED:
        return "failed", None, requests
    return "refused", None, requests


def _count(tally: Tally, outcome: str) -> None:
    if outcome == "kept":
        tally.kept += 1
    elif outcome == "refused":
        tally.refused += 1
    elif outcome == "unchanged":
        tally.unchanged += 1
    elif outcome == "unusable":
        tally.unusable += 1
    else:
        tally.failed += 1

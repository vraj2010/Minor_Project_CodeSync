"""The trap set: measuring the checks without depending on any model.

Each trap is a hand-written rewrite hiding one break. Feeding them straight to
the checks gives a score that says how good the checks are, however well or
badly a model happens to do that day. Harmless controls go through the same
checks, because a tool that refuses everything would otherwise score perfectly.

Whether a rewrite really is broken is settled by a hidden test suite that the
checks never see."""

import runpy
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .index import ProjectIndex, build_index
from .parsing import read_exact_source
from .resolve import Resolver
from .response import Rewrite, parse_rewrite
from .runners import run_pytest
from .sandbox import Sandbox
from .splice import Patch, splice
from .symbols import Symbol, SymbolKind
from .verify import Report, Verdict, Verifier, VerifyOptions

DEFAULT_FOLDER = Path(__file__).resolve().parents[2] / "experiments" / "trapset"


@dataclass(frozen=True)
class Trap:
    id: str
    category: str
    target: str
    title: str
    rewrite: str

    @property
    def is_control(self) -> bool:
        return self.id.startswith("C")


@dataclass(frozen=True)
class TrapSet:
    folder: Path
    traps: tuple[Trap, ...]
    controls: tuple[Trap, ...]

    @property
    def project(self) -> Path:
        return self.folder / "project"

    @property
    def oracle(self) -> Path:
        return self.folder / "oracle"

    @property
    def everything(self) -> tuple[Trap, ...]:
        return (*self.traps, *self.controls)


@dataclass(frozen=True)
class TrapResult:
    trap: Trap
    verdict: Verdict
    caught_by: str | None
    seconds: float
    places: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def outcome(self) -> str:
        return {
            Verdict.UNSAFE: "caught",
            Verdict.UNCONFIRMED: "unconfirmed",
            Verdict.SAFE: "kept",
        }[self.verdict]

    def to_dict(self) -> dict:
        return {
            "id": self.trap.id,
            "category": self.trap.category,
            "target": self.trap.target,
            "title": self.trap.title,
            "outcome": self.outcome,
            "caught_by": self.caught_by,
            "seconds": round(self.seconds, 3),
            "places": list(self.places),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Score:
    results: tuple[TrapResult, ...]

    @property
    def traps(self) -> list[TrapResult]:
        return [item for item in self.results if not item.trap.is_control]

    @property
    def controls(self) -> list[TrapResult]:
        return [item for item in self.results if item.trap.is_control]

    def summary(self) -> dict:
        traps = Counter(item.outcome for item in self.traps)
        controls = Counter(item.outcome for item in self.controls)
        return {
            "traps": len(self.traps),
            "caught": traps["caught"],
            "unconfirmed": traps["unconfirmed"],
            "missed": traps["kept"],
            "caught_by_check": dict(
                Counter(item.caught_by for item in self.traps if item.caught_by)
            ),
            "by_category": _by_category(self.traps),
            "controls": len(self.controls),
            "controls_kept": controls["kept"],
            "controls_refused": controls["caught"],
            "controls_unconfirmed": controls["unconfirmed"],
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "results": [item.to_dict() for item in self.results],
        }


def load_trapset(folder: Path = DEFAULT_FOLDER) -> TrapSet:
    path = folder / "traps.py"
    if not path.is_file():
        raise FileNotFoundError(f"no trap set in {folder}")
    data = runpy.run_path(str(path))
    return TrapSet(
        folder=folder,
        traps=tuple(Trap(**item) for item in data["TRAPS"]),
        controls=tuple(Trap(**item) for item in data["CONTROLS"]),
    )


def prepare(
    index: ProjectIndex, resolver: Resolver, trap: Trap
) -> tuple[Symbol, Rewrite, Patch]:
    """Turn a trap into the rewrite and patch the checks will judge."""
    target = index.table.lookup(trap.target)
    if target is None:
        raise ValueError(f"{trap.id}: nothing is called {trap.target}")
    owner = resolver.enclosing_class(target) if target.kind is SymbolKind.METHOD else None
    rewrite = parse_rewrite(
        f"```python\n{trap.rewrite.strip()}\n```",
        name=target.name,
        owner=owner.name if owner else None,
    )
    source = read_exact_source(index.root / target.relpath)
    return target, rewrite, splice(source, target, rewrite)


def run_traps(
    trapset: TrapSet,
    *,
    options: VerifyOptions | None = None,
    only: set[str] | None = None,
    progress: Callable[[TrapResult], None] | None = None,
) -> Score:
    """Put every trap and control through the checks. Those with the same
    target share a baseline, as the attempts of one refinement would."""
    index = build_index(trapset.project)
    resolver = Resolver(index)
    options = options or VerifyOptions(python=sys.executable)

    groups: dict[str, list[Trap]] = {}
    for trap in trapset.everything:
        if only is None or trap.id in only:
            groups.setdefault(trap.target, []).append(trap)

    results: list[TrapResult] = []
    for traps in groups.values():
        target = index.table.lookup(traps[0].target)
        if target is None:
            raise ValueError(f"nothing is called {traps[0].target}")
        with Verifier(index, target, options, resolver) as verifier:
            for trap in traps:
                _, rewrite, patch = prepare(index, resolver, trap)
                started = time.perf_counter()
                report = verifier.check(rewrite, patch)
                result = _result(trap, report, time.perf_counter() - started)
                results.append(result)
                if progress is not None:
                    progress(result)

    results.sort(key=lambda item: item.trap.id)
    return Score(tuple(results))


def oracle_failures(
    trapset: TrapSet, sandbox: Sandbox, patch: Patch | None, *, timeout: float = 60.0
) -> list[str]:
    """What the hidden suite says about a change: the tests that fail."""
    assert sandbox.path is not None
    if patch is not None:
        sandbox.write(patch.relpath, patch.patched)
    try:
        run = run_pytest(
            sandbox.path,
            [str(trapset.oracle)],
            python=sys.executable,
            timeout=timeout,
        )
    finally:
        sandbox.restore()
    if run.timed_out:
        return ["(the hidden suite did not finish)"]
    if run.problem is not None:
        return [f"(the hidden suite could not run: {run.problem})"]
    return sorted(run.failures)


def validate(
    trapset: TrapSet, progress: Callable[[str, list[str]], None] | None = None
) -> list[str]:
    """Check the trap set itself: the hidden suite must pass on the original,
    fail on every trap and pass on every control. Returns what is wrong."""
    index = build_index(trapset.project)
    resolver = Resolver(index)
    problems: list[str] = []

    with Sandbox(trapset.project) as sandbox:
        broken = oracle_failures(trapset, sandbox, None)
        if broken:
            return [f"the hidden suite fails on the original project: {broken}"]

        for trap in trapset.everything:
            _, _, patch = prepare(index, resolver, trap)
            failures = oracle_failures(trapset, sandbox, patch)
            if progress is not None:
                progress(trap.id, failures)
            if trap.is_control and failures:
                problems.append(f"{trap.id} is meant to be harmless but: {failures}")
            elif not trap.is_control and not failures:
                problems.append(f"{trap.id} breaks nothing the hidden suite can see")
    return problems


def _result(trap: Trap, report: Report, seconds: float) -> TrapResult:
    return TrapResult(
        trap=trap,
        verdict=report.verdict,
        caught_by=report.caught_by,
        seconds=seconds,
        places=report.places,
        notes=report.notes,
    )


def _by_category(results: list[TrapResult]) -> dict[str, dict[str, int]]:
    categories: dict[str, dict[str, int]] = {}
    for item in results:
        counts = categories.setdefault(
            item.trap.category, {"caught": 0, "unconfirmed": 0, "kept": 0}
        )
        counts[item.outcome] += 1
    return categories

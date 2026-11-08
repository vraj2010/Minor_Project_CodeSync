"""The safety checks: deciding whether a rewrite can be trusted.

The checks run from cheapest to dearest. The contract, the call sites and the
names need no more than the syntax trees already in memory and finish in
milliseconds; the type checker and the tests need a copy of the project and take
seconds. A suggestion that fails a cheap check never reaches the slow ones.

A rewrite is judged in three ways: `safe` when every check that had to run ran
and passed, `unsafe` when one failed, and `unconfirmed` when nothing failed but
something that had to run could not. Unconfirmed is treated as a no, because
a tool that cannot confirm a change is safe has no business keeping it."""

import ast
import json
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .callsites import check_call_sites, check_overrides
from .contract import Breach, compare, snapshot
from .findings import Check, CheckStatus, Finding
from .impact import Impact, analyze_impact, is_test_file
from .index import ProjectIndex
from .namecheck import check_names
from .references import UsageKind
from .resolve import Resolver
from .response import Rewrite
from .runners import TestRun, TypeRun, mypy_available, run_mypy, run_pytest
from .sandbox import Sandbox, find_python
from .splice import Patch
from .symbols import Symbol

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_EARLIER_FAILED = "not run, because an earlier check failed"
_TURNED_OFF = "turned off"


class Verdict(str, Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class VerifyOptions:
    run_types: bool = True
    run_tests: bool = True
    all_tests: bool = False
    allow_untested: bool = False
    python: str | None = None
    timeout: float = 120.0
    tests_note: str | None = None


@dataclass(frozen=True)
class CheckResult:
    check: Check
    status: CheckStatus
    seconds: float = 0.0
    findings: tuple[Finding, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "check": self.check.value,
            "status": self.status.value,
            "seconds": round(self.seconds, 3),
            "detail": self.detail,
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class Report:
    target: str
    verdict: Verdict
    checks: tuple[CheckResult, ...]
    would_break: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    tests_run: int = 0

    @property
    def findings(self) -> list[Finding]:
        return [item for check in self.checks for item in check.findings]

    @property
    def caught_by(self) -> str | None:
        """The first check that failed, cheapest first."""
        for check in self.checks:
            if check.status is CheckStatus.FAILED:
                return check.check.value
        return None

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "verdict": self.verdict.value,
            "caught_by": self.caught_by,
            "would_break": list(self.would_break),
            "places": list(self.places),
            "notes": list(self.notes),
            "tests_run": self.tests_run,
            "checks": [item.to_dict() for item in self.checks],
        }

    def feedback(self) -> str:
        """What a model is told when its rewrite is sent back: a precise,
        machine-readable list of what broke."""
        document = {
            "rejected": self.target,
            "failed_checks": [
                item.check.value
                for item in self.checks
                if item.status is CheckStatus.FAILED
            ],
            "problems": [
                {
                    key: value
                    for key, value in item.to_dict().items()
                    if value is not None
                }
                for item in self.findings
            ],
        }
        return (
            "Your rewrite was rejected, because it would break the project. "
            "These checks failed:\n\n"
            f"```json\n{json.dumps(document, indent=2)}\n```\n\n"
            "Fix every problem listed while still meeting the goal. Other code "
            "depends on this function, so keep its contract. Reply in the same "
            "format as before, with the complete definition."
        )


@dataclass
class _Baseline:
    """What the slow checks said about the unchanged project."""

    types: TypeRun | None = None
    tests: TestRun | None = None
    type_files: list[str] = field(default_factory=list)


class Verifier:
    """Checks suggestions for one target. Used as a context manager, it keeps
    one copy of the project and one baseline for all of a target's attempts."""

    def __init__(
        self,
        index: ProjectIndex,
        target: Symbol,
        options: VerifyOptions | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.index = index
        self.target = target
        self.options = options or VerifyOptions()
        self.resolver = resolver or Resolver(index)
        self.impact: Impact = analyze_impact(self.resolver, target)
        self._sandbox: Sandbox | None = None
        self._baseline = _Baseline()
        self._cache = Path(tempfile.mkdtemp(prefix="codesync-mypy-"))

    def __enter__(self) -> "Verifier":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._sandbox is not None:
            self._sandbox.__exit__()
            self._sandbox = None
        shutil.rmtree(self._cache, ignore_errors=True)

    def check(self, rewrite: Rewrite, patch: Patch) -> Report:
        node = self.resolver.node_for(self.target)
        if not isinstance(node, _FUNCTIONS):
            raise ValueError(f"`{self.target.qualname}` is not a function")
        after_node = ast.parse(rewrite.source).body[0]
        if not isinstance(after_node, _FUNCTIONS):
            raise ValueError("the rewrite is not a function")

        before = snapshot(node)
        after = snapshot(after_node)
        breaches = compare(before, after)
        references = list(self.impact.direct)

        checks: list[CheckResult] = [
            self._timed(Check.CONTRACT, lambda: _contract_findings(breaches)),
            self._timed(
                Check.CALL_SITES,
                lambda: [
                    *check_call_sites(
                        self.resolver, self.target, before, after, references
                    ),
                    *check_overrides(self.resolver, self.target, before, after),
                ],
            ),
            self._timed(
                Check.NAMES,
                lambda: check_names(
                    self.resolver,
                    self.target,
                    node,
                    after_node,
                    rewrite.source,
                    patch.patched,
                    patch.added_imports,
                ),
            ),
        ]

        tests_run = 0
        if any(item.status is CheckStatus.FAILED for item in checks):
            checks.append(CheckResult(Check.TYPES, CheckStatus.SKIPPED, detail=_EARLIER_FAILED))
            checks.append(CheckResult(Check.TESTS, CheckStatus.SKIPPED, detail=_EARLIER_FAILED))
        else:
            types = self._check_types(patch)
            checks.append(types)
            if types.status is CheckStatus.FAILED:
                checks.append(
                    CheckResult(Check.TESTS, CheckStatus.SKIPPED, detail=_EARLIER_FAILED)
                )
            else:
                result, tests_run = self._check_tests(patch)
                checks.append(result)

        return self._report(checks, breaches, tests_run)

    def _timed(self, check: Check, run) -> CheckResult:
        started = time.perf_counter()
        findings = tuple(run())
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return CheckResult(check, status, time.perf_counter() - started, findings)

    def _open_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = Sandbox(self.index.root).__enter__()
        return self._sandbox

    def _type_files(self) -> list[str]:
        files = {self.target.relpath}
        files.update(
            ref.relpath
            for ref in self.impact.direct
            if ref.kind is not UsageKind.IMPORT and not is_test_file(ref.relpath)
        )
        return sorted(files)

    def _check_types(self, patch: Patch) -> CheckResult:
        if not self.options.run_types:
            return CheckResult(Check.TYPES, CheckStatus.SKIPPED, detail=_TURNED_OFF)
        if not mypy_available():
            return CheckResult(
                Check.TYPES, CheckStatus.SKIPPED, detail="mypy is not installed"
            )

        started = time.perf_counter()
        sandbox = self._open_sandbox()
        assert sandbox.path is not None
        files = self._type_files()
        timeout = self.options.timeout

        if self._baseline.types is None:
            self._baseline.types = run_mypy(
                sandbox.path, files, cache=self._cache, timeout=timeout
            )
        baseline = self._baseline.types
        if baseline.problem is not None:
            return CheckResult(
                Check.TYPES,
                CheckStatus.SKIPPED,
                time.perf_counter() - started,
                detail=baseline.problem,
            )

        sandbox.write(patch.relpath, patch.patched)
        try:
            current = run_mypy(sandbox.path, files, cache=self._cache, timeout=timeout)
        finally:
            sandbox.restore()
        seconds = time.perf_counter() - started
        if current.problem is not None:
            return CheckResult(
                Check.TYPES, CheckStatus.SKIPPED, seconds, detail=current.problem
            )

        findings = _new_type_errors(baseline, current)
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        return CheckResult(Check.TYPES, status, seconds, tuple(findings))

    def _selected_tests(self) -> list[str] | None:
        if self.options.all_tests:
            return None
        return [item.nodeid for item in self.impact.tests]

    def _check_tests(self, patch: Patch) -> tuple[CheckResult, int]:
        options = self.options
        if not options.run_tests:
            detail = options.tests_note or "turned off, so behaviour was not checked"
            return CheckResult(Check.TESTS, CheckStatus.SKIPPED, detail=detail), 0

        selected = self._selected_tests()
        if selected is not None and not selected:
            return (
                CheckResult(
                    Check.TESTS,
                    CheckStatus.SKIPPED,
                    detail="no test reaches this function, so its behaviour "
                    "cannot be checked",
                ),
                0,
            )

        started = time.perf_counter()
        sandbox = self._open_sandbox()
        assert sandbox.path is not None
        python = options.python or find_python(self.index.root)

        if self._baseline.tests is None:
            self._baseline.tests = run_pytest(
                sandbox.path, selected, python=python, timeout=options.timeout
            )
        baseline = self._baseline.tests
        if baseline.problem is not None or not baseline.passed:
            detail = baseline.problem or "none of the tests that reach it pass today"
            return (
                CheckResult(
                    Check.TESTS,
                    CheckStatus.SKIPPED,
                    time.perf_counter() - started,
                    detail=detail,
                ),
                0,
            )

        sandbox.write(patch.relpath, patch.patched)
        try:
            current = run_pytest(
                sandbox.path, selected, python=python, timeout=options.timeout
            )
        finally:
            sandbox.restore()
        seconds = time.perf_counter() - started

        if current.timed_out or current.problem is not None:
            finding = Finding(
                Check.TESTS,
                "tests-did-not-finish" if current.timed_out else "tests-could-not-run",
                current.problem or "the tests could not run",
            )
            return CheckResult(Check.TESTS, CheckStatus.FAILED, seconds, (finding,)), 0

        findings = _new_test_failures(baseline, current)
        status = CheckStatus.FAILED if findings else CheckStatus.PASSED
        detail = None
        if baseline.failures:
            detail = f"{len(baseline.failures)} already failed before the rewrite"
        return CheckResult(Check.TESTS, status, seconds, tuple(findings), detail), (
            current.total
        )

    def _report(
        self,
        checks: list[CheckResult],
        breaches: list[Breach],
        tests_run: int,
    ) -> Report:
        status = {item.check: item.status for item in checks}
        if CheckStatus.FAILED in status.values():
            verdict = Verdict.UNSAFE
        elif status[Check.TESTS] is CheckStatus.SKIPPED and not self.options.allow_untested:
            verdict = Verdict.UNCONFIRMED
        else:
            verdict = Verdict.SAFE

        notes = [item.message for item in breaches if not item.blocking]
        notes.extend(
            f"{item.check.value}: {item.detail}"
            for item in checks
            if item.status is CheckStatus.SKIPPED
            and item.detail not in (None, _EARLIER_FAILED, _TURNED_OFF)
        )
        files, places = self._damage(checks, verdict)
        return Report(
            target=self.target.qualname,
            verdict=verdict,
            checks=tuple(checks),
            would_break=files,
            places=places,
            notes=tuple(notes),
            tests_run=tests_run,
        )

    def _damage(
        self, checks: list[CheckResult], verdict: Verdict
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The files and the places a rejected rewrite would have broken. When
        only the contract failed, nothing has been proved about any one caller,
        so every direct user of the function is named."""
        if verdict is not Verdict.UNSAFE:
            return (), ()

        findings = [
            item
            for check in checks
            for item in check.findings
            if item.check in (Check.CALL_SITES, Check.TYPES, Check.TESTS)
            and item.relpath is not None
        ]
        places = [
            f"{item.location}  {item.code_text or item.message}" for item in findings
        ]
        files = {item.relpath for item in findings if item.relpath}

        if not findings:
            for ref in self.impact.direct:
                if ref.kind is not UsageKind.IMPORT:
                    places.append(f"{ref.location}  {ref.summary}")
                    files.add(ref.relpath)
        return tuple(sorted(files)), tuple(places)


def _contract_findings(breaches: list[Breach]) -> list[Finding]:
    return [
        Finding(Check.CONTRACT, item.code, item.message)
        for item in breaches
        if item.blocking
    ]


def _new_type_errors(baseline: TypeRun, current: TypeRun) -> list[Finding]:
    """Errors the rewrite introduced. Counting them keeps a second copy of an
    existing error from hiding behind the first."""
    remaining: Counter[tuple[str, str]] = baseline.counts()
    findings = []
    for error in current.errors:
        key = (error.relpath, error.message)
        if remaining[key] > 0:
            remaining[key] -= 1
            continue
        findings.append(
            Finding(
                Check.TYPES, "type-error", error.message, error.relpath, error.lineno
            )
        )
    return findings


def _new_test_failures(baseline: TestRun, current: TestRun) -> list[Finding]:
    findings = []
    for name, message in sorted(current.failures.items()):
        if name not in baseline.failures:
            findings.append(
                Finding(
                    Check.TESTS,
                    "test-failed",
                    message,
                    _path_of_test(name),
                    code_text=name,
                )
            )
    return findings


def _path_of_test(name: str) -> str | None:
    """The file of a test, from the dotted class name pytest reports."""
    dotted = name.partition("::")[0]
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        if parts[cut - 1].startswith("test") or parts[cut - 1].endswith("_test"):
            return "/".join(parts[:cut]) + ".py"
    return None

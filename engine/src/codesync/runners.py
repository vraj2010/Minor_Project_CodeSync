"""Running mypy and pytest on a copy of a project, and reading what they said.

Both are run as separate processes with a time limit. A rewrite that loops
forever is a result, not a hang."""

import importlib.util
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import clean_environment

_MYPY_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?:\d+:)? error: (?P<message>.*)$")
_TAIL = 400


@dataclass(frozen=True)
class TypeError_:
    relpath: str
    lineno: int
    message: str


@dataclass
class TypeRun:
    errors: list[TypeError_] = field(default_factory=list)
    problem: str | None = None

    def counts(self) -> Counter[tuple[str, str]]:
        """Errors without their line numbers, since a rewrite moves lines and an
        old error on a new line is still an old error."""
        return Counter((item.relpath, item.message) for item in self.errors)


@dataclass
class TestRun:
    __test__ = False

    failures: dict[str, str] = field(default_factory=dict)
    passed: set[str] = field(default_factory=set)
    problem: str | None = None
    timed_out: bool = False

    @property
    def total(self) -> int:
        return len(self.failures) + len(self.passed)


def mypy_available() -> bool:
    return importlib.util.find_spec("mypy") is not None


def run_mypy(root: Path, relpaths: list[str], *, cache: Path, timeout: float) -> TypeRun:
    """Type check `relpaths`. The project's own mypy settings are ignored, since
    they could load plugins and because errors are only compared with each
    other, so what matters is that both runs are made the same way."""
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file=",
        "--no-error-summary",
        "--hide-error-context",
        "--no-color-output",
        "--no-pretty",
        "--show-error-codes",
        "--ignore-missing-imports",
        "--check-untyped-defs",
        "--follow-imports=silent",
        "--explicit-package-bases",
        "--cache-dir",
        str(cache),
        *relpaths,
    ]
    env = clean_environment(root)
    env["MYPYPATH"] = env.pop("PYTHONPATH")
    try:
        finished = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TypeRun(problem=f"mypy did not finish in {timeout:g} seconds")

    if finished.returncode not in (0, 1):
        tail = (finished.stderr or finished.stdout).strip()[-_TAIL:]
        return TypeRun(problem=f"mypy could not run: {tail}")

    errors = []
    for line in finished.stdout.splitlines():
        match = _MYPY_LINE.match(line.strip())
        if match:
            errors.append(
                TypeError_(
                    match["path"].replace("\\", "/"),
                    int(match["line"]),
                    match["message"],
                )
            )
    return TypeRun(errors=errors)


def run_pytest(
    root: Path, nodeids: list[str] | None, *, python: str, timeout: float
) -> TestRun:
    """Run the named tests, or all of them, and read the outcome of each."""
    # A killed process can hold its files for a moment on Windows, and a folder
    # left behind is a far smaller problem than a check that crashes.
    with tempfile.TemporaryDirectory(
        prefix="codesync-report-", ignore_cleanup_errors=True
    ) as folder:
        report = Path(folder) / "report.xml"
        output = Path(folder) / "output.txt"
        command = [
            python,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            *(nodeids or []),
        ]
        try:
            with output.open("w", encoding="utf-8", errors="replace") as handle:
                finished = subprocess.run(
                    command,
                    cwd=root,
                    env=clean_environment(root),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return TestRun(
                problem=f"the tests did not finish in {timeout:g} seconds",
                timed_out=True,
            )

        if report.is_file():
            run = _read_report(report)
            if run.total or finished.returncode == 5:
                return run
        tail = output.read_text(encoding="utf-8", errors="replace").strip()[-_TAIL:]
        return TestRun(problem=f"pytest could not run: {tail}")


def _read_report(path: Path) -> TestRun:
    run = TestRun()
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        name = "::".join(
            item for item in (case.get("classname"), case.get("name")) if item
        )
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            run.failures[name] = _first_line(problem)
        elif case.find("skipped") is None:
            run.passed.add(name)
    return run


def _first_line(element: ElementTree.Element) -> str:
    text = element.get("message") or element.text or "failed"
    lines = text.strip().splitlines()
    return lines[0][:300] if lines else "failed"

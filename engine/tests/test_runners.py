import subprocess
import sys

from codesync.runners import run_pytest


def test_missing_import_during_collection_names_module_without_temp_traceback(tmp_path):
    (tmp_path / "conftest.py").write_text("import codesync_absent_dependency\n")
    run = run_pytest(tmp_path, None, python=sys.executable, timeout=30)
    assert run.total == 0
    assert "could not import 'codesync_absent_dependency'" in run.problem
    assert str(tmp_path) not in run.problem


def test_a_file_still_locked_after_a_timeout_does_not_crash_the_check(
    tmp_path, monkeypatch
):
    """On Windows a killed test run can hold its output file for a moment, which
    used to crash the cleanup instead of reporting that the tests never finished."""
    held = []

    def times_out(command, **options):
        held.append(open(options["stdout"].name, "a"))
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr("codesync.runners.subprocess.run", times_out)

    run = run_pytest(tmp_path, None, python=sys.executable, timeout=1)
    held[0].close()

    assert run.timed_out
    assert run.problem == "the tests did not finish in 1 seconds"

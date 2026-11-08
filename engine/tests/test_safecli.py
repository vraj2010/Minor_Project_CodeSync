import json

import pytest

from codesync import safecli
from codesync.cli import main

PROJECT = {
    "shop/__init__.py": "",
    "shop/prices.py": """
        def gross(amount: float, rate: float = 0.2) -> float:
            return amount * (1 + rate)

        def lonely(amount):
            return amount
    """,
    "shop/report.py": """
        from shop.prices import gross

        def total(amounts):
            return sum(gross(item, rate=0.1) for item in amounts)
    """,
    "tests/test_prices.py": """
        from shop.prices import gross

        def test_gross():
            assert gross(100) == 120
    """,
}
SOUND = (
    "def gross(amount: float, rate: float = 0.2) -> float:\n"
    "    return amount + amount * rate\n"
)
BREAKING = "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount\n"


@pytest.fixture
def project(write_project):
    return write_project(PROJECT)


@pytest.fixture
def rewrite(tmp_path_factory):
    def write(code):
        path = tmp_path_factory.mktemp("rewrite") / "rewrite.py"
        path.write_text(code, encoding="utf-8")
        return str(path)

    return write


def test_impact_answers_what_breaks(project, capsys):
    assert main(["impact", str(project), "shop.prices.gross"]) == 0

    output = capsys.readouterr().out
    assert "if shop.prices.gross changes" in output
    assert "shop/report.py:4" in output
    assert "tests/test_prices.py::test_gross" in output


def test_impact_says_when_nothing_reaches_a_function(project, capsys):
    assert main(["impact", str(project), "shop.prices.lonely"]) == 0

    assert "nothing else in the project reaches it" in capsys.readouterr().out


def test_impact_can_write_json(project, capsys):
    assert main(["impact", str(project), "shop.prices.gross", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["files"] == ["shop/report.py"]


def test_impact_of_a_variable_points_elsewhere(write_project, capsys):
    root = write_project({"a.py": "LIMIT = 1\n"})

    assert main(["impact", str(root), "a.LIMIT"]) == 2

    assert "codesync variable" in capsys.readouterr().err


def test_deps_lists_the_modules(project, capsys):
    assert main(["deps", str(project)]) == 0

    assert "shop.report" in capsys.readouterr().out


def test_deps_can_show_one_module_as_json(project, capsys):
    assert main(["deps", str(project), "--module", "shop.report", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)[0]["imports"] == ["shop.prices"]


def test_deps_of_an_unknown_module_says_so(project, capsys):
    assert main(["deps", str(project), "--module", "nope"]) == 1

    assert "no module is called nope" in capsys.readouterr().out


def test_verify_refuses_and_names_the_damage(project, rewrite, capsys):
    code = main(
        ["verify", str(project), "shop.prices.gross", rewrite(BREAKING), "--no-types"]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "shop.prices.gross: unsafe" in output
    assert "refused: 1 place would have stopped working" in output
    assert "shop/report.py:4  gross(item, rate=0.1)" in output


@pytest.mark.slow
def test_verify_can_apply_and_undo_can_reverse(project, rewrite, capsys):
    path = project / "shop" / "prices.py"
    original = path.read_bytes()

    code = main(
        [
            "verify",
            str(project),
            "shop.prices.gross",
            rewrite(SOUND),
            "--no-types",
            "--python",
            __import__("sys").executable,
            "--apply",
        ]
    )

    assert code == 0
    assert b"amount + amount * rate" in path.read_bytes()
    assert "`codesync undo` reverses it exactly" in capsys.readouterr().out

    assert main(["undo", str(project), "--list"]) == 0
    assert "shop.prices.gross" in capsys.readouterr().out

    assert main(["undo", str(project)]) == 0
    assert path.read_bytes() == original


def test_a_refused_change_is_not_applied(project, rewrite):
    path = project / "shop" / "prices.py"
    original = path.read_bytes()

    main(
        [
            "verify",
            str(project),
            "shop.prices.gross",
            rewrite(BREAKING),
            "--no-types",
            "--apply",
        ]
    )

    assert path.read_bytes() == original


def test_verify_can_write_json(project, rewrite, capsys):
    code = main(
        [
            "verify",
            str(project),
            "shop.prices.gross",
            rewrite(BREAKING),
            "--no-types",
            "--json",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["caught_by"] == "contract"


def test_verify_says_when_nothing_changed(write_project, rewrite, capsys):
    root = write_project({"a.py": "def f(x):\n    return x\n"})

    assert main(["verify", str(root), "a.f", rewrite("def f(x):\n    return x\n")]) == 0

    assert "the same as the function" in capsys.readouterr().out


def test_an_unusable_rewrite_file_is_explained(project, rewrite, capsys):
    code = main(["verify", str(project), "shop.prices.gross", rewrite("def gross(:\n")])

    assert code == 2
    assert "cannot be used" in capsys.readouterr().err


def test_undo_with_nothing_applied_says_so(project, capsys):
    assert main(["undo", str(project)]) == 2

    assert "no applied change" in capsys.readouterr().err


def test_undo_list_with_nothing_applied_says_so(project, capsys):
    assert main(["undo", str(project), "--list"]) == 0

    assert "nothing to undo" in capsys.readouterr().out


def improve(project, tmp_path, *extra):
    return main(
        [
            "improve",
            str(project),
            "shop.prices.gross",
            "--no-types",
            "--log",
            str(tmp_path / "runs.jsonl"),
            "--python",
            __import__("sys").executable,
            *extra,
        ]
    )


def fence(code):
    return f"```python\n{code}```"


@pytest.mark.slow
def test_improve_sends_a_failure_back_and_keeps_the_fix(
    project, tmp_path, scripted, monkeypatch, capsys
):
    client = scripted(fence(BREAKING), fence(SOUND))
    monkeypatch.setattr(safecli, "create_model_client", lambda args: client)

    assert improve(project, tmp_path) == 0

    output = capsys.readouterr().out
    assert "attempt 1: rejected by the contract check" in output
    assert "attempt 2: safe" in output
    assert "decision  kept" in output
    assert "+    return amount + amount * rate" in output
    record = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8"))
    assert record["decision"] == "kept"
    assert len(record["attempts"]) == 2


def test_improve_refuses_after_the_retries(
    project, tmp_path, scripted, monkeypatch, capsys
):
    client = scripted(*[fence(BREAKING)] * 4)
    monkeypatch.setattr(safecli, "create_model_client", lambda args: client)

    assert improve(project, tmp_path) == 1

    output = capsys.readouterr().out
    assert "decision  refused" in output
    assert "shop/report.py:4" in output


@pytest.mark.slow
def test_improve_can_apply_a_kept_change(
    project, tmp_path, scripted, monkeypatch, capsys
):
    client = scripted(fence(SOUND))
    monkeypatch.setattr(safecli, "create_model_client", lambda args: client)

    assert improve(project, tmp_path, "--apply", "--no-log") == 0

    source = (project / "shop" / "prices.py").read_text(encoding="utf-8")
    assert "amount + amount * rate" in source

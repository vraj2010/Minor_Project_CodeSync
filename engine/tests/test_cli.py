import json

from codesync.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_index_reports_a_summary(capsys, sample_project):
    code, out, _ = run(capsys, "index", str(sample_project))

    assert code == 0
    assert "modules    5" in out
    assert "functions  4" in out
    assert "parse failures" not in out


def test_index_emits_json(capsys, sample_project):
    code, out, _ = run(capsys, "index", str(sample_project), "--json")

    assert code == 0
    assert json.loads(out)["summary"]["class"] == 3


def test_index_lists_parse_failures(capsys, write_project):
    root = write_project({"broken.py": "def parse(line:\n    return line\n"})

    code, out, _ = run(capsys, "index", str(root))

    assert code == 0
    assert "parse failures (1)" in out
    assert "broken.py:1  syntax:" in out


def test_symbols_can_be_filtered_by_kind(capsys, sample_project):
    code, out, _ = run(capsys, "symbols", str(sample_project), "--kind", "class")

    assert code == 0
    assert "app.models.Record" in out
    assert "app.services.parser.parse_row" not in out


def test_symbols_can_be_filtered_by_module(capsys, sample_project):
    code, out, _ = run(capsys, "symbols", str(sample_project), "--module", "report")

    assert code == 0
    assert out.strip().startswith("function  report.summarise")


def test_symbols_reports_when_nothing_matches(capsys, sample_project):
    code, out, _ = run(capsys, "symbols", str(sample_project), "--name", "absent")

    assert code == 1
    assert out.strip() == "no symbols matched"


def test_at_names_the_enclosing_definitions(capsys, sample_project):
    code, out, _ = run(capsys, "at", str(sample_project), "app/services/parser.py:10")

    assert code == 0
    assert "in module app.services.parser" in out
    assert "app.services.parser.parse_row " in out
    assert "app.services.parser.parse_row.clean" in out


def test_at_accepts_a_windows_style_path(capsys, sample_project):
    code, out, _ = run(capsys, "at", str(sample_project), "app\\models.py:12")

    assert code == 0
    assert "app.models.Record.column" in out


def test_at_reports_a_line_outside_any_definition(capsys, sample_project):
    code, out, _ = run(capsys, "at", str(sample_project), "app/services/parser.py:1")

    assert code == 1
    assert "not inside any definition" in out


def test_at_rejects_a_location_without_a_line(capsys, sample_project):
    code, _, err = run(capsys, "at", str(sample_project), "app/models.py")

    assert code == 2
    assert "expected PATH:LINE" in err


def test_a_missing_project_is_an_error(capsys, tmp_path):
    code, _, err = run(capsys, "index", str(tmp_path / "absent"))

    assert code == 2
    assert "is not a directory" in err

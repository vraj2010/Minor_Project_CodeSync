import json

import pytest

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


DISCOUNT_REPLY = (
    "```python\ndef apply_discount(item: Item, percent):\n"
    "    if not 0 <= percent <= 100:\n"
    '        raise ValueError("percent must be between 0 and 100")\n'
    "    factor = (100 - percent) / 100\n"
    "    return Item(item.sku, item.price * factor, item.quantity)\n```"
)


@pytest.fixture
def model(monkeypatch, scripted):
    """Replace the real model client with a scripted one for the CLI."""

    def install(*replies):
        client = scripted(*replies)
        monkeypatch.setattr(
            "codesync.cliargs.create_client", lambda *args, **kwargs: client
        )
        return client

    return install


def test_usages_lists_every_reference(capsys, inventory_project):
    code, out, _ = run(
        capsys, "usages", str(inventory_project), "inventory.models.Stock.value"
    )

    assert code == 0
    assert out.startswith("inventory.models.Stock.value is used in 3 places")
    assert "super().value()" in out
    assert "stock.value()   [name only]" in out


def test_usages_emits_json(capsys, inventory_project):
    code, out, _ = run(
        capsys,
        "usages",
        str(inventory_project),
        "inventory.pricing.price_with_tax",
        "--json",
    )

    assert code == 0
    assert [item["kind"] for item in json.loads(out)] == ["import", "call"]


def test_usages_of_an_unused_symbol(capsys, inventory_project):
    code, out, _ = run(
        capsys, "usages", str(inventory_project), "inventory.report.discounted"
    )

    assert code == 0
    assert out.strip() == "nothing else in the project uses inventory.report.discounted"


def test_context_prints_the_prompt_without_calling_a_model(capsys, inventory_project):
    code, out, _ = run(
        capsys, "context", str(inventory_project), "inventory.pricing.apply_discount"
    )

    assert code == 0
    assert "mode      project" in out
    assert "----- system -----" in out
    assert "----- user -----" in out
    assert "## Contract" in out


def test_context_reports_what_the_budget_left_out(capsys, inventory_project):
    code, out, _ = run(
        capsys,
        "context",
        str(inventory_project),
        "inventory.pricing.apply_discount",
        "--budget",
        "10",
    )

    assert code == 0
    assert "omitted   module, callers, callees" in out


def test_refine_prints_the_diff_and_logs_the_run(
    capsys, tmp_path, inventory_project, model
):
    model(DISCOUNT_REPLY)
    log = tmp_path / "runs.jsonl"
    patch = tmp_path / "out" / "discount.patch"

    code, out, _ = run(
        capsys,
        "refine",
        str(inventory_project),
        "inventory.pricing.apply_discount",
        "--log",
        str(log),
        "--output",
        str(patch),
    )

    assert code == 0
    assert "outcome  changed  +2 -1 lines" in out
    assert "+    factor = (100 - percent) / 100" in out
    assert patch.read_text(encoding="utf-8").startswith("--- a/inventory/pricing.py")
    assert json.loads(log.read_text(encoding="utf-8"))["outcome"] == "changed"


def test_refine_accepts_a_location(capsys, tmp_path, inventory_project, model):
    client = model(DISCOUNT_REPLY)

    code, _, _ = run(
        capsys, "refine", str(inventory_project), "inventory/pricing.py:12", "--no-log"
    )

    assert code == 0
    assert "`inventory.pricing.apply_discount`" in client.requests[0][1]["content"]


def test_refine_fails_on_an_unusable_reply(capsys, inventory_project, model):
    model("Sorry, I can't do that.")

    code, out, _ = run(
        capsys,
        "refine",
        str(inventory_project),
        "inventory.pricing.apply_discount",
        "--no-log",
        "--show-reply",
    )

    assert code == 1
    assert "outcome  invalid-reply  the reply has no code block" in out
    assert "Sorry, I can't do that." in out


def test_refine_without_an_api_key_explains_what_to_set(
    capsys, monkeypatch, inventory_project
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code, _, err = run(
        capsys, "refine", str(inventory_project), "inventory.pricing.apply_discount"
    )

    assert code == 2
    assert "GEMINI_API_KEY" in err


def test_refine_rejects_a_class(capsys, inventory_project, model):
    model(DISCOUNT_REPLY)

    code, _, err = run(
        capsys, "refine", str(inventory_project), "inventory.models.Stock", "--no-log"
    )

    assert code == 2
    assert "is a class" in err


def test_batch_runs_every_task(capsys, tmp_path, inventory_project, model):
    model(DISCOUNT_REPLY, "no code here")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "root": str(inventory_project),
                        "target": "inventory.pricing.apply_discount",
                    },
                    {
                        "root": str(inventory_project),
                        "target": "inventory.models.Stock.add",
                    },
                    {
                        "root": str(inventory_project),
                        "target": "inventory.models.Missing",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    patches = tmp_path / "patches"

    code, out, _ = run(
        capsys,
        "batch",
        str(tasks),
        "--log",
        str(tmp_path / "runs.jsonl"),
        "--patches",
        str(patches),
    )

    assert code == 0
    assert "changed" in out.splitlines()[0]
    assert "invalid-reply" in out.splitlines()[1]
    assert "bad-target" in out.splitlines()[2]
    assert out.splitlines()[-1] == "3 tasks: changed 1, invalid-reply 1, bad-target 1"
    assert [path.name for path in patches.iterdir()] == [
        "001-inventory.pricing.apply_discount.patch"
    ]
    assert len((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2

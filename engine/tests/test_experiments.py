import json
import sys

import pytest

from codesync.cli import main
from codesync.comparison import ARMS, Task, load_tasks, plan, run_comparison
from codesync.index import build_index
from codesync.llm import Completion
from codesync.resolve import Resolver
from codesync.trapset import (
    DEFAULT_FOLDER,
    load_trapset,
    prepare,
    run_traps,
    validate,
)
from codesync.verify import Verdict, VerifyOptions

FAST = VerifyOptions(python=sys.executable, run_types=False)


@pytest.fixture(scope="module")
def trapset():
    return load_trapset()


def test_there_are_fifty_traps_and_some_controls(trapset):
    assert len(trapset.traps) == 50
    assert len(trapset.controls) >= 10


def test_every_id_is_used_once(trapset):
    ids = [item.id for item in trapset.everything]

    assert len(ids) == len(set(ids))


def test_every_trap_can_be_turned_into_a_patch(trapset):
    index = build_index(trapset.project)
    resolver = Resolver(index)

    for trap in trapset.everything:
        _, _, patch = prepare(index, resolver, trap)
        assert patch.changed, trap.id


def test_a_missing_trap_set_is_explained(tmp_path):
    with pytest.raises(FileNotFoundError, match="no trap set"):
        load_trapset(tmp_path)


def test_a_cheap_trap_is_scored_by_its_check(trapset):
    score = run_traps(trapset, options=FAST, only={"T01"})

    assert score.summary()["caught"] == 1
    assert score.summary()["caught_by_check"] == {"contract": 1}


@pytest.mark.slow
def test_a_score_counts_each_outcome(trapset):
    score = run_traps(trapset, options=FAST, only={"T01", "T37", "T47", "C02", "C10"})

    summary = score.summary()
    assert summary["traps"] == 3
    assert summary["caught"] == 1
    assert summary["missed"] == 1
    assert summary["unconfirmed"] == 1
    assert summary["controls_kept"] == 1
    assert summary["controls_unconfirmed"] == 1
    assert summary["by_category"]["signature"]["caught"] == 1


@pytest.mark.slow
def test_a_harmless_control_is_kept(trapset):
    result = run_traps(trapset, options=FAST, only={"C03"}).results[0]

    assert result.verdict is Verdict.SAFE


@pytest.mark.slow
def test_the_hidden_suite_is_what_proves_a_trap(tmp_path, trapset):
    """A small copy of the set in which one trap is really harmless."""
    from dataclasses import replace

    harmless = replace(trapset.controls[0], id="T99")
    broken = replace(
        trapset,
        traps=(trapset.traps[0], harmless),
        controls=(replace(trapset.traps[1], id="C99"),),
    )

    problems = validate(broken)

    assert any("T99 breaks nothing" in item for item in problems)
    assert any("C99 is meant to be harmless" in item for item in problems)


def test_the_task_file_loads_and_names_real_functions(trapset):
    tasks = load_tasks(DEFAULT_FOLDER / "tasks.json")
    index = build_index(trapset.project)

    assert len(tasks) >= 20
    for task in tasks:
        assert index.table.lookup(task.target) is not None, task.target


def test_a_plan_states_the_cost_before_anything_is_sent():
    tasks = [Task("a", "goal"), Task("b", "goal")]

    cost = plan(tasks, ARMS, repeats=2)

    assert cost["fewest_model_requests"] == 16
    assert cost["most_model_requests"] == 28
    assert cost["arms"] == ["plain", "map", "checks", "retry"]


BREAKING = (
    "```python\ndef tax_for(rate: float = TAX_RATE, amount: float = 0.0) -> float:\n"
    "    return round_money(amount * rate)\n```"
)
SOUND = (
    "```python\ndef tax_for(amount: float, rate: float = TAX_RATE) -> float:\n"
    "    tax = amount * rate\n    return round_money(tax)\n```"
)


class StubbornThenFixed:
    """Writes the broken version until it is told what broke."""

    model = "stub"

    def complete(self, messages):
        told = any("rejected" in item["content"] for item in messages)
        return Completion(SOUND if told else BREAKING, self.model, 0.01)


@pytest.mark.slow
def test_the_four_arms_are_told_apart(trapset):
    comparison = run_comparison(
        StubbornThenFixed(),
        [Task("ledger.pricing.tax_for", "Improve the interface.")],
        trapset=trapset,
        options=FAST,
    )

    arms = {name: item.to_dict() for name, item in comparison.tallies.items()}
    assert arms["plain"]["kept_and_broken"] == 1
    assert arms["map"]["kept_and_broken"] == 1
    assert arms["checks"]["refused"] == 1
    assert arms["checks"]["kept"] == 0
    assert arms["retry"]["kept"] == 1
    assert arms["retry"]["kept_and_broken"] == 0
    assert arms["retry"]["model_requests"] == 2


@pytest.mark.slow
def test_a_comparison_can_be_saved_as_json(trapset):
    comparison = run_comparison(
        StubbornThenFixed(),
        [Task("ledger.pricing.tax_for", "Improve the interface.")],
        trapset=trapset,
        arms=ARMS[:1],
        options=FAST,
    )

    data = json.loads(json.dumps(comparison.to_dict()))

    assert data["model"] == "stub"
    assert data["runs"][0]["hidden_failures"]


def test_compare_can_show_its_cost_without_a_key(capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert main(["compare", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "fewest model requests" in output
    assert "most model requests" in output


def test_traps_can_be_scored_from_the_command_line(capsys, tmp_path):
    output = tmp_path / "score.json"

    code = main(["traps", "--only", "T01", "--no-types", "--output", str(output)])

    assert code == 0
    assert "caught       1 of 1" in capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["caught"] == 1

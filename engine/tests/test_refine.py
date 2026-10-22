import json

from codesync.context import ContextMode
from codesync.llm import ProviderError
from codesync.refine import Outcome, append_record, refine

DISCOUNT = """\
Here is a clearer version:

```python
def apply_discount(item: Item, percent):
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    factor = (100 - percent) / 100
    return Item(item.sku, item.price * factor, item.quantity)
```
"""


def run(index, qualname, client, **options):
    return refine(index, index.table.lookup(qualname), client, **options)


def test_a_rewrite_becomes_a_patch(inventory_index, scripted):
    result = run(
        inventory_index, "inventory.pricing.apply_discount", scripted(DISCOUNT)
    )

    assert result.outcome is Outcome.CHANGED
    assert result.patch.relpath == "inventory/pricing.py"
    assert "+    factor = (100 - percent) / 100\n" in result.patch.diff()
    assert result.patch.line_counts() == (2, 1)


def test_nothing_is_written_to_the_project(
    inventory_project, inventory_index, scripted
):
    path = inventory_project / "inventory" / "pricing.py"
    before = path.read_bytes()

    run(inventory_index, "inventory.pricing.apply_discount", scripted(DISCOUNT))

    assert path.read_bytes() == before


def test_the_model_sees_the_project_context(inventory_index, scripted):
    client = scripted(DISCOUNT)

    run(inventory_index, "inventory.pricing.apply_discount", client)

    request = client.requests[0][1]["content"]
    assert "## Contract" in request
    assert "pricing.apply_discount(item, percent)" in request


def test_baseline_mode_sends_only_the_function(inventory_index, scripted):
    client = scripted(DISCOUNT)

    result = run(
        inventory_index,
        "inventory.pricing.apply_discount",
        client,
        mode=ContextMode.BASELINE,
    )

    assert result.outcome is Outcome.CHANGED
    assert "## Contract" not in client.requests[0][1]["content"]


def test_a_method_returned_inside_its_class_is_spliced(inventory_index, scripted):
    reply = (
        "```python\nclass Stock:\n    def add(self, item):\n"
        "        if item is None:\n            raise ValueError('item is required')\n"
        "        self.items.append(item)\n        return self\n```"
    )

    result = run(inventory_index, "inventory.models.Stock.add", scripted(reply))

    assert result.outcome is Outcome.CHANGED
    assert "+        if item is None:\n" in result.patch.diff()


def test_an_identical_rewrite_is_unchanged(inventory_index, scripted):
    reply = (
        "```python\ndef add(self, item):\n"
        "    self.items.append(item)\n    return self\n```"
    )

    result = run(inventory_index, "inventory.models.Stock.add", scripted(reply))

    assert result.outcome is Outcome.UNCHANGED
    assert result.outcome.has_patch


def test_an_unusable_reply_is_recorded_with_its_reason(inventory_index, scripted):
    result = run(
        inventory_index,
        "inventory.pricing.apply_discount",
        scripted("I would rather not."),
    )

    assert result.outcome is Outcome.INVALID_REPLY
    assert result.reason == "no-code"
    assert result.patch is None
    assert not result.outcome.has_patch


def test_a_provider_failure_is_an_outcome_not_a_crash(inventory_index, scripted):
    result = run(
        inventory_index,
        "inventory.pricing.apply_discount",
        scripted(ProviderError("503: overloaded")),
    )

    assert result.outcome is Outcome.PROVIDER_FAILED
    assert result.error == "503: overloaded"
    assert result.completion is None


def test_the_record_holds_everything_needed_to_explain_the_run(
    inventory_index, scripted
):
    result = run(
        inventory_index, "inventory.pricing.apply_discount", scripted(DISCOUNT)
    )

    record = json.loads(json.dumps(result.to_record()))

    assert record["target"] == "inventory.pricing.apply_discount"
    assert record["outcome"] == "changed"
    assert record["model"] == "scripted-model"
    assert record["prompt_tokens"] == 400
    assert record["added_lines"] == 2
    assert record["renamed"] is False
    assert record["context"]["mode"] == "project"
    assert record["diff"].startswith("--- a/inventory/pricing.py")
    assert record["messages"][0]["role"] == "system"
    assert record["reply"] == DISCOUNT


def test_records_are_appended_as_json_lines(tmp_path, inventory_index, scripted):
    log = tmp_path / "runs" / "log.jsonl"
    result = run(
        inventory_index, "inventory.pricing.apply_discount", scripted(DISCOUNT)
    )

    append_record(log, result.to_record())
    append_record(log, result.to_record())

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["outcome"] == "changed"

import ast
from textwrap import dedent

from codesync.context import (
    ContextMode,
    SectionKind,
    build_context,
    estimate_tokens,
    instance_attributes,
    outline,
    signature,
)
from codesync.index import build_index
from codesync.prompts import BASELINE_SYSTEM, PROJECT_SYSTEM, build_messages
from codesync.resolve import Resolver


def context_for(resolver, index, qualname, **options):
    return build_context(resolver, index.table.lookup(qualname), **options)


def kinds(sections):
    return [section.kind for section in sections]


def test_baseline_sends_the_function_alone(resolver, inventory_index):
    context = context_for(
        resolver,
        inventory_index,
        "inventory.pricing.apply_discount",
        mode=ContextMode.BASELINE,
    )

    assert kinds(context.sections) == [SectionKind.TARGET]
    assert context.references == ()
    assert "def apply_discount(item: Item, percent):" in context.sections[0].body


def test_project_mode_adds_contract_callees_and_callers(resolver, inventory_index):
    context = context_for(resolver, inventory_index, "inventory.pricing.apply_discount")

    assert set(kinds(context.sections)) == {
        SectionKind.TARGET,
        SectionKind.CONTRACT,
        SectionKind.MODULE,
        SectionKind.CALLEES,
        SectionKind.CALLERS,
    }
    assert context.omitted == ()


def test_the_contract_lists_every_usage(resolver, inventory_index):
    context = context_for(resolver, inventory_index, "inventory.models.Stock.value")
    contract = next(s for s in context.sections if s.kind is SectionKind.CONTRACT)

    assert "def value(self):" in contract.body
    assert "It is used in 3 places:" in contract.body
    assert "- inventory/models.py:28: super().value()" in contract.body
    assert (
        "- inventory/report.py:10: stock.value()  (matched by name only)"
        in contract.body
    )


def test_an_unused_function_still_keeps_its_signature(write_project):
    root = write_project({"tools.py": "def unused(value):\n    return value\n"})
    index = build_index(root)

    context = context_for(Resolver(index), index, "tools.unused")
    contract = next(s for s in context.sections if s.kind is SectionKind.CONTRACT)

    assert "Nothing else in this project uses it" in contract.body


def test_a_method_comes_with_its_class_outline(resolver, inventory_index):
    context = context_for(resolver, inventory_index, "inventory.models.Stock.add")
    section = next(s for s in context.sections if s.kind is SectionKind.CLASS)

    assert "class Stock:" in section.body
    assert "def value(self):" in section.body
    assert "self.items.append" not in section.body
    assert "Instance attributes set in the class: items" in section.body


def test_a_called_class_is_shown_by_its_fields(resolver, inventory_index):
    context = context_for(resolver, inventory_index, "inventory.pricing.apply_discount")
    callee = next(s for s in context.sections if s.kind is SectionKind.CALLEES)

    assert callee.label == "inventory.models.Item"
    assert "sku: str" in callee.body
    assert "quantity: int = 0" in callee.body
    assert "def total" not in callee.body


def test_callers_are_shown_with_their_code(resolver, inventory_index):
    context = context_for(resolver, inventory_index, "inventory.pricing.price_with_tax")
    callers = [s for s in context.sections if s.kind is SectionKind.CALLERS]

    assert len(callers) == 1
    assert "`inventory.report.summary`" in callers[0].body
    assert "gross(item)" in callers[0].body


def test_a_tight_budget_keeps_only_what_is_required(resolver, inventory_index):
    context = context_for(
        resolver, inventory_index, "inventory.pricing.apply_discount", budget=10
    )

    assert kinds(context.sections) == [SectionKind.TARGET, SectionKind.CONTRACT]
    assert {section.kind for section in context.omitted} == {
        SectionKind.MODULE,
        SectionKind.CALLEES,
        SectionKind.CALLERS,
    }
    assert context.summary()["omitted"]


def test_sections_are_added_in_rank_order_until_the_budget_runs_out(
    resolver, inventory_index
):
    full = context_for(resolver, inventory_index, "inventory.pricing.apply_discount")
    required = sum(s.tokens for s in full.sections if s.required)
    module = next(s for s in full.sections if s.kind is SectionKind.MODULE)

    context = context_for(
        resolver,
        inventory_index,
        "inventory.pricing.apply_discount",
        budget=required + module.tokens,
    )

    assert SectionKind.MODULE in kinds(context.sections)
    assert SectionKind.CALLEES not in kinds(context.sections)


def test_recursive_calls_are_not_listed_as_callers(write_project):
    root = write_project(
        {
            "maths.py": """
            def factorial(n):
                return 1 if n <= 1 else n * factorial(n - 1)
            """
        }
    )
    index = build_index(root)

    context = context_for(Resolver(index), index, "maths.factorial")

    assert SectionKind.CALLERS not in kinds(context.sections)
    assert len(context.references) == 1


def test_signature_keeps_the_first_docstring_line():
    node = ast.parse(
        dedent(
            '''
            def parse(line, sep=","):
                """Split a line.

                Longer explanation.
                """
                return line.split(sep)
            '''
        )
    ).body[0]

    assert (
        signature(node)
        == 'def parse(line, sep=\',\'):\n    """Split a line."""\n    ...'
    )


def test_outline_can_keep_only_the_constructor():
    node = ast.parse(
        dedent(
            """
            class Reader:
                limit = 10

                def __init__(self, path):
                    self.path = path

                def read(self):
                    return open(self.path).read()
            """
        )
    ).body[0]

    short = outline(node, all_methods=False)

    assert "limit = 10" in short
    assert "def __init__(self, path):" in short
    assert "def read" not in short
    assert "def read(self):" in outline(node, all_methods=True)
    assert instance_attributes(node) == ["path"]


def test_token_estimate_rounds_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2


def test_messages_use_the_system_prompt_for_the_mode(resolver, inventory_index):
    project = context_for(resolver, inventory_index, "inventory.pricing.apply_discount")
    baseline = context_for(
        resolver,
        inventory_index,
        "inventory.pricing.apply_discount",
        mode=ContextMode.BASELINE,
    )

    assert build_messages(project)[0] == {"role": "system", "content": PROJECT_SYSTEM}
    assert build_messages(baseline)[0] == {"role": "system", "content": BASELINE_SYSTEM}


def test_the_request_orders_sections_and_ends_with_the_reply_format(
    resolver, inventory_index
):
    context = context_for(resolver, inventory_index, "inventory.models.Stock.add")

    request = build_messages(context)[1]["content"]

    headings = [line for line in request.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Definition to rewrite",
        "## Contract",
        "## Enclosing class",
        "## Module",
        "## Code that uses it",
        "## Reply",
    ]
    assert request.startswith("Goal: ")
    assert "complete rewritten definition of `add`" in request
    assert "not inside its class" in request


def test_both_modes_ask_for_the_same_reply_format(resolver, inventory_index):
    qualname = "inventory.pricing.apply_discount"
    project = build_messages(context_for(resolver, inventory_index, qualname))
    baseline = build_messages(
        context_for(resolver, inventory_index, qualname, mode=ContextMode.BASELINE)
    )

    assert (
        project[1]["content"].split("## Reply")[1]
        == (baseline[1]["content"].split("## Reply")[1])
    )

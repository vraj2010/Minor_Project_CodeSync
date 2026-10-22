import json

import pytest

from codesync.annotate import AnnotationCache
from codesync.index import build_index
from codesync.variables import VariableKind
from codesync.varmap import build_variable_map, parse_kinds


@pytest.fixture(scope="module")
def static_map(shop_project):
    return build_variable_map(build_index(shop_project))


@pytest.fixture
def described_map(shop_project, annotator):
    return build_variable_map(build_index(shop_project), client=annotator(), workers=1)


def names(variables):
    return [variable.qualname for variable in variables]


def test_without_a_model_the_map_is_static_only(static_map):
    assert static_map.annotations == {}
    assert static_map.usage is None
    assert static_map.model is None
    assert len(static_map.table) == 30


def test_a_model_describes_the_variables(described_map):
    ok = [
        item for item in described_map.annotations.values() if item.status.value == "ok"
    ]

    assert len(ok) == 30
    assert described_map.model == "annotator-1"
    assert described_map.usage.requests > 0


def test_the_cache_is_used_when_given(shop_project, annotator):
    cache = AnnotationCache()
    build_variable_map(
        build_index(shop_project), client=annotator(), cache=cache, workers=1
    )
    client = annotator()

    build_variable_map(build_index(shop_project), client=client, cache=cache, workers=1)

    assert client.calls == 0


def test_variables_can_be_selected_by_kind_and_module(static_map):
    assert len(static_map.select(kind="module")) == 4
    assert names(static_map.select(kind="module", module="shop.config")) == [
        "shop.config.TAX",
        "shop.config.LIMITS",
        "shop.config.counter",
    ]


def test_variables_can_be_selected_by_container_and_name(static_map):
    assert [v.name for v in static_map.select(container="shop.cart.Cart.add")] == [
        "self",
        "item",
        "qty",
        "subtotal",
        "scale",
        "names",
        "name",
        "outer",
    ]
    assert names(static_map.select(name="items")) == [
        "shop.cart.Base.items",
        "shop.cart.Cart.items",
    ]


def test_a_query_matches_part_of_the_qualified_name(static_map):
    assert names(static_map.select(query="CONFIG.TAX")) == ["shop.config.TAX"]


def test_cross_file_variables_can_be_selected(static_map):
    crossing = static_map.select(cross_file=True)

    assert names(crossing) == [
        "shop.config.TAX",
        "shop.config.LIMITS",
        "shop.config.counter",
    ]
    assert len(static_map.select(cross_file=False)) == 27


def test_described_variables_can_be_selected_by_annotation(described_map):
    assert len(described_map.select(annotated=True)) == 30
    assert described_map.select(annotated=False) == []
    assert len(described_map.select(role="state")) == 26
    assert len(described_map.select(role="receiver")) == 4


def test_nothing_is_annotated_without_a_model(static_map):
    assert static_map.select(annotated=True) == []
    assert len(static_map.select(annotated=False)) == 30


def test_an_entry_combines_the_facts_in_one_record(described_map):
    entry = described_map.entry("shop.config.TAX")

    assert entry["kind"] == "module"
    assert entry["static_type"] == "float"
    assert (entry["reads"], entry["writes"]) == (1, 1)
    assert entry["external_files"] == ["shop/cart.py"]
    assert entry["ai"]["status"] == "ok"
    assert "accesses" not in entry


def test_an_entry_can_include_every_access(static_map):
    entry = static_map.entry("shop.config.counter", accesses=True)

    assert [(item["path"], item["mode"]) for item in entry["accesses"]] == [
        ("shop/cart.py", "read"),
        ("shop/config.py", "write"),
        ("shop/config.py", "read"),
        ("shop/config.py", "write"),
        ("shop/config.py", "read"),
    ]
    assert entry["ai"] is None


def test_an_unknown_variable_has_no_entry(static_map):
    assert static_map.entry("shop.nothing") is None


def test_the_summary_counts_what_the_map_holds(described_map):
    summary = described_map.summary()

    assert summary["variables"] == 30
    assert summary["by_kind"]["parameter"] == 13
    assert summary["cross_file"] == 3
    assert summary["annotations"] == {"ok": 30, "missing": 0, "failed": 0, "skipped": 0}
    assert summary["roles"] == {"state": 26, "receiver": 4}
    assert summary["model"] == "annotator-1"
    assert summary["usage"]["requests"] == described_map.usage.requests


def test_the_map_serialises_to_json(described_map):
    document = json.loads(json.dumps(described_map.to_dict()))

    assert document["summary"]["variables"] == 30
    assert len(document["variables"]) == 30
    assert document["variables"][0]["ai"]["status"] == "ok"


def test_the_kinds_to_describe_can_be_parsed():
    assert parse_kinds(None) == frozenset(VariableKind)
    assert parse_kinds(["module", "class"]) == {VariableKind.MODULE, VariableKind.CLASS}


def test_an_unknown_kind_lists_the_valid_ones():
    with pytest.raises(ValueError, match="the kinds are module, class, instance"):
        parse_kinds(["modul"])

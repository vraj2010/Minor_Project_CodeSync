from codesync.index import build_index
from codesync.references import Confidence, UsageKind, find_callees, find_references
from codesync.resolve import Resolver


def usages(resolver, index, qualname):
    target = index.table.lookup(qualname)
    return [
        (ref.location, ref.kind, ref.confidence)
        for ref in find_references(resolver, target)
    ]


def test_an_aliased_import_is_followed_to_its_calls(resolver, inventory_index):
    found = usages(resolver, inventory_index, "inventory.pricing.price_with_tax")

    assert found == [
        ("inventory/report.py:2", UsageKind.IMPORT, Confidence.RESOLVED),
        ("inventory/report.py:9", UsageKind.CALL, Confidence.RESOLVED),
    ]


def test_a_call_through_a_module_attribute_is_found(resolver, inventory_index):
    found = usages(resolver, inventory_index, "inventory.pricing.apply_discount")

    assert found == [("inventory/report.py:15", UsageKind.CALL, Confidence.RESOLVED)]


def test_imports_annotations_and_calls_all_count(resolver, inventory_index):
    found = usages(resolver, inventory_index, "inventory.models.Item")

    assert found == [
        ("inventory/__init__.py:1", UsageKind.IMPORT, Confidence.RESOLVED),
        ("inventory/pricing.py:1", UsageKind.IMPORT, Confidence.RESOLVED),
        ("inventory/pricing.py:10", UsageKind.REFERENCE, Confidence.RESOLVED),
        ("inventory/pricing.py:13", UsageKind.CALL, Confidence.RESOLVED),
        ("scripts/restock.py:5", UsageKind.CALL, Confidence.RESOLVED),
    ]


def test_method_calls_on_unknown_values_are_name_only(resolver, inventory_index):
    found = usages(resolver, inventory_index, "inventory.models.Stock.value")

    assert found == [
        ("inventory/models.py:28", UsageKind.CALL, Confidence.RESOLVED),
        ("inventory/report.py:10", UsageKind.CALL, Confidence.NAME_ONLY),
        ("scripts/restock.py:6", UsageKind.CALL, Confidence.NAME_ONLY),
    ]


def test_constructing_the_class_counts_as_calling_init(resolver, inventory_index):
    found = usages(resolver, inventory_index, "inventory.models.Stock.__init__")

    assert found == [("inventory/report.py:8", UsageKind.CALL, Confidence.RESOLVED)]


def test_a_subclass_without_its_own_init_reaches_the_parent_init(write_project):
    root = write_project(
        {
            "shapes.py": """
            class Shape:
                def __init__(self, name):
                    self.name = name


            class Square(Shape):
                pass


            def build():
                return Square("square")
            """
        }
    )
    index = build_index(root)

    found = usages(Resolver(index), index, "shapes.Shape.__init__")

    assert found == [("shapes.py:11", UsageKind.CALL, Confidence.RESOLVED)]


def test_references_carry_their_scope_and_text(resolver, inventory_index):
    target = inventory_index.table.lookup("inventory.pricing.price_with_tax")

    call = find_references(resolver, target)[1]

    assert call.scope.qualname == "inventory.report.summary"
    assert call.text == "gross(item)"
    assert call.to_dict()["kind"] == "call"


def test_a_name_reused_for_something_else_is_not_a_reference(write_project):
    root = write_project(
        {
            "a.py": "def total(values):\n    return sum(values)\n",
            "b.py": "def run(total):\n    return total + 1\n",
        }
    )
    index = build_index(root)

    assert usages(Resolver(index), index, "a.total") == []


def test_callees_are_project_symbols_called_in_the_body(resolver, inventory_index):
    summary = inventory_index.table.lookup("inventory.report.summary")

    callees = [
        (item.symbol.qualname, item.count) for item in find_callees(resolver, summary)
    ]

    assert callees == [
        ("inventory.models.Stock", 1),
        ("inventory.pricing.price_with_tax", 1),
    ]


def test_a_super_call_is_a_callee(resolver, inventory_index):
    value = inventory_index.table.lookup("inventory.models.PerishableStock.value")

    callees = [item.symbol.qualname for item in find_callees(resolver, value)]

    assert callees == ["inventory.models.Stock.value"]


def test_nested_helpers_are_not_callees(sample_project):
    index = build_index(sample_project)
    parse_row = index.table.lookup("app.services.parser.parse_row")

    callees = [
        item.symbol.qualname for item in find_callees(Resolver(index), parse_row)
    ]

    assert callees == ["app.models.RecordError", "app.models.Record"]

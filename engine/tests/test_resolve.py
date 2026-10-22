import ast

import pytest

from codesync.index import build_index
from codesync.resolve import Resolver, absolute_module
from codesync.symbols import ModuleInfo


def expr(text: str) -> ast.expr:
    return ast.parse(text, mode="eval").body


@pytest.mark.parametrize(
    ("module", "is_package", "imported", "level", "expected"),
    [
        ("pkg.mod", False, "other", 1, "pkg.other"),
        ("pkg.mod", False, None, 1, "pkg"),
        ("pkg.sub.mod", False, "other", 2, "pkg.other"),
        ("pkg.sub", True, "mod", 1, "pkg.sub.mod"),
        ("pkg.sub", True, None, 2, "pkg"),
        ("pkg.mod", False, "json", 0, "json"),
        ("pkg.mod", False, "x", 3, None),
        ("script", False, "x", 1, None),
    ],
)
def test_relative_imports_become_absolute(
    module, is_package, imported, level, expected
):
    info = ModuleInfo(name=module, relpath="", is_package=is_package, line_count=0)

    assert absolute_module(info, imported, level) == expected


def test_imports_bind_local_names_to_dotted_paths(resolver):
    bindings = resolver.imports("inventory/report.py").bindings

    assert bindings["Stock"] == "inventory.Stock"
    assert bindings["gross"] == "inventory.pricing.price_with_tax"
    assert bindings["pricing"] == "inventory.pricing"


def test_plain_imports_bind_the_top_package(write_project):
    root = write_project(
        {
            "app/__init__.py": "",
            "app/main.py": "import os.path\nimport json as j\n",
        }
    )

    bindings = Resolver(build_index(root)).imports("app/main.py").bindings

    assert bindings == {"os": "os", "j": "json"}


def test_star_imports_are_recorded_by_module(resolver):
    assert resolver.imports("scripts/restock.py").star_modules == ["inventory.models"]


def test_canonical_follows_a_re_export(resolver, inventory_index):
    stock = inventory_index.table.lookup("inventory.models.Stock")

    assert resolver.canonical("inventory.Stock") == stock
    assert (
        resolver.canonical("inventory.Stock.add").qualname
        == "inventory.models.Stock.add"
    )


def test_canonical_finds_inherited_members(resolver):
    inherited = resolver.canonical("inventory.models.PerishableStock.add")
    overridden = resolver.canonical("inventory.models.PerishableStock.value")

    assert inherited.qualname == "inventory.models.Stock.add"
    assert overridden.qualname == "inventory.models.PerishableStock.value"


def test_canonical_is_none_outside_the_project(resolver):
    assert resolver.canonical("os.path.join") is None
    assert resolver.canonical("inventory.models.Stock.missing") is None


def test_bases_resolve_to_dotted_paths(resolver, inventory_index):
    perishable = inventory_index.table.lookup("inventory.models.PerishableStock")

    assert resolver.bases(perishable) == ["inventory.models.Stock"]


def test_self_resolves_to_the_enclosing_class(resolver, inventory_index):
    value = inventory_index.table.lookup("inventory.models.Stock.value")

    assert resolver.dotted("inventory/models.py", expr("self.items"), value) == (
        "inventory.models.Stock.items"
    )


def test_super_resolves_to_the_first_base(resolver, inventory_index):
    value = inventory_index.table.lookup("inventory.models.PerishableStock.value")

    path = resolver.dotted("inventory/models.py", expr("super().value"), value)

    assert path == "inventory.models.Stock.value"


def test_an_instance_created_in_place_resolves_to_its_class(resolver, inventory_index):
    summary = inventory_index.table.lookup("inventory.report.summary")

    path = resolver.dotted("inventory/report.py", expr("Stock([]).value"), summary)

    assert resolver.canonical(path).qualname == "inventory.models.Stock.value"


def test_star_imported_names_resolve(resolver, inventory_index):
    restock = inventory_index.table.lookup("restock.restock")

    path = resolver.dotted("scripts/restock.py", expr("Item"), restock)

    assert resolver.canonical(path).qualname == "inventory.models.Item"


def test_a_local_variable_is_left_unresolved(resolver, inventory_index):
    summary = inventory_index.table.lookup("inventory.report.summary")

    assert resolver.dotted("inventory/report.py", expr("stock.value"), summary) is None


def test_nested_functions_resolve_from_inside_their_parent(sample_project):
    index = build_index(sample_project)
    parse_row = index.table.lookup("app.services.parser.parse_row")

    path = Resolver(index).dotted("app/services/parser.py", expr("clean"), parse_row)

    assert path == "app.services.parser.parse_row.clean"


def test_node_for_returns_the_definition(resolver, inventory_index):
    symbol = inventory_index.table.lookup("inventory.pricing.apply_discount")

    node = resolver.node_for(symbol)

    assert isinstance(node, ast.FunctionDef)
    assert node.name == "apply_discount"

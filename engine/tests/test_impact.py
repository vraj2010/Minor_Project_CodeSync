import pytest

from codesync.impact import (
    analyze_impact,
    dependency_map,
    find_covering_tests,
    is_test_file,
)
from codesync.index import build_index
from codesync.references import Confidence
from codesync.resolve import Resolver

PROJECT = {
    "shop/__init__.py": "",
    "shop/prices.py": """
        def net(amount):
            return amount

        def gross(amount):
            return net(amount) * 1.2

        def unused(amount):
            return amount
    """,
    "shop/cart.py": """
        from .prices import gross

        class Cart:
            def __init__(self):
                self.lines = []

            def add(self, amount):
                self.lines.append(amount)

            def total(self):
                return sum(gross(line) for line in self.lines)
    """,
    "shop/report.py": """
        from shop.cart import Cart

        def summary(cart: Cart):
            return cart.total()
    """,
    "tests/conftest.py": """
        import pytest
        from shop.cart import Cart

        @pytest.fixture
        def filled_cart():
            cart = Cart()
            cart.add(10)
            return cart
    """,
    "tests/test_prices.py": """
        from shop.prices import gross, net

        CASES = [gross(1)]

        def test_net():
            assert net(1) == 1

        class TestGross:
            def test_gross(self):
                assert gross(1) == 1.2

        def helper():
            return gross(2)

        def test_through_a_helper():
            assert helper() == 2.4
    """,
    "tests/test_cart.py": """
        from shop.report import summary

        def test_summary(filled_cart):
            assert summary(filled_cart) == 12

        def test_the_fixture_alone(filled_cart):
            assert filled_cart.lines == [10]
    """,
}


@pytest.fixture
def project(write_project):
    index = build_index(write_project(PROJECT))
    return index, Resolver(index)


def impact_of(project, qualname, **options):
    index, resolver = project
    return analyze_impact(resolver, index.table.lookup(qualname), **options)


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("tests/test_a.py", True),
        ("test_a.py", True),
        ("pkg/a_test.py", True),
        ("tests/helpers.py", True),
        ("pkg/conftest.py", True),
        ("pkg/latest.py", False),
        ("pkg/contest/a.py", False),
    ],
)
def test_test_files_are_recognised(relpath, expected):
    assert is_test_file(relpath) is expected


def test_a_change_reaches_the_callers_of_its_callers(project):
    impact = impact_of(project, "shop.prices.net")

    reached = {item.symbol.qualname: item.depth for item in impact.affected}

    assert reached["shop.prices.gross"] == 1
    assert reached["shop.cart.Cart.total"] == 2
    assert reached["shop.report.summary"] == 3


def test_each_reached_definition_shows_its_route(project):
    impact = impact_of(project, "shop.prices.net")

    summary = next(
        item
        for item in impact.affected
        if item.symbol.qualname == "shop.report.summary"
    )

    assert summary.via == (
        "shop.prices.net",
        "shop.prices.gross",
        "shop.cart.Cart.total",
    )


def test_the_files_affected_leave_out_the_target_file_and_the_tests(project):
    impact = impact_of(project, "shop.prices.net")

    assert impact.files == ("shop/cart.py", "shop/report.py")


def test_a_definition_nothing_uses_affects_nothing(project):
    impact = impact_of(project, "shop.prices.unused")

    assert impact.direct == ()
    assert impact.affected == ()
    assert impact.tests == ()
    assert impact.files == ()


def test_a_test_that_calls_the_target_covers_it(project):
    index, resolver = project

    found = find_covering_tests(resolver, index.table.lookup("shop.prices.net"))

    assert found[0].nodeid == "tests/test_prices.py::test_net"
    assert found[0].depth == 1


def test_a_test_in_a_class_is_named_with_its_class(project):
    nodeids = [item.nodeid for item in impact_of(project, "shop.prices.gross").tests]

    assert "tests/test_prices.py::TestGross::test_gross" in nodeids


def test_a_test_reaching_it_through_a_helper_covers_it(project):
    nodeids = [item.nodeid for item in impact_of(project, "shop.prices.gross").tests]

    assert "tests/test_prices.py::test_through_a_helper" in nodeids


def test_a_module_level_use_in_a_test_file_selects_the_whole_file(project):
    nodeids = [item.nodeid for item in impact_of(project, "shop.prices.gross").tests]

    assert "tests/test_prices.py" in nodeids


def test_a_test_reached_through_a_fixture_covers_it(project):
    nodeids = [item.nodeid for item in impact_of(project, "shop.cart.Cart.add").tests]

    assert "tests/test_cart.py::test_the_fixture_alone" in nodeids


def test_tests_are_listed_nearest_first(project):
    depths = [item.depth for item in impact_of(project, "shop.prices.net").tests]

    assert depths == sorted(depths)


def test_a_route_through_a_name_only_match_stays_name_only(write_project):
    index = build_index(
        write_project(
            {
                "core.py": """
                    class Engine:
                        def start(self):
                            return 1
                """,
                "use.py": """
                    def launch(thing):
                        return thing.start()

                    def outer(thing):
                        return launch(thing)
                """,
            }
        )
    )

    impact = analyze_impact(Resolver(index), index.table.lookup("core.Engine.start"))

    assert {item.confidence for item in impact.affected} == {Confidence.NAME_ONLY}
    assert [item.symbol.qualname for item in impact.affected] == [
        "use.launch",
        "use.outer",
    ]


def test_recursion_does_not_loop(write_project):
    index = build_index(
        write_project(
            {
                "calc.py": """
                    def even(n):
                        return n == 0 or odd(n - 1)

                    def odd(n):
                        return n != 0 and even(n - 1)
                """
            }
        )
    )

    impact = analyze_impact(Resolver(index), index.table.lookup("calc.even"))

    assert [item.symbol.qualname for item in impact.affected] == ["calc.odd"]


def test_a_walk_that_stops_early_says_so(project):
    shallow = impact_of(project, "shop.prices.net", max_depth=1)
    full = impact_of(project, "shop.prices.net")

    assert shallow.truncated
    assert not full.truncated
    assert len(shallow.affected) < len(full.affected)


def test_the_impact_can_be_written_as_json(project):
    data = impact_of(project, "shop.prices.net").to_dict()

    assert data["target"] == "shop.prices.net"
    assert data["files"] == ["shop/cart.py", "shop/report.py"]
    assert data["tests"][0]["nodeid"] == "tests/test_prices.py::test_net"
    assert data["affected"][0]["via"] == ["shop.prices.net"]


def test_the_dependency_map_names_both_directions(project):
    _, resolver = project

    modules = {item.module: item for item in dependency_map(resolver)}

    assert modules["shop.cart"].imports == ("shop.prices",)
    assert "shop.report" in modules["shop.cart"].imported_by
    assert "shop.cart" in modules["shop.prices"].imported_by


def test_the_dependency_map_leaves_out_outside_modules(project):
    _, resolver = project

    modules = {item.module: item for item in dependency_map(resolver)}

    assert "pytest" not in modules["conftest"].imports
    assert modules["conftest"].imports == ("shop.cart",)


def test_importing_a_module_from_its_package_counts_as_the_module(write_project):
    index = build_index(
        write_project(
            {
                "pkg/__init__.py": "",
                "pkg/helpers.py": "VALUE = 1\n",
                "main.py": "from pkg import helpers\nimport pkg.helpers as other\n",
            }
        )
    )

    modules = {item.module: item for item in dependency_map(Resolver(index))}

    assert modules["main"].imports == ("pkg.helpers",)

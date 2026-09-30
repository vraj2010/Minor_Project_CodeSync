import ast
from textwrap import dedent

from codesync.collector import collect_symbols
from codesync.symbols import SymbolKind


def collect(source: str) -> list:
    tree = ast.parse(dedent(source).lstrip())
    return collect_symbols(tree, "pkg.mod", "pkg/mod.py")


def collect_by_name(source: str) -> dict:
    return {symbol.qualname: symbol for symbol in collect(source)}


def test_qualified_names_are_built_from_the_module_downwards():
    symbols = collect_by_name(
        """
        class Reader:
            def read(self):
                return None
        """
    )

    assert set(symbols) == {"pkg.mod.Reader", "pkg.mod.Reader.read"}
    assert symbols["pkg.mod.Reader"].kind is SymbolKind.CLASS
    assert symbols["pkg.mod.Reader.read"].kind is SymbolKind.METHOD
    assert symbols["pkg.mod.Reader.read"].parent == "pkg.mod.Reader"


def test_a_module_level_function_is_not_a_method():
    symbols = collect_by_name("def parse(line):\n    return line\n")

    assert symbols["pkg.mod.parse"].kind is SymbolKind.FUNCTION
    assert symbols["pkg.mod.parse"].parent is None
    assert symbols["pkg.mod.parse"].is_nested is False


def test_nested_functions_are_recorded_and_marked():
    symbols = collect_by_name(
        """
        def parse(line):
            def clean(value):
                return value.strip()

            return clean(line)
        """
    )

    nested = symbols["pkg.mod.parse.clean"]
    assert nested.kind is SymbolKind.FUNCTION
    assert nested.parent == "pkg.mod.parse"
    assert nested.is_nested is True


def test_positions_cover_the_whole_definition():
    symbols = collect_by_name(
        """
        def parse(line):
            return line


        VALUE = 1
        """
    )

    position = symbols["pkg.mod.parse"].position
    assert (position.lineno, position.end_lineno) == (1, 2)


def test_decorators_are_kept_as_written():
    symbols = collect_by_name(
        """
        @app.route("/rows", methods=["GET"])
        @cached
        def rows():
            return []
        """
    )

    symbol = symbols["pkg.mod.rows"]
    assert symbol.decorators == ("app.route('/rows', methods=['GET'])", "cached")
    assert symbol.decorator_lineno == 1
    assert symbol.position.lineno == 3
    assert symbol.start_lineno == 1


def test_async_definitions_are_flagged():
    symbols = collect_by_name(
        """
        async def fetch(url):
            return url


        def load(url):
            return url
        """
    )

    assert symbols["pkg.mod.fetch"].is_async is True
    assert symbols["pkg.mod.load"].is_async is False


def test_module_and_class_level_assignments_become_variables():
    symbols = collect_by_name(
        """
        SEPARATOR = ","
        FIRST, SECOND = 1, 2


        class Record:
            fields: list = []

            def width(self):
                total = 0
                return total
        """
    )

    assert symbols["pkg.mod.SEPARATOR"].kind is SymbolKind.VARIABLE
    assert symbols["pkg.mod.FIRST"].kind is SymbolKind.VARIABLE
    assert symbols["pkg.mod.SECOND"].kind is SymbolKind.VARIABLE
    assert symbols["pkg.mod.Record.fields"].parent == "pkg.mod.Record"
    assert "pkg.mod.Record.width.total" not in symbols


def test_attribute_assignments_are_not_variables():
    symbols = collect_by_name(
        """
        class Record:
            pass


        Record.width = 0
        """
    )

    assert set(symbols) == {"pkg.mod.Record"}


def test_definitions_inside_a_conditional_are_all_recorded():
    symbols = collect(
        """
        try:
            def parse(line):
                return line
        except ImportError:
            def parse(line):
                return None
        """
    )

    assert [symbol.qualname for symbol in symbols] == ["pkg.mod.parse", "pkg.mod.parse"]


def test_a_class_defined_inside_a_function_keeps_its_members():
    symbols = collect_by_name(
        """
        def build():
            class Inner:
                limit = 1

                def run(self):
                    return self.limit

            return Inner
        """
    )

    assert symbols["pkg.mod.build.Inner"].is_nested is True
    assert symbols["pkg.mod.build.Inner.run"].kind is SymbolKind.METHOD
    assert symbols["pkg.mod.build.Inner.limit"].kind is SymbolKind.VARIABLE


def test_every_symbol_carries_its_module_and_path():
    for symbol in collect("def parse(line):\n    return line\n"):
        assert symbol.module == "pkg.mod"
        assert symbol.relpath == "pkg/mod.py"

import json

from codesync.index import build_index
from codesync.symbols import SymbolKind


def test_summary_counts_the_whole_sample_project(sample_project):
    index = build_index(sample_project)

    assert index.summary() == {
        "files": 5,
        "modules": 5,
        "class": 3,
        "function": 4,
        "method": 5,
        "variable": 6,
        "failures": 0,
    }


def test_modules_record_their_path_and_size(sample_project):
    table = build_index(sample_project).table

    package = table.module("app")
    assert package.relpath == "app/__init__.py"
    assert package.is_package is True

    parser = table.module("app.services.parser")
    assert parser.is_package is False
    assert parser.line_count == 34


def test_symbols_are_reachable_by_qualified_name(sample_project):
    table = build_index(sample_project).table

    parse_row = table.lookup("app.services.parser.parse_row")
    assert parse_row.kind is SymbolKind.FUNCTION
    assert parse_row.relpath == "app/services/parser.py"
    assert (parse_row.position.lineno, parse_row.position.end_lineno) == (8, 14)

    supported = table.lookup("app.services.parser.RowReader.supported")
    assert supported.kind is SymbolKind.METHOD
    assert supported.decorators == ("staticmethod",)


def test_a_decorated_class_starts_at_its_first_decorator(sample_project):
    record = build_index(sample_project).table.lookup("app.models.Record")

    assert record.decorators == ("dataclass",)
    assert record.position.lineno == 7
    assert record.start_lineno == 6


def test_children_of_a_class_are_its_members(sample_project):
    table = build_index(sample_project).table

    children = table.children_of("app.models.Record")

    assert [child.name for child in children] == ["fields", "source", "column", "width"]


def test_symbols_in_a_module_are_ordered_by_line(sample_project):
    table = build_index(sample_project).table

    names = [symbol.name for symbol in table.symbols_in("app.services.parser")]

    assert names == [
        "STRICT_DEFAULT",
        "parse_row",
        "clean",
        "parse_stream",
        "RowReader",
        "__init__",
        "read",
        "supported",
    ]


def test_find_locates_a_short_name_across_modules(sample_project):
    table = build_index(sample_project).table

    assert [symbol.qualname for symbol in table.find("parse_row")] == [
        "app.services.parser.parse_row"
    ]
    assert [symbol.qualname for symbol in table.find("__init__")] == [
        "app.services.parser.RowReader.__init__"
    ]


def test_enclosing_returns_the_innermost_definition(sample_project):
    table = build_index(sample_project).table
    parser = "app/services/parser.py"

    assert table.enclosing(parser, 10).qualname == "app.services.parser.parse_row.clean"
    assert table.enclosing(parser, 13).qualname == "app.services.parser.parse_row"
    assert table.enclosing(parser, 5).qualname == "app.services.parser.STRICT_DEFAULT"


def test_enclosing_covers_decorator_lines(sample_project):
    table = build_index(sample_project).table

    assert table.enclosing("app/models.py", 6).qualname == "app.models.Record"
    assert table.enclosing("app/models.py", 14).qualname == "app.models.Record.width"


def test_enclosing_is_empty_outside_any_definition(sample_project):
    table = build_index(sample_project).table

    assert table.enclosing("app/services/parser.py", 1) is None
    assert table.enclosing("app/nothing.py", 1) is None


def test_ancestors_run_from_the_outermost_scope_inwards(sample_project):
    table = build_index(sample_project).table
    symbol = table.lookup("app.services.parser.RowReader.read")

    assert [item.qualname for item in table.ancestors(symbol)] == [
        "app.services.parser.RowReader"
    ]


def test_ancestors_stay_inside_the_definition_the_symbol_belongs_to(write_project):
    root = write_project(
        {
            "widgets.py": """
            if LEGACY:
                class Box:
                    def draw(self):
                        return 1
            else:
                class Box:
                    def draw(self):
                        return 2
            """
        }
    )

    table = build_index(root).table
    first, second = table.lookup_all("widgets.Box.draw")

    assert table.ancestors(first)[0].position.lineno == 2
    assert table.ancestors(second)[0].position.lineno == 6


def test_a_broken_file_is_reported_without_losing_the_rest(write_project):
    root = write_project(
        {
            "app/__init__.py": "",
            "app/good.py": "def parse(line):\n    return line\n",
            "app/broken.py": "def parse(line:\n    return line\n",
        }
    )

    index = build_index(root)

    assert [failure.relpath for failure in index.failures] == ["app/broken.py"]
    assert index.file_count == 3
    assert index.table.lookup("app.good.parse") is not None
    assert index.table.module("app.broken") is None


def test_two_files_claiming_one_module_name_are_reported(write_project):
    root = write_project(
        {
            "jobs/run.py": "def start():\n    return 1\n",
            "scripts/run.py": "def start():\n    return 2\n",
        }
    )

    index = build_index(root)

    assert [(item.name, item.kept, item.shadowed) for item in index.duplicates] == [
        ("run", "jobs/run.py", "scripts/run.py")
    ]
    assert len(index.table.lookup_all("run.start")) == 2


def test_a_redefined_name_resolves_to_the_last_definition(write_project):
    root = write_project(
        {
            "flags.py": """
            if FAST:
                def parse(line):
                    return line
            else:
                def parse(line):
                    return None
            """
        }
    )

    table = build_index(root).table

    assert len(table.lookup_all("flags.parse")) == 2
    assert table.lookup("flags.parse").position.lineno == 5


def test_the_index_serialises_to_json(sample_project):
    payload = json.loads(json.dumps(build_index(sample_project).to_dict()))

    assert payload["summary"]["modules"] == 5
    assert {module["name"] for module in payload["modules"]} == {
        "app",
        "app.models",
        "app.services",
        "app.services.parser",
        "report",
    }

    parse_row = next(
        item
        for item in payload["symbols"]
        if item["qualname"] == "app.services.parser.parse_row"
    )
    assert parse_row["kind"] == "function"
    assert parse_row["path"] == "app/services/parser.py"

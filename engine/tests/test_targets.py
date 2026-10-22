import json

import pytest

from codesync.batch import Task, load_tasks
from codesync.index import build_index
from codesync.targets import TargetError, find_refinable, find_symbol


@pytest.fixture(scope="module")
def sample_index(sample_project):
    return build_index(sample_project)


def test_a_qualified_name_selects_its_symbol(sample_index):
    assert (
        find_refinable(sample_index, "app.services.parser.parse_row").name
        == "parse_row"
    )


def test_a_line_inside_a_nested_function_selects_the_outer_one(sample_index):
    symbol = find_refinable(sample_index, "app/services/parser.py:10")

    assert symbol.qualname == "app.services.parser.parse_row"


def test_a_line_inside_a_method_selects_the_method(sample_index):
    symbol = find_refinable(sample_index, "app\\models.py:12")

    assert symbol.qualname == "app.models.Record.column"


def test_find_symbol_returns_whatever_is_at_the_line(sample_index):
    assert find_symbol(sample_index, "app/services/parser.py:10").name == "clean"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("app.models.Record", "is a class"),
        ("app.services.parser.STRICT_DEFAULT", "is a variable"),
        ("app.services.parser.parse_row.clean", "is nested inside"),
        ("app/models.py:8", "not inside a function or method"),
        ("app/services/parser.py:1", "not inside any definition"),
        ("app/missing.py:3", "not part of the index"),
    ],
)
def test_things_that_cannot_be_refined_are_explained(sample_index, spec, message):
    with pytest.raises(TargetError, match=message):
        find_refinable(sample_index, spec)


def test_an_unknown_name_suggests_close_matches(sample_index):
    with pytest.raises(
        TargetError, match="did you mean `app.services.parser.parse_row`"
    ):
        find_refinable(sample_index, "app.parser.parse_row")


def test_tasks_resolve_their_roots_against_the_task_file(tmp_path):
    path = tmp_path / "bench" / "tasks.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"root": "../projects/shop", "target": "shop.cart.total"},
                    {
                        "root": "shop",
                        "target": "shop.cart.add",
                        "goal": "Validate input.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    tasks = load_tasks(path)

    assert tasks == [
        Task((tmp_path / "projects" / "shop").resolve(), "shop.cart.total"),
        Task(
            (tmp_path / "bench" / "shop").resolve(), "shop.cart.add", "Validate input."
        ),
    ]


def test_a_bare_list_of_tasks_is_accepted(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([{"root": ".", "target": "a.b"}]), encoding="utf-8")

    assert load_tasks(path)[0].target == "a.b"


@pytest.mark.parametrize(
    "content",
    ['{"tasks": [{"target": "a.b"}]}', '{"tasks": "a.b"}', "[1]", "not json"],
)
def test_malformed_task_files_are_rejected(tmp_path, content):
    path = tmp_path / "tasks.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        load_tasks(path)

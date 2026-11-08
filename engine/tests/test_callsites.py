import ast

import pytest

from codesync.callsites import check_call_sites, check_overrides
from codesync.contract import contract_of_source, snapshot
from codesync.index import build_index
from codesync.namecheck import check_names
from codesync.references import find_references
from codesync.resolve import Resolver


def setup(write_project, files, qualname):
    index = build_index(write_project(files))
    resolver = Resolver(index)
    target = index.table.lookup(qualname)
    return resolver, target


def run(write_project, files, qualname, rewrite):
    resolver, target = setup(write_project, files, qualname)
    before = snapshot(resolver.node_for(target))
    after = contract_of_source(rewrite)
    references = find_references(resolver, target)
    return check_call_sites(resolver, target, before, after, references)


LIBRARY = {
    "lib.py": """
        def parse(line, sep=","):
            return line.split(sep)

        def pair(a):
            return a, 1

        def info():
            return {"name": 1, "size": 2}

        def label(a):
            return "x"

        class Stock:
            def __init__(self, items):
                self.items = items

            def add(self, item):
                self.items.append(item)
                return self

            @staticmethod
            def make(size):
                return size

            @classmethod
            def empty(cls, size):
                return size
    """,
    "use.py": """
        import asyncio
        from lib import Stock, info, label, pair, parse

        def a(line):
            return parse(line, sep=";")

        def b(line):
            return parse(line)

        def c():
            x, y = pair(1)
            return x

        def d():
            result = pair(1)
            first, second = result
            return first

        def e():
            return info()["size"]

        def f():
            data = info()
            return data["name"]

        def g():
            return label(1).upper()

        def h():
            stock = Stock([])
            stock.add(1)
            Stock.add(stock, 2)
            Stock.make(1)
            Stock.empty(1)
            return stock
    """,
}


def test_a_call_that_stops_binding_is_named(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.parse",
        'def parse(line, *, delimiter=","):\n    return line.split(delimiter)\n',
    )

    assert [(item.code, item.location) for item in found] == [
        ("call-no-longer-binds", "use.py:5")
    ]
    assert found[0].message == "the call has no parameter `sep`"
    assert found[0].code_text == 'parse(line, sep=";")'


def test_a_compatible_rewrite_reports_nothing(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.parse",
        'def parse(line, sep=",", strip=False):\n    return line.split(sep)\n',
    )

    assert found == []


def test_a_call_that_was_already_wrong_is_not_blamed_on_the_rewrite(write_project):
    files = {
        "lib.py": "def f(a):\n    return a\n",
        "use.py": "from lib import f\n\ndef g():\n    return f(1, 2)\n",
    }

    assert run(write_project, files, "lib.f", "def f(a, b=1):\n    return a\n") == []
    assert run(write_project, files, "lib.f", "def f(x):\n    return x\n") == []


def test_a_renamed_function_names_every_use(write_project):
    found = run(write_project, LIBRARY, "lib.parse", "def split(line):\n    pass\n")

    assert [(item.code, item.location) for item in found] == [
        ("name-gone", "use.py:2"),
        ("name-gone", "use.py:5"),
        ("name-gone", "use.py:8"),
    ]


def test_a_bound_method_call_does_not_count_self(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.Stock.add",
        "def add(self, item, quantity):\n    return self\n",
    )

    assert [(item.code, item.location) for item in found] == [
        ("call-no-longer-binds", "use.py:31"),
        ("call-no-longer-binds", "use.py:32"),
    ]


def test_a_constructor_is_checked_through_the_class(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.Stock.__init__",
        "def __init__(self, items, owner):\n    self.items = items\n",
    )

    assert [item.location for item in found] == ["use.py:30"]
    assert found[0].message == "the call is missing `owner`"


def test_a_static_method_is_not_given_self(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.Stock.make",
        "@staticmethod\ndef make():\n    return 1\n",
    )

    assert [item.location for item in found] == ["use.py:33"]


def test_a_class_method_is_given_its_class(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.Stock.empty",
        "@classmethod\ndef empty(cls, size, name):\n    return size\n",
    )

    assert [item.location for item in found] == ["use.py:34"]


def test_a_longer_tuple_breaks_unpacking(write_project):
    found = run(
        write_project, LIBRARY, "lib.pair", "def pair(a):\n    return a, 1, 2\n"
    )

    assert [(item.code, item.location) for item in found] == [
        ("unpack-mismatch", "use.py:11"),
        ("unpack-mismatch", "use.py:15"),
    ]
    assert "unpacks 2 values" in found[0].message


def test_a_lost_key_names_the_code_that_reads_it(write_project):
    found = run(
        write_project, LIBRARY, "lib.info", 'def info():\n    return {"name": 1}\n'
    )

    assert [(item.code, item.location) for item in found] == [
        ("missing-key", "use.py:20")
    ]
    assert "'size'" in found[0].message


def test_a_result_read_through_a_variable_is_followed(write_project):
    found = run(write_project, LIBRARY, "lib.info", 'def info():\n    return {"z": 1}\n')

    assert [item.location for item in found] == ["use.py:20", "use.py:23"]


def test_an_attribute_the_new_result_lacks_is_named(write_project):
    found = run(write_project, LIBRARY, "lib.label", "def label(a):\n    return [a]\n")

    assert [(item.code, item.location) for item in found] == [
        ("missing-attribute", "use.py:27")
    ]


def test_a_result_that_can_be_none_names_who_uses_it_directly(write_project):
    found = run(
        write_project,
        LIBRARY,
        "lib.label",
        'def label(a):\n    if a:\n        return "x"\n',
    )

    assert [(item.code, item.location) for item in found] == [
        ("none-not-handled", "use.py:27")
    ]


ASYNC = {
    "lib.py": """
        def fetch(a):
            return a

        async def load(a):
            return a
    """,
    "use.py": """
        import asyncio
        from lib import fetch, load

        def plain():
            return fetch(1)

        async def awaited():
            return await load(1)

        async def together():
            return await asyncio.gather(load(1))
    """,
}


def test_turning_async_names_the_calls_left_unawaited(write_project):
    found = run(write_project, ASYNC, "lib.fetch", "async def fetch(a):\n    return a\n")

    assert [(item.code, item.location) for item in found] == [
        ("missing-await", "use.py:5")
    ]


def test_turning_sync_names_the_calls_that_await(write_project):
    found = run(write_project, ASYNC, "lib.load", "def load(a):\n    return a\n")

    assert [(item.code, item.location) for item in found] == [
        ("bad-await", "use.py:8")
    ]


def test_a_name_only_match_says_so(write_project):
    files = {
        "lib.py": """
            class Stock:
                def add(self, item):
                    return self
        """,
        "use.py": "def f(thing):\n    return thing.add(1)\n",
    }

    found = run(
        write_project, files, "lib.Stock.add", "def add(self, item, n):\n    return 1\n"
    )

    assert "matched by name only" in found[0].message


def test_a_name_only_match_that_never_fit_is_left_alone(write_project):
    files = {
        "lib.py": """
            class Stock:
                def add(self, item):
                    return self
        """,
        "use.py": "def f(thing):\n    return thing.add(1, 2, 3)\n",
    }

    found = run(
        write_project, files, "lib.Stock.add", "def add(self, item, n=1):\n    return 1\n"
    )

    assert found == []


HIERARCHY = {
    "shapes.py": """
        class Shape:
            def area(self, scale):
                return 1

        class Square(Shape):
            def area(self, scale):
                return 2

        class Tiny(Square):
            pass
    """,
}


def overrides(write_project, files, qualname, rewrite):
    resolver, target = setup(write_project, files, qualname)
    return check_overrides(
        resolver,
        target,
        snapshot(resolver.node_for(target)),
        contract_of_source(rewrite),
    )


def test_a_subclass_left_behind_is_named(write_project):
    found = overrides(
        write_project,
        HIERARCHY,
        "shapes.Shape.area",
        "def area(self, factor):\n    return 1\n",
    )

    assert [(item.code, item.location) for item in found] == [
        ("override-mismatch", "shapes.py:6")
    ]
    assert "shapes.Square.area" in found[0].message


def test_a_method_that_stops_matching_its_base_is_named(write_project):
    found = overrides(
        write_project,
        HIERARCHY,
        "shapes.Square.area",
        "def area(self):\n    return 2\n",
    )

    assert "shapes.Shape.area" in found[0].message


def test_a_function_has_no_overrides(write_project):
    found = overrides(
        write_project,
        {"a.py": "def f(a):\n    return a\n"},
        "a.f",
        "def f():\n    return 1\n",
    )

    assert found == []


def names(write_project, files, qualname, rewrite, imports=()):
    resolver, target = setup(write_project, files, qualname)
    source = resolver.source(target.relpath)
    lines = source.split("\n")
    first, last = target.start_lineno, target.position.end_lineno
    indent = lines[first - 1][: len(lines[first - 1]) - len(lines[first - 1].lstrip())]
    replacement = [indent + line for line in rewrite.rstrip("\n").split("\n")]
    patched = "\n".join(
        [*imports, *lines[: first - 1], *replacement, *lines[last:]]
    )

    tree = ast.parse(rewrite)
    return check_names(
        resolver,
        target,
        resolver.node_for(target),
        tree.body[0],
        rewrite,
        patched,
        tuple(imports),
    )


MODULE = {
    "helpers.py": """
        def tidy(text, limit):
            return text[:limit]

        class Box:
            def __init__(self, size):
                self.size = size
    """,
    "main.py": """
        import helpers
        from helpers import Box, tidy

        LIMIT = 10

        def clean(text):
            return tidy(text, LIMIT)

        class Thing:
            size = 1

            def __init__(self):
                self.name = "x"

            def read(self):
                return self.name

            def helper(self):
                return 1
    """,
}


def test_a_rewrite_using_only_known_names_passes(write_project):
    assert (
        names(
            write_project,
            MODULE,
            "main.clean",
            "def clean(text):\n    return tidy(text.strip(), LIMIT)\n",
        )
        == []
    )


def test_an_undefined_name_is_found(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    return Decimal(tidy(text, LIMIT))\n",
    )

    assert [item.code for item in found] == ["undefined-name"]
    assert "`Decimal`" in found[0].message


def test_a_name_imported_with_the_rewrite_is_defined(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    return Decimal(tidy(text, LIMIT))\n",
        imports=("from decimal import Decimal",),
    )

    assert found == []


def test_a_name_in_a_nested_function_is_found(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    def inner():\n        return missing\n    return inner()\n",
    )

    assert [item.code for item in found] == ["undefined-name"]


def test_a_missing_self_attribute_is_found(write_project):
    found = names(
        write_project,
        MODULE,
        "main.Thing.read",
        "def read(self):\n    return self.title\n",
    )

    assert [item.code for item in found] == ["missing-attribute"]
    assert "`self.title`" in found[0].message


def test_a_class_or_instance_attribute_is_known(write_project):
    assert (
        names(
            write_project,
            MODULE,
            "main.Thing.read",
            "def read(self):\n    return self.name, self.size, self.helper()\n",
        )
        == []
    )


def test_an_attribute_the_rewrite_sets_itself_is_known(write_project):
    found = names(
        write_project,
        MODULE,
        "main.Thing.read",
        "def read(self):\n    self.cached = 1\n    return self.cached\n",
    )

    assert found == []


def test_a_class_with_an_outside_base_is_not_judged(write_project):
    files = {
        "main.py": """
            import threading

            class Worker(threading.Thread):
                def go(self):
                    return 1
        """
    }

    found = names(
        write_project, files, "main.Worker.go", "def go(self):\n    return self.name\n"
    )

    assert found == []


def test_a_name_missing_from_a_project_module_is_found(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    return helpers.scrub(text)\n",
    )

    assert [item.code for item in found] == ["missing-project-name"]
    assert "`helpers` has nothing called `scrub`" in found[0].message


def test_a_new_call_to_a_project_function_must_fit_it(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    return tidy(text)\n",
    )

    assert [item.code for item in found] == ["bad-call"]
    assert found[0].message == "the call to `helpers.tidy` is missing `limit`"


def test_a_new_call_to_a_project_class_must_fit_its_constructor(write_project):
    found = names(
        write_project, MODULE, "main.clean", "def clean(text):\n    return Box()\n"
    )

    assert found[0].message == "the call to `helpers.Box` is missing `size`"


def test_an_import_of_something_a_project_module_lacks_is_found(write_project):
    found = names(
        write_project,
        MODULE,
        "main.clean",
        "def clean(text):\n    return scrub(text)\n",
        imports=("from helpers import scrub",),
    )

    assert [item.code for item in found] == ["bad-import"]


def test_a_problem_the_original_already_had_is_not_blamed_on_the_rewrite(write_project):
    files = {"main.py": "def f():\n    return missing + 1\n"}

    assert names(write_project, files, "main.f", "def f():\n    return missing\n") == []


def test_a_star_import_means_names_cannot_be_judged(write_project):
    files = {"main.py": "from os.path import *\n\ndef f(a):\n    return a\n"}

    assert names(write_project, files, "main.f", "def f(a):\n    return join(a)\n") == []


@pytest.mark.parametrize("name", ["len", "print", "__name__", "ValueError"])
def test_builtins_are_known(write_project, name):
    files = {"main.py": "def f(a):\n    return a\n"}

    assert (
        names(write_project, files, "main.f", f"def f(a):\n    return {name}\n") == []
    )

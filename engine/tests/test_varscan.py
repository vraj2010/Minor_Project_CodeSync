import pytest

from codesync.index import build_index
from codesync.variables import AccessMode, VariableKind
from codesync.varscan import build_variable_table

READ, WRITE = AccessMode.READ, AccessMode.WRITE


@pytest.fixture
def scan(write_project):
    def run(files):
        if isinstance(files, str):
            files = {"app.py": files}
        return build_variable_table(build_index(write_project(files)))

    return run


def modes(table, qualname):
    return [access.mode for access in table.accesses(qualname)]


def counts(table, qualname):
    usage = table.usage(qualname)
    return usage.reads, usage.writes


def test_every_kind_of_variable_is_found(scan):
    table = scan(
        """
        LIMIT = 3


        class Box:
            size = 1

            def __init__(self, width, height=2):
                self.width = width
                area = width * height
                self.area = area
        """
    )

    kinds = {variable.qualname: variable.kind for variable in table.variables}

    assert kinds == {
        "app.LIMIT": VariableKind.MODULE,
        "app.Box.size": VariableKind.CLASS,
        "app.Box.__init__.self": VariableKind.PARAMETER,
        "app.Box.__init__.width": VariableKind.PARAMETER,
        "app.Box.__init__.height": VariableKind.PARAMETER,
        "app.Box.__init__.area": VariableKind.LOCAL,
        "app.Box.width": VariableKind.INSTANCE,
        "app.Box.area": VariableKind.INSTANCE,
    }


def test_a_variable_records_where_it_first_appears(scan):
    table = scan("import os\n\n\ndef f(a):\n    b = a\n    return b\n")

    parameter, local = table.lookup("app.f.a"), table.lookup("app.f.b")

    assert (parameter.relpath, parameter.lineno, parameter.col_offset) == (
        "app.py",
        4,
        6,
    )
    assert (local.lineno, local.container, local.module) == (5, "app.f", "app")


def test_a_local_shadows_the_module_variable_of_the_same_name(scan):
    table = scan(
        """
        total = 0


        def f():
            total = 1
            return total


        def g():
            return total
        """
    )

    assert counts(table, "app.f.total") == (1, 1)
    assert counts(table, "app.total") == (1, 1)


def test_global_writes_reach_the_module_variable(scan):
    table = scan(
        """
        count = 0


        def bump():
            global count
            count += 1
        """
    )

    assert "app.bump.count" not in table
    assert counts(table, "app.count") == (1, 2)


def test_a_variable_created_by_global_is_visible_to_earlier_functions(scan):
    table = scan(
        """
        def use():
            return CONFIG


        def setup():
            global CONFIG
            CONFIG = {}
        """
    )

    assert table.lookup("app.CONFIG").kind is VariableKind.MODULE
    assert counts(table, "app.CONFIG") == (1, 1)


def test_nonlocal_writes_reach_the_enclosing_function(scan):
    table = scan(
        """
        def outer():
            hits = 0

            def record():
                nonlocal hits
                hits += 1

            record()
            return hits
        """
    )

    assert "app.outer.record.hits" not in table
    assert counts(table, "app.outer.hits") == (2, 2)


def test_a_class_body_is_not_visible_from_its_methods(scan):
    table = scan(
        """
        name = "module"


        class C:
            name = "class"

            def method(self):
                return name
        """
    )

    assert counts(table, "app.name") == (1, 1)
    assert counts(table, "app.C.name") == (0, 1)


def test_a_class_body_sees_its_own_names(scan):
    table = scan("class C:\n    a = 1\n    b = a + 1\n")

    assert counts(table, "app.C.a") == (1, 1)


def test_a_comprehension_has_its_own_scope(scan):
    table = scan("def f(items):\n    return [item * 2 for item in items]\n")

    names = [variable.qualname for variable in table.in_container("app.f")]
    comprehension = next(name for name in names if name.endswith(".item"))

    assert "app.f.item" not in table
    assert comprehension.startswith("app.f.<listcomp@2:")
    assert counts(table, comprehension) == (1, 1)
    assert counts(table, "app.f.items") == (1, 0)
    assert table.lookup(comprehension).kind is VariableKind.LOCAL


def test_the_first_iterable_of_a_comprehension_is_read_in_the_enclosing_scope(scan):
    table = scan("data = [1, 2]\nsquares = [n * n for n in data]\n")

    assert counts(table, "app.data") == (1, 1)


def test_a_lambda_has_parameters_of_its_own(scan):
    table = scan("scale = 3\nkey = lambda x: x * scale\n")

    parameter = next(v for v in table.variables if v.name == "x")

    assert parameter.kind is VariableKind.PARAMETER
    assert parameter.scope.startswith("app.<lambda@2:")
    assert parameter.container == "app"
    assert counts(table, "app.scale") == (1, 1)


def test_a_walrus_in_a_comprehension_binds_in_the_function(scan):
    table = scan(
        """
        def f(values):
            if any((hit := v) > 1 for v in values):
                return hit
        """
    )

    assert table.lookup("app.f.hit").kind is VariableKind.LOCAL
    assert counts(table, "app.f.hit") == (1, 1)


def test_statements_that_bind_names_are_all_recognised(scan):
    table = scan(
        """
        def f(path, rows):
            with open(path) as handle:
                data = handle.read()
            for row in rows:
                print(row)
            try:
                pass
            except ValueError as error:
                print(error)
            del data
        """
    )

    assert counts(table, "app.f.handle") == (1, 1)
    assert counts(table, "app.f.row") == (1, 1)
    assert counts(table, "app.f.error") == (1, 1)
    assert counts(table, "app.f.data") == (0, 2)
    assert table.lookup("app.f.error").static_type == "ValueError"


def test_the_origin_of_a_write_is_kept_as_text(scan):
    table = scan(
        """
        def f(path, rows):
            with open(path) as handle:
                pass
            for row in rows:
                pass
            try:
                pass
            except (KeyError, ValueError) as error:
                pass
            total = 1 + 2
        """
    )

    def origin(name):
        return table.accesses(f"app.f.{name}")[0].value

    assert origin("handle") == "context of open(path)"
    assert origin("row") == "loop over rows"
    assert origin("error") == "caught (KeyError, ValueError)"
    assert origin("total") == "1 + 2"


def test_match_captures_are_variables(scan):
    table = scan(
        """
        def f(command):
            match command:
                case ["go", direction]:
                    return direction
                case {"speed": speed, **rest}:
                    return rest
                case [first, *others]:
                    return others
        """
    )

    for name in ("direction", "speed", "rest", "first", "others"):
        assert table.lookup(f"app.f.{name}").kind is VariableKind.LOCAL
    assert counts(table, "app.f.direction") == (1, 1)
    assert counts(table, "app.f.speed") == (0, 1)


def test_an_augmented_assignment_reads_and_writes(scan):
    table = scan("n = 0\nn += 5\n")

    assert counts(table, "app.n") == (1, 2)
    assert table.accesses("app.n")[-1].value == "+= 5"


def test_unpacking_pairs_values_with_targets_when_it_can(scan):
    table = scan("a, b = 1, 'x'\nc, d = divmod(7, 2)\n")

    assert table.lookup("app.a").static_type == "int"
    assert table.lookup("app.b").static_type == "str"
    assert table.lookup("app.c").static_type is None
    assert table.accesses("app.c")[0].value == "unpacked from divmod(7, 2)"


def test_a_bare_annotation_declares_an_instance_variable(scan):
    table = scan("class Point:\n    x: int\n    y: int = 0\n")

    declared, defaulted = table.lookup("app.Point.x"), table.lookup("app.Point.y")

    assert declared.kind is VariableKind.INSTANCE
    assert (declared.static_type, declared.type_source) == ("int", "annotation")
    assert counts(table, "app.Point.x") == (0, 0)
    assert defaulted.kind is VariableKind.CLASS
    assert counts(table, "app.Point.y") == (0, 1)


def test_defaults_and_annotations_are_read_in_the_enclosing_scope(scan):
    table = scan(
        """
        SIZE = 3
        Alias = dict


        def f(n=SIZE, items: Alias = None):
            return n
        """
    )

    assert counts(table, "app.SIZE") == (1, 1)
    assert counts(table, "app.Alias") == (1, 1)
    assert table.lookup("app.f.n").default == "SIZE"


def test_parameter_kinds_are_recorded(scan):
    table = scan("def f(a, /, b, *args, c=1, **options):\n    pass\n")

    kinds = {v.name: v.parameter_kind for v in table.in_container("app.f")}

    assert kinds == {
        "a": "positional-only",
        "b": "positional",
        "args": "var-positional",
        "c": "keyword-only",
        "options": "var-keyword",
    }


def test_an_import_from_another_module_reaches_its_variable(scan):
    table = scan(
        {
            "consts.py": "LIMIT = 5\n",
            "app.py": "from consts import LIMIT as cap\n\n\ndef f():\n    return cap\n",
            "other.py": "import consts\n\n\ndef g():\n    return consts.LIMIT\n",
        }
    )

    usage = table.usage("consts.LIMIT")

    assert (usage.reads, usage.writes) == (2, 1)
    assert usage.external_files == ("app.py", "other.py")
    assert usage.cross_file


def test_a_variable_reexported_by_a_package_is_followed(scan):
    table = scan(
        {
            "pkg/__init__.py": "from .settings import DEBUG\n",
            "pkg/settings.py": "DEBUG = True\n",
            "main.py": "from pkg import DEBUG\n\n\ndef f():\n    return DEBUG\n",
        }
    )

    assert table.usage("pkg.settings.DEBUG").external_files == ("main.py",)


def test_a_relative_import_reaches_the_variable(scan):
    table = scan(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "VALUE = 1\n",
            "pkg/a.py": "from . import b\nfrom .b import VALUE\n\n\n"
            "def f():\n    return b.VALUE + VALUE\n",
        }
    )

    assert counts(table, "pkg.b.VALUE") == (2, 1)


def test_attributes_through_self_are_instance_variables(scan):
    table = scan(
        """
        class Counter:
            def __init__(self):
                self.value = 0

            def bump(self):
                self.value += 1
                return self.value
        """
    )

    variable = table.lookup("app.Counter.value")

    assert variable.kind is VariableKind.INSTANCE
    assert counts(table, "app.Counter.value") == (2, 2)
    assert counts(table, "app.Counter.bump.self") == (0, 0)


def test_an_inherited_attribute_resolves_to_the_base_class(scan):
    table = scan(
        """
        class Base:
            def __init__(self):
                self.value = 1


        class Child(Base):
            def bump(self):
                self.value += 1
        """
    )

    assert "app.Child.value" not in table
    assert counts(table, "app.Base.value") == (1, 2)


def test_an_overriding_attribute_points_at_the_one_it_overrides(scan):
    table = scan(
        """
        class Base:
            def __init__(self):
                self.value = 1


        class Child(Base):
            def __init__(self):
                self.value = 2
        """
    )

    assert table.lookup("app.Child.value").overrides == "app.Base.value"
    assert table.lookup("app.Base.value").overrides is None


def test_a_mixin_reads_the_attribute_its_subclass_defines(scan):
    table = scan(
        """
        class Mixin:
            def describe(self):
                return self.label


        class Item(Mixin):
            def __init__(self):
                self.label = "x"
        """
    )

    assert counts(table, "app.Item.label") == (1, 1)


def test_class_variables_are_reached_through_the_class_name(scan):
    table = scan(
        """
        class Config:
            debug = False


        def f():
            return Config.debug
        """
    )

    assert counts(table, "app.Config.debug") == (1, 1)


def test_a_classmethod_sets_class_variables(scan):
    table = scan(
        """
        class Registry:
            @classmethod
            def make(cls):
                cls.entries = []
                return cls
        """
    )

    assert table.lookup("app.Registry.entries").kind is VariableKind.CLASS
    assert table.lookup("app.Registry.make.cls").static_type == "type[app.Registry]"


def test_a_staticmethod_has_no_receiver(scan):
    table = scan(
        """
        class Util:
            @staticmethod
            def touch(target):
                target.value = 1
        """
    )

    assert "app.Util.value" not in table
    assert counts(table, "app.Util.touch.target") == (1, 0)
    assert table.lookup("app.Util.touch.target").static_type is None


def test_a_nested_function_can_set_attributes_on_the_method_receiver(scan):
    table = scan(
        """
        class Job:
            def run(self):
                def finish():
                    self.done = True

                finish()
        """
    )

    assert table.lookup("app.Job.done").kind is VariableKind.INSTANCE
    assert counts(table, "app.Job.done") == (0, 1)


def test_attributes_of_other_objects_are_not_variables(scan):
    table = scan("def f(obj):\n    obj.count = 1\n    return obj.count\n")

    assert [variable.name for variable in table.variables] == ["obj"]
    assert counts(table, "app.f.obj") == (2, 0)


def test_types_come_from_annotations_then_from_the_values_assigned(scan):
    table = scan(
        """
        class Box:
            pass


        def make(size: int = 3, delay=0.5, *args, **kwargs):
            box = Box()
            count = 0
            count = None
            label: str = 1
            return box
        """
    )

    def kind(name):
        variable = table.lookup(f"app.make.{name}")
        return variable.static_type, variable.type_source

    assert kind("size") == ("int", "annotation")
    assert kind("delay") == ("float", "inferred")
    assert kind("args") == ("tuple", "inferred")
    assert kind("kwargs") == ("dict", "inferred")
    assert kind("box") == ("app.Box", "inferred")
    assert kind("count") == ("int | None", "inferred")
    assert kind("label") == ("str", "annotation")


def test_a_call_to_a_project_function_takes_its_return_annotation(scan):
    table = scan("def width() -> int:\n    return 1\n\n\nsize = width()\n")

    assert table.lookup("app.size").static_type == "int"


def test_self_has_the_type_of_its_class(scan):
    table = scan("class A:\n    def f(self):\n        pass\n")

    assert table.lookup("app.A.f.self").static_type == "app.A"


def test_accesses_inside_a_comprehension_belong_to_the_enclosing_function(scan):
    table = scan("def f(items):\n    return [x for x in items]\n")

    assert {a.scope for a in table.accesses("app.f.items")} == {"app.f"}
    comprehension = next(v for v in table.variables if v.name == "x")
    assert {a.scope for a in table.accesses(comprehension.qualname)} == {"app.f"}


def test_a_nested_class_in_a_function_is_scanned(scan):
    table = scan("def f():\n    class Inner:\n        z = 1\n    return Inner\n")

    assert table.lookup("app.f.Inner.z").kind is VariableKind.CLASS


def test_functions_and_classes_are_not_variables(scan):
    table = scan("def helper():\n    pass\n\n\nclass C:\n    pass\n\n\nx = helper\n")

    assert [variable.name for variable in table.variables] == ["x"]
    assert counts(table, "app.x") == (0, 1)


def test_a_file_that_does_not_parse_is_skipped(scan):
    table = scan({"good.py": "x = 1\n", "bad.py": "def broken(:\n    pass\n"})

    assert [variable.qualname for variable in table.variables] == ["good.x"]


def test_two_files_with_one_module_name_keep_separate_variables(scan):
    table = scan({"a/run.py": "X = 1\n", "b/run.py": "X = 2\n"})

    assert sorted(variable.qualname for variable in table.variables) == [
        "run.X",
        "run@b/run.py.X",
    ]


def test_conditional_redefinitions_share_their_variables(scan):
    table = scan(
        """
        if FLAG:
            def f():
                value = 1
        else:
            def f():
                value = 2
        """
    )

    assert counts(table, "app.f.value") == (0, 2)


def test_the_table_can_be_queried_several_ways(scan):
    table = scan(
        {
            "a.py": "LIMIT = 1\n\n\ndef f(x):\n    y = x\n    return y\n",
            "b.py": "LIMIT = 2\n",
        }
    )

    assert len(table) == 4
    assert [v.qualname for v in table.find("LIMIT")] == ["a.LIMIT", "b.LIMIT"]
    assert [v.name for v in table.in_container("a.f")] == ["x", "y"]
    assert [v.name for v in table.in_module("b")] == ["LIMIT"]
    assert table.containers() == ["a", "a.f", "b"]
    assert table.counts() == {
        "module": 2,
        "class": 0,
        "instance": 0,
        "parameter": 1,
        "local": 1,
    }
    assert table.access_count() == 5


def test_variables_and_accesses_serialise(scan):
    table = scan("X = 1\n")

    variable = table.lookup("app.X").to_dict()
    access = table.accesses("app.X")[0].to_dict()

    assert variable["kind"] == "module"
    assert variable["path"] == "app.py"
    assert access["mode"] == "write"
    assert access["value"] == "1"
    assert table.usage("app.X").to_dict() == {
        "reads": 0,
        "writes": 1,
        "files": ["app.py"],
        "external_files": [],
    }


def test_the_shop_fixture_matches_what_the_code_does(shop_project):
    table = build_variable_table(build_index(shop_project))

    assert table.counts() == {
        "module": 4,
        "class": 3,
        "instance": 5,
        "parameter": 13,
        "local": 5,
    }
    assert table.lookup("shop.cart.Cart.items").overrides == "shop.cart.Base.items"
    assert (
        table.lookup("shop.cart.Cart.discount").overrides == "shop.cart.Base.discount"
    )
    assert counts(table, "shop.config.counter") == (3, 2)
    assert table.usage("shop.config.TAX").external_files == ("shop/cart.py",)
    assert counts(table, "shop.cart.Cart.add.outer") == (2, 2)

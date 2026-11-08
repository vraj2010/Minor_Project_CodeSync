import ast

import pytest

from codesync.contract import (
    ParameterKind,
    bind,
    compare,
    compare_parameters,
    contract_of_source,
)


def breaches(before, after):
    return compare(contract_of_source(before), contract_of_source(after))


def codes(before, after):
    return [item.code for item in breaches(before, after) if item.blocking]


def call_of(text):
    return ast.parse(text, mode="eval").body


def test_a_snapshot_records_how_each_parameter_is_passed():
    contract = contract_of_source(
        "def f(a, /, b, c=1, *rest, d, e=2, **more) -> int:\n    return 1\n"
    )

    assert [(item.name, item.kind) for item in contract.parameters] == [
        ("a", ParameterKind.POSITIONAL_ONLY),
        ("b", ParameterKind.POSITIONAL),
        ("c", ParameterKind.POSITIONAL),
        ("rest", ParameterKind.VAR_POSITIONAL),
        ("d", ParameterKind.KEYWORD_ONLY),
        ("e", ParameterKind.KEYWORD_ONLY),
        ("more", ParameterKind.VAR_KEYWORD),
    ]
    assert contract.return_annotation == "int"


def test_a_signature_can_be_printed_back():
    contract = contract_of_source("async def f(a, b=1, *, c, **more):\n    pass\n")

    assert contract.describe() == "async f(a, b=1, *, c, **more)"


def test_the_example_from_the_project_brief_is_caught():
    found = breaches(
        'def parse_row(line, sep=",", strict=False):\n    return Record(line)\n',
        'def parse_row(line, *, delimiter=","):\n    return Record(line)\n',
    )

    assert {item.code for item in found} == {"renamed-parameter", "removed-parameter"}
    assert "`sep` was renamed to `delimiter`" in [item.message for item in found]


def test_an_identical_signature_is_not_a_breach():
    source = "def f(a, b=1):\n    return a + b\n"

    assert codes(source, source) == []


def test_changing_only_the_body_is_not_a_breach():
    assert (
        codes(
            "def f(a):\n    total = a + 1\n    return total\n",
            "def f(a):\n    return a + 1\n",
        )
        == []
    )


def test_adding_an_optional_parameter_keeps_the_contract():
    assert codes("def f(a):\n    return a\n", "def f(a, b=1):\n    return a\n") == []


def test_adding_a_required_parameter_breaks_it():
    assert codes("def f(a):\n    return a\n", "def f(a, b):\n    return a\n") == [
        "new-required"
    ]


def test_a_renamed_function_is_caught():
    assert "renamed" in codes("def f(a):\n    pass\n", "def g(a):\n    pass\n")


def test_swapped_parameters_are_caught():
    assert codes(
        "def f(a, b):\n    return a\n", "def f(b, a):\n    return a\n"
    ) == ["moved", "moved"]


def test_a_parameter_made_keyword_only_is_caught():
    assert codes("def f(a, b):\n    pass\n", "def f(a, *, b):\n    pass\n") == [
        "now-keyword-only"
    ]


def test_a_parameter_made_positional_only_is_caught():
    assert codes("def f(a, b):\n    pass\n", "def f(a, b, /):\n    pass\n") == [
        "now-positional-only",
        "now-positional-only",
    ]


def test_a_lost_default_is_caught():
    assert codes("def f(a=1):\n    pass\n", "def f(a):\n    pass\n") == [
        "default-removed"
    ]


def test_a_changed_default_is_caught():
    found = breaches("def f(a=1):\n    pass\n", "def f(a=2):\n    pass\n")

    assert found[0].code == "default-changed"
    assert "from 1 to 2" in found[0].message


def test_dropping_var_arguments_is_caught():
    assert codes("def f(*a, **b):\n    pass\n", "def f():\n    pass\n") == [
        "var-removed",
        "var-removed",
    ]


def test_turning_async_is_caught():
    assert codes("def f():\n    pass\n", "async def f():\n    pass\n") == [
        "async-changed"
    ]


def test_turning_into_a_generator_is_caught():
    assert codes(
        "def f(a):\n    return [a]\n", "def f(a):\n    yield a\n"
    ) == ["generator-changed"]


def test_yield_in_a_nested_function_does_not_make_a_generator():
    contract = contract_of_source(
        "def f():\n    def inner():\n        yield 1\n    return inner\n"
    )

    assert not contract.is_generator


def test_a_lost_decorator_is_caught():
    assert codes(
        "@cached\ndef f():\n    return 1\n", "def f():\n    return 1\n"
    ) == ["decorator-removed"]


def test_a_new_decorator_is_shown_but_does_not_block():
    found = breaches("def f():\n    return 1\n", "@cached\ndef f():\n    return 1\n")

    assert [(item.code, item.blocking) for item in found] == [
        ("decorator-added", False)
    ]


def test_a_new_decorator_that_changes_binding_blocks():
    assert codes(
        "def f(self):\n    return 1\n", "@property\ndef f(self):\n    return 1\n"
    ) == ["decorator-added"]


def test_a_value_turned_into_nothing_is_caught():
    found = breaches(
        "def f(self, x):\n    self.a.append(x)\n    return self\n",
        "def f(self, x):\n    self.a.append(x)\n",
    )

    assert found[0].code == "may-return-none"
    assert found[0].message == "it used to return a value and now returns nothing"


def test_a_path_that_falls_off_the_end_is_caught():
    found = breaches(
        "def f(a):\n    if a:\n        return 1\n    return 0\n",
        "def f(a):\n    if a:\n        return 1\n",
    )

    assert found[0].code == "may-return-none"
    assert "can now return None" in found[0].message


@pytest.mark.parametrize(
    "body",
    [
        "    if a:\n        return 1\n    else:\n        return 2\n",
        "    try:\n        return 1\n    except KeyError:\n        return 2\n",
        "    try:\n        a()\n    finally:\n        return 1\n",
        "    with a:\n        return 1\n",
        "    while True:\n        return 1\n",
        "    match a:\n        case 1:\n            return 1\n"
        "        case _:\n            return 2\n",
        "    if a:\n        return 1\n    raise ValueError(a)\n",
    ],
)
def test_a_function_that_always_returns_is_recognised(body):
    assert not contract_of_source(f"def f(a):\n{body}").may_return_none


@pytest.mark.parametrize(
    "body",
    [
        "    if a:\n        return 1\n",
        "    while a:\n        return 1\n",
        "    for b in a:\n        return b\n",
        "    while True:\n        if a:\n            break\n        return 1\n",
        "    try:\n        return 1\n    except KeyError:\n        pass\n",
    ],
)
def test_a_function_that_can_fall_through_is_recognised(body):
    assert contract_of_source(f"def f(a):\n{body}").may_return_none


def test_a_different_kind_of_value_is_caught():
    found = breaches(
        "def f(a):\n    return Record(a)\n", "def f(a):\n    return [a]\n"
    )

    assert found[0].code == "return-kind-changed"
    assert found[0].message == "it used to return Record and now returns list"


def test_a_tuple_of_another_length_is_caught():
    assert codes(
        "def f(a):\n    return a, 1\n", "def f(a):\n    return a, 1, 2\n"
    ) == ["return-kind-changed"]


def test_numbers_count_as_one_kind():
    assert codes("def f():\n    return 1\n", "def f():\n    return 1.0\n") == []


def test_an_unknown_return_is_not_judged():
    assert codes(
        "def f(a):\n    return a.value()\n", "def f(a):\n    return [a]\n"
    ) == []


def test_lost_dict_keys_are_caught():
    found = breaches(
        "def f():\n    return {'a': 1, 'b': 2}\n", "def f():\n    return {'a': 1}\n"
    )

    assert found[0].code == "dict-keys-lost"
    assert "'b'" in found[0].message


def test_a_lost_exception_is_caught():
    assert codes(
        "def f(a):\n    if a:\n        raise RecordError('x')\n    return 1\n",
        "def f(a):\n    if a:\n        raise ValueError('x')\n    return 1\n",
    ) == ["exception-removed"]


def test_a_new_exception_is_shown_but_does_not_block():
    found = breaches(
        "def f(a):\n    return 1\n",
        "def f(a):\n    if a:\n        raise ValueError('x')\n    return 1\n",
    )

    assert [(item.code, item.blocking) for item in found] == [
        ("exception-added", False)
    ]


def test_raising_a_caught_exception_again_is_not_counted():
    contract = contract_of_source(
        "def f():\n    try:\n        g()\n    except KeyError as error:\n"
        "        raise error\n"
    )

    assert contract.raises == frozenset()


def test_a_changed_return_annotation_is_caught():
    assert "return-annotation-changed" in codes(
        "def f() -> int:\n    return g()\n", "def f() -> str:\n    return g()\n"
    )


def test_another_spelling_of_an_annotation_is_not_a_change():
    assert (
        codes(
            "def f() -> Optional[List[int]]:\n    return g()\n",
            "def f() -> list[int] | None:\n    return g()\n",
        )
        == []
    )


def test_only_the_parameters_can_be_compared():
    found = compare_parameters(
        contract_of_source("def f(a):\n    return 1\n"),
        contract_of_source("def f(a):\n    return 'x'\n"),
    )

    assert found == []


@pytest.mark.parametrize(
    ("source", "call"),
    [
        ("def f(a, b=1): pass", "f(1)"),
        ("def f(a, b=1): pass", "f(1, 2)"),
        ("def f(a, b=1): pass", "f(a=1, b=2)"),
        ("def f(a, *, b): pass", "f(1, b=2)"),
        ("def f(*a): pass", "f(1, 2, 3)"),
        ("def f(**a): pass", "f(x=1)"),
        ("def f(a, b): pass", "f(*pair)"),
        ("def f(a, b): pass", "f(**options)"),
    ],
)
def test_a_valid_call_binds(source, call):
    assert bind(contract_of_source(source), call_of(call)) is None


@pytest.mark.parametrize(
    ("source", "call", "problem"),
    [
        ("def f(a): pass", "f(1, 2)", "takes 1 positional argument but 2 were given"),
        ("def f(a): pass", "f()", "is missing `a`"),
        ("def f(a): pass", "f(1, b=2)", "has no parameter `b`"),
        ("def f(a): pass", "f(1, a=2)", "got two values for `a`"),
        ("def f(a, /): pass", "f(a=1)", "has no parameter `a`"),
        ("def f(*, a): pass", "f(1)", "takes 0 positional arguments but 1 were given"),
        ("def f(a, b): pass", "f(*pair, c=1)", "has no parameter `c`"),
    ],
)
def test_an_invalid_call_says_why(source, call, problem):
    assert bind(contract_of_source(source), call_of(call)) == problem


def test_a_bound_call_skips_self():
    contract = contract_of_source("def add(self, item): pass")

    assert bind(contract, call_of("stock.add(1)"), skip_first=True) is None
    assert bind(contract, call_of("stock.add(1)")) == "is missing `item`"

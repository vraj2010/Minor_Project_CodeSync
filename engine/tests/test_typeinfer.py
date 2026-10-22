import ast

import pytest

from codesync.typeinfer import combine_types, infer_type, operator_symbol


def infer(source, resolve_call=None):
    return infer_type(ast.parse(source, mode="eval").body, resolve_call)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1", "int"),
        ("1.5", "float"),
        ("'text'", "str"),
        ("b'raw'", "bytes"),
        ("True", "bool"),
        ("None", "None"),
        ("2j", "complex"),
        ("...", "ellipsis"),
        ("f'{x}'", "str"),
        ("[1, 2]", "list"),
        ("[x for x in y]", "list"),
        ("{'a': 1}", "dict"),
        ("{k: v for k, v in y}", "dict"),
        ("{1, 2}", "set"),
        ("(1, 2)", "tuple"),
        ("(x for x in y)", "generator"),
        ("lambda: 0", "function"),
        ("a < b", "bool"),
        ("not a", "bool"),
        ("unknown_name", None),
        ("obj.attr", None),
        ("items[0]", None),
    ],
)
def test_literals_and_syntax_give_a_type(source, expected):
    assert infer(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1 + 2", "int"),
        ("1 + 2.0", "float"),
        ("1 / 2", "float"),
        ("1 // 2", "int"),
        ("2j + 1", "complex"),
        ("'a' + 'b'", "str"),
        ("'a' * 3", "str"),
        ("3 * [0]", "list"),
        ("'%s' % value", "str"),
        ("'-' * width", "str"),
        ("-5", "int"),
        ("-x", None),
        ("'a' + 1", None),
        ("x + 1", None),
    ],
)
def test_arithmetic_follows_the_operand_types(source, expected):
    assert infer(source) == expected


def test_conditionals_combine_both_branches():
    assert infer("1 if flag else None") == "int | None"
    assert infer("1 if flag else 2") == "int"
    assert infer("1 if flag else other") == "int"


def test_boolean_operators_need_agreeing_operands():
    assert infer("'a' or 'b'") == "str"
    assert infer("'a' or None") is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("int(x)", "int"),
        ("str(x)", "str"),
        ("list()", "list"),
        ("dict(a=1)", "dict"),
        ("set()", "set"),
        ("len(x)", "int"),
        ("sorted(x)", "list"),
        ("range(3)", "range"),
        ("isinstance(x, int)", "bool"),
        ("something(x)", None),
    ],
)
def test_builtin_calls_have_known_results(source, expected):
    assert infer(source) == expected


def test_other_calls_are_left_to_the_resolver():
    seen = []

    def resolve(func):
        seen.append(ast.unparse(func))
        return "shop.Cart"

    assert infer("Cart(owner)", resolve) == "shop.Cart"
    assert infer("module.make()", resolve) == "shop.Cart"
    assert seen == ["Cart", "module.make"]


def test_combine_sorts_and_puts_none_last():
    assert combine_types(["str", "int", "None", "int"]) == "int | str | None"
    assert combine_types(["int | None", "str"]) == "int | str | None"
    assert combine_types([None, None]) is None
    assert combine_types([]) is None


def test_operators_have_their_symbol():
    assert operator_symbol(ast.Add()) == "+"
    assert operator_symbol(ast.FloorDiv()) == "//"
    assert operator_symbol(ast.MatMult()) == "@"

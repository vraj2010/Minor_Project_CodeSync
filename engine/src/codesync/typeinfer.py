"""A cheap guess at the type of an expression, read from the code alone.

Only what the syntax shows is reported: literals, comprehensions, builtin
constructors and the calls a project can resolve. Anything that would need the
code to run is left unknown, and the guess is only ever evidence for the model
that describes the variable later."""

import ast
from collections.abc import Callable, Iterable

CallResolver = Callable[[ast.expr], str | None]

_CONSTRUCTORS = frozenset(
    {
        "int",
        "float",
        "str",
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
    }
)
_CALL_RESULTS = {
    "len": "int",
    "repr": "str",
    "input": "str",
    "sorted": "list",
    "range": "range",
    "any": "bool",
    "all": "bool",
    "isinstance": "bool",
    "issubclass": "bool",
    "callable": "bool",
    "enumerate": "enumerate",
    "zip": "zip",
    "map": "map",
    "filter": "filter",
    "reversed": "reversed",
}
_NUMBERS = ("int", "float", "complex")
_SEQUENCES = ("str", "bytes", "list", "tuple")
_OPERATORS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.BitAnd: "&",
    ast.MatMult: "@",
}


def operator_symbol(operator: ast.operator) -> str:
    return _OPERATORS.get(type(operator), "?")


def infer_type(node: ast.expr, resolve_call: CallResolver | None = None) -> str | None:
    if isinstance(node, ast.Constant):
        return _constant(node.value)
    if isinstance(node, ast.JoinedStr):
        return "str"
    if isinstance(node, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.GeneratorExp):
        return "generator"
    if isinstance(node, ast.Lambda):
        return "function"
    if isinstance(node, ast.Compare):
        return "bool"
    if isinstance(node, ast.UnaryOp):
        return _unary(node, resolve_call)
    if isinstance(node, ast.BoolOp):
        return _agreed([infer_type(value, resolve_call) for value in node.values])
    if isinstance(node, ast.IfExp):
        return combine_types(
            [infer_type(node.body, resolve_call), infer_type(node.orelse, resolve_call)]
        )
    if isinstance(node, ast.BinOp):
        return _binary(node, resolve_call)
    if isinstance(node, ast.Call):
        return _call(node, resolve_call)
    return None


def combine_types(types: Iterable[str | None]) -> str | None:
    """The union of the known types, written `a | b` with None last."""
    members: set[str] = set()
    for item in types:
        if item:
            members.update(part.strip() for part in item.split("|"))
    if not members:
        return None
    return " | ".join(sorted(members, key=lambda part: (part == "None", part)))


def _constant(value: object) -> str:
    if value is None:
        return "None"
    if value is Ellipsis:
        return "ellipsis"
    return type(value).__name__


def _agreed(types: list[str | None]) -> str | None:
    known = set(types)
    if len(known) == 1 and None not in known:
        return known.pop()
    return None


def _unary(node: ast.UnaryOp, resolve_call: CallResolver | None) -> str | None:
    if isinstance(node.op, ast.Not):
        return "bool"
    operand = infer_type(node.operand, resolve_call)
    return operand if operand in _NUMBERS else None


def _binary(node: ast.BinOp, resolve_call: CallResolver | None) -> str | None:
    left = infer_type(node.left, resolve_call)
    right = infer_type(node.right, resolve_call)

    if isinstance(node.op, ast.Mod) and left == "str":
        return "str"
    if isinstance(node.op, ast.Mult):
        if left in _SEQUENCES and right in ("int", None):
            return left
        if right in _SEQUENCES and left == "int":
            return right
    if left is None or right is None:
        return None

    if left in _NUMBERS and right in _NUMBERS:
        if isinstance(node.op, ast.Div):
            return "complex" if "complex" in (left, right) else "float"
        for wider in ("complex", "float"):
            if wider in (left, right):
                return wider
        return "int"
    if left == right and left in _SEQUENCES and isinstance(node.op, ast.Add):
        return left
    return None


def _call(node: ast.Call, resolve_call: CallResolver | None) -> str | None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name in _CONSTRUCTORS:
            return name
        if name in _CALL_RESULTS:
            return _CALL_RESULTS[name]
    return resolve_call(node.func) if resolve_call is not None else None

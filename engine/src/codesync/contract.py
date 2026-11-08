"""What a function promises, and whether a rewrite still keeps the promise.

A contract is a snapshot of what the rest of a project can rely on: what goes in
(the parameters, how they are passed and their defaults) and what comes out
(whether it is awaited or iterated, the kind of value returned, the exceptions
raised). Taking it before and after a rewrite turns "did this change anything
important?" into a comparison of two records.

Everything here is read from the syntax. When the kind of value a function
returns cannot be told without running it, that is recorded as unknown, and a
comparison involving an unknown stays silent: the tests are there for that."""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NEW_SCOPES = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_BINDING_DECORATORS = frozenset({"property", "classmethod", "staticmethod"})

_CALL_RESULTS = {
    "abs": "number",
    "all": "bool",
    "any": "bool",
    "bool": "bool",
    "bytes": "bytes",
    "dict": "dict",
    "float": "number",
    "frozenset": "frozenset",
    "int": "number",
    "isinstance": "bool",
    "issubclass": "bool",
    "len": "number",
    "list": "list",
    "repr": "str",
    "round": "number",
    "set": "set",
    "sorted": "list",
    "str": "str",
    "sum": "number",
    "tuple": "tuple",
}


class ParameterKind(str, Enum):
    POSITIONAL_ONLY = "positional-only"
    POSITIONAL = "positional"
    VAR_POSITIONAL = "var-positional"
    KEYWORD_ONLY = "keyword-only"
    VAR_KEYWORD = "var-keyword"


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: ParameterKind
    default: str | None = None
    annotation: str | None = None

    @property
    def required(self) -> bool:
        return self.default is None and self.kind not in (
            ParameterKind.VAR_POSITIONAL,
            ParameterKind.VAR_KEYWORD,
        )

    @property
    def by_position(self) -> bool:
        return self.kind in (ParameterKind.POSITIONAL_ONLY, ParameterKind.POSITIONAL)

    @property
    def by_keyword(self) -> bool:
        return self.kind in (ParameterKind.POSITIONAL, ParameterKind.KEYWORD_ONLY)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "default": self.default,
            "annotation": self.annotation,
        }


@dataclass(frozen=True)
class Contract:
    name: str
    parameters: tuple[Parameter, ...]
    return_annotation: str | None
    decorators: tuple[str, ...]
    is_async: bool
    is_generator: bool
    raises: frozenset[str]
    returns: frozenset[str]
    returns_unknown: bool
    may_return_none: bool
    dict_keys: frozenset[str] | None

    @property
    def returns_value(self) -> bool:
        return bool(self.returns - {"None"}) or self.returns_unknown

    @property
    def tuple_lengths(self) -> frozenset[int] | None:
        """The lengths of the tuples returned, when every return is one whose
        length is known."""
        if self.returns_unknown or not self.returns:
            return None
        lengths = set()
        for shape in self.returns:
            if not (shape.startswith("tuple[") and shape.endswith("]")):
                return None
            lengths.add(int(shape[6:-1]))
        return frozenset(lengths)

    def has_decorator(self, name: str) -> bool:
        return any(_last_part(item) == name for item in self.decorators)

    def describe(self) -> str:
        parts = []
        seen_keyword_marker = False
        for parameter in self.parameters:
            text = parameter.name
            if parameter.kind is ParameterKind.VAR_POSITIONAL:
                text = "*" + text
                seen_keyword_marker = True
            elif parameter.kind is ParameterKind.VAR_KEYWORD:
                text = "**" + text
            elif parameter.kind is ParameterKind.KEYWORD_ONLY:
                if not seen_keyword_marker:
                    parts.append("*")
                    seen_keyword_marker = True
            if parameter.default is not None:
                text += f"={parameter.default}"
            parts.append(text)
        prefix = "async " if self.is_async else ""
        return f"{prefix}{self.name}({', '.join(parts)})"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "signature": self.describe(),
            "parameters": [item.to_dict() for item in self.parameters],
            "return_annotation": self.return_annotation,
            "decorators": list(self.decorators),
            "is_async": self.is_async,
            "is_generator": self.is_generator,
            "raises": sorted(self.raises),
            "returns": sorted(self.returns),
            "returns_unknown": self.returns_unknown,
            "may_return_none": self.may_return_none,
        }


@dataclass(frozen=True)
class Breach:
    """One way a rewrite stopped keeping the contract. A breach that does not
    block is still shown, because a reader may want to know."""

    code: str
    message: str
    blocking: bool = True


def snapshot(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Contract:
    returns, unknown, keys = _return_shapes(node)
    generator = any(
        isinstance(item, (ast.Yield, ast.YieldFrom)) for item in own_nodes(node)
    )
    return Contract(
        name=node.name,
        parameters=_parameters(node.args),
        return_annotation=_text(node.returns),
        decorators=tuple(ast.unparse(item) for item in node.decorator_list),
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_generator=generator,
        raises=_raised(node),
        returns=returns,
        returns_unknown=unknown,
        may_return_none="None" in returns or not _ends(node.body),
        dict_keys=keys,
    )


def contract_of_source(source: str, name: str | None = None) -> Contract:
    """The contract of the function defined in `source`."""
    for statement in ast.parse(source).body:
        if isinstance(statement, _FUNCTIONS) and name in (None, statement.name):
            return snapshot(statement)
    raise ValueError(f"no function {name or ''} in the source".replace("  ", " "))


def compare(before: Contract, after: Contract) -> list[Breach]:
    breaches: list[Breach] = []
    if before.name != after.name:
        breaches.append(
            Breach("renamed", f"the function was renamed to `{after.name}`")
        )
    breaches.extend(compare_parameters(before, after))
    breaches.extend(_compare_kind_of_call(before, after))
    breaches.extend(_compare_decorators(before, after))
    breaches.extend(_compare_returns(before, after))
    breaches.extend(_compare_raises(before, after))
    return breaches


def compare_parameters(before: Contract, after: Contract) -> list[Breach]:
    """Every way a call that was valid against `before` may no longer be valid
    against `after`, or may mean something different."""
    breaches: list[Breach] = []
    new_by_name = {item.name: item for item in after.parameters}
    old_names = {item.name for item in before.parameters}
    has_var_positional = any(
        item.kind is ParameterKind.VAR_POSITIONAL for item in after.parameters
    )
    has_var_keyword = any(
        item.kind is ParameterKind.VAR_KEYWORD for item in after.parameters
    )
    old_positional = [item for item in before.parameters if item.by_position]
    new_positional = [item for item in after.parameters if item.by_position]
    fresh = [
        item
        for item in after.parameters
        if item.name not in old_names
        and item.kind
        not in (ParameterKind.VAR_POSITIONAL, ParameterKind.VAR_KEYWORD)
    ]

    for parameter in before.parameters:
        current = new_by_name.get(parameter.name)
        if parameter.kind is ParameterKind.VAR_POSITIONAL:
            if not has_var_positional:
                breaches.append(
                    Breach(
                        "var-removed",
                        f"it no longer accepts extra positional arguments "
                        f"(`*{parameter.name}`)",
                    )
                )
            continue
        if parameter.kind is ParameterKind.VAR_KEYWORD:
            if not has_var_keyword:
                breaches.append(
                    Breach(
                        "var-removed",
                        f"it no longer accepts extra keyword arguments "
                        f"(`**{parameter.name}`)",
                    )
                )
            continue

        if current is None:
            breaches.append(_lost_parameter(parameter, fresh))
            continue

        if parameter.by_position and not current.by_position:
            breaches.append(
                Breach(
                    "now-keyword-only",
                    f"`{parameter.name}` can no longer be passed by position",
                )
            )
        if parameter.by_keyword and not current.by_keyword:
            breaches.append(
                Breach(
                    "now-positional-only",
                    f"`{parameter.name}` can no longer be passed by keyword",
                )
            )
        if parameter.by_position and current.by_position:
            was = old_positional.index(parameter)
            now = new_positional.index(current)
            if was != now:
                breaches.append(
                    Breach(
                        "moved",
                        f"`{parameter.name}` moved from position {was + 1} to "
                        f"{now + 1}",
                    )
                )
        if parameter.default is not None and current.default is None:
            breaches.append(
                Breach(
                    "default-removed",
                    f"`{parameter.name}` no longer has a default, so it became "
                    "required",
                )
            )
        elif parameter.default is not None and parameter.default != current.default:
            breaches.append(
                Breach(
                    "default-changed",
                    f"the default of `{parameter.name}` changed from "
                    f"{parameter.default} to {current.default}",
                )
            )
        elif (
            parameter.annotation
            and current.annotation
            and _normal(parameter.annotation) != _normal(current.annotation)
        ):
            breaches.append(
                Breach(
                    "annotation-changed",
                    f"the type of `{parameter.name}` changed from "
                    f"{parameter.annotation} to {current.annotation}",
                    blocking=False,
                )
            )

    for parameter in fresh:
        if parameter.required:
            breaches.append(
                Breach(
                    "new-required",
                    f"`{parameter.name}` is a new parameter with no default, "
                    "so existing calls do not supply it",
                )
            )
    return breaches


def bind(contract: Contract, call: ast.Call, *, skip_first: bool = False) -> str | None:
    """Why `call` cannot bind to `contract`, the way the interpreter would
    decide, or None if it can. With `*args` or `**kwargs` in the call the number
    of arguments is unknown, so only what is certain is judged."""
    parameters = list(contract.parameters)
    if skip_first and parameters:
        parameters = parameters[1:]

    positional = [item for item in parameters if item.by_position]
    accepts_more = any(
        item.kind is ParameterKind.VAR_POSITIONAL for item in parameters
    )
    accepts_keywords = any(
        item.kind is ParameterKind.VAR_KEYWORD for item in parameters
    )
    has_star = any(isinstance(item, ast.Starred) for item in call.args)
    has_double_star = any(item.arg is None for item in call.keywords)
    given = sum(1 for item in call.args if not isinstance(item, ast.Starred))

    if given > len(positional) and not accepts_more:
        noun = "argument" if len(positional) == 1 else "arguments"
        return f"takes {len(positional)} positional {noun} but {given} were given"

    filled = {item.name for item in positional[:given]}
    by_name = {item.name: item for item in parameters}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        parameter = by_name.get(keyword.arg)
        if parameter is None or not parameter.by_keyword:
            if not accepts_keywords:
                return f"has no parameter `{keyword.arg}`"
            continue
        if keyword.arg in filled:
            return f"got two values for `{keyword.arg}`"
        filled.add(keyword.arg)

    if has_star or has_double_star:
        return None
    missing = [
        item.name for item in parameters if item.required and item.name not in filled
    ]
    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        return f"is missing {names}"
    return None


def own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """The nodes inside a function that belong to it: nested functions, lambdas
    and classes run on their own terms, so they are not entered."""
    stack = list(ast.iter_child_nodes(node))
    decorators = {id(item) for item in getattr(node, "decorator_list", ())}
    stack = [item for item in stack if id(item) not in decorators]
    while stack:
        current = stack.pop()
        yield current
        if not isinstance(current, _NEW_SCOPES):
            stack.extend(ast.iter_child_nodes(current))


def _lost_parameter(parameter: Parameter, fresh: list[Parameter]) -> Breach:
    """A parameter that disappeared while a new one appeared was most likely
    renamed, and saying so tells a model far more than "removed" would."""
    if fresh:
        return Breach(
            "renamed-parameter",
            f"`{parameter.name}` was renamed to `{fresh.pop(0).name}`",
        )
    return Breach("removed-parameter", f"`{parameter.name}` was removed")


def _compare_kind_of_call(before: Contract, after: Contract) -> list[Breach]:
    breaches = []
    if before.is_async != after.is_async:
        now = "async" if after.is_async else "not async"
        breaches.append(
            Breach(
                "async-changed",
                f"it is now {now}, so callers have to change how they call it",
            )
        )
    if before.is_generator != after.is_generator:
        now = "a generator" if after.is_generator else "no longer a generator"
        breaches.append(
            Breach(
                "generator-changed",
                f"it is now {now}, so what a call hands back is different",
            )
        )
    return breaches


def _compare_decorators(before: Contract, after: Contract) -> list[Breach]:
    breaches = []
    old = set(before.decorators)
    new = set(after.decorators)
    for decorator in sorted(old - new):
        breaches.append(
            Breach("decorator-removed", f"the decorator `@{decorator}` was removed")
        )
    for decorator in sorted(new - old):
        changes_binding = _last_part(decorator) in _BINDING_DECORATORS
        breaches.append(
            Breach(
                "decorator-added",
                f"the decorator `@{decorator}` was added",
                blocking=changes_binding,
            )
        )
    return breaches


def _compare_returns(before: Contract, after: Contract) -> list[Breach]:
    if before.is_generator or after.is_generator:
        return []

    breaches = []
    if (
        before.return_annotation
        and after.return_annotation
        and _normal(before.return_annotation) != _normal(after.return_annotation)
    ):
        breaches.append(
            Breach(
                "return-annotation-changed",
                f"the declared return type changed from {before.return_annotation} "
                f"to {after.return_annotation}",
            )
        )

    if before.returns_value and not before.may_return_none and after.may_return_none:
        if not after.returns_value:
            message = "it used to return a value and now returns nothing"
        else:
            message = "it used to always return a value and can now return None"
        breaches.append(Breach("may-return-none", message))

    known_before = before.returns - {"None"}
    known_after = after.returns - {"None"}
    if (
        known_before
        and known_after
        and not before.returns_unknown
        and not after.returns_unknown
        and not _overlap(known_before, known_after)
    ):
        breaches.append(
            Breach(
                "return-kind-changed",
                f"it used to return {_listing(known_before)} and now returns "
                f"{_listing(known_after)}",
            )
        )

    if before.dict_keys is not None and after.dict_keys is not None:
        lost = sorted(before.dict_keys - after.dict_keys)
        if lost:
            names = ", ".join(repr(item) for item in lost)
            breaches.append(
                Breach("dict-keys-lost", f"the returned dict lost the keys {names}")
            )
    return breaches


def _compare_raises(before: Contract, after: Contract) -> list[Breach]:
    breaches = []
    for name in sorted(before.raises - after.raises):
        breaches.append(
            Breach(
                "exception-removed",
                f"it no longer raises `{name}`, which callers may be catching",
            )
        )
    for name in sorted(after.raises - before.raises):
        breaches.append(
            Breach("exception-added", f"it can now raise `{name}`", blocking=False)
        )
    return breaches


def _overlap(first: frozenset[str] | set[str], second: frozenset[str] | set[str]) -> bool:
    return any(_same_kind(a, b) for a in first for b in second)


def _same_kind(first: str, second: str) -> bool:
    if first == second:
        return True
    tuples = first.startswith("tuple") and second.startswith("tuple")
    return tuples and "tuple" in (first, second)


def _listing(shapes: frozenset[str] | set[str]) -> str:
    return " or ".join(sorted(shapes))


def _parameters(arguments: ast.arguments) -> tuple[Parameter, ...]:
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(arguments.defaults)
    )
    defaults.extend(arguments.defaults)

    found: list[Parameter] = []
    for position, (argument, default) in enumerate(
        zip(positional, defaults, strict=True)
    ):
        kind = (
            ParameterKind.POSITIONAL_ONLY
            if position < len(arguments.posonlyargs)
            else ParameterKind.POSITIONAL
        )
        found.append(_parameter(argument, kind, default))
    if arguments.vararg is not None:
        found.append(_parameter(arguments.vararg, ParameterKind.VAR_POSITIONAL))
    for argument, default in zip(
        arguments.kwonlyargs, arguments.kw_defaults, strict=True
    ):
        found.append(_parameter(argument, ParameterKind.KEYWORD_ONLY, default))
    if arguments.kwarg is not None:
        found.append(_parameter(arguments.kwarg, ParameterKind.VAR_KEYWORD))
    return tuple(found)


def _parameter(
    argument: ast.arg, kind: ParameterKind, default: ast.expr | None = None
) -> Parameter:
    return Parameter(
        name=argument.arg,
        kind=kind,
        default=_text(default),
        annotation=_text(argument.annotation),
    )


def _text(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _normal(annotation: str) -> str:
    """An annotation with its spelling evened out, so that `List[int]` and
    `list[int]`, or `Optional[str]` and `str | None`, count as the same."""
    text = annotation.replace("typing.", "").replace(" ", "").replace("'", "")
    for old, new in (
        ("List[", "list["),
        ("Dict[", "dict["),
        ("Set[", "set["),
        ("Tuple[", "tuple["),
    ):
        text = text.replace(old, new)
    if text.startswith("Optional[") and text.endswith("]"):
        text = text[9:-1] + "|None"
    return text


def _last_part(dotted: str) -> str:
    return dotted.partition("(")[0].rpartition(".")[2]


def _raised(node: ast.AST) -> frozenset[str]:
    names = set()
    for item in own_nodes(node):
        if not isinstance(item, ast.Raise) or item.exc is None:
            continue
        raised = item.exc.func if isinstance(item.exc, ast.Call) else item.exc
        name = ast.unparse(raised).rpartition(".")[2]
        # A lower-case name is a caught exception being raised again, whose
        # type cannot be told from here.
        if name[:1].isupper():
            names.add(name)
    return frozenset(names)


def _return_shapes(
    node: ast.AST,
) -> tuple[frozenset[str], bool, frozenset[str] | None]:
    shapes: set[str] = set()
    unknown = False
    keys: set[str] = set()
    every_return_is_a_dict = True
    returned = False

    for item in own_nodes(node):
        if not isinstance(item, ast.Return):
            continue
        returned = True
        value = item.value
        if isinstance(value, ast.Dict) and _constant_keys(value) is not None:
            keys |= _constant_keys(value)
        else:
            every_return_is_a_dict = False

        found = _shapes_of(value)
        if found is None:
            unknown = True
        else:
            shapes |= found

    known_keys = frozenset(keys) if returned and every_return_is_a_dict else None
    return frozenset(shapes), unknown, known_keys


def _constant_keys(node: ast.Dict) -> set[str] | None:
    keys = set()
    for key in node.keys:
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        keys.add(key.value)
    return keys


def _shapes_of(value: ast.expr | None) -> set[str] | None:
    """The kinds of value an expression gives, or None when that takes running
    the code to know."""
    if value is None:
        return {"None"}
    if isinstance(value, ast.IfExp):
        first, second = _shapes_of(value.body), _shapes_of(value.orelse)
        return None if first is None or second is None else first | second
    shape = _shape_of(value)
    return None if shape is None else {shape}


def _shape_of(value: ast.expr) -> str | None:
    if isinstance(value, ast.Constant):
        return _constant_shape(value.value)
    if isinstance(value, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(value, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(value, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(value, ast.GeneratorExp):
        return "generator"
    if isinstance(value, ast.JoinedStr):
        return "str"
    if isinstance(value, ast.Compare):
        return "bool"
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not):
        return "bool"
    if isinstance(value, ast.Tuple):
        if any(isinstance(item, ast.Starred) for item in value.elts):
            return "tuple"
        return f"tuple[{len(value.elts)}]"
    if isinstance(value, ast.Name) and value.id == "self":
        return "self"
    if isinstance(value, ast.Call):
        return _call_shape(value)
    return None


def _constant_shape(value: object) -> str | None:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, bytes):
        return "bytes"
    return None


def _call_shape(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    else:
        return None

    if isinstance(call.func, ast.Name) and name in _CALL_RESULTS:
        return _CALL_RESULTS[name]
    return name if name[:1].isupper() else None


def _ends(statements: list[ast.stmt]) -> bool:
    """Whether control can never run off the end of these statements."""
    return any(_terminates(item) for item in statements)


def _terminates(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise)):
        return True
    if isinstance(statement, ast.If):
        return bool(statement.orelse) and _ends(statement.body) and _ends(statement.orelse)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _ends(statement.body)
    if isinstance(statement, ast.Try):
        if _ends(statement.finalbody):
            return True
        main = statement.orelse or statement.body
        return _ends(main) and all(_ends(item.body) for item in statement.handlers)
    if isinstance(statement, ast.While):
        endless = (
            isinstance(statement.test, ast.Constant) and bool(statement.test.value)
        )
        return endless and not any(
            isinstance(item, ast.Break) for item in own_nodes(statement)
        )
    if isinstance(statement, ast.Match):
        wildcard = any(
            isinstance(case.pattern, ast.MatchAs)
            and case.pattern.pattern is None
            and case.guard is None
            for case in statement.cases
        )
        return wildcard and all(_ends(case.body) for case in statement.cases)
    return False

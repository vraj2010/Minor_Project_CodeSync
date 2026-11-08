"""Does the rest of the project still fit a rewritten function?

The contract check asks whether the promise changed. This one goes to every
place that relies on the promise and asks whether that place still works. A
call is judged against both versions of the function: one that was already
wrong, or that matched by name alone and belongs to something else, is not the
rewrite's fault, and only a call that worked before and does not now is
reported."""

import ast
from dataclasses import dataclass

from .contract import Contract, bind, compare_parameters, snapshot
from .findings import Check, Finding
from .references import Confidence, Reference, UsageKind
from .resolve import Resolver
from .symbols import Symbol, SymbolKind

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_BUILTIN_TYPES = {
    "bool": bool,
    "bytes": bytes,
    "dict": dict,
    "frozenset": frozenset,
    "list": list,
    "number": float,
    "set": set,
    "str": str,
}


@dataclass(frozen=True)
class _Use:
    """One way a caller handles what a call gives back."""

    kind: str
    detail: object = None


def check_call_sites(
    resolver: Resolver,
    target: Symbol,
    before: Contract,
    after: Contract,
    references: list[Reference],
) -> list[Finding]:
    findings: list[Finding] = []
    trees = _Trees(resolver)
    renamed = before.name != after.name

    for ref in references:
        if _inside(ref, target):
            continue
        if renamed and ref.kind is not UsageKind.CALL:
            findings.append(_finding(ref, "name-gone", _gone_message(after)))
            continue
        if ref.kind is not UsageKind.CALL:
            continue

        call = _parse_call(ref.text)
        if call is None:
            continue
        if renamed:
            findings.append(_finding(ref, "name-gone", _gone_message(after)))
            continue

        skip_first = _skips_first(resolver, target, before, call, ref)
        was = bind(before, call, skip_first=skip_first)
        now = bind(after, call, skip_first=skip_first)
        if was is None and now is not None:
            findings.append(_finding(ref, "call-no-longer-binds", f"the call {now}"))
            continue

        for use in trees.uses_of(ref):
            problem = _judge_use(use, before, after)
            if problem is not None:
                findings.append(_finding(ref, problem[0], problem[1]))
    return findings


def check_overrides(
    resolver: Resolver, target: Symbol, before: Contract, after: Contract
) -> list[Finding]:
    """A method that is overridden, or that overrides another, shares its
    signature with that method: code holding the base class calls either one."""
    if target.kind is not SymbolKind.METHOD:
        return []
    cls = resolver.enclosing_class(target)
    if cls is None:
        return []

    findings: list[Finding] = []
    for other in _subclasses(resolver, cls):
        method = resolver.table.lookup(f"{other.qualname}.{target.name}")
        contract = _contract_of(resolver, method)
        if contract is None or method is None:
            continue
        if not compare_parameters(before, contract) and compare_parameters(
            after, contract
        ):
            findings.append(
                Finding(
                    Check.CALL_SITES,
                    "override-mismatch",
                    f"`{method.qualname}` overrides this method and no longer "
                    f"matches it: it is still {contract.describe()}",
                    method.relpath,
                    method.position.lineno,
                )
            )

    for path in resolver.bases(cls):
        base = resolver.canonical(path)
        if base is None or base.kind is not SymbolKind.CLASS:
            continue
        method = resolver.member(base, target.name)
        contract = _contract_of(resolver, method)
        if contract is None or method is None:
            continue
        if not compare_parameters(contract, before) and compare_parameters(
            contract, after
        ):
            findings.append(
                Finding(
                    Check.CALL_SITES,
                    "override-mismatch",
                    f"this method overrides `{method.qualname}` and no longer "
                    f"matches it: that one is {contract.describe()}",
                    method.relpath,
                    method.position.lineno,
                )
            )
    return findings


def _contract_of(resolver: Resolver, symbol: Symbol | None) -> Contract | None:
    if symbol is None or symbol.kind is not SymbolKind.METHOD:
        return None
    node = resolver.node_for(symbol)
    return snapshot(node) if isinstance(node, _FUNCTIONS) else None


def _subclasses(resolver: Resolver, cls: Symbol) -> list[Symbol]:
    found = []
    for symbol in resolver.table.symbols:
        if symbol.kind is not SymbolKind.CLASS or symbol == cls:
            continue
        if _descends_from(resolver, symbol, cls, set()):
            found.append(symbol)
    return found


def _descends_from(
    resolver: Resolver, cls: Symbol, ancestor: Symbol, seen: set[str]
) -> bool:
    if cls.qualname in seen:
        return False
    seen.add(cls.qualname)
    for path in resolver.bases(cls):
        base = resolver.canonical(path)
        if base is None:
            continue
        if base == ancestor or _descends_from(resolver, base, ancestor, seen):
            return True
    return False


def _inside(ref: Reference, target: Symbol) -> bool:
    scope = ref.scope
    return scope is not None and (
        scope == target or scope.qualname.startswith(target.qualname + ".")
    )


def _gone_message(after: Contract) -> str:
    return f"this still uses the old name, but the function is now `{after.name}`"


def _finding(ref: Reference, code: str, message: str) -> Finding:
    if ref.confidence is Confidence.NAME_ONLY:
        message += " (matched by name only)"
    return Finding(
        Check.CALL_SITES, code, message, ref.relpath, ref.lineno, ref.summary
    )


def _parse_call(text: str) -> ast.Call | None:
    try:
        expression = ast.parse(f"(\n{text}\n)", mode="eval").body
    except SyntaxError:
        return None
    return expression if isinstance(expression, ast.Call) else None


def _skips_first(
    resolver: Resolver,
    target: Symbol,
    before: Contract,
    call: ast.Call,
    ref: Reference,
) -> bool:
    """Whether Python fills in the first parameter of this call itself."""
    if target.kind is not SymbolKind.METHOD or before.has_decorator("staticmethod"):
        return False
    if target.name == "__init__" or before.has_decorator("classmethod"):
        return True
    if not isinstance(call.func, ast.Attribute):
        return True

    owner = call.func.value
    if isinstance(owner, ast.Name) and owner.id in ("self", "cls"):
        return True
    if isinstance(owner, ast.Call):
        return True
    path = resolver.dotted(ref.relpath, owner, ref.scope)
    symbol = resolver.canonical(path) if path else None
    # `Stock.add(stock, item)` passes the instance by hand.
    return symbol is None or symbol.kind is not SymbolKind.CLASS


def _judge_use(
    use: _Use, before: Contract, after: Contract
) -> tuple[str, str] | None:
    if use.kind == "not-awaited" and after.is_async and not before.is_async:
        return "missing-await", "this call is not awaited, but the function is now async"
    if use.kind == "awaited" and before.is_async and not after.is_async:
        return "bad-await", "this awaits the call, but the function is no longer async"

    if before.is_generator or after.is_generator:
        return None

    if use.kind == "unpack":
        lengths = after.tuple_lengths
        if lengths is not None and use.detail not in lengths:
            shown = " or ".join(str(item) for item in sorted(lengths))
            return (
                "unpack-mismatch",
                f"this unpacks {use.detail} values, but the function now returns "
                f"{shown}",
            )

    if use.kind == "key" and after.dict_keys is not None:
        was_there = before.dict_keys is None or use.detail in before.dict_keys
        if use.detail not in after.dict_keys and was_there:
            return (
                "missing-key",
                f"this reads the key {use.detail!r}, which the returned dict no "
                "longer has",
            )

    if use.kind == "attribute":
        shapes = after.returns - {"None"}
        if shapes and not after.returns_unknown and not before.returns & shapes:
            missing = all(
                shape in _BUILTIN_TYPES
                and not hasattr(_BUILTIN_TYPES[shape], str(use.detail))
                for shape in shapes
            )
            if missing:
                return (
                    "missing-attribute",
                    f"this reads `.{use.detail}`, which what the function now "
                    "returns does not have",
                )

    if (
        use.kind in ("attribute", "key", "unpack", "index")
        and after.may_return_none
        and not before.may_return_none
        and before.returns_value
    ):
        return (
            "none-not-handled",
            "this uses the result directly, but the function can now return None",
        )
    return None


class _Trees:
    """How callers use a call's result, read from their syntax trees."""

    def __init__(self, resolver: Resolver) -> None:
        self.resolver = resolver
        self._parents: dict[str, dict[int, ast.AST]] = {}

    def uses_of(self, ref: Reference) -> list[_Use]:
        tree = self.resolver.tree(ref.relpath)
        if tree is None:
            return []
        call = self._locate(tree, ref)
        if call is None:
            return []

        parents = self._parents_of(ref.relpath, tree)
        return self._uses(call, parents, ref, follow=True)

    def _locate(self, tree: ast.Module, ref: Reference) -> ast.Call | None:
        source = self.resolver.source(ref.relpath)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and node.lineno == ref.lineno
                and node.col_offset == ref.col_offset
                and ast.get_source_segment(source, node) == ref.text
            ):
                return node
        return None

    def _parents_of(self, relpath: str, tree: ast.Module) -> dict[int, ast.AST]:
        if relpath not in self._parents:
            self._parents[relpath] = {
                id(child): parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
        return self._parents[relpath]

    def _uses(
        self,
        node: ast.expr,
        parents: dict[int, ast.AST],
        ref: Reference,
        *,
        follow: bool,
    ) -> list[_Use]:
        parent = parents.get(id(node))
        uses: list[_Use] = []

        if isinstance(parent, ast.Await):
            return [_Use("awaited"), *self._uses(parent, parents, ref, follow=follow)]
        if isinstance(node, ast.Call) and not isinstance(parent, ast.Call):
            # Passing a coroutine on, as `asyncio.gather` and friends expect,
            # is not forgetting to await it.
            uses.append(_Use("not-awaited"))

        if isinstance(parent, ast.Attribute):
            uses.append(_Use("attribute", parent.attr))
        elif isinstance(parent, ast.Subscript) and parent.value is node:
            uses.append(_subscript(parent))
        elif isinstance(parent, ast.Assign) and parent.value is node:
            for target in parent.targets:
                if isinstance(target, ast.Tuple):
                    if not any(isinstance(i, ast.Starred) for i in target.elts):
                        uses.append(_Use("unpack", len(target.elts)))
                elif isinstance(target, ast.Name) and follow:
                    uses.extend(self._uses_of_name(target.id, parent, parents, ref))
        return uses

    def _uses_of_name(
        self,
        name: str,
        assignment: ast.Assign,
        parents: dict[int, ast.AST],
        ref: Reference,
    ) -> list[_Use]:
        """How a variable holding the result is used later in the same scope."""
        scope: ast.AST | None = assignment
        while scope is not None and not isinstance(scope, (*_FUNCTIONS, ast.Module)):
            scope = parents.get(id(scope))
        if scope is None:
            return []

        uses = []
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, ast.Load)
                and node.lineno >= assignment.lineno
            ):
                uses.extend(self._uses(node, parents, ref, follow=False))
        return uses


def _subscript(node: ast.Subscript) -> _Use:
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return _Use("key", key.value)
    return _Use("index")

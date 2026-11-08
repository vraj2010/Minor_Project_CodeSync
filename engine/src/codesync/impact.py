"""Working out what a change to one definition reaches.

`find_references` answers "who uses this?" one level deep. This module follows
those answers outwards, to reach the question the project is built around: what
breaks if this function changes, and which tests would notice?"""

import ast
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath

from .references import Confidence, Reference, UsageKind, find_references
from .resolve import Resolver, absolute_module
from .symbols import Symbol, SymbolKind

DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_SYMBOLS = 500

_CALLABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD)
_TEST_FOLDERS = frozenset({"test", "tests", "testing"})


@dataclass(frozen=True)
class Affected:
    """A definition that reaches the target, directly or through others."""

    symbol: Symbol
    depth: int
    confidence: Confidence
    via: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "qualname": self.symbol.qualname,
            "kind": self.symbol.kind.value,
            "path": self.symbol.relpath,
            "lineno": self.symbol.position.lineno,
            "depth": self.depth,
            "confidence": self.confidence.value,
            "via": list(self.via),
        }


@dataclass(frozen=True)
class CoveringTest:
    """A test that reaches the target, named the way pytest selects it. A test
    file that uses the target at module level is named as a whole, because
    nothing narrower can be said about it."""

    nodeid: str
    relpath: str
    depth: int
    confidence: Confidence
    via: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "path": self.relpath,
            "depth": self.depth,
            "confidence": self.confidence.value,
            "via": list(self.via),
        }


@dataclass(frozen=True)
class Impact:
    target: Symbol
    direct: tuple[Reference, ...]
    affected: tuple[Affected, ...]
    tests: tuple[CoveringTest, ...]
    truncated: bool = False

    @property
    def files(self) -> tuple[str, ...]:
        """Files other than the target's that hold affected code, tests left
        out because a test is what notices a break, not what suffers it."""
        found = {
            ref.relpath for ref in self.direct if not is_test_file(ref.relpath)
        } | {
            item.symbol.relpath
            for item in self.affected
            if not is_test_file(item.symbol.relpath)
        }
        found.discard(self.target.relpath)
        return tuple(sorted(found))

    def to_dict(self) -> dict:
        return {
            "target": self.target.qualname,
            "path": self.target.relpath,
            "files": list(self.files),
            "direct": [ref.to_dict() for ref in self.direct],
            "affected": [item.to_dict() for item in self.affected],
            "tests": [item.to_dict() for item in self.tests],
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ModuleDependencies:
    module: str
    relpath: str
    imports: tuple[str, ...]
    imported_by: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "path": self.relpath,
            "imports": list(self.imports),
            "imported_by": list(self.imported_by),
        }


def is_test_file(relpath: str) -> bool:
    path = PurePosixPath(relpath)
    return (
        path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or path.name == "conftest.py"
        or any(part in _TEST_FOLDERS for part in path.parts[:-1])
    )


def is_test(symbol: Symbol) -> bool:
    return (
        symbol.kind in _CALLABLE
        and not symbol.is_nested
        and symbol.name.startswith("test")
        and is_test_file(symbol.relpath)
    )


def node_id(resolver: Resolver, symbol: Symbol) -> str:
    """The pytest id of a test: its file, then any classes, then its name."""
    classes = [
        item.name
        for item in resolver.table.ancestors(symbol)
        if item.kind is SymbolKind.CLASS
    ]
    return "::".join([symbol.relpath, *classes, symbol.name])


def analyze_impact(
    resolver: Resolver,
    target: Symbol,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
) -> Impact:
    """Everything that reaches `target`, nearest first.

    The walk goes breadth first, so each definition is reported with its
    shortest route. A route through a name-only match is itself name-only, since
    one unconfirmed link leaves the whole chain unconfirmed."""
    direct = [
        ref
        for ref in find_references(resolver, target)
        if not _inside(ref.scope, target)
    ]

    seen: dict[Symbol, Affected] = {}
    tests: dict[str, CoveringTest] = {}
    truncated = False
    queue: deque[tuple[Symbol, int, Confidence, tuple[str, ...]]] = deque(
        [(target, 0, Confidence.RESOLVED, ())]
    )
    while queue:
        symbol, depth, confidence, route = queue.popleft()
        if depth >= max_depth or len(seen) >= max_symbols:
            truncated = truncated or bool(find_references(resolver, symbol))
            continue

        for ref in find_references(resolver, symbol):
            if ref.kind is UsageKind.IMPORT or _inside(ref.scope, symbol):
                continue
            link = (
                Confidence.NAME_ONLY
                if Confidence.NAME_ONLY in (confidence, ref.confidence)
                else Confidence.RESOLVED
            )
            step = (*route, symbol.qualname)
            scope = ref.scope
            if scope is None:
                if is_test_file(ref.relpath):
                    _add_test(tests, ref.relpath, ref.relpath, depth + 1, link, step)
                continue
            if scope == target or scope in seen:
                continue

            if is_test(scope):
                nodeid = node_id(resolver, scope)
                _add_test(tests, nodeid, scope.relpath, depth + 1, link, step)
                continue

            seen[scope] = Affected(scope, depth + 1, link, step)
            queue.append((scope, depth + 1, link, step))

        if _is_fixture(symbol):
            for user in _fixture_users(resolver, symbol):
                step = (*route, symbol.qualname)
                _add_test(
                    tests,
                    node_id(resolver, user),
                    user.relpath,
                    depth + 1,
                    confidence,
                    step,
                )

    return Impact(
        target=target,
        direct=tuple(direct),
        affected=tuple(seen.values()),
        tests=tuple(sorted(tests.values(), key=lambda item: (item.depth, item.nodeid))),
        truncated=truncated,
    )


def find_covering_tests(resolver: Resolver, target: Symbol) -> list[CoveringTest]:
    return list(analyze_impact(resolver, target).tests)


def dependency_map(resolver: Resolver) -> list[ModuleDependencies]:
    """Which project modules each module imports, and which import it. Imports
    of anything outside the project are left out."""
    imports: dict[str, set[str]] = {}
    for info in resolver.table.files:
        imports.setdefault(info.name, set()).update(
            _imported_modules(resolver, info.relpath)
        )

    imported_by: dict[str, set[str]] = {}
    for module, used in imports.items():
        for other in used:
            imported_by.setdefault(other, set()).add(module)

    return [
        ModuleDependencies(
            module=info.name,
            relpath=info.relpath,
            imports=tuple(sorted(imports.get(info.name, ()))),
            imported_by=tuple(sorted(imported_by.get(info.name, ()))),
        )
        for info in resolver.table.modules
    ]


def _add_test(
    tests: dict[str, CoveringTest],
    nodeid: str,
    relpath: str,
    depth: int,
    confidence: Confidence,
    via: tuple[str, ...],
) -> None:
    held = tests.get(nodeid)
    if held is None or (
        held.confidence is Confidence.NAME_ONLY and confidence is Confidence.RESOLVED
    ):
        tests[nodeid] = CoveringTest(nodeid, relpath, depth, confidence, via)


def _inside(scope: Symbol | None, target: Symbol) -> bool:
    return scope is not None and (
        scope == target or scope.qualname.startswith(target.qualname + ".")
    )


def _is_fixture(symbol: Symbol) -> bool:
    return any("fixture" in decorator for decorator in symbol.decorators)


def _fixture_users(resolver: Resolver, fixture: Symbol) -> list[Symbol]:
    """Tests that ask for a fixture by naming it as a parameter, which is a use
    no reference search can see. Fixtures that ask for it are not followed."""
    users = []
    for symbol in resolver.table.symbols:
        if not is_test(symbol):
            continue
        node = resolver.node_for(symbol)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            argument.arg == fixture.name
            for argument in (*node.args.posonlyargs, *node.args.args)
        ):
            users.append(symbol)
    return users


def _imported_modules(resolver: Resolver, relpath: str) -> set[str]:
    tree = resolver.tree(relpath)
    info = resolver.table.module_at(relpath)
    if tree is None or info is None:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(_nearest_module(resolver, alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = absolute_module(info, node.module, node.level)
            if base is None:
                continue
            for alias in node.names:
                # `from pkg import mod` imports a module; `from pkg import name`
                # imports from one.
                candidate = f"{base}.{alias.name}"
                if resolver.table.module(candidate) is not None:
                    found.add(candidate)
                else:
                    found.add(_nearest_module(resolver, base))
    found.discard("")
    found.discard(info.name)
    return found


def _nearest_module(resolver: Resolver, dotted: str) -> str:
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        name = ".".join(parts[:cut])
        if resolver.table.module(name) is not None:
            return name
    return ""

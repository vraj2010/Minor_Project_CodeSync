"""Choosing what the model sees when it rewrites a definition.

A project does not fit in a prompt, so every piece of context is a section
with a rank. The definition and its contract are always sent; the rest is added
in rank order while it fits the token budget, and whatever is left out is
recorded so a run can be explained afterwards."""

import ast
import copy
import math
import textwrap
from dataclasses import dataclass
from enum import Enum

from .references import (
    Callee,
    Confidence,
    Reference,
    UsageKind,
    find_callees,
    find_references,
)
from .resolve import Resolver
from .symbols import Symbol, SymbolKind

DEFAULT_GOAL = "Improve readability and robustness without changing behaviour."
DEFAULT_BUDGET = 6000

_USAGE_LIST_LIMIT = 30
_LEADING_CALLERS = 3
_SNIPPET_MAX_LINES = 15
_SNIPPET_RADIUS = 3


class ContextMode(str, Enum):
    BASELINE = "baseline"
    PROJECT = "project"


class SectionKind(str, Enum):
    TARGET = "target"
    CONTRACT = "contract"
    CLASS = "class"
    MODULE = "module"
    CALLEES = "callees"
    CALLERS = "callers"


@dataclass(frozen=True)
class Section:
    kind: SectionKind
    label: str
    body: str
    rank: int
    required: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.body)


@dataclass(frozen=True)
class Context:
    target: Symbol
    mode: ContextMode
    goal: str
    budget: int
    sections: tuple[Section, ...]
    omitted: tuple[Section, ...]
    references: tuple[Reference, ...]
    callees: tuple[Callee, ...]

    @property
    def tokens(self) -> int:
        return sum(section.tokens for section in self.sections)

    def summary(self) -> dict:
        return {
            "mode": self.mode.value,
            "budget": self.budget,
            "tokens": self.tokens,
            "included": [_describe(section) for section in self.sections],
            "omitted": [_describe(section) for section in self.omitted],
            "references": len(self.references),
            "callees": len(self.callees),
        }


def estimate_tokens(text: str) -> int:
    """About three characters per token, which errs on the high side for
    code, so a budget is rarely overrun."""
    return math.ceil(len(text) / 3)


def build_context(
    resolver: Resolver,
    target: Symbol,
    *,
    goal: str = DEFAULT_GOAL,
    mode: ContextMode = ContextMode.PROJECT,
    budget: int = DEFAULT_BUDGET,
) -> Context:
    candidates = [_target_section(resolver, target)]
    references: list[Reference] = []
    callees: list[Callee] = []

    if mode is ContextMode.PROJECT:
        references = find_references(resolver, target)
        callees = find_callees(resolver, target)
        candidates.append(_contract_section(resolver, target, references))
        candidates.extend(_class_sections(resolver, target))
        candidates.extend(_module_sections(resolver, target))
        candidates.extend(_callee_sections(resolver, callees))
        candidates.extend(_caller_sections(resolver, target, references))

    included, omitted = _fit(candidates, budget)
    return Context(
        target=target,
        mode=mode,
        goal=goal,
        budget=budget,
        sections=tuple(included),
        omitted=tuple(omitted),
        references=tuple(references),
        callees=tuple(callees),
    )


def definition_source(resolver: Resolver, symbol: Symbol) -> str:
    lines = resolver.source(symbol.relpath).split("\n")
    block = "\n".join(lines[symbol.start_lineno - 1 : symbol.position.end_lineno])
    return textwrap.dedent(block).strip("\n")


def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.unparse(_stub(node))


def outline(node: ast.ClassDef, *, all_methods: bool) -> str:
    """The class with method bodies replaced by `...`. Without `all_methods`
    only the constructor is kept, which is what a caller of the class needs."""
    body: list[ast.stmt] = list(_docstring_summary(node))
    for statement in node.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            body.append(statement)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            all_methods or statement.name in ("__init__", "__post_init__")
        ):
            body.append(_stub(statement))

    clone = copy.copy(node)
    clone.body = body or [ast.Expr(ast.Constant(...))]
    return ast.unparse(clone)


def instance_attributes(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            targets = child.targets
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr not in names
            ):
                names.append(target.attr)
    return names


def _stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
    clone = copy.copy(node)
    clone.body = [*_docstring_summary(node), ast.Expr(ast.Constant(...))]
    return clone


def _docstring_summary(node: ast.AST) -> list[ast.stmt]:
    docstring = ast.get_docstring(node)
    if not docstring:
        return []
    return [ast.Expr(ast.Constant(docstring.strip().splitlines()[0]))]


def _target_section(resolver: Resolver, target: Symbol) -> Section:
    span = f"lines {target.start_lineno}-{target.position.end_lineno}"
    body = (
        f"`{target.qualname}` in {target.relpath}, {span}:\n\n"
        f"```python\n{definition_source(resolver, target)}\n```"
    )
    return Section(SectionKind.TARGET, target.qualname, body, rank=0, required=True)


def _contract_section(
    resolver: Resolver, target: Symbol, references: list[Reference]
) -> Section:
    node = resolver.node_for(target)
    usages = [ref for ref in references if not _inside(ref, target)]
    lines = [
        "Other code depends on this signature:",
        "",
        "```python",
        signature(node) if node is not None else target.name,
        "```",
        "",
    ]

    if usages:
        noun = "place" if len(usages) == 1 else "places"
        lines.append(f"It is used in {len(usages)} {noun}:")
        for ref in usages[:_USAGE_LIST_LIMIT]:
            note = (
                "  (matched by name only)"
                if ref.confidence is Confidence.NAME_ONLY
                else ""
            )
            lines.append(f"- {ref.location}: {ref.summary}{note}")
        if len(usages) > _USAGE_LIST_LIMIT:
            lines.append(f"- and {len(usages) - _USAGE_LIST_LIMIT} more")
    else:
        lines.append(
            "Nothing else in this project uses it, but code outside the project "
            "may, so the signature still has to stay as it is."
        )

    return Section(
        SectionKind.CONTRACT, "contract", "\n".join(lines), rank=1, required=True
    )


def _class_sections(resolver: Resolver, target: Symbol) -> list[Section]:
    if target.kind is not SymbolKind.METHOD:
        return []
    cls = resolver.enclosing_class(target)
    node = resolver.node_for(cls) if cls is not None else None
    if not isinstance(node, ast.ClassDef):
        return []

    body = (
        f"`{target.name}` is a method of `{cls.qualname}`:\n\n"
        f"```python\n{outline(node, all_methods=True)}\n```"
    )
    attributes = instance_attributes(node)
    if attributes:
        body += "\n\nInstance attributes set in the class: " + ", ".join(attributes)
    return [Section(SectionKind.CLASS, cls.qualname, body, rank=10)]


def _module_sections(resolver: Resolver, target: Symbol) -> list[Section]:
    tree = resolver.tree(target.relpath)
    if tree is None:
        return []
    source = resolver.source(target.relpath)

    imports = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    ancestors = resolver.table.ancestors(target)
    outermost = ancestors[0] if ancestors else target
    neighbours = [
        symbol.name
        for symbol in resolver.table.symbols_in(target.module)
        if symbol.parent is None
        and symbol.relpath == target.relpath
        and symbol != outermost
    ]

    parts = []
    if imports:
        parts.append("```python\n" + "\n".join(imports) + "\n```")
    if neighbours:
        parts.append("Also defined in this module: " + ", ".join(neighbours))
    if not parts:
        return []

    body = f"Module `{target.module}` ({target.relpath}):\n\n" + "\n\n".join(parts)
    return [Section(SectionKind.MODULE, target.module, body, rank=20)]


def _callee_sections(resolver: Resolver, callees: list[Callee]) -> list[Section]:
    sections = []
    for position, callee in enumerate(callees):
        symbol = callee.symbol
        node = resolver.node_for(symbol)
        if node is None:
            continue
        if isinstance(node, ast.ClassDef):
            code = outline(node, all_methods=False)
        else:
            code = signature(node)
        times = "once" if callee.count == 1 else f"{callee.count} times"
        body = (
            f"`{symbol.qualname}` ({symbol.relpath}:{symbol.position.lineno}), "
            f"called {times}:\n\n```python\n{code}\n```"
        )
        sections.append(
            Section(SectionKind.CALLEES, symbol.qualname, body, rank=40 + position)
        )
    return sections


def _caller_sections(
    resolver: Resolver, target: Symbol, references: list[Reference]
) -> list[Section]:
    usages = [
        ref
        for ref in references
        if ref.kind is not UsageKind.IMPORT and not _inside(ref, target)
    ]
    resolved = [ref for ref in usages if ref.confidence is Confidence.RESOLVED]
    guessed = [ref for ref in usages if ref.confidence is Confidence.NAME_ONLY]

    sections: list[Section] = []
    shown: set[tuple[str, int]] = set()
    for ref in [*_interleave_by_file(resolved), *_interleave_by_file(guessed)]:
        key, label, code = _snippet(resolver, ref)
        if key in shown:
            continue
        shown.add(key)
        if ref.confidence is Confidence.NAME_ONLY:
            label += " (matched by name only)"

        position = len(sections)
        rank = 30 + position if position < _LEADING_CALLERS else 60 + position
        body = f"{label}:\n\n```python\n{code}\n```"
        sections.append(Section(SectionKind.CALLERS, ref.location, body, rank=rank))
    return sections


def _snippet(resolver: Resolver, ref: Reference) -> tuple[tuple[str, int], str, str]:
    """The code around a reference: the whole enclosing function when it is
    short, otherwise a few lines either side."""
    scope = ref.scope
    if (
        scope is not None
        and scope.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
        and scope.line_span < _SNIPPET_MAX_LINES
    ):
        label = (
            f"{scope.relpath}:{scope.start_lineno}-{scope.position.end_lineno}, "
            f"`{scope.qualname}`"
        )
        return (
            (scope.relpath, scope.start_lineno),
            label,
            definition_source(resolver, scope),
        )

    lines = resolver.source(ref.relpath).split("\n")
    first = max(ref.lineno - _SNIPPET_RADIUS, 1)
    last = min(ref.end_lineno + _SNIPPET_RADIUS, len(lines))
    code = textwrap.dedent("\n".join(lines[first - 1 : last])).strip("\n")
    label = f"{ref.relpath}:{first}-{last}"
    if scope is not None:
        label += f", in `{scope.qualname}`"
    return (ref.relpath, first), label, code


def _interleave_by_file(references: list[Reference]) -> list[Reference]:
    """Order references so each file gets one before any file gets two, which
    keeps a budget from being spent on a single heavy caller."""
    queues: dict[str, list[Reference]] = {}
    for ref in references:
        queues.setdefault(ref.relpath, []).append(ref)

    ordered: list[Reference] = []
    pending = list(queues.values())
    while pending:
        for queue in pending:
            ordered.append(queue.pop(0))
        pending = [queue for queue in pending if queue]
    return ordered


def _inside(ref: Reference, target: Symbol) -> bool:
    scope = ref.scope
    return scope is not None and (
        scope == target or scope.qualname.startswith(target.qualname + ".")
    )


def _fit(candidates: list[Section], budget: int) -> tuple[list[Section], list[Section]]:
    included: list[Section] = []
    omitted: list[Section] = []
    remaining = budget
    for section in sorted(candidates, key=lambda item: (not item.required, item.rank)):
        if section.required or section.tokens <= remaining:
            included.append(section)
            remaining -= section.tokens
        else:
            omitted.append(section)
    return included, omitted


def _describe(section: Section) -> str:
    return f"{section.kind.value}: {section.label}"

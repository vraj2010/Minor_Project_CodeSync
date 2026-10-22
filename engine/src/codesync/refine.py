"""One refinement: build the context, ask the model, splice in the reply.

Nothing here writes to the project. The result carries the patched text and
a diff; applying it safely is the job of the checks that come after."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .context import DEFAULT_BUDGET, DEFAULT_GOAL, Context, ContextMode, build_context
from .index import ProjectIndex
from .llm import Completion, ModelClient, ProviderError
from .parsing import read_exact_source
from .prompts import build_messages
from .resolve import Resolver
from .response import ResponseError, Rewrite, parse_rewrite
from .splice import Patch, SpliceError, splice
from .symbols import Symbol, SymbolKind


class Outcome(str, Enum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    INVALID_REPLY = "invalid-reply"
    SPLICE_FAILED = "splice-failed"
    PROVIDER_FAILED = "provider-failed"

    @property
    def has_patch(self) -> bool:
        return self in (Outcome.CHANGED, Outcome.UNCHANGED)


@dataclass(frozen=True)
class RefineResult:
    context: Context
    messages: list[dict[str, str]]
    outcome: Outcome
    completion: Completion | None = None
    rewrite: Rewrite | None = None
    patch: Patch | None = None
    error: str | None = None
    reason: str | None = None

    @property
    def target(self) -> Symbol:
        return self.context.target

    def to_record(self) -> dict:
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "target": self.target.qualname,
            "path": self.target.relpath,
            "goal": self.context.goal,
            "context": self.context.summary(),
            "outcome": self.outcome.value,
            "error": self.error,
            "reason": self.reason,
        }
        if self.completion is not None:
            record.update(
                model=self.completion.model,
                latency=round(self.completion.latency, 3),
                prompt_tokens=self.completion.prompt_tokens,
                completion_tokens=self.completion.completion_tokens,
                finish_reason=self.completion.finish_reason,
            )
        if self.rewrite is not None:
            record["renamed"] = self.rewrite.name != self.target.name
        if self.patch is not None:
            added, removed = self.patch.line_counts()
            record.update(
                added_lines=added,
                removed_lines=removed,
                added_imports=list(self.patch.added_imports),
                diff=self.patch.diff(),
            )
        record["messages"] = self.messages
        if self.completion is not None:
            record["reply"] = self.completion.text
        return record


def refine(
    index: ProjectIndex,
    target: Symbol,
    client: ModelClient,
    *,
    goal: str = DEFAULT_GOAL,
    mode: ContextMode = ContextMode.PROJECT,
    budget: int = DEFAULT_BUDGET,
    resolver: Resolver | None = None,
) -> RefineResult:
    resolver = resolver or Resolver(index)
    context = build_context(resolver, target, goal=goal, mode=mode, budget=budget)
    messages = build_messages(context)

    try:
        completion = client.complete(messages)
    except ProviderError as error:
        return RefineResult(
            context, messages, Outcome.PROVIDER_FAILED, error=str(error)
        )

    owner = (
        resolver.enclosing_class(target) if target.kind is SymbolKind.METHOD else None
    )
    try:
        rewrite = parse_rewrite(
            completion.text, name=target.name, owner=owner.name if owner else None
        )
    except ResponseError as error:
        return RefineResult(
            context,
            messages,
            Outcome.INVALID_REPLY,
            completion,
            error=str(error),
            reason=error.reason,
        )

    try:
        source = read_exact_source(index.root / target.relpath)
        patch = splice(source, target, rewrite)
    except (OSError, ValueError, SpliceError) as error:
        return RefineResult(
            context,
            messages,
            Outcome.SPLICE_FAILED,
            completion,
            rewrite,
            error=str(error),
        )

    outcome = Outcome.CHANGED if patch.changed else Outcome.UNCHANGED
    return RefineResult(context, messages, outcome, completion, rewrite, patch)


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

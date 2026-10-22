"""Describing every variable of a project with a hosted model.

Static analysis finds the variables and where they are used; the model only
explains them. The work is planned first, as a list of requests that can be
counted and costed, and only then run, concurrently and through a cache so
that code which has not changed is never sent twice."""

import ast
import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .context import definition_source, outline
from .llm import ModelClient, ProviderError
from .resolve import Resolver
from .symbols import Symbol, SymbolKind
from .variables import Access, AccessMode, Variable, VariableKind, VariableTable
from .varprompts import (
    AnnotationError,
    Described,
    Evidence,
    Scope,
    build_messages,
    parse_reply,
)

_SHARED_KINDS = (VariableKind.MODULE, VariableKind.CLASS, VariableKind.INSTANCE)
_RECEIVER_PURPOSE = {
    False: "The instance the method is called on.",
    True: "The class the method is called on.",
}
_TRUNCATED = "# ... truncated"

Progress = Callable[[int, int], None]


class AnnotationStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Annotation:
    variable: str
    status: AnnotationStatus
    type: str | None = None
    purpose: str | None = None
    role: str | None = None
    confidence: str | None = None
    model: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "type": self.type,
            "purpose": self.purpose,
            "role": self.role,
            "confidence": self.confidence,
            "model": self.model,
            "error": self.error,
        }


@dataclass(frozen=True)
class AnnotationOptions:
    kinds: frozenset[VariableKind] = frozenset(VariableKind)
    max_requests: int | None = None
    max_variables: int = 20
    max_chars: int = 12000
    source_limit: int = 5000
    samples: int = 3


@dataclass(frozen=True)
class PlannedRequest:
    scopes: tuple[Scope, ...]
    variables: tuple[Variable, ...]
    messages: tuple[dict[str, str], ...]

    @property
    def names(self) -> list[str]:
        return [variable.name for variable in self.variables]

    @property
    def estimated_tokens(self) -> int:
        return sum(len(message["content"]) for message in self.messages) // 3


@dataclass(frozen=True)
class Plan:
    requests: tuple[PlannedRequest, ...]
    static: tuple[Annotation, ...] = ()
    skipped: tuple[Annotation, ...] = ()

    @property
    def described(self) -> int:
        return sum(len(request.variables) for request in self.requests)

    @property
    def estimated_tokens(self) -> int:
        return sum(request.estimated_tokens for request in self.requests)

    def summary(self) -> dict:
        return {
            "requests": len(self.requests),
            "variables": self.described,
            "estimated_prompt_tokens": self.estimated_tokens,
            "static": len(self.static),
            "skipped": len(self.skipped),
        }


@dataclass
class AnnotationUsage:
    requests: int = 0
    cached: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AnnotationResult:
    annotations: dict[str, Annotation]
    usage: AnnotationUsage
    stopped: bool = False


@dataclass(frozen=True)
class _Piece:
    scope: Scope
    variables: tuple[Variable, ...]


class AnnotationCache:
    """Model answers kept by the content of the request that produced them,
    in memory and, when given a path, in a file of JSON lines."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    self._entries[record["key"]] = record["entry"]
                except (ValueError, KeyError, TypeError):
                    continue

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            self._entries[key] = entry
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"key": key, "entry": entry}) + "\n")


def cache_key(model: str, messages: Iterable[dict[str, str]]) -> str:
    payload = json.dumps([model, list(messages)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_annotations(
    resolver: Resolver, table: VariableTable, options: AnnotationOptions | None = None
) -> Plan:
    options = options or AnnotationOptions()
    static: list[Annotation] = []
    skipped: list[Annotation] = []
    groups: dict[str, list[Variable]] = {}

    for variable in table.variables:
        if variable.receiver:
            static.append(_receiver(variable))
        elif variable.kind not in options.kinds:
            skipped.append(_skipped(variable, "its kind was not selected"))
        else:
            groups.setdefault(variable.container, []).append(variable)

    lines = _Lines(resolver)
    pieces = []
    for container in sorted(
        groups, key=lambda name: _priority(resolver, name, groups[name])
    ):
        for chunk in _chunks(groups[container], options.max_variables):
            pieces.append(_piece(resolver, table, lines, container, chunk, options))

    requests = _bundle(pieces, options)
    if options.max_requests is not None and len(requests) > options.max_requests:
        for request in requests[options.max_requests :]:
            skipped.extend(
                _skipped(item, "the request budget was reached")
                for item in request.variables
            )
        requests = requests[: options.max_requests]
    return Plan(tuple(requests), tuple(static), tuple(skipped))


def run_annotations(
    plan: Plan,
    client: ModelClient,
    *,
    cache: AnnotationCache | None = None,
    workers: int = 4,
    progress: Progress | None = None,
    should_stop: Callable[[], bool] | None = None,
    failure_limit: int = 5,
) -> AnnotationResult:
    annotations = {item.variable: item for item in (*plan.static, *plan.skipped)}
    usage = AnnotationUsage()
    stopped = False
    if not plan.requests:
        return AnnotationResult(annotations, usage)

    breaker = _Breaker(failure_limit)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(_execute, request, client, cache, breaker, should_stop)
            for request in plan.requests
        ]
        for done, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            stopped = stopped or outcome.stopped
            _tally(usage, outcome)
            annotations.update(_annotations(outcome))
            if progress is not None:
                progress(done, len(futures))
    return AnnotationResult(annotations, usage, stopped)


@dataclass
class _Outcome:
    request: PlannedRequest
    answers: dict[int, Described] | None = None
    model: str | None = None
    error: str | None = None
    cached: bool = False
    stopped: bool = False
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _Breaker:
    """Stops a run that keeps failing at the provider, so a wrong key or an
    exhausted quota costs a handful of requests rather than all of them."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.reason: str | None = None
        self._streak = 0
        self._lock = threading.Lock()

    @property
    def tripped(self) -> bool:
        return self.reason is not None

    def succeeded(self) -> None:
        with self._lock:
            self._streak = 0

    def failed(self, message: str) -> None:
        with self._lock:
            self._streak += 1
            if self._streak >= self.limit and self.reason is None:
                self.reason = (
                    f"stopped after {self._streak} provider errors in a row: {message}"
                )


def _execute(
    request: PlannedRequest,
    client: ModelClient,
    cache: AnnotationCache | None,
    breaker: _Breaker,
    should_stop: Callable[[], bool] | None,
) -> _Outcome:
    if should_stop is not None and should_stop():
        return _Outcome(request, error="the run was stopped", stopped=True)
    if breaker.tripped:
        return _Outcome(request, error=breaker.reason, stopped=True)

    messages = list(request.messages)
    key = cache_key(client.model, messages)
    hit = cache.get(key) if cache is not None else None
    if hit is not None:
        answers = {
            int(number): Described(**item) for number, item in hit["answers"].items()
        }
        return _Outcome(request, answers, hit.get("model"), cached=True)

    outcome = _Outcome(request, model=client.model)
    try:
        answers = _ask(client, messages, request.names, outcome)
    except ProviderError as error:
        breaker.failed(str(error))
        outcome.error = str(error)
        return outcome
    except AnnotationError as error:
        outcome.error = str(error)
        return outcome
    except Exception as error:
        outcome.error = f"unexpected {type(error).__name__}: {error}"
        return outcome

    breaker.succeeded()
    outcome.answers = answers
    if cache is not None:
        entry = {
            "model": outcome.model,
            "answers": {str(number): asdict(item) for number, item in answers.items()},
        }
        cache.put(key, entry)
    return outcome


def _ask(
    client: ModelClient,
    messages: list[dict[str, str]],
    names: list[str],
    outcome: _Outcome,
) -> dict[int, Described]:
    completion = client.complete(messages)
    _count(outcome, completion)
    try:
        return parse_reply(completion.text, names)
    except AnnotationError as error:
        correction = (
            f"That reply was not usable ({error}). "
            "Reply again with only the JSON object."
        )
        retry = [
            *messages,
            {"role": "assistant", "content": completion.text},
            {"role": "user", "content": correction},
        ]

    completion = client.complete(retry)
    _count(outcome, completion)
    return parse_reply(completion.text, names)


def _count(outcome: _Outcome, completion) -> None:
    outcome.calls += 1
    outcome.model = completion.model or outcome.model
    outcome.prompt_tokens += completion.prompt_tokens or 0
    outcome.completion_tokens += completion.completion_tokens or 0


def _tally(usage: AnnotationUsage, outcome: _Outcome) -> None:
    usage.requests += outcome.calls
    usage.prompt_tokens += outcome.prompt_tokens
    usage.completion_tokens += outcome.completion_tokens
    if outcome.cached:
        usage.cached += 1
    elif outcome.answers is None and not outcome.stopped:
        usage.failed += 1


def _annotations(outcome: _Outcome) -> dict[str, Annotation]:
    found: dict[str, Annotation] = {}
    for number, variable in enumerate(outcome.request.variables, start=1):
        described = outcome.answers.get(number) if outcome.answers is not None else None
        if described is not None:
            found[variable.qualname] = Annotation(
                variable.qualname,
                AnnotationStatus.OK,
                described.type,
                described.purpose,
                described.role,
                described.confidence,
                outcome.model,
            )
            continue

        if outcome.answers is not None:
            status, error = AnnotationStatus.MISSING, "the model did not describe it"
        elif outcome.stopped:
            status, error = AnnotationStatus.SKIPPED, outcome.error
        else:
            status, error = AnnotationStatus.FAILED, outcome.error
        found[variable.qualname] = Annotation(
            variable.qualname, status, model=outcome.model, error=error
        )
    return found


def _receiver(variable: Variable) -> Annotation:
    on_class = (variable.static_type or "").startswith("type[")
    return Annotation(
        variable.qualname,
        AnnotationStatus.OK,
        variable.static_type,
        _RECEIVER_PURPOSE[on_class],
        "receiver",
        "high",
        "static",
    )


def _skipped(variable: Variable, reason: str) -> Annotation:
    return Annotation(
        variable.qualname, AnnotationStatus.SKIPPED, error=f"skipped: {reason}"
    )


def _priority(resolver: Resolver, container: str, variables: list[Variable]) -> tuple:
    symbol = resolver.table.lookup(container)
    rank = {None: 0, SymbolKind.CLASS: 1}.get(symbol.kind if symbol else None, 2)
    first = variables[0]
    return rank, first.relpath, first.lineno


def _chunks(variables: list[Variable], size: int) -> Iterable[list[Variable]]:
    for start in range(0, len(variables), size):
        yield variables[start : start + size]


def _piece(
    resolver: Resolver,
    table: VariableTable,
    lines: "_Lines",
    container: str,
    variables: list[Variable],
    options: AnnotationOptions,
) -> _Piece:
    symbol = resolver.table.lookup(container)
    label, code = _container_view(resolver, container, symbol, variables[0], options)
    items = tuple(_evidence(table, lines, variable, options) for variable in variables)
    return _Piece(Scope(label, code, items), tuple(variables))


def _container_view(
    resolver: Resolver,
    container: str,
    symbol: Symbol | None,
    first: Variable,
    options: AnnotationOptions,
) -> tuple[str, str | None]:
    if symbol is None:
        return f"module `{container}` ({first.relpath})", _imports(
            resolver, first.relpath
        )

    label = f"{symbol.kind.value} `{symbol.qualname}` ({symbol.location})"
    if symbol.kind is SymbolKind.CLASS:
        node = resolver.node_for(symbol)
        code = (
            outline(node, all_methods=True) if isinstance(node, ast.ClassDef) else None
        )
    else:
        code = definition_source(resolver, symbol)
    return label, _truncate(code, options.source_limit)


def _imports(resolver: Resolver, relpath: str) -> str | None:
    tree = resolver.tree(relpath)
    if tree is None:
        return None
    statements = [
        ast.unparse(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    return "\n".join(statements[:20]) or None


def _truncate(code: str | None, limit: int) -> str | None:
    if code is None or len(code) <= limit:
        return code
    cut = code[:limit].rsplit("\n", 1)[0]
    return f"{cut}\n{_TRUNCATED}"


def _evidence(
    table: VariableTable,
    lines: "_Lines",
    variable: Variable,
    options: AnnotationOptions,
) -> Evidence:
    accesses = table.accesses(variable.qualname)
    usage = table.usage(variable.qualname)
    assigned = _distinct(
        item.value for item in accesses if item.mode is AccessMode.WRITE and item.value
    )[:3]
    samples: tuple[str, ...] = ()
    if variable.kind in _SHARED_KINDS:
        picked = _pick_samples(accesses, variable, options.samples)
        samples = tuple(lines.sample(item) for item in picked)
    return Evidence(
        variable, assigned, samples, usage.reads, usage.writes, len(usage.files)
    )


def _pick_samples(
    accesses: list[Access], variable: Variable, count: int
) -> list[Access]:
    """Usages worth showing: other files first, reads before writes."""
    ranked = sorted(
        accesses,
        key=lambda item: (
            item.relpath == variable.relpath,
            item.mode is AccessMode.WRITE,
        ),
    )
    picked: list[Access] = []
    seen: set[tuple[str, int]] = {(variable.relpath, variable.lineno)}
    for item in ranked:
        place = (item.relpath, item.lineno)
        if place not in seen:
            seen.add(place)
            picked.append(item)
        if len(picked) == count:
            break
    return picked


def _distinct(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _bundle(pieces: list[_Piece], options: AnnotationOptions) -> list[PlannedRequest]:
    groups: list[list[_Piece]] = []
    current: list[_Piece] = []
    count = size = 0
    for piece in pieces:
        weight = len(piece.scope.code or "") + 250 * len(piece.variables)
        over = count + len(piece.variables) > options.max_variables
        if current and (over or size + weight > options.max_chars):
            groups.append(current)
            current, count, size = [], 0, 0
        current.append(piece)
        count += len(piece.variables)
        size += weight
    if current:
        groups.append(current)
    return [_request(group) for group in groups]


def _request(group: list[_Piece]) -> PlannedRequest:
    scopes = tuple(piece.scope for piece in group)
    variables = tuple(item for piece in group for item in piece.variables)
    return PlannedRequest(scopes, variables, tuple(build_messages(scopes)))


class _Lines:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._cache: dict[str, list[str]] = {}

    def sample(self, access: Access) -> str:
        if access.relpath not in self._cache:
            self._cache[access.relpath] = self._resolver.source(access.relpath).split(
                "\n"
            )
        lines = self._cache[access.relpath]
        text = lines[access.lineno - 1].strip() if access.lineno <= len(lines) else ""
        if len(text) > 100:
            text = text[:97] + "..."
        return f"{access.relpath}:{access.lineno} `{text}`"

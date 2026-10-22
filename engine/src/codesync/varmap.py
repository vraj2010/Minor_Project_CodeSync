"""The variable map: every variable of a project, how it is used, and what
the model says it is."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .annotate import (
    Annotation,
    AnnotationCache,
    AnnotationOptions,
    AnnotationStatus,
    AnnotationUsage,
    Progress,
    plan_annotations,
    run_annotations,
)
from .index import ProjectIndex
from .llm import ModelClient
from .resolve import Resolver
from .variables import Variable, VariableKind, VariableTable
from .varscan import build_variable_table


@dataclass
class VariableMap:
    root: Path
    table: VariableTable
    annotations: dict[str, Annotation] = field(default_factory=dict)
    usage: AnnotationUsage | None = None
    model: str | None = None

    def select(
        self,
        *,
        kind: str | None = None,
        module: str | None = None,
        container: str | None = None,
        name: str | None = None,
        query: str | None = None,
        cross_file: bool | None = None,
        annotated: bool | None = None,
        role: str | None = None,
    ) -> list[Variable]:
        needle = query.lower() if query else None
        chosen = []
        for variable in self.table.variables:
            if kind is not None and variable.kind.value != kind:
                continue
            if module is not None and variable.module != module:
                continue
            if container is not None and variable.container != container:
                continue
            if name is not None and variable.name != name:
                continue
            if needle is not None and needle not in variable.qualname.lower():
                continue
            if (
                cross_file is not None
                and self.table.usage(variable.qualname).cross_file != cross_file
            ):
                continue
            annotation = self.annotations.get(variable.qualname)
            if annotated is not None and (_is_described(annotation) != annotated):
                continue
            if role is not None and (annotation is None or annotation.role != role):
                continue
            chosen.append(variable)
        return chosen

    def entry(self, qualname: str, *, accesses: bool = False) -> dict | None:
        variable = self.table.lookup(qualname)
        if variable is None:
            return None

        entry = variable.to_dict()
        entry.update(self.table.usage(qualname).to_dict())
        annotation = self.annotations.get(qualname)
        entry["ai"] = annotation.to_dict() if annotation is not None else None
        if accesses:
            entry["accesses"] = [
                item.to_dict() for item in self.table.accesses(qualname)
            ]
        return entry

    def summary(self) -> dict:
        statuses = Counter(item.status.value for item in self.annotations.values())
        roles = Counter(
            item.role for item in self.annotations.values() if item.role is not None
        )
        return {
            "variables": len(self.table),
            "by_kind": self.table.counts(),
            "accesses": self.table.access_count(),
            "cross_file": sum(
                1
                for variable in self.table.variables
                if self.table.usage(variable.qualname).cross_file
            ),
            "annotations": {
                status.value: statuses.get(status.value, 0)
                for status in AnnotationStatus
            },
            "roles": dict(roles.most_common()),
            "model": self.model,
            "usage": self.usage.to_dict() if self.usage is not None else None,
        }

    def to_dict(self, *, accesses: bool = False) -> dict:
        return {
            "root": str(self.root),
            "summary": self.summary(),
            "variables": [
                self.entry(variable.qualname, accesses=accesses)
                for variable in self.table.variables
            ],
        }


def build_variable_map(
    index: ProjectIndex,
    *,
    resolver: Resolver | None = None,
    client: ModelClient | None = None,
    options: AnnotationOptions | None = None,
    cache: AnnotationCache | None = None,
    workers: int = 4,
    progress: Progress | None = None,
) -> VariableMap:
    """The static map of a project and, when a model client is given, its AI
    descriptions."""
    resolver = resolver or Resolver(index)
    table = build_variable_table(index, resolver)
    variable_map = VariableMap(index.root, table)
    if client is None:
        return variable_map

    plan = plan_annotations(resolver, table, options)
    result = run_annotations(
        plan, client, cache=cache, workers=workers, progress=progress
    )
    variable_map.annotations = result.annotations
    variable_map.usage = result.usage
    variable_map.model = client.model
    return variable_map


def parse_kinds(names: list[str] | None) -> frozenset[VariableKind]:
    if not names:
        return frozenset(VariableKind)
    try:
        return frozenset(VariableKind(name) for name in names)
    except ValueError as error:
        known = ", ".join(kind.value for kind in VariableKind)
        raise ValueError(f"{error}; the kinds are {known}") from None


def _is_described(annotation: Annotation | None) -> bool:
    return annotation is not None and annotation.status is AnnotationStatus.OK

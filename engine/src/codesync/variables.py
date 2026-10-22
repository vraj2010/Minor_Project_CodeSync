"""The variable model: every variable in a project and where it is used."""

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum


class VariableKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    INSTANCE = "instance"
    PARAMETER = "parameter"
    LOCAL = "local"


class AccessMode(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class Variable:
    qualname: str
    name: str
    kind: VariableKind
    scope: str
    container: str
    module: str
    relpath: str
    lineno: int
    col_offset: int
    annotation: str | None = None
    default: str | None = None
    parameter_kind: str | None = None
    static_type: str | None = None
    type_source: str | None = None
    overrides: str | None = None
    receiver: bool = False

    @property
    def location(self) -> str:
        return f"{self.relpath}:{self.lineno}"

    def to_dict(self) -> dict:
        return {
            "qualname": self.qualname,
            "name": self.name,
            "kind": self.kind.value,
            "scope": self.scope,
            "container": self.container,
            "module": self.module,
            "path": self.relpath,
            "lineno": self.lineno,
            "col_offset": self.col_offset,
            "annotation": self.annotation,
            "default": self.default,
            "parameter_kind": self.parameter_kind,
            "static_type": self.static_type,
            "type_source": self.type_source,
            "overrides": self.overrides,
            "receiver": self.receiver,
        }


@dataclass(frozen=True)
class Access:
    variable: str
    mode: AccessMode
    relpath: str
    lineno: int
    col_offset: int
    scope: str
    value: str | None = None

    @property
    def location(self) -> str:
        return f"{self.relpath}:{self.lineno}"

    def to_dict(self) -> dict:
        return {
            "variable": self.variable,
            "mode": self.mode.value,
            "path": self.relpath,
            "lineno": self.lineno,
            "col_offset": self.col_offset,
            "scope": self.scope,
            "value": self.value,
        }


@dataclass(frozen=True)
class Usage:
    reads: int
    writes: int
    files: tuple[str, ...]
    external_files: tuple[str, ...]

    @property
    def cross_file(self) -> bool:
        return bool(self.external_files)

    def to_dict(self) -> dict:
        return {
            "reads": self.reads,
            "writes": self.writes,
            "files": list(self.files),
            "external_files": list(self.external_files),
        }


class VariableTable:
    def __init__(self) -> None:
        self._variables: dict[str, Variable] = {}
        self._accesses: dict[str, list[Access]] = defaultdict(list)
        self._by_container: dict[str, list[Variable]] = defaultdict(list)
        self._by_module: dict[str, list[Variable]] = defaultdict(list)
        self._by_name: dict[str, list[Variable]] = defaultdict(list)

    def add(self, variable: Variable) -> None:
        self._variables[variable.qualname] = variable
        self._by_container[variable.container].append(variable)
        self._by_module[variable.module].append(variable)
        self._by_name[variable.name].append(variable)

    def add_access(self, access: Access) -> None:
        self._accesses[access.variable].append(access)

    def __len__(self) -> int:
        return len(self._variables)

    def __contains__(self, qualname: str) -> bool:
        return qualname in self._variables

    @property
    def variables(self) -> list[Variable]:
        return sorted(
            self._variables.values(),
            key=lambda item: (
                item.relpath,
                item.lineno,
                item.col_offset,
                item.qualname,
            ),
        )

    def lookup(self, qualname: str) -> Variable | None:
        return self._variables.get(qualname)

    def in_container(self, container: str) -> list[Variable]:
        return _ordered(self._by_container.get(container, ()))

    def in_module(self, module: str) -> list[Variable]:
        return _ordered(self._by_module.get(module, ()))

    def find(self, name: str) -> list[Variable]:
        return _ordered(self._by_name.get(name, ()))

    def containers(self) -> list[str]:
        return sorted(self._by_container)

    def accesses(self, qualname: str) -> list[Access]:
        return sorted(
            self._accesses.get(qualname, ()),
            key=lambda item: (item.relpath, item.lineno, item.col_offset),
        )

    def usage(self, qualname: str) -> Usage:
        variable = self._variables.get(qualname)
        accesses = self._accesses.get(qualname, ())
        reads = sum(1 for item in accesses if item.mode is AccessMode.READ)
        files = sorted({item.relpath for item in accesses})
        external = [path for path in files if variable and path != variable.relpath]
        return Usage(reads, len(accesses) - reads, tuple(files), tuple(external))

    def counts(self) -> dict[str, int]:
        counts = {kind.value: 0 for kind in VariableKind}
        for variable in self._variables.values():
            counts[variable.kind.value] += 1
        return counts

    def access_count(self) -> int:
        return sum(len(items) for items in self._accesses.values())


def _ordered(variables) -> list[Variable]:
    return sorted(
        variables, key=lambda item: (item.relpath, item.lineno, item.qualname)
    )

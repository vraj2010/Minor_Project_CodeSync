"""The symbol model and the table that holds it."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum


class SymbolKind(str, Enum):
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


@dataclass(frozen=True)
class Position:
    lineno: int
    end_lineno: int
    col_offset: int
    end_col_offset: int


@dataclass(frozen=True)
class Symbol:
    qualname: str
    name: str
    kind: SymbolKind
    module: str
    relpath: str
    position: Position
    parent: str | None = None
    decorators: tuple[str, ...] = ()
    decorator_lineno: int | None = None
    is_async: bool = False
    is_nested: bool = False

    @property
    def start_lineno(self) -> int:
        """First line of the definition, decorators included."""
        if self.decorator_lineno is None:
            return self.position.lineno
        return min(self.decorator_lineno, self.position.lineno)

    @property
    def line_span(self) -> int:
        return self.position.end_lineno - self.start_lineno

    @property
    def location(self) -> str:
        return f"{self.relpath}:{self.position.lineno}-{self.position.end_lineno}"

    def covers(self, lineno: int) -> bool:
        return self.start_lineno <= lineno <= self.position.end_lineno

    def to_dict(self) -> dict:
        data = {
            "qualname": self.qualname,
            "name": self.name,
            "kind": self.kind.value,
            "module": self.module,
            "path": self.relpath,
            "lineno": self.position.lineno,
            "end_lineno": self.position.end_lineno,
            "col_offset": self.position.col_offset,
            "end_col_offset": self.position.end_col_offset,
            "parent": self.parent,
        }
        if self.decorators:
            data["decorators"] = list(self.decorators)
            data["decorator_lineno"] = self.decorator_lineno
        if self.is_async:
            data["is_async"] = True
        if self.is_nested:
            data["is_nested"] = True
        return data


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    relpath: str
    is_package: bool
    line_count: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.relpath,
            "is_package": self.is_package,
            "line_count": self.line_count,
        }


class SymbolTable:
    def __init__(self) -> None:
        self._modules: dict[str, ModuleInfo] = {}
        self._modules_by_path: dict[str, ModuleInfo] = {}
        self._definitions: dict[str, list[Symbol]] = defaultdict(list)
        self._by_module: dict[str, list[Symbol]] = defaultdict(list)
        self._by_parent: dict[str | None, list[Symbol]] = defaultdict(list)
        self._by_name: dict[str, list[Symbol]] = defaultdict(list)

    def add_module(self, info: ModuleInfo) -> None:
        self._modules.setdefault(info.name, info)
        self._modules_by_path[info.relpath] = info

    def add_symbol(self, symbol: Symbol) -> None:
        self._definitions[symbol.qualname].append(symbol)
        self._by_module[symbol.module].append(symbol)
        self._by_parent[symbol.parent].append(symbol)
        self._by_name[symbol.name].append(symbol)

    @property
    def modules(self) -> list[ModuleInfo]:
        return sorted(self._modules.values(), key=lambda info: info.name)

    @property
    def files(self) -> list[ModuleInfo]:
        """Every indexed file, including one whose module name is shadowed."""
        return sorted(self._modules_by_path.values(), key=lambda info: info.relpath)

    @property
    def symbols(self) -> list[Symbol]:
        return sorted(
            (symbol for group in self._definitions.values() for symbol in group),
            key=lambda symbol: (symbol.relpath, symbol.position.lineno),
        )

    def module(self, name: str) -> ModuleInfo | None:
        return self._modules.get(name)

    def module_at(self, relpath: str) -> ModuleInfo | None:
        return self._modules_by_path.get(relpath)

    def lookup(self, qualname: str) -> Symbol | None:
        """The binding a qualified name resolves to. A name defined more than
        once (a conditional or `try`/`except` definition) resolves to the last
        one, which is what the interpreter would leave in place."""
        definitions = self._definitions.get(qualname)
        return definitions[-1] if definitions else None

    def lookup_all(self, qualname: str) -> list[Symbol]:
        return list(self._definitions.get(qualname, ()))

    def symbols_in(self, module: str) -> list[Symbol]:
        return sorted(
            self._by_module.get(module, ()), key=lambda symbol: symbol.position.lineno
        )

    def children_of(self, qualname: str | None) -> list[Symbol]:
        return sorted(
            self._by_parent.get(qualname, ()), key=lambda symbol: symbol.position.lineno
        )

    def find(self, name: str) -> list[Symbol]:
        return sorted(self._by_name.get(name, ()), key=lambda symbol: symbol.qualname)

    def enclosing(self, relpath: str, lineno: int) -> Symbol | None:
        """The innermost symbol whose definition spans `lineno`."""
        info = self._modules_by_path.get(relpath)
        if info is None:
            return None
        candidates = [
            symbol
            for symbol in self._by_module.get(info.name, ())
            if symbol.relpath == relpath and symbol.covers(lineno)
        ]
        if not candidates:
            return None
        return min(
            candidates, key=lambda symbol: (symbol.line_span, -symbol.start_lineno)
        )

    def ancestors(self, symbol: Symbol) -> list[Symbol]:
        """Enclosing symbols, outermost first."""
        chain: list[Symbol] = []
        current = symbol
        while current.parent is not None:
            found = self._parent_of(current)
            if found is None:
                break
            chain.append(found)
            current = found
        chain.reverse()
        return chain

    def _parent_of(self, symbol: Symbol) -> Symbol | None:
        """The definition a symbol sits inside, chosen from the same file so
        that a name defined in several places resolves to the right one."""
        enclosing = [
            candidate
            for candidate in self._definitions.get(symbol.parent, ())
            if candidate.relpath == symbol.relpath
            and candidate.covers(symbol.start_lineno)
        ]
        if enclosing:
            return min(enclosing, key=lambda candidate: candidate.line_span)
        return self.lookup(symbol.parent)

    def counts(self) -> dict[str, int]:
        counter = Counter(symbol.kind.value for symbol in self.symbols)
        return {kind.value: counter.get(kind.value, 0) for kind in SymbolKind}

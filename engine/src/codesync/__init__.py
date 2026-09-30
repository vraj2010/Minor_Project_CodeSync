"""Project-aware static analysis for safe code refinement."""

from .discovery import DEFAULT_EXCLUDES, SourceFile, discover, module_name
from .index import DuplicateModule, ProjectIndex, build_index
from .parsing import ParseFailure, SourceError, load_module
from .symbols import ModuleInfo, Position, Symbol, SymbolKind, SymbolTable

__all__ = [
    "DEFAULT_EXCLUDES",
    "DuplicateModule",
    "ModuleInfo",
    "ParseFailure",
    "Position",
    "ProjectIndex",
    "SourceError",
    "SourceFile",
    "Symbol",
    "SymbolKind",
    "SymbolTable",
    "build_index",
    "discover",
    "load_module",
    "module_name",
]

__version__ = "0.1.0"

"""Project-aware AI code refinement."""

from .context import Context, ContextMode, build_context
from .discovery import DEFAULT_EXCLUDES, SourceFile, discover, module_name
from .index import DuplicateModule, ProjectIndex, build_index
from .llm import ChatClient, Completion, ProviderError, create_client
from .parsing import ParseFailure, SourceError, load_module
from .references import Reference, find_callees, find_references
from .refine import Outcome, RefineResult, refine
from .resolve import Resolver
from .response import ResponseError, Rewrite, parse_rewrite
from .splice import Patch, SpliceError, splice
from .symbols import ModuleInfo, Position, Symbol, SymbolKind, SymbolTable
from .targets import TargetError, find_refinable, find_symbol

__all__ = [
    "DEFAULT_EXCLUDES",
    "ChatClient",
    "Completion",
    "Context",
    "ContextMode",
    "DuplicateModule",
    "ModuleInfo",
    "Outcome",
    "ParseFailure",
    "Patch",
    "Position",
    "ProjectIndex",
    "ProviderError",
    "Reference",
    "RefineResult",
    "Resolver",
    "ResponseError",
    "Rewrite",
    "SourceError",
    "SourceFile",
    "SpliceError",
    "Symbol",
    "SymbolKind",
    "SymbolTable",
    "TargetError",
    "build_context",
    "build_index",
    "create_client",
    "discover",
    "find_callees",
    "find_references",
    "find_refinable",
    "find_symbol",
    "load_module",
    "module_name",
    "parse_rewrite",
    "refine",
    "splice",
]

__version__ = "0.2.0"

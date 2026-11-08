"""Project-aware AI for Python repositories."""

from .annotate import Annotation, AnnotationCache, AnnotationOptions, plan_annotations
from .apply import ApplyError, apply_patch, undo
from .context import Context, ContextMode, build_context
from .contract import Contract, compare, snapshot
from .discovery import DEFAULT_EXCLUDES, SourceFile, discover, module_name
from .impact import Impact, analyze_impact, dependency_map, find_covering_tests
from .index import DuplicateModule, ProjectIndex, build_index
from .llm import ChatClient, Completion, ProviderError, create_client
from .parsing import ParseFailure, SourceError, load_module
from .references import Reference, find_callees, find_references
from .refine import Outcome, RefineResult, refine
from .resolve import Resolver
from .response import ResponseError, Rewrite, parse_rewrite
from .safe import Decision, SafeResult, refine_safely
from .splice import Patch, SpliceError, splice
from .symbols import ModuleInfo, Position, Symbol, SymbolKind, SymbolTable
from .targets import TargetError, find_refinable, find_symbol
from .variables import Access, AccessMode, Variable, VariableKind, VariableTable
from .varmap import VariableMap, build_variable_map
from .varscan import build_variable_table
from .verify import Report, Verdict, Verifier, VerifyOptions

__all__ = [
    "DEFAULT_EXCLUDES",
    "Access",
    "AccessMode",
    "Annotation",
    "AnnotationCache",
    "AnnotationOptions",
    "ApplyError",
    "ChatClient",
    "Completion",
    "Context",
    "ContextMode",
    "Contract",
    "Decision",
    "DuplicateModule",
    "Impact",
    "ModuleInfo",
    "Outcome",
    "ParseFailure",
    "Patch",
    "Position",
    "ProjectIndex",
    "ProviderError",
    "Reference",
    "RefineResult",
    "Report",
    "Resolver",
    "ResponseError",
    "Rewrite",
    "SafeResult",
    "SourceError",
    "SourceFile",
    "SpliceError",
    "Symbol",
    "SymbolKind",
    "SymbolTable",
    "TargetError",
    "Variable",
    "VariableKind",
    "VariableMap",
    "VariableTable",
    "Verdict",
    "Verifier",
    "VerifyOptions",
    "analyze_impact",
    "apply_patch",
    "build_context",
    "build_index",
    "build_variable_map",
    "build_variable_table",
    "compare",
    "create_client",
    "dependency_map",
    "discover",
    "find_callees",
    "find_covering_tests",
    "find_references",
    "find_refinable",
    "find_symbol",
    "load_module",
    "module_name",
    "parse_rewrite",
    "plan_annotations",
    "refine",
    "refine_safely",
    "snapshot",
    "splice",
    "undo",
]

__version__ = "0.3.0"

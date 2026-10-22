"""Arguments and helpers shared by the command line modules."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from .index import ProjectIndex, build_index
from .llm import DEFAULT_MODEL, MODEL_ENV, ChatClient, create_client


def add_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", help="path to the project")
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="index files that the project's .gitignore excludes",
    )


def add_model_arguments(
    parser: argparse.ArgumentParser, *, max_tokens: int = 2048, retries: int = 4
) -> None:
    parser.add_argument(
        "--model",
        help=f"Gemini model name (default: ${MODEL_ENV}, then {DEFAULT_MODEL})",
    )
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=max_tokens)
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for a reply"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=retries,
        help="times to retry when Gemini is busy or slow, waiting longer each time",
    )


def create_model_client(args: argparse.Namespace) -> ChatClient:
    return create_client(
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        retries=args.retries,
    )


def load_index(args: argparse.Namespace) -> ProjectIndex:
    return build_index(Path(args.root), use_gitignore=not args.no_gitignore)


def print_rows(rows: Sequence[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}  {value}")


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Rows in columns, each as wide as its longest cell. The last column is
    left unpadded so long text does not leave trailing spaces."""
    widths = [
        max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)
    ]
    for line in (headers, *rows):
        cells = [
            cell.ljust(width) for cell, width in zip(line[:-1], widths, strict=False)
        ]
        print("  ".join([*cells, line[-1]]))


def explain_error(message: str) -> str | None:
    """What to try, for the failures people actually run into."""
    text = message.lower()
    if "returned 404" in text and "model" in text:
        return (
            "that model is not available, perhaps retired: `codesync models` "
            "lists the current ones; then use --model, or set GEMINI_MODEL"
        )
    if "returned 401" in text or "api key" in text:
        return (
            "the key was rejected: check it was copied whole, has no spaces, "
            "and is a Gemini key (from aistudio.google.com/apikey)"
        )
    if "finish reason 'length'" in text:
        return (
            "the model ran out of tokens before it answered, which models that "
            "think do: raise --max-tokens"
        )
    if "returned 503" in text or "high demand" in text or "overloaded" in text:
        return (
            "Gemini is overloaded, which is usually brief: wait a minute "
            "and run it again, or try another model (`codesync models`)"
        )
    if "returned 403" in text:
        return (
            "access was denied: check the key is allowed to use the Gemini API "
            "and that the model is available to your account"
        )
    if "returned 429" in text:
        return "rate limited, or the quota is used up: wait, or lower --workers"
    if "could not reach" in text or "no answer from" in text:
        return "check the internet connection, and any proxy or firewall"
    return None

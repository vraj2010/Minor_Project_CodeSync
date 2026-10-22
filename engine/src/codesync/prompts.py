"""The messages sent to the model.

Both modes ask for the same reply format, so a baseline run and a
project-aware run differ only in what the model is told about the project."""

from .context import Context, ContextMode, SectionKind
from .symbols import SymbolKind

BASELINE_SYSTEM = (
    "You are an experienced Python developer. You rewrite one function at a "
    "time to meet the goal you are given."
)

PROJECT_SYSTEM = (
    BASELINE_SYSTEM + "\n\n"
    "The function belongs to a larger project and other code depends on it. "
    "A rewrite that changes how it is called or what it returns breaks that "
    "code, even if the function itself is better.\n\n"
    "Rules:\n"
    "- Keep the name, the parameters (their names, order, kinds and defaults) "
    "and the type of value returned.\n"
    "- Keep raising the exceptions that callers may rely on.\n"
    "- Keep the decorators.\n"
    "- Use only names that the module defines or imports, or add the import."
)

_HEADINGS = {
    SectionKind.TARGET: "Definition to rewrite",
    SectionKind.CONTRACT: "Contract",
    SectionKind.CLASS: "Enclosing class",
    SectionKind.MODULE: "Module",
    SectionKind.CALLEES: "Code it calls",
    SectionKind.CALLERS: "Code that uses it",
}


def build_messages(context: Context) -> list[dict[str, str]]:
    system = PROJECT_SYSTEM if context.mode is ContextMode.PROJECT else BASELINE_SYSTEM
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": render_request(context)},
    ]


def render_request(context: Context) -> str:
    parts = [f"Goal: {context.goal}"]
    for kind in SectionKind:
        sections = sorted(
            (section for section in context.sections if section.kind is kind),
            key=lambda section: section.rank,
        )
        if not sections:
            continue
        parts.append(f"## {_HEADINGS[kind]}")
        parts.extend(section.body for section in sections)
    parts.append(reply_instructions(context))
    return "\n\n".join(parts)


def reply_instructions(context: Context) -> str:
    name = context.target.name
    lines = [
        "## Reply",
        f"Reply with one ```python code block containing the complete rewritten "
        f"definition of `{name}`, decorators included.",
        "If it needs a new import, put the import statement at the top of the "
        "same block. Put no other code in the block.",
    ]
    if context.target.kind is SymbolKind.METHOD:
        lines.append("Write the method on its own, not inside its class.")
    return "\n".join(lines)

"""Task files: a list of refinements to run in one go, as the comparison
experiments do.

    {"tasks": [{"root": "projects/inventory",
                "target": "inventory.pricing.apply_discount",
                "goal": "Make the validation clearer."}]}

A root is relative to the task file, and a task without a goal uses the
default one."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    root: Path
    target: str
    goal: str | None = None


def load_tasks(path: Path) -> list[Task]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f"{path} should hold a list of tasks")

    tasks = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or "root" not in item or "target" not in item:
            raise ValueError(f"task {number} in {path} needs a root and a target")
        tasks.append(
            Task(
                root=(path.parent / item["root"]).resolve(),
                target=item["target"],
                goal=item.get("goal"),
            )
        )
    return tasks

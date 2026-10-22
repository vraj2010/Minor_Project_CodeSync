from pathlib import Path
from textwrap import dedent

import pytest

from codesync.index import build_index
from codesync.llm import Completion
from codesync.resolve import Resolver

FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedClient:
    """Stands in for a model: answers with the replies it was given, in order,
    and raises any exception found among them."""

    model = "scripted-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages):
        self.requests.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(
            text=reply,
            model=self.model,
            latency=0.25,
            prompt_tokens=400,
            completion_tokens=60,
            finish_reason="stop",
        )

    def close(self):
        pass


@pytest.fixture
def scripted():
    return ScriptedClient


@pytest.fixture(scope="session")
def sample_project() -> Path:
    return FIXTURES / "sample_project"


@pytest.fixture(scope="session")
def inventory_project() -> Path:
    return FIXTURES / "inventory"


@pytest.fixture(scope="session")
def inventory_index(inventory_project):
    return build_index(inventory_project)


@pytest.fixture
def resolver(inventory_index) -> Resolver:
    return Resolver(inventory_index)


@pytest.fixture
def write_project(tmp_path):
    def write(files: dict[str, str]) -> Path:
        for relpath, content in files.items():
            path = tmp_path / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dedent(content).lstrip(), encoding="utf-8")
        return tmp_path

    return write

import json
import re
import threading
import time
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


class AnnotatingClient:
    """Answers variable-description requests the way a model does: with a JSON
    object keyed by the numbers in the prompt. It can be told to leave
    variables out, to start with unusable replies, or to fail outright."""

    model = "annotator-1"

    def __init__(self, omit=(), garbled=0, fail=None, delay=0.0):
        self.omit = set(omit)
        self.garbled = garbled
        self.fail = fail
        self.delay = delay
        self.requests = []
        self._lock = threading.Lock()

    @property
    def calls(self):
        return len(self.requests)

    def complete(self, messages):
        with self._lock:
            self.requests.append(messages)
            garble = self.garbled > 0
            if garble:
                self.garbled -= 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail

        if garble:
            text = "I could not produce JSON for this."
        elif len(messages) < 2:
            text = "OK"
        else:
            found = re.findall(r"^(\d+)\. `([^`]+)`", messages[1]["content"], re.M)
            answers = {
                number: {
                    "type": "int",
                    "purpose": f"Holds the value of {name}.",
                    "role": "state",
                    "confidence": "high",
                }
                for number, name in found
                if name not in self.omit
            }
            text = "```json\n" + json.dumps(answers) + "\n```"
        return Completion(
            text=text,
            model=self.model,
            latency=0.01,
            prompt_tokens=100,
            completion_tokens=40,
            finish_reason="stop",
        )

    def list_models(self):
        if self.fail is not None:
            raise self.fail
        return ["alpha-flash", "alpha-pro", "beta-1"]

    def close(self):
        pass


@pytest.fixture
def scripted():
    return ScriptedClient


@pytest.fixture
def annotator():
    return AnnotatingClient


@pytest.fixture(scope="session")
def sample_project() -> Path:
    return FIXTURES / "sample_project"


@pytest.fixture(scope="session")
def inventory_project() -> Path:
    return FIXTURES / "inventory"


@pytest.fixture(scope="session")
def shop_project() -> Path:
    return FIXTURES / "shop"


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

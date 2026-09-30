from pathlib import Path
from textwrap import dedent

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def sample_project() -> Path:
    return FIXTURES / "sample_project"


@pytest.fixture
def write_project(tmp_path):
    def write(files: dict[str, str]) -> Path:
        for relpath, content in files.items():
            path = tmp_path / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dedent(content).lstrip(), encoding="utf-8")
        return tmp_path

    return write

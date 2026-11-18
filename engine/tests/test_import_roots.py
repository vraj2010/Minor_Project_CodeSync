"""A library uploaded as `repo/src/pkg/` must be tested from that copy.

Root cause of the "Not confirmed" demo: only the project root and `root/src`
were put on PYTHONPATH, so a library nested one folder deeper could not be
imported (ModuleNotFoundError in conftest), or, worse, an installed copy of the
same library was imported and the pass said nothing about the rewrite."""

import sys

import pytest

from codesync.runners import run_pytest
from codesync.sandbox import clean_environment, import_roots


def write(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


NESTED = {
    "repo/src/mylib/__init__.py": "",
    "repo/src/mylib/core.py": "def value():\n    return 'project'\n",
    "repo/tests/__init__.py": "",
    "repo/tests/test_core.py": "from mylib.core import value\n\n\n"
    "def test_value():\n    assert value() == 'project'\n",
}


def test_a_library_nested_below_the_root_is_an_import_root(tmp_path):
    write(tmp_path, NESTED)

    roots = import_roots(tmp_path)

    assert tmp_path.resolve() / "repo" / "src" in roots
    assert roots[-1] == tmp_path.resolve()
    assert str(tmp_path.resolve() / "repo" / "src") in clean_environment(
        tmp_path, {}
    )["PYTHONPATH"]


def test_a_flat_or_src_layout_keeps_its_import_roots(tmp_path):
    write(
        tmp_path,
        {"src/pkg/__init__.py": "", "other/__init__.py": "", "script.py": ""},
    )

    roots = import_roots(tmp_path)

    assert tmp_path.resolve() / "src" in roots
    assert tmp_path.resolve() in roots


@pytest.mark.slow
def test_nested_library_tests_import_the_project_copy_not_an_installed_one(tmp_path):
    """`colorsys` stands in for an installed copy of the library: the standard
    library has one, and the project's own must still win."""
    write(
        tmp_path,
        {
            "repo/src/colorsys/__init__.py": "MARK = 'project'\n",
            "repo/tests/__init__.py": "",
            "repo/tests/conftest.py": "import colorsys\n",
            "repo/tests/test_which.py": "import colorsys\n\n\n"
            "def test_project_copy():\n"
            "    assert colorsys.MARK == 'project'\n"
            "    assert str(colorsys.__file__).startswith(%r)\n" % str(tmp_path.resolve()),
        },
    )

    run = run_pytest(tmp_path, None, python=sys.executable, timeout=120)

    assert run.problem is None
    assert not run.failures
    assert run.total == 1

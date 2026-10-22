import shutil
import subprocess
from textwrap import dedent

import pytest

from codesync.index import build_index
from codesync.parsing import read_exact_source
from codesync.response import Rewrite
from codesync.splice import SpliceError, splice

PRICING = """\
\"\"\"Prices and discounts.\"\"\"

import math

TAX_RATE = 0.18


def price_with_tax(item, rate=TAX_RATE):
    return round(item.price * (1 + rate), 2)


class Basket:
    def __init__(self):
        self.items = []

    @property
    def total(self):
        return sum(item.price for item in self.items)
"""


def project(tmp_path, text=PRICING, name="pricing.py"):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return build_index(tmp_path), path


def rewrite(source, imports=(), name=None):
    source = dedent(source).strip("\n")
    return Rewrite(
        name=name or source.split("(")[0].split()[-1], source=source, imports=imports
    )


def test_a_function_is_replaced_and_nothing_else_moves(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.price_with_tax")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite(
            """
            def price_with_tax(item, rate=TAX_RATE):
                gross = item.price * (1 + rate)
                return round(gross, 2)
            """
        ),
    )

    assert patch.changed
    assert (
        "    gross = item.price * (1 + rate)\n    return round(gross, 2)\n"
        in patch.patched
    )
    assert (
        patch.patched.replace(
            "    gross = item.price * (1 + rate)\n    return round(gross, 2)\n",
            "    return round(item.price * (1 + rate), 2)\n",
        )
        == PRICING
    )


def test_a_method_is_indented_to_its_class(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.Basket.total")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite(
            """
            @property
            def total(self):
                return math.fsum(item.price for item in self.items)
            """
        ),
    )

    assert (
        "    @property\n"
        "    def total(self):\n"
        "        return math.fsum(item.price for item in self.items)\n"
    ) in patch.patched
    assert "return sum(item.price" not in patch.patched


def test_decorators_are_replaced_with_the_definition(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.Basket.total")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite("def total(self):\n    return 0\n"),
    )

    assert "@property" not in patch.patched


def test_windows_line_endings_are_kept(tmp_path):
    index, path = project(tmp_path, PRICING.replace("\n", "\r\n"))
    symbol = index.table.lookup("pricing.Basket.total")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite("@property\ndef total(self):\n    return 0\n"),
    )

    assert "\n" not in patch.patched.replace("\r\n", "")
    assert "        return 0\r\n" in patch.patched


def test_tab_indentation_is_followed(tmp_path):
    text = "class Basket:\n\tdef total(self):\n\t\treturn 1\n"
    index, path = project(tmp_path, text, name="basket.py")
    symbol = index.table.lookup("basket.Basket.total")

    patch = splice(
        read_exact_source(path), symbol, rewrite("def total(self):\n    return 2\n")
    )

    assert patch.patched == "class Basket:\n\tdef total(self):\n\t    return 2\n"


def test_a_new_import_goes_after_the_existing_ones(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.price_with_tax")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite(
            "def price_with_tax(item, rate=TAX_RATE):\n"
            "    return float(Decimal(item.price) * Decimal(1 + rate))\n",
            imports=("from decimal import Decimal", "import math"),
        ),
    )

    assert patch.added_imports == ("from decimal import Decimal",)
    assert patch.patched.startswith(
        '"""Prices and discounts."""\n\n'
        "import math\nfrom decimal import Decimal\n\nTAX_RATE"
    )


def test_an_import_already_covered_by_a_wider_one_is_skipped(tmp_path):
    text = "from os import path, sep\n\n\ndef join(a, b):\n    return a + sep + b\n"
    index, file = project(tmp_path, text, name="paths.py")

    patch = splice(
        read_exact_source(file),
        index.table.lookup("paths.join"),
        rewrite(
            "def join(a, b):\n    return path.join(a, b)\n",
            imports=("from os import path",),
        ),
    )

    assert patch.added_imports == ()


def test_without_imports_a_new_one_follows_the_docstring(tmp_path):
    text = '"""Tools."""\n\n\ndef double(x):\n    return x * 2\n'
    index, path = project(tmp_path, text, name="tools.py")

    patch = splice(
        read_exact_source(path),
        index.table.lookup("tools.double"),
        rewrite(
            "def double(x):\n    return operator.mul(x, 2)\n",
            imports=("import operator",),
        ),
    )

    assert patch.patched.startswith('"""Tools."""\nimport operator\n\n\ndef double')


def test_an_identical_rewrite_is_unchanged(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.price_with_tax")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite(
            "def price_with_tax(item, rate=TAX_RATE):\n"
            "    return round(item.price * (1 + rate), 2)\n"
        ),
    )

    assert not patch.changed
    assert patch.diff() == ""
    assert patch.line_counts() == (0, 0)


def test_the_diff_is_a_unified_diff_of_the_file(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.price_with_tax")

    patch = splice(
        read_exact_source(path),
        symbol,
        rewrite("def price_with_tax(item, rate=TAX_RATE):\n    return item.price\n"),
    )
    diff = patch.diff()

    assert diff.startswith("--- a/pricing.py\n+++ b/pricing.py\n@@ ")
    assert "-    return round(item.price * (1 + rate), 2)\n" in diff
    assert "+    return item.price\n" in diff
    assert patch.line_counts() == (1, 1)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_diff_applies_with_git(tmp_path):
    index, path = project(tmp_path)
    patch = splice(
        read_exact_source(path),
        index.table.lookup("pricing.Basket.total"),
        rewrite(
            "@property\ndef total(self):\n    return 0\n", imports=("import operator",)
        ),
    )
    (tmp_path / "change.patch").write_text(patch.diff(), encoding="utf-8", newline="")

    git = ["git", "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "apply", "change.patch"], cwd=tmp_path, check=True)

    assert path.read_bytes().decode("utf-8") == patch.patched


def test_a_missing_final_newline_is_marked_in_the_diff(tmp_path):
    index, path = project(tmp_path, "def last():\n    return 1", name="tail.py")

    patch = splice(
        read_exact_source(path),
        index.table.lookup("tail.last"),
        rewrite("def last():\n    return 2\n"),
    )

    assert (
        "-    return 1\n\\ No newline at end of file\n+    return 2\n" in patch.diff()
    )


def test_a_file_edited_since_indexing_is_refused(tmp_path):
    index, path = project(tmp_path)
    symbol = index.table.lookup("pricing.price_with_tax")
    path.write_text("TAX_RATE = 0.2\n" * 20, encoding="utf-8")

    with pytest.raises(SpliceError, match="no longer hold `price_with_tax`"):
        splice(
            read_exact_source(path),
            symbol,
            rewrite("def price_with_tax():\n    pass\n"),
        )

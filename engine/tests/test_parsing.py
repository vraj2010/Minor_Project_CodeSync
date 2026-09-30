import ast

import pytest

from codesync.parsing import SourceError, load_module, read_source


def test_reads_a_file_with_an_encoding_declaration(tmp_path):
    path = tmp_path / "legacy.py"
    path.write_bytes("# -*- coding: latin-1 -*-\nLABEL = 'café'\n".encode("latin-1"))

    assert "café" in read_source(path)


def test_strips_a_utf8_byte_order_mark(tmp_path):
    path = tmp_path / "bom.py"
    path.write_bytes(b"\xef\xbb\xbfLABEL = 1\n")

    assert read_source(path) == "LABEL = 1\n"


def test_returns_the_tree_and_the_text_it_was_parsed_from(tmp_path):
    path = tmp_path / "ok.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")

    tree, text = load_module(path, "ok.py")

    assert isinstance(tree, ast.Module)
    assert text == "VALUE = 1\n"


def test_a_syntax_error_carries_its_line_number(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def parse(:\n    return 1\n", encoding="utf-8")

    with pytest.raises(SourceError) as caught:
        load_module(path, "broken.py")

    failure = caught.value.failure
    assert failure.relpath == "broken.py"
    assert failure.reason == "syntax"
    assert failure.lineno == 1


def test_a_file_that_is_not_text_is_reported_not_raised(tmp_path):
    path = tmp_path / "binary.py"
    path.write_bytes(b"value = 1\x00\n")

    with pytest.raises(SourceError) as caught:
        load_module(path, "binary.py")

    assert caught.value.failure.reason == "decode"


def test_an_unknown_encoding_declaration_is_reported(tmp_path):
    path = tmp_path / "odd.py"
    path.write_bytes(b"# -*- coding: nonsense -*-\nVALUE = 1\n")

    with pytest.raises(SourceError) as caught:
        load_module(path, "odd.py")

    assert caught.value.failure.reason == "decode"


def test_an_unreadable_file_is_reported_as_a_read_failure(tmp_path):
    with pytest.raises(SourceError) as caught:
        load_module(tmp_path / "absent.py", "absent.py")

    assert caught.value.failure.reason == "read"


def test_failures_describe_themselves_for_the_report(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("class Record\n", encoding="utf-8")

    with pytest.raises(SourceError) as caught:
        load_module(path, "app/broken.py")

    assert caught.value.failure.describe().startswith("app/broken.py:1  syntax:")

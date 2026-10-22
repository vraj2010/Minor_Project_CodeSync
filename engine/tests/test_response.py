import pytest

from codesync.response import ResponseError, code_blocks, parse_rewrite


def test_a_single_block_is_the_rewrite():
    reply = (
        "Here you go:\n\n```python\ndef total(values):\n    return sum(values)\n```\n"
    )

    rewrite = parse_rewrite(reply, name="total")

    assert rewrite.name == "total"
    assert rewrite.source == "def total(values):\n    return sum(values)"
    assert rewrite.imports == ()
    assert rewrite.is_async is False


def test_decorators_are_part_of_the_definition():
    reply = "```python\n@cache\ndef total(values):\n    return sum(values)\n```"

    assert parse_rewrite(reply, name="total").source.startswith("@cache\ndef total")


def test_the_last_block_defining_the_name_wins():
    reply = (
        "Original:\n```python\ndef total(values):\n    return sum(values)\n```\n"
        "Improved:\n```python\ndef total(values):\n    return sum(values or [])\n```\n"
        "Usage:\n```python\nprint(total([1, 2]))\n```\n"
    )

    assert "values or []" in parse_rewrite(reply, name="total").source


def test_imports_at_the_top_of_the_block_are_kept_separately():
    reply = (
        "```python\nimport math\nfrom decimal import Decimal\n\n"
        "def total(values):\n    return math.fsum(values)\n```"
    )

    rewrite = parse_rewrite(reply, name="total")

    assert rewrite.imports == ("import math", "from decimal import Decimal")
    assert rewrite.source.startswith("def total")


def test_a_method_sent_back_inside_its_class_is_taken_out():
    reply = (
        "```python\nclass Stock:\n    def add(self, item):\n"
        "        self.items.append(item)\n        return self\n```"
    )

    rewrite = parse_rewrite(reply, name="add", owner="Stock")

    assert (
        rewrite.source
        == "def add(self, item):\n    self.items.append(item)\n    return self"
    )


def test_an_indented_method_is_dedented():
    reply = "```python\n    def add(self, item):\n        return self\n```"

    assert (
        parse_rewrite(reply, name="add").source
        == "def add(self, item):\n    return self"
    )


def test_a_reply_without_fences_is_read_as_code():
    rewrite = parse_rewrite(
        "def total(values):\n    return sum(values)\n", name="total"
    )

    assert rewrite.name == "total"


def test_a_reply_cut_off_before_the_closing_fence_still_counts():
    reply = "```python\ndef total(values):\n    return sum(values)\n"

    assert parse_rewrite(reply, name="total").name == "total"


def test_windows_line_endings_are_accepted():
    reply = "```python\r\ndef total(values):\r\n    return sum(values)\r\n```\r\n"

    assert parse_rewrite(reply, name="total").source == (
        "def total(values):\n    return sum(values)"
    )


def test_a_lone_function_with_another_name_is_a_rename():
    reply = "```python\ndef sum_values(values):\n    return sum(values)\n```"

    assert parse_rewrite(reply, name="total").name == "sum_values"


def test_async_is_reported():
    reply = "```python\nasync def fetch(url):\n    return url\n```"

    assert parse_rewrite(reply, name="fetch").is_async is True


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("I cannot help with that.", "no-code"),
        ("", "no-code"),
        ("```text\nnot python\n```", "no-code"),
        ("```python\ndef total(values)\n    return 1\n```", "syntax"),
        ("```python\nx = 1\n```", "no-definition"),
        (
            "```python\ndef a():\n    pass\n\ndef b():\n    pass\n```",
            "no-definition",
        ),
        (
            "```python\ndef total(values):\n    return sum(values)\n\n"
            "print(total([]))\n```",
            "extra-code",
        ),
        (
            "```python\ndef helper(x):\n    return x\n\ndef total(values):\n"
            "    return sum(map(helper, values))\n```",
            "extra-code",
        ),
    ],
)
def test_unusable_replies_are_rejected_with_a_reason(reply, reason):
    with pytest.raises(ResponseError) as caught:
        parse_rewrite(reply, name="total")

    assert caught.value.reason == reason


def test_extra_code_is_named_in_the_error():
    reply = (
        "```python\ndef helper(x):\n    return x\n\n"
        "def total(values):\n    return 1\n```"
    )

    with pytest.raises(ResponseError, match="`helper` on line 1"):
        parse_rewrite(reply, name="total")


def test_code_blocks_ignore_other_languages():
    text = "```bash\npip install x\n```\n```py\nvalue = 1\n```"

    assert code_blocks(text) == ["value = 1\n"]

import json
import sys

import pytest

from codesync.apply import ApplyError, apply_patch, list_applied, undo
from codesync.findings import Check, CheckStatus
from codesync.index import build_index
from codesync.llm import ProviderError
from codesync.parsing import read_exact_source
from codesync.response import parse_rewrite
from codesync.safe import Decision, refine_safely
from codesync.sandbox import Sandbox, clean_environment, find_python
from codesync.splice import splice
from codesync.verify import Verdict, Verifier, VerifyOptions

PROJECT = {
    "shop/__init__.py": "",
    "shop/prices.py": """
        def gross(amount: float, rate: float = 0.2) -> float:
            return amount * (1 + rate)

        def label(amount: float) -> str:
            return f"{gross(amount):.2f}"

        def lonely(amount):
            return amount
    """,
    "shop/report.py": """
        from shop.prices import gross

        def total(amounts):
            return sum(gross(item, rate=0.1) for item in amounts)
    """,
    "tests/test_prices.py": """
        from shop.prices import gross, label

        def test_gross():
            assert gross(100) == 120

        def test_label():
            assert label(100) == "120.00"
    """,
}

FAST = VerifyOptions(python=sys.executable, run_types=False)
FULL = VerifyOptions(python=sys.executable)


def make(index, qualname, code):
    target = index.table.lookup(qualname)
    rewrite = parse_rewrite(f"```python\n{code}\n```", name=target.name)
    source = read_exact_source(index.root / target.relpath)
    return target, rewrite, splice(source, target, rewrite)


def verify(index, qualname, code, options=FAST):
    target, rewrite, patch = make(index, qualname, code)
    with Verifier(index, target, options) as verifier:
        return verifier.check(rewrite, patch)


def statuses(report):
    return {item.check: item.status for item in report.checks}


@pytest.fixture
def index(write_project):
    return build_index(write_project(PROJECT))


SAFE_REWRITE = "def gross(amount: float, rate: float = 0.2) -> float:\n    return amount + amount * rate"


@pytest.mark.slow
def test_a_sound_rewrite_is_safe(index):
    report = verify(index, "shop.prices.gross", SAFE_REWRITE)

    assert report.verdict is Verdict.SAFE
    assert report.caught_by is None
    assert report.tests_run == 2
    assert report.would_break == ()


@pytest.mark.slow
def test_a_rewrite_that_breaks_behaviour_is_caught_by_the_tests(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, rate: float = 0.2) -> float:\n    return amount * rate",
    )

    assert report.verdict is Verdict.UNSAFE
    assert report.caught_by == "tests"
    assert "tests/test_prices.py" in report.would_break
    names = {item.code_text for item in report.findings}
    assert names == {"tests.test_prices::test_gross", "tests.test_prices::test_label"}


def test_a_broken_contract_never_reaches_the_slow_checks(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount",
    )

    assert report.verdict is Verdict.UNSAFE
    assert report.caught_by == "contract"
    assert statuses(report)[Check.TYPES] is CheckStatus.SKIPPED
    assert statuses(report)[Check.TESTS] is CheckStatus.SKIPPED


def test_a_refusal_names_the_file_that_would_have_broken(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount",
    )

    assert report.would_break == ("shop/report.py",)
    assert report.places == ("shop/report.py:4  gross(item, rate=0.1)",)


def test_when_only_the_contract_fails_every_user_is_named(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, rate: float = 0.5) -> float:\n    return amount",
    )

    assert report.caught_by == "contract"
    assert "shop/report.py" in report.would_break
    assert "shop/prices.py" in report.would_break


@pytest.mark.slow
def test_nothing_covering_a_function_leaves_it_unconfirmed(index):
    report = verify(
        index, "shop.prices.lonely", "def lonely(amount):\n    return amount + 0"
    )

    assert report.verdict is Verdict.UNCONFIRMED
    assert "no test reaches this function" in report.notes[0]


@pytest.mark.slow
def test_untested_code_can_be_allowed_on_request(index):
    options = VerifyOptions(python=sys.executable, run_types=False, allow_untested=True)

    report = verify(
        index, "shop.prices.lonely", "def lonely(amount):\n    return amount + 0", options
    )

    assert report.verdict is Verdict.SAFE


def test_with_tests_turned_off_a_sound_rewrite_is_unconfirmed(index):
    options = VerifyOptions(
        run_tests=False, run_types=False, tests_note="the hosted service does not run code"
    )

    report = verify(index, "shop.prices.gross", SAFE_REWRITE, options)

    assert report.verdict is Verdict.UNCONFIRMED
    assert report.notes == ("tests: the hosted service does not run code",)


@pytest.mark.slow
def test_a_test_that_already_failed_is_not_blamed_on_the_rewrite(write_project):
    files = dict(PROJECT)
    files["tests/test_prices.py"] = """
        from shop.prices import gross

        def test_gross():
            assert gross(100) == 120

        def test_already_broken():
            assert gross(1) == 999
    """
    index = build_index(write_project(files))

    report = verify(index, "shop.prices.gross", SAFE_REWRITE)

    assert report.verdict is Verdict.SAFE
    result = next(item for item in report.checks if item.check is Check.TESTS)
    assert result.detail == "1 already failed before the rewrite"


@pytest.mark.slow
def test_a_rewrite_that_never_finishes_is_caught(index):
    options = VerifyOptions(python=sys.executable, run_types=False, timeout=8)

    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, rate: float = 0.2) -> float:\n"
        "    while True:\n"
        "        amount += rate\n"
        "    return amount",
        options,
    )

    assert report.verdict is Verdict.UNSAFE
    assert report.findings[0].code == "tests-did-not-finish"


@pytest.mark.slow
def test_a_rewrite_that_stops_a_module_importing_is_caught(index):
    report = verify(
        index,
        "shop.prices.gross",
        "@undefined_at_import\n"
        "def gross(amount: float, rate: float = 0.2) -> float:\n"
        "    return amount",
    )

    assert report.verdict is Verdict.UNSAFE


@pytest.mark.slow
def test_mypy_catches_a_type_error_the_other_checks_cannot_see(write_project):
    files = {
        "app.py": """
            def width(text: str) -> int:
                return len(text)

            def double(text: str) -> int:
                return width(text) * 2
        """,
        "test_app.py": """
            from app import double

            def test_double():
                assert double("ab") == 4
        """,
    }
    index = build_index(write_project(files))

    report = verify(
        index,
        "app.double",
        "def double(text: str) -> int:\n    return width(len(text)) * 2",
        FULL,
    )

    assert report.verdict is Verdict.UNSAFE
    assert report.caught_by == "types"
    assert statuses(report)[Check.TESTS] is CheckStatus.SKIPPED


@pytest.mark.slow
def test_a_type_error_the_project_already_had_does_not_count(write_project):
    files = {
        "app.py": """
            def broken() -> int:
                return "already wrong"

            def double(a: int) -> int:
                return a * 2
        """,
        "test_app.py": """
            from app import double

            def test_double():
                assert double(2) == 4
        """,
    }
    index = build_index(write_project(files))

    report = verify(
        index, "app.double", "def double(a: int) -> int:\n    return a + a", FULL
    )

    assert report.verdict is Verdict.SAFE


@pytest.mark.slow
def test_the_real_project_is_never_touched(index):
    path = index.root / "shop" / "prices.py"
    before = path.read_bytes()

    verify(index, "shop.prices.gross", "def gross(amount, rate=0.2):\n    return 0")

    assert path.read_bytes() == before


def test_feedback_lists_what_broke_as_json(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount",
    )

    text = report.feedback()
    document = json.loads(text.split("```json\n")[1].split("\n```")[0])

    assert document["rejected"] == "shop.prices.gross"
    assert document["failed_checks"] == ["contract", "call-sites"]
    assert {
        "check": "call-sites",
        "code": "call-no-longer-binds",
        "message": "the call has no parameter `rate`",
        "path": "shop/report.py",
        "lineno": 4,
        "text": "gross(item, rate=0.1)",
    } in document["problems"]


def test_the_report_can_be_written_as_json(index):
    report = verify(
        index,
        "shop.prices.gross",
        "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount",
    )

    data = json.loads(json.dumps(report.to_dict()))

    assert data["verdict"] == "unsafe"
    assert data["caught_by"] == "contract"
    assert [item["check"] for item in data["checks"]] == [
        "contract",
        "call-sites",
        "names",
        "types",
        "tests",
    ]


BROKEN = "```python\ndef gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount\n```"
FIXED = f"```python\n{SAFE_REWRITE}\n```"
UNTESTED = "```python\ndef lonely(amount):\n    return amount + 0\n```"


def safely(index, qualname, client, **options):
    options.setdefault("options", FAST)
    return refine_safely(index, index.table.lookup(qualname), client, **options)


@pytest.mark.slow
def test_a_safe_rewrite_is_kept_on_the_first_try(index, scripted):
    client = scripted(FIXED)

    result = safely(index, "shop.prices.gross", client)

    assert result.decision is Decision.KEPT
    assert len(result.attempts) == 1
    assert result.patch is not None
    assert len(client.requests) == 1


@pytest.mark.slow
def test_a_failed_check_is_sent_back_and_the_fix_is_kept(index, scripted):
    client = scripted(BROKEN, FIXED)

    result = safely(index, "shop.prices.gross", client)

    assert result.decision is Decision.KEPT
    assert len(result.attempts) == 2
    assert result.attempts[0].report.verdict is Verdict.UNSAFE
    assert result.attempts[1].report.verdict is Verdict.SAFE


def test_the_model_is_told_exactly_what_broke(index, scripted):
    client = scripted(BROKEN, BROKEN, BROKEN, BROKEN)

    safely(index, "shop.prices.gross", client)

    second = client.requests[1]
    assert [item["role"] for item in second] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second[2]["content"] == BROKEN
    assert "call-no-longer-binds" in second[3]["content"]
    assert "shop/report.py" in second[3]["content"]


def test_after_three_fixes_it_refuses_and_names_the_damage(index, scripted):
    client = scripted(BROKEN, BROKEN, BROKEN, BROKEN, BROKEN)

    result = safely(index, "shop.prices.gross", client)

    assert result.decision is Decision.REFUSED
    assert len(client.requests) == 4
    assert result.reason == "still not safe after 4 attempts"
    assert result.report.would_break == ("shop/report.py",)
    assert result.patch is None


def test_no_fixes_means_one_attempt(index, scripted):
    client = scripted(BROKEN, FIXED)

    result = safely(index, "shop.prices.gross", client, fixes=0)

    assert result.decision is Decision.REFUSED
    assert len(client.requests) == 1


@pytest.mark.slow
def test_an_unconfirmed_change_is_refused_without_retrying(index, scripted):
    client = scripted(UNTESTED, UNTESTED)

    result = safely(index, "shop.prices.lonely", client)

    assert result.decision is Decision.REFUSED
    assert len(client.requests) == 1
    assert "could not be confirmed safe" in result.reason


@pytest.mark.slow
def test_an_unusable_reply_gets_another_try(index, scripted):
    client = scripted("I would rather not.", FIXED)

    result = safely(index, "shop.prices.gross", client)

    assert result.decision is Decision.KEPT
    assert "could not be used" in client.requests[1][3]["content"]


def test_a_provider_failure_stops_the_loop(index, scripted):
    client = scripted(ProviderError("503: overloaded"), FIXED)

    result = safely(index, "shop.prices.gross", client)

    assert result.decision is Decision.FAILED
    assert result.reason == "503: overloaded"
    assert len(client.requests) == 1


def test_an_identical_rewrite_is_nothing_to_keep(write_project, scripted):
    index = build_index(write_project({"a.py": "def f(x):\n    return x\n"}))

    result = safely(index, "a.f", scripted("```python\ndef f(x):\n    return x\n```"))

    assert result.decision is Decision.UNCHANGED
    assert result.patch is None


def test_the_record_explains_every_attempt(index, scripted):
    client = scripted(BROKEN)

    result = safely(index, "shop.prices.gross", client, fixes=0)

    record = json.loads(json.dumps(result.to_record()))
    assert record["decision"] == "refused"
    assert record["attempts"][0]["report"]["caught_by"] == "contract"
    assert record["attempts"][0]["diff"].startswith("--- a/shop/prices.py")


def test_a_change_can_be_applied_and_undone_exactly(index):
    path = index.root / "shop" / "prices.py"
    original = path.read_bytes()
    _, _, patch = make(index, "shop.prices.gross", SAFE_REWRITE)

    change = apply_patch(index.root, patch, "shop.prices.gross")

    assert b"amount + amount * rate" in path.read_bytes()
    assert [item.id for item in list_applied(index.root)] == [change.id]

    undone = undo(index.root)

    assert undone.id == change.id
    assert path.read_bytes() == original
    assert list_applied(index.root) == []


def test_undo_restores_line_endings_and_encoding_exactly(write_project):
    root = write_project({"a.py": "x"})
    path = root / "a.py"
    original = "﻿# café\r\ndef f(x):\r\n    return x\r\n".encode()
    path.write_bytes(original)
    index = build_index(root)
    _, _, patch = make(index, "a.f", "def f(x):\n    return x + 0")

    apply_patch(root, patch, "a.f")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"return x + 0\r\n" in path.read_bytes()
    undo(root)

    assert path.read_bytes() == original


def test_a_file_changed_since_the_check_is_not_overwritten(index):
    path = index.root / "shop" / "prices.py"
    _, _, patch = make(index, "shop.prices.gross", SAFE_REWRITE)
    path.write_text("# edited by hand\n", encoding="utf-8")

    with pytest.raises(ApplyError, match="changed after this rewrite was checked"):
        apply_patch(index.root, patch, "shop.prices.gross")

    assert path.read_text(encoding="utf-8") == "# edited by hand\n"
    assert list_applied(index.root) == []


def test_undo_will_not_throw_away_a_later_edit(index):
    path = index.root / "shop" / "prices.py"
    _, _, patch = make(index, "shop.prices.gross", SAFE_REWRITE)
    apply_patch(index.root, patch, "shop.prices.gross")
    path.write_text("# edited by hand\n", encoding="utf-8")

    with pytest.raises(ApplyError, match="edited after the change was applied"):
        undo(index.root)

    assert path.read_text(encoding="utf-8") == "# edited by hand\n"


def test_changes_to_one_file_are_undone_newest_first(index):
    _, _, first = make(index, "shop.prices.gross", SAFE_REWRITE)
    applied = apply_patch(index.root, first, "shop.prices.gross")
    second_index = build_index(index.root)
    _, _, second = make(
        second_index, "shop.prices.label", 'def label(amount: float) -> str:\n    return "x"'
    )
    apply_patch(index.root, second, "shop.prices.label")

    with pytest.raises(ApplyError, match="undo that first"):
        undo(index.root, applied.id)


def test_undo_with_nothing_applied_says_so(index):
    with pytest.raises(ApplyError, match="no applied change"):
        undo(index.root)


def test_a_sandbox_is_a_copy_that_goes_away(write_project):
    root = write_project({"a.py": "x = 1\n", ".venv/skip.py": "y = 2\n"})

    with Sandbox(root) as sandbox:
        copy = sandbox.path
        assert (copy / "a.py").is_file()
        assert not (copy / ".venv").exists()

        sandbox.write("a.py", "x = 2\n")
        assert (copy / "a.py").read_text() == "x = 2\n"
        assert (root / "a.py").read_text() == "x = 1\n"

        sandbox.restore()
        assert (copy / "a.py").read_text() == "x = 1\n"

    assert not copy.exists()


def test_secrets_stay_out_of_the_tests_environment(tmp_path):
    env = clean_environment(
        tmp_path,
        {
            "PATH": "/bin",
            "GEMINI_API_KEY": "s",
            "CODESYNC_API_KEYS": "s",
            "GITHUB_TOKEN": "s",
            "AWS_SECRET_ACCESS_KEY": "s",
            "DB_PASSWORD": "s",
        },
    )

    assert set(env) == {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}


def test_a_src_layout_is_put_on_the_path(tmp_path):
    (tmp_path / "src").mkdir()

    env = clean_environment(tmp_path, {})

    assert str(tmp_path / "src") in env["PYTHONPATH"]


def test_a_project_virtual_environment_is_preferred(tmp_path):
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("")

    assert find_python(tmp_path) == str(python)


def test_without_one_the_current_python_is_used(tmp_path):
    assert find_python(tmp_path) == sys.executable

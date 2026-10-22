import json

import pytest

from codesync.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def model(monkeypatch, annotator):
    client = annotator()
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )
    return client


def test_variables_are_listed_in_a_table(capsys, shop_project):
    code, out, _ = run(capsys, "variables", str(shop_project), "--kind", "module")

    lines = out.splitlines()
    assert code == 0
    assert lines[0].split() == ["kind", "variable", "type", "r/w", "location"]
    assert "shop.config.counter" in out
    assert "int" in lines[-3]
    assert out.rstrip().endswith("4 variables")


def test_variables_can_be_narrowed_down(capsys, shop_project):
    code, out, _ = run(
        capsys,
        "variables",
        str(shop_project),
        "--container",
        "shop.cart.Cart.add",
        "--name",
        "qty",
    )

    assert code == 0
    assert "shop.cart.Cart.add.qty" in out
    assert out.rstrip().endswith("1 variable")


def test_only_cross_file_variables_can_be_listed(capsys, shop_project):
    code, out, _ = run(capsys, "variables", str(shop_project), "--cross-file")

    assert code == 0
    assert out.rstrip().endswith("3 variables")
    assert "shop.cart" not in out


def test_the_list_can_be_cut_short(capsys, shop_project):
    code, out, _ = run(capsys, "variables", str(shop_project), "--limit", "5")

    assert code == 0
    assert out.rstrip().endswith("30 variables, 25 more not shown (see --limit)")


def test_variables_are_emitted_as_json(capsys, shop_project):
    code, out, _ = run(
        capsys, "variables", str(shop_project), "--kind", "module", "--json"
    )

    entries = json.loads(out)
    assert code == 0
    assert [entry["name"] for entry in entries] == [
        "__all__",
        "TAX",
        "LIMITS",
        "counter",
    ]
    assert entries[1]["external_files"] == ["shop/cart.py"]


def test_a_filter_with_no_match_says_so(capsys, shop_project):
    code, out, _ = run(
        capsys, "variables", str(shop_project), "--query", "nothing-here"
    )

    assert code == 1
    assert out.strip() == "no variables matched"


def test_one_variable_is_shown_with_every_access(capsys, shop_project):
    code, out, _ = run(capsys, "variable", str(shop_project), "shop.config.counter")

    assert code == 0
    assert "kind      module variable" in out
    assert "type      int (inferred)" in out
    assert "used      3 reads and 2 writes in 2 files" in out
    assert "written (2)" in out and "read (3)" in out
    assert "shop/config.py:8  += 1" in out
    assert "shop/cart.py:34" in out


def test_a_parameter_is_described_by_how_it_is_passed(capsys, shop_project):
    code, out, _ = run(
        capsys, "variable", str(shop_project), "shop.cart.Cart.__init__.strict"
    )

    assert code == 0
    assert "keyword-only parameter" in out
    assert "default   False" in out


def test_an_overriding_attribute_names_what_it_overrides(capsys, shop_project):
    _, out, _ = run(capsys, "variable", str(shop_project), "shop.cart.Cart.items")

    assert "overrides  shop.cart.Base.items" in out


def test_one_variable_can_be_emitted_as_json(capsys, shop_project):
    code, out, _ = run(
        capsys, "variable", str(shop_project), "shop.config.TAX", "--json"
    )

    entry = json.loads(out)
    assert code == 0
    assert entry["qualname"] == "shop.config.TAX"
    assert len(entry["accesses"]) == 2


def test_an_unknown_variable_suggests_close_names(capsys, shop_project):
    code, _, err = run(capsys, "variable", str(shop_project), "shop.cart.TAX")

    assert code == 2
    assert "did you mean `shop.config.TAX`" in err


def test_a_dry_run_shows_the_plan_and_sends_nothing(capsys, shop_project, model):
    code, out, _ = run(capsys, "annotate", str(shop_project), "--dry-run")

    assert code == 0
    assert "variables                30" in out
    assert "described by the model   26" in out
    assert "described statically     4 (self and cls)" in out
    assert "estimated prompt tokens" in out
    assert model.calls == 0


def test_a_dry_run_needs_no_api_key(capsys, shop_project, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code, _, _ = run(capsys, "annotate", str(shop_project), "--dry-run")

    assert code == 0


def test_annotate_writes_the_variable_map(capsys, tmp_path, shop_project, model):
    output = tmp_path / "out" / "map.json"

    code, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(output),
        "--cache",
        str(tmp_path / "cache.jsonl"),
        "--quiet",
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert "described      30" in out
    assert "not described  0 missing, 0 failed" in out
    assert str(output) in out
    assert document["summary"]["annotations"]["ok"] == 30
    assert document["variables"][0]["ai"]["status"] == "ok"
    assert "accesses" not in document["variables"][0]
    assert model.calls > 0


def test_a_sample_of_the_descriptions_is_printed(capsys, tmp_path, shop_project, model):
    _, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "map.json"),
        "--cache",
        str(tmp_path / "cache.jsonl"),
        "--quiet",
    )

    table = out[out.index("variable  ") :]
    assert table.splitlines()[0].split() == ["variable", "type", "role", "purpose"]
    assert "Holds the value of" in table
    assert "... and" in table and "more in the variable map" in table


def test_a_second_run_uses_the_cache(capsys, tmp_path, shop_project, model):
    arguments = [
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "map.json"),
        "--cache",
        str(tmp_path / "cache.jsonl"),
        "--quiet",
    ]
    run(capsys, *arguments)
    calls = model.calls

    code, out, _ = run(capsys, *arguments)

    assert code == 0
    assert model.calls == calls
    assert "0 sent," in out and "from cache" in out


def test_the_cache_can_be_switched_off(capsys, tmp_path, shop_project, model):
    arguments = [
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "map.json"),
        "--quiet",
        "--no-cache",
    ]
    run(capsys, *arguments)
    calls = model.calls

    run(capsys, *arguments)

    assert model.calls == 2 * calls
    assert not (tmp_path / "cache.jsonl").exists()


def test_only_some_kinds_can_be_described(capsys, tmp_path, shop_project, model):
    output = tmp_path / "map.json"

    code, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--kinds",
        "module, class",
        "--output",
        str(output),
        "--no-cache",
        "--quiet",
    )

    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert code == 0
    assert summary["annotations"]["ok"] == 11
    assert summary["annotations"]["skipped"] == 19


def test_the_map_can_include_every_access(capsys, tmp_path, shop_project, model):
    output = tmp_path / "map.json"

    run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(output),
        "--no-cache",
        "--quiet",
        "--accesses",
    )

    assert "accesses" in json.loads(output.read_text(encoding="utf-8"))["variables"][0]


def test_an_unknown_kind_is_an_error(capsys, shop_project, model):
    code, _, err = run(
        capsys, "annotate", str(shop_project), "--kinds", "gizmo", "--dry-run"
    )

    assert code == 2
    assert "the kinds are module, class, instance, parameter, local" in err


def test_without_a_key_annotate_says_what_to_set(
    capsys, tmp_path, shop_project, monkeypatch
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code, _, err = run(
        capsys, "annotate", str(shop_project), "--output", str(tmp_path / "m.json")
    )

    assert code == 2
    assert "GEMINI_API_KEY" in err
    assert not (tmp_path / "m.json").exists()


def test_a_failing_provider_gives_a_non_zero_exit(
    capsys, tmp_path, shop_project, monkeypatch, annotator
):
    from codesync.llm import ProviderError

    client = annotator(fail=ProviderError("401: invalid api key"))
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    code, out, err = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
    )

    assert code == 1
    assert "failed" in out
    assert (tmp_path / "m.json").exists()


def test_a_failed_run_says_why_and_what_to_try(
    capsys, tmp_path, shop_project, monkeypatch, annotator
):
    from codesync.llm import ProviderError

    client = annotator(
        fail=ProviderError("https://x.test/v1 returned 401: Authentication Fails")
    )
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    code, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
    )

    assert code == 1
    assert "what went wrong" in out
    assert "26 variables: https://x.test/v1 returned 401: Authentication Fails" in out
    assert "-> the key was rejected" in out
    assert "Run `codesync check`" in out


def flaky(annotator, failing_calls):
    """A model that fails on the numbered calls and answers the rest."""
    from codesync.llm import ProviderError

    base = type(annotator())

    class Flaky(base):
        def complete(self, messages):
            if len(self.requests) + 1 in failing_calls:
                self.requests.append(messages)
                raise ProviderError("x returned 503: high demand")
            return super().complete(messages)

    return Flaky()


def test_a_partly_failed_run_can_be_finished_by_running_it_again(
    capsys, tmp_path, shop_project, monkeypatch, annotator
):
    arguments = [
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--cache",
        str(tmp_path / "cache.jsonl"),
        "--quiet",
        "--workers",
        "1",
    ]
    first = flaky(annotator, failing_calls={2})
    monkeypatch.setattr("codesync.cliargs.create_client", lambda **kwargs: first)

    code, out, _ = run(capsys, *arguments)

    assert code == 0
    assert "described      22" in out
    assert "8 failed" in out
    assert "Run the same command again" in out
    assert "only the failed requests are sent" in out

    second = annotator()
    monkeypatch.setattr("codesync.cliargs.create_client", lambda **kwargs: second)

    code, out, _ = run(capsys, *arguments)

    assert code == 0
    assert second.calls == 1
    assert "described      30" in out
    assert "1 sent, 1 from cache" in out
    assert "what went wrong" not in out


def test_without_a_cache_there_is_nothing_to_resume(
    capsys, tmp_path, shop_project, monkeypatch, annotator
):
    client = flaky(annotator, failing_calls={2})
    monkeypatch.setattr("codesync.cliargs.create_client", lambda **kwargs: client)

    _, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
        "--workers",
        "1",
    )

    assert "what went wrong" in out
    assert "Run the same command again" not in out


def test_the_same_error_with_different_request_ids_is_reported_once(
    capsys, tmp_path, shop_project, monkeypatch, annotator
):
    from codesync.llm import ProviderError

    errors = iter(
        [
            ProviderError("returned 401: bad key (request_id: aaa-111)"),
            ProviderError("returned 401: bad key (request_id: bbb-222)"),
        ]
    )

    class Rotating(type(annotator())):
        def complete(self, messages):
            self.fail = next(errors)
            return super().complete(messages)

    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: Rotating()
    )

    _, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
        "--workers",
        "1",
    )

    assert out.count("returned 401: bad key") == 1
    assert "request_id" not in out


def test_variables_that_were_only_not_selected_are_not_a_problem(
    capsys, tmp_path, shop_project, model
):
    _, out, _ = run(
        capsys,
        "annotate",
        str(shop_project),
        "--kinds",
        "module",
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
    )

    assert "what went wrong" not in out


def test_a_working_key_is_confirmed(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaabc12")
    client = annotator()
    client.base_url = "https://api.test"
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    code, out, _ = run(capsys, "check")

    assert code == 0
    assert "model    annotator-1" in out
    assert "key      GEMINI_API_KEY, 9 characters" in out
    assert "the key works" in out
    assert "AIzaabc12" not in out


def test_check_leaves_room_for_a_model_that_thinks(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaabc12")
    client = annotator()
    client.base_url = "https://api.test"
    seen = {}

    def make(*args, **kwargs):
        seen.update(kwargs)
        return client

    monkeypatch.setattr("codesync.cliargs.create_client", make)

    run(capsys, "check")
    assert seen["max_tokens"] == 512

    run(capsys, "check", "--max-tokens", "2000")
    assert seen["max_tokens"] == 2000


def test_check_is_quick_and_a_real_run_is_patient(
    capsys, monkeypatch, annotator, shop_project, tmp_path
):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaabc12")
    client = annotator()
    client.base_url = "https://api.test"
    seen = []

    def make(*args, **kwargs):
        seen.append(kwargs["retries"])
        return client

    monkeypatch.setattr("codesync.cliargs.create_client", make)

    run(capsys, "check")
    run(
        capsys,
        "annotate",
        str(shop_project),
        "--output",
        str(tmp_path / "m.json"),
        "--no-cache",
        "--quiet",
    )
    run(capsys, "check", "--retries", "3")

    assert seen == [1, 4, 3]


def test_a_rejected_key_is_diagnosed(capsys, monkeypatch, annotator):
    from codesync.llm import ProviderError

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaabc12")
    client = annotator(fail=ProviderError("x returned 401: Authentication Fails"))
    client.base_url = "https://api.test"
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    code, out, _ = run(capsys, "check")

    assert code == 1
    assert "failed: x returned 401: Authentication Fails" in out
    assert "-> the key was rejected" in out
    assert "the key works" not in out


def test_a_key_with_spaces_is_called_out(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "GEMINI API KEY")
    client = annotator()
    client.base_url = "https://api.test"
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    _, out, _ = run(capsys, "check")

    assert "the key contains spaces, so it is not a real key" in out


def test_check_without_a_key_says_what_to_set(capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code, _, err = run(capsys, "check")

    assert code == 2
    assert "GEMINI_API_KEY" in err


def test_the_models_on_offer_are_listed(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaKey")
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: annotator()
    )

    code, out, _ = run(capsys, "models")

    assert code == 0
    assert out.splitlines()[:3] == ["alpha-flash", "alpha-pro", "beta-1"]
    assert "3 models. Use one with --model, or set GEMINI_MODEL." in out


def test_the_model_list_can_be_filtered(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaab")
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: annotator()
    )

    code, out, _ = run(capsys, "models", "--contains", "ALPHA")

    assert code == 0
    assert out.splitlines()[:2] == ["alpha-flash", "alpha-pro"]
    assert "beta-1" not in out
    assert "2 models. Use one with --model, or set GEMINI_MODEL." in out


def test_a_filter_that_matches_nothing_says_so(capsys, monkeypatch, annotator):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaab")
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: annotator()
    )

    code, out, _ = run(capsys, "models", "--contains", "gizmo")

    assert code == 1
    assert out.strip() == "no models matched"


def test_a_failure_while_listing_models_is_explained(capsys, monkeypatch, annotator):
    from codesync.llm import ProviderError

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaab")
    client = annotator(fail=ProviderError("x returned 401: Authentication Fails"))
    monkeypatch.setattr(
        "codesync.cliargs.create_client", lambda *args, **kwargs: client
    )

    code, out, _ = run(capsys, "models")

    assert code == 1
    assert "failed: x returned 401" in out
    assert "-> the key was rejected" in out


def test_models_without_a_key_says_what_to_set(capsys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code, _, err = run(capsys, "models")

    assert code == 2
    assert "GEMINI_API_KEY" in err


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "x returned 404: This model models/gemini-old is no longer available",
            "`codesync models` lists the current ones",
        ),
        ("x returned 401: Authentication Fails", "key was rejected"),
        ("x returned 400: Please pass a valid API key", "key was rejected"),
        (
            "the reply did not contain a message (finish reason 'length', fields [])",
            "raise --max-tokens",
        ),
        ("x returned 503: This model is experiencing high demand", "overloaded"),
        ("the model is overloaded right now", "overloaded"),
        ("x returned 403: PERMISSION_DENIED", "access was denied"),
        ("x returned 429: slow down", "rate limited"),
        ("could not reach https://x: refused", "internet connection"),
        ("no answer from https://x after 3 attempts", "internet connection"),
    ],
)
def test_common_failures_come_with_advice(message, expected):
    from codesync.cliargs import explain_error

    assert expected in explain_error(message)


def test_an_unfamiliar_failure_gets_no_made_up_advice():
    from codesync.cliargs import explain_error

    assert explain_error("x returned 500: boom") is None


def test_serve_starts_the_app(capsys, monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **options: started.update(app=app, **options)
    )
    monkeypatch.setenv("CODESYNC_DATA_DIR", str(tmp_path))

    code, _, _ = run(capsys, "serve", "--host", "0.0.0.0", "--port", "9123")

    assert code == 0
    assert (started["host"], started["port"]) == ("0.0.0.0", 9123)
    assert started["app"].title == "CodeSync"

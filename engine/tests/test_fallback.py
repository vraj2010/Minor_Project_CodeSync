"""Exercise routing without making network calls or spending API quota."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from codesync import llm
from codesync.cli import main
from codesync.llm import ChatClient, FallbackClient, ProviderError, create_client
from codesync.safe import Decision, refine_safely
from codesync.verify import VerifyOptions

MESSAGES = [{"role": "user", "content": "rewrite this"}]


def answer(model):
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    })


@pytest.fixture
def route(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "primary-model")
    monkeypatch.setenv("GROQ_MODEL", "backup-model")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.delenv("GROQ_BASE_URL", raising=False)
    llm._cooldown_for.cache_clear()
    clients = []

    def make(handler):
        client = create_client(max_tokens=512, temperature=0.1)
        assert isinstance(client, FallbackClient)
        client.primary._http.close()
        client.fallback._http.close()
        transport = httpx.MockTransport(handler)
        client.primary._http = httpx.Client(transport=transport)
        client.fallback._http = httpx.Client(transport=transport)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()
    llm._cooldown_for.cache_clear()


def test_healthy_gemini_does_not_contact_groq(route):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert request.headers["authorization"] == "Bearer gemini-test-key"
        assert json.loads(request.content)["model"] == "primary-model"
        return answer("gemini-actual")

    completion = route(handler).complete(MESSAGES)
    assert seen == ["generativelanguage.googleapis.com"]
    assert completion.model == "gemini-actual"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_temporary_failure_switches_immediately_and_keeps_usage(route, status, monkeypatch):
    seen = []
    monkeypatch.setattr(llm.time, "sleep", lambda _: pytest.fail("must not stack retries"))

    def handler(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            return httpx.Response(status, json={"error": {"message": "unavailable"}})
        assert request.headers["authorization"] == "Bearer groq-test-key"
        assert json.loads(request.content) == {
            "model": "backup-model", "messages": MESSAGES,
            "temperature": 0.1, "max_tokens": 512,
        }
        return answer("groq-actual")

    completion = route(handler).complete(MESSAGES)
    assert seen == ["generativelanguage.googleapis.com", "api.groq.com"]
    assert (completion.model, completion.prompt_tokens, completion.completion_tokens) == (
        "groq-actual", 12, 2,
    )


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_connection_failure_also_uses_groq(route, error_type):
    def handler(request):
        if request.url.host == "generativelanguage.googleapis.com":
            raise error_type("offline", request=request)
        return answer("groq-actual")

    assert route(handler).complete(MESSAGES).model == "groq-actual"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_failure_is_visible_without_fallback(route, status):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(status, json={"error": {"message": "fix configuration"}})

    with pytest.raises(ProviderError, match=f"returned {status}"):
        route(handler).complete(MESSAGES)
    assert seen == ["generativelanguage.googleapis.com"]


def test_malformed_success_is_not_treated_as_a_quota_failure(route):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ProviderError, match="did not contain a message"):
        route(handler).complete(MESSAGES)
    assert len(seen) == 1


def test_invalid_transport_configuration_does_not_trigger_fallback(route):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        raise httpx.UnsupportedProtocol("bad address", request=request)

    with pytest.raises(ProviderError, match="bad address"):
        route(handler).complete(MESSAGES)
    assert len(seen) == 1


def test_a_groq_rewrite_still_has_to_pass_the_contract_checks(route, inventory_index):
    def handler(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(429, json={})
        response = answer("groq-actual")
        body = response.json()
        body["choices"][0]["message"]["content"] = (
            "```python\ndef apply_discount(item):\n    return item\n```"
        )
        return httpx.Response(200, json=body)

    target = inventory_index.table.lookup("inventory.pricing.apply_discount")
    result = refine_safely(
        inventory_index, target, route(handler), fixes=0,
        options=VerifyOptions(run_types=False, run_tests=False),
    )
    assert result.decision is Decision.REFUSED
    attempt = result.attempts[0]
    assert attempt.result.completion.model == "groq-actual"
    assert attempt.report.verdict.value == "unsafe"


def test_cooldown_is_shared_across_new_jobs_and_honors_retry_after(route, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com" and now[0] < 1120:
            return httpx.Response(429, headers={"Retry-After": "120"}, json={})
        return answer(request.url.host)

    route(handler).complete(MESSAGES)
    now[0] = 1061.0
    route(handler).complete(MESSAGES)
    assert seen == ["generativelanguage.googleapis.com", "api.groq.com", "api.groq.com"]
    now[0] = 1121.0
    route(handler).complete(MESSAGES)
    assert seen[-1] == "generativelanguage.googleapis.com"


def test_default_cooldown_recovers_after_a_minute(route, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503, json={}) if len(seen) == 1 else answer(request.url.host)

    client = route(handler)
    client.complete(MESSAGES)
    client.complete(MESSAGES)
    now[0] += 61
    assert client.complete(MESSAGES).model == "generativelanguage.googleapis.com"
    assert seen.count("generativelanguage.googleapis.com") == 2


def test_concurrent_requests_during_cooldown_use_only_groq(route):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(429, json={})
        return answer("groq-actual")

    client = route(handler)
    client.complete(MESSAGES)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: client.complete(MESSAGES), range(8)))
    assert all(reply.model == "groq-actual" for reply in replies)
    assert seen.count("generativelanguage.googleapis.com") == 1


def test_both_failures_are_reported_without_a_partial_result(route):
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "quota exhausted"}})

    with pytest.raises(ProviderError) as caught:
        route(handler).complete(MESSAGES)
    assert "Gemini unavailable" in str(caught.value)
    assert "Groq fallback failed" in str(caught.value)
    assert "try again" in str(caught.value)


def test_key_changes_do_not_inherit_a_previous_cooldown(route, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(429, json={})
        return answer("OK")

    route(handler).complete(MESSAGES)
    monkeypatch.setenv("GEMINI_API_KEY", "new-gemini-key")
    route(handler).complete(MESSAGES)
    assert seen[-1] == "Bearer new-gemini-key"


@pytest.mark.parametrize("disabled", ["0", "false", "off", "no"])
def test_environment_can_disable_fallback(route, monkeypatch, disabled):
    monkeypatch.setenv("CODESYNC_FALLBACK", disabled)
    client = create_client()
    try:
        assert isinstance(client, ChatClient)
        assert client.retries == 4
    finally:
        client.close()


def test_no_groq_key_keeps_single_provider_behavior(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = create_client()
    try:
        assert isinstance(client, ChatClient)
        assert client.retries == 4
    finally:
        client.close()


def test_explicit_groq_uses_its_own_settings(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
    monkeypatch.setenv("GROQ_MODEL", "chosen-model")
    monkeypatch.setenv("GROQ_BASE_URL", "https://gateway.test/v1")
    client = create_client(provider="groq")
    try:
        assert isinstance(client, ChatClient)
        assert client.model == "chosen-model"
        assert client.base_url == "https://gateway.test/v1"
        assert client._headers() == {"Authorization": "Bearer groq-test-key"}
    finally:
        client.close()


def test_other_providers_do_not_implicitly_fall_back(route):
    client = create_client(provider="ollama")
    try:
        assert isinstance(client, ChatClient)
    finally:
        client.close()


def test_route_has_a_distinct_cache_identity_and_closes_both_clients(route):
    client = route(lambda _: answer("OK"))
    assert client.model == "primary-model -> backup-model"
    client.close()
    assert client.primary._http.is_closed
    assert client.fallback._http.is_closed


def test_check_shows_the_model_that_actually_answered(route, monkeypatch, capsys):
    def handler(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(429, json={})
        return answer("groq-actual")

    client = route(handler)
    monkeypatch.setattr("codesync.cliargs.create_client", lambda **_: client)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "groq-actual" in out
    assert "the model route works" in out
    assert "the key works" not in out
    assert "groq-test-key" not in out


def test_no_fallback_flag_reaches_the_factory(route, monkeypatch):
    captured = []
    original = create_client

    def factory(**kwargs):
        captured.append(kwargs)
        client = original(**kwargs)
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(lambda _: answer("OK")))
        return client

    monkeypatch.setattr("codesync.cliargs.create_client", factory)
    assert main(["check", "--no-fallback"]) == 0
    assert captured[0]["fallback"] is False

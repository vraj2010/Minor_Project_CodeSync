import json
from pathlib import Path

import httpx
import pytest

from codesync.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ChatClient,
    ProviderError,
    create_client,
)

MESSAGES = [{"role": "user", "content": "rewrite this"}]


def reply(text="```python\ndef f():\n    return 1\n```", **extra):
    return {
        "model": "test-model-0613",
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        **extra,
    }


def client_for(handler, **options):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    options.setdefault("retry_delay", 0)
    return ChatClient("https://llm.test/v1/", "test-model", http=http, **options)


def test_sends_an_openai_style_chat_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply())

    client_for(handler, api_key="secret", temperature=0.0, max_tokens=512).complete(
        MESSAGES
    )

    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {
        "model": "test-model",
        "messages": MESSAGES,
        "temperature": 0.0,
        "max_tokens": 512,
    }


def test_no_authorization_header_without_a_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=reply())

    client_for(handler).complete(MESSAGES)

    assert seen["auth"] is None


def test_the_completion_carries_text_usage_and_timing():
    completion = client_for(
        lambda request: httpx.Response(200, json=reply("hello"))
    ).complete(MESSAGES)

    assert completion.text == "hello"
    assert completion.model == "test-model-0613"
    assert completion.prompt_tokens == 120
    assert completion.completion_tokens == 30
    assert completion.finish_reason == "stop"
    assert completion.latency >= 0


def test_a_busy_server_is_retried():
    statuses = iter([503, 429, 200])

    def handler(request):
        status = next(statuses)
        return httpx.Response(status, json=reply() if status == 200 else {})

    completion = client_for(handler, retries=2).complete(MESSAGES)

    assert completion.text.startswith("```python")


@pytest.fixture
def pauses(monkeypatch):
    recorded = []
    monkeypatch.setattr("codesync.llm.time.sleep", recorded.append)
    return recorded


def test_the_wait_doubles_between_retries(pauses):
    client = client_for(
        lambda request: httpx.Response(503, json={}), retries=4, retry_delay=2.0
    )

    with pytest.raises(ProviderError):
        client.complete(MESSAGES)

    assert pauses == [2.0, 4.0, 8.0, 16.0]


def test_a_wait_never_exceeds_the_cap(pauses):
    client = client_for(
        lambda request: httpx.Response(503, json={}), retries=6, retry_delay=10.0
    )

    with pytest.raises(ProviderError):
        client.complete(MESSAGES)

    assert pauses == [10.0, 20.0, 30.0, 30.0, 30.0, 30.0]


def test_the_providers_own_retry_hint_is_followed(pauses):
    statuses = iter([429, 200])

    def handler(request):
        if next(statuses) == 429:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json=reply())

    client_for(handler, retry_delay=2.0).complete(MESSAGES)

    assert pauses == [7.0]


@pytest.mark.parametrize(
    ("hint", "expected"), [("500", 30.0), ("soon", 2.0), ("-3", 0.0), ("", 2.0)]
)
def test_an_odd_retry_hint_is_capped_or_ignored(pauses, hint, expected):
    statuses = iter([503, 200])

    def handler(request):
        if next(statuses) == 503:
            return httpx.Response(503, headers={"Retry-After": hint}, json={})
        return httpx.Response(200, json=reply())

    client_for(handler, retry_delay=2.0).complete(MESSAGES)

    assert pauses == [expected]


def test_a_slow_provider_is_waited_for_too(pauses):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=reply())

    client_for(handler, retry_delay=1.0).complete(MESSAGES)

    assert pauses == [1.0, 2.0]


def test_clients_are_patient_by_default(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key")

    assert create_client().retries == 4
    assert create_client(retries=1).retries == 1


def test_retrying_stops_after_the_last_attempt():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(ProviderError, match="503: overloaded"):
        client_for(handler, retries=2).complete(MESSAGES)
    assert len(attempts) == 3


def test_the_models_on_offer_are_listed_without_prefixes():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        body = {
            "object": "list",
            "data": [
                {"id": "models/gemini-flash"},
                {"id": "models/gemini-pro"},
                {"id": "gemini-flash"},
                {"object": "model"},
            ],
        }
        return httpx.Response(200, json=body)

    names = client_for(handler, api_key="key").list_models()

    assert names == ["gemini-flash", "gemini-pro"]
    assert seen == {"url": "https://llm.test/v1/models", "auth": "Bearer key"}


def test_a_listing_that_uses_another_shape_is_understood():
    body = {"models": [{"name": "models/z-model"}, {"name": "models/a-model"}]}

    names = client_for(lambda request: httpx.Response(200, json=body)).list_models()

    assert names == ["a-model", "z-model"]


def test_a_provider_with_no_models_gives_an_empty_list():
    assert client_for(lambda request: httpx.Response(200, json={})).list_models() == []


def test_listing_models_reports_a_rejected_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ProviderError, match="401: bad key"):
        client_for(handler).list_models()


def test_listing_models_reports_a_failed_connection():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError, match="could not reach"):
        client_for(handler).list_models()


def test_an_error_wrapped_in_a_list_is_unwrapped():
    body = [{"error": {"code": 400, "message": "Please pass a valid API key"}}]

    with pytest.raises(ProviderError, match="400: Please pass a valid API key$"):
        client_for(lambda request: httpx.Response(400, json=body)).complete(MESSAGES)


def test_an_error_without_a_message_falls_back_to_the_body():
    with pytest.raises(ProviderError, match="502: gateway down"):
        client_for(
            lambda request: httpx.Response(502, text="gateway down"), retries=0
        ).complete(MESSAGES)


def test_a_client_error_is_reported_without_retrying():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    with pytest.raises(ProviderError, match="401: invalid api key"):
        client_for(handler).complete(MESSAGES)
    assert len(attempts) == 1


def test_timeouts_are_retried_then_reported():
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError, match="after 2 attempts"):
        client_for(handler, retries=1).complete(MESSAGES)
    assert len(attempts) == 2


def test_an_unreachable_server_fails_at_once():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError, match="could not reach"):
        client_for(handler).complete(MESSAGES)


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "   "}}]},
    ],
)
def test_a_reply_without_text_is_an_error(body):
    with pytest.raises(ProviderError):
        client_for(lambda request: httpx.Response(200, json=body)).complete(MESSAGES)


def test_a_reply_cut_off_before_any_text_says_so():
    body = {"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]}

    with pytest.raises(ProviderError) as caught:
        client_for(lambda request: httpx.Response(200, json=body)).complete(MESSAGES)

    assert "did not contain a message" in str(caught.value)
    assert "finish reason 'length'" in str(caught.value)
    assert "message fields ['role']" in str(caught.value)


def test_a_reply_with_an_empty_message_says_why():
    body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}

    with pytest.raises(ProviderError, match="empty.*finish reason 'length'"):
        client_for(lambda request: httpx.Response(200, json=body)).complete(MESSAGES)


def test_a_reply_without_choices_lists_what_it_did_hold():
    body = {"id": "x", "object": "chat.completion", "choices": []}

    with pytest.raises(
        ProviderError, match=r"top-level fields \['choices', 'id', 'object'\]"
    ):
        client_for(lambda request: httpx.Response(200, json=body)).complete(MESSAGES)


def test_a_reply_that_is_not_an_object_is_described():
    with pytest.raises(ProviderError, match="body is a list"):
        client_for(lambda request: httpx.Response(200, json=[1, 2])).complete(MESSAGES)


def test_a_body_that_is_not_json_is_an_error():
    with pytest.raises(ProviderError, match="not JSON"):
        client_for(lambda request: httpx.Response(200, text="<html>")).complete(
            MESSAGES
        )


def test_the_client_uses_geminis_address_and_default_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)

    client = create_client()

    assert client.base_url == DEFAULT_BASE_URL
    assert client.base_url.startswith("https://generativelanguage.googleapis.com/")
    assert client.model == DEFAULT_MODEL


def test_a_missing_key_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ProviderError, match="set the GEMINI_API_KEY environment"):
        create_client()


def test_an_empty_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")

    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        create_client()


def test_the_model_can_be_chosen_by_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-next")

    assert create_client().model == "gemini-next"
    assert create_client(model="explicit").model == "explicit"


def test_the_address_can_be_chosen_by_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gateway.example/v1")

    assert create_client().base_url == "https://gateway.example/v1"


def test_the_key_is_sent_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-secret")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=reply())

    client = create_client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client.complete(MESSAGES)

    assert seen["auth"] == "Bearer AIza-secret"


def test_nothing_in_the_client_refers_to_another_provider():
    import codesync.llm as llm

    source = Path(llm.__file__).read_text(encoding="utf-8").lower()

    assert "deepseek" not in source

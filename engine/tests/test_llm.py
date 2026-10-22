import json

import httpx
import pytest

from codesync.llm import PROVIDERS, ChatClient, ProviderError, create_client

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


def test_retrying_stops_after_the_last_attempt():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    with pytest.raises(ProviderError, match="503: overloaded"):
        client_for(handler, retries=2).complete(MESSAGES)
    assert len(attempts) == 3


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


def test_a_body_that_is_not_json_is_an_error():
    with pytest.raises(ProviderError, match="not JSON"):
        client_for(lambda request: httpx.Response(200, text="<html>")).complete(
            MESSAGES
        )


def test_providers_fill_in_their_defaults(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    client = create_client("deepseek")

    assert client.base_url == PROVIDERS["deepseek"].base_url
    assert client.model == PROVIDERS["deepseek"].model


def test_a_missing_key_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        create_client("gemini")


def test_ollama_needs_no_key():
    client = create_client("ollama", model="qwen2.5-coder:7b")

    assert client.base_url == "http://localhost:11434/v1"
    assert client.model == "qwen2.5-coder:7b"


def test_any_compatible_server_can_be_named_by_url():
    client = create_client("lab", base_url="http://gpu-box:8000/v1", model="local")

    assert client.base_url == "http://gpu-box:8000/v1"


def test_an_unknown_provider_without_a_url_is_rejected():
    with pytest.raises(ProviderError, match="unknown provider"):
        create_client("mystery")

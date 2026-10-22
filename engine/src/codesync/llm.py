"""Talking to a chat model over the OpenAI-compatible API.

DeepSeek, Ollama and Gemini all serve that API, so one client covers them and
choosing a provider is only a matter of base URL, model name and key."""

import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    model: str
    key_env: str | None


PROVIDERS = {
    provider.name: provider
    for provider in (
        Provider(
            "deepseek", "https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY"
        ),
        Provider("ollama", "http://localhost:11434/v1", "deepseek-coder:6.7b", None),
        Provider(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-2.5-flash",
            "GEMINI_API_KEY",
        ),
    )
}


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    latency: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


class ProviderError(Exception):
    pass


class ModelClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, str]]) -> Completion: ...


class ChatClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 120.0,
        retries: int = 2,
        retry_delay: float = 2.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = retries
        self.retry_delay = retry_delay
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=timeout)

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        started = time.perf_counter()
        data = self._post(payload)
        return _completion(data, self.model, time.perf_counter() - started)

    def close(self) -> None:
        self._http.close()

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        attempt = 0
        while True:
            try:
                response = self._http.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as error:
                if attempt >= self.retries:
                    raise ProviderError(
                        f"no answer from {url} after {attempt + 1} attempts"
                    ) from error
            except httpx.HTTPError as error:
                raise ProviderError(f"could not reach {url}: {error}") from error
            else:
                if (
                    response.status_code not in _RETRY_STATUSES
                    or attempt >= self.retries
                ):
                    return _decode(url, response)
            attempt += 1
            time.sleep(self.retry_delay * attempt)


def create_client(
    provider: str = "deepseek",
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> ChatClient:
    """A client for a named provider. Any setting given here wins over the
    provider's default, and an unknown provider works if a base URL and a
    model are supplied."""
    preset = PROVIDERS.get(provider)
    if preset is None and base_url is None:
        known = ", ".join(PROVIDERS)
        raise ProviderError(
            f"unknown provider {provider!r}: use one of {known}, "
            "or give a base URL for another OpenAI-compatible server"
        )

    model = model or (preset.model if preset else None)
    if not model:
        raise ProviderError("a model name is needed for this provider")

    key_env = api_key_env or (preset.key_env if preset else None)
    api_key = None
    if key_env:
        api_key = os.environ.get(key_env)
        if not api_key:
            raise ProviderError(
                f"set the {key_env} environment variable to your API key"
            )

    return ChatClient(
        base_url or preset.base_url,
        model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _decode(url: str, response: httpx.Response) -> dict:
    if response.is_error:
        raise ProviderError(
            f"{url} returned {response.status_code}: {_error_message(response)}"
        )
    try:
        return response.json()
    except ValueError as error:
        raise ProviderError(f"{url} returned a body that is not JSON") from error


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return response.text[:200] or response.reason_phrase
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or response.reason_phrase)


def _completion(data: dict, model: str, latency: float) -> Completion:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError("the reply did not contain a message") from error
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("the reply message was empty")

    usage = data.get("usage") or {}
    return Completion(
        text=text,
        model=data.get("model") or model,
        latency=latency,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        finish_reason=choice.get("finish_reason"),
    )

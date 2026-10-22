"""Talking to Google's Gemini over its OpenAI-compatible API."""

import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_MODEL"
BASE_URL_ENV = "GEMINI_BASE_URL"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-3.8-flash"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_WAIT = 30.0


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
        retries: int = 4,
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

    def list_models(self) -> list[str]:
        """The model names Gemini currently offers, which change often enough
        that a hard-coded default can go stale."""
        url = f"{self.base_url}/models"
        try:
            response = self._http.get(url, headers=self._headers())
        except httpx.TimeoutException as error:
            raise ProviderError(f"no answer from {url}") from error
        except httpx.HTTPError as error:
            raise ProviderError(f"could not reach {url}: {error}") from error

        data = _decode(url, response)
        items = data.get("data") or data.get("models") or []
        names = {_model_name(item) for item in items if isinstance(item, dict)}
        return sorted(name for name in names if name)

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        attempt = 0
        while True:
            response = None
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
            time.sleep(self._wait(attempt, response))

    def _wait(self, attempt: int, response: httpx.Response | None) -> float:
        """How long to pause before a retry: what the provider asked for if it
        said, otherwise a delay that doubles each time."""
        hinted = _retry_after(response)
        delay = hinted if hinted is not None else self.retry_delay * 2 ** (attempt - 1)
        return min(delay, _MAX_WAIT)


def create_client(
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 120.0,
    retries: int = 4,
) -> ChatClient:
    """A Gemini client, using the key in GEMINI_API_KEY.

    The model comes from the argument, then GEMINI_MODEL, then the default.
    GEMINI_BASE_URL sends the requests to another address, such as a gateway."""
    api_key = os.environ.get(KEY_ENV)
    if not api_key:
        raise ProviderError(f"set the {KEY_ENV} environment variable to your API key")

    return ChatClient(
        os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL,
        model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=retries,
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


def _retry_after(response: httpx.Response | None) -> float | None:
    value = response.headers.get("retry-after", "") if response is not None else ""
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _describe_reply(data: object) -> str:
    """What a reply without usable text did contain, so the cause is visible."""
    if not isinstance(data, dict):
        return f"the body is a {type(data).__name__}, not an object"
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        message = choice.get("message")
        fields = sorted(message) if isinstance(message, dict) else "none"
        return f"finish reason {choice.get('finish_reason')!r}, message fields {fields}"
    return f"top-level fields {sorted(data)}"


def _model_name(item: dict) -> str:
    name = str(item.get("id") or item.get("name") or "")
    return name.removeprefix("models/")


def _error_message(response: httpx.Response) -> str:
    """Gemini's own explanation, which it sometimes wraps in a list."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    if isinstance(data, list) and data:
        data = data[0]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or response.text[:200] or response.reason_phrase)


def _completion(data: dict, model: str, latency: float) -> Completion:
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ProviderError(
            f"the reply did not contain a message ({_describe_reply(data)})"
        ) from error
    if not isinstance(text, str) or not text.strip():
        raise ProviderError(f"the reply message was empty ({_describe_reply(data)})")

    usage = data.get("usage") or {}
    return Completion(
        text=text,
        model=data.get("model") or model,
        latency=latency,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        finish_reason=choice.get("finish_reason"),
    )

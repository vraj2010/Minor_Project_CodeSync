"""Talking to a chat model over the OpenAI-compatible API.

Gemini, Groq, DeepSeek and Ollama all serve that API, so one client covers them and a
provider is only an address, a default model and the setting its key is in.
Gemini is the default."""

import logging
import threading
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Protocol

import httpx

from . import envfile
from .envfile import setting

PROVIDER_ENV = "CODESYNC_PROVIDER"
_LOCAL_TIMEOUT = 600.0
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    base_url: str
    model: str
    key_env: str | None
    """The setting the key is read from; None for a server that needs none."""
    local: bool = False
    """A model on this computer, which is slow enough to need a longer wait."""

    @property
    def prefix(self) -> str:
        return self.name.upper()

    @property
    def model_env(self) -> str:
        return f"{self.prefix}_MODEL"

    @property
    def base_url_env(self) -> str:
        return f"{self.prefix}_BASE_URL"

    def key(self) -> str | None:
        return setting(self.key_env) if self.key_env else None

    def configured(self) -> bool:
        """Whether a client could be made without a missing key."""
        return self.key_env is None or bool(self.key())


PROVIDERS = {
    provider.name: provider
    for provider in (
        Provider(
            "gemini",
            "Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "gemini-3.8-flash",
            "GEMINI_API_KEY",
        ),
        Provider(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            "deepseek-chat",
            "DEEPSEEK_API_KEY",
        ),
        Provider(
            "ollama",
            "Ollama",
            "http://localhost:11434/v1",
            "deepseek-coder:6.7b",
            None,
            local=True,
        ),
        Provider(
            "groq",
            "Groq",
            "https://api.groq.com/openai/v1",
            "openai/gpt-oss-120b",
            "GROQ_API_KEY",
        ),
    )
}
DEFAULT_PROVIDER = "gemini"

# Gemini's settings, kept under their old names for the code that uses them.
KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_MODEL"
BASE_URL_ENV = "GEMINI_BASE_URL"
DEFAULT_BASE_URL = PROVIDERS["gemini"].base_url
DEFAULT_MODEL = PROVIDERS["gemini"].model

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
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


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
        provider: Provider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
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
        """The model names the provider currently offers, which change often
        enough that a hard-coded default can go stale."""
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
                        f"no answer from {url} after {attempt + 1} attempts",
                        retryable=True,
                    ) from error
            except httpx.HTTPError as error:
                raise ProviderError(
                    f"could not reach {url}: {error}",
                    retryable=isinstance(
                        error, (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)
                    ),
                ) from error
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


class _Cooldown:
    """Share a primary provider's failure across workers and web jobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0
        self._error: ProviderError | None = None

    def failure(self) -> ProviderError | None:
        with self._lock:
            if time.monotonic() < self._until:
                return self._error
            return None

    def failed(self, error: ProviderError) -> None:
        with self._lock:
            self._until = max(
                self._until, time.monotonic() + max(60.0, error.retry_after or 0.0)
            )
            # Keep the reason, without retaining an exception traceback and its clients.
            self._error = ProviderError(
                str(error), retryable=error.retryable,
                status_code=error.status_code, retry_after=error.retry_after,
            )


@lru_cache(maxsize=32)
def _cooldown_for(base_url: str, model: str, api_key: str | None) -> _Cooldown:
    # A changed key, endpoint or model must not inherit an old configuration's error.
    return _Cooldown()


class FallbackClient:
    """Gemini first, then Groq for quota, service or connection failures.

    Each provider gets one attempt, avoiding stacked retry delays. The model
    name describes the route for jobs and cache keys; each Completion retains
    the actual model that answered. Model discovery always uses the primary.
    """

    def __init__(
        self, primary: ChatClient, fallback: ChatClient, cooldown: _Cooldown
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self._cooldown = cooldown
        self.model = f"{primary.model} -> {fallback.model}"
        self.provider = primary.provider
        self.base_url = primary.base_url

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        started = time.perf_counter()
        primary_error = self._cooldown.failure()
        if primary_error is None:
            try:
                return self.primary.complete(messages)
            except ProviderError as error:
                if not error.retryable:
                    raise
                primary_error = error
                self._cooldown.failed(error)
                _LOG.warning(
                    "Gemini temporarily unavailable (HTTP %s); using Groq fallback",
                    error.status_code or "connection failure",
                )

        try:
            completion = self.fallback.complete(messages)
        except ProviderError as error:
            raise ProviderError(
                f"Gemini unavailable: {primary_error}. "
                f"Groq fallback failed: {error}. "
                "No AI result was produced. If quotas are exhausted, try again "
                "after they reset; otherwise check the provider configuration.",
                retryable=error.retryable,
                status_code=error.status_code,
                retry_after=error.retry_after,
            ) from error
        return replace(completion, latency=time.perf_counter() - started)

    def list_models(self) -> list[str]:
        return self.primary.list_models()

    def close(self) -> None:
        try:
            self.primary.close()
        finally:
            self.fallback.close()


def resolve_provider(name: str | None = None) -> Provider:
    """The provider named, then the one in CODESYNC_PROVIDER, then Gemini."""
    chosen = (name or setting(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    provider = PROVIDERS.get(chosen)
    if provider is None:
        raise ProviderError(
            f"no provider is called {chosen!r}; choose one of "
            + ", ".join(PROVIDERS)
        )
    return provider


def create_client(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 120.0,
    retries: int = 4,
    fallback: bool | None = None,
) -> ChatClient | FallbackClient:
    """A client for one provider, with its key from the environment or from the
    engine's `.env` file. Settings are named after the provider: for Gemini,
    GEMINI_API_KEY, GEMINI_MODEL and GEMINI_BASE_URL.

    The model comes from the argument, then <PROVIDER>_MODEL, then the
    provider's default. <PROVIDER>_BASE_URL sends the requests elsewhere, such
    as a gateway. A model on this computer is given longer to answer.

    Gemini automatically falls back to Groq when GROQ_API_KEY is configured.
    Set CODESYNC_FALLBACK=0, or pass fallback=False, to use one provider only.
    Explicitly choosing another provider never adds a fallback."""
    chosen = resolve_provider(provider)
    api_key = chosen.key()
    if chosen.key_env and not api_key:
        raise ProviderError(
            f"set the {chosen.key_env} environment variable to your API key, "
            f"or put it in {envfile.ENV_FILE}"
        )

    fallback_enabled = fallback if fallback is not None else (
        (setting("CODESYNC_FALLBACK", "1") or "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    groq = PROVIDERS["groq"]
    use_fallback = fallback_enabled and chosen.name == "gemini" and bool(groq.key())
    primary = ChatClient(
        setting(chosen.base_url_env) or chosen.base_url,
        model or setting(chosen.model_env) or chosen.model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=max(timeout, _LOCAL_TIMEOUT) if chosen.local else timeout,
        retries=0 if use_fallback else retries,
        provider=chosen,
    )
    if not use_fallback:
        return primary
    try:
        secondary = ChatClient(
            setting(groq.base_url_env) or groq.base_url,
            setting(groq.model_env) or groq.model,
            api_key=groq.key(),
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=0,
            provider=groq,
        )
        return FallbackClient(
            primary, secondary,
            _cooldown_for(primary.base_url, primary.model, api_key),
        )
    except Exception:
        primary.close()
        raise


def _decode(url: str, response: httpx.Response) -> dict:
    if response.is_error:
        raise ProviderError(
            f"{url} returned {response.status_code}: {_error_message(response)}",
            retryable=response.status_code in _RETRY_STATUSES,
            status_code=response.status_code,
            retry_after=_retry_after(response),
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
    """The provider's own explanation, which Gemini sometimes wraps in a list."""
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

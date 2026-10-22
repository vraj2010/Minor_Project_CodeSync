"""API key authentication.

Every project and job belongs to the key that created it, and the owner id is
a digest of that key, so the key itself is never stored."""

import hmac

from fastapi import HTTPException, Request

from .settings import Settings, digest

ANONYMOUS = "anonymous"


def request_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def authenticate(settings: Settings, key: str | None) -> str:
    """The owner id for a request, or an HTTP error if it may not proceed."""
    if settings.allow_anonymous:
        return ANONYMOUS
    if not settings.api_key_digests:
        raise HTTPException(
            503, "this server has no API keys configured; set CODESYNC_API_KEYS"
        )

    if key:
        candidate = digest(key)
        if any(
            hmac.compare_digest(candidate, known) for known in settings.api_key_digests
        ):
            return candidate[:16]
    raise HTTPException(
        401,
        "a valid API key is required, in the X-API-Key header",
        headers={"WWW-Authenticate": "Bearer"},
    )

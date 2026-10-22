"""Server configuration, read from environment variables."""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

_MB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key_digests: frozenset[str]
    allow_anonymous: bool = False
    max_upload_bytes: int = 25 * _MB
    max_unpacked_bytes: int = 100 * _MB
    max_files: int = 5000
    project_ttl: timedelta = timedelta(hours=24)
    max_projects_per_owner: int = 10
    max_requests_per_job: int = 300
    daily_request_budget: int = 2000
    job_workers: int = 2
    request_workers: int = 4
    cors_origins: tuple[str, ...] = ()
    github_token: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        keys = [item.strip() for item in env.get("CODESYNC_API_KEYS", "").split(",")]
        origins = [
            item.strip() for item in env.get("CODESYNC_CORS_ORIGINS", "").split(",")
        ]
        return cls(
            data_dir=Path(env.get("CODESYNC_DATA_DIR") or ".codesync/server"),
            api_key_digests=frozenset(digest(key) for key in keys if key),
            allow_anonymous=_flag(env, "CODESYNC_ALLOW_ANONYMOUS"),
            max_upload_bytes=_number(env, "CODESYNC_MAX_UPLOAD_MB", 25) * _MB,
            max_unpacked_bytes=_number(env, "CODESYNC_MAX_UNPACKED_MB", 100) * _MB,
            max_files=_number(env, "CODESYNC_MAX_FILES", 5000),
            project_ttl=timedelta(hours=_number(env, "CODESYNC_PROJECT_TTL_HOURS", 24)),
            max_projects_per_owner=_number(env, "CODESYNC_MAX_PROJECTS", 10),
            max_requests_per_job=_number(env, "CODESYNC_MAX_REQUESTS_PER_JOB", 300),
            daily_request_budget=_number(env, "CODESYNC_DAILY_REQUESTS", 2000),
            job_workers=_number(env, "CODESYNC_JOB_WORKERS", 2),
            request_workers=_number(env, "CODESYNC_REQUEST_WORKERS", 4),
            cors_origins=tuple(item for item in origins if item),
            github_token=env.get("GITHUB_TOKEN") or None,
        )


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value

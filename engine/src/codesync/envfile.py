"""Reading settings from the engine's own `.env` file.

The file is always `engine/.env`, wherever a command is run from. Looking in the
current folder instead would let a `.env` inside someone else's project point the
model client, and so the key, at another address.

Values are handed out one at a time and never copied into `os.environ`, so a key
that lives here does not reach the project code the checks run."""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def setting(name: str, default: str | None = None) -> str | None:
    """A setting from the environment, then from the `.env` file. The
    environment wins, so one terminal can override the file for a single run."""
    return os.environ.get(name) or read_env_file(ENV_FILE).get(name) or default


def read_env_file(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in _decode(raw).splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, _, value = text.removeprefix("export ").partition("=")
        values[name.strip()] = _clean(value.strip())
    return values


def _decode(raw: bytes) -> str:
    """Windows editors and PowerShell's `>` save a file in more than one way."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _clean(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.partition(" #")[0].strip()

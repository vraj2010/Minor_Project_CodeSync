"""Repeatable local demo of the verification flow the web UI uses.

    python scripts/demo_verify.py PATH\TO\samples.zip [safe|breaking|unavailable|all]

Uploads the archive to an in-process CodeSync server (anonymous, tests on: this
is a trusted local run), checks hand-written rewrites of
requests._internal_utils.to_native_string with /verify (the backend behind
"Check my version"), and applies the safe one. No model is called."""

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from codesync.server.app import create_app
from codesync.server.settings import Settings

TARGET = "requests._internal_utils.to_native_string"

SAFE = '''def to_native_string(string: str | bytes, encoding: str = "ascii") -> str:
    """Given a string object, regardless of type, returns a representation of
    that string in the native string type, encoding and decoding where
    necessary. This assumes ASCII unless told otherwise.
    """
    if isinstance(string, builtin_str):
        return string
    return string.decode(encoding)
'''
BREAKING = '''def to_native_string(string: str | bytes) -> str:
    """Given a string object, regardless of type, returns a representation of
    that string in the native string type, encoding and decoding where
    necessary. This assumes ASCII unless told otherwise.
    """
    if isinstance(string, builtin_str):
        return string
    return string.decode("ascii")
'''


def make_client(data_dir: Path, run_tests: bool) -> TestClient:
    settings = Settings(
        data_dir=data_dir,
        api_key_digests=frozenset(),
        allow_anonymous=True,
        run_tests=run_tests,
        check_timeout=300,
    )
    return TestClient(create_app(settings))


def upload(client: TestClient, archive: Path) -> str:
    with archive.open("rb") as handle:
        reply = client.post(
            "/v1/projects/upload", files={"file": (archive.name, handle, "application/zip")}
        )
    reply.raise_for_status()
    return reply.json()["id"]


def show(label: str, reply: dict) -> None:
    report = reply["report"]
    print(f"\n== {label}")
    print("verdict:", report["verdict"])
    for check in report["checks"]:
        print(f"  {check['check']:<10} {check['status']:<8} {check.get('detail') or ''}")
        for finding in check.get("findings", [])[:6]:
            print("     -", finding.get("path", ""), finding.get("message", finding)[:110]
                  if isinstance(finding, dict) else finding)
    print("tests run:", report.get("tests_run"), "| keys:", sorted(report))


def main() -> int:
    archive = Path(sys.argv[1])
    case = sys.argv[2] if len(sys.argv) > 2 else "all"
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        if case in ("safe", "breaking", "all"):
            client = make_client(folder / "on", True)
            project = upload(client, archive)
            if case in ("breaking", "all"):
                reply = client.post(
                    f"/v1/projects/{project}/verify", json={"target": TARGET, "code": BREAKING}
                )
                reply.raise_for_status()
                show("BREAKING (encoding removed)", reply.json())
                refused = client.post(
                    f"/v1/projects/{project}/apply", json={"change": reply.json()["change"]}
                )
                print("apply ->", refused.status_code, refused.json())
            if case in ("safe", "all"):
                reply = client.post(
                    f"/v1/projects/{project}/verify", json={"target": TARGET, "code": SAFE}
                )
                reply.raise_for_status()
                show("SAFE (return directly)", reply.json())
                applied = client.post(
                    f"/v1/projects/{project}/apply", json={"change": reply.json()["change"]}
                )
                print("apply ->", applied.status_code)
                source = client.get(f"/v1/projects/{project}/symbols/{TARGET}").json()
                print("engine copy now contains 'out = string':", "out = string" in source.get("source", ""))
        if case in ("unavailable", "all"):
            client = make_client(folder / "off", False)
            project = upload(client, archive)
            reply = client.post(
                f"/v1/projects/{project}/verify", json={"target": TARGET, "code": SAFE}
            )
            reply.raise_for_status()
            show("TESTS UNAVAILABLE (server never runs tests)", reply.json())
            refused = client.post(
                f"/v1/projects/{project}/apply", json={"change": reply.json()["change"]}
            )
            print("apply ->", refused.status_code, refused.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())

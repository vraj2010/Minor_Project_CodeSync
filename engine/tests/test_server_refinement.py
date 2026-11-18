import io
import shutil
import subprocess
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from codesync.llm import ProviderError
from codesync.server.app import create_app
from codesync.server.settings import Settings

KEY = "key-one"
OTHER_KEY = "key-two"
PROJECT = {
    "shop/__init__.py": "",
    "shop/prices.py": (
        "def gross(amount: float, rate: float = 0.2) -> float:\n"
        "    return amount * (1 + rate)\n"
    ),
    "shop/report.py": (
        "from shop.prices import gross\n\n\n"
        "def total(amounts):\n"
        "    return sum(gross(item, rate=0.1) for item in amounts)\n"
    ),
    "tests/test_prices.py": (
        "from shop.prices import gross\n\n\n"
        "def test_gross():\n"
        "    assert gross(100) == 120\n"
    ),
}
SOUND = (
    "def gross(amount: float, rate: float = 0.2) -> float:\n"
    "    return amount + amount * rate\n"
)
BREAKING = "def gross(amount: float, *, tax: float = 0.2) -> float:\n    return amount\n"
MISBEHAVING = (
    "def gross(amount: float, rate: float = 0.2) -> float:\n    return amount * rate\n"
)


def archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in PROJECT.items():
            handle.writestr(name, content)
    return buffer.getvalue()


def wait_for(client, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job}")


@pytest.fixture
def make_client(tmp_path, scripted):
    def build(*replies, **overrides):
        env = {
            "CODESYNC_API_KEYS": f"{KEY},{OTHER_KEY}",
            "CODESYNC_DATA_DIR": str(tmp_path / "data"),
        }
        env.update({name: str(value) for name, value in overrides.items()})
        app = create_app(
            Settings.from_env(env), client_factory=lambda: scripted(*replies)
        )
        return TestClient(app, headers={"X-API-Key": KEY})

    return build


@pytest.fixture
def client(make_client):
    with make_client() as running:
        yield running


def upload(client):
    response = client.post(
        "/v1/projects/upload",
        files={"file": ("repo.zip", archive(), "application/zip")},
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.fixture
def project_id(client):
    return upload(client)


def test_a_server_says_what_it_can_do(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    assert client.get("/v1/capabilities").json() == {
        "runs_tests": False,
        "model_configured": True,
    }


def test_running_tests_is_refused_on_a_shared_server():
    with pytest.raises(ValueError, match="only allowed for local use"):
        Settings.from_env({"CODESYNC_RUN_TESTS": "1", "CODESYNC_API_KEYS": "k"})


def test_running_tests_is_allowed_for_local_use():
    settings = Settings.from_env(
        {"CODESYNC_RUN_TESTS": "1", "CODESYNC_ALLOW_ANONYMOUS": "1"}
    )

    assert settings.run_tests


def test_tests_are_off_unless_asked_for():
    assert not Settings.from_env({"CODESYNC_API_KEYS": "k"}).run_tests


def test_functions_are_listed_without_the_tests(client, project_id):
    data = client.get(f"/v1/projects/{project_id}/symbols").json()

    assert [item["qualname"] for item in data["items"]] == [
        "shop.prices.gross",
        "shop.report.total",
    ]


def test_tests_can_be_listed_on_request(client, project_id):
    data = client.get(
        f"/v1/projects/{project_id}/symbols", params={"include_tests": True}
    ).json()

    assert data["total"] == 3


def test_functions_can_be_searched(client, project_id):
    data = client.get(f"/v1/projects/{project_id}/symbols", params={"q": "GROSS"}).json()

    assert [item["name"] for item in data["items"]] == ["gross"]


def test_one_function_comes_with_its_code_and_contract(client, project_id):
    data = client.get(f"/v1/projects/{project_id}/symbols/shop.prices.gross").json()

    assert data["source"].startswith("def gross(")
    assert data["contract"]["signature"] == "gross(amount, rate=0.2)"


def test_an_unknown_function_is_not_found(client, project_id):
    response = client.get(f"/v1/projects/{project_id}/symbols/shop.nothing")

    assert response.status_code == 404
    assert "nothing in the index" in response.json()["detail"]


def test_the_impact_of_a_change_can_be_asked_for(client, project_id):
    data = client.get(f"/v1/projects/{project_id}/impact/shop.prices.gross").json()

    assert data["files"] == ["shop/report.py"]
    assert data["tests"][0]["nodeid"] == "tests/test_prices.py::test_gross"


def test_the_dependency_map_can_be_asked_for(client, project_id):
    data = client.get(f"/v1/projects/{project_id}/dependencies").json()

    modules = {item["module"]: item for item in data}
    assert modules["shop.report"]["imports"] == ["shop.prices"]


def test_another_key_cannot_see_a_project(client, project_id):
    headers = {"X-API-Key": OTHER_KEY}

    for url in (
        f"/v1/projects/{project_id}/symbols",
        f"/v1/projects/{project_id}/impact/shop.prices.gross",
        f"/v1/projects/{project_id}/dependencies",
    ):
        assert client.get(url, headers=headers).status_code == 404

    response = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": SOUND},
        headers=headers,
    )
    assert response.status_code == 404


def test_a_breaking_rewrite_is_refused_and_the_damage_named(client, project_id):
    data = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": BREAKING},
    ).json()

    assert data["report"]["verdict"] == "unsafe"
    assert data["report"]["would_break"] == ["shop/report.py"]
    assert data["diff"].startswith("--- a/shop/prices.py")


@pytest.mark.slow
def test_the_hosted_service_says_the_tests_were_not_run(client, project_id):
    data = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": SOUND},
    ).json()

    report = data["report"]
    assert report["verdict"] == "unconfirmed"
    assert "never runs uploaded code" in report["notes"][0]
    tests = next(item for item in report["checks"] if item["check"] == "tests")
    assert tests["status"] == "skipped"


@pytest.mark.slow
def test_uploaded_code_is_never_run_by_default(make_client, tmp_path):
    marker = tmp_path / "ran.txt"
    PROJECT["tests/test_prices.py"] += (
        f"\n\nopen({str(marker)!r}, 'w').write('ran')\n"
    )
    try:
        with make_client() as client:
            project_id = upload(client)
            client.post(
                f"/v1/projects/{project_id}/verify",
                json={"target": "shop.prices.gross", "code": SOUND},
            )
    finally:
        PROJECT["tests/test_prices.py"] = PROJECT["tests/test_prices.py"].split("\n\nopen(")[0]

    assert not marker.exists()


@pytest.mark.slow
def test_a_local_server_runs_the_tests(make_client):
    with make_client(CODESYNC_RUN_TESTS=1, CODESYNC_ALLOW_ANONYMOUS=1) as client:
        project_id = upload(client)
        data = client.post(
            f"/v1/projects/{project_id}/verify",
            json={"target": "shop.prices.gross", "code": MISBEHAVING},
        ).json()

    assert data["report"]["verdict"] == "unsafe"
    assert data["report"]["caught_by"] == "tests"


def test_rewriting_nothing_is_reported_as_no_change(client, project_id):
    code = client.get(f"/v1/projects/{project_id}/symbols/shop.prices.gross").json()

    data = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": code["source"]},
    ).json()

    assert data == {"changed": False, "diff": "", "report": None}


def test_a_rewrite_that_does_not_parse_is_explained(client, project_id):
    response = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": "def gross(:\n"},
    )

    assert response.status_code == 422
    assert "cannot be used" in response.json()["detail"]


def test_a_huge_rewrite_is_refused(client, project_id):
    response = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": "x" * 300_000},
    )

    assert response.status_code == 422


@pytest.mark.slow
def test_a_job_retries_and_reports_every_attempt(make_client):
    fenced = [f"```python\n{code}```" for code in (BREAKING, MISBEHAVING, SOUND)]
    with make_client(
        *fenced, CODESYNC_RUN_TESTS=1, CODESYNC_ALLOW_ANONYMOUS=1
    ) as client:
        project_id = upload(client)
        job = client.post(
            f"/v1/projects/{project_id}/improve",
            json={"target": "shop.prices.gross", "goal": "Tidy it."},
        ).json()

        assert job["kind"] == "improve"
        finished = wait_for(client, job["id"])

    result = finished["result"]
    assert finished["status"] == "done"
    assert result["decision"] == "kept"
    assert [item["caught_by"] for item in result["attempts"]] == [
        "contract",
        "tests",
        None,
    ]
    assert result["before"].startswith("def gross(")
    assert "amount + amount * rate" in result["after"]
    assert finished["progress"] == {"done": 3, "total": 4}


def test_a_refusal_keeps_the_code_that_was_turned_down(make_client):
    reply = f"```python\n{BREAKING}```"
    with make_client(reply, reply) as client:
        project_id = upload(client)
        job = client.post(
            f"/v1/projects/{project_id}/improve",
            json={"target": "shop.prices.gross", "fixes": 1},
        ).json()
        finished = wait_for(client, job["id"])

    result = finished["result"]
    assert result["decision"] == "refused"
    assert "tax" in result["after"]
    assert result["report"]["would_break"] == ["shop/report.py"]


def test_a_model_failure_fails_the_job(make_client):
    with make_client(ProviderError("503: overloaded")) as client:
        project_id = upload(client)
        job = client.post(
            f"/v1/projects/{project_id}/improve", json={"target": "shop.prices.gross"}
        ).json()
        finished = wait_for(client, job["id"])

    assert finished["status"] == "failed"
    assert finished["error"] == "503: overloaded"


def test_unused_requests_are_handed_back(make_client):
    reply = f"```python\n{BREAKING}```"
    with make_client(
        reply, reply, reply, reply, CODESYNC_DAILY_REQUESTS=4
    ) as client:
        project_id = upload(client)
        first = client.post(
            f"/v1/projects/{project_id}/improve",
            json={"target": "shop.prices.gross", "fixes": 0},
        ).json()
        wait_for(client, first["id"])

        second = client.post(
            f"/v1/projects/{project_id}/improve",
            json={"target": "shop.prices.gross", "fixes": 2},
        )

    assert second.status_code == 202


def test_a_job_over_the_daily_budget_is_refused(make_client):
    with make_client(CODESYNC_DAILY_REQUESTS=2) as client:
        project_id = upload(client)
        response = client.post(
            f"/v1/projects/{project_id}/improve",
            json={"target": "shop.prices.gross", "fixes": 3},
        )

    assert response.status_code == 429


def test_more_retries_than_the_limit_are_refused(client, project_id):
    response = client.post(
        f"/v1/projects/{project_id}/improve",
        json={"target": "shop.prices.gross", "fixes": 9},
    )

    assert response.status_code == 422


def test_a_missing_model_key_is_explained(tmp_path):
    def no_key():
        raise ProviderError("set the GEMINI_API_KEY environment variable")

    settings = Settings.from_env(
        {"CODESYNC_API_KEYS": KEY, "CODESYNC_DATA_DIR": str(tmp_path / "data")}
    )
    with TestClient(
        create_app(settings, client_factory=no_key), headers={"X-API-Key": KEY}
    ) as client:
        project_id = upload(client)
        response = client.post(
            f"/v1/projects/{project_id}/improve", json={"target": "shop.prices.gross"}
        )

    assert response.status_code == 503


def on_disk(tmp_path, project_id, relpath="shop/prices.py"):
    return tmp_path / "data" / "projects" / project_id / "src" / relpath


def checked(client, project_id, code):
    data = client.post(
        f"/v1/projects/{project_id}/verify",
        json={"target": "shop.prices.gross", "code": code},
    ).json()
    return data["change"], data["report"]["verdict"]


@pytest.mark.slow
def test_a_safe_rewrite_is_applied_and_undone_byte_for_byte(make_client, tmp_path):
    original = PROJECT["shop/prices.py"].encode()
    with make_client(CODESYNC_RUN_TESTS=1, CODESYNC_ALLOW_ANONYMOUS=1) as client:
        project_id = upload(client)
        change, verdict = checked(client, project_id, SOUND)
        assert verdict == "safe"

        applied = client.post(
            f"/v1/projects/{project_id}/apply", json={"change": change}
        )
        assert applied.status_code == 200
        assert applied.json()["message"] == "Changes saved to shop/prices.py"
        assert b"amount + amount * rate" in on_disk(tmp_path, project_id).read_bytes()
        assert [item["target"] for item in applied.json()["history"]] == [
            "shop.prices.gross"
        ]
        symbol = client.get(f"/v1/projects/{project_id}/symbols/shop.prices.gross")
        assert "amount + amount * rate" in symbol.json()["source"]

        undone = client.post(f"/v1/projects/{project_id}/undo")
        assert undone.status_code == 200
        assert undone.json()["history"] == []
        assert on_disk(tmp_path, project_id).read_bytes() == original
        assert client.get(f"/v1/projects/{project_id}/applied").json() == []


@pytest.mark.slow
def test_a_change_checked_before_the_project_changed_is_not_applied(make_client):
    with make_client(CODESYNC_RUN_TESTS=1, CODESYNC_ALLOW_ANONYMOUS=1) as client:
        project_id = upload(client)
        first, _ = checked(client, project_id, SOUND)
        second, _ = checked(client, project_id, SOUND)
        client.post(f"/v1/projects/{project_id}/apply", json={"change": first})

        response = client.post(
            f"/v1/projects/{project_id}/apply", json={"change": second}
        )

    assert response.status_code == 409
    assert "changed since" in response.json()["detail"]


@pytest.mark.slow
def test_a_kept_improvement_can_be_applied(make_client, tmp_path):
    with make_client(
        f"```python\n{SOUND}```", CODESYNC_RUN_TESTS=1, CODESYNC_ALLOW_ANONYMOUS=1
    ) as client:
        project_id = upload(client)
        job = client.post(
            f"/v1/projects/{project_id}/improve", json={"target": "shop.prices.gross"}
        ).json()
        result = wait_for(client, job["id"])["result"]
        response = client.post(
            f"/v1/projects/{project_id}/apply", json={"change": result["change"]}
        )

    assert result["decision"] == "kept"
    assert response.status_code == 200
    assert b"amount + amount * rate" in on_disk(tmp_path, project_id).read_bytes()


@pytest.mark.slow
def test_an_unconfirmed_rewrite_is_not_applied(client, project_id, tmp_path):
    change, verdict = checked(client, project_id, SOUND)

    response = client.post(f"/v1/projects/{project_id}/apply", json={"change": change})

    assert verdict == "unconfirmed"
    assert response.status_code == 409
    assert "unconfirmed" in response.json()["detail"]
    assert on_disk(tmp_path, project_id).read_bytes() == PROJECT["shop/prices.py"].encode()


def test_an_unsafe_rewrite_is_not_applied(client, project_id, tmp_path):
    change, verdict = checked(client, project_id, BREAKING)

    response = client.post(f"/v1/projects/{project_id}/apply", json={"change": change})

    assert verdict == "unsafe"
    assert response.status_code == 409
    assert "unsafe" in response.json()["detail"]
    assert on_disk(tmp_path, project_id).read_bytes() == PROJECT["shop/prices.py"].encode()
    assert client.get(f"/v1/projects/{project_id}/applied").json() == []


def test_an_unknown_change_is_not_found(client, project_id):
    response = client.post(f"/v1/projects/{project_id}/apply", json={"change": "nope"})

    assert response.status_code == 404


def test_undo_with_nothing_applied_is_refused(client, project_id):
    response = client.post(f"/v1/projects/{project_id}/undo")

    assert response.status_code == 409
    assert "no applied change" in response.json()["detail"]


def test_another_key_cannot_apply_a_change(client, project_id):
    change, _ = checked(client, project_id, BREAKING)

    response = client.post(
        f"/v1/projects/{project_id}/apply",
        json={"change": change},
        headers={"X-API-Key": OTHER_KEY},
    )

    assert response.status_code == 404


def test_a_checked_rewrite_downloads_as_a_patch(client, project_id):
    change, _ = checked(client, project_id, BREAKING)

    by_id = client.get(f"/v1/projects/{project_id}/patch", params={"change": change})
    newest = client.get(f"/v1/projects/{project_id}/patch")

    assert by_id.status_code == 200
    assert by_id.text.startswith("--- a/shop/prices.py\n+++ b/shop/prices.py\n")
    assert "+def gross(amount: float, *, tax: float = 0.2) -> float:" in by_id.text
    assert by_id.headers["content-type"].startswith("text/x-diff")
    assert 'filename="shop.prices.gross.patch"' in by_id.headers["content-disposition"]
    assert newest.text == by_id.text


def test_a_patch_needs_a_checked_rewrite(client, project_id):
    assert client.get(f"/v1/projects/{project_id}/patch").status_code == 404
    assert (
        client.get(
            f"/v1/projects/{project_id}/patch", params={"change": "nope"}
        ).status_code
        == 404
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_patch_applies_with_git(client, project_id, tmp_path):
    change, _ = checked(client, project_id, BREAKING)
    patch = client.get(
        f"/v1/projects/{project_id}/patch", params={"change": change}
    ).content
    copy = tmp_path / "copy"
    for name, content in PROJECT.items():
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_bytes(content.encode())
    (tmp_path / "change.patch").write_bytes(patch)

    subprocess.run(
        ["git", "apply", str(tmp_path / "change.patch")], cwd=copy, check=True
    )

    assert "tax: float" in (copy / "shop/prices.py").read_text()

import io
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from codesync.llm import ProviderError
from codesync.server.app import create_app
from codesync.server.settings import Settings

KEY = "key-one"
OTHER_KEY = "key-two"
PROJECT = {
    "app/__init__.py": "",
    "app/config.py": "LIMIT = 3\nNAME = 'shop'\n",
    "app/cart.py": (
        "from app.config import LIMIT\n\n\n"
        "class Cart:\n"
        "    def __init__(self, owner):\n"
        "        self.owner = owner\n"
        "        self.items = []\n\n"
        "    def add(self, item, qty=1):\n"
        "        total = item * qty\n"
        "        return [x for x in self.items if x < LIMIT] + [total]\n"
    ),
}


def archive(files=PROJECT, top="repo-main"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, content in files.items():
            handle.writestr(f"{top}/{name}" if top else name, content)
    return buffer.getvalue()


def upload(client, files=PROJECT, name="repo.zip", top="repo-main", **kwargs):
    return client.post(
        "/v1/projects/upload",
        files={"file": (name, archive(files, top), "application/zip")},
        **kwargs,
    )


def wait_for(client, job_id, *, until=("done", "failed", "cancelled"), timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in until:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {job}")


@pytest.fixture
def make_client(tmp_path, annotator):
    def build(
        *, client_factory=None, github_client=None, keys=(KEY, OTHER_KEY), **overrides
    ):
        env = {
            "CODESYNC_API_KEYS": ",".join(keys),
            "CODESYNC_DATA_DIR": str(tmp_path / "data"),
            "CODESYNC_REQUEST_WORKERS": "1",
        }
        env.update({name: str(value) for name, value in overrides.items()})
        factory = client_factory or (lambda: annotator())
        app = create_app(
            Settings.from_env(env), client_factory=factory, github_client=github_client
        )
        client = TestClient(app, headers={"X-API-Key": KEY})
        return client

    return build


@pytest.fixture
def client(make_client):
    with make_client() as running:
        yield running


@pytest.fixture
def project_id(client):
    return upload(client).json()["id"]


def test_the_service_describes_itself(client):
    assert client.get("/health").json() == {"status": "ok", "version": "0.3.0"}
    assert client.get("/").json()["docs"] == "/docs"
    assert client.get("/openapi.json").status_code == 200


def test_the_docs_page_has_a_place_to_enter_the_key(client):
    spec = client.get("/openapi.json").json()

    scheme = spec["components"]["securitySchemes"]["APIKeyHeader"]

    assert (scheme["type"], scheme["in"], scheme["name"]) == (
        "apiKey",
        "header",
        "X-API-Key",
    )
    assert spec["paths"]["/v1/projects/upload"]["post"]["security"] == [
        {"APIKeyHeader": []}
    ]
    assert "security" not in spec["paths"]["/health"]["get"]


def test_a_request_without_a_key_is_refused(make_client):
    anonymous = TestClient(make_client().app)

    response = anonymous.get("/v1/projects")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_wrong_key_is_refused(make_client):
    other = TestClient(make_client().app, headers={"X-API-Key": "guess"})

    assert other.get("/v1/projects").status_code == 401


def test_a_bearer_token_works_as_well(make_client):
    bearer = TestClient(
        make_client().app, headers={"Authorization": f"Bearer {OTHER_KEY}"}
    )

    assert bearer.get("/v1/projects").status_code == 200


def test_a_server_with_no_keys_says_so(make_client):
    unconfigured = TestClient(make_client(keys=()).app)

    response = unconfigured.get("/v1/projects")

    assert response.status_code == 503
    assert "CODESYNC_API_KEYS" in response.json()["detail"]


def test_anonymous_access_can_be_allowed_for_development(make_client):
    open_client = TestClient(make_client(keys=(), CODESYNC_ALLOW_ANONYMOUS="1").app)

    assert open_client.get("/v1/projects").json() == []


def test_the_health_check_needs_no_key(make_client):
    anonymous = TestClient(make_client().app)

    assert anonymous.get("/health").status_code == 200


def test_a_zip_becomes_a_project(client):
    response = upload(client)

    body = response.json()
    assert response.status_code == 201
    assert body["name"] == "repo"
    assert body["files"] == 3
    assert body["summary"]["variables"] == 11
    assert body["summary"]["by_kind"]["instance"] == 2
    assert body["failures"] == []
    assert body["expires"] > body["created"]


def test_zipping_the_package_folder_itself_keeps_imports_working(client):
    created = upload(client, PROJECT, top=None).json()
    modules = client.get(
        f"/v1/projects/{created['id']}/variables", params={"kind": "module"}
    ).json()

    assert [item["qualname"] for item in modules["items"]] == [
        "app.config.LIMIT",
        "app.config.NAME",
    ]
    assert created["summary"]["cross_file"] == 1


def test_paths_in_a_project_are_relative_to_its_root(client, project_id):
    items = client.get(f"/v1/projects/{project_id}/variables").json()["items"]

    assert {item["path"] for item in items} == {"app/config.py", "app/cart.py"}


def test_server_paths_are_never_exposed(client, project_id, tmp_path):
    for url in (
        f"/v1/projects/{project_id}",
        f"/v1/projects/{project_id}/map",
        "/v1/projects",
    ):
        assert str(tmp_path) not in client.get(url).text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not a zip", "not a valid zip"),
        (archive({"README.md": "hi"}), "no Python files"),
        (archive({"../evil.py": "x = 1"}, top=None), "unsafe path"),
    ],
)
def test_an_unusable_upload_is_a_client_error(client, content, message):
    response = client.post("/v1/projects/upload", files={"file": ("x.zip", content)})

    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_an_oversized_upload_is_refused(make_client):
    small = make_client(CODESYNC_MAX_UPLOAD_MB=1)
    payload = archive({"big.py": "x = '" + "0" * 3_000_000 + "'\n"})

    response = small.post("/v1/projects/upload", files={"file": ("big.zip", payload)})

    assert response.status_code == 413


def test_the_declared_size_is_checked_before_reading(make_client):
    small = make_client(CODESYNC_MAX_UPLOAD_MB=1)

    response = small.post(
        "/v1/projects/upload",
        content=b"x",
        headers={
            "content-length": str(10 * 1024 * 1024),
            "content-type": "multipart/form-data",
        },
    )

    assert response.status_code == 413


def test_projects_belong_to_the_key_that_made_them(make_client):
    mine = make_client()
    created = upload(mine).json()["id"]
    theirs = TestClient(mine.app, headers={"X-API-Key": OTHER_KEY})

    assert theirs.get(f"/v1/projects/{created}").status_code == 404
    assert theirs.get(f"/v1/projects/{created}/variables").status_code == 404
    assert theirs.delete(f"/v1/projects/{created}").status_code == 404
    assert theirs.get("/v1/projects").json() == []
    assert [item["id"] for item in mine.get("/v1/projects").json()] == [created]


def test_a_project_can_be_deleted(client, project_id):
    assert client.delete(f"/v1/projects/{project_id}").status_code == 204

    assert client.get(f"/v1/projects/{project_id}").status_code == 404
    assert client.get("/v1/projects").json() == []


def test_the_number_of_projects_is_limited(make_client):
    limited = make_client(CODESYNC_MAX_PROJECTS=2)
    upload(limited)
    upload(limited)

    response = upload(limited)

    assert response.status_code == 429
    assert "delete one first" in response.json()["detail"]


def test_a_failed_upload_leaves_nothing_behind(make_client, tmp_path):
    client = make_client()
    upload(client, {"README.md": "no python"})

    projects = tmp_path / "data" / "projects"
    assert not projects.exists() or list(projects.iterdir()) == []


def test_variables_are_listed_with_their_usage(client, project_id):
    body = client.get(
        f"/v1/projects/{project_id}/variables", params={"module": "app.config"}
    ).json()

    assert body["total"] == 2
    limit = next(item for item in body["items"] if item["name"] == "LIMIT")
    assert (limit["reads"], limit["writes"]) == (1, 1)
    assert limit["external_files"] == ["app/cart.py"]
    assert limit["static_type"] == "int"
    assert limit["ai"] is None


def test_variables_can_be_filtered(client, project_id):
    def total(**params):
        return client.get(f"/v1/projects/{project_id}/variables", params=params).json()[
            "total"
        ]

    assert total(kind="instance") == 2
    assert total(kind="parameter") == 5
    assert total(container="app.cart.Cart.add") == 5
    assert total(name="owner") == 2
    assert total(q="CONFIG") == 2
    assert total(cross_file="true") == 1
    assert total(annotated="true") == 0
    assert total(kind="module", q="limit") == 1


def test_variables_are_paged(client, project_id):
    first = client.get(
        f"/v1/projects/{project_id}/variables", params={"limit": 4}
    ).json()
    second = client.get(
        f"/v1/projects/{project_id}/variables", params={"limit": 4, "offset": 4}
    ).json()

    assert first["total"] == 11
    assert len(first["items"]) == 4 and len(second["items"]) == 4
    assert {i["qualname"] for i in first["items"]}.isdisjoint(
        i["qualname"] for i in second["items"]
    )


@pytest.mark.parametrize(
    "params", [{"kind": "gizmo"}, {"limit": 0}, {"limit": 501}, {"offset": -1}]
)
def test_bad_query_values_are_rejected(client, project_id, params):
    assert (
        client.get(f"/v1/projects/{project_id}/variables", params=params).status_code
        == 422
    )


def test_one_variable_shows_every_access_with_its_code(client, project_id):
    body = client.get(f"/v1/projects/{project_id}/variables/app.config.LIMIT").json()

    assert body["qualname"] == "app.config.LIMIT"
    assert [(a["mode"], a["path"], a["code"]) for a in body["accesses"]] == [
        (
            "read",
            "app/cart.py",
            "return [x for x in self.items if x < LIMIT] + [total]",
        ),
        ("write", "app/config.py", "LIMIT = 3"),
    ]
    assert body["accesses_truncated"] is False


def test_a_variable_in_a_comprehension_can_be_fetched_by_name(client, project_id):
    listing = client.get(
        f"/v1/projects/{project_id}/variables",
        params={"container": "app.cart.Cart.add"},
    )
    name = next(
        i["qualname"] for i in listing.json()["items"] if "<listcomp" in i["qualname"]
    )

    response = client.get(f"/v1/projects/{project_id}/variables/{name}")

    assert response.status_code == 200
    assert response.json()["qualname"] == name


def test_an_unknown_variable_is_a_404(client, project_id):
    assert (
        client.get(f"/v1/projects/{project_id}/variables/app.nothing").status_code
        == 404
    )


def test_the_whole_map_is_one_document(client, project_id):
    body = client.get(f"/v1/projects/{project_id}/map").json()

    assert body["root"] == "repo"
    assert body["summary"]["variables"] == 11
    assert len(body["variables"]) == 11
    assert "accesses" not in body["variables"][0]
    with_accesses = client.get(
        f"/v1/projects/{project_id}/map", params={"accesses": "true"}
    ).json()
    assert "accesses" in with_accesses["variables"][0]


def test_describing_runs_as_a_job(client, project_id):
    started = client.post(f"/v1/projects/{project_id}/annotate", json={})

    assert started.status_code == 202
    job = wait_for(client, started.json()["id"])

    assert job["status"] == "done"
    assert job["model"] == "annotator-1"
    assert job["progress"] == {
        "done": job["plan"]["requests"],
        "total": job["plan"]["requests"],
    }
    assert job["usage"]["requests"] == job["plan"]["requests"]
    assert job["error"] is None and job["finished"]


def test_describing_needs_no_request_body(client, project_id):
    started = client.post(f"/v1/projects/{project_id}/annotate")

    assert started.status_code == 202
    assert wait_for(client, started.json()["id"])["status"] == "done"


def test_the_descriptions_appear_in_the_map(client, project_id):
    job = client.post(f"/v1/projects/{project_id}/annotate", json={}).json()
    wait_for(client, job["id"])

    summary = client.get(f"/v1/projects/{project_id}").json()["summary"]
    described = client.get(
        f"/v1/projects/{project_id}/variables", params={"annotated": "true"}
    ).json()
    detail = client.get(f"/v1/projects/{project_id}/variables/app.config.LIMIT").json()

    assert summary["annotations"]["ok"] == 11
    assert summary["model"] == "annotator-1"
    assert described["total"] == 11
    assert detail["ai"]["status"] == "ok"
    assert detail["ai"]["purpose"].startswith("Holds the value of LIMIT")


def test_only_chosen_kinds_are_described(client, project_id):
    job = client.post(
        f"/v1/projects/{project_id}/annotate", json={"kinds": ["module"]}
    ).json()
    wait_for(client, job["id"])

    summary = client.get(f"/v1/projects/{project_id}").json()["summary"]

    assert summary["annotations"]["ok"] == 4
    assert summary["annotations"]["skipped"] == 7


def test_an_unchanged_project_is_served_from_the_cache(make_client, annotator):
    model = annotator()
    with make_client(client_factory=lambda: model) as client:
        identifier = upload(client).json()["id"]
        wait_for(
            client,
            client.post(f"/v1/projects/{identifier}/annotate", json={}).json()["id"],
        )
        calls = model.calls

        again = client.post(f"/v1/projects/{identifier}/annotate", json={}).json()
        job = wait_for(client, again["id"])

    assert model.calls == calls
    assert job["usage"]["cached"] == job["plan"]["requests"]


def test_a_project_that_is_already_being_described_refuses_another_job(
    make_client, annotator
):
    slow = annotator(delay=0.3)
    with make_client(client_factory=lambda: slow) as client:
        identifier = upload(client).json()["id"]
        first = client.post(f"/v1/projects/{identifier}/annotate", json={}).json()

        second = client.post(f"/v1/projects/{identifier}/annotate", json={})

        assert second.status_code == 409
        wait_for(client, first["id"])
        assert (
            client.post(f"/v1/projects/{identifier}/annotate", json={}).status_code
            == 202
        )


def test_a_job_can_be_cancelled(make_client, annotator):
    slow = annotator(delay=0.3)
    with make_client(client_factory=lambda: slow) as client:
        identifier = upload(client).json()["id"]
        job = client.post(f"/v1/projects/{identifier}/annotate", json={}).json()

        cancelled = client.delete(f"/v1/jobs/{job['id']}")

        assert cancelled.status_code == 202
        assert wait_for(client, job["id"])["status"] == "cancelled"


def test_a_missing_gemini_key_is_a_503(make_client):
    def unconfigured():
        raise ProviderError(
            "set the GEMINI_API_KEY environment variable to your API key"
        )

    with make_client(client_factory=unconfigured) as client:
        identifier = upload(client).json()["id"]

        response = client.post(f"/v1/projects/{identifier}/annotate", json={})

    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_a_job_is_capped_at_the_servers_request_limit(make_client):
    many = {
        f"m{n}.py": "\n".join(
            f"def f{k}(a, b, c):\n    d = a\n    return d" for k in range(10)
        )
        for n in range(6)
    }
    with make_client(CODESYNC_MAX_REQUESTS_PER_JOB=2) as client:
        identifier = upload(client, many).json()["id"]

        job = client.post(
            f"/v1/projects/{identifier}/annotate", json={"max_requests": 50}
        ).json()
        finished = wait_for(client, job["id"])
        summary = client.get(f"/v1/projects/{identifier}").json()["summary"]

    assert finished["plan"]["requests"] == 2
    assert summary["annotations"]["skipped"] > 0


def test_the_daily_budget_stops_further_runs(make_client):
    with make_client(CODESYNC_DAILY_REQUESTS=1) as client:
        identifier = upload(client).json()["id"]
        wait_for(
            client,
            client.post(f"/v1/projects/{identifier}/annotate", json={}).json()["id"],
        )

        second = client.post(f"/v1/projects/{identifier}/annotate", json={})

    assert second.status_code == 429
    assert "remain today" in second.json()["detail"]


def test_jobs_belong_to_the_key_that_started_them(make_client):
    with make_client() as mine:
        identifier = upload(mine).json()["id"]
        job = mine.post(f"/v1/projects/{identifier}/annotate", json={}).json()
        theirs = TestClient(mine.app, headers={"X-API-Key": OTHER_KEY})

        assert theirs.get(f"/v1/jobs/{job['id']}").status_code == 404
        assert theirs.delete(f"/v1/jobs/{job['id']}").status_code == 404
        wait_for(mine, job["id"])


def test_a_failing_provider_fails_the_job(make_client, annotator):
    broken = annotator(fail=ProviderError("401: invalid api key"))
    with make_client(client_factory=lambda: broken) as client:
        identifier = upload(client).json()["id"]
        job = client.post(f"/v1/projects/{identifier}/annotate", json={}).json()

        finished = wait_for(client, job["id"])
    summary = client.get(f"/v1/projects/{identifier}").json()["summary"]

    assert finished["status"] == "failed"
    assert finished["error"] == "401: invalid api key"
    assert finished["usage"]["failed"] == 1
    assert summary["annotations"]["failed"] == 9
    assert summary["annotations"]["ok"] == 2


def test_a_project_can_be_imported_from_github(make_client):
    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://codeload.github.com/o/r/zip/main"}
            )
        return httpx.Response(200, content=archive())

    mock = httpx.Client(transport=httpx.MockTransport(handler))
    with make_client(github_client=mock) as client:
        response = client.post(
            "/v1/projects/github",
            json={"repository": "https://github.com/octo/widgets.git"},
        )

    assert response.status_code == 201
    assert response.json()["name"] == "widgets"
    assert response.json()["summary"]["variables"] == 11


@pytest.mark.parametrize(
    "repository", ["https://evil.example/a/b", "no-slash", "a/b/c"]
)
def test_only_github_repositories_can_be_imported(client, repository):
    response = client.post("/v1/projects/github", json={"repository": repository})

    assert response.status_code == 400


def test_a_missing_github_repository_is_a_client_error(make_client):
    mock = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    with make_client(github_client=mock) as client:
        response = client.post("/v1/projects/github", json={"repository": "o/missing"})

    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_cross_origin_requests_are_allowed_only_from_configured_origins(make_client):
    with make_client(CODESYNC_CORS_ORIGINS="https://app.example") as client:
        allowed = client.get("/health", headers={"Origin": "https://app.example"})
        denied = client.get("/health", headers={"Origin": "https://other.example"})

    assert allowed.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-origin" not in denied.headers

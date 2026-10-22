from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from codesync.server.auth import ANONYMOUS, authenticate
from codesync.server.jobs import BudgetExceeded, JobManager
from codesync.server.settings import Settings, digest
from codesync.server.store import MARKER, LimitReached, NotFound, ProjectStore

START = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def settings_for(tmp_path, **env):
    return Settings.from_env({"CODESYNC_DATA_DIR": str(tmp_path), **env})


def write_source(destination):
    (destination / "app.py").write_text("X = 1\n", encoding="utf-8")


def test_defaults_suit_a_small_deployment(tmp_path):
    settings = Settings.from_env({})

    assert settings.max_upload_bytes == 25 * 1024 * 1024
    assert settings.project_ttl == timedelta(hours=24)
    assert settings.max_requests_per_job == 300
    assert settings.allow_anonymous is False
    assert settings.api_key_digests == frozenset()
    assert settings.cors_origins == ()


def test_settings_are_read_from_the_environment(tmp_path):
    settings = settings_for(
        tmp_path,
        CODESYNC_API_KEYS="one, two,,",
        CODESYNC_ALLOW_ANONYMOUS="Yes",
        CODESYNC_MAX_UPLOAD_MB="5",
        CODESYNC_PROJECT_TTL_HOURS="2",
        CODESYNC_CORS_ORIGINS="https://a.example, https://b.example",
        GITHUB_TOKEN="ghp_x",
    )

    assert settings.api_key_digests == {digest("one"), digest("two")}
    assert settings.allow_anonymous is True
    assert settings.max_upload_bytes == 5 * 1024 * 1024
    assert settings.project_ttl == timedelta(hours=2)
    assert settings.cors_origins == ("https://a.example", "https://b.example")
    assert settings.github_token == "ghp_x"


def test_api_keys_are_kept_only_as_digests(tmp_path):
    settings = settings_for(tmp_path, CODESYNC_API_KEYS="super-secret")

    assert "super-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("value", "message"),
    [("many", "whole number"), ("0", "at least 1"), ("-3", "at least 1")],
)
def test_a_bad_number_names_the_setting(tmp_path, value, message):
    with pytest.raises(ValueError, match=f"CODESYNC_MAX_FILES.*{message}"):
        settings_for(tmp_path, CODESYNC_MAX_FILES=value)


def test_a_blank_number_falls_back_to_the_default(tmp_path):
    assert settings_for(tmp_path, CODESYNC_MAX_FILES="  ").max_files == 5000


def test_a_known_key_identifies_its_owner(tmp_path):
    settings = settings_for(tmp_path, CODESYNC_API_KEYS="one,two")

    first, second = authenticate(settings, "one"), authenticate(settings, "two")

    assert first != second
    assert first == digest("one")[:16]
    assert authenticate(settings, "one") == first


@pytest.mark.parametrize("key", [None, "", "nope", "ONE"])
def test_anything_else_is_unauthorised(tmp_path, key):
    settings = settings_for(tmp_path, CODESYNC_API_KEYS="one")

    with pytest.raises(HTTPException) as caught:
        authenticate(settings, key)

    assert caught.value.status_code == 401


def test_anonymous_mode_ignores_the_key(tmp_path):
    settings = settings_for(tmp_path, CODESYNC_ALLOW_ANONYMOUS="1")

    assert authenticate(settings, None) == ANONYMOUS
    assert authenticate(settings, "anything") == ANONYMOUS


def test_a_project_expires_after_its_lifetime(tmp_path):
    clock = Clock()
    store = ProjectStore(settings_for(tmp_path, CODESYNC_PROJECT_TTL_HOURS="2"), clock)
    project = store.create("me", "demo", write_source)
    assert project.expires == START + timedelta(hours=2)

    clock.advance(hours=1, minutes=59)
    assert store.get(project.id, "me") is project

    clock.advance(minutes=2)
    with pytest.raises(NotFound):
        store.get(project.id, "me")
    assert not project.path.exists()


def test_the_wrong_owner_cannot_see_a_project(tmp_path):
    store = ProjectStore(settings_for(tmp_path))
    project = store.create("me", "demo", write_source)

    with pytest.raises(NotFound):
        store.get(project.id, "someone-else")
    assert store.list("someone-else") == []
    assert store.list("me") == [project]


def test_the_number_of_projects_per_owner_is_limited(tmp_path):
    store = ProjectStore(settings_for(tmp_path, CODESYNC_MAX_PROJECTS="1"))
    store.create("me", "one", write_source)

    with pytest.raises(LimitReached):
        store.create("me", "two", write_source)
    store.create("you", "theirs", write_source)


def test_a_project_that_cannot_be_built_leaves_no_files(tmp_path):
    store = ProjectStore(settings_for(tmp_path))

    def explode(destination):
        (destination / "half.py").write_text("x = 1")
        raise ValueError("bad archive")

    with pytest.raises(ValueError):
        store.create("me", "demo", explode)

    assert list((tmp_path / "projects").iterdir()) == []
    assert store.list("me") == []


def test_a_sweep_removes_only_what_the_server_made(tmp_path):
    root = tmp_path / "projects"
    ours, theirs = root / "ours", root / "someone-elses"
    ours.mkdir(parents=True)
    theirs.mkdir()
    (ours / MARKER).write_text("ours")
    (theirs / "important.txt").write_text("keep me")

    ProjectStore(settings_for(tmp_path)).sweep()

    assert not ours.exists()
    assert (theirs / "important.txt").read_text() == "keep me"


def test_a_sweep_with_nothing_to_do_is_fine(tmp_path):
    ProjectStore(settings_for(tmp_path)).sweep()


def test_a_projects_describes_itself_without_paths(tmp_path):
    store = ProjectStore(settings_for(tmp_path))
    project = store.create("me", "demo", write_source)

    description = project.to_dict()

    assert description["files"] == 1
    assert description["summary"]["variables"] == 1
    assert str(tmp_path) not in repr(description)


def manager(tmp_path, annotator, clock=None, **env):
    settings = settings_for(tmp_path, **env)
    return settings, JobManager(settings, lambda: annotator(), clock or Clock())


def test_the_daily_budget_resets_the_next_day(tmp_path, annotator):
    clock = Clock()
    settings, jobs = manager(tmp_path, annotator, clock, CODESYNC_DAILY_REQUESTS="3")
    jobs._reserve("me", 2)

    with pytest.raises(BudgetExceeded, match="only 1 remain"):
        jobs._reserve("me", 2)
    jobs._reserve("you", 3)

    clock.advance(days=1)
    jobs._reserve("me", 3)
    jobs.shutdown()


def test_unused_requests_are_given_back(tmp_path, annotator):
    _, jobs = manager(tmp_path, annotator, CODESYNC_DAILY_REQUESTS="5")
    jobs._reserve("me", 5)

    jobs._refund("me", 4)
    jobs._reserve("me", 4)

    with pytest.raises(BudgetExceeded):
        jobs._reserve("me", 1)
    jobs.shutdown()


def test_a_refund_never_goes_below_zero(tmp_path, annotator):
    _, jobs = manager(tmp_path, annotator, CODESYNC_DAILY_REQUESTS="2")

    jobs._refund("me", 10)
    jobs._refund("me", 0)
    jobs._reserve("me", 2)

    with pytest.raises(BudgetExceeded):
        jobs._reserve("me", 1)
    jobs.shutdown()

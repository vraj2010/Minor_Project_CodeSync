import io
import zipfile

import httpx
import pytest

from codesync.server.uploads import (
    Limits,
    UploadError,
    extract_python_files,
    fetch_github_archive,
    parse_repository,
)

LIMITS = Limits(max_files=50, max_unpacked_bytes=1_000_000)


def make_zip(files, *, links=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "target.py")
    buffer.seek(0)
    return buffer


def extract(files, destination, limits=LIMITS, **options):
    return extract_python_files(make_zip(files, **options), destination, limits)


def test_python_files_are_unpacked(tmp_path):
    count = extract({"app.py": "x = 1\n", "pkg/mod.py": "y = 2\n"}, tmp_path)

    assert count == 2
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    assert (tmp_path / "pkg" / "mod.py").read_text() == "y = 2\n"


def test_a_single_top_level_folder_is_stripped(tmp_path):
    extract(
        {
            "owner-repo-1a2b3c/app.py": "x = 1\n",
            "owner-repo-1a2b3c/pkg/m.py": "y = 2\n",
        },
        tmp_path,
    )

    assert (tmp_path / "app.py").is_file()
    assert (tmp_path / "pkg" / "m.py").is_file()
    assert not (tmp_path / "owner-repo-1a2b3c").exists()


def test_a_zipped_package_keeps_its_folder(tmp_path):
    extract({"shop/__init__.py": "", "shop/cart.py": "x = 1\n"}, tmp_path)

    assert (tmp_path / "shop" / "__init__.py").is_file()
    assert (tmp_path / "shop" / "cart.py").is_file()


def test_a_github_wrapper_is_stripped_even_around_a_package(tmp_path):
    files = {"o-r-1a2b/__init__.py": "", "o-r-1a2b/cart.py": "x = 1\n"}

    extract_python_files(make_zip(files), tmp_path, LIMITS, github_wrapper=True)

    assert (tmp_path / "__init__.py").is_file()
    assert (tmp_path / "cart.py").is_file()


def test_a_wrapper_around_a_package_is_stripped_but_the_package_stays(tmp_path):
    extract(
        {"repo-main/shop/__init__.py": "", "repo-main/shop/cart.py": "x = 1\n"},
        tmp_path,
    )

    assert (tmp_path / "shop" / "cart.py").is_file()
    assert not (tmp_path / "repo-main").exists()


def test_a_folder_is_kept_when_there_is_more_than_one(tmp_path):
    extract({"src/app.py": "x = 1\n", "tests/test_app.py": "y = 2\n"}, tmp_path)

    assert (tmp_path / "src" / "app.py").is_file()
    assert (tmp_path / "tests" / "test_app.py").is_file()


def test_only_python_files_and_the_root_gitignore_are_kept(tmp_path):
    extract(
        {
            "repo/app.py": "x = 1\n",
            "repo/.gitignore": "build/\n",
            "repo/sub/.gitignore": "ignored\n",
            "repo/README.md": "# readme\n",
            "repo/data.bin": "binary",
            "repo/docs/Section: Intro.md": "odd name, never extracted",
        },
        tmp_path,
    )

    kept = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )

    assert kept == [".gitignore", "app.py"]


@pytest.mark.parametrize(
    "name",
    [
        "../evil.py",
        "pkg/../../evil.py",
        "..\\evil.py",
        "/etc/evil.py",
        "C:/evil.py",
        "pkg/C:evil.py",
    ],
)
def test_paths_that_escape_the_project_are_refused(tmp_path, name):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(UploadError, match="unsafe path"):
        extract({name: "boom = 1\n", "ok/a.py": "x = 1\n"}, project)

    assert not (tmp_path / "evil.py").exists()
    assert not list(project.rglob("evil.py"))


def test_a_parent_folder_is_never_mistaken_for_the_top_level_folder(tmp_path):
    with pytest.raises(UploadError, match="unsafe path"):
        extract({"../evil.py": "x = 1\n"}, tmp_path)

    assert not (tmp_path / "evil.py").exists()


def test_links_are_ignored(tmp_path):
    extract({"app.py": "x = 1\n"}, tmp_path, links=("link.py",))

    assert [path.name for path in tmp_path.iterdir()] == ["app.py"]


def test_an_archive_without_python_is_refused(tmp_path):
    with pytest.raises(UploadError, match="no Python files"):
        extract({"README.md": "hello"}, tmp_path)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    with pytest.raises(UploadError, match="not a valid zip"):
        extract_python_files(io.BytesIO(b"hello"), tmp_path, LIMITS)


def test_too_many_files_are_refused(tmp_path):
    files = {f"m{n}.py": "x = 1\n" for n in range(6)}

    with pytest.raises(UploadError, match="6 Python files; the limit is 5"):
        extract(files, tmp_path, Limits(max_files=5, max_unpacked_bytes=10**6))


def test_a_zip_bomb_is_stopped_while_unpacking(tmp_path):
    bomb = "x = '" + "0" * 3_000_000 + "'\n"

    with pytest.raises(UploadError, match="too large once unpacked"):
        extract(
            {"big.py": bomb}, tmp_path, Limits(max_files=5, max_unpacked_bytes=500_000)
        )

    assert not (tmp_path / "big.py").exists()


def test_the_unpacked_limit_counts_every_file(tmp_path):
    files = {f"m{n}.py": "x = '" + "0" * 200_000 + "'\n" for n in range(4)}

    with pytest.raises(UploadError, match="too large once unpacked"):
        extract(files, tmp_path, Limits(max_files=10, max_unpacked_bytes=500_000))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pallets/flask", ("pallets", "flask")),
        ("https://github.com/psf/requests", ("psf", "requests")),
        ("https://github.com/psf/requests/", ("psf", "requests")),
        ("https://github.com/psf/requests.git", ("psf", "requests")),
        ("  vraj2010/CodeSync  ", ("vraj2010", "CodeSync")),
        ("a.b/c-d_e", ("a.b", "c-d_e")),
    ],
)
def test_repositories_are_parsed(text, expected):
    assert parse_repository(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "flask",
        "a/b/c",
        "https://evil.example/owner/name",
        "http://github.com/owner/name",
        "file:///etc/passwd",
        "owner/name; rm -rf /",
        "../etc/passwd",
        "",
    ],
)
def test_other_addresses_are_refused(text):
    with pytest.raises(UploadError):
        parse_repository(text)


def archive_bytes():
    return make_zip({"repo-main/app.py": "x = 1\n"}).getvalue()


def github(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_an_archive_is_downloaded_through_github_redirects():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://codeload.github.com/o/r/zip/main"}
            )
        return httpx.Response(200, content=archive_bytes())

    with fetch_github_archive(
        "o/r", None, max_bytes=10**6, client=github(handler)
    ) as archive:
        assert zipfile.ZipFile(archive).namelist() == ["repo-main/app.py"]

    assert [request.url.host for request in seen] == [
        "api.github.com",
        "codeload.github.com",
    ]
    assert str(seen[0].url) == "https://api.github.com/repos/o/r/zipball"


def test_a_ref_is_added_to_the_address():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=archive_bytes())

    fetch_github_archive(
        "o/r", "feature/x-1", max_bytes=10**6, client=github(handler)
    ).close()

    assert seen == ["https://api.github.com/repos/o/r/zipball/feature/x-1"]


def test_the_token_is_not_sent_to_the_redirect_target():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"location": "https://codeload.github.com/o/r/zip/main"}
            )
        return httpx.Response(200, content=archive_bytes())

    fetch_github_archive(
        "o/r", None, max_bytes=10**6, token="secret", client=github(handler)
    ).close()

    assert seen == ["Bearer secret", None]


def test_a_redirect_away_from_github_is_refused():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://evil.example/archive.zip"}
        )

    with pytest.raises(UploadError, match="somewhere unexpected"):
        fetch_github_archive("o/r", None, max_bytes=10**6, client=github(handler))


def test_a_redirect_to_plain_http_is_refused():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "http://codeload.github.com/o/r/zip/main"}
        )

    with pytest.raises(UploadError, match="somewhere unexpected"):
        fetch_github_archive("o/r", None, max_bytes=10**6, client=github(handler))


def test_endless_redirects_are_cut_off():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://api.github.com/loop"})

    with pytest.raises(UploadError, match="too many times"):
        fetch_github_archive("o/r", None, max_bytes=10**6, client=github(handler))


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, "not found, or it is private"),
        (403, "limiting requests"),
        (429, "limiting requests"),
        (500, "status 500"),
    ],
)
def test_github_errors_are_explained(status, message):
    def handler(request):
        return httpx.Response(status)

    with pytest.raises(UploadError, match=message):
        fetch_github_archive("o/r", None, max_bytes=10**6, client=github(handler))


def test_a_download_past_the_limit_is_stopped():
    def handler(request):
        return httpx.Response(200, content=b"0" * 5000)

    with pytest.raises(UploadError, match="larger than"):
        fetch_github_archive("o/r", None, max_bytes=1000, client=github(handler))


@pytest.mark.parametrize("ref", ["../main", "a b", "x;y", "", "a" * 200])
def test_an_unsafe_ref_is_refused(ref):
    with pytest.raises(UploadError, match="ref"):
        fetch_github_archive(
            "o/r",
            ref,
            max_bytes=10**6,
            client=github(lambda request: httpx.Response(200)),
        )


def test_a_network_failure_is_reported():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(UploadError, match="could not reach GitHub"):
        fetch_github_archive("o/r", None, max_bytes=10**6, client=github(handler))

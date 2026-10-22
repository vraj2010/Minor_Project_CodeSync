"""Getting a project onto the server from an archive.

Everything here handles input from strangers. Archives are unpacked only to
read their Python files: nothing in them is ever run. Paths that climb out of
the destination, links, and archives that expand past a limit are refused,
and GitHub is reached only at its own addresses."""

import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import httpx

_CHUNK = 64 * 1024
_MB = 1024 * 1024
_SPOOL_BYTES = 8 * _MB
_SYMLINK = 0o120000
_GITHUB_HOSTS = frozenset({"api.github.com", "codeload.github.com"})
_REPOSITORY = re.compile(
    r"^(?:https://github\.com/)?([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
_MAX_REDIRECTS = 3


class UploadError(ValueError):
    """The archive or repository cannot be used; the message says why."""


@dataclass(frozen=True)
class Limits:
    max_files: int
    max_unpacked_bytes: int


def extract_python_files(
    source: BinaryIO,
    destination: Path,
    limits: Limits,
    *,
    github_wrapper: bool = False,
) -> int:
    """Unpack the `.py` files of a zip archive, and its root `.gitignore`, into
    `destination`. Returns how many Python files were written.

    A single top-level folder is a wrapper, like the `owner-repo-sha` folder of a
    GitHub archive, and is stripped. The exception is a folder that is itself a
    Python package, which a user zipped on purpose, unless the archive is known
    to come from GitHub, where the wrapper is always there."""
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise UploadError("the file is not a valid zip archive") from None

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        prefix = _common_folder(entries)
        if prefix is not None and not github_wrapper and _is_package(entries, prefix):
            prefix = None

        wanted: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in entries:
            relative = _strip_folder(info.filename, prefix)
            if _is_link(info) or not _is_wanted(relative):
                continue
            if _is_unsafe(relative):
                raise UploadError(f"the archive holds an unsafe path: {info.filename}")
            wanted.append((info, Path(*relative.parts)))

        python_files = sum(1 for _, path in wanted if path.suffix == ".py")
        if python_files == 0:
            raise UploadError("the archive contains no Python files")
        if python_files > limits.max_files:
            raise UploadError(
                f"the archive has {python_files} Python files; "
                f"the limit is {limits.max_files}"
            )

        written = 0
        root = destination.resolve()
        for info, path in wanted:
            target = (root / path).resolve()
            if root not in target.parents:
                raise UploadError(
                    f"the archive holds a path outside the project: {info.filename}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            written += _copy(archive, info, target, limits.max_unpacked_bytes - written)
        return python_files


def parse_repository(text: str) -> tuple[str, str]:
    match = _REPOSITORY.match(text.strip())
    if match is None:
        raise UploadError(
            "give a GitHub repository as owner/name or https://github.com/owner/name"
        )
    return match.group(1), match.group(2)


def fetch_github_archive(
    repository: str,
    ref: str | None,
    *,
    max_bytes: int,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> BinaryIO:
    """Download a public repository's zip archive into a temporary file."""
    owner, name = parse_repository(repository)
    if ref is not None and (not _REF.match(ref) or ".." in ref):
        raise UploadError("the ref may only hold letters, digits and . _ - /")

    url = f"https://api.github.com/repos/{owner}/{name}/zipball" + (
        f"/{ref}" if ref else ""
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "codesync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        return _download(http, url, headers, max_bytes)
    except httpx.HTTPError as error:
        raise UploadError(f"could not reach GitHub: {error}") from None
    finally:
        if owned:
            http.close()


def _download(
    http: httpx.Client, url: str, headers: dict[str, str], max_bytes: int
) -> BinaryIO:
    for _ in range(_MAX_REDIRECTS + 1):
        with http.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                url = _redirect_target(response)
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key != "Authorization"
                }
                continue
            _check_status(response)

            spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
            received = 0
            for chunk in response.iter_bytes(_CHUNK):
                received += len(chunk)
                if received > max_bytes:
                    spool.close()
                    raise UploadError(
                        f"the repository archive is larger than {max_bytes // _MB} MB"
                    )
                spool.write(chunk)
            spool.seek(0)
            return spool
    raise UploadError("GitHub redirected too many times")


def _redirect_target(response: httpx.Response) -> str:
    location = response.headers.get("location", "")
    target = (
        httpx.URL(location)
        if location.startswith("http")
        else response.url.join(location)
    )
    if target.scheme != "https" or target.host not in _GITHUB_HOSTS:
        raise UploadError("GitHub redirected somewhere unexpected")
    return str(target)


def _check_status(response: httpx.Response) -> None:
    if response.status_code == 404:
        raise UploadError("the repository was not found, or it is private")
    if response.status_code in (403, 429):
        raise UploadError(
            "GitHub is limiting requests; try again later "
            "or upload a zip archive instead"
        )
    if response.is_error:
        raise UploadError(f"GitHub answered with status {response.status_code}")


def _common_folder(entries: list[zipfile.ZipInfo]) -> str | None:
    firsts = set()
    for info in entries:
        parts = PurePosixPath(info.filename.replace("\\", "/")).parts
        if len(parts) < 2:
            return None
        firsts.add(parts[0])
    if len(firsts) != 1:
        return None
    folder = firsts.pop()
    return None if _is_unsafe(PurePosixPath(folder)) else folder


def _is_package(entries: list[zipfile.ZipInfo], folder: str) -> bool:
    return any(
        PurePosixPath(info.filename.replace("\\", "/")).parts[:2]
        == (folder, "__init__.py")
        for info in entries
    )


def _strip_folder(name: str, prefix: str | None) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if prefix is not None and path.parts[:1] == (prefix,):
        return PurePosixPath(*path.parts[1:])
    return path


def _is_wanted(path: PurePosixPath) -> bool:
    return path.suffix == ".py" or path.as_posix() == ".gitignore"


def _is_unsafe(path: PurePosixPath) -> bool:
    return path.is_absolute() or any(
        part in ("", ".", "..") or ":" in part for part in path.parts
    )


def _is_link(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == _SYMLINK


def _copy(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path, allowance: int
) -> int:
    """Write one member, counting the bytes actually produced rather than
    believing the size the archive claims."""
    size = 0
    with archive.open(info) as source, target.open("wb") as sink:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            if size > allowance:
                sink.close()
                target.unlink()
                raise UploadError("the archive is too large once unpacked")
            sink.write(chunk)
    return size

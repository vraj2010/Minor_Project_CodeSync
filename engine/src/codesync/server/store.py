"""The projects the server holds, kept in memory and on disk until they expire."""

import secrets
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..annotate import AnnotationCache
from ..index import ProjectIndex, build_index
from ..resolve import Resolver
from ..splice import Patch
from ..varmap import VariableMap
from ..varscan import build_variable_table
from .settings import Settings

MARKER = ".codesync-project"
_KEPT_CHANGES = 50


class NotFound(LookupError):
    pass


class LimitReached(Exception):
    pass


@dataclass(frozen=True)
class CheckedChange:
    """A rewrite the checks have looked at, kept so it can be applied or
    downloaded without being sent again. `revision` is the version of the
    project it was checked against."""

    id: str
    target: str
    patch: Patch
    verdict: str | None
    revision: int


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of a project, for work that must not see it change
    halfway through."""

    index: ProjectIndex
    resolver: Resolver
    revision: int


@dataclass
class ProjectState:
    id: str
    owner: str
    name: str
    created: datetime
    expires: datetime
    path: Path
    index: ProjectIndex
    resolver: Resolver
    variable_map: VariableMap
    cache: AnnotationCache
    revision: int = 0
    checked: "OrderedDict[str, CheckedChange]" = field(default_factory=OrderedDict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def journal(self) -> Path:
        """Where applied changes are recorded, outside the uploaded code so it
        never shows up in it."""
        return self.path / "applied"

    def snapshot(self) -> Snapshot:
        with self.lock:
            return Snapshot(self.index, self.resolver, self.revision)

    def remember(
        self, target: str, patch: Patch, verdict: str | None, revision: int
    ) -> CheckedChange:
        change = CheckedChange(
            secrets.token_urlsafe(9), target, patch, verdict, revision
        )
        with self.lock:
            self.checked[change.id] = change
            while len(self.checked) > _KEPT_CHANGES:
                self.checked.popitem(last=False)
        return change

    def checked_change(self, change_id: str | None) -> CheckedChange:
        """A checked change by id, or the newest one when no id is given."""
        with self.lock:
            if change_id is None:
                if not self.checked:
                    raise NotFound("no rewrite has been checked yet")
                return next(reversed(self.checked.values()))
            change = self.checked.get(change_id)
        if change is None:
            raise NotFound(f"no checked rewrite has the id {change_id}")
        return change

    def reload(self) -> None:
        """Read the project again after one of its files changed. Every verdict
        given before was about the old code, so the revision moves on and those
        changes can no longer be applied. Descriptions of variables are kept."""
        index = build_index(self.index.root)
        resolver = Resolver(index)
        table = build_variable_table(index, resolver)
        with self.lock:
            old = self.variable_map
            self.variable_map = VariableMap(
                old.root,
                table,
                annotations=dict(old.annotations),
                usage=old.usage,
                model=old.model,
            )
            self.index = index
            self.resolver = resolver
            self.revision += 1

    def source_line(self, relpath: str, lineno: int) -> str:
        lines = self.resolver.source(relpath).split("\n")
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        return text if len(text) <= 160 else text[:157] + "..."

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created": self.created.isoformat(timespec="seconds"),
            "expires": self.expires.isoformat(timespec="seconds"),
            "files": self.index.file_count,
            "summary": self.variable_map.summary(),
            "failures": [
                {"path": item.relpath, "reason": item.reason, "message": item.message}
                for item in self.index.failures
            ],
        }


class ProjectStore:
    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self._root = settings.data_dir / "projects"
        self._clock = clock
        self._projects: dict[str, ProjectState] = {}
        self._lock = threading.Lock()

    def sweep(self) -> None:
        """Remove what an earlier run left behind. Only folders this server
        made, recognised by their marker file, are touched."""
        if not self._root.is_dir():
            return
        for folder in self._root.iterdir():
            if folder.is_dir() and (folder / MARKER).is_file():
                shutil.rmtree(folder, ignore_errors=True)

    def create(
        self, owner: str, name: str, populate: Callable[[Path], object]
    ) -> ProjectState:
        self.purge_expired()
        with self._lock:
            held = sum(1 for item in self._projects.values() if item.owner == owner)
        if held >= self.settings.max_projects_per_owner:
            raise LimitReached(
                f"{held} projects are already held for this key; delete one first"
            )

        project_id = secrets.token_urlsafe(9)
        path = self._root / project_id
        source = path / "src"
        source.mkdir(parents=True)
        (path / MARKER).write_text(project_id, encoding="utf-8")
        try:
            populate(source)
            index = build_index(source)
            resolver = Resolver(index)
            variable_map = VariableMap(source, build_variable_table(index, resolver))
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

        now = self._clock()
        project = ProjectState(
            id=project_id,
            owner=owner,
            name=name,
            created=now,
            expires=now + self.settings.project_ttl,
            path=path,
            index=index,
            resolver=resolver,
            variable_map=variable_map,
            cache=AnnotationCache(path / "annotations.jsonl"),
        )
        with self._lock:
            self._projects[project_id] = project
        return project

    def get(self, project_id: str, owner: str) -> ProjectState:
        self.purge_expired()
        with self._lock:
            project = self._projects.get(project_id)
        if project is None or project.owner != owner:
            raise NotFound(project_id)
        return project

    def list(self, owner: str) -> list[ProjectState]:
        self.purge_expired()
        with self._lock:
            mine = [item for item in self._projects.values() if item.owner == owner]
        return sorted(mine, key=lambda item: item.created)

    def delete(self, project_id: str, owner: str) -> None:
        project = self.get(project_id, owner)
        self._remove(project)

    def purge_expired(self) -> None:
        now = self._clock()
        with self._lock:
            expired = [item for item in self._projects.values() if item.expires <= now]
        for project in expired:
            self._remove(project)

    def _remove(self, project: ProjectState) -> None:
        with self._lock:
            self._projects.pop(project.id, None)
        shutil.rmtree(project.path, ignore_errors=True)

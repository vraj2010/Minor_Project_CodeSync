"""Annotation jobs: planning a run, holding it to its limits and running it
in the background."""

import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum

from ..annotate import (
    AnnotationOptions,
    AnnotationStatus,
    Plan,
    plan_annotations,
    run_annotations,
)
from ..llm import ModelClient
from ..varmap import parse_kinds
from .settings import Settings
from .store import NotFound, ProjectState

ClientFactory = Callable[[], ModelClient]


class JobConflict(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def active(self) -> bool:
        return self in (JobStatus.QUEUED, JobStatus.RUNNING)


@dataclass
class Job:
    id: str
    project_id: str
    owner: str
    created: datetime
    model: str | None = None
    status: JobStatus = JobStatus.QUEUED
    total_requests: int = 0
    done_requests: int = 0
    plan: dict | None = None
    usage: dict | None = None
    error: str | None = None
    finished: datetime | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "status": self.status.value,
            "model": self.model,
            "progress": {"done": self.done_requests, "total": self.total_requests},
            "plan": self.plan,
            "usage": self.usage,
            "error": self.error,
            "created": self.created.isoformat(timespec="seconds"),
            "finished": self.finished.isoformat(timespec="seconds")
            if self.finished
            else None,
        }


class JobManager:
    def __init__(
        self,
        settings: Settings,
        client_factory: ClientFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._clock = clock
        self._pool = ThreadPoolExecutor(
            max_workers=settings.job_workers, thread_name_prefix="codesync-job"
        )
        self._jobs: dict[str, Job] = {}
        self._spent: dict[tuple[str, date], int] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        project: ProjectState,
        *,
        kinds: list[str] | None = None,
        max_requests: int | None = None,
    ) -> Job:
        """Plan a run and queue it. Raises ProviderError when no Gemini key is
        configured, JobConflict when the project is already being described,
        and BudgetExceeded when the owner has used up the day."""
        client = self._client_factory()
        cap = self.settings.max_requests_per_job
        options = AnnotationOptions(
            kinds=parse_kinds(kinds),
            max_requests=min(max_requests or cap, cap),
        )

        job = Job(
            id=secrets.token_urlsafe(9),
            project_id=project.id,
            owner=project.owner,
            created=self._clock(),
            model=client.model,
        )
        with self._lock:
            if any(
                item.project_id == project.id and item.status.active
                for item in self._jobs.values()
            ):
                client.close()
                raise JobConflict("this project is already being described")
            self._jobs[job.id] = job

        try:
            plan = plan_annotations(
                project.resolver, project.variable_map.table, options
            )
            self._reserve(project.owner, len(plan.requests))
        except BaseException:
            with self._lock:
                self._jobs.pop(job.id, None)
            client.close()
            raise

        job.total_requests = len(plan.requests)
        job.plan = plan.summary()
        self._pool.submit(self._run, job, project, plan, client)
        return job

    def get(self, job_id: str, owner: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise NotFound(job_id)
        return job

    def cancel(self, job_id: str, owner: str) -> Job:
        job = self.get(job_id, owner)
        job.cancel.set()
        return job

    def cancel_project(self, project_id: str) -> None:
        with self._lock:
            active = [
                item
                for item in self._jobs.values()
                if item.project_id == project_id and item.status.active
            ]
        for job in active:
            job.cancel.set()

    def shutdown(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(
        self, job: Job, project: ProjectState, plan: Plan, client: ModelClient
    ) -> None:
        job.status = JobStatus.RUNNING
        try:
            result = run_annotations(
                plan,
                client,
                cache=project.cache,
                workers=self.settings.request_workers,
                progress=lambda done, total: setattr(job, "done_requests", done),
                should_stop=job.cancel.is_set,
            )
        except Exception as error:
            job.status = JobStatus.FAILED
            job.error = f"unexpected {type(error).__name__}: {error}"
            self._refund(job.owner, job.total_requests - job.done_requests)
        else:
            self._finish(job, project, plan, result)
        finally:
            client.close()
            job.finished = self._clock()

    def _finish(self, job: Job, project: ProjectState, plan: Plan, result) -> None:
        merged = dict(project.variable_map.annotations)
        merged.update(result.annotations)
        project.variable_map.annotations = merged
        project.variable_map.usage = result.usage
        project.variable_map.model = job.model
        job.usage = result.usage.to_dict()
        job.done_requests = job.total_requests

        if job.cancel.is_set():
            job.status = JobStatus.CANCELLED
        elif result.stopped:
            job.status = JobStatus.FAILED
            job.error = _first_error(
                result, AnnotationStatus.SKIPPED, "the run stopped early"
            )
        elif job.total_requests and result.usage.failed == job.total_requests:
            job.status = JobStatus.FAILED
            job.error = _first_error(
                result, AnnotationStatus.FAILED, "every request failed"
            )
        else:
            job.status = JobStatus.DONE
        unrun = sum(
            1
            for request in plan.requests
            if result.annotations[request.variables[0].qualname].status
            is AnnotationStatus.SKIPPED
        )
        self._refund(job.owner, unrun)

    def _reserve(self, owner: str, requests: int) -> None:
        today = self._clock().date()
        with self._lock:
            used = self._spent.get((owner, today), 0)
            if used + requests > self.settings.daily_request_budget:
                left = max(self.settings.daily_request_budget - used, 0)
                raise BudgetExceeded(
                    f"this run needs {requests} model requests but only {left} "
                    "remain today"
                )
            self._spent[(owner, today)] = used + requests

    def _refund(self, owner: str, requests: int) -> None:
        if requests <= 0:
            return
        today = self._clock().date()
        with self._lock:
            self._spent[(owner, today)] = max(
                self._spent.get((owner, today), 0) - requests, 0
            )


def _first_error(result, status: AnnotationStatus, default: str) -> str:
    return next(
        (
            item.error
            for item in result.annotations.values()
            if item.status is status and item.error
        ),
        default,
    )

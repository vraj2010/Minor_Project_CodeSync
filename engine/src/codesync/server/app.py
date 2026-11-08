"""The CodeSync web API.

A client uploads a repository (or names a public one on GitHub), browses what
was found in it, and has its functions rewritten and checked. Anything that calls
a hosted model is a background job, because it takes too long for one request.

The service reads uploaded code and never runs it. Rewrites are held to every
check that does not need to run the code, and a report says plainly that the
tests were not run. Only a server started for local use can run them."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from .. import __version__
from ..context import DEFAULT_GOAL
from ..envfile import setting
from ..impact import analyze_impact, dependency_map, is_test
from ..llm import KEY_ENV, ModelClient, ProviderError, create_client
from ..response import ResponseError
from ..safe import DEFAULT_FIXES
from ..splice import SpliceError
from ..symbols import SymbolKind
from ..targets import TargetError, find_refinable, find_symbol
from ..variables import VariableKind
from .auth import authenticate, request_key
from .jobs import BudgetExceeded, JobConflict, JobManager
from .refinement import check_rewrite, describe_symbol, options_for
from .settings import Settings
from .store import LimitReached, NotFound, ProjectState, ProjectStore
from .uploads import Limits, UploadError, extract_python_files, fetch_github_archive

_ACCESS_LIMIT = 500
_UPLOAD_OVERHEAD = 1024 * 1024
_MAX_REWRITE = 200_000


class GithubSource(BaseModel):
    repository: str = Field(description="owner/name, or https://github.com/owner/name")
    ref: str | None = Field(
        default=None, description="branch, tag or commit; default branch if left out"
    )


class AnnotateRequest(BaseModel):
    kinds: list[VariableKind] | None = Field(
        default=None, description="variable kinds to describe; all if left out"
    )
    max_requests: int | None = Field(
        default=None, ge=1, description="cap on model requests"
    )


class VerifyRequest(BaseModel):
    target: str = Field(description="qualified name of the function")
    code: str = Field(
        max_length=_MAX_REWRITE, description="the new definition of the function"
    )


class ImproveRequest(BaseModel):
    target: str = Field(description="qualified name of the function")
    goal: str = Field(
        default=DEFAULT_GOAL, max_length=2000, description="what the rewrite is for"
    )
    fixes: int = Field(
        default=DEFAULT_FIXES, ge=0, le=DEFAULT_FIXES, description="retries allowed"
    )


def default_client_factory() -> ModelClient:
    return create_client(max_tokens=4096)


def create_app(
    settings: Settings | None = None,
    *,
    client_factory=default_client_factory,
    github_client=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = ProjectStore(settings)
    jobs = JobManager(settings, client_factory)
    checks = threading.BoundedSemaphore(settings.concurrent_checks)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.sweep()
        yield
        jobs.shutdown()

    app = FastAPI(
        title="CodeSync",
        version=__version__,
        description="Project-aware code refinement for Python repositories.",
        lifespan=lifespan,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def limit_upload_size(request: Request, call_next):
        if request.url.path == "/v1/projects/upload":
            declared = request.headers.get("content-length", "0")
            if (
                declared.isdigit()
                and int(declared) > settings.max_upload_bytes + _UPLOAD_OVERHEAD
            ):
                return JSONResponse(
                    {"detail": "the upload is too large"}, status_code=413
                )
        return await call_next(request)

    api_key = APIKeyHeader(
        name="X-API-Key",
        auto_error=False,
        description="One of the keys in the server's CODESYNC_API_KEYS",
    )

    def owner(request: Request, key: str | None = Security(api_key)) -> str:
        return authenticate(settings, key or request_key(request))

    def project_of(project_id: str, who: str) -> ProjectState:
        try:
            return store.get(project_id, who)
        except NotFound:
            raise HTTPException(404, "no such project") from None

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": "codesync", "version": __version__, "docs": "/docs"}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/capabilities", tags=["projects"])
    def capabilities(who: str = Depends(owner)) -> dict:
        """What this server can do, so a client can say so up front."""
        return {
            "runs_tests": settings.run_tests,
            "model_configured": bool(setting(KEY_ENV)),
        }

    @app.post("/v1/projects/upload", status_code=201, tags=["projects"])
    def upload_project(file: UploadFile, who: str = Depends(owner)) -> dict:
        """Upload a zip archive of a Python project."""
        size = file.file.seek(0, 2)
        file.file.seek(0)
        if size > settings.max_upload_bytes:
            raise HTTPException(413, "the upload is too large")

        limits = Limits(settings.max_files, settings.max_unpacked_bytes)
        name = Path(file.filename or "upload").stem or "upload"
        return _create(
            who, name, lambda dest: extract_python_files(file.file, dest, limits)
        )

    @app.post("/v1/projects/github", status_code=201, tags=["projects"])
    def import_from_github(source: GithubSource, who: str = Depends(owner)) -> dict:
        """Import a public GitHub repository."""

        def populate(dest: Path) -> None:
            archive = fetch_github_archive(
                source.repository,
                source.ref,
                max_bytes=settings.max_upload_bytes,
                token=settings.github_token,
                client=github_client,
            )
            with archive:
                limits = Limits(settings.max_files, settings.max_unpacked_bytes)
                extract_python_files(archive, dest, limits, github_wrapper=True)

        name = (
            source.repository.strip()
            .rstrip("/")
            .removesuffix(".git")
            .rpartition("/")[2]
        )
        return _create(who, name or "repository", populate)

    def _create(who: str, name: str, populate) -> dict:
        try:
            return store.create(who, name, populate).to_dict()
        except UploadError as error:
            raise HTTPException(400, str(error)) from None
        except LimitReached as error:
            raise HTTPException(429, str(error)) from None

    @app.get("/v1/projects", tags=["projects"])
    def list_projects(who: str = Depends(owner)) -> list[dict]:
        return [project.to_dict() for project in store.list(who)]

    @app.get("/v1/projects/{project_id}", tags=["projects"])
    def get_project(project_id: str, who: str = Depends(owner)) -> dict:
        return project_of(project_id, who).to_dict()

    @app.delete("/v1/projects/{project_id}", status_code=204, tags=["projects"])
    def delete_project(project_id: str, who: str = Depends(owner)) -> Response:
        project_of(project_id, who)
        jobs.cancel_project(project_id)
        store.delete(project_id, who)
        return Response(status_code=204)

    @app.get("/v1/projects/{project_id}/variables", tags=["variables"])
    def list_variables(
        project_id: str,
        kind: VariableKind | None = None,
        module: str | None = None,
        container: str | None = None,
        name: str | None = None,
        q: str | None = Query(None, description="text the qualified name contains"),
        cross_file: bool | None = None,
        annotated: bool | None = None,
        role: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        who: str = Depends(owner),
    ) -> dict:
        """List variables, with their usage and AI description when there is one."""
        variable_map = project_of(project_id, who).variable_map
        found = variable_map.select(
            kind=kind.value if kind else None,
            module=module,
            container=container,
            name=name,
            query=q,
            cross_file=cross_file,
            annotated=annotated,
            role=role,
        )
        page = found[offset : offset + limit]
        return {
            "total": len(found),
            "offset": offset,
            "limit": limit,
            "items": [variable_map.entry(item.qualname) for item in page],
        }

    @app.get("/v1/projects/{project_id}/variables/{qualname:path}", tags=["variables"])
    def get_variable(project_id: str, qualname: str, who: str = Depends(owner)) -> dict:
        """One variable with every place it is read and written."""
        project = project_of(project_id, who)
        entry = project.variable_map.entry(qualname, accesses=True)
        if entry is None:
            raise HTTPException(404, "no such variable")

        accesses = entry["accesses"]
        entry["accesses_truncated"] = len(accesses) > _ACCESS_LIMIT
        entry["accesses"] = accesses[:_ACCESS_LIMIT]
        for access in entry["accesses"]:
            access["code"] = project.source_line(access["path"], access["lineno"])
        return entry

    @app.get("/v1/projects/{project_id}/map", tags=["variables"])
    def get_map(
        project_id: str, accesses: bool = False, who: str = Depends(owner)
    ) -> dict:
        """The whole variable map as one document."""
        project = project_of(project_id, who)
        document = project.variable_map.to_dict(accesses=accesses)
        document["root"] = project.name
        return document

    @app.get("/v1/projects/{project_id}/symbols", tags=["analysis"])
    def list_symbols(
        project_id: str,
        q: str | None = Query(None, description="text the qualified name contains"),
        include_tests: bool = False,
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        who: str = Depends(owner),
    ) -> dict:
        """The functions and methods that can be rewritten."""
        table = project_of(project_id, who).index.table
        needle = (q or "").lower()
        found = [
            symbol
            for symbol in table.symbols
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
            and not symbol.is_nested
            and (include_tests or not is_test(symbol))
            and needle in symbol.qualname.lower()
        ]
        return {
            "total": len(found),
            "items": [item.to_dict() for item in found[offset : offset + limit]],
        }

    @app.get("/v1/projects/{project_id}/symbols/{qualname:path}", tags=["analysis"])
    def get_symbol(project_id: str, qualname: str, who: str = Depends(owner)) -> dict:
        """One function: its code and its contract."""
        project = project_of(project_id, who)
        target = _refinable(project, qualname)
        return describe_symbol(project.resolver, target)

    @app.get("/v1/projects/{project_id}/impact/{qualname:path}", tags=["analysis"])
    def get_impact(project_id: str, qualname: str, who: str = Depends(owner)) -> dict:
        """What breaks if this function changes, and which tests would notice."""
        project = project_of(project_id, who)
        try:
            target = find_symbol(project.index, qualname)
        except TargetError as error:
            raise HTTPException(404, str(error)) from None
        return analyze_impact(project.resolver, target).to_dict()

    @app.get("/v1/projects/{project_id}/dependencies", tags=["analysis"])
    def get_dependencies(project_id: str, who: str = Depends(owner)) -> list[dict]:
        """Which modules import which."""
        resolver = project_of(project_id, who).resolver
        return [item.to_dict() for item in dependency_map(resolver)]

    @app.post("/v1/projects/{project_id}/verify", tags=["refinement"])
    def verify(project_id: str, body: VerifyRequest, who: str = Depends(owner)) -> dict:
        """Check a rewrite written by hand. No model is involved."""
        project = project_of(project_id, who)
        target = _refinable(project, body.target)
        if not checks.acquire(timeout=settings.check_timeout):
            raise HTTPException(429, "too many checks are running; try again shortly")
        try:
            return check_rewrite(project, target, body.code, options_for(settings))
        except (ResponseError, SpliceError, ValueError) as error:
            raise HTTPException(422, f"the rewrite cannot be used: {error}") from None
        finally:
            checks.release()

    @app.post("/v1/projects/{project_id}/improve", status_code=202, tags=["refinement"])
    def improve(
        project_id: str, body: ImproveRequest, who: str = Depends(owner)
    ) -> dict:
        """Start a model rewriting one function, with the checks and retries."""
        project = project_of(project_id, who)
        target = _refinable(project, body.target)
        try:
            job = jobs.submit_improvement(
                project,
                target,
                goal=body.goal,
                fixes=body.fixes,
                options=options_for(settings),
            )
        except ProviderError as error:
            raise HTTPException(503, str(error)) from None
        except JobConflict as error:
            raise HTTPException(409, str(error)) from None
        except BudgetExceeded as error:
            raise HTTPException(429, str(error)) from None
        return job.to_dict()

    def _refinable(project: ProjectState, qualname: str):
        try:
            return find_refinable(project.index, qualname)
        except TargetError as error:
            raise HTTPException(404, str(error)) from None

    @app.post("/v1/projects/{project_id}/annotate", status_code=202, tags=["jobs"])
    def annotate(
        project_id: str,
        body: AnnotateRequest | None = None,
        who: str = Depends(owner),
    ) -> dict:
        """Start describing the project's variables with a hosted model."""
        body = body or AnnotateRequest()
        project = project_of(project_id, who)
        try:
            job = jobs.submit(
                project,
                kinds=[kind.value for kind in body.kinds] if body.kinds else None,
                max_requests=body.max_requests,
            )
        except ProviderError as error:
            raise HTTPException(503, str(error)) from None
        except JobConflict as error:
            raise HTTPException(409, str(error)) from None
        except BudgetExceeded as error:
            raise HTTPException(429, str(error)) from None
        return job.to_dict()

    @app.get("/v1/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str, who: str = Depends(owner)) -> dict:
        try:
            return jobs.get(job_id, who).to_dict()
        except NotFound:
            raise HTTPException(404, "no such job") from None

    @app.delete("/v1/jobs/{job_id}", status_code=202, tags=["jobs"])
    def cancel_job(job_id: str, who: str = Depends(owner)) -> dict:
        """Stop a running job. Requests already in flight finish first."""
        try:
            return jobs.cancel(job_id, who).to_dict()
        except NotFound:
            raise HTTPException(404, "no such job") from None

    return app

"""Import-safe assembly for the bounded REST and MCP gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import (
    build_local_principal_dependency,
    build_rest_principal_dependency,
)
from app.collaboration.audit import AuditLog
from app.collaboration.github import GitHubReadClient, GitHubWriteClient
from app.config import Settings, get_settings
from app.core.check_status import (
    CheckStatusService,
    CredentialUnavailableStatusService,
)
from app.core.open_pr import OpenPrService, WriteDisabledOpenPrService
from app.core.task_claims import TaskClaimService, WriteDisabledTaskClaimService
from app.core.content_search import ContentSearch
from app.core.project_context import ProjectContextAssembler
from app.core.snapshot import Snapshot, validate_snapshot
from app.core.symbol_index import SymbolIndex
from app.core.task_index import TaskIndex
from app.mcp.device_oauth import create_device_provider
from app.mcp.github_oauth import ContributorAuthorizer, create_github_provider
from app.mcp.server import create_mcp_server
from app.rest.content import create_content_router
from app.rest.errors import install_error_contract
from app.rest.health import create_health_router
from app.rest.projects import create_projects_router
from app.rest.symbols import create_symbols_router
from app.rest.tasks import create_tasks_router


@dataclass(frozen=True)
class DataPlane:
    snapshot: Snapshot
    content: ContentSearch
    tasks: TaskIndex
    projects: ProjectContextAssembler
    symbols: SymbolIndex


def build_data_plane(settings: Settings) -> DataPlane:
    snapshot = validate_snapshot(settings.jn_snapshot_path)
    content = ContentSearch(snapshot, search_engine=settings.search_engine)
    tasks = TaskIndex(snapshot)
    symbols = SymbolIndex(snapshot)
    projects = ProjectContextAssembler(snapshot, tasks)
    return DataPlane(
        snapshot=snapshot,
        content=content,
        tasks=tasks,
        projects=projects,
        symbols=symbols,
    )


def _allowed_hosts(settings: Settings) -> list[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    if settings.public_base_url:
        hostname = urlsplit(settings.public_base_url).hostname
        if hostname:
            hosts.add(hostname)
    if settings.app_env == "development":
        hosts.add("testserver")
    return sorted(hosts)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    if settings.service_profile != "engine":
        raise RuntimeError("engine application requires SERVICE_PROFILE=engine")

    data = build_data_plane(settings)
    provider = None
    authorizer: ContributorAuthorizer | None = None
    if settings.auth_mode == "github":
        provider, authorizer = create_github_provider(settings)
    elif settings.auth_mode == "device":
        provider = create_device_provider(settings)

    if provider is None and settings.auth_mode != "authless_local":
        raise RuntimeError("the reviewed contributor authenticator is unavailable")
    if settings.auth_mode == "authless_local":
        require_contributor = build_local_principal_dependency()
        status: CheckStatusService | CredentialUnavailableStatusService = (
            CredentialUnavailableStatusService()
        )
        pr_writes: OpenPrService | WriteDisabledOpenPrService = (
            WriteDisabledOpenPrService()
        )
        task_claims: TaskClaimService | WriteDisabledTaskClaimService = (
            WriteDisabledTaskClaimService()
        )
    else:
        assert provider is not None
        require_contributor = build_rest_principal_dependency(provider)
        status = CheckStatusService(
            GitHubReadClient(settings.github_actions_read_token),
            AuditLog(settings.audit_log_path),
        )
        if settings.enable_write_actions:
            audit = AuditLog(settings.audit_log_path)
            pr_writes = OpenPrService(
                GitHubWriteClient(settings.github_pr_write_token),
                audit,
            )
            task_claims = TaskClaimService(data.tasks, audit)
        else:
            pr_writes = WriteDisabledOpenPrService()
            task_claims = WriteDisabledTaskClaimService()

    mcp_server = create_mcp_server(
        settings,
        auth=provider,
        content=data.content,
        tasks=data.tasks,
        projects=data.projects,
        symbols=data.symbols,
        statuses=status,
        pr_writes=pr_writes,
        task_claims=task_claims,
    )
    mcp_http_app = mcp_server.http_app(
        path=settings.mcp_path,
        transport="streamable-http",
        stateless_http=True,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            async with mcp_http_app.lifespan(mcp_http_app):
                yield
        finally:
            status.close()
            pr_writes.close()
            if authorizer is not None:
                await authorizer.aclose()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_allowed_hosts(settings),
    )
    install_error_contract(application)
    application.state.settings = settings
    application.state.authenticator = provider
    application.state.data_plane = data
    application.state.scaffold_phase = "check-status"
    application.include_router(create_health_router(data.snapshot.manifest.commit))
    application.include_router(
        create_content_router(data.content, require_contributor)
    )
    application.include_router(create_tasks_router(data.tasks, require_contributor))
    application.include_router(
        create_projects_router(data.projects, require_contributor)
    )
    application.include_router(
        create_symbols_router(data.symbols, require_contributor)
    )
    application.mount("/", mcp_http_app)
    return application


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Import-safe assembly for the dedicated gateway-repository MCP service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.gateway_repo.content import GatewayRepositoryContent
from app.gateway_repo.server import create_gateway_repository_mcp_server
from app.gateway_repo.snapshot import GatewaySnapshot, validate_gateway_snapshot
from app.mcp.device_oauth import create_device_provider
from app.mcp.github_oauth import ContributorAuthorizer, create_github_provider
from app.rest.errors import install_error_contract
from app.rest.health import create_health_router


@dataclass(frozen=True)
class GatewayRepositoryDataPlane:
    snapshot: GatewaySnapshot
    content: GatewayRepositoryContent


def build_gateway_repository_data_plane(
    settings: Settings,
) -> GatewayRepositoryDataPlane:
    snapshot = validate_gateway_snapshot(settings.gateway_repo_snapshot_path)
    return GatewayRepositoryDataPlane(
        snapshot=snapshot,
        content=GatewayRepositoryContent(snapshot),
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


def create_gateway_repository_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()
    if settings.service_profile != "gateway_repository":
        raise RuntimeError(
            "gateway repository application requires "
            "SERVICE_PROFILE=gateway_repository"
        )

    data = build_gateway_repository_data_plane(settings)
    provider = None
    authorizer: ContributorAuthorizer | None = None
    if settings.auth_mode == "github":
        provider, authorizer = create_github_provider(settings)
    elif settings.auth_mode == "device":
        provider = create_device_provider(settings)
    if provider is None and settings.auth_mode != "authless_local":
        raise RuntimeError("the reviewed contributor authenticator is unavailable")

    mcp_server = create_gateway_repository_mcp_server(
        auth=provider,
        content=data.content,
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
            if authorizer is not None:
                await authorizer.aclose()

    application = FastAPI(
        title="jn-engine-gateway-development",
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
    application.state.scaffold_phase = "gateway-repository-mcp"
    application.include_router(create_health_router(data.snapshot.manifest.commit))
    application.mount("/", mcp_http_app)
    return application


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    global _app
    if name == "app":
        if _app is None:
            _app = create_gateway_repository_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

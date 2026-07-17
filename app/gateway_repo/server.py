"""Three authenticated read-only tools for developing this gateway repository."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from pydantic import Field

from app.gateway_repo.content import (
    GatewayContentError,
    GatewayInvalidIdError,
    GatewayRepositoryContent,
    GatewayRequestError,
    GatewayStaleIdError,
)
from app.gateway_repo.models import (
    GatewayContextOutput,
    GatewayFetchOutput,
    GatewaySearchOutput,
)
from app.mcp.server import READ_ONLY_ANNOTATIONS

GatewayScope = Literal["all", "source", "docs", "tests", "deploy"]


def create_gateway_repository_mcp_server(
    *,
    auth: AuthProvider | None,
    content: GatewayRepositoryContent,
) -> FastMCP:
    server = FastMCP(
        "jn-engine-gateway-development",
        version="0.1.0",
        instructions=(
            "Read-only access to the immutable jn-engine-contributor-mcp repository. "
            "Use repository_context to orient, then search and fetch commit-bound source."
        ),
        auth=auth,
        mask_error_details=True,
    )

    @server.tool(
        description=(
            "Search the active jn-engine-contributor-mcp snapshot by case-insensitive "
            "literal across source, docs, tests, deployment files, and workflows."
        ),
        output_schema=GatewaySearchOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def search(
        query: Annotated[str, Field(min_length=1, max_length=200)],
        scope: GatewayScope = "all",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> GatewaySearchOutput:
        try:
            return content.search(query, scope=scope, limit=limit)
        except GatewayRequestError as exc:
            raise ToolError("invalid gateway repository search") from exc
        except GatewayContentError as exc:
            raise ToolError("gateway repository content is temporarily unavailable") from exc

    @server.tool(
        description=(
            "Fetch one bounded gateway-repository file by a jng1_ ID returned by "
            "search. Search again after the immutable snapshot changes."
        ),
        output_schema=GatewayFetchOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def fetch(
        id: Annotated[str, Field(min_length=1, max_length=8_192)],
    ) -> GatewayFetchOutput:
        try:
            return content.fetch(id)
        except GatewayStaleIdError as exc:
            raise ToolError("gateway snapshot changed; search again") from exc
        except GatewayInvalidIdError as exc:
            raise ToolError("invalid gateway content ID; search first") from exc
        except GatewayContentError as exc:
            raise ToolError("gateway repository content is temporarily unavailable") from exc

    @server.tool(
        description=(
            "Assemble a bounded implementation briefing from the gateway README, "
            "build spec, MCP contract, security model, deployment guide, and run report."
        ),
        output_schema=GatewayContextOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def repository_context(
        max_chars: Annotated[int, Field(ge=1_000, le=20_000)] = 12_000,
    ) -> GatewayContextOutput:
        try:
            return content.repository_context(max_chars=max_chars)
        except GatewayRequestError as exc:
            raise ToolError("invalid gateway repository context request") from exc
        except GatewayContentError as exc:
            raise ToolError("gateway repository content is temporarily unavailable") from exc

    return server

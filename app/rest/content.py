"""Protected search and fetch REST adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.content_search import (
    ContentSearch,
    SearchEngineError,
    SearchRequestError,
)
from app.core.path_safety import (
    ContentNotFoundError,
    ContentUnavailableError,
    InvalidContentIdError,
    StaleContentIdError,
    UnsafePathError,
)
from app.models.content import (
    FetchOutput,
    RestSearchResponse,
    fetch_output,
    rest_search_output,
)
from app.rest.errors import GatewayHTTPError

ContributorDependency = Callable[..., object]
SearchScope = Literal["all", "source", "docs", "re", "tasks"]


def create_content_router(
    content: ContentSearch,
    require_contributor: ContributorDependency,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_contributor)])

    @router.get(
        "/v1/content/search",
        response_model=RestSearchResponse,
        response_model_exclude_none=True,
    )
    def search(
        q: Annotated[str, Query(min_length=1, max_length=200)],
        scope: SearchScope = "all",
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        include_content: bool = False,
    ) -> RestSearchResponse:
        try:
            result = content.search(
                q,
                scope=scope,
                limit=limit,
                include_content=include_content,
            )
            return rest_search_output(result, content.snapshot)
        except SearchRequestError as exc:
            raise GatewayHTTPError(
                400,
                "invalid_request",
                "search parameters are invalid",
            ) from exc
        except SearchEngineError as exc:
            raise GatewayHTTPError(
                503,
                "temporarily_unavailable",
                "content search is temporarily unavailable",
            ) from exc

    @router.get("/v1/content/fetch", response_model=FetchOutput)
    def fetch(
        id: Annotated[str, Query(min_length=1, max_length=8_192)],
    ) -> FetchOutput:
        try:
            return fetch_output(content.fetch(id))
        except (InvalidContentIdError, UnsafePathError) as exc:
            raise GatewayHTTPError(
                400,
                "invalid_id",
                "content ID is invalid",
            ) from exc
        except StaleContentIdError as exc:
            raise GatewayHTTPError(
                409,
                "snapshot_changed",
                "snapshot changed; search again",
            ) from exc
        except ContentNotFoundError as exc:
            raise GatewayHTTPError(
                404,
                "not_found",
                "content was not found; search again",
            ) from exc
        except ContentUnavailableError as exc:
            raise GatewayHTTPError(
                503,
                "temporarily_unavailable",
                "content is temporarily unavailable",
            ) from exc

    return router

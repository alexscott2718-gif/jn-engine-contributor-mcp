"""Protected project-context REST adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.project_context import (
    ProjectContextAssembler,
    ProjectContextRequestError,
)
from app.models.projects import ProjectContextOutput, project_context_output
from app.rest.errors import GatewayHTTPError

ContributorDependency = Callable[..., object]


def create_projects_router(
    projects: ProjectContextAssembler,
    require_contributor: ContributorDependency,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_contributor)])

    @router.get("/v1/projects/context", response_model=ProjectContextOutput)
    def project_context(
        max_chars: Annotated[int, Query(ge=1_000, le=20_000)] = 12_000,
    ) -> ProjectContextOutput:
        try:
            result = projects.build(max_chars=max_chars)
            return project_context_output(result, projects.snapshot)
        except ProjectContextRequestError as exc:
            raise GatewayHTTPError(
                400,
                "invalid_request",
                "project context parameters are invalid",
            ) from exc

    return router

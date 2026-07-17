"""Public liveness route with bounded snapshot identity."""

from __future__ import annotations

from fastapi import APIRouter

from app.models.common import HealthResponse


def create_health_router(commit: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(commit=commit)

    return router

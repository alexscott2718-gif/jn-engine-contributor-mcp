"""Sanitized request IDs and the frozen REST error contract."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.common import ErrorResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayHTTPError(Exception):
    status_code: int
    code: str
    detail: str
    headers: dict[str, str] | None = None


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        code=code,
        detail=detail,
        request_id=_request_id(request),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def install_error_contract(application: FastAPI) -> None:
    @application.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(GatewayHTTPError)
    async def gateway_error(request: Request, exc: GatewayHTTPError) -> JSONResponse:
        return _response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            detail=exc.detail,
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _response(
            request,
            status_code=400,
            code="invalid_request",
            detail="request parameters are invalid",
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == 401:
            return _response(
                request,
                status_code=401,
                code="unauthenticated",
                detail="valid contributor authentication is required",
                headers=exc.headers,
            )
        if exc.status_code == 403:
            return _response(
                request,
                status_code=403,
                code="forbidden",
                detail="repository collaborator access is required",
                headers=exc.headers,
            )
        if exc.status_code == 503:
            return _response(
                request,
                status_code=503,
                code="auth_dependency_unavailable",
                detail="contributor authorization is temporarily unavailable",
                headers=exc.headers,
            )
        if exc.status_code == 404:
            return _response(
                request,
                status_code=404,
                code="not_found",
                detail="resource not found",
                headers=exc.headers,
            )
        return _response(
            request,
            status_code=exc.status_code,
            code="invalid_request",
            detail="request cannot be processed",
            headers=exc.headers,
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        logger.error(
            "request failed request_id=%s category=unexpected",
            _request_id(request),
        )
        return _response(
            request,
            status_code=500,
            code="temporarily_unavailable",
            detail="request is temporarily unavailable",
        )

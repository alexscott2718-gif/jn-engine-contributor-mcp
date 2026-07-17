"""Typed collaboration errors and their sanitized MCP representation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from fastmcp.exceptions import ToolError

CollaborationErrorCode = Literal[
    "credential_unavailable",
    "upstream_unavailable",
    "bad_args",
    "not_found",
    "write_disabled",
    "conflict",
]


@dataclass(frozen=True)
class CollaborationError(Exception):
    code: CollaborationErrorCode
    detail: str


def credential_unavailable() -> CollaborationError:
    return CollaborationError(
        "credential_unavailable",
        "the read-only GitHub credential is unavailable",
    )


def upstream_unavailable() -> CollaborationError:
    return CollaborationError(
        "upstream_unavailable",
        "GitHub status data is temporarily unavailable",
    )


def bad_args(detail: str) -> CollaborationError:
    return CollaborationError("bad_args", detail)


def not_found() -> CollaborationError:
    return CollaborationError(
        "not_found",
        "the requested pull request, branch, or commit was not found",
    )


def write_disabled() -> CollaborationError:
    return CollaborationError(
        "write_disabled",
        "pull-request writes are disabled on this deployment",
    )


def conflict(detail: str) -> CollaborationError:
    return CollaborationError("conflict", detail)


def tool_error(error: CollaborationError) -> ToolError:
    """Preserve a machine-readable code without leaking an upstream response."""
    payload = {"code": error.code, "detail": error.detail}
    return ToolError(json.dumps(payload, separators=(",", ":"), sort_keys=True))

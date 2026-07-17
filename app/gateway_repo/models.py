"""Typed outputs for the dedicated gateway-development MCP."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models.common import OutputModel


class GatewaySearchResult(OutputModel):
    id: str
    title: str
    url: str


class GatewaySearchOutput(OutputModel):
    results: list[GatewaySearchResult] = Field(max_length=50)


class GatewayFetchMetadata(OutputModel):
    path: str
    kind: Literal["source", "documentation", "test", "deployment"]
    language: str | None
    repository: str
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    text_chars: int = Field(ge=0, le=200_000)
    truncated: bool


class GatewayFetchOutput(OutputModel):
    id: str
    title: str
    text: str = Field(max_length=200_000)
    url: str
    metadata: GatewayFetchMetadata


class GatewayContextOutput(OutputModel):
    repository: str
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    context: str = Field(max_length=20_000)
    important_files: list[str] = Field(max_length=10)

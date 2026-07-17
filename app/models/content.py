"""Frozen content search and fetch response models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.content_search import FetchRecord, SearchResponse
from app.core.snapshot import Snapshot
from app.models.common import OutputModel, SnapshotRef, snapshot_ref

ContentKind = Literal["source", "documentation", "reverse_engineering", "task"]


class Match(OutputModel):
    line: int = Field(ge=1)
    text: str = Field(max_length=500)


class RestSearchResult(OutputModel):
    id: str
    path: str
    title: str
    kind: ContentKind
    matches: list[Match]
    url: str
    content: str | None = Field(default=None, max_length=10_000)


class RestSearchResponse(OutputModel):
    query: str = Field(min_length=1, max_length=200)
    scope: Literal["all", "source", "docs", "re", "tasks"]
    snapshot: SnapshotRef
    count: int = Field(ge=0, le=50)
    results: list[RestSearchResult] = Field(max_length=50)


class SearchToolResult(OutputModel):
    id: str
    title: str
    url: str


class SearchToolOutput(OutputModel):
    results: list[SearchToolResult] = Field(max_length=50)


class FetchMetadata(OutputModel):
    path: str
    kind: ContentKind
    language: str | None
    repository: str
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    text_chars: int = Field(ge=0, le=200_000)
    truncated: bool


class FetchOutput(OutputModel):
    id: str
    title: str
    text: str = Field(max_length=200_000)
    url: str
    metadata: FetchMetadata


def rest_search_output(
    response: SearchResponse,
    snapshot: Snapshot,
) -> RestSearchResponse:
    return RestSearchResponse(
        query=response.query,
        scope=response.scope.value,
        snapshot=snapshot_ref(snapshot),
        count=response.count,
        results=[
            RestSearchResult(
                id=result.id,
                path=result.path,
                title=result.title,
                kind=result.kind.value,
                matches=[
                    Match(line=match.line, text=match.text)
                    for match in result.matches
                ],
                url=result.url,
                content=result.content,
            )
            for result in response.results
        ],
    )


def search_tool_output(response: SearchResponse) -> SearchToolOutput:
    return SearchToolOutput(
        results=[
            SearchToolResult(id=result.id, title=result.title, url=result.url)
            for result in response.results
        ]
    )


def fetch_output(record: FetchRecord) -> FetchOutput:
    return FetchOutput(
        id=record.id,
        title=record.title,
        text=record.text,
        url=record.url,
        metadata=FetchMetadata(
            path=record.path,
            kind=record.kind.value,
            language=record.language,
            repository=record.repository,
            ref=record.ref,
            commit=record.commit,
            text_chars=record.text_chars,
            truncated=record.truncated,
        ),
    )

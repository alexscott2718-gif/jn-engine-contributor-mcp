"""Frozen structured-symbol response models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.snapshot import Snapshot
from app.core.symbol_index import SymbolLookup
from app.models.common import (
    OutputModel,
    SnapshotRef,
    SourceRef,
    snapshot_ref,
    source_ref,
)


class SymbolQuery(OutputModel):
    name: str | None
    address: str | None
    class_name: str | None
    fourcc: str | None


class LinkageRecord(OutputModel):
    aspect: str
    domain: str
    status: Literal["linked", "linked-blocked"]
    oracle: str | None
    source: SourceRef


class SymbolRecord(OutputModel):
    kind: Literal["class", "function", "fourcc"]
    name: str
    address: str | None
    signature: str | None
    class_name: str | None
    fourcc: str | None
    status: str | None
    linkage: list[LinkageRecord] = Field(max_length=10)
    summary: str | None = Field(default=None, max_length=1_000)
    source: SourceRef


class SymbolLookupOutput(OutputModel):
    query: SymbolQuery
    snapshot: SnapshotRef
    count: int = Field(ge=0, le=50)
    results: list[SymbolRecord] = Field(max_length=50)


def symbol_lookup_output(
    lookup: SymbolLookup,
    snapshot: Snapshot,
) -> SymbolLookupOutput:
    return SymbolLookupOutput(
        query=SymbolQuery(
            name=lookup.query.name,
            address=lookup.query.address,
            class_name=lookup.query.class_name,
            fourcc=lookup.query.fourcc,
        ),
        snapshot=snapshot_ref(snapshot),
        count=lookup.count,
        results=[
            SymbolRecord(
                kind=record.kind.value,
                name=record.name,
                address=record.address,
                signature=record.signature,
                class_name=record.class_name,
                fourcc=record.fourcc,
                status=record.status,
                linkage=[
                    LinkageRecord(
                        aspect=linkage.aspect,
                        domain=linkage.domain,
                        status=linkage.status.value,
                        oracle=linkage.oracle,
                        source=source_ref(
                            path=linkage.source.path,
                            line=linkage.source.line,
                            url=linkage.source.url,
                        ),
                    )
                    for linkage in record.linkage
                ],
                summary=record.summary,
                source=source_ref(
                    path=record.source.path,
                    line=record.source.line,
                    url=record.source.url,
                ),
            )
            for record in lookup.results
        ],
    )

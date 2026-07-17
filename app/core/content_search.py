"""Bounded literal search and fetch over one immutable snapshot.

The Python and ripgrep engines only identify matching line numbers. A shared
post-processing path reopens inventoried files, rechecks each literal match, and
builds deterministic output, keeping both engines byte-equivalent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from app.config import EXPECTED_REF, EXPECTED_REPOSITORY
from app.core.path_safety import (
    ContentUnavailableError,
    citation_url,
    encode_content_id,
    language_for_path,
    read_text_bounded,
    resolve_content_id,
    title_from_text,
)
from app.core.snapshot import ALLOWED_SUFFIXES, MAX_FILE_BYTES, Snapshot, SnapshotFile

MAX_QUERY_CHARS = 200
DEFAULT_SEARCH_RESULTS = 20
MAX_SEARCH_RESULTS = 50
MAX_MATCHES_PER_FILE = 20
MAX_MATCH_TEXT_CHARS = 500
MAX_SEARCH_CONTENT_CHARS = 10_000
RIPGREP_TIMEOUT_SECONDS = 10

TASK_SOURCE_PATHS = frozenset(
    (
        "docs/decomp/_next_session.md",
        "docs/qa/qa_backlog_campaign_handoff.md",
        "docs/linkage_certificates.csv",
        "docs/decomp_ledger.csv",
        "docs/asset_catalog/behavior_todo.md",
    )
)


class SearchScope(StrEnum):
    ALL = "all"
    SOURCE = "source"
    DOCS = "docs"
    RE = "re"
    TASKS = "tasks"


class ContentKind(StrEnum):
    SOURCE = "source"
    DOCUMENTATION = "documentation"
    REVERSE_ENGINEERING = "reverse_engineering"
    TASK = "task"


class SearchRequestError(ValueError):
    """Search input is outside the frozen request bounds."""


class SearchEngineError(RuntimeError):
    """The explicitly selected ripgrep engine could not complete safely."""


@dataclass(frozen=True)
class SearchMatch:
    line: int
    text: str


@dataclass(frozen=True)
class SearchResult:
    id: str
    path: str
    title: str
    kind: ContentKind
    matches: tuple[SearchMatch, ...]
    url: str
    content: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    query: str
    scope: SearchScope
    count: int
    results: tuple[SearchResult, ...]


@dataclass(frozen=True)
class FetchRecord:
    id: str
    title: str
    text: str
    url: str
    path: str
    kind: ContentKind
    language: str | None
    repository: str
    ref: str
    commit: str
    text_chars: int
    truncated: bool


@dataclass(frozen=True)
class ClassifiedFile:
    item: SnapshotFile
    kind: ContentKind
    scopes: frozenset[SearchScope]


def _is_re_path(path: str) -> bool:
    return (
        path.startswith("docs/decomp/")
        or path == "docs/decomp_ledger.csv"
        or path == "docs/_gam_classids.tsv"
        or path.startswith("docs/ghidra")
        or path == "docs/gam_schema.md"
        or path.startswith("docs/linkage")
        or path.startswith("docs/vtable")
        or path.startswith("tools/ghidra/")
        or path.startswith("tools/linkage_oracles/")
        or path == "instrument/proxy/protocol.h"
    )


def classify_file(item: SnapshotFile) -> ClassifiedFile:
    path = item.relative_path
    scopes: set[SearchScope] = {SearchScope.ALL}
    is_task = path in TASK_SOURCE_PATHS
    is_re = _is_re_path(path)
    is_source = (
        path == "Makefile"
        or path.startswith("src/")
        or path.startswith("instrument/")
        or path.startswith("tools/")
        or path.startswith("web/")
    )
    if is_source:
        scopes.add(SearchScope.SOURCE)
    if is_re:
        scopes.add(SearchScope.RE)
    if is_task:
        scopes.add(SearchScope.TASKS)
    if (
        path in {"AGENTS.md", "README.md"}
        or (path.startswith("docs/") and not is_task and not is_re)
    ):
        scopes.add(SearchScope.DOCS)

    if is_task:
        kind = ContentKind.TASK
    elif is_re:
        kind = ContentKind.REVERSE_ENGINEERING
    elif path in {"AGENTS.md", "README.md"} or path.startswith("docs/"):
        kind = ContentKind.DOCUMENTATION
    else:
        kind = ContentKind.SOURCE
    return ClassifiedFile(item=item, kind=kind, scopes=frozenset(scopes))


class ContentSearch:
    """One process-lifetime view shared by REST and MCP adapters."""

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        search_engine: Literal["auto", "ripgrep", "python"] = "auto",
    ) -> None:
        self.snapshot = snapshot
        self.search_engine = search_engine
        self._files = tuple(classify_file(item) for item in snapshot.files)
        self._by_path = {entry.item.relative_path: entry for entry in self._files}

    @property
    def files(self) -> tuple[ClassifiedFile, ...]:
        return self._files

    def _selected_files(self, scope: SearchScope) -> tuple[ClassifiedFile, ...]:
        return tuple(entry for entry in self._files if scope in entry.scopes)

    @staticmethod
    def _validate_query(query: str) -> str:
        clean = query.strip()
        if not clean or len(clean) > MAX_QUERY_CHARS:
            raise SearchRequestError("query must contain 1..200 characters after trimming")
        return clean

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise SearchRequestError("limit must be in 1..50")
        return limit

    @staticmethod
    def _scope(value: SearchScope | str) -> SearchScope:
        try:
            return SearchScope(value)
        except ValueError as exc:
            raise SearchRequestError("scope must be all, source, docs, re, or tasks") from exc

    def _python_line_map(
        self,
        query: str,
        selected: tuple[ClassifiedFile, ...],
    ) -> dict[str, list[int]]:
        needle = query.casefold()
        line_map: dict[str, list[int]] = {}
        for entry in selected:
            text, _ = read_text_bounded(
                self.snapshot,
                entry.item,
                max_chars=MAX_FILE_BYTES,
            )
            matches: list[int] = []
            for line_number, line in enumerate(text.split("\n"), start=1):
                if needle in line.casefold():
                    matches.append(line_number)
                    if len(matches) >= MAX_MATCHES_PER_FILE:
                        break
            if matches:
                line_map[entry.item.relative_path] = matches
        return line_map

    def _ripgrep_line_map(
        self,
        query: str,
        selected: tuple[ClassifiedFile, ...],
    ) -> dict[str, list[int]]:
        executable = shutil.which("rg")
        if executable is None:
            raise SearchEngineError("ripgrep is unavailable")
        root = self.snapshot.content_root.resolve(strict=True)
        command = [
            executable,
            "--json",
            "--ignore-case",
            "--fixed-strings",
            "--no-ignore",
            "--max-count",
            str(MAX_MATCHES_PER_FILE),
            "--max-filesize",
            str(MAX_FILE_BYTES),
        ]
        for suffix in sorted(ALLOWED_SUFFIXES):
            command.extend(("--iglob", f"*{suffix}"))
        command.extend(("--iglob", "Makefile", "-e", query, "--", str(root)))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RIPGREP_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SearchEngineError("ripgrep execution failed") from exc
        if completed.returncode not in (0, 1):
            raise SearchEngineError("ripgrep returned an unexpected status")

        selected_paths = {
            entry.item.relative_path: entry for entry in selected
        }
        line_map: dict[str, list[int]] = {}
        for raw_event in completed.stdout.splitlines():
            try:
                event = json.loads(raw_event)
                if event.get("type") != "match":
                    continue
                data = event["data"]
                path_text = data["path"]["text"]
                resolved = Path(path_text).resolve(strict=True)
                relative = resolved.relative_to(root).as_posix()
                line_number = int(data["line_number"])
            except (
                KeyError,
                TypeError,
                ValueError,
                OSError,
                json.JSONDecodeError,
            ):
                continue
            entry = selected_paths.get(relative)
            if entry is None:
                continue
            # Reapply all path, inode, and containment checks before accepting rg output.
            resolve_content_id(
                self.snapshot,
                encode_content_id(
                    self.snapshot.manifest.commit,
                    entry.item.relative_path,
                ),
            )
            numbers = line_map.setdefault(relative, [])
            if line_number not in numbers and len(numbers) < MAX_MATCHES_PER_FILE:
                numbers.append(line_number)
        for numbers in line_map.values():
            numbers.sort()
        return line_map

    def _line_map(
        self,
        query: str,
        selected: tuple[ClassifiedFile, ...],
        engine: Literal["auto", "ripgrep", "python"] | None,
    ) -> dict[str, list[int]]:
        choice = engine or self.search_engine
        if choice not in {"auto", "ripgrep", "python"}:
            raise SearchRequestError("search engine is invalid")
        if choice == "python":
            return self._python_line_map(query, selected)
        if choice == "auto" and shutil.which("rg") is None:
            return self._python_line_map(query, selected)
        try:
            return self._ripgrep_line_map(query, selected)
        except SearchEngineError:
            if choice == "ripgrep":
                raise
            return self._python_line_map(query, selected)

    def search(
        self,
        query: str,
        *,
        scope: SearchScope | str = SearchScope.ALL,
        limit: int = DEFAULT_SEARCH_RESULTS,
        include_content: bool = False,
        engine: Literal["auto", "ripgrep", "python"] | None = None,
    ) -> SearchResponse:
        clean_query = self._validate_query(query)
        bounded_limit = self._validate_limit(limit)
        selected_scope = self._scope(scope)
        selected = self._selected_files(selected_scope)
        line_map = self._line_map(clean_query, selected, engine)
        needle = clean_query.casefold()
        results: list[SearchResult] = []

        for entry in selected:
            numbers = line_map.get(entry.item.relative_path)
            if not numbers:
                continue
            try:
                text, _ = read_text_bounded(
                    self.snapshot,
                    entry.item,
                    max_chars=MAX_FILE_BYTES,
                )
            except ContentUnavailableError:
                continue
            lines = text.split("\n")
            matches = tuple(
                SearchMatch(
                    line=line_number,
                    text=lines[line_number - 1][:MAX_MATCH_TEXT_CHARS],
                )
                for line_number in numbers
                if 1 <= line_number <= len(lines)
                and needle in lines[line_number - 1].casefold()
            )[:MAX_MATCHES_PER_FILE]
            if not matches:
                continue
            path = entry.item.relative_path
            title = title_from_text(path, text)
            results.append(
                SearchResult(
                    id=encode_content_id(self.snapshot.manifest.commit, path),
                    path=path,
                    title=title,
                    kind=entry.kind,
                    matches=matches,
                    url=citation_url(
                        self.snapshot.manifest.commit,
                        path,
                        line=matches[0].line,
                    ),
                    content=(
                        text[:MAX_SEARCH_CONTENT_CHARS]
                        if include_content
                        else None
                    ),
                )
            )

        kind_order = {
            ContentKind.TASK: 0,
            ContentKind.REVERSE_ENGINEERING: 1,
            ContentKind.DOCUMENTATION: 2,
            ContentKind.SOURCE: 3,
        }

        def sort_key(result: SearchResult) -> tuple[int, int, str, str]:
            filename = PurePosixPath(result.path).name.casefold()
            title = result.title.casefold()
            folded_path = result.path.casefold()
            if needle in {filename, title}:
                match_rank = 0
            elif needle in filename or needle in folded_path:
                match_rank = 1
            else:
                match_rank = 2
            return (
                match_rank,
                kind_order[result.kind],
                folded_path,
                result.path,
            )

        results.sort(key=sort_key)
        bounded = tuple(results[:bounded_limit])
        return SearchResponse(
            query=clean_query,
            scope=selected_scope,
            count=len(bounded),
            results=bounded,
        )

    def fetch(self, content_id: str) -> FetchRecord:
        item = resolve_content_id(self.snapshot, content_id)
        entry = self._by_path[item.relative_path]
        text, truncated = read_text_bounded(self.snapshot, item)
        return FetchRecord(
            id=content_id,
            title=title_from_text(item.relative_path, text),
            text=text,
            url=citation_url(
                self.snapshot.manifest.commit,
                item.relative_path,
            ),
            path=item.relative_path,
            kind=entry.kind,
            language=language_for_path(item.relative_path),
            repository=EXPECTED_REPOSITORY,
            ref=EXPECTED_REF,
            commit=self.snapshot.manifest.commit,
            text_chars=len(text),
            truncated=truncated,
        )

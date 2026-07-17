"""Bounded literal search and fetch for the gateway's own immutable snapshot."""

from __future__ import annotations

import base64
import binascii
import re
import stat
from enum import StrEnum
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from app.config import EXPECTED_GATEWAY_REF, EXPECTED_GATEWAY_REPOSITORY
from app.gateway_repo.models import (
    GatewayContextOutput,
    GatewayFetchMetadata,
    GatewayFetchOutput,
    GatewaySearchOutput,
    GatewaySearchResult,
)
from app.gateway_repo.snapshot import (
    MAX_FILE_BYTES,
    GatewaySnapshot,
    GatewaySnapshotFile,
    is_admitted_gateway_path,
)

CONTENT_ID_PREFIX = "jng1_"
MAX_CONTENT_ID_CHARS = 8_192
MAX_FETCH_CHARS = 200_000
MAX_QUERY_CHARS = 200
MAX_MATCHES_PER_FILE = 20
MAX_RESULTS = 50
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PAYLOAD = re.compile(r"^[A-Za-z0-9_-]+$")
_H1 = re.compile(r"^#(?:[ \t]+)(.+?)\s*$")


class GatewaySearchScope(StrEnum):
    ALL = "all"
    SOURCE = "source"
    DOCS = "docs"
    TESTS = "tests"
    DEPLOY = "deploy"


class GatewayContentError(RuntimeError):
    pass


class GatewayRequestError(ValueError):
    pass


class GatewayInvalidIdError(GatewayRequestError):
    pass


class GatewayStaleIdError(GatewayRequestError):
    pass


def _validate_path(path: str) -> str:
    if not is_admitted_gateway_path(path):
        raise GatewayInvalidIdError("path is outside the gateway corpus")
    return PurePosixPath(path).as_posix()


def encode_gateway_id(commit: str, path: str) -> str:
    if not _COMMIT.fullmatch(commit):
        raise GatewayInvalidIdError("commit is malformed")
    payload = f"{commit}\x00{_validate_path(path)}".encode("utf-8")
    return CONTENT_ID_PREFIX + base64.urlsafe_b64encode(payload).decode(
        "ascii"
    ).rstrip("=")


def decode_gateway_id(content_id: str) -> tuple[str, str]:
    if (
        not content_id.startswith(CONTENT_ID_PREFIX)
        or len(content_id) > MAX_CONTENT_ID_CHARS
    ):
        raise GatewayInvalidIdError("unknown or oversized gateway content ID")
    encoded = content_id[len(CONTENT_ID_PREFIX) :]
    if not encoded or not _PAYLOAD.fullmatch(encoded):
        raise GatewayInvalidIdError("gateway content ID is malformed")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise GatewayInvalidIdError("gateway content ID is malformed") from exc
    if raw.count(b"\x00") != 1:
        raise GatewayInvalidIdError("gateway content ID is malformed")
    try:
        commit, path = (part.decode("utf-8") for part in raw.split(b"\x00", 1))
    except UnicodeDecodeError as exc:
        raise GatewayInvalidIdError("gateway content ID is malformed") from exc
    if not _COMMIT.fullmatch(commit):
        raise GatewayInvalidIdError("gateway content ID commit is malformed")
    return commit, _validate_path(path)


def _citation(commit: str, path: str, line: int | None = None) -> str:
    url = (
        f"https://github.com/{EXPECTED_GATEWAY_REPOSITORY}/blob/"
        f"{commit}/{quote(path, safe='/')}"
    )
    return f"{url}#L{line}" if line is not None else url


def _title(path: str, text: str) -> str:
    for line in text.splitlines()[:40]:
        match = _H1.fullmatch(line)
        if match:
            return match.group(1)[:200]
    return PurePosixPath(path).name[:200]


def _language(path: str) -> str | None:
    if path == "Dockerfile":
        return "dockerfile"
    return {
        ".css": "css",
        ".html": "html",
        ".json": "json",
        ".md": "markdown",
        ".py": "python",
        ".sh": "shell",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(PurePosixPath(path).suffix.lower())


def _kind(path: str) -> str:
    if path.startswith("tests/"):
        return "test"
    if path.startswith(("deploy/", "cloudflared/", ".github/")) or path in {
        ".dockerignore",
        ".env.example",
        ".env.gateway-repo.example",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.gateway-repo.yml",
    }:
        return "deployment"
    if path.startswith("docs/") or path in {
        "CONTRIBUTING.md",
        "README.md",
        "SECURITY.md",
    }:
        return "documentation"
    return "source"


def _in_scope(path: str, scope: GatewaySearchScope) -> bool:
    if scope is GatewaySearchScope.ALL:
        return True
    kind = _kind(path)
    if scope is GatewaySearchScope.SOURCE:
        return kind == "source"
    if scope is GatewaySearchScope.DOCS:
        return kind == "documentation"
    if scope is GatewaySearchScope.TESTS:
        return kind == "test" or path.startswith(".github/workflows/")
    return kind == "deployment"


class GatewayRepositoryContent:
    def __init__(self, snapshot: GatewaySnapshot) -> None:
        self.snapshot = snapshot

    def _revalidate(self, item: GatewaySnapshotFile) -> Path:
        current = self.snapshot.content_root
        for index, part in enumerate(PurePosixPath(item.relative_path).parts):
            current /= part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise GatewayContentError("gateway content is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise GatewayContentError("gateway content is unavailable")
            final = index == len(PurePosixPath(item.relative_path).parts) - 1
            if final:
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise GatewayContentError("gateway content is unavailable")
                if (
                    metadata.st_size != item.size_bytes
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise GatewayContentError("gateway content changed after startup")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise GatewayContentError("gateway content is unavailable")
        try:
            current.resolve(strict=True).relative_to(
                self.snapshot.content_root.resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise GatewayContentError("gateway content failed containment") from exc
        return current

    def _read(
        self,
        item: GatewaySnapshotFile,
        max_chars: int = MAX_FETCH_CHARS,
    ) -> tuple[str, bool]:
        path = self._revalidate(item)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise GatewayContentError("gateway content cannot be read") from exc
        if len(text) <= max_chars:
            return text, False
        return text[:max_chars], True

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        limit: int = 20,
    ) -> GatewaySearchOutput:
        clean = query.strip()
        if not clean or len(clean) > MAX_QUERY_CHARS:
            raise GatewayRequestError("query must contain 1..200 characters")
        if not 1 <= limit <= MAX_RESULTS:
            raise GatewayRequestError("limit must be in 1..50")
        try:
            selected_scope = GatewaySearchScope(scope)
        except ValueError as exc:
            raise GatewayRequestError(
                "scope must be all, source, docs, tests, or deploy"
            ) from exc

        needle = clean.casefold()
        ranked: list[tuple[int, str, GatewaySearchResult]] = []
        for item in self.snapshot.files:
            if not _in_scope(item.relative_path, selected_scope):
                continue
            text, _ = self._read(item, MAX_FILE_BYTES)
            lines = text.splitlines()
            matches = [
                number
                for number, line in enumerate(lines, 1)
                if needle in line.casefold()
            ][:MAX_MATCHES_PER_FILE]
            if not matches:
                continue
            title = _title(item.relative_path, text)
            filename = PurePosixPath(item.relative_path).name.casefold()
            if needle in {filename, title.casefold()}:
                rank = 0
            elif needle in item.relative_path.casefold():
                rank = 1
            else:
                rank = 2
            ranked.append(
                (
                    rank,
                    item.relative_path.casefold(),
                    GatewaySearchResult(
                        id=encode_gateway_id(
                            self.snapshot.manifest.commit,
                            item.relative_path,
                        ),
                        title=title,
                        url=_citation(
                            self.snapshot.manifest.commit,
                            item.relative_path,
                            matches[0],
                        ),
                    ),
                )
            )
        ranked.sort(key=lambda row: (row[0], row[1], row[2].title))
        return GatewaySearchOutput(results=[row[2] for row in ranked[:limit]])

    def fetch(self, content_id: str) -> GatewayFetchOutput:
        commit, path = decode_gateway_id(content_id)
        if commit != self.snapshot.manifest.commit:
            raise GatewayStaleIdError("gateway snapshot changed; search again")
        item = self.snapshot.file(path)
        if item is None:
            raise GatewayInvalidIdError("gateway content is not in the active snapshot")
        text, truncated = self._read(item)
        return GatewayFetchOutput(
            id=content_id,
            title=_title(path, text),
            text=text,
            url=_citation(commit, path),
            metadata=GatewayFetchMetadata(
                path=path,
                kind=_kind(path),
                language=_language(path),
                repository=EXPECTED_GATEWAY_REPOSITORY,
                ref=EXPECTED_GATEWAY_REF,
                commit=commit,
                text_chars=len(text),
                truncated=truncated,
            ),
        )

    def repository_context(self, max_chars: int = 12_000) -> GatewayContextOutput:
        if not 1_000 <= max_chars <= 20_000:
            raise GatewayRequestError("max_chars must be in 1000..20000")
        important = [
            "README.md",
            "docs/intended_usage.md",
            "docs/mcp_surface.md",
            "docs/security_model.md",
            "docs/deployment.md",
            "docs/public_repository_boundary.md",
        ]
        headers = [f"## {path}\n\n" for path in important]
        separators = 2 * (len(important) - 1)
        body_budget = max_chars - sum(map(len, headers)) - separators
        per_file = max(1, body_budget // len(important))
        sections: list[str] = []
        for path, header in zip(important, headers, strict=True):
            item = self.snapshot.file(path)
            if item is None:
                continue
            text, _ = self._read(item, per_file)
            sections.append(f"{header}{text}")
        context = "\n\n".join(sections)[:max_chars]
        return GatewayContextOutput(
            repository=EXPECTED_GATEWAY_REPOSITORY,
            ref=EXPECTED_GATEWAY_REF,
            commit=self.snapshot.manifest.commit,
            context=context,
            important_files=important,
        )

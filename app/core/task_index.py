"""Deterministic task index over five committed, allowlisted sources."""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from app.core.content_search import TASK_SOURCE_PATHS
from app.core.path_safety import citation_url, read_text_bounded, resolve_relative_file
from app.core.snapshot import MAX_FILE_BYTES, Snapshot

logger = logging.getLogger(__name__)

MAX_TASKS = 10_000
MAX_TASK_DETAIL_CHARS = 1_000
MAX_TASK_TITLE_CHARS = 500
DEFAULT_TASK_LIMIT = 50
MAX_TASK_LIMIT = 100

HANDOFF_PATH = "docs/decomp/_next_session.md"
QA_PATH = "docs/qa/qa_backlog_campaign_handoff.md"
LINKAGE_PATH = "docs/linkage_certificates.csv"
DECOMP_PATH = "docs/decomp_ledger.csv"
CATALOG_PATH = "docs/asset_catalog/behavior_todo.md"

DECOMP_HEADERS = (
    "class",
    "base_chain",
    "vftable",
    "ctor",
    "n_methods",
    "family",
    "wave",
    "status",
    "owner",
    "confidence",
    "notes",
)
LINKAGE_HEADERS = (
    "class",
    "aspect",
    "domain",
    "status",
    "oracle",
    "linkage_doc",
    "note",
)
QA_HEADERS = ("#", "Level", "Model", "Cat", "Issue", "Group", "Status")
CATALOG_QUEUE_HEADERS = (
    "Rank",
    "Score",
    "FourCC",
    "Class",
    "Family",
    "Inst",
    "Lvls",
    "Visual",
    "Levels",
)
CATALOG_UNUSED_HEADERS = ("FourCC", "Class", "Visual status", "Decomp doc")


class TaskStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    DONE = "done"


class TaskStatusFilter(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    DONE = "done"
    ALL = "all"


class TaskSourceKind(StrEnum):
    HANDOFF = "handoff"
    QA = "qa"
    LINKAGE = "linkage"
    DECOMP = "decomp"
    CATALOG = "catalog"


class TaskSourceFilter(StrEnum):
    ALL = "all"
    HANDOFF = "handoff"
    QA = "qa"
    LINKAGE = "linkage"
    DECOMP = "decomp"
    CATALOG = "catalog"


class TaskIndexError(RuntimeError):
    """A canonical task source does not match its frozen grammar."""


class TaskRequestError(ValueError):
    """A task-list filter or limit is invalid."""


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    status: TaskStatus
    source_kind: TaskSourceKind
    category: str | None
    detail: str | None
    source_path: str
    source_line: int
    source_url: str
    commit: str


@dataclass(frozen=True)
class TaskList:
    status: TaskStatusFilter
    source: TaskSourceFilter
    count: int
    tasks: tuple[TaskRecord, ...]


def _bounded(value: str, limit: int) -> str | None:
    clean = " ".join(value.split())
    return clean[:limit] if clean else None


def _plain_markdown(value: str) -> str:
    clean = value.strip()
    clean = re.sub(r"\x60([^\x60]*)\x60", r"\1", clean)
    clean = clean.replace("**", "").replace("__", "")
    return " ".join(clean.split())


def _semantic_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:96] or "task"


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _csv_rows(
    text: str,
    headers: tuple[str, ...],
    path: str,
) -> list[tuple[int, dict[str, str]]]:
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if tuple(reader.fieldnames or ()) != headers:
            raise TaskIndexError(f"{path} has unexpected headers")
        rows: list[tuple[int, dict[str, str]]] = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise TaskIndexError(f"{path} contains a malformed row")
            rows.append(
                (
                    reader.line_num,
                    {key: value or "" for key, value in row.items()},
                )
            )
        return rows
    except csv.Error as exc:
        raise TaskIndexError(f"{path} is not valid CSV") from exc


QA_STATUS_RULES = (
    ("NOT FIXED", TaskStatus.OPEN),
    ("WONTFIX", TaskStatus.DONE),
    ("INCOMPLETE", TaskStatus.BLOCKED),
    ("DEFERRED", TaskStatus.BLOCKED),
    ("BLOCKED", TaskStatus.BLOCKED),
    ("RESOLVED", TaskStatus.DONE),
    ("PENDING", TaskStatus.OPEN),
    ("CLOSED", TaskStatus.DONE),
    ("FIXED", TaskStatus.DONE),
    ("DONE", TaskStatus.DONE),
    ("OPEN", TaskStatus.OPEN),
)


def normalize_qa_status(raw_status: str) -> TaskStatus | None:
    uppercase = raw_status.upper()
    for token, normalized in QA_STATUS_RULES:
        if re.search(
            rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])",
            uppercase,
        ):
            return normalized
    return None


class TaskIndex:
    """Immutable parsed task records for one active snapshot."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        missing = TASK_SOURCE_PATHS.difference(snapshot.files_by_path)
        if missing:
            raise TaskIndexError("one or more required task sources are missing")
        tasks: list[TaskRecord] = []
        tasks.extend(self._parse_handoff())
        tasks.extend(self._parse_qa())
        tasks.extend(self._parse_linkage())
        tasks.extend(self._parse_decomp())
        tasks.extend(self._parse_catalog())
        if len(tasks) > MAX_TASKS:
            raise TaskIndexError("task index exceeds 10000 records")
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise TaskIndexError("task index contains duplicate semantic IDs")
        status_order = {
            TaskStatus.OPEN: 0,
            TaskStatus.BLOCKED: 1,
            TaskStatus.DONE: 2,
        }
        source_order = {
            TaskSourceKind.HANDOFF: 0,
            TaskSourceKind.QA: 1,
            TaskSourceKind.LINKAGE: 2,
            TaskSourceKind.CATALOG: 3,
            TaskSourceKind.DECOMP: 4,
        }
        tasks.sort(
            key=lambda task: (
                status_order[task.status],
                source_order[task.source_kind],
                task.source_line,
                task.id,
            )
        )
        self._tasks = tuple(tasks)
        self._tasks_by_id = {task.id: task for task in self._tasks}

    @property
    def tasks(self) -> tuple[TaskRecord, ...]:
        return self._tasks

    def _text(self, path: str) -> str:
        item = resolve_relative_file(self.snapshot, path)
        text, _ = read_text_bounded(
            self.snapshot,
            item,
            max_chars=MAX_FILE_BYTES,
        )
        return text

    def _record(
        self,
        *,
        task_id: str,
        title: str,
        status: TaskStatus,
        source_kind: TaskSourceKind,
        category: str | None,
        detail: str | None,
        path: str,
        line: int,
    ) -> TaskRecord:
        return TaskRecord(
            id=task_id,
            title=_plain_markdown(title)[:MAX_TASK_TITLE_CHARS],
            status=status,
            source_kind=source_kind,
            category=_bounded(category or "", 200),
            detail=_bounded(detail or "", MAX_TASK_DETAIL_CHARS),
            source_path=path,
            source_line=line,
            source_url=citation_url(
                self.snapshot.manifest.commit,
                path,
                line=line,
            ),
            commit=self.snapshot.manifest.commit,
        )

    def _parse_handoff(self) -> list[TaskRecord]:
        text = self._text(HANDOFF_PATH)
        lines = text.splitlines()
        in_section = False
        records: list[TaskRecord] = []
        for line_number, line in enumerate(lines, start=1):
            heading = re.fullmatch(r"##\s+(.+?)\s*", line)
            if heading:
                title = heading.group(1).casefold()
                in_section = (
                    title.startswith("your task this session")
                    or title.startswith("live options")
                    or title.startswith("recommended next campaign")
                )
                continue
            if not in_section:
                continue
            checkbox = re.fullmatch(r"[-*+]\s+\[([ xX])\]\s+(.+)", line)
            bullet = re.fullmatch(r"[-*+]\s+(.+)", line)
            numbered = re.fullmatch(r"\d+[.)]\s+(.+)", line)
            if checkbox:
                checked, title = checkbox.groups()
                status = (
                    TaskStatus.DONE
                    if checked.casefold() == "x"
                    else TaskStatus.OPEN
                )
            elif bullet:
                title = bullet.group(1)
                status = TaskStatus.OPEN
            elif numbered:
                title = numbered.group(1)
                status = TaskStatus.OPEN
            else:
                continue
            plain = _plain_markdown(title)
            label_match = re.match(r"^\(([a-z0-9]+)\)\s*", plain, re.IGNORECASE)
            semantic = (
                label_match.group(1)
                if label_match
                else _semantic_slug(plain)
            )
            records.append(
                self._record(
                    task_id=f"handoff:{semantic.casefold()}",
                    title=plain,
                    status=status,
                    source_kind=TaskSourceKind.HANDOFF,
                    category="current",
                    detail=title,
                    path=HANDOFF_PATH,
                    line=line_number,
                )
            )
        if not records:
            raise TaskIndexError("handoff current-task section contains no tasks")
        return records

    def _parse_qa(self) -> list[TaskRecord]:
        lines = self._text(QA_PATH).splitlines()
        in_section = False
        header_seen = False
        records: list[TaskRecord] = []
        for line_number, line in enumerate(lines, start=1):
            heading = re.fullmatch(r"##\s+(.+?)\s*", line)
            if heading:
                in_section = heading.group(1).startswith("Master report ledger")
                if header_seen and not in_section:
                    break
                continue
            if not in_section:
                continue
            cells = _table_cells(line)
            if cells is None:
                if header_seen and line.strip():
                    break
                continue
            if tuple(cells) == QA_HEADERS:
                header_seen = True
                continue
            if _is_separator_row(cells):
                continue
            if not header_seen:
                continue
            if len(cells) != len(QA_HEADERS):
                raise TaskIndexError("QA ledger contains a malformed row")
            row = dict(zip(QA_HEADERS, cells, strict=True))
            report_number = row["#"]
            if not report_number.isdigit():
                logger.info(
                    "task row skipped path=%s line=%d reason=invalid_report_id",
                    QA_PATH,
                    line_number,
                )
                continue
            status = normalize_qa_status(row["Status"])
            if status is None:
                logger.info(
                    "task row skipped path=%s line=%d reason=unknown_status",
                    QA_PATH,
                    line_number,
                )
                continue
            records.append(
                self._record(
                    task_id=f"qa:{report_number}",
                    title=(
                        f"QA #{report_number} {row['Level']} {row['Model']}: "
                        f"{row['Issue']}"
                    ),
                    status=status,
                    source_kind=TaskSourceKind.QA,
                    category=row["Cat"],
                    detail=f"group={row['Group']}; status={row['Status']}",
                    path=QA_PATH,
                    line=line_number,
                )
            )
        if not header_seen:
            raise TaskIndexError("QA master ledger header is missing")
        return records

    def _parse_linkage(self) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for line_number, row in _csv_rows(
            self._text(LINKAGE_PATH),
            LINKAGE_HEADERS,
            LINKAGE_PATH,
        ):
            raw_status = row["status"].strip().casefold()
            if raw_status == "linked":
                status = TaskStatus.DONE
            elif raw_status == "linked-blocked":
                status = TaskStatus.BLOCKED
            else:
                logger.info(
                    "task row skipped path=%s line=%d reason=unknown_status",
                    LINKAGE_PATH,
                    line_number,
                )
                continue
            class_name = row["class"].strip()
            aspect = row["aspect"].strip()
            if not class_name or not aspect:
                raise TaskIndexError("linkage row is missing class or aspect")
            records.append(
                self._record(
                    task_id=(
                        f"linkage:{class_name.casefold()}:"
                        f"{_semantic_slug(aspect)}"
                    ),
                    title=f"{class_name} / {aspect}",
                    status=status,
                    source_kind=TaskSourceKind.LINKAGE,
                    category=row["domain"],
                    detail=(
                        f"status={row['status']}; "
                        f"oracle={row['oracle'] or 'none'}; {row['note']}"
                    ),
                    path=LINKAGE_PATH,
                    line=line_number,
                )
            )
        return records

    def _parse_decomp(self) -> list[TaskRecord]:
        status_map = {
            "todo": TaskStatus.OPEN,
            "in_progress": TaskStatus.OPEN,
            "spec": TaskStatus.DONE,
            "ported": TaskStatus.DONE,
            "ported(optional)": TaskStatus.DONE,
            "validated": TaskStatus.DONE,
        }
        records: list[TaskRecord] = []
        for line_number, row in _csv_rows(
            self._text(DECOMP_PATH),
            DECOMP_HEADERS,
            DECOMP_PATH,
        ):
            raw_status = row["status"].strip().casefold()
            status = status_map.get(raw_status)
            if status is None:
                logger.info(
                    "task row skipped path=%s line=%d reason=unknown_status",
                    DECOMP_PATH,
                    line_number,
                )
                continue
            class_name = row["class"].strip()
            if not class_name:
                raise TaskIndexError("decomp row is missing its class")
            records.append(
                self._record(
                    task_id=f"decomp:{class_name.casefold()}",
                    title=class_name,
                    status=status,
                    source_kind=TaskSourceKind.DECOMP,
                    category=row["family"],
                    detail=(
                        f"status={row['status']}; wave={row['wave']}; "
                        f"confidence={row['confidence']}; {row['notes']}"
                    ),
                    path=DECOMP_PATH,
                    line=line_number,
                )
            )
        return records

    def _parse_catalog_table(
        self,
        lines: list[str],
        start: int,
        headers: tuple[str, ...],
    ) -> tuple[list[TaskRecord], int]:
        records: list[TaskRecord] = []
        index = start
        header_seen = False
        while index < len(lines):
            line = lines[index]
            if line.startswith("## "):
                break
            cells = _table_cells(line)
            if cells is None:
                index += 1
                continue
            if tuple(cells) == headers:
                header_seen = True
                index += 1
                continue
            if _is_separator_row(cells):
                index += 1
                continue
            if not header_seen:
                index += 1
                continue
            if len(cells) != len(headers):
                raise TaskIndexError("catalog contains a malformed table row")
            row = dict(zip(headers, cells, strict=True))
            fourcc = row["FourCC"].strip("\x60 ")
            class_name = row["Class"].strip("\x60 ")
            if fourcc == "-" and class_name == "-":
                index += 1
                continue
            if len(fourcc) != 4 or not class_name:
                raise TaskIndexError("catalog task has invalid FourCC or class")
            detail_parts = [
                f"{key}={value}"
                for key, value in row.items()
                if key not in {"FourCC", "Class"} and value and value != "-"
            ]
            records.append(
                self._record(
                    task_id=(
                        f"catalog:{fourcc.casefold()}:{class_name.casefold()}"
                    ),
                    title=f"{fourcc} {class_name}",
                    status=TaskStatus.OPEN,
                    source_kind=TaskSourceKind.CATALOG,
                    category=row.get("Family") or "unplaced",
                    detail="; ".join(detail_parts),
                    path=CATALOG_PATH,
                    line=index + 1,
                )
            )
            index += 1
        if not header_seen:
            raise TaskIndexError("catalog named section is missing its table header")
        return records, index

    def _parse_catalog(self) -> list[TaskRecord]:
        lines = self._text(CATALOG_PATH).splitlines()
        records: list[TaskRecord] = []
        found_sections: set[str] = set()
        expected_sections = {
            "Full Missing Native Behavior Queue",
            "Enemy-Family Specs With No Current `.gam` Placement",
        }
        index = 0
        while index < len(lines):
            heading = re.fullmatch(r"##\s+(.+?)\s*", lines[index])
            if heading and heading.group(1) in expected_sections:
                name = heading.group(1)
                found_sections.add(name)
                headers = (
                    CATALOG_QUEUE_HEADERS
                    if name == "Full Missing Native Behavior Queue"
                    else CATALOG_UNUSED_HEADERS
                )
                parsed, index = self._parse_catalog_table(
                    lines,
                    index + 1,
                    headers,
                )
                records.extend(parsed)
                continue
            index += 1
        if found_sections != expected_sections:
            raise TaskIndexError("catalog required sections are missing")
        return records

    def list_tasks(
        self,
        *,
        status: TaskStatusFilter | str = TaskStatusFilter.OPEN,
        source: TaskSourceFilter | str = TaskSourceFilter.ALL,
        limit: int = DEFAULT_TASK_LIMIT,
    ) -> TaskList:
        try:
            selected_status = TaskStatusFilter(status)
            selected_source = TaskSourceFilter(source)
        except ValueError as exc:
            raise TaskRequestError("task status or source filter is invalid") from exc
        if not 1 <= limit <= MAX_TASK_LIMIT:
            raise TaskRequestError("limit must be in 1..100")
        tasks = tuple(
            task
            for task in self._tasks
            if (
                selected_status is TaskStatusFilter.ALL
                or task.status.value == selected_status.value
            )
            and (
                selected_source is TaskSourceFilter.ALL
                or task.source_kind.value == selected_source.value
            )
        )[:limit]
        return TaskList(
            status=selected_status,
            source=selected_source,
            count=len(tasks),
            tasks=tasks,
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return one exact committed task ID without accepting fuzzy aliases."""
        return self._tasks_by_id.get(task_id)

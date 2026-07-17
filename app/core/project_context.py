"""Deterministic contributor briefing over eight frozen project files."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from app.core.path_safety import citation_url, read_text_bounded, resolve_relative_file
from app.core.snapshot import MAX_FILE_BYTES, Snapshot
from app.core.task_index import TaskIndex, TaskRecord, TaskSourceKind, TaskStatus

DEFAULT_CONTEXT_CHARS = 12_000
MIN_CONTEXT_CHARS = 1_000
MAX_CONTEXT_CHARS = 20_000
MAX_SUMMARY_CHARS = 500
MAX_STATE_CHARS = 1_000
MAX_EXCERPT_CHARS = 1_600
MAX_CONTEXT_TASKS = 10


@dataclass(frozen=True)
class ContextFileSpec:
    path: str
    role: str
    section_prefixes: tuple[str, ...]


CONTEXT_FILE_SPECS = (
    ContextFileSpec(
        "AGENTS.md",
        "Mission, contributor rules, current frontier, and repository gotchas",
        ("Mission", "Shared memory", "The current frontier", "Gotchas"),
    ),
    ContextFileSpec(
        "README.md",
        "Project overview, build entry points, and repository layout",
        ("Build", "Repo layout"),
    ),
    ContextFileSpec(
        "docs/ARCHITECTURE.md",
        "Current architecture, runtime modes, build map, and invariants",
        ("1. The 10,000-foot view", "9. Build & run", "12. The invariants"),
    ),
    ContextFileSpec(
        "docs/PROJECT_HISTORY.md",
        "Settled project history, latest dated state, and durable invariants",
        ("Invariants", "Where to go next"),
    ),
    ContextFileSpec(
        "docs/decomp/_next_session.md",
        "Active handoff, live work, hard rules, and session completion checks",
        ("Current state", "Your task this session", "Hard rules", "Definition of done"),
    ),
    ContextFileSpec(
        "docs/native_port_plan.md",
        "Native-port implementation contract, sequencing, and validation",
        ("0. Where the engine stands", "1. The implementation contract", "3. Validation"),
    ),
    ContextFileSpec(
        "docs/local_env.md",
        "Local environment safety and instrumentation setup",
        ("Required variables", "Recommended setup", "Rebuilding the proxy"),
    ),
    ContextFileSpec(
        "docs/claude_code_failure_patterns.md",
        "Measured JN Engine failure patterns and operating cautions",
        (
            "1. Repeating known conclusions",
            "2. Fixing the wrong layer",
            "3. Making proxy assumptions",
            "4. Trusting deployed binaries",
            "5. Launching the game",
            "9. Blocking the game render thread",
            "10. Misreading alpha",
            "11. Continuing a known dead-end",
        ),
    ),
)

CONTEXT_PATHS = tuple(spec.path for spec in CONTEXT_FILE_SPECS)
HANDOFF_SECTION_ALTERNATIVES = {
    "Current state": ("Current state", "What just landed"),
    "Your task this session": (
        "Your task this session",
        "Recommended next campaign",
    ),
    "Hard rules": ("Hard rules", "Standard validation"),
}
BRANCH_DECLARATION = re.compile(
    r"(?i)\bbranch\s*:\s*`?([a-z0-9][a-z0-9._/-]*)"
)
NAMED_STALE_BRANCH = re.compile(
    r"(?i)\b(native-port|decomp-campaign|linked|main)\s+branch\b"
)
DATED_HEADING = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class ProjectContextError(RuntimeError):
    """The frozen project-context corpus cannot be assembled safely."""


class ProjectContextRequestError(ValueError):
    """The requested context bound is invalid."""


@dataclass(frozen=True)
class ImportantFile:
    title: str
    role: str
    path: str
    line: int
    url: str


@dataclass(frozen=True)
class ProjectContext:
    summary: str
    current_state: tuple[str, ...]
    important_files: tuple[ImportantFile, ...]
    open_tasks: tuple[TaskRecord, ...]
    context: str


@dataclass(frozen=True)
class _HeadingSection:
    level: int
    title: str
    line: int
    body: str


def _plain_markdown(value: str) -> str:
    clean = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    clean = re.sub(r"`([^`]*)`", r"\1", clean)
    clean = clean.replace("**", "").replace("__", "").replace("*", "")
    return " ".join(clean.split())


def _bounded_plain(value: str, limit: int) -> str:
    return _plain_markdown(value)[:limit]


def _heading_sections(text: str) -> tuple[_HeadingSection, ...]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING.fullmatch(line)
        if match is not None:
            headings.append((index, len(match.group(1)), match.group(2)))
    sections: list[_HeadingSection] = []
    for position, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        sections.append(
            _HeadingSection(
                level=level,
                title=title,
                line=start + 1,
                body="\n".join(lines[start + 1 : end]).strip(),
            )
        )
    return tuple(sections)


def _first_description_paragraph(text: str) -> str:
    lines = text.splitlines()
    paragraph: list[str] = []
    passed_title = False
    for line in lines:
        if not passed_title and MARKDOWN_HEADING.fullmatch(line):
            passed_title = True
            continue
        if not passed_title:
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        if line.lstrip().startswith((">", "|", "```")):
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    summary = _bounded_plain(" ".join(paragraph), MAX_SUMMARY_CHARS)
    if not summary:
        raise ProjectContextError("README has no project-description paragraph")
    return summary


class ProjectContextAssembler:
    """Preload the closed source set and build bounded context on demand."""

    def __init__(self, snapshot: Snapshot, task_index: TaskIndex) -> None:
        if snapshot.manifest.commit != task_index.snapshot.manifest.commit:
            raise ProjectContextError("task index and project context use different snapshots")
        self.snapshot = snapshot
        self.task_index = task_index
        missing = [path for path in CONTEXT_PATHS if path not in snapshot.files_by_path]
        if missing:
            raise ProjectContextError("one or more required project-context files are missing")
        self._texts = {path: self._read(path) for path in CONTEXT_PATHS}
        self._sections = {
            path: _heading_sections(text) for path, text in self._texts.items()
        }
        self._summary = _first_description_paragraph(self._texts["README.md"])
        self._important_files = self._build_important_files()
        self._current_state = self._build_current_state()
        self._excerpts = self._build_excerpts()

    def _read(self, path: str) -> str:
        item = resolve_relative_file(self.snapshot, path)
        text, _ = read_text_bounded(self.snapshot, item, max_chars=MAX_FILE_BYTES)
        return text

    def _source_url(self, path: str, line: int = 1) -> str:
        return citation_url(self.snapshot.manifest.commit, path, line=line)

    def _build_important_files(self) -> tuple[ImportantFile, ...]:
        files: list[ImportantFile] = []
        for spec in CONTEXT_FILE_SPECS:
            h1 = next(
                (section for section in self._sections[spec.path] if section.level == 1),
                None,
            )
            if h1 is None:
                raise ProjectContextError("project-context file is missing its title")
            files.append(
                ImportantFile(
                    title=_bounded_plain(h1.title, 300),
                    role=spec.role,
                    path=spec.path,
                    line=h1.line,
                    url=self._source_url(spec.path, h1.line),
                )
            )
        return tuple(files)

    def _mission_state(self) -> str:
        mission = next(
            (
                section
                for section in self._sections["AGENTS.md"]
                if section.level == 2 and section.title.startswith("Mission")
            ),
            None,
        )
        if mission is None:
            raise ProjectContextError("AGENTS.md is missing its Mission section")
        state = _bounded_plain(mission.body, MAX_STATE_CHARS)
        if not state:
            raise ProjectContextError("AGENTS.md Mission section is empty")
        return state

    def _latest_history_states(self) -> tuple[str, ...]:
        dated: list[tuple[str, _HeadingSection]] = []
        for section in self._sections["docs/PROJECT_HISTORY.md"]:
            if section.level != 2:
                continue
            match = DATED_HEADING.search(section.title)
            if match is not None:
                dated.append((match.group(1), section))
        if not dated:
            raise ProjectContextError("PROJECT_HISTORY has no dated level-two entry")
        latest = max(date for date, _ in dated)
        return tuple(
            _bounded_plain(f"Latest project history ({latest}): {section.title}", MAX_STATE_CHARS)
            for date, section in dated
            if date == latest
        )

    def _stale_branch_states(self) -> tuple[str, ...]:
        handoff_path = "docs/decomp/_next_session.md"
        text = self._texts[handoff_path]
        branches = {
            match.group(1)
            for match in BRANCH_DECLARATION.finditer(text)
            if match.group(1).casefold() not in {"master", "refs/heads/master"}
        }
        branches.update(match.group(1) for match in NAMED_STALE_BRANCH.finditer(text))
        return tuple(
            (
                f"Stale branch notice: {handoff_path} names {branch}; the served "
                f"snapshot is refs/heads/master at {self.snapshot.manifest.commit}."
            )[:MAX_STATE_CHARS]
            for branch in sorted(branches, key=str.casefold)
        )

    def _task_states(self) -> tuple[str, str]:
        statuses = Counter(task.status for task in self.task_index.tasks)
        linkage = Counter(
            task.status
            for task in self.task_index.tasks
            if task.source_kind is TaskSourceKind.LINKAGE
        )
        return (
            (
                "Committed task surface: "
                f"{statuses[TaskStatus.OPEN]} open, "
                f"{statuses[TaskStatus.BLOCKED]} blocked, and "
                f"{statuses[TaskStatus.DONE]} done records across the frozen task sources."
            ),
            (
                "Committed linkage certificates: "
                f"{linkage[TaskStatus.DONE]} linked and "
                f"{linkage[TaskStatus.BLOCKED]} linked-blocked records in "
                "docs/linkage_certificates.csv."
            ),
        )

    def _build_current_state(self) -> tuple[str, ...]:
        return (
            *self._stale_branch_states(),
            self._mission_state(),
            *self._latest_history_states(),
            *self._task_states(),
        )

    def _selected_sections(self, spec: ContextFileSpec) -> list[_HeadingSection]:
        selected: list[_HeadingSection] = []
        for prefix in spec.section_prefixes:
            alternatives = (
                HANDOFF_SECTION_ALTERNATIVES.get(prefix, (prefix,))
                if spec.path == "docs/decomp/_next_session.md"
                else (prefix,)
            )
            matches = [
                section
                for section in self._sections[spec.path]
                if any(section.title.startswith(option) for option in alternatives)
            ]
            if not matches:
                raise ProjectContextError("project-context file is missing a named heading")
            selected.extend(section for section in matches if section not in selected)
        if spec.path == "docs/PROJECT_HISTORY.md":
            dated: list[tuple[str, _HeadingSection]] = []
            for section in self._sections[spec.path]:
                match = DATED_HEADING.search(section.title)
                if section.level == 2 and match is not None:
                    dated.append((match.group(1), section))
            if dated:
                latest = max(date for date, _ in dated)
                selected.extend(
                    section
                    for date, section in dated
                    if date == latest and section not in selected
                )
        selected.sort(key=lambda section: section.line)
        if not selected:
            raise ProjectContextError("project-context file has no selected named headings")
        return selected

    def _build_excerpts(self) -> tuple[str, ...]:
        excerpts: list[str] = []
        for spec in CONTEXT_FILE_SPECS:
            for section in self._selected_sections(spec):
                body = section.body.strip()
                excerpt = f"{'#' * section.level} {section.title}"
                if body:
                    excerpt = f"{excerpt}\n\n{body}"
                excerpts.append(
                    f"Source: {spec.path} ({self._source_url(spec.path, section.line)})\n"
                    f"{excerpt[:MAX_EXCERPT_CHARS]}"
                )
        return tuple(excerpts)

    def _open_tasks(self) -> tuple[TaskRecord, ...]:
        return tuple(
            task
            for task in self.task_index.tasks
            if task.status in {TaskStatus.OPEN, TaskStatus.BLOCKED}
        )[:MAX_CONTEXT_TASKS]

    def build(self, *, max_chars: int = DEFAULT_CONTEXT_CHARS) -> ProjectContext:
        if not MIN_CONTEXT_CHARS <= max_chars <= MAX_CONTEXT_CHARS:
            raise ProjectContextRequestError("max_chars must be in 1000..20000")
        open_tasks = self._open_tasks()
        state_lines = "\n".join(f"- {state}" for state in self._current_state)
        task_lines = "\n".join(
            (
                f"- [{task.status.value}] {task.title} "
                f"({task.source_path}#L{task.source_line})"
            )
            for task in open_tasks
        ) or "- None in the committed task index."
        context = (
            "# JN Engine contributor context\n\n"
            f"Snapshot: alexscott2718-gif/jn-engine refs/heads/master "
            f"{self.snapshot.manifest.commit}\n\n"
            f"Summary: {self._summary}\n\n"
            f"## Current state\n{state_lines}\n\n"
            f"## Open and blocked tasks\n{task_lines}\n\n"
            "## Grounded excerpts\n\n"
            + "\n\n".join(self._excerpts)
        )[:max_chars]
        return ProjectContext(
            summary=self._summary,
            current_state=self._current_state,
            important_files=self._important_files,
            open_tasks=open_tasks,
            context=context,
        )

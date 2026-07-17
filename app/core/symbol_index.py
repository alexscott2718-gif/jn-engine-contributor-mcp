"""Immutable symbol index over committed reverse-engineering sources."""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from app.core.path_safety import citation_url, read_text_bounded, resolve_relative_file
from app.core.snapshot import MAX_FILE_BYTES, Snapshot

DECOMP_PATH = "docs/decomp_ledger.csv"
CLASS_IDS_PATH = "docs/_gam_classids.tsv"
LINKAGE_PATH = "docs/linkage_certificates.csv"
DECOMP_DOCS_PREFIX = "docs/decomp/"

MAX_SYMBOL_RECORDS = 50_000
MAX_SYMBOL_SUMMARY_CHARS = 1_000
MAX_LINKAGE_PER_SYMBOL = 10
DEFAULT_SYMBOL_LIMIT = 20
MAX_SYMBOL_LIMIT = 50
MAX_SYMBOL_QUERY_CHARS = 128

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
CLASS_ID_HEADERS = (
    "imm_fourcc",
    "reversed",
    "@site",
    "function",
    "class_or_nearby_string",
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

ADDRESS_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])(?:0x|FUN_|@)?([0-9a-f]{8})(?![0-9a-f])"
)
ADDRESS_INPUT_PATTERN = re.compile(r"(?i)^(?:0x|FUN_|@)?([0-9a-f]{8})$")
FOURCC_PATTERN = re.compile(r"^[\x20-\x7e]{4}$")
EXPLICIT_FOURCC_PATTERN = re.compile(r"^`([\x20-\x7e]{4})`(?:\s|$)")


class SymbolKind(StrEnum):
    CLASS = "class"
    FUNCTION = "function"
    FOURCC = "fourcc"


class LinkageStatus(StrEnum):
    LINKED = "linked"
    LINKED_BLOCKED = "linked-blocked"


class SymbolIndexError(RuntimeError):
    """A canonical symbol source violates its frozen grammar."""


class SymbolRequestError(ValueError):
    """A symbol query is invalid."""


@dataclass(frozen=True)
class SourceRef:
    path: str
    line: int | None
    url: str


@dataclass(frozen=True)
class LinkageRecord:
    aspect: str
    domain: str
    status: LinkageStatus
    oracle: str | None
    source: SourceRef


@dataclass(frozen=True)
class SymbolRecord:
    kind: SymbolKind
    name: str
    address: str | None
    signature: str | None
    class_name: str | None
    fourcc: str | None
    status: str | None
    linkage: tuple[LinkageRecord, ...]
    summary: str | None
    source: SourceRef


@dataclass(frozen=True)
class SymbolQuery:
    name: str | None
    address: str | None
    class_name: str | None
    fourcc: str | None


@dataclass(frozen=True)
class SymbolLookup:
    query: SymbolQuery
    count: int
    results: tuple[SymbolRecord, ...]


@dataclass(frozen=True)
class _LedgerRow:
    class_name: str
    vftables: tuple[str, ...]
    ctors: tuple[str, ...]
    status: str | None
    notes: str | None
    line: int


@dataclass(frozen=True)
class _ClassIdRow:
    fourcc: str
    site: str
    function: str | None
    nearby_class: str | None
    canonical_class: str | None
    line: int


@dataclass(frozen=True)
class _MethodRow:
    name: str
    addresses: tuple[str, ...]
    signature: str | None
    status: str | None
    summary: str | None
    line: int


@dataclass(frozen=True)
class _ClassDoc:
    explicit_fourcc: str | None
    signature: str | None
    methods: tuple[_MethodRow, ...]


def normalize_address(raw: str) -> str:
    candidate = raw.strip()
    match = ADDRESS_INPUT_PATTERN.fullmatch(candidate)
    if match is None:
        raise SymbolRequestError("address must be an exact 32-bit hexadecimal value")
    return match.group(1).casefold()


def _bounded(value: str, limit: int) -> str | None:
    clean = " ".join(value.split())
    return clean[:limit] if clean else None


def _plain_markdown(value: str) -> str:
    clean = re.sub(r"`([^`]*)`", r"\1", value.strip())
    clean = clean.replace("**", "").replace("__", "")
    return " ".join(clean.split())


def _normalize_nearby_class(value: str) -> str | None:
    clean = _plain_markdown(value)
    clean = re.sub(r"\(\)$", "", clean).strip()
    return clean or None


def _table_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def _is_separator_row(cells: tuple[str, ...]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _strict_csv_rows(
    text: str,
    *,
    path: str,
    headers: tuple[str, ...],
    delimiter: str = ",",
    skipinitialspace: bool = False,
) -> list[tuple[int, dict[str, str]]]:
    try:
        reader = csv.DictReader(
            io.StringIO(text),
            delimiter=delimiter,
            skipinitialspace=skipinitialspace,
            strict=True,
        )
        if tuple(reader.fieldnames or ()) != headers:
            raise SymbolIndexError(f"{path} has unexpected headers")
        rows: list[tuple[int, dict[str, str]]] = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise SymbolIndexError(f"{path} contains a malformed row")
            rows.append((reader.line_num, {key: value or "" for key, value in row.items()}))
        return rows
    except csv.Error as exc:
        raise SymbolIndexError(f"{path} is not valid delimited text") from exc


def _addresses_from_cell(value: str) -> tuple[str, ...]:
    addresses = tuple(match.casefold() for match in ADDRESS_PATTERN.findall(value))
    if not addresses:
        raise SymbolIndexError("canonical address field contains no exact address")
    if len(addresses) != len(set(addresses)):
        raise SymbolIndexError("canonical address field repeats an address")
    return addresses


def _canonical_address(raw: str, *, label: str) -> str:
    try:
        return normalize_address(raw)
    except SymbolRequestError as exc:
        raise SymbolIndexError(f"{label} contains an invalid canonical address") from exc


def _canonical_address_list(raw: str, *, label: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    tokens = raw.split(";")
    if any(token != token.strip() or not token for token in tokens):
        raise SymbolIndexError(f"{label} contains a malformed address list")
    addresses = tuple(_canonical_address(token, label=label) for token in tokens)
    if len(addresses) != len(set(addresses)):
        raise SymbolIndexError(f"{label} repeats a canonical address")
    return addresses


def _source(snapshot: Snapshot, path: str, line: int | None) -> SourceRef:
    return SourceRef(
        path=path,
        line=line,
        url=citation_url(snapshot.manifest.commit, path, line=line),
    )


class SymbolIndex:
    """Build and query the bounded symbol corpus for one active snapshot."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        for path in (DECOMP_PATH, CLASS_IDS_PATH, LINKAGE_PATH):
            if path not in snapshot.files_by_path:
                raise SymbolIndexError("one or more canonical symbol sources are missing")

        ledger = self._parse_ledger()
        canonical_classes = {row.class_name.casefold(): row.class_name for row in ledger}
        class_ids = self._parse_class_ids(canonical_classes)
        docs = {
            row.class_name: self._parse_class_doc(row.class_name)
            for row in ledger
        }
        linkage = self._parse_linkage(canonical_classes)
        self._validate_explicit_fourccs(ledger, docs, class_ids)

        records = self._build_records(ledger, class_ids, docs, linkage)
        if len(records) > MAX_SYMBOL_RECORDS:
            raise SymbolIndexError("symbol index exceeds 50000 records")
        records.sort(key=self._stable_record_key)
        self._records = tuple(records)
        self.decomp_row_count = len(ledger)
        self.class_id_row_count = len(class_ids)
        self.linkage_row_count = sum(len(rows) for rows in linkage.values())

    @property
    def records(self) -> tuple[SymbolRecord, ...]:
        return self._records

    def _text(self, path: str) -> str:
        item = resolve_relative_file(self.snapshot, path)
        text, _ = read_text_bounded(self.snapshot, item, max_chars=MAX_FILE_BYTES)
        return text

    def _parse_ledger(self) -> tuple[_LedgerRow, ...]:
        rows: list[_LedgerRow] = []
        seen: set[str] = set()
        for line, raw in _strict_csv_rows(
            self._text(DECOMP_PATH), path=DECOMP_PATH, headers=DECOMP_HEADERS
        ):
            class_name = raw["class"].strip()
            if not class_name or len(class_name) > MAX_SYMBOL_QUERY_CHARS:
                raise SymbolIndexError("decomp ledger contains an invalid class")
            key = class_name.casefold()
            if key in seen:
                raise SymbolIndexError("decomp ledger contains conflicting canonical classes")
            seen.add(key)
            vftables = _canonical_address_list(raw["vftable"], label="decomp vftable")
            ctors = _canonical_address_list(raw["ctor"], label="decomp ctor")
            rows.append(
                _LedgerRow(
                    class_name=class_name,
                    vftables=vftables,
                    ctors=ctors,
                    status=_bounded(raw["status"], 128),
                    notes=_bounded(raw["notes"], MAX_SYMBOL_SUMMARY_CHARS),
                    line=line,
                )
            )
        if not rows:
            raise SymbolIndexError("decomp ledger contains no rows")
        return tuple(rows)

    def _parse_class_ids(
        self,
        canonical_classes: dict[str, str],
    ) -> tuple[_ClassIdRow, ...]:
        rows: list[_ClassIdRow] = []
        sites: dict[str, tuple[str, str | None]] = {}
        for line, raw in _strict_csv_rows(
            self._text(CLASS_IDS_PATH),
            path=CLASS_IDS_PATH,
            headers=CLASS_ID_HEADERS,
            delimiter=" ",
            skipinitialspace=True,
        ):
            immediate = raw["imm_fourcc"]
            fourcc = raw["reversed"]
            if (
                FOURCC_PATTERN.fullmatch(immediate) is None
                or FOURCC_PATTERN.fullmatch(fourcc) is None
                or immediate[::-1] != fourcc
            ):
                raise SymbolIndexError("class-id row has a conflicting canonical FourCC")
            site = _canonical_address(raw["@site"], label="class-id site")
            function_raw = raw["function"].strip()
            function = (
                None
                if function_raw == "??"
                else _canonical_address(function_raw, label="class-id function")
            )
            nearby = _normalize_nearby_class(raw["class_or_nearby_string"])
            canonical = canonical_classes.get((nearby or "").casefold())
            identity = (fourcc.casefold(), (nearby or "").casefold() or None)
            prior = sites.get(site)
            if prior is not None and prior != identity:
                raise SymbolIndexError("class-id rows conflict at one registration site")
            if prior is not None:
                raise SymbolIndexError("class-id rows repeat one registration site")
            sites[site] = identity
            rows.append(
                _ClassIdRow(
                    fourcc=fourcc,
                    site=site,
                    function=function,
                    nearby_class=nearby,
                    canonical_class=canonical,
                    line=line,
                )
            )
        if not rows:
            raise SymbolIndexError("class-id table contains no rows")
        return tuple(rows)

    def _section(
        self,
        lines: list[str],
        heading_pattern: re.Pattern[str],
        *,
        required: bool,
    ) -> tuple[int, list[tuple[int, str]]] | None:
        matches = [
            index
            for index, line in enumerate(lines)
            if heading_pattern.fullmatch(line)
        ]
        if len(matches) > 1:
            raise SymbolIndexError("class document repeats a canonical section")
        if not matches:
            if required:
                raise SymbolIndexError("class document is missing a canonical section")
            return None
        start = matches[0]
        body: list[tuple[int, str]] = []
        for index in range(start + 1, len(lines)):
            if lines[index].startswith("## "):
                break
            body.append((index + 1, lines[index]))
        return start + 1, body

    def _first_table(
        self,
        section: list[tuple[int, str]],
    ) -> tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] | None:
        header: tuple[str, ...] | None = None
        rows: list[tuple[int, tuple[str, ...]]] = []
        separator_seen = False
        for line_number, line in section:
            cells = _table_cells(line)
            if cells is None:
                if header is not None and separator_seen:
                    break
                continue
            if header is None:
                header = cells
                continue
            if _is_separator_row(cells):
                if separator_seen or len(cells) != len(header):
                    raise SymbolIndexError("class document has a malformed table separator")
                separator_seen = True
                continue
            if not separator_seen or len(cells) != len(header):
                raise SymbolIndexError("class document has a malformed table row")
            rows.append((line_number, cells))
        if header is None:
            return None
        if not separator_seen:
            raise SymbolIndexError("class document table is missing its separator")
        return header, rows

    def _parse_class_doc(self, class_name: str) -> _ClassDoc:
        path = f"{DECOMP_DOCS_PREFIX}{class_name}.md"
        if path not in self.snapshot.files_by_path:
            raise SymbolIndexError("a decomp ledger class document is missing")
        lines = self._text(path).splitlines()
        identity_section = self._section(
            lines, re.compile(r"## Identity"), required=True
        )
        assert identity_section is not None
        identity_table = self._first_table(identity_section[1])
        if identity_table is None or identity_table[0] != ("Item", "Value"):
            raise SymbolIndexError("class Identity table has unexpected headers")
        identity: dict[str, str] = {}
        for _, cells in identity_table[1]:
            item = _plain_markdown(cells[0])
            if item in identity:
                raise SymbolIndexError("class Identity table repeats an item")
            identity[item] = cells[1].strip()
        rtti = _plain_markdown(identity.get("RTTI name", ""))
        if rtti != class_name:
            raise SymbolIndexError("class document conflicts with its canonical RTTI name")
        explicit_fourcc = None
        if "FourCC" in identity:
            match = EXPLICIT_FOURCC_PATTERN.match(identity["FourCC"])
            if match is not None:
                explicit_fourcc = match.group(1)
            elif identity["FourCC"].lstrip().startswith("`"):
                raise SymbolIndexError("class Identity has an invalid explicit FourCC")
        signatures = [
            _bounded(_plain_markdown(identity[key]), 1_000)
            for key in ("Signature", "Prototype")
            if key in identity and _plain_markdown(identity[key])
        ]
        if len(signatures) > 1 and len(set(signatures)) > 1:
            raise SymbolIndexError("class Identity has conflicting signature fields")
        class_signature = signatures[0] if signatures else None

        method_section = self._section(
            lines, re.compile(r"## Vtable Methods(?: \(owned\))?"), required=True
        )
        assert method_section is not None
        method_table = self._first_table(method_section[1])
        methods: list[_MethodRow] = []
        if method_table is not None:
            headers, table_rows = method_table
            signature_headers = tuple(
                header for header in headers if header in {"Signature", "Prototype"}
            )
            if len(signature_headers) > 1:
                raise SymbolIndexError("Vtable Methods table has unexpected headers")
            structural_headers = tuple(
                header for header in headers if header not in {"Signature", "Prototype"}
            )
            accepted_headers = {
                ("Slot", "Address", "Name", "Behavior", "Status"),
                ("Slot", "Address", "Role", "Behavior"),
                ("Slot", "Address", "Name", "Behavior"),
                ("Slot(s)", "Address(es)", "Name", "Behavior", "Status"),
            }
            if structural_headers not in accepted_headers:
                raise SymbolIndexError("Vtable Methods table has unexpected headers")
            address_header = "Address" if "Address" in headers else "Address(es)"
            name_header = "Name" if "Name" in headers else "Role"
            for line, cells in table_rows:
                row = dict(zip(headers, cells, strict=True))
                name = _plain_markdown(row[name_header])
                if not name:
                    raise SymbolIndexError("Vtable Methods row has no name")
                signature_value = row.get("Signature", row.get("Prototype", ""))
                methods.append(
                    _MethodRow(
                        name=name[:500],
                        addresses=_addresses_from_cell(row[address_header]),
                        signature=_bounded(_plain_markdown(signature_value), 1_000),
                        status=_bounded(_plain_markdown(row.get("Status", "")), 128),
                        summary=_bounded(
                            _plain_markdown(row["Behavior"]),
                            MAX_SYMBOL_SUMMARY_CHARS,
                        ),
                        line=line,
                    )
                )
        return _ClassDoc(
            explicit_fourcc=explicit_fourcc,
            signature=class_signature,
            methods=tuple(methods),
        )

    def _parse_linkage(
        self,
        canonical_classes: dict[str, str],
    ) -> dict[str, tuple[LinkageRecord, ...]]:
        grouped: defaultdict[str, list[LinkageRecord]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for line, raw in _strict_csv_rows(
            self._text(LINKAGE_PATH), path=LINKAGE_PATH, headers=LINKAGE_HEADERS
        ):
            class_name = canonical_classes.get(raw["class"].strip().casefold())
            if class_name is None:
                raise SymbolIndexError("linkage row references an unknown canonical class")
            aspect = _bounded(raw["aspect"], 256)
            domain = _bounded(raw["domain"], 256)
            if aspect is None or domain is None:
                raise SymbolIndexError("linkage row is missing its aspect or domain")
            try:
                status = LinkageStatus(raw["status"].strip().casefold())
            except ValueError as exc:
                raise SymbolIndexError("linkage row has an invalid status") from exc
            identity = (class_name.casefold(), aspect.casefold())
            if identity in seen:
                raise SymbolIndexError("linkage table repeats a class/aspect identity")
            seen.add(identity)
            grouped[class_name.casefold()].append(
                LinkageRecord(
                    aspect=aspect,
                    domain=domain,
                    status=status,
                    oracle=_bounded(raw["oracle"], 500),
                    source=_source(self.snapshot, LINKAGE_PATH, line),
                )
            )
        return {
            key: tuple(
                sorted(
                    records,
                    key=lambda record: (
                        record.aspect.casefold(),
                        record.domain.casefold(),
                        record.source.line or 0,
                    ),
                )[:MAX_LINKAGE_PER_SYMBOL]
            )
            for key, records in grouped.items()
        }

    def _validate_explicit_fourccs(
        self,
        ledger: tuple[_LedgerRow, ...],
        docs: dict[str, _ClassDoc],
        class_ids: tuple[_ClassIdRow, ...],
    ) -> None:
        by_class: defaultdict[str, set[str]] = defaultdict(set)
        for row in class_ids:
            if row.canonical_class is not None:
                by_class[row.canonical_class.casefold()].add(row.fourcc.casefold())
        for row in ledger:
            explicit = docs[row.class_name].explicit_fourcc
            committed = by_class[row.class_name.casefold()]
            if explicit is not None and committed and explicit.casefold() not in committed:
                raise SymbolIndexError("class document conflicts with canonical FourCC rows")

    def _class_fourccs(
        self,
        ledger: tuple[_LedgerRow, ...],
        docs: dict[str, _ClassDoc],
        class_ids: tuple[_ClassIdRow, ...],
    ) -> dict[str, str | None]:
        by_class: defaultdict[str, set[str]] = defaultdict(set)
        orientation: dict[tuple[str, str], str] = {}
        for row in class_ids:
            if row.canonical_class is None:
                continue
            key = row.canonical_class.casefold()
            folded = row.fourcc.casefold()
            by_class[key].add(folded)
            orientation[(key, folded)] = row.fourcc
        selected: dict[str, str | None] = {}
        for row in ledger:
            key = row.class_name.casefold()
            explicit = docs[row.class_name].explicit_fourcc
            if explicit is not None:
                selected[key] = explicit
            elif len(by_class[key]) == 1:
                folded = next(iter(by_class[key]))
                selected[key] = orientation[(key, folded)]
            else:
                selected[key] = None
        return selected

    def _build_records(
        self,
        ledger: tuple[_LedgerRow, ...],
        class_ids: tuple[_ClassIdRow, ...],
        docs: dict[str, _ClassDoc],
        linkage: dict[str, tuple[LinkageRecord, ...]],
    ) -> list[SymbolRecord]:
        records: list[SymbolRecord] = []
        fourccs = self._class_fourccs(ledger, docs, class_ids)
        for row in ledger:
            key = row.class_name.casefold()
            class_linkage = linkage.get(key, ())
            addresses = row.vftables + row.ctors
            for address in addresses or (None,):
                records.append(
                    SymbolRecord(
                        kind=SymbolKind.CLASS,
                        name=row.class_name,
                        address=address,
                        signature=docs[row.class_name].signature,
                        class_name=row.class_name,
                        fourcc=fourccs[key],
                        status=row.status,
                        linkage=class_linkage,
                        summary=row.notes,
                        source=_source(self.snapshot, DECOMP_PATH, row.line),
                    )
                )
            doc_path = f"{DECOMP_DOCS_PREFIX}{row.class_name}.md"
            for method in docs[row.class_name].methods:
                for address in method.addresses:
                    records.append(
                        SymbolRecord(
                            kind=SymbolKind.FUNCTION,
                            name=method.name,
                            address=address,
                            signature=method.signature,
                            class_name=row.class_name,
                            fourcc=fourccs[key],
                            status=method.status,
                            linkage=class_linkage,
                            summary=method.summary,
                            source=_source(self.snapshot, doc_path, method.line),
                        )
                    )
        for row in class_ids:
            class_name = row.canonical_class or row.nearby_class
            for address, label in ((row.site, "registration site"), (row.function, "registration function")):
                if address is None:
                    continue
                records.append(
                    SymbolRecord(
                        kind=SymbolKind.FOURCC,
                        name=row.fourcc,
                        address=address,
                        signature=None,
                        class_name=class_name,
                        fourcc=row.fourcc,
                        status=None,
                        linkage=linkage.get((row.canonical_class or "").casefold(), ()),
                        summary=label,
                        source=_source(self.snapshot, CLASS_IDS_PATH, row.line),
                    )
                )
        return records

    @staticmethod
    def _stable_record_key(record: SymbolRecord) -> tuple[object, ...]:
        return (
            record.kind.value,
            record.source.path,
            record.source.line or 0,
            record.address or "",
            record.name.casefold(),
            (record.class_name or "").casefold(),
            (record.fourcc or "").casefold(),
        )

    @staticmethod
    def _match_rank(value: str | None, query: str | None) -> int:
        if query is None:
            return 0
        folded_value = (value or "").casefold()
        folded_query = query.casefold()
        if folded_value == folded_query:
            return 0
        if folded_value.startswith(folded_query):
            return 1
        return 2

    def lookup(
        self,
        *,
        name: str | None = None,
        address: str | None = None,
        class_name: str | None = None,
        fourcc: str | None = None,
        limit: int = DEFAULT_SYMBOL_LIMIT,
    ) -> SymbolLookup:
        normalized_name = name.strip() if name is not None else None
        normalized_class = class_name.strip() if class_name is not None else None
        if normalized_name == "" or normalized_class == "":
            raise SymbolRequestError("name and class_name must not be blank")
        if normalized_name is not None and len(normalized_name) > MAX_SYMBOL_QUERY_CHARS:
            raise SymbolRequestError("name must contain 1..128 characters")
        if normalized_class is not None and len(normalized_class) > MAX_SYMBOL_QUERY_CHARS:
            raise SymbolRequestError("class_name must contain 1..128 characters")
        normalized_address = normalize_address(address) if address is not None else None
        if fourcc is not None and FOURCC_PATTERN.fullmatch(fourcc) is None:
            raise SymbolRequestError("fourcc must be exactly four printable ASCII characters")
        if not any((normalized_name, normalized_address, normalized_class, fourcc)):
            raise SymbolRequestError("at least one symbol lookup axis is required")
        if not 1 <= limit <= MAX_SYMBOL_LIMIT:
            raise SymbolRequestError("limit must be in 1..50")

        matches = [
            record
            for record in self._records
            if (
                normalized_name is None
                or normalized_name.casefold() in record.name.casefold()
            )
            and (normalized_address is None or record.address == normalized_address)
            and (
                normalized_class is None
                or normalized_class.casefold() in (record.class_name or "").casefold()
            )
            and (
                fourcc is None
                or fourcc.casefold() == (record.fourcc or "").casefold()
            )
        ]
        matches.sort(
            key=lambda record: (
                self._match_rank(record.name, normalized_name),
                self._match_rank(record.class_name, normalized_class),
                self._match_rank(record.fourcc, fourcc),
                record.kind.value,
                record.source.path,
                record.source.line or 0,
                record.address or "",
                record.name.casefold(),
            )
        )
        selected = tuple(matches[:limit])
        query = SymbolQuery(
            name=normalized_name,
            address=normalized_address,
            class_name=normalized_class,
            fourcc=fourcc,
        )
        return SymbolLookup(query=query, count=len(selected), results=selected)

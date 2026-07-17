"""Structured symbol parsing, lookup semantics, and real-corpus grounding."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from app.core.snapshot import compute_content_inventory, validate_snapshot
from app.core.symbol_index import (
    CLASS_IDS_PATH,
    DECOMP_HEADERS,
    DECOMP_PATH,
    LINKAGE_HEADERS,
    LINKAGE_PATH,
    LinkageStatus,
    SymbolIndex,
    SymbolIndexError,
    SymbolKind,
    SymbolRequestError,
)
from tests.conftest import GROUNDING_COMMIT

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)


def _refresh_manifest(snapshot: Path) -> None:
    manifest_path = snapshot / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    inventory = compute_content_inventory(snapshot / "content")
    payload.update(
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        content_sha256=inventory.content_sha256,
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def _write(snapshot: Path, relative: str, text: str) -> None:
    path = snapshot / "content" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ledger_row(
    class_name: str,
    vftable: str,
    ctor: str,
    notes: str,
) -> str:
    return (
        f"{class_name},CBase,{vftable},{ctor},2,family,1,spec,owner,High,{notes}"
    )


@pytest.fixture()
def symbol_snapshot(snapshot: Path) -> Path:
    _write(
        snapshot,
        DECOMP_PATH,
        ",".join(DECOMP_HEADERS)
        + "\n"
        + _ledger_row(
            "CAlpha",
            "00401000;00401010",
            "00401020",
            "Alpha class summary",
        )
        + "\n"
        + _ledger_row("CAddressless", "", "", "No known class address")
        + "\n",
    )
    _write(
        snapshot,
        CLASS_IDS_PATH,
        "imm_fourcc reversed @site function class_or_nearby_string\n"
        "PLA3 3ALP @00402000 FUN_00437c40 CAlpha()\n"
        "ATEB BETA @00403000 ?? CAddressless\n",
    )
    _write(
        snapshot,
        LINKAGE_PATH,
        ",".join(LINKAGE_HEADERS)
        + "\nCAlpha,free-roam,gameplay,linked,oracle.py,"
        "docs/decomp/CAlpha.md,grounded\n",
    )
    _write(
        snapshot,
        "docs/decomp/CAlpha.md",
        """# CAlpha
## Identity

| Item | Value |
|---|---|
| RTTI name | `CAlpha` |
| Base chain | `CBase` |
| Vftable(s) | `00401000`, `00401010` |
| Ctor(s) | `00401020` |
| Dtor(s) | TODO |
| Ledger row | `docs/decomp_ledger.csv` |
| FourCC | `3ALP` |

## Vtable Methods

| Slot | Address | Name | Behavior | Status | Signature |
|---:|---|---|---|---|---|
| 1 | `00437c40` | `UpdateAlpha` | Updates alpha state. | non-trivial | `void CAlpha::UpdateAlpha()` |
| 2 | `00404000` | `AlphaHelper` | Helper body. | TODO | |

## Notes
Done.
""",
    )
    _write(
        snapshot,
        "docs/decomp/CAddressless.md",
        """# CAddressless
## Identity

| Item | Value |
|---|---|
| RTTI name | `CAddressless` |
| Base chain | `CBase` |
| Ctor(s) | TODO |
| Dtor(s) | TODO |
| Ledger row | `docs/decomp_ledger.csv` |

## Vtable Methods (owned)

No class-owned methods are documented.
""",
    )
    _refresh_manifest(snapshot)
    return snapshot


@pytest.fixture()
def index(symbol_snapshot: Path) -> SymbolIndex:
    return SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))


def test_source_counts_record_shapes_and_addressless_class(index: SymbolIndex):
    assert (index.decomp_row_count, index.class_id_row_count, index.linkage_row_count) == (
        2,
        2,
        1,
    )
    assert Counter(record.kind for record in index.records) == {
        SymbolKind.CLASS: 4,
        SymbolKind.FUNCTION: 2,
        SymbolKind.FOURCC: 3,
    }
    addressless = index.lookup(name="CAddressless", limit=50)
    assert addressless.count == 1
    assert addressless.results[0].address is None
    assert addressless.results[0].signature is None


def test_name_class_fourcc_and_combined_axes(index: SymbolIndex):
    by_name = index.lookup(name="updatealpha")
    assert [record.name for record in by_name.results] == ["UpdateAlpha"]
    by_class = index.lookup(class_name="alpha", limit=50)
    assert by_class.count > 1
    assert all("alpha" in (record.class_name or "").casefold() for record in by_class.results)
    by_fourcc = index.lookup(fourcc="3alp", limit=50)
    assert by_fourcc.count > 1
    assert all(record.fourcc == "3ALP" for record in by_fourcc.results)
    combined = index.lookup(
        name="update",
        class_name="CAlpha",
        fourcc="3ALP",
    )
    assert [record.name for record in combined.results] == ["UpdateAlpha"]


@pytest.mark.parametrize("address", ["00437c40", "0x00437C40", "FUN_00437c40", "@00437c40"])
def test_address_normalization_and_documented_duplicates(
    index: SymbolIndex,
    address: str,
):
    result = index.lookup(address=address, limit=50)
    assert result.query.address == "00437c40"
    assert [(record.kind, record.name) for record in result.results] == [
        (SymbolKind.FOURCC, "3ALP"),
        (SymbolKind.FUNCTION, "UpdateAlpha"),
    ]


def test_signature_linkage_summary_sources_and_committed_orientation(index: SymbolIndex):
    record = index.lookup(name="UpdateAlpha").results[0]
    assert record.signature == "void CAlpha::UpdateAlpha()"
    assert record.summary == "Updates alpha state."
    assert record.fourcc == "3ALP"
    assert record.source.path == "docs/decomp/CAlpha.md"
    assert record.source.line is not None
    assert GROUNDING_COMMIT in record.source.url
    assert len(record.linkage) == 1
    assert record.linkage[0].status is LinkageStatus.LINKED
    assert record.linkage[0].source.path == LINKAGE_PATH
    assert index.lookup(name="AlphaHelper").results[0].signature is None


def test_exact_matches_sort_before_prefix_and_substring(index: SymbolIndex):
    exact = index.lookup(name="CAlpha", limit=50)
    assert exact.results
    assert all(record.name == "CAlpha" for record in exact.results)
    partial = index.lookup(name="Alpha", limit=50)
    assert partial.results[0].name == "AlphaHelper"
    assert partial.results[-1].name == "UpdateAlpha"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"name": " "},
        {"class_name": "x" * 129},
        {"address": "0x123"},
        {"address": "00437c40junk"},
        {"fourcc": "ABC"},
        {"fourcc": "A\nCD"},
        {"name": "Alpha", "limit": 0},
        {"name": "Alpha", "limit": 51},
    ],
)
def test_invalid_queries_fail_closed(index: SymbolIndex, kwargs: dict[str, object]):
    with pytest.raises(SymbolRequestError):
        index.lookup(**kwargs)


def test_limit_is_applied_after_deterministic_sort(index: SymbolIndex):
    one = index.lookup(class_name="CAlpha", limit=1)
    all_records = index.lookup(class_name="CAlpha", limit=50)
    assert one.count == 1
    assert one.results == all_records.results[:1]


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (DECOMP_PATH, "wrong,headers\nx,y\n", "unexpected headers"),
        (
            CLASS_IDS_PATH,
            "wrong headers\nvalue value\n",
            "unexpected headers",
        ),
        (LINKAGE_PATH, "wrong,headers\nx,y\n", "unexpected headers"),
        (
            "docs/decomp/CAlpha.md",
            "# CAlpha\n## Identity\n| Wrong | Header |\n|---|---|\n| x | y |\n"
            "## Vtable Methods\nnone\n",
            "Identity table has unexpected headers",
        ),
        (
            "docs/decomp/CAlpha.md",
            "# CAlpha\n## Identity\n| Item | Value |\n|---|---|\n"
            "| RTTI name | `CWrong` |\n## Vtable Methods\nnone\n",
            "canonical RTTI",
        ),
        (
            "docs/decomp/CAlpha.md",
            "# CAlpha\n## Identity\n| Item | Value |\n|---|---|\n"
            "| RTTI name | `CAlpha` |\n| FourCC | `3BAD` |\n"
            "## Vtable Methods\nnone\n",
            "canonical FourCC",
        ),
    ],
)
def test_malformed_canonical_sources_fail_startup(
    symbol_snapshot: Path,
    path: str,
    replacement: str,
    message: str,
):
    _write(symbol_snapshot, path, replacement)
    _refresh_manifest(symbol_snapshot)
    with pytest.raises(SymbolIndexError, match=message):
        SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))


def test_missing_referenced_class_doc_fails_startup(symbol_snapshot: Path):
    (symbol_snapshot / "content/docs/decomp/CAlpha.md").unlink()
    _refresh_manifest(symbol_snapshot)
    with pytest.raises(SymbolIndexError, match="class document is missing"):
        SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))


def test_conflicting_class_and_registration_site_fail_startup(symbol_snapshot: Path):
    ledger = (symbol_snapshot / "content" / DECOMP_PATH).read_text(encoding="utf-8")
    _write(
        symbol_snapshot,
        DECOMP_PATH,
        ledger + _ledger_row("calpha", "00409999", "", "conflict") + "\n",
    )
    _refresh_manifest(symbol_snapshot)
    with pytest.raises(SymbolIndexError, match="conflicting canonical classes"):
        SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))

    _write(
        symbol_snapshot,
        DECOMP_PATH,
        ledger,
    )
    class_ids = (symbol_snapshot / "content" / CLASS_IDS_PATH).read_text(
        encoding="utf-8"
    )
    _write(
        symbol_snapshot,
        CLASS_IDS_PATH,
        class_ids + "ATEZ ZETA @00402000 ?? CAddressless\n",
    )
    _refresh_manifest(symbol_snapshot)
    with pytest.raises(SymbolIndexError, match="conflict at one registration site"):
        SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))


def test_vtable_method_missing_required_header_fails_startup(symbol_snapshot: Path):
    path = symbol_snapshot / "content/docs/decomp/CAlpha.md"
    text = path.read_text(encoding="utf-8").replace(
        "| Slot | Address | Name | Behavior | Status | Signature |",
        "| Slot | Address | Name | Status | Signature |",
    ).replace(
        "|---:|---|---|---|---|---|",
        "|---:|---|---|---|---|",
    ).replace(
        "| 1 | `00437c40` | `UpdateAlpha` | Updates alpha state. | non-trivial | `void CAlpha::UpdateAlpha()` |",
        "| 1 | `00437c40` | `UpdateAlpha` | non-trivial | `void CAlpha::UpdateAlpha()` |",
    ).replace(
        "| 2 | `00404000` | `AlphaHelper` | Helper body. | TODO | |",
        "| 2 | `00404000` | `AlphaHelper` | TODO | |",
    )
    path.write_text(text, encoding="utf-8")
    _refresh_manifest(symbol_snapshot)
    with pytest.raises(SymbolIndexError, match="unexpected headers"):
        SymbolIndex(validate_snapshot(symbol_snapshot, require_read_only=False))


def test_real_symbol_grounding_and_required_counts():
    assert REAL_SNAPSHOT.is_dir()
    index = SymbolIndex(validate_snapshot(REAL_SNAPSHOT))
    assert index.decomp_row_count == 208
    assert index.linkage_row_count == 29
    assert index.class_id_row_count == 238

    player = index.lookup(name="C3DPlayer", limit=50)
    assert player.count == 5
    assert all(record.class_name == "C3DPlayer" for record in player.results)
    assert any(
        linkage.status is LinkageStatus.LINKED_BLOCKED
        for linkage in player.results[0].linkage
    )

    address = index.lookup(address="00437c40", limit=50)
    assert any(
        record.kind is SymbolKind.FUNCTION
        and record.name == "UpdateGroundMoveA"
        and record.class_name == "C3DPlayer"
        and record.signature is None
        for record in address.results
    )

    fourcc = index.lookup(fourcc="3AIT", limit=50)
    assert any(
        record.kind is SymbolKind.FOURCC
        and record.class_name == "C3DAITrigger"
        and record.fourcc == "3AIT"
        for record in fourcc.results
    )

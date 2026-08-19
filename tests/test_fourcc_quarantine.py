"""The registrar scan must be able to veto a class document's FourCC claim.

Regression test for the blind spot measured against jn-engine 9f4d2f6, where
lookup_symbol served 3DIN for C3DDarwinFish and 5VEL - a *level* class id - for
C3DSparrow. Both classes lack a named registrar row, so the original guard
short-circuited and the document's claim won unopposed.
"""
from collections import defaultdict

from app.core.symbol_index import SymbolIndex

LEVEL_IDS = {"lev5", "5vel"}


def _owners(mapping):
    out = defaultdict(set)
    for fourcc, classes in mapping.items():
        out[fourcc] = set(classes)
    return out


def test_own_registrar_row_is_always_honoured():
    """A class that owns the FourCC is never suppressed, whatever else says otherwise."""
    assert not SymbolIndex._fourcc_is_contradicted(
        "c3deye", "3EYE", {"3eye"}, _owners({"3eye": {"c3deye"}}), LEVEL_IDS,
        {"3eye": "c3dsomethingelse"},
    )


def test_level_class_id_is_rejected():
    """C3DSparrow claiming 5VEL - the class id of level 5."""
    assert SymbolIndex._fourcc_is_contradicted(
        "c3dsparrow", "5VEL", set(), _owners({}), LEVEL_IDS, {},
    )


def test_fourcc_owned_by_another_named_class_is_rejected():
    assert SymbolIndex._fourcc_is_contradicted(
        "c3dvrtrophy", "3TRO", set(), _owners({"3tro": {"c3dtrophy"}}), LEVEL_IDS, {},
    )


def test_shipped_object_tag_can_contradict():
    """C3DDarwinFish claiming 3DIN, which the shipped levels tag C3DDINO."""
    tags = {"3din": "c3ddino", "3fis": "c3ddarwinfish"}
    assert SymbolIndex._fourcc_is_contradicted(
        "c3ddarwinfish", "3DIN", set(), _owners({}), LEVEL_IDS, tags,
    )


def test_unknown_tag_does_not_veto():
    """An ObjectTag naming nothing we know of must not suppress a valid claim."""
    tags = {"3xyz": "somethingunrelated"}
    assert not SymbolIndex._fourcc_is_contradicted(
        "c3dfan", "3FAN", set(), _owners({}), LEVEL_IDS, tags,
    )


def test_no_evidence_means_no_suppression():
    """With nothing to contradict it, an uncorroborated claim is still served."""
    assert not SymbolIndex._fourcc_is_contradicted(
        "c3dgate1", "3GAT", set(), _owners({}), LEVEL_IDS, {},
    )

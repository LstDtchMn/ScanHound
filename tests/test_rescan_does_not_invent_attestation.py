"""A rescan must not decide the attestation state of a row that never had one.

``category_attested`` is a THREE-state fact and the third state lives in the
KEY, not the value:

    key absent -> the crawl has never checked this release
    True       -> the crawl attested this release's category
    False      -> checked, not attested

``attest_scan_categories`` (the ONE writer that reaches a release the crawl
skips as already cached) deliberately skips any row where the key is already
PRESENT, because it is a one-time backfill over exactly those three states.

So persisting ``category_attested: False`` onto a key-absent row is not a
neutral write -- it permanently withdraws that row from the only process that
could ever attest it, and ``get_scan_category`` then returns ``None`` for it
forever.

The control is the whole test. Both rows are identical; the only difference is
whether a rescan touched one of them.
"""
import copy
import json

import pytest

from backend.api.routes import scanner as scanner_routes


#: The PAYLOAD -- what lives inside a background_scan_cache row's JSON blob.
CACHED_NEVER_CHECKED = {
    "url": "https://example.invalid/release/never-checked",
    "title": "Some Release 2026",
    "category": "tv",
    # NOTE: no "category_attested" key at all. That is the state under test.
}


def _row(payload):
    """rescan_classification takes a cache ROW and parses row["data"]; it does
    not take the payload. Handing it the payload directly returns ('', False,
    None) for everything, which reads exactly like the fix failing."""
    return {"url": payload.get("url"), "data": json.dumps(payload)}


def test_the_fixture_really_is_key_absent():
    """Premise. If this row ever gains the key, every assertion below is
    vacuous -- it would be testing the False path, not the absent path."""
    assert "category_attested" not in CACHED_NEVER_CHECKED


def test_the_fixture_row_is_actually_readable():
    """Second premise, and it earned its place: the first version of this file
    passed the PAYLOAD where a cache ROW was expected, so every field came back
    empty and the whole suite read as the fix not working. If the category does
    not survive, nothing below is testing what it claims."""
    category, _conflict, _attested = scanner_routes.rescan_classification(
        _row(copy.deepcopy(CACHED_NEVER_CHECKED))
    )
    assert category == "tv", (
        "the fixture row is not readable by rescan_classification (category "
        "came back %r); every assertion in this file would be vacuous" % (category,)
    )


def test_rescan_classification_reports_unknown_as_None_not_False():
    """False means 'checked and not attested'. A row nobody checked must not
    claim to have been checked."""
    _category, _conflict, attested = scanner_routes.rescan_classification(
        _row(copy.deepcopy(CACHED_NEVER_CHECKED))
    )
    assert attested is None, (
        "a never-checked row reported attested=%r; False would tell "
        "attest_scan_categories this row has already been decided" % (attested,)
    )


def test_a_checked_row_still_reports_its_real_value():
    """Control for the above: the fix must not turn every row into unknown."""
    for stored, expected in ((True, True), (False, False)):
        payload = dict(CACHED_NEVER_CHECKED, category_attested=stored)
        _c, _x, attested = scanner_routes.rescan_classification(_row(payload))
        assert attested is expected, (
            "a row storing %r reported %r" % (stored, attested)
        )


def test_the_serializer_omits_the_key_when_it_is_unknown():
    """The persistence boundary. Emitting the key AT ALL is the destructive
    act, so the serializer must leave it out rather than write a value."""
    scanner_service = pytest.importorskip("backend.scanner_service")
    item = scanner_service.MediaItem(id="x1", title="Some Release 2026", year=2026)
    assert item.category_attested is None, (
        "a freshly built MediaItem claims an attestation state it was never given"
    )
    persisted = scanner_routes._media_item_to_dict(item)
    assert "category_attested" not in persisted, (
        "the rescan persisted category_attested=%r onto a row that never had "
        "the key; attest_scan_categories skips any row where the key is "
        "present, so this withdraws the row from it permanently"
        % (persisted.get("category_attested"),)
    )


def test_the_serializer_still_emits_a_known_value():
    """Control. Omitting the key must be conditional on UNKNOWN, not blanket --
    otherwise a genuine attestation would stop being recorded."""
    scanner_service = pytest.importorskip("backend.scanner_service")
    for value in (True, False):
        item = scanner_service.MediaItem(id="x2", title="t", year=2026,
                                         category_attested=value)
        persisted = scanner_routes._media_item_to_dict(item)
        assert persisted.get("category_attested") is value, (
            "a row storing %r lost its recorded attestation" % (value,)
        )


def test_unknown_round_trips_as_unknown():
    """cache -> item -> cache. The state must survive the trip in both
    readers, which is where the sibling invariant was found enforced in one
    reader only."""
    scanner_service = pytest.importorskip("backend.scanner_service")
    # _media_item_from_dict is an INSTANCE method. Build the row the way the
    # cache actually stores it -- id/title/year are required to reconstruct.
    row = dict(copy.deepcopy(CACHED_NEVER_CHECKED),
               id="x3", title="Some Release 2026", year=2026)
    svc = scanner_service.ScannerService.__new__(scanner_service.ScannerService)
    restored = svc._media_item_from_dict(row)
    assert restored is not None, "the cache reader rejected the fixture row"
    assert restored.category_attested is None, (
        "the cache reader invented attested=%r for a key-absent row"
        % (restored.category_attested,)
    )
    again = scanner_routes._media_item_to_dict(restored)
    assert "category_attested" not in again, (
        "unknown did not survive the round trip; it came back as %r"
        % (again.get("category_attested"),)
    )

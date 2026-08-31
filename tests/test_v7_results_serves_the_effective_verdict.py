"""V7: the API and the matcher must answer the same thing about one row.

TEMPORARY. This file exists only for the V6/V7 bridge and is DELETED when the
canonical media-type state becomes the sole reader and writer of the verdict
(docs/design/2026-08-31-media-type-authority-model.md, Phase B).

THE DEFECT. ``database.mark_scan_category_conflict`` sets ``category_conflict``
in place on a row the crawl SKIPS as already cached, and nothing re-derives the
stored verdict. ``backend/api/routes/results.py`` had ZERO references to
``cached_media_type`` and served the RAW blob, while the matcher goes through
``cached_media_type``. So after an out-of-band conflict mark the API said
'movie'/'tv' about a row the matcher called 'ambiguous' -- 3 of the 12
reachable listing rows at d04ab63.

THE SHAPE OF THE FIX, and why this one. Read-side normalisation, NOT a
re-derive inside ``mark_scan_category_conflict``: the second would add another
old-model WRITER of the verdict, which is what the redesign is removing. A copy
of each cached row goes through the existing effective reader on the way out,
so serving, filtering, faceting, bookmark keying and export all see one answer.
No persisted byte changes.

TWO INSTRUMENTS, DELIBERATELY. ``test_v7_end_to_end_*`` drives the real
endpoint against a real DB row and the real ``mark_scan_category_conflict`` --
the consumer, not the component. The unit tests below pin the specific
behaviours that endpoint test would still pass with if the normalisation were
applied in the wrong place or to the wrong field.

WHAT THE END-TO-END TEST FOUND, which the unit tests could not. The first run
of ``test_v7_end_to_end_out_of_band_conflict_mark`` FAILED with the bridge
already in place: ``mark_scan_category_conflict`` mutates the blob in place, so
it changes neither ``COUNT(*)`` nor ``MAX(last_seen_at)``, and
``get_background_cache_version`` -- the parse-cache invalidation token -- was
unchanged. ``/results/cached`` kept serving its memoised PRE-MARK parse, so the
normalisation ran on rows that still said ``category_conflict: False``. Correct
code reaching nobody. ``database.py`` now bumps ``_bg_cache_rev`` there (and in
``attest_scan_categories``, the same in-place blob write with the same gap),
which is the bump ``rematch_cache`` and the reparse pass already carry.

HOW EACH ASSERTION WAS SHOWN TO FAIL:

  * ``test_v7_bypassing_the_bridge_reproduces_the_defect`` runs the SAME rows
    through the raw-blob read the module did before the bridge and asserts the
    disagreements come back -- the defect reintroduced inside the suite rather
    than by hand. It fails if either half of the bridge is ever a no-op.
  * Replacing ``_load_cached_items``' ``items.append(_normalize_cached_row(data))``
    with ``items.append(data)``: the end-to-end test fails, serving 'movie' for
    a row the matcher calls 'ambiguous'.
  * Deleting the ``_bg_cache_rev`` bump from ``mark_scan_category_conflict``:
    the end-to-end test fails the same way, which is what proves the bump is
    load-bearing and not decoration.
  * Deleting the same bump from ``attest_scan_categories``:
    ``test_v7_an_in_place_blob_write_invalidates_the_parse_cache`` fails on
    that operation by name.

Deliberately no line numbers above. This branch already carries a harness
whose literal line numbers drifted into comments
(tests/tools/r4_94_1_mutation_check.py), which is what an address-based
reference decays into.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import backend.api.dependencies as deps
from backend.api.main import create_app
from backend.api.routes import results as results_route
from backend.scanner_service import cached_media_type
from tests.tools import v6_v7_bridge_sweep as sweep

URL = "https://hdencode.org/v7-bridge-probe/"
TITLE = "V7 Bridge Probe 2019 1080p"


# ── the measurement, before and after, from one head ────────────────────────


def test_v7_bridge_leaves_no_api_vs_matcher_disagreement():
    """0 / 12 on the listing basis and 0 / 77 on the full reachable closure.

    The basis is the design lane's enumeration (tests/tools/reachable_rows.py),
    not a second one, so this number is comparable to the 3 / 12 the round-6
    design request reported.
    """
    result = sweep.measure()
    assert result["listing"]["total"] == 12, (
        "the listing basis is the 12 rows the round-6 figure was measured on; "
        "it changed size, so the before/after numbers are not comparable")
    assert result["listing"]["disagreements"] == []
    assert result["closure"]["total"] == 77
    assert result["closure"]["disagreements"] == []


def test_v7_bypassing_the_bridge_reproduces_the_defect():
    """THE DEFECT REINTRODUCED, and it must come back at the recorded size.

    ``--defect both`` is d04ab63: the writer blind to the conflict AND the
    reader serving the raw blob. If either half of the bridge is silently a
    no-op, one of these numbers collapses to 0 and this test fails.
    """
    both = sweep.measure(defect_v6=True, defect_v7=True)
    assert len(both["listing"]["disagreements"]) == 3, (
        "the round-6 figure was 3 of 12; reproduced %d"
        % len(both["listing"]["disagreements"]))
    stored = [d for d in both["closure"]["disagreements"]
              if d["shape"] == "current"]
    assert len(stored) == 3
    for d in stored:
        assert d["matcher"] == "ambiguous" and d["api"] in ("tv", "movie"), d
        assert d["conflict"] is True, d

    # V6 ALONE IS NOT ENOUGH: a row can acquire its conflict out of band, after
    # the writer is finished with it. Those rows are only in the closure.
    v7_only = sweep.measure(defect_v6=False, defect_v7=True)
    assert len([d for d in v7_only["closure"]["disagreements"]
                if d["shape"] == "current"]) == 3

    # V7 ALONE IS NOT ENOUGH EITHER, and this is the assertion that says why
    # the reviewer refused the split. With the reader normalising, this metric
    # reads 0 while the writer is still manufacturing the contradiction -- the
    # disagreement is not fixed, it is merely no longer observable HERE. The
    # instrument that does see it is the writer's own consistency test.
    v6_only = sweep.measure(defect_v6=True, defect_v7=False)
    assert v6_only["listing"]["disagreements"] == [], (
        "expected read-side normalisation to MASK the writer defect; if this "
        "ever fails, the masking argument in the sweep docstring is wrong")


# ── the unit behaviour of the normaliser ────────────────────────────────────


def test_v7_normalizer_rewrites_a_stale_stored_verdict():
    row = {"title": TITLE, "category": "4k", "season": None, "is_tv": False,
           "category_conflict": True, "category_attested": True,
           "media_type": "movie", "media_type_provisional": True}
    out = results_route._normalize_cached_row(dict(row))
    assert out["media_type"] == "ambiguous"
    assert out["media_type"] == cached_media_type(row)[0]
    assert out["media_type_provisional"] is True


def test_v7_normalizer_does_not_mutate_the_caller_s_row():
    """It normalises a COPY. ``_load_cached_items`` hands it the freshly parsed
    blob, but nothing downstream may depend on that being the only caller."""
    row = {"title": TITLE, "category": "4k", "season": None, "is_tv": False,
           "category_conflict": True, "category_attested": True,
           "media_type": "movie", "media_type_provisional": True}
    before = json.dumps(row, sort_keys=True)
    results_route._normalize_cached_row(dict(row))
    assert json.dumps(row, sort_keys=True) == before


def test_v7_normalizer_leaves_an_unconflicted_row_alone():
    """CONTROL. Without this, "set media_type to 'ambiguous' always" passes
    every other assertion about conflicted rows in this file."""
    row = {"title": TITLE, "category": "4k", "season": None, "is_tv": False,
           "category_conflict": False, "category_attested": True,
           "media_type": "movie", "media_type_provisional": True}
    out = results_route._normalize_cached_row(dict(row))
    assert out["media_type"] == "movie"
    assert out["media_type_provisional"] is True


def test_v7_normalizer_gives_a_legacy_row_the_verdict_the_matcher_derives():
    """A pre-#93 row stores no verdict. The matcher reconstructs one from the
    cached category/title/season; the API used to serve nothing, so the two
    disagreed on 34 of the 77 reachable rows. Normalising every row -- not only
    conflicted ones -- closes that too."""
    row = {"title": TITLE, "category": "tv", "season": None, "is_tv": True,
           "category_attested": True}
    out = results_route._normalize_cached_row(dict(row))
    assert out["media_type"] == "tv"
    assert out["media_type_provisional"] is True
    assert out["media_type"] == cached_media_type(row)[0]


def test_v7_bookmark_key_follows_the_effective_verdict():
    """The bookmark key is derived from ``media_type``, so normalising at the
    load boundary is what makes a conflicted row key as 'ambiguous' instead of
    as a confident 'movie' it no longer is."""
    row = {"title": TITLE, "year": 2019, "category": "4k", "season": None,
           "is_tv": False, "category_conflict": True, "category_attested": True,
           "media_type": "movie", "media_type_provisional": True}
    raw_key = results_route._bookmark_key_for_item(row)
    bridged_key = results_route._bookmark_key_for_item(
        results_route._normalize_cached_row(dict(row)))
    assert raw_key[-1] == "movie"
    assert bridged_key[-1] == "ambiguous"


# ── the consumer: the real endpoint, the real conflict writer ───────────────


def _seed(row):
    # The REGISTRY's DatabaseManager, not a fresh one. The parse-cache
    # invalidation token includes an in-process revision counter held on the
    # instance, so a second connection would mark the row in SQLite and leave
    # the serving instance's memo untouched -- which is not what production
    # does. background_scanner.py:588 calls this on the registry's db.
    deps.registry.db.upsert_background_cache([{
        "url": URL, "title": row["title"], "year": row.get("year", 2019),
        "status": "missing", "source_category": "HDEncode",
        "data": json.dumps(row)}])


def _mark_conflict():
    assert deps.registry.db.mark_scan_category_conflict([URL]) == 1


def _cached_item(client):
    resp = client.get("/results/cached", params={"per_page": 500})
    assert resp.status_code == 200, resp.text
    items = [i for i in resp.json()["items"] if i.get("url") == URL]
    assert len(items) == 1, "seeded row not served: %d matches" % len(items)
    return items[0]


@pytest.fixture()
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c
    # The parsed-cache memo is keyed on the cache VERSION, which every
    # upsert/mark bumps, so no cross-test bleed -- but reset it anyway so a
    # failure here is never a stale-memo artefact.
    results_route._cache_parse_cache["version"] = None


def test_v7_end_to_end_out_of_band_conflict_mark(client):
    """THE CONSUMER. Seed an unconflicted row exactly as the listing crawl
    writes it, serve it, then run the PRODUCTION conflict marker and serve it
    again. The endpoint's answer must move with the matcher's.

    This is the sequence V7 names: a release the crawl SKIPS as already cached,
    marked out of band, with no operation in between to re-derive anything.
    """
    row = {"url": URL, "title": TITLE, "year": 2019, "status": "missing",
           "category": "4k", "season": None, "is_tv": False,
           "category_conflict": False, "category_attested": True,
           "media_type": "movie", "media_type_provisional": True}
    _seed(row)
    before = _cached_item(client)
    assert before["media_type"] == "movie", before
    assert cached_media_type(row)[0] == "movie"

    _mark_conflict()
    marked = dict(row, category_conflict=True)
    after = _cached_item(client)
    assert cached_media_type(marked)[0] == "ambiguous", (
        "control: the matcher must refuse this row, or the test proves nothing")
    assert after["media_type"] == "ambiguous", (
        "the endpoint still serves %r for a row the matcher calls 'ambiguous'"
        % after["media_type"])
    assert after["category_conflict"] is True


def test_v7_end_to_end_conflict_does_not_erase_title_evidence(client):
    """A conflict suppresses the ROUTE, not the TITLE. A conflicted row whose
    cached title carries a season token must still be served as TV.

    Without this, a bridge that answered 'ambiguous' for every conflicted row
    would pass the test above and quietly unresolve half the corpus.
    """
    row = {"url": URL, "title": "Great Show S03 1080p", "year": 2019,
           "status": "missing", "category": "4k", "season": 3, "is_tv": True,
           "category_conflict": False, "category_attested": True,
           "media_type": "tv", "media_type_provisional": False}
    _seed(row)
    _mark_conflict()
    item = _cached_item(client)
    assert item["media_type"] == "tv", item


def test_v7_an_in_place_blob_write_invalidates_the_parse_cache(client):
    """DELIVERY, pinned on its own rather than only through the endpoint.

    ``get_background_cache_version`` is ``(COUNT(*), MAX(last_seen_at),
    _bg_cache_rev)``. An in-place blob UPDATE moves neither of the first two, so
    a writer that forgets the third leaves ``/results/cached`` memoising a parse
    of the PREVIOUS bytes -- for as long as no other write happens.

    SHOWN TO FAIL: delete either bump and this test fails on that operation,
    before the endpoint test does.
    """
    db = deps.registry.db
    row = {"url": URL, "title": TITLE, "year": 2019, "status": "missing",
           "category": "4k", "season": None, "is_tv": False,
           "media_type": "movie", "media_type_provisional": True}
    _seed(row)

    before = db.get_background_cache_version()
    assert db.mark_scan_category_conflict([URL]) == 1
    assert db.get_background_cache_version() != before, (
        "the conflict mark left the parse-cache token unchanged; the endpoint "
        "will serve the pre-mark parse")

    # The same gap, one method over. Seed a fresh unattested row: attestation is
    # written only where the key is absent AND no conflict is recorded.
    other = dict(row, url=URL)
    other.pop("category_attested", None)
    db.upsert_background_cache([{
        "url": URL + "attest/", "title": TITLE, "year": 2019,
        "status": "missing", "source_category": "HDEncode",
        "data": json.dumps(dict(other, url=URL + "attest/"))}])
    before = db.get_background_cache_version()
    assert db.attest_scan_categories([URL + "attest/"]) == 1
    assert db.get_background_cache_version() != before, (
        "attest_scan_categories left the parse-cache token unchanged")


def test_v7_normalization_is_applied_EXACTLY_ONCE_and_why_that_matters():
    """The normaliser is NOT idempotent, and cannot be made so in the old model.

    Writing the derived verdict into ``media_type`` makes a LEGACY row look
    CURRENT-FORMAT, and ``cached_type_evidence`` deliberately stops admitting
    ``is_tv`` as DETAIL evidence on a current-format row (R4-94-2: there it is a
    shadow of the verdict, not an observation). So a legacy row whose only
    evidence WAS ``is_tv``, on a conflicted route, reads 'tv' once and
    'ambiguous' twice. Measured: 1 of the 77 reachable rows.

    This is the design doc's own L2 observation -- "read_legacy(read_legacy(row))
    is not a thing that can be written, the output is a different type from the
    input" -- showing up in the bridge. It is SAFE because ``_load_cached_items``
    applies it exactly once, to a fresh ``json.loads`` product, and memoises the
    normalised items rather than re-normalising them on the next request.

    So the invariant that has to hold is "exactly one call site", and that is
    what this test pins. If a second call site is added, this fails and the
    reader has the reason in front of them.

    SHOWN TO FAIL: add ``_normalize_cached_row`` anywhere else in results.py --
    for instance defensively inside ``_shape_results`` -- and the count goes to
    2. Feed the normaliser its own output and the assertion below fails with
    'tv' vs 'ambiguous'.
    """
    import inspect

    row = {"title": "Some Film 2019 1080p", "category": "", "season": None,
           "is_tv": True, "category_conflict": True}
    once = results_route._normalize_cached_row(dict(row))
    twice = results_route._normalize_cached_row(dict(once))
    assert once["media_type"] == "tv"
    assert twice["media_type"] == "ambiguous", (
        "if this now agrees, the normaliser became idempotent and the "
        "one-call-site rule below can be relaxed -- update the docstring "
        "rather than deleting the test")

    source = inspect.getsource(results_route)
    calls = source.count("_normalize_cached_row(")
    # One `def`, one call in _load_cached_items. The two references in this
    # test module do not count -- inspect.getsource reads results.py only.
    assert calls == 2, (
        "expected exactly one call site (plus the definition); found %d "
        "occurrences of _normalize_cached_row( in results.py. A second call "
        "site re-normalises an already-normalised row, which is not a no-op "
        "-- see this test's docstring." % calls)

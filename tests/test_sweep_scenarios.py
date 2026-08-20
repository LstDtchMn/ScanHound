"""The eleven predeclared scenarios (design rev 2.1 §11.8).

These are end-to-end: real schema, real sessions, real verdicts, crossing
session.py -> completion.py -> health.py -> gate.py -> structure.py. The unit
tests prove each module honours its own rules; these prove the rules still hold
when the modules are wired together, which is where the original RSS analysis
went wrong.

Scenario list, verbatim from the design:
  one-cycle lag · 6-24 h yellow · >24 h RED recovered by sweep · burst spilling
  to page 2 · missed sweep · task delay · parser returns unexpected empty ·
  restart mid-sweep · canonical variants · normal vs catch-up acquisition ·
  stale readiness endpoint
"""

import datetime as dt
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.hdencode_shadow import compare_shadow
from backend.sweep.completion import PageOutcome, evaluate_completion, parse_posted
from backend.sweep.gate import (
    IdentityCoverage,
    RssAcquisition,
    classify_item,
    evaluate_promotion,
)
from backend.sweep.health import IntervalState
from backend.sweep.session import SweepSessionStore
from backend.sweep.structure import PageStructure, classify_page_structure

NOW = dt.datetime(2026, 8, 1, 12, 0, 0)
SOURCE = "4k_movies"


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "scenarios.db")
    DatabaseManager(path)
    conn = sqlite3.connect(path)
    yield SweepSessionStore(conn, owner="sweeper")
    conn.close()


def ago(hours):
    return NOW - dt.timedelta(hours=hours)


def page(idx, *, new, posts=30, age_hours=48, ok=True):
    return PageOutcome(
        page_index=idx, parsed_ok=ok, posts_found=posts, new_identities=new,
        oldest_posted=parse_posted(f"Posted {age_hours} hours ago", NOW) if ok else None,
    )


def posts(*urls, page_index=1):
    return [{"canonical_url": u, "raw_url": u + "/", "title": u, "page_index": page_index}
            for u in urls]


# ── 1 ───────────────────────────────────────────────────────────────────────

def test_scenario_01_one_cycle_lag(store):
    """A release surfaces one poll cycle after publication. The observation lag
    is real but small; nothing is missed and nothing is degraded.

    This is the shape 99 of the 100 'shadow misses' actually had."""
    verdict = classify_item(
        {"canonical_url": "https://hdencode.org/a/",
         "published_at": ago(3), "first_normal_at": ago(2)}, now=NOW)
    assert verdict.rss_state is RssAcquisition.GREEN
    assert verdict.coverage_state is IdentityCoverage.COVERED_BY_RSS
    assert not verdict.degrades_feed_health

    s = store.begin(SOURCE, now=NOW)
    store.commit_success(s, now=NOW)
    assert store.health(SOURCE, now=NOW).state is IntervalState.CURRENT


# ── 2 ───────────────────────────────────────────────────────────────────────

def test_scenario_02_six_to_twenty_four_hours_is_yellow(store):
    """Slow but acquired. Feed health degrades; coverage does not."""
    v = classify_item(
        {"canonical_url": "https://hdencode.org/b/",
         "published_at": ago(30), "first_normal_at": ago(20)}, now=NOW)
    assert v.rss_state is RssAcquisition.YELLOW
    assert v.is_covered and v.degrades_feed_health

    s = store.begin(SOURCE, now=NOW)
    store.commit_success(s, now=NOW)
    assert evaluate_promotion(
        source_health={SOURCE: store.health(SOURCE, now=NOW)},
        required_sources=[SOURCE], item_verdicts=[v],
        **_ALL_EVIDENCE).ready


# ── 3 ───────────────────────────────────────────────────────────────────────

def test_scenario_03_red_recovered_by_sweep(store):
    """§8's load-bearing rule, end to end: the feed missed it entirely, a
    COMPLETE sweep found it, and promotion is not blocked."""
    s = store.begin(SOURCE, now=NOW)
    store.record_observations(s, posts("https://hdencode.org/red/"))
    store.commit_success(s, now=NOW)

    v = classify_item(
        {"canonical_url": "https://hdencode.org/red/", "published_at": ago(60),
         "first_normal_at": None, "first_sweep_at": ago(1), "sweep_complete": True},
        now=NOW)
    assert v.rss_state is RssAcquisition.RED
    assert v.coverage_state is IdentityCoverage.RSS_RED_COVERED_BY_SWEEP

    result = evaluate_promotion(
        source_health={SOURCE: store.health(SOURCE, now=NOW)},
        required_sources=[SOURCE], item_verdicts=[v], **_ALL_EVIDENCE)
    assert result.ready, result.blocking
    assert any("feed-health metric" in s for s in result.satisfied)


# ── 4 ───────────────────────────────────────────────────────────────────────

def test_scenario_04_burst_spills_to_page_two(store):
    """A publication burst pushes unseen releases onto page 2. The sweep may not
    stop until it reaches a page with nothing new — a timestamp crossing alone
    would truncate the burst."""
    both_new = [page(1, new=25), page(2, new=12)]
    assert evaluate_completion(both_new, stop_target=ago(12),
                               all_persisted=True, page_cap=15).incomplete

    with_clean = both_new + [page(3, new=0)]
    assert evaluate_completion(with_clean, stop_target=ago(12),
                               all_persisted=True, page_cap=15).complete


# ── 5 ───────────────────────────────────────────────────────────────────────

def test_scenario_05_missed_sweep(store):
    """A sweep that never runs must age into `overdue` and block promotion, then
    recover cleanly once one completes."""
    s = store.begin(SOURCE, now=ago(20))
    store.commit_success(s, now=ago(20))

    stale = store.health(SOURCE, now=NOW)
    assert stale.state is IntervalState.OVERDUE
    assert evaluate_promotion(source_health={SOURCE: stale},
                              required_sources=[SOURCE], item_verdicts=[],
                              **_ALL_EVIDENCE).blocked

    s2 = store.begin(SOURCE, now=NOW)
    store.commit_success(s2, now=NOW)
    assert store.health(SOURCE, now=NOW).state is IntervalState.CURRENT


# ── 6 ───────────────────────────────────────────────────────────────────────

def test_scenario_06_task_delay_leaves_no_hole(store):
    """A late sweep must re-cover the interval it was late for. The overlap is
    measured from the PRIOR watermark, not from the late start — otherwise the
    delay itself becomes an uncovered gap."""
    s1 = store.begin(SOURCE, now=ago(20))
    store.commit_success(s1, now=ago(20))

    late = store.begin(SOURCE, now=NOW, overlap_hours=6.0)
    assert late.stop_target == ago(20) - dt.timedelta(hours=6)
    # The target reaches back BEHIND the previous watermark, so the 20-hour
    # delay is inside the swept interval rather than beside it.
    assert late.stop_target < ago(20)


# ── 7 ───────────────────────────────────────────────────────────────────────

def test_scenario_07_parser_returns_unexpected_empty(store):
    """The full-disc shape. A page parses cleanly and yields nothing where posts
    were expected: structural failure, sweep incomplete, watermark unmoved."""
    s1 = store.begin(SOURCE, now=ago(10))
    store.commit_success(s1, now=ago(10))

    structure = classify_page_structure(page_index=2, posts_found=0,
                                        selector_tier=None, body_bytes=40_000,
                                        expected_typical=30)
    assert structure.structure is PageStructure.STRUCTURE_LOST
    assert structure.is_failure

    verdict = evaluate_completion(
        [page(1, new=0), page(2, new=0, posts=0)],
        stop_target=ago(20), all_persisted=True, page_cap=15)
    assert verdict.incomplete
    assert any("structurally empty" in b for b in verdict.blocking)

    s2 = store.begin(SOURCE, now=NOW)
    store.mark_incomplete(s2, reason="structure lost", pages_crawled=2)
    assert store.coverage_through(SOURCE) == ago(10)      # NOT advanced


# ── 8 ───────────────────────────────────────────────────────────────────────

def test_scenario_08_restart_mid_sweep(store, tmp_path):
    """The process dies mid-crawl and a NEW worker picks it up. The resumed
    sweep must commit the ORIGINAL start, not the restart clock — otherwise it
    claims coverage of the interval it was dead for."""
    s1 = store.begin(SOURCE, now=NOW, lease_seconds=60)
    store.record_observations(s1, posts("https://hdencode.org/1/",
                                        "https://hdencode.org/2/"))
    # No mark_incomplete: a crash writes nothing. The lease simply expires.

    reborn = SweepSessionStore(store.conn, owner="sweeper-after-restart")
    s2 = reborn.begin(SOURCE, now=NOW + dt.timedelta(hours=2))
    assert s2.uuid == s1.uuid and s2.started_at == NOW

    # Replaying the pages it already had must not inflate the new-identity count.
    assert reborn.record_observations(s2, posts("https://hdencode.org/1/",
                                                "https://hdencode.org/2/")) == 0

    reborn.commit_success(s2, now=NOW + dt.timedelta(hours=3))
    assert reborn.coverage_through(SOURCE) == NOW


# ── 9 ───────────────────────────────────────────────────────────────────────

def test_scenario_09_canonical_variants(store, tmp_path):
    """The incident itself. The RSS canonicaliser keeps a trailing slash and the
    listing one strips it; the same release must still be ONE identity
    everywhere it is compared."""
    result = compare_shadow(
        rss_urls=["https://hdencode.org/same/"],
        listing_items=[{"url": "https://hdencode.org/same", "status": "in_library"}],
        rss_requests=2, listing_requests=8, normal_feeds_complete=True)
    assert result.duplicate_count == 1
    assert result.outcome == "success"

    # And the guard that would have caught it had the canonicalisers disagreed.
    broken = compare_shadow(
        rss_urls=["https://hdencode.org/same/"],
        listing_items=[{"url": "https://other.example/same", "status": "in_library"}],
        rss_requests=2, listing_requests=8, normal_feeds_complete=True)
    assert broken.outcome == "disjoint_identity_sets"
    assert not broken.is_conclusive


# ── 10 ──────────────────────────────────────────────────────────────────────

def test_scenario_10_normal_versus_catchup_acquisition():
    """Round 6 required latency to be measured from first_normal_at. An item
    reached only by catch-up has NOT been acquired by the normal feed, and
    scoring it green would hide a genuinely blind feed."""
    catchup_only = classify_item(
        {"canonical_url": "https://hdencode.org/c/", "published_at": ago(30),
         "first_normal_at": None, "first_sweep_at": ago(1), "sweep_complete": True},
        now=NOW)
    assert catchup_only.rss_state is not RssAcquisition.GREEN
    assert catchup_only.normal_feed_latency_hours is None

    normal = classify_item(
        {"canonical_url": "https://hdencode.org/d/", "published_at": ago(30),
         "first_normal_at": ago(29), "first_sweep_at": ago(1)}, now=NOW)
    assert normal.rss_state is RssAcquisition.GREEN
    assert normal.normal_feed_latency_hours == pytest.approx(1.0)


# ── 11 ──────────────────────────────────────────────────────────────────────

def test_scenario_11_stale_readiness_endpoint(store):
    """A readiness cross-check that cannot be demonstrated must block. This is
    the module-level form of the collector bug: the reconciliation had never
    once succeeded, and the gate passed anyway."""
    s = store.begin(SOURCE, now=NOW)
    store.commit_success(s, now=NOW)
    health = {SOURCE: store.health(SOURCE, now=NOW)}

    evidence = dict(_ALL_EVIDENCE)
    evidence["reconciliation_fail_closed"] = None       # never demonstrated
    stale = evaluate_promotion(source_health=health, required_sources=[SOURCE],
                               item_verdicts=[], **evidence)
    assert stale.blocked
    assert any("NOT DEMONSTRATED" in b and "fail-closed" in b for b in stale.blocking)

    assert evaluate_promotion(source_health=health, required_sources=[SOURCE],
                              item_verdicts=[], **_ALL_EVIDENCE).ready


_ALL_EVIDENCE = dict(
    all_discoveries_persisted=True,
    watermark_advanced_after_partial_persistence=False,
    restart_recovery_proven=True,
    missed_poll_recovery_proven=True,
    incomplete_sweep_recovery_proven=True,
    reconciliation_fail_closed=True,
    request_floor_met=True,
    listing_volume_evidence=True,
    auto_grab_enabled=False,
)

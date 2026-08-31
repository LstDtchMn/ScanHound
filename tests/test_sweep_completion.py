"""Sweep completion rules — the semantics three review rounds produced.

Every test here encodes a specific correction from those rounds. The value is
not coverage; it is that each rule has a case which FAILS if someone relaxes it.
"""

import datetime as dt

import pytest

from backend.sweep.completion import (
    PageOutcome,
    evaluate_completion,
    parse_posted,
)

NOW = dt.datetime(2026, 8, 1, 12, 0, 0)


def posted(text):
    return parse_posted(text, NOW)


# ─────────────────────────────── relative time ─────────────────────────────

class TestParsePosted:
    @pytest.mark.parametrize("text,seconds", [
        ("Posted 2 minutes ago", 120),
        ("Posted 3 hours ago", 10800),
        ("Posted 2 days ago", 172800),
        ("posted 1 week ago", 604800),
        ("Posted about 5 hours ago", 18000),
    ])
    def test_parses_observed_formats(self, text, seconds):
        """These are the shapes actually seen on hdencode.org listing blocks."""
        p = posted(text)
        assert p is not None and p.offset_seconds == seconds

    def test_absent_time_returns_none_not_a_default(self):
        """None must mean 'no time we understand'. Defaulting an unknown time to
        'old' would let a sweep stop early on an unparseable page."""
        assert posted("Time.and.Water.2026.2160p | 2026 | 9.9 GB") is None
        assert posted("") is None

    def test_retains_raw_string_and_granularity(self):
        """The derived datetime must not launder away its own imprecision."""
        p = posted("Posted 2 days ago")
        assert p.raw == "Posted 2 days ago"
        assert p.granularity_seconds == 86400

    def test_the_reading_spans_an_interval_not_a_point(self):
        """Listing times round DOWN, so '2 days ago' means an age in [2d, 3d):
        the true publication time lies in (earliest_possible, absolute]."""
        p = posted("Posted 2 days ago")
        assert p.absolute == NOW - dt.timedelta(days=2)
        assert p.earliest_possible == NOW - dt.timedelta(days=3)
        assert p.newest_possible == p.absolute


# ─────────────────────────── conjunctive completion ────────────────────────

def _page(idx=2, ok=True, posts=30, new=0, age_hours=48, err=None):
    return PageOutcome(
        page_index=idx, parsed_ok=ok, posts_found=posts, new_identities=new,
        oldest_posted=posted(f"Posted {age_hours} hours ago") if ok else None,
        error=err,
    )


TARGET = NOW - dt.timedelta(hours=12)


class TestCompletion:
    def test_all_conditions_met_completes(self):
        v = evaluate_completion([_page(idx=1, new=3), _page(idx=2, new=0)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.complete, v.blocking

    def test_timestamp_alone_is_not_enough(self):
        """THE CONJUNCTION. Old enough, but every page still had new identities —
        rev 1 would have stopped here."""
        v = evaluate_completion([_page(idx=1, new=5), _page(idx=2, new=2)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("source-new identities" in b for b in v.blocking)

    def test_clean_page_alone_is_not_enough(self):
        """Nothing new, but we never reached far enough back in time."""
        v = evaluate_completion([_page(idx=1, new=0, age_hours=1),
                                 _page(idx=2, new=0, age_hours=2)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("timestamp target NOT crossed" in b for b in v.blocking)

    def test_page_one_alone_MAY_complete_on_a_quiet_source(self):
        """Deliberately asserts the reviewer's rule over my instinct.

        I first wrote this test the other way round — requiring a minimum page
        count so page 1 could never prove completion — and the implementation
        failed it. The implementation was right: ChatGPT's round-6 ruling was
        explicit that "on a quiet source, page 1 may satisfy all three. A minimum
        page count would add cost, not evidence."

        The protection against a stalled crawler is the CONJUNCTION, not a page
        floor: a stalled crawler fails the timestamp condition, or the parser
        condition, or persistence. Page 1 completing here is correct because all
        three genuinely hold."""
        v = evaluate_completion([_page(idx=1, new=0, age_hours=48)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.complete, v.blocking

    def test_quiet_page_one_still_fails_if_any_condition_missing(self):
        """The companion to the above: page 1 gets no special dispensation."""
        v = evaluate_completion([_page(idx=1, new=0, age_hours=1)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("timestamp target NOT crossed" in b for b in v.blocking)

    def test_parser_failure_blocks_even_when_other_conditions_pass(self):
        """We cannot know what an unreadable page held, so we cannot claim to
        have covered its interval."""
        v = evaluate_completion([_page(idx=1, new=0), _page(idx=2, ok=False, err="500")],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("failed to parse" in b for b in v.blocking)

    def test_empty_page_beyond_first_is_structural_failure_not_success(self):
        """THE FULL-DISC SHAPE. A page that parses but yields nothing where posts
        were expected must never read as 'no unseen identities'."""
        v = evaluate_completion([_page(idx=1, new=0), _page(idx=2, posts=0)],
                                stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("structurally empty" in b for b in v.blocking)

    def test_unpersisted_discoveries_block_completion(self):
        """The watermark may not advance on work that was not durably written."""
        v = evaluate_completion([_page(idx=1, new=3), _page(idx=2, new=0)],
                                stop_target=TARGET, all_persisted=False, page_cap=15)
        assert v.incomplete
        assert any("durably persisted" in b for b in v.blocking)

    def test_no_parseable_time_anywhere_blocks(self):
        pages = [PageOutcome(1, True, 30, 0, None), PageOutcome(2, True, 30, 0, None)]
        v = evaluate_completion(pages, stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("parseable post time" in b for b in v.blocking)

    def test_the_whole_possible_interval_must_be_past_the_target(self):
        """REGRESSION (review blocker 2). A reading whose OLDEST edge crosses the
        target but whose NEWEST edge does not proves only that the post MIGHT be
        old enough — and a might is not a completion.

        'Posted 2 days ago' means the true time is in (NOW-3d, NOW-2d]. Against a
        2.5-day target, the oldest edge (NOW-3d) is past it but the newest edge
        (NOW-2d) is not, so the post could genuinely be NOW-2.1d — inside the
        interval we would be claiming to have swept. The old rule completed here
        and advanced coverage_through over ground it had never traversed."""
        page = PageOutcome(page_index=1, parsed_ok=True, posts_found=30,
                           new_identities=0, oldest_posted=posted("Posted 2 days ago"))
        target = NOW - dt.timedelta(days=2, hours=12)
        v = evaluate_completion([page], stop_target=target,
                                all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("could be as recent as" in b for b in v.blocking)

    def test_it_completes_once_the_newest_edge_also_clears_the_target(self):
        """The companion: push the target back past the newest edge and the same
        reading now genuinely proves the crossing."""
        page = PageOutcome(page_index=1, parsed_ok=True, posts_found=30,
                           new_identities=0, oldest_posted=posted("Posted 2 days ago"))
        v = evaluate_completion([page], stop_target=NOW - dt.timedelta(days=1, hours=12),
                                all_persisted=True, page_cap=15)
        assert v.complete, v.blocking

    def test_a_fine_grained_reading_needs_no_extra_margin(self):
        """Hour granularity: a 48-hour reading against a 47-hour target clears,
        because the newest possible time is 48 h and that is already past."""
        v = evaluate_completion([_page(idx=1, new=0, age_hours=48),
                                 _page(idx=2, new=0, age_hours=48)],
                                stop_target=NOW - dt.timedelta(hours=47),
                                all_persisted=True, page_cap=15)
        assert v.complete, v.blocking

    def test_no_pages_is_incomplete(self):
        v = evaluate_completion([], stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete

    def test_page_cap_reached_while_blocked_is_reported(self):
        pages = [_page(idx=i, new=1, age_hours=1) for i in range(1, 16)]
        v = evaluate_completion(pages, stop_target=TARGET, all_persisted=True, page_cap=15)
        assert v.incomplete
        assert any("page cap" in b for b in v.blocking)

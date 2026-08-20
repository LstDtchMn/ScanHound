"""Structural integrity of listing pages.

§1: an unexpectedly empty or structurally changed page is a PARSER FAILURE,
never "no unseen identities". These tests pin the distinction, and pin that the
module fails closed when it genuinely cannot tell the two apart.
"""

import pytest
from bs4 import BeautifulSoup

from backend.sweep.structure import (
    PageStructure,
    classify_page_structure,
    select_with_tier,
)

BODY = 40_000


def judge(**kw):
    base = dict(page_index=1, posts_found=25, selector_tier=0, body_bytes=BODY)
    base.update(kw)
    return classify_page_structure(**base)


class TestHealthyPages:
    def test_primary_selector_with_normal_volume_is_ok(self):
        v = judge(expected_typical=25)
        assert v.structure is PageStructure.OK
        assert not v.is_failure and not v.is_warning

    def test_a_source_with_no_history_still_passes_when_posts_exist(self):
        assert judge(expected_typical=None).structure is PageStructure.OK


class TestSelectorDrift:
    def test_fallback_tier_is_reported_as_degraded(self):
        """The early warning. A chain of `a or b or c` silently falls through, so
        a breakage that has already burned two of three fallbacks looks exactly
        like a healthy page — and the eventual total failure arrives with no
        warning history at all."""
        v = judge(selector_tier=2, expected_typical=25)
        assert v.structure is PageStructure.DEGRADED
        assert v.is_warning
        assert not v.is_failure      # it still parsed a full page

    def test_degraded_names_the_tier_so_drift_can_be_tracked(self):
        assert "tier 2" in judge(selector_tier=2, expected_typical=25).detail


class TestStructureLost:
    def test_zero_posts_beyond_page_one_is_always_structural(self):
        """Pagination led us here, so posts were expected. This is the full-disc
        shape: something ran cleanly and found nothing."""
        v = judge(page_index=3, posts_found=0, selector_tier=None,
                  expected_typical=None)
        assert v.structure is PageStructure.STRUCTURE_LOST
        assert v.is_failure

    def test_zero_posts_on_page_one_is_structural_when_history_disagrees(self):
        v = judge(posts_found=0, selector_tier=None, expected_typical=25)
        assert v.structure is PageStructure.STRUCTURE_LOST
        assert "stopped matching" in v.detail

    def test_zero_posts_with_NO_history_fails_closed(self):
        """THE FAIL-CLOSED RULE. We cannot distinguish an empty source from a
        broken selector, so we refuse to call it empty. A false alarm costs a
        re-fetch; the opposite error is invisible undercoverage."""
        v = judge(posts_found=0, selector_tier=None, expected_typical=None)
        assert v.structure is PageStructure.EMPTY_UNVERIFIABLE
        assert v.is_failure


class TestVolumeAnomalyIsDisabledUntilCalibrated:
    """REGRESSION (review ruling). The 0.5 fraction was CHOSEN, not measured.
    An invented constant must not be able to create a mandatory stop — or clear
    one — before listing-volume evidence exists (§7)."""

    def test_a_volume_collapse_does_NOT_fire_by_default(self):
        assert judge(posts_found=3, expected_typical=25).structure is PageStructure.OK

    def test_the_detector_still_works_when_explicitly_enabled(self):
        v = judge(posts_found=3, expected_typical=25, volume_anomaly_enabled=True)
        assert v.structure is PageStructure.VOLUME_ANOMALY
        assert v.is_failure

    def test_zero_post_detection_is_UNAFFECTED_by_the_switch(self):
        """The categorical signal — 'this source always has posts and now has
        none' — carries no invented threshold, so it stays on."""
        v = judge(posts_found=0, selector_tier=None, expected_typical=25)
        assert v.structure is PageStructure.STRUCTURE_LOST
        assert v.is_failure


class TestVolumeAnomalyWhenEnabled:
    def _judge(self, **kw):
        return judge(volume_anomaly_enabled=True, **kw)

    def test_a_collapse_in_volume_is_structural_not_a_quiet_day(self):
        v = self._judge(posts_found=3, expected_typical=25)
        assert v.structure is PageStructure.VOLUME_ANOMALY
        assert v.is_failure

    def test_a_normal_dip_is_not_flagged(self):
        assert self._judge(posts_found=20, expected_typical=25).structure is PageStructure.OK

    def test_the_floor_is_exactly_half(self):
        assert self._judge(posts_found=13, expected_typical=25).structure is PageStructure.OK
        assert self._judge(posts_found=12, expected_typical=25).structure is \
            PageStructure.VOLUME_ANOMALY

    def test_volume_anomaly_outranks_degraded(self):
        """A page that fell back to tier 2 AND collapsed in volume is a failure,
        not a warning — the worse fact governs."""
        v = self._judge(posts_found=2, selector_tier=2, expected_typical=25)
        assert v.structure is PageStructure.VOLUME_ANOMALY


class TestUnusableResponses:
    def test_a_tiny_body_is_not_a_listing_page(self):
        """An error page or truncated response must not be read as an empty
        category."""
        v = judge(posts_found=0, selector_tier=None, body_bytes=120)
        assert v.structure is PageStructure.UNUSABLE
        assert v.is_failure

    def test_body_size_is_checked_before_anything_else(self):
        """Even with posts somehow reported, a 100-byte body is not evidence."""
        assert judge(posts_found=25, body_bytes=100).structure is PageStructure.UNUSABLE


# ───────────────────────── the selector helper ──────────────────────────────

HTML = """
<html><body>
  <div class="data"><h5><a href="/a/">A</a></h5></div>
  <h2 class="entry-title"><a href="/b/">B</a></h2>
</body></html>
"""


class TestSelectWithTier:
    def test_primary_match_reports_tier_zero(self):
        soup = BeautifulSoup(HTML, "html.parser")
        found, tier = select_with_tier(soup, ["div.data h5 a", "h2.entry-title a"])
        assert len(found) == 1 and tier == 0

    def test_fallback_match_reports_its_tier(self):
        soup = BeautifulSoup(HTML, "html.parser")
        found, tier = select_with_tier(
            soup, [".nonexistent a", "div.data h5 a", "h2.entry-title a"])
        assert len(found) == 1 and tier == 1

    def test_no_match_reports_None_not_zero(self):
        """None and 0 must not be confused: 0 is the healthiest possible result
        and None is total failure."""
        soup = BeautifulSoup(HTML, "html.parser")
        found, tier = select_with_tier(soup, [".nope a", ".also-nope a"])
        assert found == [] and tier is None


class TestScannerWiring:
    def test_scanner_selectors_still_find_the_same_posts(self):
        """The refactor from an `or` chain to a data table must not change what
        gets selected — only make the tier visible."""
        from backend.scanner_service import ScannerService
        soup = BeautifulSoup(HTML, "html.parser")
        assert len(ScannerService._select_posts(soup, "hdencode")) == 1

    def test_scanner_reports_the_tier(self):
        from backend.scanner_service import ScannerService
        soup = BeautifulSoup(HTML, "html.parser")
        _, tier = ScannerService._select_posts_with_tier(soup, "hdencode")
        assert tier == 0

    def test_scanner_falls_back_and_says_so(self):
        from backend.scanner_service import ScannerService
        soup = BeautifulSoup(
            '<h2 class="entry-title"><a href="/b/">B</a></h2>', "html.parser")
        found, tier = ScannerService._select_posts_with_tier(soup, "hdencode")
        assert len(found) == 1 and tier == 2      # div.data h5 a, div.data a, then this

    def test_unknown_source_uses_the_hdencode_selectors(self):
        from backend.scanner_service import ScannerService
        soup = BeautifulSoup(HTML, "html.parser")
        assert len(ScannerService._select_posts(soup, "something-new")) == 1

    @pytest.mark.parametrize("source", ["ddlbase", "adithd", "hdencode"])
    def test_every_source_keeps_its_full_selector_ladder(self, source):
        from backend.scanner_service import ScannerService
        assert len(ScannerService.POST_SELECTORS[source]) >= 2

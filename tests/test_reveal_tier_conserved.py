"""`_find_reveal_control`'s tier must survive into the diagnostic.

THE COLLAPSE (peer review 2026-08-12). `_log_page_diagnostics` mapped three
materially different observations onto one code, whose comment asserted "A real
layout change":

    destination-rejected -> a links-labelled submit EXISTS but its destination no
        longer matches the expected unlock endpoint. POSITIVE evidence the reveal
        contract changed.
    ambiguous            -> several otherwise-valid reveal controls. Also positive
        unexpected-structure evidence.
    none                 -> nothing qualifying was proven. Equally consistent with
        a page-specific restriction, a login/region gate, a pulled release, an
        error page, an unrecognised block, or an alternate template.

Calling `none` a layout change picks one hypothesis without evidence. Because
these also feed source health, one unclassified page could read as a source-wide
regression — which is exactly the signal worth detecting, and it was being
impersonated. Live: `The Young Riders` failed `layout_changed` while the same
source completed 312 items, three of them that day.

Both codes still affect source health exactly as before; moving that decision to
AGGREGATION is a deliberate follow-up, not done here.

These tests drive the REAL `_log_page_diagnostics`, not a re-implementation of
its branch — an earlier draft asserted a copy of the condition, which would have
passed no matter what production did.
"""
from unittest.mock import MagicMock

import pytest

from backend.download_service import DownloadService
from backend.scrape_outcome import (
    _MESSAGES, _SIGNAL_BEARING_CODES, ScrapeCode, ScrapeDiagnostic,
)
from backend.source_health import SourceHealthState, health_state_for_diagnostic


@pytest.fixture
def svc():
    return DownloadService({}, None, server_mode=True)


def _driver(html="<html><body>Download</body></html>", title="A Release"):
    d = MagicMock()
    d.page_source = html
    d.title = title
    return d


def _diagnose(svc, tier):
    """Run the real classifier for an access-control failure at `tier`."""
    return svc._log_page_diagnostics(
        _driver(), None, stage="access_control", source_kind="hdencode",
        reveal_tier=tier,
    )


# ── the split itself, through production code ────────────────────────────────

def test_absent_control_is_no_longer_called_a_layout_change(svc):
    """THE bug: tier 'none' proves nothing about the layout."""
    diag = _diagnose(svc, "none")
    assert diag.code is ScrapeCode.REVEAL_CONTROL_ABSENT


@pytest.mark.parametrize("tier", ["ambiguous", "destination-rejected"])
def test_structural_tiers_keep_layout_changed(svc, tier):
    """Positive control: these ARE positive structural evidence, so the split
    must not over-fire and relabel real evidence as unknown."""
    diag = _diagnose(svc, tier)
    assert diag.code is ScrapeCode.LAYOUT_CHANGED


def test_the_tier_is_conserved_in_the_signals(svc):
    """Aggregation can only tell 'this page' from 'this site' if the tier
    survives into the persisted diagnostic."""
    for tier in ("none", "ambiguous", "destination-rejected"):
        diag = _diagnose(svc, tier)
        tiers = [s.split(":", 1)[1] for s in diag.signals
                 if str(s).startswith("reveal-tier:")]
        assert tiers == [tier], (tier, diag.signals)


def test_unknown_tier_still_classifies_and_says_so(svc):
    """A tier we did not anticipate must not silently become 'structural'."""
    diag = _diagnose(svc, None)
    assert diag.code is ScrapeCode.REVEAL_CONTROL_ABSENT
    assert any(str(s).startswith("reveal-tier:") for s in diag.signals)


# ── behaviour preservation ───────────────────────────────────────────────────

def test_both_codes_still_affect_source_health_identically():
    """Splitting the code must not silently change how source health is
    computed — that is a separate, deliberate decision."""
    for code in (ScrapeCode.LAYOUT_CHANGED, ScrapeCode.REVEAL_CONTROL_ABSENT):
        diag = ScrapeDiagnostic(code, retryable=False,
                                affects_source_health=True, signals=())
        assert health_state_for_diagnostic(diag) is SourceHealthState.DEGRADED, code


def test_access_control_failures_stay_non_retryable(svc):
    """The split is about naming the evidence, not about retry policy."""
    for tier in ("none", "ambiguous", "destination-rejected"):
        assert _diagnose(svc, tier).retryable is False


# ── the message says only what was observed ──────────────────────────────────

def test_the_absent_message_names_no_cause():
    msg = _MESSAGES[ScrapeCode.REVEAL_CONTROL_ABSENT].lower()
    assert "not found" in msg
    assert "the layout changed" not in msg          # no single-hypothesis claim
    assert any(w in msg for w in ("gate", "template", "error page"))


def test_absent_code_carries_its_signals():
    """The tier IS the evidence for this code, so it must be persisted."""
    assert ScrapeCode.REVEAL_CONTROL_ABSENT in _SIGNAL_BEARING_CODES
    diag = ScrapeDiagnostic(
        ScrapeCode.REVEAL_CONTROL_ABSENT, retryable=False,
        affects_source_health=True,
        signals=("access_control_present", "reveal-tier:none"),
    )
    assert "reveal-tier:none" in diag.persisted_message

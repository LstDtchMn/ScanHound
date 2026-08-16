"""Failure scope must be EARNED by evidence, not assumed.

Design review, the central finding: "an item-local failure must not become a
source-wide control action without positive evidence that the failure is
source-wide."

On 2026-08-13 ONE observed reveal stall parked 61 items for 48 hours. Verified
afterwards: of 62 queue rows carrying a source_temporarily_blocked reason,
exactly one had transport_attempted=1. The other 61 were siblings rewritten by
policy -- consequences presented as observations.

reveal_verification_stalled is the disputed seam. The producer observes only
that a reveal did not complete in time, which is equally consistent with a
pulled release, a changed template, or the source throttling everyone. It was
made source-wide because routing it to _fail turned 78 items into permanent
failures -- so item-local here must mean DEFERRED, never failed.

A RECOGNISED interactive challenge is deliberately exempt from this vote: it is
positive evidence about the source by construction, and no amount of counting
may weaken the verification hold.
"""
from unittest.mock import MagicMock

import pytest

from backend.download_queue import DownloadQueueService


@pytest.fixture
def svc():
    s = DownloadQueueService.__new__(DownloadQueueService)
    s.db = MagicMock()
    s.db.distinct_items_failing = MagicMock(return_value=0)
    return s


ITEM = {"item_uuid": "i1", "source": "hdencode", "title": "T"}


def _outcome(reason):
    return {"success": False, "affected_scope": "source", "reason_code": reason}


class TestAmbiguousOutcomesStayItemLocal:
    def test_ONE_reveal_stall_does_not_earn_source_scope(self, svc):
        """The 61:1 blast radius, at its origin."""
        svc.db.distinct_items_failing.return_value = 1
        assert svc._scope_is_earned(ITEM, _outcome("reveal_verification_stalled")) is False

    def test_SEVERAL_DISTINCT_items_do_earn_it(self, svc):
        """Control: the guard must not make source scope unreachable."""
        svc.db.distinct_items_failing.return_value = 2
        assert svc._scope_is_earned(ITEM, _outcome("reveal_verification_stalled")) is True

    def test_the_evidence_must_be_DISTINCT_items(self, svc):
        """Retrying one stubborn page must not manufacture its own evidence.

        Pinned by asserting the classifier is asked for DISTINCT items -- a
        count of attempts would let a single bad release convince the system an
        entire source is refusing.
        """
        svc.db.distinct_items_failing.return_value = 1
        svc._scope_is_earned(ITEM, _outcome("reveal_verification_stalled"))
        svc.db.distinct_items_failing.assert_called_once()
        assert svc.db.distinct_items_failing.call_args[0][0] == "hdencode"


class TestRecognisedChallengeIsExempt:
    def test_a_challenge_is_source_wide_with_NO_vote(self, svc):
        """The safety property. A recognised challenge is positive evidence by
        construction; counting must never be able to downgrade it."""
        svc.db.distinct_items_failing.return_value = 0
        assert svc._scope_is_earned(ITEM, _outcome("interactive_challenge")) is True
        svc.db.distinct_items_failing.assert_not_called()

    def test_an_explicitly_blocked_source_is_exempt_too(self, svc):
        svc.db.distinct_items_failing.return_value = 0
        assert svc._scope_is_earned(ITEM, _outcome("source_temporarily_blocked")) is True

    def test_a_disabled_source_is_exempt_too(self, svc):
        assert svc._scope_is_earned(ITEM, _outcome("source_disabled")) is True


class TestFailsSafe:
    def test_an_unreadable_classifier_keeps_the_OLD_behaviour(self, svc):
        """A telemetry gap must not silently disable a protection."""
        svc.db.distinct_items_failing.side_effect = RuntimeError("db down")
        assert svc._scope_is_earned(ITEM, _outcome("reveal_verification_stalled")) is True

    def test_no_db_keeps_the_old_behaviour(self, svc):
        svc.db = None
        assert svc._scope_is_earned(ITEM, _outcome("reveal_verification_stalled")) is True

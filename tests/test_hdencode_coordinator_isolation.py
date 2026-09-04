"""TST-2: the HDEncode traffic coordinator singleton leaked across tests.

backend/download_service.py:~2520 drives get_hdencode_coordinator().observe_challenge()
on real challenge signals, which sets _local_cooldown_until an hour ahead on the
module-level singleton backend/hdencode_coordinator.py:596 _COORDINATOR.
backend/download_queue.py:335 _assert_hdencode_available() (called by retry_item/
retry_ready) reads that same singleton's snapshot()["blocked"] and raises
DownloadQueueSourceHeld. Nothing reset the singleton between tests, so a
predecessor test that ever observed a challenge left every later test's queue
held for up to an hour: concretely,
tests/test_scrape_outcomes.py::test_challenge_iframe_signal_drops_path_query_and_fragment
running before tests/test_queue_review_followups.py made the latter fail --
purely because of file collection order, not because of anything either file
asserts.

The fix is tests/conftest.py::_fresh_hdencode_coordinator_per_test, an autouse
fixture that replaces backend.hdencode_coordinator._COORDINATOR with a brand
new HDEncodeTrafficCoordinator() for the duration of each test. That is the
guard this file exists to prove: remove the fixture from conftest.py and
test_b below fails, because it starts life poisoned by test_a's real
observe_challenge() call.

Test order inside this file is deliberate and load-bearing: test_a must run
(and hold the coordinator) before test_b asserts the hold is gone. Pytest
collects a module's tests in source order by default, but the isolation
property under test does not depend on that -- running this file in reverse
order (`pytest file::test_b file::test_a`) must also pass, because each test
gets a fresh coordinator regardless of what ran immediately before it.

test_b passes trivially when run alone -- with `-k`, `--lf`, or as a single
node -- because a coordinator nobody has poisoned yet is indistinguishable
from a coordinator the fixture freshly reset. Its proof is the pair (test_a
then test_b run together in the same process) plus the fixture-removed
mutant (delete _fresh_hdencode_coordinator_per_test from conftest.py and
test_b fails). Running the file in reverse order must still pass, but reverse
order is not itself part of test_b's proof.
"""
from unittest.mock import MagicMock

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService
from backend.hdencode_coordinator import get_hdencode_coordinator
import backend.hdencode_coordinator as hdencode_coordinator_module

_SEEN_COORDINATORS = []


def test_a_real_challenge_observation_holds_the_source():
    """The predecessor's effect, reproduced deliberately: a real
    observe_challenge() call (no mocks) puts the process-wide coordinator
    into a blocked state, exactly as a live challenge response does."""
    _SEEN_COORDINATORS.append(get_hdencode_coordinator())

    get_hdencode_coordinator().observe_challenge()

    assert get_hdencode_coordinator().snapshot()["blocked"] is True


def test_b_the_next_test_starts_with_an_unheld_coordinator():
    """Defined AFTER test_a: without the autouse fixture in conftest.py, this
    test inherits test_a's hour-long cooldown and _assert_hdencode_available()
    raises DownloadQueueSourceHeld here. With the fixture, this test gets its
    own fresh coordinator and the queue is available."""
    _SEEN_COORDINATORS.append(get_hdencode_coordinator())

    assert get_hdencode_coordinator().snapshot()["blocked"] is False

    dm = DatabaseManager()
    try:
        fake_download_service = MagicMock()
        svc = DownloadQueueService(
            {}, dm, fake_download_service, broadcast=lambda *a: None
        )
        svc._assert_hdencode_available()
    finally:
        dm.close()


def test_c_the_coordinator_is_a_fresh_object_per_test():
    """Plan A: the fixture swaps in a new HDEncodeTrafficCoordinator() every
    test, so the object identity seen across tests always differs. Compare
    the held objects themselves, not id() -- CPython can reuse a freed
    object's address for a later instance, which would make an id()-based
    comparison fail on a correctly working fixture (and pass near-vacuously
    otherwise)."""
    _SEEN_COORDINATORS.append(get_hdencode_coordinator())

    if len(_SEEN_COORDINATORS) < 2:
        pytest.skip("needs test_a and test_b in the same process")

    for i, a in enumerate(_SEEN_COORDINATORS):
        for b in _SEEN_COORDINATORS[i + 1:]:
            assert a is not b

    assert get_hdencode_coordinator() is hdencode_coordinator_module._COORDINATOR

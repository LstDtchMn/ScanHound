"""The qualification collector's notification state machine.

Two review rounds of PR #70 each found a suppression wedge:

  round 1: clear DELETED the marker; delivered CLEAR + failed unlink left the
           old STOP signature authoritative -> reappearing stop suppressed.
  round 2: clear was persisted in ONE post-send write; delivered CLEAR +
           failed write_marker left STOP:A authoritative -- the same wedge
           with replace() substituted for unlink().

The protocol is now: durably write PENDING(target) BEFORE sending, promote to
the target only on confirmed delivery, and NEVER suppress on a pending marker.
A failure anywhere can produce a duplicate alert, never a silent one. These
tests live in the repo suite so the machine has an automated owner -- this
bundle's own selftest.py rotted for three schema bumps because nothing ran it.
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.abspath(os.path.join(
    HERE, "..", "docs", "feature-pack-review", "qualification", "collector",
    "collect_shadow_evidence.py"))


def _load():
    spec = importlib.util.spec_from_file_location("qual_collector", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # tests must not append to the real shadow-window.log
    mod.log_line = lambda text: None
    return mod


SIG_A = "RSS MISS NEVER RESOLVED xN (real coverage loss)"
SIG_B = SIG_A + "\nunexpected_schema_version=8"


class TestRequiredRegression:
    def test_confirmed_clear_with_failed_promotion_cannot_suppress(self, tmp_path):
        """THE round-2 required regression, verbatim:

            1. marker = STOP:A
            2. CLEAR notification reports success
            3. final state persistence FAILS
            4. A reappears
            5. assert STOP A is proposed/sent again

        The round-1 implementation (promote in one post-send write) fails this
        at step 5: the marker still says delivered STOP:A, so the reappearing A
        is suppressed while the operator's phone says CLEARED.
        """
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})

        real_write = m.write_marker
        def failing_promotion(path, payload):
            if payload.get("state") == "pending":
                return real_write(path, payload)
            raise OSError("disk full at promotion")   # step 3
        m.write_marker = failing_promotion

        sent = m.deliver_transition(marker, None, lambda _n: True)   # step 2
        assert sent == "clear", "the clear must have been sent"
        m.write_marker = real_write

        # step 4/5: A reappears -- the pending marker must NOT suppress it
        sends = []
        m.deliver_transition(marker, SIG_A, lambda n: sends.append(n) or True)
        assert sends == ["stop"], (
            "a stop reappearing after a delivered-but-unpromoted clear was "
            "suppressed -- the round-2 wedge is back")

    def test_pending_write_failure_sends_nothing_and_claims_nothing(self, tmp_path):
        """The other half of the ordering: if the PENDING write itself fails,
        no send happens, the delivered state is untouched, and suppressing an
        unchanged stop remains correct -- the operator was never told anything
        new."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})

        real_write = m.write_marker
        m.write_marker = lambda p, d: (_ for _ in ()).throw(OSError("no space"))
        sends = []
        out = m.deliver_transition(marker, None, lambda n: sends.append(n) or True)
        m.write_marker = real_write

        assert out == "blocked" and sends == [], "nothing may be sent without a durable pending"
        assert m.read_marker(marker) == {"state": "stop", "signature": SIG_A}
        # and the unchanged stop stays suppressed, which is correct here
        note, _ = m.marker_transition(m.read_marker(marker), SIG_A)
        assert note is None


class TestStateMachine:
    def test_stop_clear_stop_realerts_on_the_happy_path(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        assert m.deliver_transition(marker, SIG_A, lambda _n: True) == "stop"
        assert m.deliver_transition(marker, None, lambda _n: True) == "clear"
        assert marker.is_file(), "clear persists a state, never deletes one"
        sends = []
        m.deliver_transition(marker, SIG_A, lambda n: sends.append(n) or True)
        assert sends == ["stop"]

    def test_unchanged_stop_is_suppressed(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.deliver_transition(marker, SIG_A, lambda _n: True)
        assert m.deliver_transition(marker, SIG_A, lambda _n: True) == "suppressed"

    def test_a_changed_stop_alerts(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.deliver_transition(marker, SIG_A, lambda _n: True)
        assert m.deliver_transition(marker, SIG_B, lambda _n: True) == "stop"

    def test_failed_send_leaves_pending_and_retries(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        assert m.deliver_transition(marker, SIG_A, lambda _n: False) == "stop"
        assert m.read_marker(marker)["state"] == "pending"
        sends = []
        m.deliver_transition(marker, SIG_A, lambda n: sends.append(n) or True)
        assert sends == ["stop"], "an unconfirmed send must retry, not suppress"

    def test_no_stop_and_no_history_stays_silent(self, tmp_path):
        m = _load()
        assert m.deliver_transition(tmp_path / "absent", None, lambda _n: True) == "suppressed"

    def test_clear_after_clear_stays_silent(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "clear"})
        assert m.deliver_transition(marker, None, lambda _n: True) == "suppressed"

    def test_pending_never_suppresses(self, tmp_path):
        """The rule stated in one line: pending means unconfirmed, and the only
        safe reading of unconfirmed is to notify again."""
        m = _load()
        for target in ({"state": "stop", "signature": SIG_A}, {"state": "clear"}):
            note, _ = m.marker_transition({"state": "pending", "target": target}, SIG_A)
            assert note == "stop", f"pending({target}) suppressed an active stop"


class TestMarkerParsing:
    def test_legacy_plaintext_reads_as_a_delivered_stop(self, tmp_path):
        """The pre-JSON format stored the raw signature; the live server file
        was seeded that way. It must parse as a delivered STOP so migration
        does not re-alert an unchanged set."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        marker.write_text(SIG_A, encoding="utf-8")
        assert m.read_marker(marker) == {"state": "stop", "signature": SIG_A}
        assert m.deliver_transition(marker, SIG_A, lambda _n: True) == "suppressed"

    def test_corrupt_json_fails_conservative(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        marker.write_text('{"state": "st', encoding="utf-8")
        assert m.deliver_transition(marker, SIG_A, lambda _n: True) == "stop"

    def test_write_is_atomic_via_replace(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})
        assert json.loads(marker.read_text(encoding="utf-8"))["signature"] == SIG_A
        assert not (tmp_path / "stop-condition.last.tmp").exists()

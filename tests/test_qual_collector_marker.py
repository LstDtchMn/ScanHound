"""The qualification collector's notification state machine.

Peer review of PR #70, MEDIUM: the first dedup design deleted the marker file
on a delivered CLEAR. A delivered CLEAR followed by a FAILED unlink left the
old STOP signature on disk, so the same stop reappearing matched it and was
suppressed indefinitely -- with the operator last told "cleared". CLEAR is now
an explicit persisted state and nothing is ever deleted, so that wedge is
impossible by construction. These tests pin the required sequence and live in
the repo suite so the state machine has an automated owner -- this bundle's own
selftest.py sat broken for three schema bumps precisely because nothing ran it.
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
    return mod


SIG_A = "RSS MISS NEVER RESOLVED xN (real coverage loss)"
SIG_B = SIG_A + "\nunexpected_schema_version=8"


class TestRequiredSequence:
    def test_stop_clear_then_same_stop_realerts(self, tmp_path):
        """THE reviewer-specified regression, end to end through the marker
        file: deliver STOP A, deliver CLEAR, reintroduce the SAME stop A --
        it must alert again. Under the deleted-file design this exact sequence
        could suppress forever if the unlink failed; here there is no unlink
        to fail."""
        m = _load()
        marker = tmp_path / "stop-condition.last"

        # STOP A delivered
        state, sig = m.read_marker(marker)
        note, new = m.marker_transition(state, sig, SIG_A)
        assert note == "stop"
        m.write_marker(marker, new)

        # CLEAR delivered
        state, sig = m.read_marker(marker)
        note, new = m.marker_transition(state, sig, None)
        assert note == "clear"
        m.write_marker(marker, new)
        assert marker.is_file(), "clear must persist a state, not delete one"

        # The SAME stop reappears -> must alert
        state, sig = m.read_marker(marker)
        note, _ = m.marker_transition(state, sig, SIG_A)
        assert note == "stop", "a stop reappearing after a delivered clear was suppressed"

    def test_failed_sends_never_advance_the_marker(self, tmp_path):
        """The caller persists only on confirmed delivery; the transition is
        pure. So an undelivered notification leaves the marker alone, and the
        next run proposes the identical notification again."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        state, sig = m.read_marker(marker)
        note1, _ = m.marker_transition(state, sig, SIG_A)
        # send fails -> nothing written
        state, sig = m.read_marker(marker)
        note2, _ = m.marker_transition(state, sig, SIG_A)
        assert note1 == note2 == "stop"


class TestDedupBehaviour:
    def test_unchanged_stop_is_suppressed(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})
        state, sig = m.read_marker(marker)
        note, _ = m.marker_transition(state, sig, SIG_A)
        assert note is None

    def test_a_changed_stop_alerts(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})
        state, sig = m.read_marker(marker)
        note, new = m.marker_transition(state, sig, SIG_B)
        assert note == "stop" and new["signature"] == SIG_B

    def test_no_stop_and_no_history_stays_silent(self, tmp_path):
        m = _load()
        state, sig = m.read_marker(tmp_path / "absent")
        note, _ = m.marker_transition(state, sig, None)
        assert note is None, "a clear with nothing to clear must not alert"

    def test_clear_after_clear_stays_silent(self, tmp_path):
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "clear"})
        state, sig = m.read_marker(marker)
        note, _ = m.marker_transition(state, sig, None)
        assert note is None


class TestMarkerParsing:
    def test_legacy_plaintext_reads_as_a_delivered_stop(self, tmp_path):
        """The pre-JSON format stored the raw signature; the live server file
        was seeded that way. It must parse as a delivered STOP so the migration
        does not re-alert an unchanged set."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        marker.write_text(SIG_A, encoding="utf-8")
        state, sig = m.read_marker(marker)
        assert (state, sig) == ("stop", SIG_A)
        note, _ = m.marker_transition(state, sig, SIG_A)
        assert note is None

    def test_corrupt_json_fails_conservative(self, tmp_path):
        """Garbage parses as a stop with a garbage signature: never equal to a
        real one, so the next stop RE-ALERTS rather than being suppressed."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        marker.write_text('{"state": "st', encoding="utf-8")
        state, sig = m.read_marker(marker)
        note, _ = m.marker_transition(state, sig, SIG_A)
        assert note == "stop"

    def test_write_is_atomic_via_replace(self, tmp_path):
        """write_marker goes through a temp file + replace, so a reader never
        sees a half-written marker under the real name."""
        m = _load()
        marker = tmp_path / "stop-condition.last"
        m.write_marker(marker, {"state": "stop", "signature": SIG_A})
        assert json.loads(marker.read_text(encoding="utf-8"))["signature"] == SIG_A
        assert not (tmp_path / "stop-condition.last.tmp").exists()

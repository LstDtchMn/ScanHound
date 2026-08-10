"""Tests for Dolby Vision FEL/MEL detection (dv_detect).

dovi_tool is never actually invoked — its presence (shutil.which) and the two
subprocess stages (extract-rpu, info) are mocked, so these run fully offline and
exercise the parsing + fail-safe behavior of the verified recipe.

The stages are mocked at ``dv_detect.run_cancellable`` rather than at
``subprocess.run``. That is the seam dv_detect actually depends on: the full
extract now passes a ``stall_timeout``, which makes run_cancellable poll a Popen
instead of calling subprocess.run, so a subprocess.run patch would silently stop
intercepting anything and every one of these tests would pass a real
``/usr/local/bin/dovi_tool`` that does not exist.

Full-pass tests pin ``bounded_first=False``. Without it the FEL-positive
accelerator would answer first and these tests would quietly stop covering the
full-pass branch they were written for.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.rename import dv_detect
from backend.rename.process_control import ProcessStalled


def _proc(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── pure summary parsing ──────────────────────────────────────────────

class TestParseInfo:
    def test_profile7_fel(self):
        assert dv_detect._parse_info("Profile: 7 (FEL)") == dv_detect.LAYER_FEL

    def test_profile7_mel(self):
        assert dv_detect._parse_info("Profile: 7 (MEL)") == dv_detect.LAYER_MEL

    def test_mixed_mel_fel_counts_as_fel(self):
        # Any FEL frame makes the grab a FEL.
        assert dv_detect._parse_info("Profile: 7 (MEL, FEL)") == dv_detect.LAYER_FEL

    def test_profile8_single_layer(self):
        assert dv_detect._parse_info("Profile: 8.1") == dv_detect.LAYER_P8

    def test_profile5_single_layer(self):
        assert dv_detect._parse_info("Profile: 5") == dv_detect.LAYER_P5

    def test_zero_padded_profile_classifies(self):
        # Defensive: "07" / "08.1" must not fall through to NONE.
        assert dv_detect._classify("07", "") == dv_detect.LAYER_MEL
        assert dv_detect._classify("08.1", "") == dv_detect.LAYER_P8
        assert dv_detect._classify("05", "") == dv_detect.LAYER_P5

    def test_no_profile_line_is_none(self):
        assert dv_detect._parse_info("garbage output") == dv_detect.LAYER_NONE


# ── detect_layer integration (mocked subprocess) ──────────────────────

class TestDetectLayer:
    def test_unavailable_tool_returns_unknown(self, tmp_path):
        f = tmp_path / "x.mkv"; f.write_bytes(b"x")
        with patch("shutil.which", return_value=None):
            r = dv_detect.detect_layer(str(f))
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert r["tool"] is False

    def test_missing_file_returns_unknown(self):
        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"):
            r = dv_detect.detect_layer("/nope/missing.mkv")
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert r["error"] == "file not found"

    def test_unsupported_container_skipped(self, tmp_path):
        f = tmp_path / "x.avi"; f.write_bytes(b"x")
        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"):
            r = dv_detect.detect_layer(str(f))
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert "unsupported" in r["error"]

    def _run_with_stages(self, tmp_path, extract_proc, info_stdout=b"",
                         rpu_size=10):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        calls = {"n": 0}

        def fake_run(args, **kw):
            calls["n"] += 1
            if "extract-rpu" in args:
                # Simulate dovi_tool writing the RPU file (size controls the
                # "no RPU" branch).
                out_idx = args.index("-o") + 1
                with open(args[out_idx], "wb") as fh:
                    fh.write(b"\0" * rpu_size)
                return extract_proc
            return _proc(stdout=info_stdout)

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            return dv_detect.detect_layer(str(f), bounded_first=False)

    def test_fel_detected(self, tmp_path):
        r = self._run_with_stages(
            tmp_path, _proc(returncode=0), info_stdout=b"Profile: 7 (FEL)\n")
        assert r["layer"] == dv_detect.LAYER_FEL and r["tool"] is True

    def test_mel_detected(self, tmp_path):
        r = self._run_with_stages(
            tmp_path, _proc(returncode=0), info_stdout=b"Profile: 7 (MEL)\n")
        assert r["layer"] == dv_detect.LAYER_MEL

    def test_no_rpu_means_no_dolby_vision(self, tmp_path):
        # extract-rpu produces an empty file → no DV.
        r = self._run_with_stages(
            tmp_path, _proc(returncode=2, stderr=b"No RPU found"), rpu_size=0)
        assert r["layer"] == dv_detect.LAYER_NONE
        assert r["error"] is None

    def test_extract_hard_error_is_unknown(self, tmp_path):
        r = self._run_with_stages(
            tmp_path, _proc(returncode=1, stderr=b"corrupt stream"), rpu_size=5)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert "corrupt" in r["error"]

    def test_hard_error_with_NO_rpu_written_is_unknown_not_none(self, tmp_path):
        """The realistic failure shape, and the one the suite used to miss.

        dovi_tool writes the output file only on success, and mkstemp has
        already pre-created it at zero bytes — so a genuine failure leaves
        returncode != 0 AND rpu_size == 0 together. The old ordering tested
        the empty file first and reported an authoritative 'no Dolby Vision'
        with error=None. The existing hard-error test above passed only
        because it used rpu_size=5, a state dovi_tool never actually leaves
        behind on failure.
        """
        r = self._run_with_stages(
            tmp_path, _proc(returncode=1, stderr=b"Failed to read file"),
            rpu_size=0)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert r["error"] and "Failed to read" in r["error"]

    def test_silent_hard_error_with_no_rpu_is_still_unknown(self, tmp_path):
        # Nonzero exit, no stderr at all, nothing written: still a failure,
        # so still 'unknown' -- and the error string must not be empty.
        r = self._run_with_stages(
            tmp_path, _proc(returncode=1, stderr=b""), rpu_size=0)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert r["error"]

    def test_clean_exit_with_empty_rpu_is_no_dolby_vision(self, tmp_path):
        # The tool ran fine and produced nothing: a real, positive "no DV".
        r = self._run_with_stages(
            tmp_path, _proc(returncode=0, stderr=b""), rpu_size=0)
        assert r["layer"] == dv_detect.LAYER_NONE
        assert r["error"] is None

    def test_info_failure_is_unknown_not_none(self, tmp_path):
        # extract-rpu succeeds (valid RPU) but `info` fails → must be 'unknown',
        # NOT 'none' (which would falsely claim the file has no Dolby Vision).
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")

        def fake_run(args, **kw):
            if "extract-rpu" in args:
                out_idx = args.index("-o") + 1
                with open(args[out_idx], "wb") as fh:
                    fh.write(b"\0" * 10)
                return _proc(returncode=0)
            return _proc(returncode=1, stderr=b"malformed RPU")  # info fails

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            r = dv_detect.detect_layer(str(f), bounded_first=False)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert "info failed" in r["error"]

    def test_timeout_is_fail_safe(self, tmp_path):
        import subprocess
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable",
                   side_effect=subprocess.TimeoutExpired("dovi_tool", 1)):
            r = dv_detect.detect_layer(str(f), bounded_first=False)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN and r["error"] == "timeout"

    def test_stall_is_fail_safe_and_distinguishable_from_timeout(self, tmp_path):
        """A wedged extract must be 'unknown', and must SAY it stalled.

        Both resolve to a non-authoritative layer, but the scanner backs a file
        off on the strength of the reason, so collapsing the two would make a
        permanently-wedged file indistinguishable from a merely slow one.
        """
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable",
                   side_effect=ProcessStalled("no read progress for 180s")):
            r = dv_detect.detect_layer(str(f), bounded_first=False)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert r["error"] == "stalled"


class TestBoundedFelAccelerator:
    """The bounded probe is only ever allowed to say FEL.

    A sample proving FEL is final; a sample showing anything else proves
    nothing, because a later frame may still be FEL. These tests pick inputs
    where the safe rule and the unsafe one DISAGREE, so a regression that
    finalised a bounded non-FEL fails here rather than passing quietly.
    """

    def _stage(self, tmp_path, bounded_info, full_info, bounded_rc=0,
               bounded_rpu=10):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        # Which stage `info` belongs to is decided by the extract that preceded
        # it, NOT by call order: a bounded probe that writes an empty RPU
        # returns without ever calling info, so counting calls would attribute
        # the full pass's info to the bounded probe.
        seen = {"full": 0, "last_bounded": None}

        def fake_run(args, **kw):
            if "extract-rpu" in args:
                bounded = "-l" in args
                seen["last_bounded"] = bounded
                if not bounded:
                    seen["full"] += 1
                with open(args[args.index("-o") + 1], "wb") as fh:
                    fh.write(b"\0" * (bounded_rpu if bounded else 10))
                return _proc(returncode=bounded_rc if bounded else 0)
            return _proc(stdout=bounded_info if seen["last_bounded"] else full_info)

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            return dv_detect.detect_layer(str(f)), seen

    def test_bounded_fel_short_circuits_and_is_marked_as_such(self, tmp_path):
        r, seen = self._stage(tmp_path, b"Profile: 7 (FEL)\n", b"Profile: 7 (FEL)\n")
        assert r["layer"] == dv_detect.LAYER_FEL
        assert r["evidence"] == "bounded"
        assert seen["full"] == 0, "a proven FEL must not pay for a full pass"

    def test_bounded_mel_does_NOT_finalise_and_full_pass_wins(self, tmp_path):
        # THE load-bearing case: the head of the file is MEL, but the file is
        # really a mixed title whose FEL frames come later. Finalising the
        # bounded sample would report MEL for a FEL grab.
        r, seen = self._stage(tmp_path, b"Profile: 7 (MEL)\n",
                              b"Profile: 7 (MEL, FEL)\n")
        assert r["layer"] == dv_detect.LAYER_FEL
        assert r["evidence"] == "full"
        assert seen["full"] == 1

    def test_bounded_no_rpu_does_NOT_finalise_absence(self, tmp_path):
        # An empty bounded sample must never become an authoritative 'none' —
        # that is the value dv_labeler acts on to REMOVE a label.
        r, seen = self._stage(tmp_path, b"", b"Profile: 7 (FEL)\n",
                              bounded_rpu=0)
        assert r["layer"] == dv_detect.LAYER_FEL
        assert seen["full"] == 1

    def test_bounded_failure_falls_through_to_full_pass(self, tmp_path):
        r, seen = self._stage(tmp_path, b"", b"Profile: 8\n",
                              bounded_rc=1, bounded_rpu=0)
        assert r["layer"] == dv_detect.LAYER_P8
        assert r["evidence"] == "full"

    def test_probe_returns_bool_never_a_layer(self, tmp_path):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")

        def fake_run(args, **kw):
            if "extract-rpu" in args:
                with open(args[args.index("-o") + 1], "wb") as fh:
                    fh.write(b"\0" * 10)
                return _proc(returncode=0)
            return _proc(stdout=b"Profile: 5\n")

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            assert dv_detect.probe_fel_bounded(str(f)) is False

    def test_probe_passes_a_frame_limit(self, tmp_path):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        captured = []

        def fake_run(args, **kw):
            captured.append(list(args))
            if "extract-rpu" in args:
                with open(args[args.index("-o") + 1], "wb") as fh:
                    fh.write(b"\0" * 10)
                return _proc(returncode=0)
            return _proc(stdout=b"Profile: 7 (FEL)\n")

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            assert dv_detect.probe_fel_bounded(str(f), limit=250) is True
        extract = next(c for c in captured if "extract-rpu" in c)
        assert "-l" in extract and extract[extract.index("-l") + 1] == "250"


class TestMultiProfileSummary:
    """`Profiles: 7, 8` is emitted when the RPU set spans several profiles.

    The singular-only pattern did not merely fail to parse it — it produced
    LAYER_NONE with error=None, an AUTHORITATIVE "no Dolby Vision" that
    dv_labeler acts on by REMOVING the managed label. So the bug's real shape
    was a mixed-profile title silently losing its DV badge.
    """

    def test_plural_profiles_line_parses(self):
        assert dv_detect._parse_info("Profiles: 7, 8") == dv_detect.LAYER_MEL

    def test_plural_with_fel_token_is_fel(self):
        assert dv_detect._parse_info("Profiles: 7, 8 (MEL, FEL)") == dv_detect.LAYER_FEL

    def test_plural_profiles_is_never_a_silent_none(self):
        # The regression this guards: any recognised profile list must not
        # collapse to the value that authorises label removal.
        for summary in ("Profiles: 7, 8", "Profiles: 5, 8", "Profiles: 8"):
            assert dv_detect._parse_info(summary) != dv_detect.LAYER_NONE, summary

    def test_singular_still_parses(self):
        assert dv_detect._parse_info("Profile: 7 (FEL)") == dv_detect.LAYER_FEL


class TestDependencyStatus:
    def test_reports_dovi_tool_key(self):
        s = dv_detect.dependency_status()
        assert set(s) == {"dovi_tool"} and isinstance(s["dovi_tool"], bool)


class TestOnlyRpuSpecificMessagesMeanNoDolbyVision:
    """Peer review caught this: a bare "not found" test also matched failures
    like "input file not found" and "video track not found" -- and a file CAN
    vanish between the isfile() check and the subprocess, which is exactly the
    mount failure this module exists to classify honestly. Those must stay
    'unknown'; only dovi_tool positively reporting an absent RPU means 'none'.
    """

    def _run(self, tmp_path, stderr, rc=1, rpu_size=0):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")

        def fake_run(args, **kw):
            if "extract-rpu" in args:
                out_idx = args.index("-o") + 1
                with open(args[out_idx], "wb") as fh:
                    fh.write(b"\0" * rpu_size)
                return _proc(returncode=rc, stderr=stderr)
            return _proc(stdout=b"")

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("backend.rename.dv_detect.run_cancellable", side_effect=fake_run):
            return dv_detect.detect_layer(str(f), bounded_first=False)

    @pytest.mark.parametrize("stderr", [
        b"No RPU found",
        b"RPU not found in stream",
        b"No Dolby Vision RPU present",
    ])
    def test_rpu_specific_absence_is_no_dolby_vision(self, tmp_path, stderr):
        r = self._run(tmp_path, stderr)
        assert r["layer"] == dv_detect.LAYER_NONE
        assert r["error"] is None

    @pytest.mark.parametrize("stderr", [
        b"input file not found",
        b"video track not found",
        b"NAL unit not found",
        b"Error: configuration record not found",
    ])
    def test_generic_not_found_failures_stay_unknown(self, tmp_path, stderr):
        r = self._run(tmp_path, stderr)
        assert r["layer"] == dv_detect.LAYER_UNKNOWN, stderr
        assert r["error"]

    def test_clean_exit_with_no_rpu_is_still_no_dolby_vision(self, tmp_path):
        # rc == 0 needs no message: the tool ran and produced nothing.
        r = self._run(tmp_path, b"", rc=0)
        assert r["layer"] == dv_detect.LAYER_NONE
        assert r["error"] is None

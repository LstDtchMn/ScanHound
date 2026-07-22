"""Tests for Dolby Vision FEL/MEL detection (dv_detect).

dovi_tool is never actually invoked — its presence (shutil.which) and the two
subprocess stages (extract-rpu, info) are mocked, so these run fully offline and
exercise the parsing + fail-safe behavior of the verified recipe.
"""
from types import SimpleNamespace
from unittest.mock import patch
import subprocess

import pytest

from backend.rename import dv_detect


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
             patch("subprocess.run", side_effect=fake_run):
            return dv_detect.detect_layer(str(f))

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
             patch("subprocess.run", side_effect=fake_run):
            r = dv_detect.detect_layer(str(f))
        assert r["layer"] == dv_detect.LAYER_UNKNOWN
        assert "info failed" in r["error"]

    def test_timeout_is_fail_safe(self, tmp_path):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dovi_tool", 1)):
            r = dv_detect.detect_layer(str(f))
        assert r["layer"] == dv_detect.LAYER_UNKNOWN and r["error"] == "timeout"

    def test_cancellation_terminates_inflight_extract(self, tmp_path):
        f = tmp_path / "movie.mkv"; f.write_bytes(b"x")

        class Process:
            returncode = None
            terminated = False

            def communicate(self, timeout=None):
                if self.terminated:
                    self.returncode = -15
                    return b"", b""
                raise subprocess.TimeoutExpired("dovi_tool", timeout)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.terminated = True

        process = Process()
        checks = iter([False, True])

        with patch("shutil.which", return_value="/usr/local/bin/dovi_tool"), \
             patch("subprocess.Popen", return_value=process):
            r = dv_detect.detect_layer(
                str(f), cancel_requested=lambda: next(checks, True)
            )

        assert r == {
            "layer": dv_detect.LAYER_UNKNOWN,
            "tool": True,
            "error": "cancelled",
        }
        assert process.terminated is True


class TestDependencyStatus:
    def test_reports_dovi_tool_key(self):
        s = dv_detect.dependency_status()
        assert set(s) == {"dovi_tool"} and isinstance(s["dovi_tool"], bool)

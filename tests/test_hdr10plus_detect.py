"""Contracts for conservative, full-file HDR10+ classification."""

import json
from pathlib import Path

from backend.rename import hdr10plus_detect as subject


def test_first_frame_miss_is_not_an_hdr10plus_negative(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_quick_frame_evidence", lambda *_: False)
    monkeypatch.setattr(
        subject, "_full_extract", lambda *_: {"state": "unknown", "error": "timeout"}
    )

    result = subject.detect_hdr10plus(str(tmp_path / "movie.mkv"))

    assert result["state"] == "unknown"
    assert result["method"] == "full_extract"


def test_completed_full_extract_with_no_metadata_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_quick_frame_evidence", lambda *_: False)
    monkeypatch.setattr(subject, "_full_extract", lambda *_: {"state": "absent"})

    assert subject.detect_hdr10plus(str(tmp_path / "movie.mkv"))["state"] == "absent"


def test_quick_positive_avoids_full_extract(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_quick_frame_evidence", lambda *_: True)
    monkeypatch.setattr(
        subject,
        "_full_extract",
        lambda *_: (_ for _ in ()).throw(AssertionError("full extraction should not run")),
    )

    result = subject.detect_hdr10plus(str(tmp_path / "movie.mkv"))

    assert result == {"state": "present", "method": "ffprobe_first_frame", "tool_version": None, "error": None}


def test_full_extract_gives_tool_a_new_output_path(monkeypatch, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated-test-media")
    monkeypatch.setattr(subject.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(subject, "_tool_version", lambda *_: "test-version")

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        assert not output.exists(), "hdr10plus_tool output must not be pre-created"
        output.write_text(json.dumps({"SceneInfo": []}), encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subject.subprocess, "run", fake_run)

    result = subject._full_extract(str(media))

    assert result["state"] == "present"

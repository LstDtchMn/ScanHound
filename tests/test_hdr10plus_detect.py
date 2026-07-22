"""Contracts for conservative, full-file HDR10+ classification."""

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

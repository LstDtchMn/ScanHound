import json
from unittest.mock import patch, MagicMock
from backend.rename import mediainfo

FFPROBE_JSON = json.dumps({
    "format": {"format_name": "matroska,webm", "size": "42000000000",
               "duration": "7200.0", "bit_rate": "46000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
         "color_transfer": "smpte2084",
         "side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"codec_type": "audio", "codec_name": "truehd", "channels": 8,
         "channel_layout": "7.1"},
    ],
})

def test_probe_specs_parses_ffprobe(tmp_path):
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))
    assert s["present"] is True
    assert s["resolution"] == "2160p"
    assert s["video_codec"] == "HEVC"
    assert s["hdr"] == "Dolby Vision"          # DOVI side_data wins over PQ
    assert s["audio"].startswith("TrueHD")
    assert s["size_bytes"] == 42000000000
    assert s["duration_min"] == 120

def test_probe_specs_missing_file_returns_not_present():
    assert mediainfo.probe_specs("/no/such.mkv")["present"] is False


def test_probe_detailed_uses_full_detector_after_plain_hdr10(monkeypatch):
    monkeypatch.setattr(mediainfo, "probe_specs", lambda *_args, **_kwargs: {
        "present": True, "path": "/movie.mkv", "hdr": "HDR10", "video_codec": "HEVC",
    })
    monkeypatch.setattr(mediainfo.hdr10plus_detect, "detect_hdr10plus", lambda *_args, **_kwargs: {
        "state": "unknown", "method": "full_extract", "tool_version": "1.7.2", "error": "timeout",
    })

    result = mediainfo.probe_detailed("/movie.mkv")

    assert result["hdr"] == "HDR10"
    assert result["hdr10plus_state"] == "unknown"
    assert result["hdr10plus_evidence"]["method"] == "full_extract"


def test_probe_detailed_promotes_authoritative_hdr10plus(monkeypatch):
    monkeypatch.setattr(mediainfo, "probe_specs", lambda *_args, **_kwargs: {
        "present": True, "path": "/movie.mkv", "hdr": "HDR10", "video_codec": "HEVC",
    })
    monkeypatch.setattr(mediainfo.hdr10plus_detect, "detect_hdr10plus", lambda *_args, **_kwargs: {
        "state": "present", "method": "full_extract", "tool_version": "1.7.2", "error": None,
    })

    result = mediainfo.probe_detailed("/movie.mkv")

    assert result["hdr"] == "HDR10+"
    assert result["hdr10plus_state"] == "present"


def test_probe_detailed_preserves_all_streams_and_dolby_vision_fields(monkeypatch, tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"generated-test-media")
    monkeypatch.setattr(mediainfo, "probe_specs", lambda *_args, **_kwargs: {
        "present": True, "path": str(media), "hdr": "Dolby Vision", "video_codec": "HEVC",
    })
    monkeypatch.setattr(mediainfo.hdr10plus_detect, "detect_hdr10plus", lambda *_args, **_kwargs: {
        "state": "absent", "method": "full_extract", "tool_version": "1.7.2", "error": None,
    })
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "nb_streams": 4},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
             "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
             "color_space": "bt2020nc", "color_transfer": "smpte2084",
             "color_primaries": "bt2020", "side_data_list": [{
                 "side_data_type": "DOVI configuration record", "dv_profile": 7,
                 "dv_level": 6, "rpu_present_flag": 1, "el_present_flag": 1,
                 "bl_present_flag": 1, "dv_bl_signal_compatibility_id": 6,
             }]},
            {"codec_type": "audio", "index": 1, "codec_name": "truehd", "channels": 8,
             "channel_layout": "7.1", "tags": {"language": "eng", "title": "Atmos"}},
            {"codec_type": "audio", "index": 2, "codec_name": "ac3", "channels": 6,
             "tags": {"language": "spa"}},
            {"codec_type": "subtitle", "index": 3, "codec_name": "subrip",
             "disposition": {"forced": 1}, "tags": {"language": "eng"}},
        ],
    })
    monkeypatch.setattr(mediainfo.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(
        mediainfo.subprocess, "run",
        lambda *_args, **_kwargs: MagicMock(returncode=0, stdout=stream_json),
    )

    result = mediainfo.probe_detailed(str(media))

    assert result["dv_profile"] == "7"
    assert result["video_details"]["bit_depth"] == 10
    assert result["video_details"]["dolby_vision"]["el_present"] is True
    assert [stream["language"] for stream in result["audio_streams"]] == ["eng", "spa"]
    assert result["subtitle_streams"] == [{
        "index": 3, "codec": "subrip", "language": "eng", "title": None,
        "default": False, "forced": True, "hearing_impaired": False,
    }]
    assert result["hdr10plus_state"] == "absent"

def test_probe_specs_no_ffprobe_returns_none(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    with patch("shutil.which", return_value=None):
        assert mediainfo.probe_specs(str(f)) is None

def test_probe_specs_dv_layer_from_cache_only(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.get_dv_scan.return_value = {"dv_layer": "fel", "sig_mtime": None, "sig_size": None}
    db.dv_scan_is_current.return_value = True
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f), db=db)
    assert s["dv_layer"] == "fel"
    db.get_dv_scan.assert_called_once()          # cache read, no dovi_tool


# ── FIX 1: resolution must be classified by WIDTH, not HEIGHT — an
#    aspect-ratio crop (2.39:1 scope master) shrinks height without changing
#    the true resolution tier ──────────────────────────────────────────────

def test_res_label_cropped_4k_classified_by_width_not_height():
    # 2.39:1-cropped 4K master: height alone (1600) would misread this as
    # "1440p" (or worse). Width (3840) correctly keeps it 2160p.
    assert mediainfo._res_label(3840, 1600) == "2160p"


def test_res_label_cropped_1080p_classified_by_width_not_height():
    # 2.39:1-cropped 1080p rip: height alone (800) would misread this as
    # "720p". Width (1920) correctly keeps it 1080p.
    assert mediainfo._res_label(1920, 800) == "1080p"


def test_res_label_falls_back_to_height_when_width_missing():
    assert mediainfo._res_label(None, 2160) == "2160p"
    assert mediainfo._res_label(None, None) is None


def test_res_label_narrow_aspect_classified_by_max_axis():
    # Pillarbox / 4:3 crops shrink WIDTH, not height — the max-of-both-axes
    # tier keeps them correct (width alone would under-tier them).
    assert mediainfo._res_label(2880, 2160) == "2160p"  # 4:3 in a UHD frame
    assert mediainfo._res_label(1440, 1080) == "1080p"  # 4:3 HD (width 1440)
    # Normal 16:9 tiers are unchanged.
    assert mediainfo._res_label(2560, 1440) == "1440p"
    assert mediainfo._res_label(1280, 720) == "720p"


# ── FIX 3: a stale dv_scan row (signature mismatch — the file at this path
#    was overwritten since it was scanned) must NOT be trusted as this file's
#    DV layer ─────────────────────────────────────────────────────────────

def test_probe_specs_stale_dv_cache_ignored_after_overwrite(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    # A dv_scan row exists for this path (from before an Overwrite replaced
    # the file), but its signature no longer matches the on-disk file —
    # dv_scan_is_current must gate the cache read.
    db.get_dv_scan.return_value = {"dv_layer": "fel", "sig_mtime": 111.0, "sig_size": 999}
    db.dv_scan_is_current.return_value = False
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f), db=db)
    assert s["dv_layer"] is None
    db.dv_scan_is_current.assert_called_once()
    db.get_dv_scan.assert_not_called()  # never even consults the stale row


# ── Task 2: probe_specs() cache integration (media_probe) ─────────────────

def test_probe_specs_cache_hit_skips_ffprobe(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    st = f.stat()
    db = MagicMock()
    cached = {"present": True, "path": str(f), "size_bytes": 1, "container": "matroska",
              "duration_min": 1, "bitrate": 1, "resolution": "2160p", "video_codec": "HEVC",
              "hdr": None, "dv_layer": None, "audio": None}
    db.media_probe_is_current.return_value = True
    db.get_media_probe.return_value = {"probe_json": json.dumps(cached)}
    with patch("subprocess.run") as run_spy:
        s = mediainfo.probe_specs(str(f), db=db)
    run_spy.assert_not_called()
    assert s["resolution"] == "2160p"


def test_probe_specs_cache_miss_probes_and_caches(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.media_probe_is_current.return_value = False
    # No dv_scan cache entry for this fresh file — realistic first-probe
    # state, and required so the write-through result dict is JSON-safe
    # (an unconfigured MagicMock() here would make _cached_dv_layer return
    # a non-serializable Mock, breaking json.dumps(result) below).
    db.dv_scan_is_current.return_value = False
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake) as run_spy:
        s = mediainfo.probe_specs(str(f), db=db)
    run_spy.assert_called_once()
    assert s["present"] is True
    db.upsert_media_probe.assert_called_once()
    args, kwargs = db.upsert_media_probe.call_args
    assert args[0] == str(f)
    assert json.loads(args[1])["resolution"] == "2160p"


def test_probe_specs_failed_probe_not_cached(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    db = MagicMock()
    db.media_probe_is_current.return_value = False
    with patch("shutil.which", return_value=None):
        s = mediainfo.probe_specs(str(f), db=db)
    assert s is None
    db.upsert_media_probe.assert_not_called()


def test_probe_specs_no_db_still_works_uncached(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))  # db=None, existing default
    assert s["present"] is True


# ── Task 1: richer audio profile detection + HDR10+ frame probe ───────────

def test_probe_specs_detects_atmos_from_track_title(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "bt709"},
            {"codec_type": "audio", "codec_name": "truehd", "channels": 8,
             "channel_layout": "7.1", "tags": {"title": "TrueHD 7.1 Atmos"}},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert s["audio_profile"] == "TrueHD 7.1 Atmos"

def test_probe_specs_detects_dts_hd_profile(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "dts", "channels": 6,
             "channel_layout": "5.1", "profile": "DTS-HD MA"},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert "DTS-HD MA" in (s["audio_profile"] or "")

def test_probe_specs_no_atmos_no_special_profile_returns_none_audio_profile(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo"},
        ],
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream):
        s = mediainfo.probe_specs(str(f))
    assert s["audio_profile"] is None

def test_probe_specs_detects_hdr10_plus_via_frame_probe(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "smpte2084"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1"},
        ],
    })
    frame_json = json.dumps({
        "frames": [{"side_data_list": [
            {"side_data_type": "Mastering display metadata"},
            {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
        ]}]
    })
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    fake_frame = MagicMock(returncode=0, stdout=frame_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", side_effect=[fake_stream, fake_frame]):
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "HDR10+"

def test_probe_specs_plain_hdr10_stays_hdr10_when_no_hdr10_plus_metadata(tmp_path):
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    stream_json = json.dumps({
        "format": {"format_name": "matroska", "size": "1000", "duration": "60", "bit_rate": "1000"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
             "color_transfer": "smpte2084"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1"},
        ],
    })
    frame_json = json.dumps({"frames": [{"side_data_list": []}]})
    fake_stream = MagicMock(returncode=0, stdout=stream_json)
    fake_frame = MagicMock(returncode=0, stdout=frame_json)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", side_effect=[fake_stream, fake_frame]):
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "HDR10"

def test_probe_specs_dolby_vision_skips_frame_probe_entirely(tmp_path):
    """DV outranks/precludes an HDR10+ frame-probe call — only ONE subprocess.run
    should fire when the stream-level probe already found DOVI side_data."""
    f = tmp_path / "m.mkv"; f.write_bytes(b"x")
    fake_stream = MagicMock(returncode=0, stdout=FFPROBE_JSON)  # existing DV fixture
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake_stream) as mock_run:
        s = mediainfo.probe_specs(str(f))
    assert s["hdr"] == "Dolby Vision"
    assert mock_run.call_count == 1  # frame probe was skipped

def test_probe_specs_parses_ffprobe_still_passes_with_frame_probe_added(tmp_path):
    """Regression: the ORIGINAL fixture/test (single-mock, no 'frames' key) must
    keep passing once a second subprocess.run call exists in probe_specs — the
    frame-probe path must tolerate a stream-shaped mock gracefully."""
    f = tmp_path / "movie.mkv"; f.write_bytes(b"x")
    fake = MagicMock(returncode=0, stdout=FFPROBE_JSON)
    with patch("shutil.which", return_value="/usr/bin/ffprobe"), \
         patch("subprocess.run", return_value=fake):
        s = mediainfo.probe_specs(str(f))
    assert s["present"] is True
    assert s["hdr"] == "Dolby Vision"  # DV path — frame probe skipped, no crash

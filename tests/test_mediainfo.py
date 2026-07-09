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

"""Structured video technical-spec probe (ffprobe) for the conflict compare.

One ffprobe call per file → a stable spec dict. Fail-safe: returns None when
ffprobe is unavailable / errors / times out, and {"present": False} when the
file does not exist. The DV FEL/MEL layer is read ONLY from the dv_scan cache
(never shells the slow dovi_tool) — an on-demand scan resolves it separately.
Mirrors the fail-safe pattern of llm_identify.probe_video_width.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

_RES_LADDER = ((2160, "2160p"), (1440, "1440p"), (1080, "1080p"),
               (720, "720p"), (480, "480p"), (0, None))
_CODEC_LABEL = {"hevc": "HEVC", "h265": "HEVC", "avc": "H.264", "h264": "H.264",
                "av1": "AV1", "vc1": "VC-1", "mpeg2video": "MPEG-2"}
_AUDIO_LABEL = {"truehd": "TrueHD", "eac3": "EAC3", "ac3": "AC3", "dts": "DTS",
                "aac": "AAC", "flac": "FLAC", "opus": "Opus"}


def _res_label(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """Classify a video's resolution tier primarily from WIDTH. An
    aspect-ratio crop (2.39:1 scope masters etc.) shrinks HEIGHT without
    changing the true resolution tier, so keying off height alone
    misclassifies them: a cropped 4K release (3840x1600) would read as
    "1440p" and a cropped 1080p rip (1920x800) would read as "720p". Height
    is used only as a fallback when width is unavailable. Fail-safe: None
    when neither dimension is known."""
    w = width or 0
    if w:
        if w >= 3000:
            return "2160p"
        if w >= 2000:
            return "1440p"
        if w >= 1600:
            return "1080p"
        if w >= 1100:
            return "720p"
        if w >= 640:
            return "480p"
        return None
    h = height or 0
    for floor, label in _RES_LADDER:
        if h >= floor:
            return label
    return None


def _cached_dv_layer(path: str, mtime: Optional[float], size: Optional[int], db) -> Optional[str]:
    """Return the cached DV layer for ``path`` ONLY if the cache's stored
    (mtime, size) signature still matches the file on disk. Without this
    check, a stale dv_scan row (keyed by path) would keep reporting the OLD
    occupant's layer after that path is overwritten by a different file (e.g.
    an Overwrite conflict-resolution) — silently mislabeling the new file."""
    if db is None or mtime is None or size is None:
        return None
    try:
        if not db.dv_scan_is_current(path, mtime, size):
            return None
        row = db.get_dv_scan(path)
        if not row or not row.get("dv_layer"):
            return None
        return row.get("dv_layer")
    except Exception:
        return None


def probe_specs(path: str, timeout: int = 30, db=None) -> Optional[dict]:
    if not path:
        return None
    if not os.path.exists(path):
        return {"present": False, "path": path, "size_bytes": None,
                "container": None, "duration_min": None, "bitrate": None,
                "resolution": None, "video_codec": None, "hdr": None,
                "dv_layer": None, "audio": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    try:
        size = int(fmt.get("size")) if fmt.get("size") else os.path.getsize(path)
    except (TypeError, ValueError, OSError):
        size = None
    # On-disk (mtime, size) signature — used ONLY to validate the dv_scan
    # cache (dv_scan_is_current), independent of the ffprobe-reported size
    # above, so a stale cache entry from a since-overwritten file is detected
    # even if ffprobe's container size happens to coincide.
    try:
        _st = os.stat(path)
        disk_mtime, disk_size = _st.st_mtime, _st.st_size
    except OSError:
        disk_mtime, disk_size = None, None
    try:
        dur = float(fmt.get("duration")) if fmt.get("duration") else None
        duration_min = round(dur / 60) if dur and dur > 0 else None
    except (TypeError, ValueError):
        duration_min = None
    try:
        bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        bitrate = None

    vcodec = _CODEC_LABEL.get(str(video.get("codec_name") or "").lower(),
                              (video.get("codec_name") or None))
    resolution = _res_label(video.get("width"), video.get("height"))

    # HDR: Dolby Vision (DOVI side_data) outranks PQ/HLG.
    hdr = None
    sd = video.get("side_data_list") or []
    if any("dovi" in str(x.get("side_data_type", "")).lower()
           or "dolby vision" in str(x.get("side_data_type", "")).lower() for x in sd):
        hdr = "Dolby Vision"
    else:
        ct = str(video.get("color_transfer") or "").lower()
        if ct == "smpte2084":
            hdr = "HDR10"
        elif ct in ("arib-std-b67", "bt2020-10", "bt2020-12"):
            hdr = "HLG"

    acodec = _AUDIO_LABEL.get(str(audio.get("codec_name") or "").lower(),
                              (audio.get("codec_name") or None))
    chans = audio.get("channel_layout") or (
        f"{audio.get('channels')}ch" if audio.get("channels") else None)
    audio_label = f"{acodec} {chans}".strip() if acodec else None

    return {
        "present": True, "path": path, "size_bytes": size,
        "container": (fmt.get("format_name") or None),
        "duration_min": duration_min, "bitrate": bitrate,
        "resolution": resolution, "video_codec": vcodec, "hdr": hdr,
        "dv_layer": _cached_dv_layer(path, disk_mtime, disk_size, db),
        "audio": audio_label,
    }

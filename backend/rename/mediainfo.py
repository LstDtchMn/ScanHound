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
import re
import shutil
import subprocess
from typing import Optional

from backend.rename import hdr10plus_detect

_CODEC_LABEL = {"hevc": "HEVC", "h265": "HEVC", "avc": "H.264", "h264": "H.264",
                "av1": "AV1", "vc1": "VC-1", "mpeg2video": "MPEG-2"}
_AUDIO_LABEL = {"truehd": "TrueHD", "eac3": "EAC3", "ac3": "AC3", "dts": "DTS",
                "aac": "AAC", "flac": "FLAC", "opus": "Opus"}

_ATMOS_RE = re.compile(r"atmos", re.IGNORECASE)
_DTS_HD_RE = re.compile(r"dts[-\s]?hd|dts[:\s]?x", re.IGNORECASE)

# Per-axis tier thresholds (pixels → tier rank). A resolution tier is the
# MAX of the width-derived and height-derived tier, so neither a horizontal
# crop (2.39:1 scope: 3840x1600 → 2160p) nor a vertical/pillarbox crop
# (4:3 in a UHD frame: 2880x2160 → 2160p; 4:3 HD 1440x1080 → 1080p) is
# under-tiered by keying off a single shrunken axis.
_WIDTH_TIERS = ((3000, 5), (2000, 4), (1600, 3), (1100, 2), (640, 1))
_HEIGHT_TIERS = ((1700, 5), (1250, 4), (880, 3), (620, 2), (340, 1))
_TIER_LABEL = {5: "2160p", 4: "1440p", 3: "1080p", 2: "720p", 1: "480p", 0: None}


def _axis_tier(px: int, tiers) -> int:
    for floor, rank in tiers:
        if px >= floor:
            return rank
    return 0


def _res_label(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """Classify a video's resolution tier from the MAX of its width- and
    height-derived tiers. An aspect-ratio crop shrinks one axis without
    changing the true tier (2.39:1 scope shrinks height; 4:3/pillarbox
    shrinks width), so taking the max of both axes classifies either crop
    direction correctly. Fail-safe: None when neither dimension is known."""
    rank = max(_axis_tier(width or 0, _WIDTH_TIERS),
               _axis_tier(height or 0, _HEIGHT_TIERS))
    return _TIER_LABEL[rank]


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
                "dv_layer": None, "audio": None, "audio_profile": None}
    try:
        _st = os.stat(path)
        disk_mtime, disk_size = _st.st_mtime, _st.st_size
    except OSError:
        disk_mtime, disk_size = None, None
    # Cache check: a signature-matching prior probe is reused verbatim,
    # skipping the ffprobe subprocess entirely. A probe FAILURE (None) is
    # never cached (see the bottom of this function), so there's nothing to
    # hit here for a file that previously failed to probe.
    if db is not None and disk_mtime is not None and db.media_probe_is_current(path, disk_mtime, disk_size):
        cached_row = db.get_media_probe(path)
        if cached_row and cached_row.get("probe_json"):
            try:
                return json.loads(cached_row["probe_json"])
            except (json.JSONDecodeError, TypeError):
                pass  # corrupt cache row — fall through and re-probe
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

    # HDR10+ requires a SEPARATE frame-level probe — the stream-level probe
    # never surfaces it (it's a per-frame dynamic-metadata side_data, not a
    # stream-level field). Only run it when the stream-level probe already
    # found plain HDR10 (PQ transfer) and DV wasn't already detected — a DV
    # file's hdr stays "Dolby Vision" and never needs this extra call.
    if hdr == "HDR10":
        try:
            fr = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_frames", "-read_intervals", "%+#1",
                 "-select_streams", "v:0", path],
                capture_output=True, text=True, timeout=timeout)
            if fr.returncode == 0:
                frame_data = json.loads(fr.stdout)
                frames = frame_data.get("frames") or []
                for fr_entry in frames:
                    for sd in (fr_entry.get("side_data_list") or []):
                        sdt = str(sd.get("side_data_type", ""))
                        if "HDR10+" in sdt or "SMPTE2094-40" in sdt:
                            hdr = "HDR10+"
                            break
                    if hdr == "HDR10+":
                        break
        except Exception:
            pass  # frame probe failure must never fail the whole probe — stays plain HDR10

    acodec = _AUDIO_LABEL.get(str(audio.get("codec_name") or "").lower(),
                              (audio.get("codec_name") or None))
    chans = audio.get("channel_layout") or (
        f"{audio.get('channels')}ch" if audio.get("channels") else None)
    audio_label = f"{acodec} {chans}".strip() if acodec else None

    # Richer audio-profile detection: Atmos/DTS-HD sub-profile signal lives
    # in the audio stream's tags.title (e.g. "TrueHD 7.1 Atmos") and
    # sometimes the profile field — NOT reliably in codec_name alone.
    audio_signal = f"{acodec or ''} {(audio.get('profile') or '')} {((audio.get('tags') or {}).get('title') or '')}"
    audio_profile = None
    if _ATMOS_RE.search(audio_signal):
        base = acodec or "Audio"
        audio_profile = f"{base} {chans} Atmos".strip() if chans else f"{base} Atmos"
    elif _DTS_HD_RE.search(audio_signal):
        # Prefer the real profile string when ffprobe supplied one (e.g. "DTS-HD MA");
        # otherwise fall back to a generic label.
        audio_profile = (audio.get("profile") or "DTS-HD").strip()
        if chans:
            audio_profile = f"{audio_profile} {chans}"

    result = {
        "present": True, "path": path, "size_bytes": size,
        "container": (fmt.get("format_name") or None),
        "duration_min": duration_min, "bitrate": bitrate,
        "resolution": resolution, "video_codec": vcodec, "hdr": hdr,
        "dv_layer": _cached_dv_layer(path, disk_mtime, disk_size, db),
        "audio": audio_label, "audio_profile": audio_profile,
    }
    if db is not None and disk_mtime is not None:
        try:
            db.upsert_media_probe(path, json.dumps(result),
                                   sig_mtime=disk_mtime, sig_size=disk_size)
        except Exception:
            pass  # cache write failure must never fail the probe itself
    return result


def probe_detailed(path: str, timeout: int = 30, db=None) -> Optional[dict]:
    """Return scan-grade technical evidence without changing ``probe_specs``.

    Existing rename callers retain the compact/cacheable ``probe_specs``
    contract.  The full-library inventory uses this opt-in function, which
    preserves the difference between an authoritative HDR10+ negative and an
    unavailable or incomplete detector.
    """
    result = probe_specs(path, timeout=timeout, db=db)
    if result is None:
        return None

    detailed = dict(result)
    if not result.get("present"):
        detailed.update({
            "hdr10plus_state": "unknown",
            "hdr10plus_evidence": {
                "state": "unknown", "method": "not_present", "tool_version": None,
                "error": "file_not_present",
            },
        })
        return detailed

    hdr = result.get("hdr")
    if hdr == "HDR10+":
        evidence = {
            "state": "present", "method": "ffprobe_first_frame", "tool_version": None,
            "error": None,
        }
    elif hdr == "HDR10":
        evidence = hdr10plus_detect.detect_hdr10plus(
            path, quick_timeout=timeout, full_timeout=max(timeout, 300)
        )
        if evidence.get("state") == "present":
            detailed["hdr"] = "HDR10+"
    elif hdr == "Dolby Vision":
        # Dolby Vision can coexist with HDR10+ compatibility metadata. Do not
        # infer its absence merely because the compact probe prioritizes DV.
        evidence = {
            "state": "unknown", "method": "dv_requires_full_stream_analysis",
            "tool_version": None, "error": None,
        }
    else:
        evidence = {"state": "absent", "method": "not_hdr10_pq", "tool_version": None,
                    "error": None}

    detailed["hdr10plus_state"] = evidence["state"]
    detailed["hdr10plus_evidence"] = evidence
    return detailed

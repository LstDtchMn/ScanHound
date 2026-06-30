"""Dolby Vision enhancement-layer detection (FEL vs MEL vs single-layer/none).

The question "does this 4K file carry a Dolby Vision *Full* Enhancement Layer
(FEL) or only a *Minimal* one (MEL)?" CANNOT be answered from container or
track-size metadata — in Profile 7 the BL+EL are interleaved into one HEVC
stream and a MEL EL is not zero bytes, so ffprobe/MediaInfo can't tell them
apart. The only reliable signal is the RPU's NLQ (non-linear quantizer) data,
which ``dovi_tool`` (quietvoid) resolves to an authoritative ``(FEL)``/``(MEL)``
token on its ``info`` summary line.

Verified recipe (two stages — there is no single-call HEVC→FEL/MEL path):

    dovi_tool extract-rpu "<file>" -o <rpu.bin>          # full pass, no decode
    dovi_tool info -i <rpu.bin> -s                       # grep the Profile line

The ``Profile: 7 (FEL)`` / ``Profile: 7 (MEL)`` / ``Profile: 7 (MEL, FEL)``
parenthetical is the discriminator. Profile 5/8 are single-layer (no EL); a
missing RPU means no Dolby Vision at all.

Everything here is fail-safe: a missing ``dovi_tool``, an unreadable file, a
timeout, or any subprocess error yields ``layer="unknown"`` (never an
exception), so a caller in the rename pipeline can never be crashed by it.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Container extensions dovi_tool can demux directly. (.m2ts has no clean tag
# slot downstream, but detection still works.)
_SUPPORTED_EXTS = frozenset({".mkv", ".m2ts", ".ts", ".hevc", ".h265", ".mp4"})

# Generous: a full RPU extraction streams the whole file (no pixel decode), so a
# big 4K remux can take a couple of minutes, dominated by disk read.
_EXTRACT_TIMEOUT = 1800
_INFO_TIMEOUT = 120

# The parenthetical token(s) after "Profile: N" in `dovi_tool info -s` output.
_PROFILE_RE = re.compile(r"Profile:\s*([0-9.]+)\s*(?:\(([^)]*)\))?", re.IGNORECASE)

# Result layer values:
#   'fel'       Profile 7 with a Full Enhancement Layer (the prize)
#   'mel'       Profile 7 with only a Minimal Enhancement Layer (≡ P8.1)
#   'profile5'  single-layer DV, not HDR10-compatible
#   'profile8'  single-layer DV (8.x; EL absent or stripped)
#   'none'      no Dolby Vision RPU found (may still be HDR10/HDR10+)
#   'unknown'   detection could not run (no dovi_tool / error / unreadable)
LAYER_FEL = "fel"
LAYER_MEL = "mel"
LAYER_P5 = "profile5"
LAYER_P8 = "profile8"
LAYER_NONE = "none"
LAYER_UNKNOWN = "unknown"


def available() -> bool:
    """Whether the ``dovi_tool`` binary is on PATH."""
    return bool(shutil.which("dovi_tool"))


def dependency_status() -> dict:
    """Report the binary this module needs, mirroring llm_identify's shape."""
    return {"dovi_tool": available()}


def _classify(profile: str, subtoken: str) -> str:
    """Map a parsed ``Profile: N (tokens)`` pair to a layer constant.

    ``(MEL, FEL)`` (a mixed title with some FEL frames) counts as FEL — any FEL
    frame makes the file a FEL grab.
    """
    sub = (subtoken or "").upper()
    if "FEL" in sub:
        return LAYER_FEL
    if "MEL" in sub:
        return LAYER_MEL
    # Compare on the integer part so zero-padding ("07") or a sub-profile
    # ("8.1") still classifies correctly.
    try:
        major = int(float((profile or "").strip()))
    except (TypeError, ValueError):
        major = -1
    if major == 5:
        return LAYER_P5
    if major == 7:
        # P7 with no FEL/MEL token shouldn't happen, but an EL is present —
        # err toward the conservative non-FEL bucket.
        return LAYER_MEL
    if major == 8:
        return LAYER_P8
    return LAYER_NONE


def _parse_info(summary: str) -> str:
    """Extract a layer constant from ``dovi_tool info -s`` output."""
    best = LAYER_NONE
    for m in _PROFILE_RE.finditer(summary or ""):
        layer = _classify(m.group(1), m.group(2))
        # FEL wins over everything; otherwise take the first concrete signal.
        if layer == LAYER_FEL:
            return LAYER_FEL
        if best in (LAYER_NONE,) and layer != LAYER_NONE:
            best = layer
    return best


def detect_layer(path: str) -> dict:
    """Detect the Dolby Vision enhancement-layer type of a video file.

    Returns a dict::

        {"layer": <LAYER_*>, "tool": bool, "error": str | None}

    ``tool`` is False when ``dovi_tool`` is unavailable. The function never
    raises — any failure resolves to ``layer="unknown"``.
    """
    if not available():
        return {"layer": LAYER_UNKNOWN, "tool": False, "error": "dovi_tool not installed"}
    if not path or not os.path.isfile(path):
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "file not found"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in _SUPPORTED_EXTS:
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": f"unsupported container {ext}"}

    dovi = shutil.which("dovi_tool")
    rpu = None
    try:
        fd, rpu = tempfile.mkstemp(suffix=".rpu.bin")
        os.close(fd)
        # Stage 1: extract the RPU. dovi_tool demuxes the container itself —
        # preferred over an ffmpeg pipe, which can drop EL NALs and misreport a
        # true FEL as MEL/P8.
        ex = subprocess.run([dovi, "extract-rpu", path, "-o", rpu],
                            capture_output=True, timeout=_EXTRACT_TIMEOUT)
        rpu_size = os.path.getsize(rpu)
        if ex.returncode != 0 or not rpu_size:
            err = (ex.stderr or b"").decode("utf-8", "ignore").strip()
            low = err.lower()
            # An empty RPU, or an explicit "no RPU", means no Dolby Vision (the
            # file may still be HDR10 — out of scope). A nonzero exit with some
            # other error is a genuine failure → unknown, not "no DV".
            if not rpu_size or "no rpu" in low or "not found" in low:
                return {"layer": LAYER_NONE, "tool": True, "error": None}
            return {"layer": LAYER_UNKNOWN, "tool": True, "error": err[:200] or "extract failed"}
        # Stage 2: read the FEL/MEL token from the summary. A failed info call
        # must NOT be parsed as "no Profile line found" (→ false 'none'); the RPU
        # extracted fine, so a failure here is 'unknown'.
        info = subprocess.run([dovi, "info", "-i", rpu, "-s"],
                              capture_output=True, timeout=_INFO_TIMEOUT)
        if info.returncode != 0:
            ierr = (info.stderr or b"").decode("utf-8", "ignore").strip()
            return {"layer": LAYER_UNKNOWN, "tool": True,
                    "error": f"info failed: {ierr[:180]}" if ierr else "info failed"}
        out = (info.stdout or b"").decode("utf-8", "ignore")
        return {"layer": _parse_info(out), "tool": True, "error": None}
    except subprocess.TimeoutExpired:
        logger.warning("dovi_tool timed out on %s", path)
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "timeout"}
    except Exception as e:
        logger.debug("dv_detect failed on %s: %s", path, e)
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": str(e)[:200]}
    finally:
        if rpu:
            try:
                os.remove(rpu)
            except OSError:
                pass

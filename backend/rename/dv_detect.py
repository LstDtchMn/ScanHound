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

WHAT A dv_layer VALUE FROM THIS MODULE ASSERTS.

The contract is "this file contains at least one frame of the reported layer",
NOT "this file completed a full successful scan". That is already how the
consumer reads it -- dv_labeler.pick_layer aggregates parts with "one part
proving Dolby Vision proves it for the title" -- and it is what makes the
bounded accelerator below sound: FEL observed anywhere is FEL, full stop.
The inverse does not hold, which is why only a FEL observation may skip the
full pass. See probe_fel_bounded.

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

from backend.rename.process_control import (
    ProcessCancelled,
    ProcessStalled,
    run_cancellable,
)

logger = logging.getLogger(__name__)

# Container extensions dovi_tool can demux directly. (.m2ts has no clean tag
# slot downstream, but detection still works.)
_SUPPORTED_EXTS = frozenset({".mkv", ".m2ts", ".ts", ".hevc", ".h265", ".mp4"})

# Generous: a full RPU extraction streams the whole file (no pixel decode), so a
# big 4K remux can take a couple of minutes, dominated by disk read.
_EXTRACT_TIMEOUT = 1800
_INFO_TIMEOUT = 120

# Bytes-read stall window for the FULL extract. Measured 2026-08-09: healthy
# extractions on this library sustain 57-153 MB/s end-to-end, against a storage
# path that streams 145-221 MB/s -- so three minutes of ZERO bytes read is not
# a slow file, it is a wedged process. Two titles reproduce exactly that (95%
# of one core, no read syscalls at all) and previously burned the full 1800 s
# each, on every run, forever. The wall-clock cap above is kept as the outer
# bound for genuinely slow reads; this is what catches a hang quickly.
_EXTRACT_STALL = 180

# The bounded FEL-positive accelerator (see probe_fel_bounded).
_BOUNDED_FRAME_LIMIT = 1000
_BOUNDED_TIMEOUT = 300
_BOUNDED_STALL = 60

# The profile line in `dovi_tool info -s` output. Both spellings are real:
# "Profile: 7 (FEL)" for a single profile, and "Profiles: 7, 8" when the RPU set
# spans more than one. Matching only the singular did not merely fail to parse
# the plural -- it fell through to LAYER_NONE with error=None, i.e. an
# AUTHORITATIVE "no Dolby Vision" for a file that has it. dv_labeler treats
# 'none' as authoritative and removes the managed label on it, so the old regex
# could strip a real DV badge off a mixed-profile title.
_PROFILE_RE = re.compile(
    r"Profiles?:\s*([0-9.]+(?:\s*,\s*[0-9.]+)*)\s*(?:\(([^)]*)\))?", re.IGNORECASE)

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

#: dovi_tool messages that positively assert "this stream carries no RPU".
#: Deliberately RPU-SPECIFIC. A bare "not found" test also matched failures
#: like "input file not found" / "video track not found" / "NAL unit not
#: found" -- and a file CAN vanish between the isfile() check and the
#: subprocess, which is precisely the NAS/mount failure this module exists to
#: classify honestly. Matching those as absence would rebuild the
#: false-authoritative path: a mount hiccup becoming "no Dolby Vision", which
#: then authorises label removal.
_NO_RPU_MESSAGES = (
    "no rpu",              # "No RPU found"
    "rpu not found",
    "no dolby vision rpu",
)


def _says_no_rpu(stderr_lower: str) -> bool:
    """True only when dovi_tool itself reported an absent RPU."""
    return any(m in stderr_lower for m in _NO_RPU_MESSAGES)


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
    # PROFILE FIRST, then tokens -- and tokens only where they are meaningful.
    #
    # The previous order tested `"FEL" in sub` before looking at the profile at
    # all, on a raw substring. Two ways that produced an authoritative WRONG
    # label: "Profile: 8 (FEL)" classified as FEL even though a profile-8 stream
    # has no FEL/MEL subtype, and "Profile: 7 (NOT FEL)" also classified as FEL
    # because the negation contains the token. A bare "Profile: 7" additionally
    # returned MEL, described in the old comment as "the conservative non-FEL
    # bucket" -- but MEL is not a conservative bucket here. It is a MANAGED Plex
    # label (DV MEL) that authoritative reconciliation can use to replace
    # another. The conservative value for "profile 7 seen, subtype not proven"
    # is unknown, which is non-authoritative and can never remove a label.
    # (Consolidation blocker 3.)
    #
    # Compare on the integer part so zero-padding ("07") or a sub-profile
    # ("8.1") still classifies correctly.
    try:
        major = int(float((profile or "").strip()))
    except (TypeError, ValueError):
        major = -1

    # Exact tokens, not substrings: split the parenthetical on non-letters so
    # "NOT FEL" yields {"NOT", "FEL"} and never matches as a bare "FEL".
    tokens = set(re.split(r"[^A-Z]+", (subtoken or "").upper())) - {""}

    if major == 7:
        # Only profile 7 carries an enhancement layer, so only here do the
        # FEL/MEL tokens mean anything.
        #
        # An UNRECOGNISED token makes the whole parenthetical ambiguous, so the
        # answer is unknown rather than a guess. Exact tokens alone are not
        # enough: "(NOT FEL)" tokenises to {NOT, FEL} and would otherwise read
        # as FEL -- a negation classified as its own opposite. Every real
        # dovi_tool summary contains only FEL and/or MEL here, so this rejects
        # malformed input without refusing anything the tool actually emits.
        if tokens - {"FEL", "MEL"}:
            return LAYER_UNKNOWN
        # FEL wins a mixed "(MEL, FEL)" title: any FEL frame makes it a FEL grab.
        if "FEL" in tokens:
            return LAYER_FEL
        if "MEL" in tokens:
            return LAYER_MEL
        return LAYER_UNKNOWN
    if major == 5:
        return LAYER_P5
    if major == 8:
        return LAYER_P8
    return LAYER_NONE


def _parse_info(summary: str) -> str:
    """Extract a layer constant from ``dovi_tool info -s`` output."""
    best = LAYER_NONE
    for m in _PROFILE_RE.finditer(summary or ""):
        # "Profiles: 7, 8" carries several values on one line; classify each and
        # let the same precedence apply as if they had been separate lines.
        for profile in (p.strip() for p in (m.group(1) or "").split(",")):
            if not profile:
                continue
            layer = _classify(profile, m.group(2))
            # FEL wins over everything; otherwise take the first concrete signal.
            if layer == LAYER_FEL:
                return LAYER_FEL
            if best in (LAYER_NONE,) and layer != LAYER_NONE:
                best = layer
    return best


def probe_fel_bounded(path: str, *, cancel_requested=None,
                      limit: int = _BOUNDED_FRAME_LIMIT) -> bool:
    """Cheap FEL-POSITIVE accelerator. True ONLY when FEL is proven.

    ``dovi_tool extract-rpu -l N`` stops after N frames, which reads the head of
    the file instead of all of it: measured 1.8-9.6 s versus 2-24 minutes for a
    full pass on the same titles.

    THE SEMANTICS ARE ASYMMETRIC, AND THAT ASYMMETRY IS THE WHOLE DESIGN.

      * A bounded sample containing a FEL frame PROVES the title contains FEL.
        No frame later in the file can retract it. This is final, and it is
        exactly the property dv_labeler.pick_layer already acts on ("one part
        proving Dolby Vision proves it for the title").
      * A bounded sample containing only MEL -- or Profile 5, Profile 8, or no
        RPU at all -- PROVES NOTHING. A later frame may still be FEL, and a
        mixed "(MEL, FEL)" title can legitimately open on MEL.

    So this returns True or False, never a layer. False means NEEDS_FULL_SCAN,
    never "not FEL". Treating a bounded non-FEL as authoritative would let a
    sampled 'none' remove a real DV badge, which is the one outcome the whole
    module is built to prevent.

    Validated 2026-08-09 against 22 titles whose layer came from a completed
    full pass (8 FEL, 8 MEL, 3 P8, 2 P5, 1 none): 22/22 agreed, and every FEL
    title was already FEL within the first 1000 frames. Note what that does NOT
    cover -- no title reporting a mixed "(MEL, FEL)" appeared in the sample, so
    the MEL half stays unvalidated by construction, which is precisely why only
    the FEL half is trusted here.
    """
    if not available() or not path or not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() not in _SUPPORTED_EXTS:
        return False
    dovi = shutil.which("dovi_tool")
    rpu = None
    try:
        fd, rpu = tempfile.mkstemp(suffix=".rpu.bin")
        os.close(fd)
        ex = run_cancellable(
            [dovi, "extract-rpu", path, "-l", str(int(limit)), "-o", rpu],
            timeout=_BOUNDED_TIMEOUT,
            cancel_requested=cancel_requested,
            stall_timeout=_BOUNDED_STALL,
        )
        if ex.returncode != 0 or not os.path.getsize(rpu):
            return False
        info = run_cancellable(
            [dovi, "info", "-i", rpu, "-s"],
            timeout=_INFO_TIMEOUT,
            cancel_requested=cancel_requested,
        )
        if info.returncode != 0:
            return False
        out = (info.stdout or b"").decode("utf-8", "ignore")
        return _parse_info(out) == LAYER_FEL
    except ProcessCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        # An accelerator that cannot fail closed is not an accelerator. Any
        # trouble here just means the full pass decides, as it always did.
        logger.debug("bounded FEL probe failed on %s: %s", path, e)
        return False
    finally:
        if rpu:
            try:
                os.remove(rpu)
            except OSError:
                pass


def detect_layer(path: str, *, cancel_requested=None, bounded_first: bool = True) -> dict:
    """Detect the Dolby Vision enhancement-layer type of a video file.

    Returns a dict::

        {"layer": <LAYER_*>, "tool": bool, "error": str | None, "evidence": str}

    ``tool`` is False when ``dovi_tool`` is unavailable. ``evidence`` is
    ``"bounded"`` when the answer came from the fast FEL-positive probe and
    ``"full"`` when a complete pass produced it — same layer values either way,
    recorded so an operator can tell which path answered. The function never
    raises — any failure resolves to ``layer="unknown"``.

    Set *bounded_first* False to force a full pass (used by the tests that must
    exercise the slow path, and available as an escape hatch).
    """
    if not available():
        return {"layer": LAYER_UNKNOWN, "tool": False, "error": "dovi_tool not installed",
                "evidence": None}
    if not path or not os.path.isfile(path):
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "file not found",
                "evidence": None}
    ext = os.path.splitext(path)[1].lower()
    if ext not in _SUPPORTED_EXTS:
        return {"layer": LAYER_UNKNOWN, "tool": True,
                "error": f"unsupported container {ext}", "evidence": None}

    # FEL-positive fast path. Only a positive result short-circuits; anything
    # else falls through to the full pass below, so no non-FEL verdict is ever
    # reached from a sample.
    if bounded_first:
        try:
            if probe_fel_bounded(path, cancel_requested=cancel_requested):
                return {"layer": LAYER_FEL, "tool": True, "error": None,
                        "evidence": "bounded"}
        except ProcessCancelled:
            return {"layer": LAYER_UNKNOWN, "tool": True, "error": "cancelled",
                    "evidence": None}

    dovi = shutil.which("dovi_tool")
    rpu = None
    try:
        fd, rpu = tempfile.mkstemp(suffix=".rpu.bin")
        os.close(fd)
        # Stage 1: extract the RPU. dovi_tool demuxes the container itself —
        # preferred over an ffmpeg pipe, which can drop EL NALs and misreport a
        # true FEL as MEL/P8.
        ex = run_cancellable(
            [dovi, "extract-rpu", path, "-o", rpu],
            timeout=_EXTRACT_TIMEOUT,
            cancel_requested=cancel_requested,
            stall_timeout=_EXTRACT_STALL,
        )
        rpu_size = os.path.getsize(rpu)
        if ex.returncode != 0 or not rpu_size:
            err = (ex.stderr or b"").decode("utf-8", "ignore").strip()
            low = err.lower()
            # "No Dolby Vision" is a POSITIVE finding and may only be reported
            # when the tool actually succeeded, or said so itself.
            #
            # The empty-RPU test must NOT come first: mkstemp pre-creates the
            # output file at zero bytes and dovi_tool writes it only on
            # success, so EVERY failure mode leaves rpu_size == 0 — a read
            # error on the media mount, a truncated file, a demux error. The
            # old ordering therefore reported those as an authoritative
            # LAYER_NONE with error=None, exactly inverting the intent this
            # comment block has always stated. On 9p/SMB mounts, where
            # dovi_tool read failures are an expected event rather than a
            # rare one, that silently marked real Dolby Vision files as
            # having none.
            if _says_no_rpu(low) or (ex.returncode == 0 and not rpu_size):
                return {"layer": LAYER_NONE, "tool": True, "error": None,
                        "evidence": "full"}
            return {"layer": LAYER_UNKNOWN, "tool": True,
                    "error": err[:200] or "extract produced no RPU",
                    "evidence": None}
        # Stage 2: read the FEL/MEL token from the summary. A failed info call
        # must NOT be parsed as "no Profile line found" (→ false 'none'); the RPU
        # extracted fine, so a failure here is 'unknown'.
        info = run_cancellable(
            [dovi, "info", "-i", rpu, "-s"],
            timeout=_INFO_TIMEOUT,
            cancel_requested=cancel_requested,
        )
        if info.returncode != 0:
            ierr = (info.stderr or b"").decode("utf-8", "ignore").strip()
            return {"layer": LAYER_UNKNOWN, "tool": True,
                    "error": f"info failed: {ierr[:180]}" if ierr else "info failed",
                    "evidence": None}
        out = (info.stdout or b"").decode("utf-8", "ignore")
        return {"layer": _parse_info(out), "tool": True, "error": None,
                "evidence": "full"}
    except ProcessCancelled:
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "cancelled",
                "evidence": None}
    except ProcessStalled as e:
        # Distinct from 'timeout' on purpose: this one says the process was
        # ALIVE and doing nothing, which is the signature of a file that will
        # never complete rather than one that merely needs longer. The scanner
        # uses the distinction to back it off instead of retrying it hourly.
        logger.warning("dovi_tool stalled on %s (%s)", path, e)
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "stalled",
                "evidence": None}
    except subprocess.TimeoutExpired:
        logger.warning("dovi_tool timed out on %s", path)
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": "timeout",
                "evidence": None}
    except Exception as e:
        logger.debug("dv_detect failed on %s: %s", path, e)
        return {"layer": LAYER_UNKNOWN, "tool": True, "error": str(e)[:200],
                "evidence": None}
    finally:
        if rpu:
            try:
                os.remove(rpu)
            except OSError:
                pass

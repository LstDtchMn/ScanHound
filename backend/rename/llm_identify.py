"""Optional Ollama-assisted media identification.

A thin, fail-safe adapter: given a messy release filename, ask a local Ollama
model to extract structured fields. Used ONLY as a fallback for low-confidence
deterministic matches — the result is always re-validated against TMDB and the
confidence gate, so the model never supplies IDs or bypasses review. Any error,
timeout, or non-JSON response yields ``None`` and the caller falls back cleanly.

Uses ``requests`` (already a ScanHound dependency); no extra packages.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, List, Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_SYSTEM = (
    "You extract structured metadata from a single media release filename. "
    "Respond ONLY with a JSON object with keys: "
    "title (string), year (integer or null), type ('movie' or 'tv'), "
    "season (integer or null), episode (integer or null). "
    "Do not invent IDs. Use null when a field is unknown. "
    "When TMDB candidates are provided, pick the one that best matches the "
    "filename and return its exact title and year."
)


def identify(filename: str, *, base_url: str, model: str,
             timeout: float = _TIMEOUT,
             parsed_year: Optional[int] = None,
             candidates: Optional[List[dict]] = None) -> Optional[dict[str, Any]]:
    """Ask Ollama to parse a release filename into structured fields.

    ``parsed_year`` anchors disambiguation when the filename year is known.
    ``candidates`` is a list of ``{title, year, confidence}`` dicts from a
    prior TMDB search — giving the model concrete options cuts hallucination
    on ambiguous titles (remakes, foreign films, generic names).

    Returns ``{title, year, media_type, season, episode}`` or ``None`` on any
    failure (the caller then keeps its deterministic result).
    """
    if not filename or not base_url or not model:
        return None

    user_content = filename
    if parsed_year:
        user_content += f"\nParsed year: {parsed_year}"
    if candidates:
        lines = [
            f"  {i + 1}. \"{c['title']}\" ({c.get('year') or '?'}) "
            f"— confidence {c.get('confidence', 0):.0f}"
            for i, c in enumerate(candidates[:5])
        ]
        user_content += "\nTMDB candidates:\n" + "\n".join(lines)

    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return _normalize(json.loads(content))
    except Exception as e:
        logger.debug("Ollama identify failed: %s", e)
        return None


def _normalize(data: Any) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return None

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    mtype = data.get("type")
    return {
        "title": title,
        "year": _int(data.get("year")),
        "media_type": mtype if mtype in ("movie", "tv") else None,
        "season": _int(data.get("season")),
        "episode": _int(data.get("episode")),
    }


def video_duration_minutes(video_path: str, timeout: int = 30) -> float | None:
    """Return the duration of a video file in minutes via ffprobe.

    Returns a rounded integer number of minutes, or ``None`` if ffprobe is
    unavailable or the file cannot be read.  Entirely fail-safe.
    """
    import shutil
    import subprocess

    if not video_path:
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", video_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        duration = float(json.loads(r.stdout)["format"]["duration"])
        return round(duration / 60) if duration > 0 else None
    except Exception:
        return None


# Keep a seconds-based alias used internally by identify_from_frames.
def _video_duration_seconds(video_path: str) -> float | None:
    mins = video_duration_minutes(video_path)
    return mins * 60 if mins is not None else None


def probe_video_width(video_path: str, timeout: int = 30) -> Optional[int]:
    """Return the pixel width of a video file's primary video stream via
    ffprobe, or ``None`` if ffprobe is unavailable, the file can't be read,
    or the call errors/times out. Entirely fail-safe — mirrors
    :func:`video_duration_minutes`'s invocation pattern (same binary
    resolution via ``shutil.which``, same subprocess/timeout/error handling).

    Used to disambiguate routing when a filename carries no resolution tag:
    callers treat a returned width >= 3000 as effectively 4K/2160p.
    """
    import shutil
    import subprocess

    if not video_path:
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "json", video_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        streams = json.loads(r.stdout).get("streams") or []
        if not streams:
            return None
        width = streams[0].get("width")
        return int(width) if width else None
    except Exception:
        return None


_VISION_TIMEOUT = 45.0
_VISION_SYSTEM = (
    "You identify movies and TV shows from video frames. "
    "Look for title cards, opening or closing credits, watermarks, and any "
    "visible on-screen text. Respond ONLY with a JSON object with keys: "
    "title (string or null), year (integer or null), type ('movie' or 'tv'). "
    "If you cannot identify the title from this frame, set title to null."
)
# Phase 1: high-value timestamps likely to contain title cards / opening credits.
# End-credits offset (~95% of duration) is appended dynamically when duration is known.
_FRAME_PRIORITY = [30, 60, 120]
# Phase 2: if priority frames all miss, sample this many evenly-spaced intervals
# across the video (or fall back to fixed times if duration is unknown).
_FRAME_GRID_COUNT = 8
_FRAME_GRID_FIXED = [180, 300, 600, 900, 1200, 1500, 1800, 2400]
_FRAME_MAX = 12  # hard cap across both phases


def identify_from_frames(
    video_path: str, *, base_url: str, model: str,
    timeout: float = _VISION_TIMEOUT,
    candidates: Optional[List[dict]] = None,
) -> Optional[dict[str, Any]]:
    """Extract frames from a video file and ask a vision-capable Ollama model
    to identify the title.

    Two-phase strategy:
      Phase 1 — priority timestamps (30s, 60s, 120s + end-credits at ~95%)
                 that are most likely to show a title card or opening credits.
      Phase 2 — if no hit, expand to an evenly-spaced grid across the full
                 video (or fixed fallback times when duration is unknown).

    ``candidates`` is a list of ``{title, year}`` dicts from a prior (weak) TMDB
    search. When provided, the model is constrained to pick one of them (or
    null) rather than identifying open-ended — this sharply cuts the
    hallucination a single out-of-context frame otherwise invites.

    Returns on the first positive identification, or ``None`` after exhausting
    up to ``_FRAME_MAX`` frames.  Entirely fail-safe — any exception at any
    step returns ``None``.
    """
    import base64
    import os as _os
    import subprocess
    import tempfile

    if not video_path or not base_url or not model:
        return None

    cand_hint = ""
    if candidates:
        lines = [f"  {i + 1}. \"{c['title']}\" ({c.get('year') or '?'})"
                 for i, c in enumerate(candidates[:6])]
        cand_hint = (
            "\n\nThis file is most likely ONE of these known titles. If the "
            "frame matches one, return its EXACT title and year. If the frame "
            "clearly matches none of them, return null.\n" + "\n".join(lines))

    def _run(args: list[str], tout: int = 15) -> Optional[bytes]:
        try:
            r = subprocess.run(args, capture_output=True, timeout=tout)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None

    def _duration() -> Optional[float]:
        return _video_duration_seconds(video_path)

    def _extract_frame(offset_sec: int) -> Optional[str]:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        _run([
            "ffmpeg", "-y", "-ss", str(offset_sec), "-i", video_path,
            "-vframes", "1", "-q:v", "3", "-f", "image2", tmp_path,
        ])
        try:
            with open(tmp_path, "rb") as f:
                raw = f.read()
            _os.unlink(tmp_path)
            return base64.b64encode(raw).decode() if raw else None
        except Exception:
            return None

    def _ask_vision(b64_image: str) -> Optional[dict[str, Any]]:
        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{
                "role": "user",
                "content": (
                    _VISION_SYSTEM
                    + "\n\nIdentify the movie or TV show in this frame."
                    + cand_hint
                ),
                "images": [b64_image],
            }],
        }
        try:
            resp = requests.post(base_url.rstrip("/") + "/api/chat",
                                 json=payload, timeout=timeout)
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = _normalize(json.loads(content))
            return result if result and result.get("title") else None
        except Exception as e:
            logger.debug("Ollama vision frame failed: %s", e)
            return None

    def _try(offset: int, tried: set) -> Optional[dict[str, Any]]:
        if offset in tried:
            return None
        tried.add(offset)
        b64 = _extract_frame(offset)
        if not b64:
            return None
        result = _ask_vision(b64)
        if result and result.get("title"):
            logger.debug("Vision identified at %ds: %s", offset, result.get("title"))
            return result
        return None

    dur = _duration()
    tried: set[int] = set()

    # ── Phase 1: priority timestamps ──────────────────────────────────────
    phase1 = list(_FRAME_PRIORITY)
    if dur and dur > 300:
        phase1.append(int(dur * 0.95))  # end credits

    for offset in phase1:
        if len(tried) >= _FRAME_MAX:
            break
        if dur and offset >= dur:
            continue
        result = _try(offset, tried)
        if result:
            return result

    # ── Phase 2: grid scan across the video ──────────────────────────────
    if dur:
        # Evenly-spaced samples at 10%–90% of duration, avoiding already tried.
        grid = [int(dur * p / (_FRAME_GRID_COUNT + 1))
                for p in range(1, _FRAME_GRID_COUNT + 1)]
    else:
        grid = list(_FRAME_GRID_FIXED)

    for offset in grid:
        if len(tried) >= _FRAME_MAX:
            break
        if dur and offset >= dur:
            continue
        result = _try(offset, tried)
        if result:
            return result

    logger.debug("Vision: no identification after %d frames for %s",
                 len(tried), video_path)
    return None


# ── Subtitle-based identification ──────────────────────────────────────────

_SUBTITLE_TIMEOUT = 25.0
_SUBTITLE_SYSTEM = (
    "You identify a movie or TV show from a short sample of its subtitle "
    "dialogue. Respond ONLY with a JSON object with keys: title (string or "
    "null), year (integer or null), type ('movie' or 'tv'), season (integer or "
    "null), episode (integer or null). When candidate titles are provided, pick "
    "the one whose plot/dialogue best fits, or null if none fit. Use null when "
    "you are not confident."
)


def _strip_srt(srt_text: str) -> list[str]:
    """Turn raw SRT into plain dialogue lines (drop indices/timestamps/markup)."""
    out: list[str] = []
    for line in srt_text.splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        s = re.sub(r"<[^>]+>", "", s)      # <i>, <b> styling
        s = re.sub(r"\{[^}]+\}", "", s)    # {\an8} ASS overrides
        s = s.strip()
        if s:
            out.append(s)
    return out


def _extract_subtitle_text(video_path: str, timeout: float = 30.0) -> Optional[str]:
    """Get subtitle text for a video: a sidecar .srt if present, else the first
    embedded text subtitle stream via ffmpeg. Image subs (PGS/VobSub) can't
    convert and return None. Entirely fail-safe."""
    import os as _os
    import shutil
    import subprocess

    base = _os.path.splitext(video_path)[0]
    for ext in (".srt", ".en.srt", ".eng.srt", ".english.srt"):
        p = base + ext
        try:
            if _os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
        except Exception:
            pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "quiet", "-i", video_path, "-map", "0:s:0",
             "-f", "srt", "-"],
            capture_output=True, timeout=timeout)
        if r.returncode == 0 and r.stdout:
            return r.stdout.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return None


def identify_from_subtitles(
    video_path: str, *, base_url: str, model: str,
    timeout: float = _SUBTITLE_TIMEOUT,
    candidates: Optional[List[dict]] = None,
) -> Optional[dict[str, Any]]:
    """Identify a title from its subtitle dialogue. Cheaper than vision (a text
    call, no frame extraction) and dialogue is highly identifying — so it runs
    as a rung ahead of the vision fallback. Returns None when there are no usable
    subtitles or on any error."""
    if not video_path or not base_url or not model:
        return None
    srt = _extract_subtitle_text(video_path)
    if not srt:
        return None
    lines = _strip_srt(srt)
    if len(lines) < 8:
        return None
    # Sample from the middle (skip intro/recap/credits boilerplate), then
    # thin to ~30 evenly-spaced lines to keep the prompt small.
    lo, hi = int(len(lines) * 0.15), int(len(lines) * 0.85)
    window = lines[lo:hi] or lines
    sample = window[:: max(1, len(window) // 30)][:30]
    user_content = "Subtitle dialogue:\n" + "\n".join(sample)
    if candidates:
        clines = [f"  {i + 1}. \"{c['title']}\" ({c.get('year') or '?'})"
                  for i, c in enumerate(candidates[:6])]
        user_content += "\n\nCandidate titles:\n" + "\n".join(clines)

    payload = {
        "model": model, "format": "json", "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _SUBTITLE_SYSTEM},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return _normalize(json.loads(content))
    except Exception as e:
        logger.debug("Ollama subtitle identify failed: %s", e)
        return None


# ── OCR-the-credits identification ─────────────────────────────────────────

_OCR_SYSTEM = (
    "You are given raw OCR text extracted from a video's title card and end "
    "credits. Identify the movie or TV show. Respond ONLY with a JSON object "
    "with keys: title (string or null), year (integer or null), type ('movie' "
    "or 'tv'), season (integer or null), episode (integer or null). When "
    "candidate titles are provided, pick one of them or null. Use null when "
    "the text is unreadable or matches none."
)


# HDR→SDR tone-map + downscale: HDR/DV frames pulled naively are dark and
# washed-out (PQ values mapped flat to SDR), which wrecks OCR. Downscaling to
# ~1280px also speeds tesseract up and de-noises huge 4K frames.
_TONEMAP_VF = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
               "tonemap=hable,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,"
               "scale=1280:-2")
_SCALE_VF = "scale=1280:-2"


def _is_hdr(video_path: str) -> bool:
    """True if the primary video stream signals an HDR transfer (PQ or HLG)."""
    import shutil
    import subprocess
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=nw=1:nk=1", video_path],
            capture_output=True, text=True, timeout=15)
        return r.stdout.strip().lower() in ("smpte2084", "arib-std-b67")
    except Exception:
        return False


def _ocr_frame(video_path: str, offset_sec: int, vf: Optional[str] = None,
               timeout: int = 25) -> str:
    """Grab one frame at ``offset_sec`` (optionally through filter ``vf``) and
    OCR it with tesseract. Returns the recognized text (possibly empty).
    Entirely fail-safe."""
    import os as _os
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    tess = shutil.which("tesseract")
    if not ffmpeg or not tess:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img = tmp.name
    try:
        # Two-stage seek: jump fast to a keyframe ~5s before the target, then
        # decode accurately to the exact second. Plain input-seek on long-GOP
        # HDR files lands on a different frame run-to-run, which makes the OCR
        # (and any cast match) non-reproducible; this pins it down.
        pre = max(0, offset_sec - 5)
        args = [ffmpeg, "-y", "-ss", str(pre), "-i", video_path,
                "-ss", str(offset_sec - pre)]
        if vf:
            args += ["-vf", vf]
        args += ["-frames:v", "1", img]
        subprocess.run(args, capture_output=True, timeout=timeout)
        r = subprocess.run([tess, img, "-", "--psm", "6"],
                           capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", errors="ignore") if r.returncode == 0 else ""
    except Exception:
        return ""
    finally:
        try:
            _os.unlink(img)
        except Exception:
            pass


def _norm_ocr(text: str) -> str:
    """Lower-case, keep alphanumerics+spaces, collapse runs — for title hunts."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _name_match_kind(name: str, norm_text: str) -> Optional[str]:
    """How a person's name appears in the OCR text: "full" (both tokens, in
    order — e.g. "adam sandler"), "surname" (distinctive last name only,
    whole-word), or None. Surname-only is noise-prone on crew lists, so callers
    treat "full" as a strong signal and "surname" as weak."""
    n = _norm_ocr(name)
    if not n:
        return None
    parts = n.split()
    if len(parts) >= 2 and n in norm_text:
        return "full"
    surname = parts[-1] if parts else ""
    if len(surname) >= 4 and f" {surname} " in f" {norm_text} ":
        return "surname"
    return None


def _name_in_text(name: str, norm_text: str) -> bool:
    """True if a person's name appears in the OCR text at all (full or surname)."""
    return _name_match_kind(name, norm_text) is not None


def _match_people(norm_text: str, candidates: list) -> Optional[dict]:
    """Pick the candidate whose cast/director appear most in the OCR'd credits.
    Deterministic and guarded: the winner needs >=2 distinct people AND must
    strictly out-rank the runner-up, so a single shared/common surname or a tie
    never matches. (Lead actors are routinely credited by surname only — e.g.
    "MR. SANDLER" / "MS. BARRYMORE" — so a full-name requirement would miss real
    matches; the >=2-distinct-plus-clear-winner rule is what holds.) Returns the
    chosen candidate, or None. Reproducible frame extraction upstream keeps a
    stray OCR misread from coincidentally clearing the bar."""
    scored = []
    for c in candidates:
        hits = set()
        for name in (c.get("cast") or []):
            if _name_in_text(name, norm_text):
                hits.add(_norm_ocr(name))
        director = c.get("director")
        if director and _name_in_text(director, norm_text):
            hits.add("director:" + _norm_ocr(director))
        scored.append((len(hits), c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None
    top_n, top_c = scored[0]
    second_n = scored[1][0] if len(scored) > 1 else 0
    if top_n >= 2 and top_n > second_n:
        return {"title": top_c["title"], "year": top_c.get("year"),
                "media_type": top_c.get("media_type") or "movie",
                "season": None, "episode": None}
    return None


def identify_from_credits_ocr(
    video_path: str, *, base_url: str, model: str,
    timeout: float = _SUBTITLE_TIMEOUT,
    candidates: Optional[List[dict]] = None,
) -> Optional[dict[str, Any]]:
    """OCR the title card and end credits and identify the title. A candidate
    title that appears verbatim on-screen is decisive (returned immediately);
    otherwise the OCR text + candidates go to the text model. Faster than the
    vision model (tesseract, not minutes-long inference). Returns None when
    tesseract is unavailable, nothing readable is found, or on any error."""
    import shutil

    if not video_path or not shutil.which("tesseract"):
        return None
    dur = _video_duration_seconds(video_path)
    vf = _TONEMAP_VF if _is_hdr(video_path) else _SCALE_VF

    # Where the identifying text lives: the opening title sequence, and — most
    # reliably — the principal-cast cards that cluster in a narrow band near the
    # start of the end credits (~90-97%). Sample that band densely; a wide step
    # walks right over the one frame that names the cast. Ordered highest-yield
    # first, since the incremental checks below exit as soon as one matches.
    if dur and dur > 300:
        # Cast cards LEAD the end credits (cast → crew → music → legal), in a
        # band roughly 90-97% of runtime. Sample it finely — a single frame can
        # land between cards or on a fade — then the opening title sequence.
        band = [int(dur * p) for p in
                (0.945, 0.93, 0.96, 0.915, 0.975, 0.90, 0.99)]
        offsets = band[:6] + [60, 90, 45] + band[6:]
    else:
        offsets = [30, 45, 60, 90, 120, 150, 210]

    # Pre-normalize candidate titles once for the verbatim on-screen check.
    cand_norm = [(c, _norm_ocr(c.get("title") or "")) for c in (candidates or [])]

    text = ""
    seen: set[int] = set()
    for off in offsets:
        off = int(off)
        if off in seen or (dur and off >= dur):
            continue
        seen.add(off)
        frame_text = _ocr_frame(video_path, off, vf=vf)
        if not frame_text:
            continue
        text += "\n" + frame_text
        norm = _norm_ocr(text)
        # Decisive #1 — a candidate title literally on-screen.
        for c, t in cand_norm:
            if t and len(t) >= 4 and t in norm:
                return {"title": c["title"], "year": c.get("year"),
                        "media_type": c.get("media_type") or "movie",
                        "season": None, "episode": None}
        # Decisive #2 — a candidate's cast/director printed in the credits. The
        # title may never appear on-screen, but the people always do. Checked
        # incrementally so dense sampling exits the moment the guard is met.
        people = _match_people(norm, candidates or [])
        if people:
            return people
        if len(text) > 8000:
            break

    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 12:
        return None

    # Otherwise have the text model choose from candidates using the OCR text.
    if not base_url or not model:
        return None
    user_content = "OCR text from title card / credits:\n" + text[:4000]
    if candidates:
        clines = [f"  {i + 1}. \"{c['title']}\" ({c.get('year') or '?'})"
                  for i, c in enumerate(candidates[:6])]
        user_content += "\n\nCandidate titles:\n" + "\n".join(clines)
    payload = {
        "model": model, "format": "json", "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _OCR_SYSTEM},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
        return _normalize(json.loads(content))
    except Exception as e:
        logger.debug("Ollama OCR identify failed: %s", e)
        return None


def dependency_status() -> dict:
    """Report whether the external binaries the rename fallbacks rely on are
    installed, so silent dependency loss (e.g. a rebuild dropping tesseract) is
    diagnosable rather than just degrading quietly."""
    import shutil
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "tesseract": bool(shutil.which("tesseract")),
    }


def test_connection(base_url: str, timeout: float = 5.0) -> dict:
    """Probe Ollama's ``/api/tags``. Returns ``{ok, models?, error?}``."""
    if not base_url:
        return {"ok": False, "error": "No base URL configured"}
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        resp.raise_for_status()
        return {"ok": True, "models": [m.get("name") for m in resp.json().get("models", [])]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Episode disambiguator ─────────────────────────────────────────────────

_DISAMBIG_SYSTEM = (
    "You identify which TV episode a media file most likely corresponds to. "
    "Given a filename and 2-3 candidate episodes, pick the best match. "
    "Respond ONLY with JSON: {\"episode\": <number>, \"season\": <number>}"
)


def disambiguate_episode(
    filename: str,
    candidates: list,
    *,
    base_url: str,
    model: str,
    timeout: float = _TIMEOUT,
) -> Optional[dict]:
    """Choose between close episode candidates using the Ollama chat API.

    Returns ``{episode: int, season: int}`` or ``None`` on any failure.
    """
    if not filename or not base_url or not model or len(candidates) < 2:
        return None
    lines = [
        f"  {i + 1}. S{c['season']:02d}E{c['episode']:02d} "
        f"\"{c.get('title', '')}\" ({c.get('runtime', '?')}min)"
        for i, c in enumerate(candidates[:3])
    ]
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _DISAMBIG_SYSTEM},
            {"role": "user", "content": (
                f"Filename: {filename}\nCandidates:\n"
                + "\n".join(lines)
                + "\nWhich episode does this file most likely contain?"
            )},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=timeout)
        resp.raise_for_status()
        data = json.loads(resp.json().get("message", {}).get("content", "{}"))
        ep = int(data.get("episode", 0))
        sn = int(data.get("season", 0))
        if ep > 0 and sn > 0:
            return {"episode": ep, "season": sn}
    except Exception as e:
        logger.debug("Ollama episode disambiguate failed: %s", e)
    return None


# ── Page hint extraction ──────────────────────────────────────────────────

_HINT_COMBINED_RE = re.compile(
    r'\b(?:double[\s\-]?episode|2[\s\-]in[\s\-]1|two[\s\-](?:part|episode)s?'
    r'|episodes?\s+\d+\s*[&+]\s*\d+|multi[\s\-]?episode)\b',
    re.IGNORECASE,
)
_HINT_SPLIT_RE = re.compile(
    r'\bpart\s*[12]\b|\bpt\.?\s*[12]\b|\b[12]\s+of\s+2\b',
    re.IGNORECASE,
)
_HINT_PART_NUM_RE = re.compile(
    r'\bpart\s*(\d)\b|\bpt\.?\s*(\d)\b|\b(\d)\s+of\s+\d\b',
    re.IGNORECASE,
)
_HINT_EP_COUNT_RE = re.compile(r'\b(\d+)[\s\-]?(?:episodes?|eps?)\b', re.IGNORECASE)

_HINT_SYSTEM = (
    "Extract multi-episode metadata from media download page text. "
    "Respond ONLY with JSON: "
    "{\"is_combined\": bool, \"is_split\": bool, "
    "\"part_number\": int_or_null, \"episode_count\": int_or_null}"
)


def extract_page_hints(
    page_text: str,
    *,
    base_url: str = "",
    model: str = "",
) -> dict:
    """Extract combined/split episode hints from download page text.

    Tries Ollama first when configured; falls back to regex when Ollama is
    unconfigured or fails.  Always returns a dict with all four keys.
    """
    empty: dict = {
        "is_combined": False, "is_split": False,
        "part_number": None, "episode_count": None,
    }
    if not page_text:
        return empty
    if base_url and model:
        ollama_result = _ollama_page_hints(page_text[:3000],
                                           base_url=base_url, model=model)
        if ollama_result:
            return ollama_result
    return _regex_page_hints(page_text)


def _ollama_page_hints(text: str, *, base_url: str, model: str) -> Optional[dict]:
    payload = {
        "model": model,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _HINT_SYSTEM},
            {"role": "user", "content": f"Extract episode info from:\n{text}"},
        ],
    }
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/chat",
                             json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = json.loads(resp.json().get("message", {}).get("content", "{}"))
        ep_count = data.get("episode_count")
        part_num = data.get("part_number")
        return {
            "is_combined": bool(data.get("is_combined", False)),
            "is_split": bool(data.get("is_split", False)),
            "part_number": int(part_num) if part_num else None,
            "episode_count": int(ep_count) if ep_count else None,
        }
    except Exception as e:
        logger.debug("Ollama page hint extraction failed: %s", e)
        return None


def _regex_page_hints(text: str) -> dict:
    is_combined = bool(_HINT_COMBINED_RE.search(text))
    is_split = bool(_HINT_SPLIT_RE.search(text))
    part_number: Optional[int] = None
    episode_count: Optional[int] = None

    pm = _HINT_PART_NUM_RE.search(text)
    if pm:
        part_number = int(next(g for g in pm.groups() if g is not None))
        is_split = True

    em = _HINT_EP_COUNT_RE.search(text)
    if em and int(em.group(1)) > 1:
        episode_count = int(em.group(1))
        if episode_count == 2:
            is_combined = True

    return {
        "is_combined": is_combined,
        "is_split": is_split,
        "part_number": part_number,
        "episode_count": episode_count,
    }

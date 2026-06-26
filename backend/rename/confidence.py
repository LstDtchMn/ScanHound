"""String-similarity confidence scoring for media identification.

Ported/adapted from Nomen's ``similarity.py`` (Dice bigram + word-overlap),
plus a combined 0–100 score tuned for matching a parsed release title against a
metadata-provider candidate. Pure functions — no I/O, fully unit-testable.
"""
from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_LEN = 10_000


def _bigrams(s: str) -> list[str]:
    s = s.lower()
    return [s[i:i + 2] for i in range(len(s) - 1)]


def dice_similarity(a: str, b: str) -> float:
    """Dice coefficient over character bigrams. Returns 0.0–1.0."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return 0.0
    a, b = a[:_MAX_LEN], b[:_MAX_LEN]
    bg_a, bg_b = _bigrams(a), _bigrams(b)
    if not bg_a or not bg_b:
        return 1.0 if a.lower() == b.lower() else 0.0
    counts_a: dict[str, int] = {}
    for bg in bg_a:
        counts_a[bg] = counts_a.get(bg, 0) + 1
    counts_b: dict[str, int] = {}
    for bg in bg_b:
        counts_b[bg] = counts_b.get(bg, 0) + 1
    overlap = sum(min(c, counts_b.get(bg, 0)) for bg, c in counts_a.items())
    return (2.0 * overlap) / (len(bg_a) + len(bg_b))


def word_overlap_similarity(a: str, b: str) -> float:
    """Word-level Jaccard similarity (case-insensitive). Returns 0.0–1.0."""
    if not isinstance(a, str) or not isinstance(b, str):
        return 0.0
    wa = set(_WORD_RE.findall(a.lower()))
    wb = set(_WORD_RE.findall(b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def title_similarity(a: str, b: str) -> float:
    """Best of Dice and word-overlap similarity (0.0–1.0)."""
    return max(dice_similarity(a, b), word_overlap_similarity(a, b))


def match_confidence(parsed_title, candidate_title,
                     parsed_year=None, candidate_year=None) -> float:
    """Confidence (0–100) that a parsed release matches a provider candidate.

    Title similarity is the core signal; a matching year nudges it up and a
    clearly conflicting year pulls it down, so a remake isn't mistaken for the
    original.
    """
    score = title_similarity(parsed_title or "", candidate_title or "") * 100.0
    try:
        if parsed_year and candidate_year:
            if int(parsed_year) == int(candidate_year):
                score = min(100.0, score + 8.0)
            elif abs(int(parsed_year) - int(candidate_year)) > 1:
                score = max(0.0, score - 25.0)
    except (TypeError, ValueError):
        pass
    return round(score, 1)


def runtime_confidence_delta(file_minutes: float, tmdb_minutes: float) -> float:
    """Confidence adjustment (positive or negative) from a runtime comparison.

    Uses percentage deviation rather than flat minute thresholds so that the
    same absolute gap has a proportional effect — a 5-min variance on a 22-min
    episode (22% off) is very different from the same gap on a 180-min movie
    (2.8% off).  Returns 0.0 when either value is missing or invalid.

    Deviation from TMDB runtime → adjustment:
        ≤  3%  → +10  (near-exact: strong confirmation)
        ≤  8%  →  +5  (close: likely correct with encode/credits padding)
        ≤ 15%  →   0  (neutral: within plausible variance)
        ≤ 30%  → -10  (suspicious mismatch)
        ≤ 50%  → -20  (likely wrong content)
        >  50%  → -30  (almost certainly misidentified)
    """
    try:
        if not file_minutes or not tmdb_minutes or float(tmdb_minutes) <= 0:
            return 0.0
        pct = abs(float(file_minutes) - float(tmdb_minutes)) / float(tmdb_minutes)
    except (TypeError, ValueError):
        return 0.0

    if pct <= 0.03:
        return 10.0
    if pct <= 0.08:
        return 5.0
    if pct <= 0.10:
        return 0.0
    if pct <= 0.30:
        return -10.0
    if pct <= 0.50:
        return -20.0
    return -30.0


def filesize_plausibility_delta(file_bytes: int, tmdb_minutes: float,
                                resolution: str | None = None) -> float:
    """Confidence adjustment from file-size / runtime plausibility.

    Fallback used when ffprobe is unavailable.  Computes GB/min and checks
    whether the ratio is in the expected range for the detected resolution.
    Absurdly small files (stubs, samples) and absurdly large ones are penalised.

    GB/min ranges (from Nomen's calibrated values):
        2160p  0.08 – 1.30  (4.8 – 78 GB/hr)
        1080p  0.025 – 0.65  (1.5 – 39 GB/hr)
        720p   0.012 – 0.30  (0.7 – 18 GB/hr)
    """
    try:
        if not file_bytes or not tmdb_minutes or float(tmdb_minutes) <= 0:
            return 0.0
        gb_per_min = (float(file_bytes) / (1024 ** 3)) / float(tmdb_minutes)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0

    ranges = {
        "2160p": (0.08, 1.30),
        "4k":    (0.08, 1.30),
        "1080p": (0.025, 0.65),
        "720p":  (0.012, 0.30),
    }
    lo, hi = ranges.get((resolution or "1080p").lower(), (0.025, 0.65))

    if lo <= gb_per_min <= hi:
        return 5.0
    if gb_per_min < lo * 0.1 or gb_per_min > hi * 5:
        return -25.0   # stub / obviously wrong file
    if gb_per_min < lo * 0.5 or gb_per_min > hi * 2:
        return -10.0
    return 0.0


def episode_correction_candidates(
    file_minutes: float,
    episodes: list,
    current_episode: int,
    *,
    search_radius: int = 3,
    min_gain: float = 15.0,
) -> list:
    # Score TMDB episodes near current_episode by runtime fit against file_minutes.
    current_score: float | None = None
    candidates: list[tuple[int, float]] = []

    for ep in episodes:
        ep_num = ep.get("episode_number")
        ep_rt = ep.get("runtime")
        if not ep_num or not ep_rt:
            continue
        if abs(ep_num - current_episode) > search_radius:
            continue
        score = runtime_confidence_delta(float(file_minutes), float(ep_rt))
        if ep_num == current_episode:
            current_score = score
        else:
            candidates.append((ep_num, score))

    if current_score is None:
        return []

    better = [(n, s) for n, s in candidates if s - current_score >= min_gain]
    return sorted(better, key=lambda x: x[1], reverse=True)

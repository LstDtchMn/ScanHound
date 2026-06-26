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


def runtime_confidence_delta(file_seconds: float, tmdb_minutes: int) -> float:
    """Confidence adjustment (positive or negative) from a runtime comparison.

    A large gap between the actual file length and the TMDB-reported runtime
    is strong evidence of a wrong match — a 2-hour movie cannot be a 45-minute
    episode.  Returns 0.0 when either value is missing or invalid.

    Thresholds (difference in minutes):
        ≤  5 min  → +10  (near-exact, solid confirmation)
        ≤ 15 min  →   0  (within acceptable encode/credits variance)
        ≤ 30 min  → -10  (noticeable gap — mild suspicion)
        ≤ 60 min  → -20  (major gap — likely wrong match)
        >  60 min → -35  (wildly off — almost certainly wrong)
    """
    try:
        if not file_seconds or not tmdb_minutes or int(tmdb_minutes) <= 0:
            return 0.0
        diff = abs(file_seconds / 60.0 - float(tmdb_minutes))
    except (TypeError, ValueError):
        return 0.0

    if diff <= 5:
        return 10.0
    if diff <= 15:
        return 0.0
    if diff <= 30:
        return -10.0
    if diff <= 60:
        return -20.0
    return -35.0

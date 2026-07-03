"""Destination-conflict + release-quality helpers.

Pure, DB-free, self-free logic extracted from ``rename/service.py`` (C5
decomposition): given a list of job dicts (as returned by
``DatabaseManager.list_rename_jobs`` or similar), rank competing releases by
quality and flag duplicate-destination conflicts. No I/O, no ``RenameService``
coupling — these operate purely on the dicts passed in, which is what makes
them unit-testable without a DB and safe to import from route handlers.
"""
from __future__ import annotations

import re as _re
from typing import Optional

# Active candidates: a file WILL be placed at this destination if applied.
_ACTIVE_STATUSES = frozenset({"pending", "matched", "needs_review"})
# Statuses that occupy/claim a destination on disk. ``applied`` is included
# because the file is already there — a new candidate landing on the same path
# is a real conflict (its apply would collide). ``failed``/``reverted`` released
# the slot and are excluded.
_CLAIMING_STATUSES = _ACTIVE_STATUSES | frozenset({"applied"})

# Dolby Vision tag, excluding the camera/rip formats "DV.Cam" / "dv-rip" whose
# dot/dash makes a naive \bdv\b match. Negative lookahead drops cam/rip suffixes.
_DV_RE = _re.compile(r"\b(?:dovi|dolby[.\s-]?vision)\b|\bdv\b(?![.\s_-]?(?:cam|rip))",
                     _re.IGNORECASE)


def _dest_key(job: dict) -> Optional[str]:
    """Normalized full destination (dir + filename) for a job, or None if it has
    no target yet. Case-folded with normalized separators so the comparison is
    robust across the NTFS-backed (case-insensitive) library mounts."""
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or ""
    if not name and not dest:
        return None
    full = f"{dest}/{name}" if dest else name
    return full.replace("\\", "/").casefold()


def _quality_score(job: dict) -> tuple:
    """Rank a release by desirability from its filename + parsed resolution, so a
    duplicate-target group can recommend which copy to keep. Higher is better.

    Returns a comparable tuple (so ties fall through to the next signal):
        (resolution_rank, dolby_vision, hdr, source_rank, audio_rank, edition)
    Pure string heuristics over the original filename — no I/O."""
    name = str(job.get("original_filename") or "").lower()

    res = str(job.get("resolution") or "").lower()
    if not res:  # fall back to filename if the column wasn't set
        for r in ("2160p", "1080p", "720p", "480p"):
            if r in name:
                res = r
                break
        if "2160" not in res and ("4k" in name or "uhd" in name):
            res = "2160p"
    res_rank = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(res, 0)

    # Dolby Vision is the headline upgrade (esp. for this user). Match DoVi /
    # "dolby vision" / a standalone DV tag — but NOT the camera/rip formats
    # "DV.Cam" / "dv-rip" (the dot/dash is a word boundary, so a naive \bdv\b
    # matches those false positives).
    dv = 1 if _re.search(_DV_RE, name) else 0
    hdr = 1 if _re.search(r"\bhdr(10)?(\+|plus)?\b|\bhlg\b", name) else 0

    if _re.search(r"\bremux\b", name):
        source = 4
    elif _re.search(r"\b(blu[.\s-]?ray|bdrip|bd(25|50|66|100))\b", name):
        source = 3
    elif _re.search(r"\bweb[.\s-]?dl\b", name):
        source = 2
    elif _re.search(r"\bweb[.\s-]?rip\b|\bhdtv\b", name):
        source = 1
    else:
        source = 0

    if _re.search(r"\b(truehd|atmos)\b", name):
        audio = 3
    elif _re.search(r"\bdts[.\s-]?hd\b|\bdts[.\s-]?x\b", name):
        audio = 2
    elif _re.search(r"\b(ddp|eac3|dd\+)\b", name):
        audio = 1
    else:
        audio = 0

    edition = 1 if _re.search(r"\b(imax|extended|uncut|remastered|criterion)\b", name) else 0

    return (res_rank, dv, hdr, source, audio, edition)


def _quality_reason(job: dict) -> str:
    """Short human reason a release was picked as the keeper (its standout tags)."""
    name = (job.get("original_filename") or "").lower()
    bits = []
    if job.get("resolution"):
        bits.append(job["resolution"])
    elif "2160p" in name or "4k" in name or "uhd" in name:
        bits.append("2160p")
    if _re.search(_DV_RE, name):
        bits.append("Dolby Vision")
    if _re.search(r"\bhdr(10)?(\+|plus)?\b", name):
        bits.append("HDR")
    if _re.search(r"\bremux\b", name):
        bits.append("Remux")
    elif _re.search(r"\bblu[.\s-]?ray\b", name):
        bits.append("BluRay")
    if _re.search(r"\b(truehd|atmos)\b", name):
        bits.append("Atmos/TrueHD")
    return " · ".join(bits)


def recommend_keep(group) -> Optional[int]:
    """Given the jobs of one duplicate-target group, return the id of the release
    to keep (best quality), or None if it's a tie at the top or input is empty."""
    ranked = [(j, _quality_score(j)) for j in group if j.get("id") is not None]
    if len(ranked) < 2:
        return ranked[0][0]["id"] if ranked else None
    ranked.sort(key=lambda t: t[1], reverse=True)
    if ranked[0][1] == ranked[1][1]:
        return None  # genuine tie — no clear winner, let the human choose
    return ranked[0][0]["id"]


def destination_conflict_ids(jobs) -> set:
    """Return the ids of ACTIVE jobs whose destination is also claimed by another
    job (active or already applied) — i.e. a duplicate-target conflict such as two
    releases of one movie, or a new grab landing on a file already in the library.

    A settled ``applied`` job counts toward a destination's claim (so its active
    rival is flagged) but is never itself flagged — it's done. Pure and
    DB-free, so it drives the jobs-list 'duplicate' badge and unit-tests cleanly."""
    by_key: dict = {}
    for j in jobs:
        if j.get("status") not in _CLAIMING_STATUSES:
            continue
        k = _dest_key(j)
        if not k:
            continue
        by_key.setdefault(k, []).append(j)
    out: set = set()
    for group in by_key.values():
        if len(group) < 2:
            continue
        for j in group:
            if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None:
                out.add(j.get("id"))
    return out


def conflict_annotations(jobs) -> dict:
    """Map job-id -> {destination_conflict, keep_recommended, keep_reason} for the
    jobs list. For each duplicate-target group with an active conflict, flags all
    active members and marks the single best-quality active release as the
    recommended keeper (with a short reason). Pure, DB-free."""
    by_key: dict = {}
    for j in jobs:
        if j.get("status") not in _CLAIMING_STATUSES:
            continue
        k = _dest_key(j)
        if not k:
            continue
        by_key.setdefault(k, []).append(j)
    out: dict = {}
    for group in by_key.values():
        active = [j for j in group
                  if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None]
        if len(group) < 2 or not active:
            continue
        # Recommend over the WHOLE group (incl. applied): if an already-applied
        # copy is the best, keep_id is its id — which isn't in `active`, so no
        # active rival is wrongly flagged "keep" over the better library copy.
        keep_id = recommend_keep(group)
        for j in active:
            ann = out.setdefault(j["id"], {"destination_conflict": True,
                                           "keep_recommended": False,
                                           "keep_reason": None})
            if keep_id is not None and j["id"] == keep_id:
                ann["keep_recommended"] = True
                ann["keep_reason"] = _quality_reason(j) or None
    return out

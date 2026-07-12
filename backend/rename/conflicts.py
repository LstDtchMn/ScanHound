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

# Ranks for DV layer names as returned by dovi_tool. "fel" is the ceiling (3).
_DV_LAYER_RANK = {"fel": 3, "mel": 2, "profile8": 1, "profile5": 1}


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


def _identity_key(job: dict) -> Optional[tuple]:
    """Movie-*identity* signal for a job — what film it IS, independent of the
    raw destination path a mid-session ``auto_rename_movie_flat`` toggle happened
    to freeze onto (nested ``Title (Year)/`` subfolder vs flat library root).

    imdb_id is authoritative; else normalized title + year; else None (no
    identity signal — group on path alone, the historical behavior). TV jobs
    return None (movies-only, matching ``find_library_duplicate``'s own scope).

    Pure and DB-free. ``normalize_title`` is imported locally (as
    ``find_library_duplicate`` does) to avoid a module-level import cycle."""
    if (job.get("media_type") or "movie") == "tv":
        return None
    imdb_id = job.get("imdb_id")
    if imdb_id:
        return ("imdb", imdb_id)
    from backend.app_service import normalize_title
    title = normalize_title(job.get("title") or "")
    if title:
        return ("title", title, job.get("year"))
    return None


def _conflict_groups(jobs) -> list:
    """Cluster the claiming jobs into conflict groups. Two jobs share a group if
    they share EITHER a destination path (:func:`_dest_key`) OR a movie identity
    (:func:`_identity_key`) — so a duplicate is caught whether the two copies
    collide on the same path OR are the same film frozen onto two different
    paths. Returns a list of job-dict lists.

    Pure in-memory union-find over the (small) jobs list — no I/O. Path grouping
    (the historical behavior) is one of the two keys, so this only ever ADDS
    connections, never drops one; every job appears in exactly one group."""
    claiming = [j for j in jobs if j.get("status") in _CLAIMING_STATUSES]
    parent = list(range(len(claiming)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for key_fn in (_dest_key, _identity_key):
        first_seen: dict = {}
        for idx, j in enumerate(claiming):
            k = key_fn(j)
            if k is None:
                continue
            if k in first_seen:
                union(idx, first_seen[k])
            else:
                first_seen[k] = idx

    groups: dict = {}
    for idx in range(len(claiming)):
        groups.setdefault(find(idx), []).append(claiming[idx])
    return list(groups.values())


def _quality_score(job: dict) -> tuple:
    """Rank a release by desirability from its filename + parsed resolution, so a
    duplicate-target group can recommend which copy to keep. Higher is better.

    Returns a comparable tuple (so ties fall through to the next signal):
        (resolution_rank, dolby_vision, dv_layer_rank, hdr, source_rank, audio_rank, edition)
    Pure string heuristics over the original filename — no I/O.

    Note on tuple placement: `dv_layer_rank` sits right after the binary `dv` bit
    rather than before it (as a naive reading of "ranks above the binary DV bit"
    might suggest) so that index 1 stays the `dv` bit — preserving an existing
    regression test that indexes into it directly. This is a no-op behaviorally:
    dv_layer_rank > 0 always forces dv = 1 (see below), so the two orderings are
    mathematically equivalent for every comparison — the achievable (dv,
    dv_layer_rank) pairs form a single total-ordered chain either way:
    (0,0) < (1,0) < (1,1) < (1,2) < (1,3)."""
    name = str(job.get("original_filename") or "").lower()

    res = str(job.get("resolution") or "").lower()
    if not res:  # fall back to filename if the column wasn't set
        for r in ("2160p", "1440p", "1080p", "720p", "480p"):
            if r in name:
                res = r
                break
        if "2160" not in res and ("4k" in name or "uhd" in name):
            res = "2160p"
    res_rank = {"2160p": 5, "1440p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(res, 0)

    # Explicit probed DV layer (from probe_specs) outranks the binary filename DV
    # bit; absent → 0 so pure-filename callers are unchanged.
    dv_layer_rank = _DV_LAYER_RANK.get(str(job.get("dv_layer") or "").lower(), 0)

    # Dolby Vision is the headline upgrade (esp. for this user). Match DoVi /
    # "dolby vision" / a standalone DV tag — but NOT the camera/rip formats
    # "DV.Cam" / "dv-rip" (the dot/dash is a word boundary, so a naive \bdv\b
    # matches those false positives).
    dv = 1 if _re.search(_DV_RE, name) else 0
    # An explicit DV layer also implies the binary DV bit for filename-only rivals.
    if dv_layer_rank:
        dv = 1
    explicit_hdr = job.get("hdr")
    # A probed hdr of "Dolby Vision" (ffprobe DOVI side_data) is real DV even
    # when the file hasn't been dovi_tool-scanned yet (no cached dv_layer) —
    # force the bit so an uncached-but-genuine-DV library file isn't outranked
    # by a tag-rich but lower-quality rival on the dv bit alone.
    if explicit_hdr == "Dolby Vision":
        dv = 1
    # HDR tier is probed-data-first: an explicit probed "HDR10+" (ffprobe
    # SMPTE2094-40 side_data) is the top tier (2); any other explicit hdr —
    # including "Dolby Vision", whose own precedence is carried by the separate
    # dv/dv_layer_rank fields ahead of this — stays tier 1, never double-counted.
    # With no probed hdr, fall back to the exact filename-regex logic, with an
    # HDR10+/HDR10plus filename tag now also promoted to tier 2 (checked first,
    # since the generic regex below matches "hdr10+" as plain HDR10 otherwise).
    if explicit_hdr == "HDR10+":
        hdr = 2
    elif explicit_hdr:
        hdr = 1
    elif _re.search(r"\bhdr10\+|\bhdr10plus\b", name):
        hdr = 2
    elif _re.search(r"\bhdr(10)?(\+|plus)?\b|\bhlg\b", name):
        hdr = 1
    else:
        hdr = 0

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

    # Audio tier is probed-data-first: a probed audio_profile (from probe_specs —
    # e.g. "TrueHD 7.1 Atmos", "DTS-HD MA 5.1") decides the tier when present, so a
    # tag-stripped library file whose codec-buried Atmos/DTS-HD only ffprobe can
    # see isn't beaten by a filename. With no probed profile, fall back to the
    # exact filename-regex logic (byte-identical to prior behavior).
    audio_profile = str(job.get("audio_profile") or "").lower()
    if audio_profile:
        if "atmos" in audio_profile or "truehd" in audio_profile:
            audio = 3
        elif "dts-hd" in audio_profile or "dts:x" in audio_profile or "dts hd" in audio_profile:
            audio = 2
        elif "ddp" in audio_profile or "eac3" in audio_profile or "dd+" in audio_profile:
            audio = 1
        else:
            audio = 0
    elif _re.search(r"\b(truehd|atmos)\b", name):
        audio = 3
    elif _re.search(r"\bdts[.\s-]?hd\b|\bdts[.\s-]?x\b", name):
        audio = 2
    elif _re.search(r"\b(ddp|eac3|dd\+)\b", name):
        audio = 1
    else:
        audio = 0

    edition = 1 if _re.search(r"\b(imax|extended|uncut|remastered|criterion)\b", name) else 0

    return (res_rank, dv, dv_layer_rank, hdr, source, audio, edition)


def _quality_reason(job: dict) -> str:
    """Short human reason a release was picked as the keeper (its standout tags)."""
    name = (job.get("original_filename") or "").lower()
    bits = []
    if job.get("resolution"):
        bits.append(job["resolution"])
    elif "2160p" in name or "4k" in name or "uhd" in name:
        bits.append("2160p")
    if job.get("dv_layer") or _re.search(_DV_RE, name):
        bits.append("Dolby Vision")
    elif job.get("hdr"):
        bits.append(job["hdr"])
    elif _re.search(r"\bhdr(10)?(\+|plus)?\b", name):
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
    """Return the ids of ACTIVE jobs that conflict with another claiming job —
    a duplicate-target conflict. Two jobs conflict when they share a destination
    path OR the same movie identity (imdb / title+year), so this catches both a
    same-path collision (two releases landing on one path, or a new grab hitting
    a library file) AND the same film frozen onto two DIFFERENT paths by a
    mid-session ``auto_rename_movie_flat`` toggle.

    A settled ``applied`` job counts toward a claim (so its active rival is
    flagged) but is never itself flagged — it's done. Pure and DB-free, so it
    drives the jobs-list 'duplicate' badge and unit-tests cleanly."""
    out: set = set()
    for group in _conflict_groups(jobs):
        if len(group) < 2:
            continue
        for j in group:
            if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None:
                out.add(j.get("id"))
    return out


def conflict_annotations(jobs) -> dict:
    """Map job-id -> {destination_conflict, keep_recommended, keep_reason} for the
    jobs list. For each conflict group with an active member, flags all active
    members with ``destination_conflict`` and — only when the group is genuinely
    the SAME film — marks the single best-quality active release as the
    recommended keeper (with a short reason). Pure, DB-free.

    A group that shares only a raw-path collision across DIFFERENT identities
    (e.g. two unrelated films rendered to one flat path by a ``{{year}}``-less
    custom template) is a real filesystem conflict a human must resolve, so it's
    still flagged ``destination_conflict`` — but same-movie quality guidance
    (``recommend_keep``) is NOT applied across two different movies. Distinct
    non-None identities ⇒ not the same movie; all-None (no identity signal at
    all) falls back to the historical path-only behavior and still recommends."""
    out: dict = {}
    for group in _conflict_groups(jobs):
        active = [j for j in group
                  if j.get("status") in _ACTIVE_STATUSES and j.get("id") is not None]
        if len(group) < 2 or not active:
            continue
        identities = {_identity_key(j) for j in group}
        identities.discard(None)
        same_movie = len(identities) <= 1
        # Recommend over the WHOLE group (incl. applied): if an already-applied
        # copy is the best, keep_id is its id — which isn't in `active`, so no
        # active rival is wrongly flagged "keep" over the better library copy.
        # Suppressed entirely for a cross-identity path collision (different
        # films) — those get the filesystem-conflict flag but no keeper.
        keep_id = recommend_keep(group) if same_movie else None
        for j in active:
            ann = out.setdefault(j["id"], {"destination_conflict": True,
                                           "keep_recommended": False,
                                           "keep_reason": None})
            if keep_id is not None and j["id"] == keep_id:
                ann["keep_recommended"] = True
                ann["keep_reason"] = _quality_reason(j) or None
    return out


def rank_conflict(existing: Optional[dict], incoming: dict) -> dict:
    """Recommend which of an existing on-disk file vs an incoming release to keep,
    judging on explicit probed spec fields when present (so a tag-stripped library
    file isn't unfairly beaten by a tag-rich lower-quality release). Returns
    {recommended: 'existing'|'incoming'|'tie'|None, reason: str|None}."""
    if not existing or existing.get("present") is False:
        return {"recommended": "incoming", "reason": _quality_reason(incoming) or None}
    se, si = _quality_score(existing), _quality_score(incoming)
    if si > se:
        return {"recommended": "incoming", "reason": _quality_reason(incoming) or None}
    if se > si:
        return {"recommended": "existing", "reason": _quality_reason(existing) or None}
    return {"recommended": "tie", "reason": None}


def _full_dest_path(job: dict) -> Optional[str]:
    """Job's would-be full destination path, or None if not yet targeted."""
    dest = (job.get("destination_path") or "").rstrip("/\\")
    name = job.get("new_filename") or ""
    if not dest or not name:
        return None
    return f"{dest}/{name}".replace("\\", "/").casefold()


def find_library_duplicate(job: dict, plex_cache_rows: list) -> Optional[dict]:
    """Match *job* against the Plex library by imdb_id (exact) or normalized
    title+year (fallback), for movies only. Returns the matched plex_cache
    row, or None if there's no match, the job is TV, the job isn't in an
    ACTIVE status (an applied/failed/reverted job has nothing left to
    resolve — matches _ACTIVE_STATUSES' semantics, same statuses
    destination_conflict is restricted to), or the only match is at the
    job's own destination path (that's the exact-path case, already covered
    by destination_conflict — never double-flag it here).

    Pure, DB-free — plex_cache_rows is whatever the caller already fetched."""
    if (job.get("media_type") or "movie") != "movie":
        return None
    if job.get("status") not in _ACTIVE_STATUSES:
        return None
    imdb_id = job.get("imdb_id")
    candidates = [r for r in plex_cache_rows if not r.get("is_tv")]
    match = None
    if imdb_id:
        match = next((r for r in candidates if r.get("imdb_id") == imdb_id), None)
    if not match:
        from backend.app_service import normalize_title
        job_key = (normalize_title(job.get("title") or ""), job.get("year"))
        if job_key[0]:
            match = next(
                (r for r in candidates
                 if (normalize_title(r.get("title") or ""), r.get("year")) == job_key),
                None)
    if not match:
        return None
    job_dest = _full_dest_path(job)
    match_path = (match.get("file_path") or "").replace("\\", "/").casefold()
    if job_dest and match_path and job_dest == match_path:
        return None  # same-path — the exact-path collision case, not this one
    return match


def _tuple_winner(a: tuple, b: tuple) -> str:
    """Which side wins a full _quality_score() tuple comparison."""
    if a > b:
        return "existing"
    if b > a:
        return "incoming"
    return "tie"


def needs_dv_layer_scan(existing: dict, incoming: dict) -> bool:
    """Whether resolving the Dolby Vision FEL/MEL layer via dovi_tool could
    actually change rank_conflict()'s recommended winner between *existing*
    and *incoming* — i.e. whether the scan's multi-minute cost buys anything.

    Both sides must already be Dolby Vision (the ``dv`` bit set); otherwise
    there's no layer to resolve. If BOTH sides already have a known
    dv_layer, there's nothing left to learn either way — skip.

    Otherwise, at least one side's true dv_layer_rank (tuple index 2) is
    unknown and could turn out to be any of the values dovi_tool can report
    (0 = no recognized layer, up to 3 = fel — see _DV_LAYER_RANK). This
    checks whether the FULL _quality_score() tuple comparison — not index 2
    in isolation — would produce the SAME winner (existing / incoming / tie)
    for every one of those possible values. If the winner is invariant, the
    scan changes nothing and is skipped; if it varies for even one possible
    outcome — including a confident win degrading to a genuine tie, not
    just a full reversal — the scan is worth running.

    This must simulate the complete tuple, not just dv_layer_rank alone:
    naively assuming "the known side is already at the ceiling rank, so it
    always wins" is WRONG, because a tie at dv_layer_rank (the unscanned
    side could also turn out to be the ceiling rank) falls through to later
    tiers (hdr/source/audio/edition), which are independent of DV layer and
    can already differ between the two files — a real winner reversal is
    reachable through that fallthrough, not just a downgrade to tie."""
    se = _quality_score(existing)
    si = _quality_score(incoming)
    if se[1] != 1 or si[1] != 1:
        return False  # not both Dolby Vision — no layer distinction applies

    # "unknown" is a real, load-bearing sentinel (dv_detect.LAYER_UNKNOWN) —
    # detection ran but couldn't determine a layer (missing tool/error), NOT
    # a settled "no DV layer" result like LAYER_NONE. The host detector's own
    # dv_host_scan.py deliberately stores a NULL signature for "unknown" so
    # the next scan retries it. Treating it as "known" here would skip a
    # scan that could still resolve to something conclusive — a plain
    # bool(dv_layer) check can't tell "unknown" apart from a real value.
    from backend.rename.dv_detect import LAYER_UNKNOWN as _LAYER_UNKNOWN
    e_known = bool(existing.get("dv_layer")) and existing.get("dv_layer") != _LAYER_UNKNOWN
    i_known = bool(incoming.get("dv_layer")) and incoming.get("dv_layer") != _LAYER_UNKNOWN
    if e_known and i_known:
        return False  # both already resolved — nothing left to learn

    possible_ranks = sorted(set(_DV_LAYER_RANK.values()) | {0})

    def _with_rank(t: tuple, rank: int) -> tuple:
        return t[:2] + (rank,) + t[3:]

    outcomes = set()
    if e_known:
        for rank in possible_ranks:
            outcomes.add(_tuple_winner(se, _with_rank(si, rank)))
            if len(outcomes) > 1:
                return True
    elif i_known:
        for rank in possible_ranks:
            outcomes.add(_tuple_winner(_with_rank(se, rank), si))
            if len(outcomes) > 1:
                return True
    else:
        # Neither known — both vary independently over the full range.
        for e_rank in possible_ranks:
            for i_rank in possible_ranks:
                outcomes.add(_tuple_winner(_with_rank(se, e_rank), _with_rank(si, i_rank)))
                if len(outcomes) > 1:
                    return True
    return False

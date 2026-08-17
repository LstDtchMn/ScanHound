"""Badge a Plex movie with HOW MANY versions it has.

The owner keeps multiple versions deliberately — 1,032 movies in this library
have more than one file — and wants the poster to say so. Kometa overlays are
label-gated with fixed text, so a COUNT means one label per value rather than
one label with a number in it.

DELIBERATELY SEPARATE FROM dv_labeler's MANAGED SET. reconcile_movie computes
``removed = existing_managed - desired_set``, and desired_set comes from
desired_labels(layer), which knows only about Dolby Vision. Putting "2 Versions"
into MANAGED would therefore have the DV sync STRIP it on every run — the exact
trap RETIRED_LABELS exists to document. These labels get their own closed set
and their own reconcile pass, sharing the DV labeler's discipline but not its
vocabulary.
"""
import logging

logger = logging.getLogger(__name__)

#: Above this, the count collapses into the catch-all bucket.
MAX_EXACT_VERSIONS = 4

#: The CLOSED set this module may remove. Nothing outside it is ever touched, so
#: a label the owner applied by hand is safe — the same rule that stopped the DV
#: labeler deleting 'DV Cut'.
VERSION_LABELS = frozenset(
    [f"{n} Versions" for n in range(2, MAX_EXACT_VERSIONS + 1)]
    + [f"{MAX_EXACT_VERSIONS + 1}+ Versions"]
)


def version_label(count):
    """The label for a movie with *count* versions, or None if it needs none.

    A single version gets NO label rather than "1 Version": the badge exists to
    flag duplicates, and badging every movie in the library would make it noise.

    Counts above MAX_EXACT_VERSIONS collapse into "5+ Versions" instead of
    generating an unbounded label vocabulary. That bucket is not padding — Kometa
    needs one overlay block per label, so a count with no label produces no
    overlay, and the poster would silently lose its badge in a way that looks
    exactly like "this movie has one version". Live data today: 983 twos, 48
    threes, 1 four, 0 fives.
    """
    if not isinstance(count, int) or count < 2:
        return None
    if count > MAX_EXACT_VERSIONS:
        return f"{MAX_EXACT_VERSIONS + 1}+ Versions"
    return f"{count} Versions"


def count_versions(rows):
    """``{rating_key: version_count}`` from plex_cache movie rows.

    One row per version is the shape plex_cache stores. Rows with no rating_key
    are dropped — they cannot be attributed to a title, and counting them into
    someone else's total would be worse than ignoring them.

    Returns COUNTS ONLY, deliberately: this is the half of the duplicate feature
    that does not need to identify individual versions, so it does not depend on
    media_id being unique (6 of 1,032 multi-version movies have repeated ones).
    """
    counts = {}
    for r in rows or ():
        key = r.get("rating_key")
        if key is None or key == "":
            continue
        counts[str(key)] = counts.get(str(key), 0) + 1
    return counts


def reconcile_movie_versions(movie, counts, pm, *, dry_run=False):
    """Reconcile one movie's version badge. Returns ``{added, removed}``.

    ``counts`` is ``{rating_key: version_count}``. A rating_key ABSENT from it is
    UNKNOWN, not "one version", and unknown never authorises removal — the same
    rule the DV labeler enforces, and for the same reason: a cache gap must not
    be read as evidence and strip a correct badge.
    """
    rating_key = str(getattr(movie, "ratingKey", "") or "")
    existing = set()
    for lab in (getattr(movie, "labels", None) or []):
        tag = getattr(lab, "tag", None) or (lab if isinstance(lab, str) else None)
        if tag:
            existing.add(tag)
    existing_managed = existing & VERSION_LABELS

    if rating_key not in counts:
        # No cached row for this title. Say nothing rather than guess.
        return {"added": [], "removed": [], "count": None}

    count = counts[rating_key]
    wanted = version_label(count)
    desired = {wanted} if wanted else set()

    added = sorted(desired - existing_managed)
    removed = sorted(existing_managed - desired)

    if not dry_run:
        for lab in added:
            try:
                pm.add_label(movie.ratingKey, lab)
            except Exception as e:  # noqa: BLE001
                logger.warning("add_label %s on %s failed: %s", lab, rating_key, e)
        for lab in removed:
            try:
                pm.remove_label(movie.ratingKey, lab)
            except Exception as e:  # noqa: BLE001
                logger.warning("remove_label %s on %s failed: %s", lab, rating_key, e)

    return {"added": added, "removed": removed, "count": count}


def sync_version_labels(db, pm, config, *, dry_run=False, progress_cb=None):
    """Badge every movie in the configured libraries with its version count.

    Mirrors dv_labeler.sync_labels' shape: read the cache once, walk the
    libraries, reconcile per title. The counts come from plex_cache, so this
    makes no per-movie Plex call.
    """
    rows = db.list_plex_cache_movies() if hasattr(db, "list_plex_cache_movies") else []
    counts = count_versions(rows)
    multi = sum(1 for c in counts.values() if c > 1)
    logger.info("version labels: %d cached movie rows -> %d titles, %d multi-version",
                len(rows), len(counts), multi)

    libs = config.get("movie_libs") or config.get("known_movie_libraries") or []
    seen = set()
    movies = []
    for name in libs:
        try:
            lib = pm.get_library_section(name)
            if not lib:
                continue
            for mv in lib.all():
                if mv.ratingKey in seen:
                    continue
                seen.add(mv.ratingKey)
                movies.append(mv)
        except Exception as e:  # noqa: BLE001
            logger.warning("version labels: library %s failed: %s", name, e)

    added_n = removed_n = badged_n = unknown_n = 0
    for i, mv in enumerate(movies):
        try:
            res = reconcile_movie_versions(mv, counts, pm, dry_run=dry_run)
            added_n += len(res["added"])
            removed_n += len(res["removed"])
            if res["count"] is None:
                unknown_n += 1
            elif res["count"] > 1:
                badged_n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("version labels: title %s failed: %s",
                           getattr(mv, "title", "?"), e)
        if progress_cb:
            progress_cb(i + 1, len(movies))

    return {
        "total": len(movies),
        "added": added_n,
        "removed": removed_n,
        "multi_version": badged_n,
        # Titles with no cached row. Reported rather than folded into "single
        # version", because the two mean different things and only one of them
        # is a reason to look at the cache.
        "unknown": unknown_n,
        "dry_run": dry_run,
    }

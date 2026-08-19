"""Badge a Plex movie with HOW MANY versions it has.

The owner keeps multiple versions deliberately — 1,029 movies in this library
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
import time

logger = logging.getLogger(__name__)

#: Above this, the count collapses into the catch-all bucket.
MAX_EXACT_VERSIONS = 4

#: Inter-write pause, same value and same reason as dv_labeler's: a full
#: backfill touches roughly a thousand titles and must not hammer Plex.
_THROTTLE_S = 0.05

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
    
    COUNTS DISTINCT ``media_id``, NOT ROWS. plex_cache stores one row per
    PART, not per version: `_extract_movie_data()` emits a row for each part and
    gives multipart media separate cache keys while REUSING the media_id, with a
    two-file DVD rip as its worked example. Counting rows therefore reports a
    one-version two-part film as "2 Versions", and a two-version film where one
    is multipart as "3 Versions".
    
    Not hypothetical: six live titles are in that shape, including
    `Friday the 13th: The New Blood` (2 rows, 1 media_id -> should carry NO
    badge) and `Lawrence of Arabia` (3 rows, 2 media_ids -> "2 Versions", not
    three). An earlier version of this docstring cited those same six as a
    reason NOT to depend on media_id. That was exactly backwards -- ignoring it
    is what produced the wrong count (peer review 2026-08-19, H1).
    
    A row with no media_id makes its title UNKNOWN rather than guessed: parts
    and versions become indistinguishable for that title, and the module's
    standing rule is that an unknown count touches nothing. Zero live rows are
    in that state; this is a guard against the shape, not a fix for existing
    data.
    
    Rows with no rating_key are dropped -- they cannot be attributed to a title,
    and counting them into someone else's total would be worse than ignoring
    them.
    """
    media_by_key = {}
    unusable = set()
    for r in rows or ():
        key = r.get("rating_key")
        if key is None or key == "":
            continue
        key = str(key)
        media = r.get("media_id")
        if media is None or media == "":
            unusable.add(key)
            continue
        media_by_key.setdefault(key, set()).add(str(media))
    return {k: len(v) for k, v in media_by_key.items() if k not in unusable}

def reconcile_movie_versions(movie, counts, pm, *, dry_run=False):
    """Reconcile one movie's version badge. Returns ``{added, removed}``.

    ``counts`` is ``{rating_key: version_count}``. A rating_key ABSENT from it is
    UNKNOWN, not "one version", and unknown never authorises removal — the same
    rule the DV labeler enforces, and for the same reason: a cache gap must not
    be read as evidence and strip a correct badge.
    """
    failed = 0
    rating_key = str(getattr(movie, "ratingKey", "") or "")
    existing = set()
    for lab in (getattr(movie, "labels", None) or []):
        tag = getattr(lab, "tag", None) or (lab if isinstance(lab, str) else None)
        if tag:
            existing.add(tag)
    existing_managed = existing & VERSION_LABELS

    if rating_key not in counts:
        # No cached row for this title. Say nothing rather than guess.
        return {"added": [], "removed": [], "count": None, "failed": 0}

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
                failed += 1
                logger.warning("add_label %s on %s failed: %s", lab, rating_key, e)
        for lab in removed:
            try:
                pm.remove_label(movie.ratingKey, lab)
            except Exception as e:  # noqa: BLE001
                failed += 1
                logger.warning("remove_label %s on %s failed: %s", lab, rating_key, e)

        if (added or removed) and not dry_run:
            # Pace the writes, exactly as dv_labeler does: the first backfill
            # touches ~1,000 titles, and an unpaced burst is what makes Plex
            # start rejecting them -- which, combined with a watermark that
            # advanced anyway, would have marked the generation done with the
            # badges missing.
            time.sleep(_THROTTLE_S)
    # `failed` is the number of label WRITES that did not land. Reported rather
    # than only logged, because the caller decides whether the pass may be
    # called complete, and "the function returned" is not "the work happened".
    return {"added": added, "removed": removed, "count": count, "failed": failed}


def sync_version_labels(db, pm, config, *, dry_run=False, progress_cb=None):
    """Badge every movie in the configured libraries with its version count.

    Mirrors dv_labeler.sync_labels' shape: read the cache once, walk the
    libraries, reconcile per title. The counts come from plex_cache, so this
    makes no per-movie Plex call.

    REPORTS `complete`, AND THE CALLER MUST HONOUR IT. Every failure in here is
    caught and logged so one bad title cannot abandon the rest -- a library that
    will not enumerate, a title that raises, a label write Plex rejects. That is
    the right behaviour for the pass and the wrong signal for a watermark:
    returning normally after a hundred rejected writes would let the scheduler
    mark the cache generation reconciled and never retry it, leaving the badges
    missing (peer review 2026-08-19, M2).

    `complete` is False if ANY library failed to enumerate, ANY title raised, or
    ANY label write failed. A library that could not be listed counts, because
    its titles were never reconciled at all.
    """
    # STRICT READ. `list_plex_cache_movies()` turns a database error into `[]`,
    # which here is indistinguishable from an empty cache: every live movie
    # becomes "unknown", the reconciler correctly touches nothing, NO counter
    # records a failure, and the pass reports complete -- so the generation is
    # consumed and the badges stay stale (peer review M2/B). An empty table is
    # still a valid answer; only a failed read raises.
    cache_failures = 0
    try:
        if hasattr(db, "list_plex_cache_movies_strict"):
            rows = db.list_plex_cache_movies_strict()
        else:
            rows = db.list_plex_cache_movies() if hasattr(db, "list_plex_cache_movies") else []
    except Exception as e:  # noqa: BLE001
        cache_failures = 1
        rows = []
        logger.warning("version labels: plex_cache read failed: %s", e)
    counts = count_versions(rows)
    multi = sum(1 for c in counts.values() if c > 1)
    logger.info("version labels: %d cached movie rows -> %d titles, %d multi-version",
                len(rows), len(counts), multi)

    libs = config.get("movie_libs") or config.get("known_movie_libraries") or []
    lib_failures = 0
    seen = set()
    movies = []
    for name in libs:
        try:
            lib = pm.get_library_section(name)
            if not lib:
                # NOT a harmless skip. Production PlexManager catches its own
                # connect/lookup errors and RETURNS None, so this is what a real
                # failure looks like -- the mock that raises only proved the
                # except branch. Its titles were never reconciled, so the pass
                # is not complete (peer review M2/A).
                lib_failures += 1
                logger.warning("version labels: library %s did not resolve", name)
                continue
            for mv in lib.all():
                if mv.ratingKey in seen:
                    continue
                seen.add(mv.ratingKey)
                movies.append(mv)
        except Exception as e:  # noqa: BLE001
            lib_failures += 1
            logger.warning("version labels: library %s failed: %s", name, e)

    added_attempted = removed_attempted = badged_n = unknown_n = 0
    title_failures = write_failures = 0
    for i, mv in enumerate(movies):
        try:
            res = reconcile_movie_versions(mv, counts, pm, dry_run=dry_run)
            added_attempted += len(res["added"])
            removed_attempted += len(res["removed"])
            write_failures += res.get("failed", 0)
            if res["count"] is None:
                unknown_n += 1
            elif res["count"] > 1:
                badged_n += 1
        except Exception as e:  # noqa: BLE001
            title_failures += 1
            logger.warning("version labels: title %s failed: %s",
                           getattr(mv, "title", "?"), e)
        if progress_cb:
            progress_cb(i + 1, len(movies))

    return {
        "total": len(movies),
        # ATTEMPTED, not confirmed. reconcile computes the diff, then tries the
        # writes; a rejected write still counts here and is reported separately
        # in write_failures. Named for what they are so the operational log
        # cannot imply a badge landed when Plex refused it (peer review L2).
        "added_attempted": added_attempted,
        "removed_attempted": removed_attempted,
        "multi_version": badged_n,
        # Titles with no cached row. Reported rather than folded into "single
        # version", because the two mean different things and only one of them
        # is a reason to look at the cache.
        "unknown": unknown_n,
        "dry_run": dry_run,
        # The counters the caller's watermark decision rests on.
        "cache_failures": cache_failures,
        "lib_failures": lib_failures,
        "title_failures": title_failures,
        "write_failures": write_failures,
        "complete": not (cache_failures or lib_failures or title_failures
                         or write_failures),
    }

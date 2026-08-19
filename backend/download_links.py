"""Annotate live download rows with their source page, first-seen date, and the
semantic identity (TV season, title, year) recorded when they were grabbed.

Both transports that carry download results to the UI must annotate, not just
the REST one: the Downloads page replaces its whole list from the
`download:results` WebSocket push, so enriching only the REST poll would make
the link appear every 5s and vanish on the next push.

WHAT AUTHORISES A LINK (peer review Finding 1, 2026-08-13).

Only recorded provenance -- `download_results.provenance_url`, set by the poller
when a package's file-host links match links ScanHound recorded submitting.

The previous version matched on the JDownloader package NAME and refused only
when two ScanHound releases shared it. That is a closed-world guard:
`poll_results()` enumerates JDownloader's ENTIRE package list, so a package added
by hand has no `downloads` row, collides with nothing, and was handed a confident
link to an unrelated release -- along with that release's date. Name matching is
gone from this path entirely rather than tightened, because a name is evidence of
nothing: a package is ours because we can show we sent its links, or it is not
ours and gets no link.
"""
import logging

logger = logging.getLogger(__name__)


#: Every key this helper guarantees on a row, with its unproven value. Rows are
#: initialised from this BEFORE any lookup, so a failure part-way through cannot
#: leave some rows carrying identity keys and others not -- an inconsistent
#: shape between the two transports is the exact defect this module exists to
#: prevent, and identity is now load-bearing rather than decorative.
UNPROVEN = {
    "source_url": None,
    "first_seen_at": None,
    "identity_kind": "unknown",
    "identity_title": None,
    "identity_year": None,
    "identity_season": None,
    "identity_source": "unknown",
}

#: Titles that are a stand-in for "we did not record one", casefolded. They are
#: NOT identities: several unrelated releases carry the same string, so treating
#: them as real would hand a whole set of unrelated packages one identical
#: identity -- exactly the collision this module exists to prevent, and worse
#: than an absent title because it looks answered.
#:
#: Both are live defaults, not hypotheticals: `DownloadRequest.title` defaults to
#: "Untitled" (backend/api/routes/downloads.py) and the RSS action path falls
#: back to "RSS Candidate" (backend/hdencode_action_service.py). Neither appears
#: in the current `downloads` table -- this is a guard against the shape, not a
#: fix for existing rows.
_PLACEHOLDER_TITLES = frozenset({"untitled", "rss candidate"})


def annotate_source_links(db, rows):
    """Add the source link and the SEMANTIC IDENTITY to each row, in place.

    Every key in `UNPROVEN` is always set, so consumers never have to
    distinguish "not annotated yet" from "no match".

    WHAT IDENTITY IS FOR. The UI groups downloads to offer "keep the best copy,
    cancel the rest", and until now it decided whether two rows were the same
    thing by parsing their JDownloader package names. That string cannot carry
    the answer: it is capped at 50 characters, and 17 live rows share a name
    spanning several seasons -- `Law & Order: LA (2010) [1080p]` covers 13 of
    them. Carrying what the grab actually recorded replaces a guess with a fact
    for the rows that have it.

    THE ONLY POSITIVE KIND EMITTED IS `tv_season`, and only when a season was
    actually recorded. A seasonless row is `unknown` -- never `movie`.

    That is not caution, it is the absence of a fact. `add_to_history()` takes
    no media-type argument and `downloads` stores no such column, so
    `("Notting Hill", 1999, None)` is structurally identical to a 1999 show
    whose season was never captured. A year identifies an edition; it does not
    prove a media kind.

    Three narrower rules were tried and each failed for a reason the previous
    one did not anticipate -- placeholder titles, then 16 live rows that are
    plainly TV (`...-s02-...` in their source url) with no recorded season,
    then year=0 as an ingest sentinel. Three misses in a row is the signature of
    a wrong premise rather than a leaky guard, so the inference is gone until a
    media kind is RECORDED AT INGEST (peer review 2026-08-18, M1).

    KNOWN COVERAGE GAP, and it predates this function. Identity resolves only
    when `provenance_url` matches a `downloads.url`. The RSS auto-grab path
    records provenance under the canonical release url
    (`hdencode_action_service.py` -> `record_submitted_links(canonical_url, ...)`)
    but writes its history rows under EACH FILE-HOST LINK
    (`for link in links: save_to_history(link, ...)`), so those two never meet
    and such a row stays `unknown` forever. `first_seen_at` has been silently
    absent for that path for the same reason since long before identity existed;
    this simply inherits the join. Not fixed here because the fix belongs in how
    that service records history, and changing which urls land in `downloads`
    also changes `load_download_history()`, which suppresses re-grabs. All 31
    provenance-carrying rows in the live table came through `download_item()`
    and do join, so nothing is currently affected -- but a row from that path
    would be, and it would fail CLOSED (unknown), not wrong.

    KNOWN LIMIT, stated rather than papered over. Identity is the CURRENT
    recorded metadata for a release url, not an immutable per-submission
    snapshot: `downloads.url` is the primary key and `add_to_history()` updates
    title and season on conflict. Two grabs of the same url therefore share one
    identity by construction, which is correct for a re-grab and would be wrong
    if one url were ever reused for two different releases. No production path
    does that today.

    Never raises: enrichment is decoration on a live progress list, and the
    caller's real job (showing downloads, driving the poller loop) must not fail
    because a lookup did. A failure leaves every row `unknown`, which is the
    fail-closed direction -- the UI withholds the destructive action.
    """
    rows = rows or []
    for row in rows:
        # Default to unproven, then upgrade. Set FIRST so an exception below
        # cannot leave some rows carrying the keys and others not.
        row.update(UNPROVEN)
    if db is None:
        return rows
    try:
        # FRESHNESS POLICY: the LAST PROOF stays authoritative until it is
        # retracted, and BOTH transports must apply it. The poller's in-memory
        # row carries provenance_url=None whenever it could not observe a
        # package's links, while download_results deliberately keeps the
        # previous proof in that case. Left alone, the WebSocket row resolves to
        # `unknown` while a REST poll of the same package resolves to a full
        # identity -- the two transports disagreeing about a fact that gates
        # cancelling downloads, which is exactly what this module exists to
        # prevent (peer review 2026-08-18, M2).
        #
        # Recovering the persisted value here is the same rule the database
        # already implements, applied one layer up. Costs ONE batched query,
        # and only when a row is in that state, which is normally none.
        # Keyed by the durable download_results.id, never by package name.
        # poll_results() attaches that id to every row it emits and REST rows
        # carry the same one; a row that somehow has none is left unrecovered,
        # because recovering by name would let a different same-named row donate
        # its provenance -- the exact collision identity exists to remove.
        stale = [r for r in rows
                 if not r.get("provenance_url")
                 and r.get("provenance_observed") is False
                 and r.get("id") is not None]
        if stale:
            persisted = db.get_persisted_provenance([r.get("id") for r in stale]) or {}
            for row in stale:
                recovered = persisted.get(row.get("id"))
                if recovered:
                    row["provenance_url"] = recovered

        proven = {}
        for row in rows:
            url = row.get("provenance_url")
            if url:
                proven[str(url)] = True
        if not proven:
            return rows
        found = db.get_release_identity(list(proven)) or {}
    except Exception:
        logger.exception("download results: source-link enrichment failed")
        return rows
    for row in rows:
        url = row.get("provenance_url")
        if not url:
            continue
        row["source_url"] = str(url)
        # A proven url with no history row still gets its link: provenance is
        # what authorises the link, and the identity is separate information
        # that may simply be absent.
        rec = found.get(str(url))
        if not rec:
            continue
        row["first_seen_at"] = rec.get("date_added")
        title = rec.get("title")
        clean = str(title).strip() if title else ""
        if not clean or clean.casefold() in _PLACEHOLDER_TITLES:
            # A row with no recorded title cannot identify anything, so it must
            # not claim a kind either -- "movie with no title" would group every
            # such row together. A PLACEHOLDER is the same failure wearing a
            # value: "Untitled" is not a title, and two rows carrying it are not
            # the same release.
            continue
        season = rec.get("season")
        # `or None` because 0 IS THE INGEST SENTINEL FOR "no year", not a year.
        # scanner_service writes `year=d.get('year', 0) or 0`, and 6 live rows
        # carry year=0 -- the only sub-1900 value in the column. Testing
        # `year is None` alone let all six through: five have a season and would
        # have gone out as tv_season carrying identity_year=0, a WRONG value
        # rather than an absent one, and the sixth has no season either and would
        # have been a confident `movie` whose guard had just been added to stop
        # exactly that.
        year = rec.get("year") or None
        if season is None:
            # NO RECORDED SEASON MEANS UNKNOWN, NOT MOVIE.
            #
            # This verdict has now failed three times, each for a reason the
            # previous fix did not anticipate: placeholder titles, then 16 live
            # TV rows whose season was never recorded, then year=0. The peer
            # review named why that keeps happening -- the problem is
            # CATEGORICAL, not a sentinel hunt. `("Notting Hill", 1999, None)`
            # is structurally identical to a 1999 TV show with a missing season.
            # `add_to_history()` takes no media-type argument and `downloads`
            # stores no such column, so NOTHING in the data distinguishes them.
            # A year identifies an edition; it does not prove a media kind.
            #
            # The old positive test asserted `Notting Hill` came back as a
            # movie. That proved the convention, not the discriminator -- the
            # test author knew it was a film; production never did.
            #
            # So a seasonless row stays unknown until a media kind is RECORDED
            # AT INGEST. That costs movie identity entirely for now, which is
            # the honest price: the destructive action this gates is already
            # withheld from every movie today, and re-enabling it on an
            # unprovable inference is how the previous three holes happened.
            continue
        row["identity_title"] = clean
        row["identity_year"] = year
        row["identity_season"] = season
        row["identity_kind"] = "tv_season"
        row["identity_source"] = "provenance"
    return rows

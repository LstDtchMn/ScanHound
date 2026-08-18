"""Annotate live download rows with their source page, first-seen date, and the
semantic identity (movie/TV, title, year, season) recorded when they were
grabbed.

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

    `identity_kind` is `tv_season` when a season was recorded and `movie` when
    one was not. That is the discriminator the backend already uses on itself
    (`save_to_history` keys its lookup on `season is not None`), but it is a
    CONVENTION rather than a schema constraint, and on its own it DOES NOT HOLD.

    An earlier version of this docstring defended it by noting that no
    season-less history row carries a season token in its title or package name.
    That was true and misleading: the season is in the SOURCE URL, which that
    check never looked at. Measured properly, 16 season-less rows are plainly TV
    (`...-s02-...` in the hdencode slug), and four title groups hold more than
    one -- `Law & Order: Special Victims Unit` has three, which are seasons 1, 2
    and 3.

    So `movie` additionally requires a YEAR. All 16 of those rows also lack one,
    which makes the year a clean discriminator rather than a patch aimed at
    them, and it is the principled rule anyway: a movie identity IS title+year,
    so a row with neither season nor year has nothing to identify with and stays
    `unknown`. Cost: 179 year-less rows lose a movie identity, 273 keep one.
    None of the 16 reaches the wire today (none carries provenance), so this
    closes the mechanism before it can produce a wrong answer rather than after.

    A row we cannot look up at all likewise stays `unknown` rather than
    defaulting to `movie` -- guessing "movie" would authorise cancelling one
    season against another.

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

    KNOWN LIMIT, stated rather than papered over. `movie` rests on that
    convention holding at INGEST: `download_item()` receives `season` from the
    scraped listing, so a TV grab whose listing yielded no season would be
    recorded with `season=None` and read back here as a movie. Two seasons of
    such a show would then share one title+year identity. Nothing in the current
    data is in that state, and no heuristic here can detect it -- the season is
    absent from every column, not merely from the display name. The durable fix
    is a recorded media type at ingest; until then a consumer that treats
    `movie` as permission to cancel is trusting the ingest path, and should say
    so where it makes that decision.

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
        year = rec.get("year")
        if season is None and year is None:
            # NEITHER discriminator. "movie" here would be a guess, and a wrong
            # one: 16 live history rows are plainly TV -- their source url slug
            # says `-s02-` -- with no season recorded, and ALL 16 also lack a
            # year. Three are Law & Order: Special Victims Unit seasons 1, 2
            # and 3, which would have collapsed onto ONE identity: the exact
            # collision this module exists to prevent, produced by the module
            # itself. A movie identity IS title+year, so a row with neither
            # season nor year has nothing to identify with.
            continue
        row["identity_title"] = clean
        row["identity_year"] = year
        row["identity_season"] = season
        row["identity_kind"] = "movie" if season is None else "tv_season"
        row["identity_source"] = "provenance"
    return rows

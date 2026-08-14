"""Annotate live download rows with their source page and first-seen date.

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


def annotate_source_links(db, rows):
    """Add `source_url` / `first_seen_at` to each row, in place.

    Both keys are always set (to None when unproven) so consumers never have to
    distinguish "not annotated yet" from "no match" -- an inconsistent shape
    between the two transports is what produced the flicker this helper exists
    to prevent.

    Never raises: enrichment is decoration on a live progress list, and the
    caller's real job (showing downloads, driving the poller loop) must not fail
    because a lookup did.
    """
    rows = rows or []
    for row in rows:
        # Default to unproven, then upgrade. Set FIRST so an exception below
        # cannot leave some rows carrying the keys and others not.
        row["source_url"] = None
        row["first_seen_at"] = None
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
        dates = db.get_release_first_seen(list(proven)) or {}
    except Exception:
        logger.exception("download results: source-link enrichment failed")
        return rows
    for row in rows:
        url = row.get("provenance_url")
        if not url:
            continue
        row["source_url"] = str(url)
        # A proven url with no history row still gets its link: provenance is
        # what authorises the link, and the date is separate information that
        # may simply be absent.
        row["first_seen_at"] = dates.get(str(url))
    return rows

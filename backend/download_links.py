"""Annotate live download rows with their source page and first-grab date.

Both transports that carry download results to the UI must annotate, not just
the REST one: the Downloads page replaces its whole list from the
`download:results` WebSocket push, so enriching only the REST poll would make
the link appear every 5s and vanish on the next push.
"""
import logging

logger = logging.getLogger(__name__)


def annotate_source_links(db, rows):
    """Add `source_url` / `first_grabbed_at` to each row, in place.

    Both keys are always set (to None when unresolved) so consumers never have
    to distinguish "not annotated yet" from "no match" — a row that reaches the
    UI without the keys would render as a missing link either way, but an
    inconsistent shape between the two transports is what produced the flicker
    this helper exists to prevent.

    Never raises: enrichment is decoration on a live progress list, and the
    caller's real job (showing downloads, driving the poller loop) must not fail
    because a lookup did.
    """
    rows = rows or []
    links = {}
    if db is not None:
        try:
            links = db.get_download_source_links([r.get("name") for r in rows]) or {}
        except Exception:
            logger.exception("download results: source-link enrichment failed")
            links = {}
    for row in rows:
        link = links.get(row.get("name")) or {}
        row["source_url"] = link.get("source_url")
        row["first_grabbed_at"] = link.get("first_grabbed_at")
    return rows

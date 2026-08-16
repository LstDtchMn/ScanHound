"""A per-item history, in language a non-programmer can act on.

WHY THIS EXISTS. The owner: *"we still never implemented a visible 'Selected',
'Links grabbed', etc ... maybe we should make it so the user can click the items
in that menu for more info, like a direct link, a history of the item, retries,
date posted, date item selected, date item links grab or any attempts that
failed, if the links are dead, wtf..."*

Everything here is DERIVED from rows that already exist. The queue item carries
`created_at` (when it was selected), `completed_at` (when the links were
grabbed), `attempt_count`, `automated_retry_count`, `canonical_url` and
`last_message`; `download_queue_attempts` carries one row per real attempt with
its outcome. Nothing new is captured.

TWO RULES THIS MODULE EXISTS TO ENFORCE.

1. **No reason code reaches the screen.** `reveal_verification_stalled` is not a
   sentence. Wording comes from `download_outcome._FAILURE_TITLES`, which is the
   vocabulary the notifications already use -- a SECOND wording table would drift
   from it, and this codebase has been bitten by two registries answering one
   question more than once. `describe_reason()` falls back to a neutral phrase
   rather than inventing a cause.

2. **No claim the data cannot support.** Attempts recorded before
   `ATTEMPT_HISTORY_TRUSTED_FROM` were fabricated by a backstop and say FAILED
   for downloads that succeeded, so they are excluded and their absence is stated
   ("no detailed history recorded before ..."), never silently rendered as "this
   item was never tried".
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from backend.download_outcome import _FAILURE_TITLES

#: The backstop's reason code: the attempt escaped without reporting anything.
#: Its transport_attempted is a DEFAULT, not an observation, so no claim about
#: whether the source was contacted may be built on it.
_NOT_CLOSED = "attempt_not_closed"

#: What we say when a code has no entry. Deliberately describes OUR state, not a
#: cause: naming a cause we have not established is the mistake several of these
#: codes were split apart to stop making.
_UNKNOWN_REASON = "It did not finish, and the reason was not recorded"

#: Plain-English names for the states a user can actually be looking at.
_STATE_LABELS = {
    "scheduled": "Waiting its turn",
    "ready": "Ready to try",
    "claimed": "Working on it now",
    "waiting_source": "Waiting for HDEncode",
    "verification_required": "Needs you",
    "completed": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def describe_reason(reason_code: Optional[str]) -> str:
    """One human sentence for a reason code, or a neutral fallback.

    Never returns the raw code: a screen that shows `layout_changed` has simply
    moved the problem from the log to the user.
    """
    if not reason_code:
        return _UNKNOWN_REASON
    title = _FAILURE_TITLES.get(str(reason_code))
    return title or _UNKNOWN_REASON


def describe_state(state: Optional[str]) -> str:
    return _STATE_LABELS.get(str(state or ""), str(state or "Unknown"))


def _event(at: Optional[str], text: str, kind: str = "info",
           detail: str = "") -> Dict[str, Any]:
    return {"at": at, "text": text, "kind": kind, "detail": detail}


def build_timeline(item: Mapping[str, Any],
                   attempts: List[Mapping[str, Any]],
                   *, trusted_from: str = "") -> List[Dict[str, Any]]:
    """The item's story, oldest first.

    `attempts` must already be filtered to the trustworthy window; this function
    does not re-decide that, so the caller and the UI cannot disagree about which
    rows are shown.
    """
    events: List[Dict[str, Any]] = []

    if item.get("created_at"):
        events.append(_event(item["created_at"], "You added this to the queue",
                             kind="queued"))

    for a in attempts:
        status = str(a.get("terminal_status") or "")
        reason = a.get("reason_code")
        started = a.get("started_at")
        if status == "SUCCESS":
            # source_progress is the only positive evidence the source handed
            # anything over; item completion is not, because a cache-resolved
            # duplicate completes without ever contacting HDEncode.
            if a.get("source_progress"):
                events.append(_event(started, "Got the download links", kind="ok"))
            else:
                events.append(_event(
                    started, "Already had this one, so nothing was downloaded",
                    kind="ok"))
        elif status == "INTENTIONALLY_SKIPPED":
            events.append(_event(
                started, "Skipped this time — %s" % describe_reason(reason).lower(),
                kind="skipped"))
        elif status == "IN_PROGRESS":
            events.append(_event(started, "Started — still running", kind="running"))
        else:
            scope = str(a.get("affected_scope") or "item")
            text = describe_reason(reason)
            if scope == "source":
                text += " (this affected HDEncode as a whole, not just this item)"
            elif not a.get("transport_attempted") and reason != _NOT_CLOSED:
                # Worth saying: the difference between "we asked and it went
                # wrong" and "we never got as far as asking" is the difference
                # between blaming the source and blaming us.
                #
                # EXCEPT on attempt_not_closed. There the flag is the backstop's
                # DEFAULT, not an observation -- the attempt escaped without
                # reporting anything, so whether a request went out is precisely
                # what we do not know. Caught by running this against production:
                # a real stuck item rendered "the reason was not recorded (no
                # request reached the source)", whose two halves contradict each
                # other. The first is honest; the second asserts a fact nobody
                # established.
                text += " (no request reached the source)"
            events.append(_event(started, text, kind="failed"))

    if item.get("completed_at"):
        events.append(_event(item["completed_at"],
                             item.get("last_message") or "Finished", kind="ok"))
    elif item.get("cancelled_at"):
        events.append(_event(item["cancelled_at"], "Cancelled", kind="skipped"))

    events.sort(key=lambda e: (e["at"] is None, str(e["at"])))
    return events


def item_history(item: Mapping[str, Any], attempts: List[Mapping[str, Any]],
                 *, trusted_from: str) -> Dict[str, Any]:
    """Everything the detail sheet needs, already in display terms."""
    attempt_count = int(item.get("attempt_count") or 0)
    automated = int(item.get("automated_retry_count") or 0)
    return {
        "item_uuid": item.get("item_uuid"),
        "title": item.get("title"),
        "year": item.get("year"),
        "state": item.get("state"),
        "state_label": describe_state(item.get("state")),
        # The direct link the owner asked for. Named source_url to match what
        # the downloads list already calls this concept.
        "source_url": item.get("canonical_url"),
        "added_at": item.get("created_at"),
        "links_grabbed_at": item.get("completed_at"),
        "last_tried_at": item.get("last_attempt_at"),
        "next_try_at": item.get("scheduled_for"),
        "cooldown_until": item.get("cooldown_until"),
        "tries": attempt_count,
        "automatic_retries": automated,
        # Say what is currently in the way, in one sentence, so the sheet does
        # not make the reader infer it from a badge.
        "current_reason": (describe_reason(item.get("last_reason_code"))
                           if item.get("last_reason_code") else None),
        "last_message": item.get("last_message"),
        "timeline": build_timeline(item, attempts, trusted_from=trusted_from),
        # Stated, not implied. An item with no trustworthy rows must not look
        # like an item that was never attempted.
        "detailed_history_from": trusted_from,
        "has_detailed_history": bool(attempts),
        # attempt_count counts every claim ever made, including before attempt
        # records existed -- so a gap between this and len(timeline) is expected
        # and is explained rather than hidden.
        "attempts_recorded": len(attempts),
    }

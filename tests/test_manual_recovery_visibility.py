"""A batch that will NEVER auto-resume must say so.

THE SILENT FOURTH STATE (peer review 2026-08-12). Three queries gate recovery
observability on ``auto_resume_after_cooldown = 1`` — the resume eligibility
query, ``_warn_exhausted_batches``, and the ``stuck`` query feeding
``_log_unresumable_batch``. So:

    enabled + budget available -> automatic recovery
    enabled + budget spent     -> exhaustion warning
    enabled + due but blocked  -> unresumable diagnostic
    verification hold          -> explicit hold diagnostic
    DISABLED + cooldown due    -> no path, no warning, no diagnostic

Observed in production 2026-08-10..12: a batch sat with an expired cooldown and
one deferred item for two days, retry budget UNTOUCHED (so not exhaustion),
reported by nothing. A manual retry completed it immediately.

The invariant restored here: every nonterminal ``paused_source`` batch is
automatically recoverable, deliberately safety-held, or durably reported as
needing a human. Configuration may choose WHICH branch applies; it may not
create a silent fourth state.
"""
import logging

from unittest.mock import MagicMock

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService

EXPIRED = "2000-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
STAMP = "2026-08-12T04:00:00+00:00"


def _items(n, prefix="release"):
    # DISTINCT URLs PER BATCH. schedule_batch raises DownloadQueueConflict when
    # every selected item is already active, keyed on (source, url,
    # service_type). A test that builds a second batch must therefore not reuse
    # the first batch's URLs — reusing them made the mixed-source test FLAKY
    # rather than merely order-dependent: whether the second batch collided
    # depended on which random item_uuid sorted first in _set_sources.
    return [{"url": "https://hdencode.org/%s-%d-2160p/" % (prefix, i),
             "title": "%s %d" % (prefix.title(), i), "media_type": "movie"}
            for i in range(n)]


def _rig(db, *, auto_resume, count=2, prefix="release"):
    fake = MagicMock()
    service = DownloadQueueService({}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(_items(count, prefix), interval_minutes=0,
                                   mode="immediate",
                                   auto_resume_after_cooldown=auto_resume)
    return service, batch["batch_uuid"]


def _park(db, service, batch_uuid, *, cooldown=EXPIRED, hold=None):
    """Park the batch exactly as a source-wide throttle parks one."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE download_queue_items "
            "SET state='waiting_source', queue_reason='source_deferred', "
            "    cooldown_until=?, last_reason_code='source_temporarily_blocked', "
            "    updated_at=? WHERE batch_uuid=? AND state NOT IN "
            "    ('completed','cancelled')",
            (cooldown, STAMP, batch_uuid))
        conn.execute(
            "UPDATE download_queue_batches SET state='paused_source', "
            "  cooldown_until=?, verification_hold_source=?, "
            "  last_reason_code='source_temporarily_blocked' "
            "WHERE batch_uuid=?", (cooldown, hold, batch_uuid))


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_manual_only_parked_batch_is_reported(tmp_path, caplog):
    """THE bug: auto-resume disabled + cooldown due was reported by nothing."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog) if batch_uuid in m]
    assert msgs, "a batch that can never auto-resume was reported by nothing"
    assert any("manual_recovery_required" in m for m in msgs)


def test_it_is_not_reported_as_budget_exhaustion(tmp_path, caplog):
    """'never permitted' and 'permitted and spent' are different causes needing
    different operator responses, so they must not share a reason."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog) if batch_uuid in m]
    assert msgs
    assert not any("auto_resume_budget_exhausted" in m for m in msgs)


def test_it_does_not_authorise_a_retry(tmp_path, caplog):
    """The diagnostic reports; it must not promote anything. The flag is a
    deliberate policy choice, not a fault to be auto-corrected."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    states = {r["state"] for r in db._query_dicts(
        "SELECT state FROM download_queue_items WHERE batch_uuid = ?",
        (batch_uuid,), default=[])}
    assert states == {"waiting_source"}, "nothing may be promoted by a diagnostic"
    row = db._query("SELECT state FROM download_queue_batches WHERE batch_uuid = ?",
                    (batch_uuid,), one=True, default=None)
    assert row["state"] == "paused_source"


def test_a_verification_hold_is_not_re_reported_as_manual_recovery(tmp_path, caplog):
    """A deliberate hold already has its own diagnostic and its own release rule."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid, hold="hdencode")

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog) if batch_uuid in m]
    assert not any("manual_recovery_required" in m for m in msgs), \
        "a held batch must be described as held, not as manual recovery"


def test_not_reported_before_the_cooldown_is_due(tmp_path, caplog):
    """Fail closed on noise: a batch still inside its cooldown is not parked."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid, cooldown=FUTURE)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    assert not any("manual_recovery_required" in m
                   for m in _warnings(caplog) if batch_uuid in m)


def test_reported_once_per_parking_episode(tmp_path, caplog):
    """Said once, not on every worker poll."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()
        service._maybe_auto_resume()
        service._maybe_auto_resume()

    hits = [m for m in _warnings(caplog)
            if batch_uuid in m and "manual_recovery_required" in m]
    assert len(hits) == 1, "the diagnostic must not repeat on every poll"


def _set_sources(db, batch_uuid, mapping):
    """Give the batch's deferred rows distinct sources (mixed-source batch).

    Ordered by URL, NOT by item_uuid: uuids are random, so ordering by them made
    which row became which source a coin flip — and with it whether a later
    batch's URL collided with an active (source, url) key. Deterministic input
    is the difference between a test and a lottery.
    """
    rows = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid = ? "
        "ORDER BY canonical_url", (batch_uuid,), default=[])
    with db.transaction() as conn:
        for row, source in zip(rows, mapping):
            conn.execute("UPDATE download_queue_items SET source = ? "
                         "WHERE item_uuid = ?", (source, row["item_uuid"]))


def test_a_held_source_does_not_mask_an_unrelated_source_in_the_same_batch(
        tmp_path, caplog):
    """PEER REVIEW ROUND 2, MEDIUM BLOCKER — and the same bug class this
    diagnostic exists to remove.

    The first version skipped the WHOLE batch when ANY deferred source was held.
    A verification hold is SOURCE-scoped, so in a mixed batch one held source
    erased an unrelated, unheld, overdue group's disposition — an adjacent
    aggregate condition creating a silent branch over actionable work.
    """
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False, count=2)
    _set_sources(db, batch_uuid, ["hdencode", "othersite"])
    _park(db, service, batch_uuid)
    # A DIFFERENT batch puts 'hdencode' under a verification hold, so hdencode
    # is in held_sources while 'othersite' is not. Distinct URLs so this batch
    # cannot collide with the first one's still-active rows.
    _, held_batch = _rig(db, auto_resume=False, count=1, prefix="held")
    _park(db, service, held_batch, hold="hdencode")

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog)
            if batch_uuid in m and "manual_recovery_required" in m]
    assert any("othersite" in m for m in msgs), \
        "a held source masked an unrelated source group's disposition"
    assert not any("source hdencode" in m for m in msgs), \
        "the held group must stay with the hold diagnostic"


def test_an_unknown_outcome_child_is_affirmatively_surfaced(tmp_path, caplog):
    """PEER REVIEW ROUND 2 — generic boilerplate is not enough.

    The manual `_resume_batch(automated=False)` path selects deferred rows
    WITHOUT decide(), clears exactly these reason codes and makes the rows ready.
    So the natural operator sequence (see this warning -> press Resume batch) can
    retry a row whose delivery outcome is unknown. The diagnostic must say so
    when a child actually carries one.
    """
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False, count=2)
    _park(db, service, batch_uuid)
    rows = db._query_dicts(
        "SELECT item_uuid FROM download_queue_items WHERE batch_uuid = ? "
        "ORDER BY item_uuid", (batch_uuid,), default=[])
    with db.transaction() as conn:
        conn.execute("UPDATE download_queue_items "
                     "SET last_reason_code = 'operation_timeout_unknown' "
                     "WHERE item_uuid = ?", (rows[0]["item_uuid"],))

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog) if batch_uuid in m]
    assert any("adjudicate_before_retry" in m for m in msgs), \
        "an unknown-outcome child must be named, not collapsed into generic text"
    assert any("do NOT use plain batch resume" in m for m in msgs)


def test_no_adjudication_text_when_no_unknown_outcome_exists(tmp_path, caplog):
    """Control for the test above: the safety sentence must appear only when the
    condition is real, or it becomes boilerplate operators learn to ignore."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=False, count=2)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    msgs = [m for m in _warnings(caplog) if batch_uuid in m]
    assert msgs and any("manual_recovery_required" in m for m in msgs)
    assert not any("adjudicate_before_retry" in m for m in msgs)


def test_auto_resume_enabled_batches_are_left_to_the_existing_diagnostics(
        tmp_path, caplog):
    """Positive control / no double-reporting: an auto-resume-ENABLED batch is
    owned by the recovery path and the existing warnings, so the new diagnostic
    must stay silent about it. Without this, the test above could pass by
    reporting every parked batch."""
    db = DatabaseManager(str(tmp_path / "q.sqlite"))
    service, batch_uuid = _rig(db, auto_resume=True)
    _park(db, service, batch_uuid)

    with caplog.at_level(logging.WARNING):
        service._maybe_auto_resume()

    assert not any("manual_recovery_required" in m
                   for m in _warnings(caplog) if batch_uuid in m)

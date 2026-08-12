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


def _items(n):
    return [{"url": "https://hdencode.org/release-%d-2160p/" % i,
             "title": "Release %d" % i, "media_type": "movie"} for i in range(n)]


def _rig(db, *, auto_resume, count=2):
    fake = MagicMock()
    service = DownloadQueueService({}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(_items(count), interval_minutes=0,
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

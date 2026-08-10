"""The migration must not invent the evidence the classifier requires.

PEER REVIEW 2026-08-09. The first version searched for rows whose
`last_reason_code` was `reveal_verification_stalled` with
`transport_attempted = 1` and called those challenge triggers.

That is backwards. `reveal_verification_stalled` is exactly the code the runtime
classifier emits when a reveal stalled and there was NO active Turnstile
evidence. Treating it as proof of Turnstile fabricates the very fact the
classifier was rewritten to demand, and then writes the fabrication into the row
as `cause_code = turnstile_challenge_failed` -- a confident claim about a page
nobody looked at. It also gave one episode to EVERY parked batch for the source,
so an unrelated parked batch joined an incident it had nothing to do with.

Historical rows cannot be reclassified, because the evidence that would settle it
is gone. So the operator names the incident and the script does only what it is
told. These tests pin that it refuses everything else, and a manual smoke run is
not a substitute for them.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.database import DatabaseManager
from backend.queue_recovery_policy import (
    AUTHORISED, ItemFacts, SharedFacts, VERIFICATION_HOLD, decide,
)

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts"
             / "migrate_challenge_episode.py")
NOW = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
EXPIRED = (NOW - timedelta(hours=8)).isoformat()


def _seed(path, *, batches=1, per_batch=3):
    """Parked rows shaped like production's: one stalled trigger per batch."""
    db = DatabaseManager(str(path))                # runs the schema migration
    made = []
    with db.transaction() as conn:
        for b in range(batches):
            batch_uuid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO download_queue_batches (batch_uuid, mode, "
                "interval_seconds, state, source, total_items, deferred_items, "
                "auto_resume_after_cooldown, created_at, updated_at, "
                "cooldown_until) VALUES (?, 'staggered', 600, 'paused_source', "
                "'hdencode', ?, ?, 1, ?, ?, ?)",
                (batch_uuid, per_batch, per_batch, EXPIRED, EXPIRED, EXPIRED))
            items = []
            for i in range(per_batch):
                item_uuid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO download_queue_items (item_uuid, batch_uuid, "
                    "sequence_number, source, canonical_url, title, "
                    "service_type, queue_reason, state, cooldown_until, "
                    "last_reason_code, transport_attempted, created_at, "
                    "updated_at) VALUES (?, ?, ?, 'hdencode', ?, ?, "
                    "'Rapidgator', 'source_deferred', 'waiting_source', ?, ?, "
                    "?, ?, ?)",
                    (item_uuid, batch_uuid, i,
                     f"https://hdencode.org/b{b}-r{i}/", f"B{b} Release {i}",
                     EXPIRED,
                     "reveal_verification_stalled" if i == 0
                     else "source_temporarily_blocked",
                     1 if i == 0 else 0, EXPIRED, EXPIRED))
                items.append(item_uuid)
            made.append((batch_uuid, items))
    return db, made


def _run(db_path, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, "--db", str(db_path), *args],
        capture_output=True, text=True)


def _verdicts(db):
    """Judge through the PRODUCTION policy, not by reading columns back.

    Asserting the UPDATE ran only proves the UPDATE ran.
    """
    rows = db._query_dicts(
        "SELECT i.item_uuid, i.state, i.queue_reason, i.cooldown_until, "
        "       (SELECT COUNT(*) FROM download_queue_batches eb "
        "         WHERE eb.challenge_episode_id IS NOT NULL "
        "           AND EXISTS (SELECT 1 FROM download_queue_items ei "
        "                        WHERE ei.batch_uuid = eb.batch_uuid "
        "                          AND ei.source = i.source)) AS open_ep "
        "FROM download_queue_items i", (), default=[])
    out = {}
    for row in rows:
        out[row["item_uuid"]] = decide(
            ItemFacts(state=row["state"],
                      cooldown_until=datetime.fromisoformat(
                          row["cooldown_until"]),
                      queue_reason=row["queue_reason"]),
            SharedFacts(cooldown_until=None, auto_resume_enabled=True,
                        attempts_used=0,
                        challenge_open=bool(row["open_ep"])),
            NOW)
    return out


class TestItRefusesToGuess:

    def test_it_will_not_run_without_an_explicit_trigger(self, tmp_path):
        db, _made = _seed(tmp_path / "guess.db")
        try:
            result = _run(tmp_path / "guess.db", "--apply")
            assert result.returncode != 0
            assert "--trigger is required" in result.stderr
            # And nothing was written.
            assert set(_verdicts(db).values()) == {AUTHORISED}
        finally:
            db.close()

    def test_a_stalled_row_is_not_treated_as_turnstile_evidence(self, tmp_path):
        """The heart of the finding: `reveal_verification_stalled` means the
        classifier found NO active challenge evidence. It can never be promoted
        to proof of one."""
        db, _made = _seed(tmp_path / "noinfer.db")
        try:
            result = _run(tmp_path / "noinfer.db")
            assert result.returncode != 0
            assert "reveal_verification_stalled" in result.stderr
        finally:
            db.close()

    def test_an_unknown_item_is_refused_rather_than_skipped(self, tmp_path):
        """A typo'd or already-resumed id would otherwise silently shrink the
        incident while the run still reported success."""
        db, _made = _seed(tmp_path / "typo.db")
        try:
            result = _run(tmp_path / "typo.db", "--trigger", str(uuid.uuid4()),
                          "--apply")
            assert result.returncode != 0
            assert "not a parked" in result.stderr
            assert set(_verdicts(db).values()) == {AUTHORISED}
        finally:
            db.close()


class TestItIsIncidentScoped:

    def test_an_unrelated_parked_batch_does_not_join_the_episode(self, tmp_path):
        """The over-reach: naming one incident used to hold every parked batch
        for the source."""
        db, made = _seed(tmp_path / "scoped.db", batches=2)
        try:
            (batch_a, items_a), (_batch_b, items_b) = made
            result = _run(tmp_path / "scoped.db", "--trigger", items_a[0],
                          "--apply")
            assert result.returncode == 0, result.stderr
            verdicts = _verdicts(db)
            # NOTE the episode hold is source-scoped by design, so batch B's
            # rows ARE held once an episode is open for hdencode. What must not
            # happen is batch B being written into the incident itself.
            episodes = db._query_dicts(
                "SELECT batch_uuid FROM download_queue_batches "
                "WHERE challenge_episode_id IS NOT NULL", (), default=[])
            assert [row["batch_uuid"] for row in episodes] == [batch_a], (
                "an unrelated parked batch was written into the incident")
            # And no row in batch B was relabelled as a challenge trigger.
            relabelled = db._query_dicts(
                "SELECT item_uuid FROM download_queue_items "
                "WHERE queue_reason = 'interactive_challenge'", (), default=[])
            assert [row["item_uuid"] for row in relabelled] == [items_a[0]]
            assert verdicts[items_b[0]] == VERIFICATION_HOLD
        finally:
            db.close()

    def test_the_named_trigger_and_its_siblings_are_held(self, tmp_path):
        db, made = _seed(tmp_path / "held.db")
        try:
            _batch, items = made[0]
            before = _verdicts(db)
            assert set(before.values()) == {AUTHORISED}, (
                f"the control is vacuous -- these rows were not releasable: "
                f"{before}")

            assert _run(tmp_path / "held.db", "--trigger", items[0],
                        "--apply").returncode == 0
            after = _verdicts(db)
            assert set(after.values()) == {VERIFICATION_HOLD}
        finally:
            db.close()

    def test_a_dry_run_writes_nothing(self, tmp_path):
        db, made = _seed(tmp_path / "dry.db")
        try:
            _batch, items = made[0]
            result = _run(tmp_path / "dry.db", "--trigger", items[0])
            assert result.returncode == 0
            assert "DRY RUN" in result.stdout
            assert set(_verdicts(db).values()) == {AUTHORISED}
        finally:
            db.close()

    def test_cooldowns_are_left_intact(self, tmp_path):
        """Nulling them "for safety" would make the siblings NO_AUTHORISATION
        forever and block the legitimate release after a probe succeeds."""
        db, made = _seed(tmp_path / "cooldowns.db")
        try:
            _batch, items = made[0]
            _run(tmp_path / "cooldowns.db", "--trigger", items[0], "--apply")
            rows = db._query_dicts(
                "SELECT cooldown_until FROM download_queue_items", (),
                default=[])
            assert all(row["cooldown_until"] == EXPIRED for row in rows)
        finally:
            db.close()

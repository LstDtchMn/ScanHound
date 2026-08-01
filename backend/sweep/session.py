"""Sweep session lifecycle: leases, continuation, atomic watermark commit.

The I/O half of the hybrid sweep. `completion.py` decides *whether* a sweep is
done; this decides *what is durably recorded* as a result.

Three invariants, each of which an earlier design got wrong:

1. `coverage_through` is the ORIGINAL session start `S`, preserved across every
   continuation, restart and lease reacquisition. Resetting it at the page cap
   would let a long crawl claim coverage of an interval it never traversed.

2. Continuation resumes from a TIMESTAMP + ANCHOR URL, never a page number.
   Pages shift as new posts publish, so "resume at page 7" silently skips
   whatever moved onto page 6 in the meantime.

3. The watermark advances ONLY on conjunctive completion with everything
   durably persisted, in a single transaction. A partial write must leave the
   prior coverage exactly as it was.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

CANONICALIZER_VERSION = "1"
DEFAULT_LEASE_SECONDS = 1800


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


class SweepLeaseError(RuntimeError):
    """Another worker holds a live lease on this source."""


@dataclass
class SweepSession:
    uuid: str
    source_key: str
    started_at: dt.datetime          # S — never reassigned
    stop_target: Optional[dt.datetime]
    overlap_hours: float
    attempt_count: int
    continuation_frontier_at: Optional[dt.datetime] = None
    continuation_anchor_url: Optional[str] = None
    pages_crawled: int = 0

    @property
    def is_continuation(self) -> bool:
        return self.attempt_count > 1


class SweepSessionStore:
    """All sweep persistence. Callers hand in a live sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection, *, owner: str = "scanhound"):
        self.conn = conn
        self.owner = owner

    # ── coverage ────────────────────────────────────────────────────────
    def coverage_through(self, source_key: str) -> Optional[dt.datetime]:
        row = self.conn.execute(
            "SELECT coverage_through FROM hdencode_source_coverage WHERE source_key=?",
            (source_key,),
        ).fetchone()
        return _parse(row[0]) if row else None

    def bootstrap_complete(self, source_key: str) -> bool:
        row = self.conn.execute(
            "SELECT bootstrap_complete FROM hdencode_source_coverage WHERE source_key=?",
            (source_key,),
        ).fetchone()
        return bool(row and row[0])

    # ── session lifecycle ───────────────────────────────────────────────
    def begin(
        self,
        source_key: str,
        *,
        overlap_hours: float = 6.0,
        bootstrap_hours: float = 30.0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: Optional[dt.datetime] = None,
    ) -> SweepSession:
        """Start a new session, or RESUME the existing one for this source.

        Resuming rather than starting fresh is what preserves `S`. A restart
        mid-sweep must continue the same logical session, not begin a second one
        that would commit a later coverage boundary than was actually proven.
        """
        now = now or _now()

        live = self.conn.execute(
            "SELECT sweep_session_uuid, started_at, stop_target, overlap_hours, "
            "       attempt_count, continuation_frontier_at, continuation_anchor_url, "
            "       pages_crawled, lease_owner, lease_expires_at "
            "FROM hdencode_sweep_sessions "
            "WHERE source_key=? AND terminal_status IS NULL",
            (source_key,),
        ).fetchone()

        if live:
            expires = _parse(live[9])
            if expires and expires > now and live[8] != self.owner:
                raise SweepLeaseError(
                    f"{source_key}: lease held by {live[8]} until {live[9]}"
                )
            # Same owner, or an expired lease we may take over. Either way this
            # is a CONTINUATION: attempt_count increments, started_at does not.
            self.conn.execute(
                "UPDATE hdencode_sweep_sessions SET attempt_count=attempt_count+1, "
                "       lease_owner=?, lease_expires_at=? WHERE sweep_session_uuid=?",
                (self.owner, _iso(now + dt.timedelta(seconds=lease_seconds)), live[0]),
            )
            return SweepSession(
                uuid=live[0], source_key=source_key, started_at=_parse(live[1]),
                stop_target=_parse(live[2]), overlap_hours=live[3],
                attempt_count=(live[4] or 1) + 1,
                continuation_frontier_at=_parse(live[5]),
                continuation_anchor_url=live[6], pages_crawled=live[7] or 0,
            )

        prior = self.coverage_through(source_key)
        if prior is None:
            # Bootstrap: derived as 24 h RED band + 6 h overlap, not chosen.
            stop_target = now - dt.timedelta(hours=bootstrap_hours)
        else:
            stop_target = prior - dt.timedelta(hours=overlap_hours)

        session = SweepSession(
            uuid=str(uuid.uuid4()), source_key=source_key, started_at=now,
            stop_target=stop_target, overlap_hours=overlap_hours, attempt_count=1,
        )
        self.conn.execute(
            "INSERT INTO hdencode_sweep_sessions "
            "(sweep_session_uuid, source_key, started_at, prior_coverage_through, "
            " overlap_hours, stop_target, lease_owner, lease_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session.uuid, source_key, _iso(now), _iso(prior), overlap_hours,
             _iso(stop_target), self.owner,
             _iso(now + dt.timedelta(seconds=lease_seconds))),
        )
        return session

    # ── ledger ──────────────────────────────────────────────────────────
    def record_observations(self, session: SweepSession, posts) -> int:
        """Write listing observations. Returns how many were NEW to this source.

        Idempotent: re-running a page after a restart updates last_observed_at
        and cannot inflate the new-identity count, which is what makes replay
        safe.
        """
        now_iso = _iso(_now())
        new = 0
        for post in posts:
            existing = self.conn.execute(
                "SELECT 1 FROM listing_source_ledger WHERE source_key=? AND canonical_url=?",
                (session.source_key, post["canonical_url"]),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE listing_source_ledger SET last_observed_at=?, "
                    "       last_page_index=?, status_snapshot=? "
                    "WHERE source_key=? AND canonical_url=?",
                    (now_iso, post.get("page_index"), post.get("status"),
                     session.source_key, post["canonical_url"]),
                )
                continue
            new += 1
            self.conn.execute(
                "INSERT INTO listing_source_ledger "
                "(source_key, canonical_url, raw_url, canonicalizer_version, "
                " first_observed_at, last_observed_at, displayed_published_at, "
                " first_page_index, last_page_index, title_snapshot, "
                " status_snapshot, sweep_session_uuid, persisted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (session.source_key, post["canonical_url"], post.get("raw_url", ""),
                 CANONICALIZER_VERSION, now_iso, now_iso,
                 post.get("displayed_published_at"), post.get("page_index"),
                 post.get("page_index"), post.get("title"), post.get("status"),
                 session.uuid),
            )
        return new

    def known_urls(self, source_key: str) -> set:
        return {
            r[0] for r in self.conn.execute(
                "SELECT canonical_url FROM listing_source_ledger WHERE source_key=?",
                (source_key,),
            )
        }

    # ── terminal states ─────────────────────────────────────────────────
    def commit_success(self, session: SweepSession, *, discoveries: int = 0,
                       requests: int = 0, now: Optional[dt.datetime] = None) -> None:
        """Advance the watermark to S — atomically, or not at all."""
        now = now or _now()
        # Flush any pending ledger writes FIRST, then take the watermark in its
        # own immediate transaction. python-sqlite3 auto-opens a transaction on
        # DML, so BEGIN IMMEDIATE here would raise "cannot start a transaction
        # within a transaction" — caught by the tests.
        #
        # Committing the ledger separately is also the correct ORDER, not just a
        # workaround: persistence must precede the advance. If the watermark
        # commit then fails, the ledger rows survive (harmless — they are simply
        # re-observed next sweep) and coverage does NOT move. The forbidden
        # outcome is the reverse, and this ordering makes it impossible.
        if self.conn.in_transaction:
            self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "UPDATE hdencode_sweep_sessions SET terminal_status='complete', "
                "       completed_at=?, discoveries=?, request_count=?, "
                "       lease_owner=NULL, lease_expires_at=NULL "
                "WHERE sweep_session_uuid=?",
                (_iso(now), discoveries, requests, session.uuid),
            )
            self.conn.execute(
                "INSERT INTO hdencode_source_coverage "
                "(source_key, coverage_through, last_success_session_uuid, "
                " last_success_started_at, last_success_completed_at, last_attempt_at, "
                " bootstrap_complete, interval_state, consecutive_failures, updated_at) "
                "VALUES (?,?,?,?,?,?,1,'current',0,?) "
                "ON CONFLICT(source_key) DO UPDATE SET "
                "  coverage_through=excluded.coverage_through, "
                "  last_success_session_uuid=excluded.last_success_session_uuid, "
                "  last_success_started_at=excluded.last_success_started_at, "
                "  last_success_completed_at=excluded.last_success_completed_at, "
                "  last_attempt_at=excluded.last_attempt_at, "
                "  bootstrap_complete=1, interval_state='current', "
                "  consecutive_failures=0, updated_at=excluded.updated_at",
                (session.source_key, _iso(session.started_at), session.uuid,
                 _iso(session.started_at), _iso(now), _iso(now), _iso(now)),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def mark_incomplete(self, session: SweepSession, *, reason: str,
                        frontier_at: Optional[dt.datetime] = None,
                        anchor_url: Optional[str] = None,
                        pages_crawled: int = 0,
                        now: Optional[dt.datetime] = None) -> None:
        """Persist continuation state. `coverage_through` is NOT touched.

        The session stays non-terminal so the next attempt resumes it — that is
        how a backlog larger than the page cap eventually clears instead of
        restarting at page 1 forever.
        """
        now = now or _now()
        self.conn.execute(
            "UPDATE hdencode_sweep_sessions SET failure_reason=?, "
            "       continuation_frontier_at=?, continuation_anchor_url=?, "
            "       pages_crawled=?, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE sweep_session_uuid=?",
            (reason, _iso(frontier_at), anchor_url, pages_crawled, session.uuid),
        )
        self.conn.execute(
            "INSERT INTO hdencode_source_coverage "
            "(source_key, last_attempt_at, interval_state, consecutive_failures, updated_at) "
            "VALUES (?,?, 'incomplete', 1, ?) "
            "ON CONFLICT(source_key) DO UPDATE SET "
            "  last_attempt_at=excluded.last_attempt_at, interval_state='incomplete', "
            "  consecutive_failures=hdencode_source_coverage.consecutive_failures+1, "
            "  updated_at=excluded.updated_at",
            (session.source_key, _iso(now), _iso(now)),
        )
        self.conn.commit()

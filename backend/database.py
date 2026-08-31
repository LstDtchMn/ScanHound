"""Database Manager — SQLite persistence layer for ScanHound.

Provides thread-safe access to the application database with automatic
schema migration, connection recovery, and helper methods for all
subsystems (downloads, Plex cache, scan history).
"""

import json
import sqlite3
import os
import datetime
import logging

from backend.url_identity import canonicalize_listing_url
from backend import release_grammar
import time
import threading
import uuid
from contextlib import contextmanager

from backend.config import DB_PATH

logger = logging.getLogger(__name__)


class RenameJobDBError(Exception):
    """Raised by create_rename_job() when the INSERT genuinely fails at the DB
    layer (connection unavailable, disk error, etc). Distinct from the
    ordinary "already tracked" skip (RenameService._claim_path checks
    path_has_rename_job() *before* calling create_rename_job, so that case
    never reaches here) and from a malformed-job caller bug (missing
    original_path, which still returns None — the caller passed bad data,
    not a DB failure). Callers that need to tell "silently dropped due to a
    DB problem" apart from "legitimately skipped" should catch this."""


#: Diagnostic buckets in the shadow-miss integrity check that must never hold a
#: row without a matching explanation.
_MISS_DIAGNOSTIC_BUCKETS = ("unsupported", "corrupt")


def reconcile_bucket_reporting(per_cycle):
    """Every row counted as bad must have produced its own finding.

    Returns the findings for any cycle whose bucket count and reported count
    disagree. Empty means the accounting is consistent.

    WHY THIS IS A MODULE-LEVEL FUNCTION AND NOT AN INLINE LOOP. Round 5's version
    of this check lived inline and asked whether ANY integrity finding mentioned
    the cycle, which one unrelated finding satisfied for any number of unreported
    rows in the same cycle. Its test passed with the check deleted -- the test
    could not construct the state that reaches it, because production code always
    reports correctly, so there was nothing to detect. I recorded that rather than
    reword it.

    Pulling it out makes the state constructible: the reviewer's case
    (unsupported=2 with one reported, corrupt=1 with one reported) is now three
    lines of a dict, and the assertion is that exactly ONE finding appears --
    naming the unsupported bucket -- despite a corrupt finding already existing
    for that same cycle. That is the case the string match got wrong.

    A shortfall can only arise if a branch increments a bucket without going
    through the reporting helper, which is exactly the mistake being guarded.
    """
    findings = []
    for cycle, slot in per_cycle.items():
        for bucket in _MISS_DIAGNOSTIC_BUCKETS:
            counted = int(slot.get(bucket) or 0)
            reported = int(slot.get("reported_" + bucket) or 0)
            shortfall = counted - reported
            if shortfall > 0:
                findings.append(f"unreported_{bucket}_rows:{cycle}:{shortfall}")
            elif shortfall < 0:
                # More explanations than bad rows means this accounting is itself
                # wrong, which is no better than the bug it looks for.
                findings.append(
                    f"overreported_{bucket}_rows:{cycle}:{-shortfall}")
    return findings


class DatabaseManager:
    """Thread-safe SQLite database manager with connection pooling and auto-recovery."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = None
        self._lock = threading.RLock()  # Reentrant lock for thread-safe DB access
        self._init_depth = 0  # Guard against infinite recursion during recovery
        # Monotonic in-process revision, bumped on every background-cache write.
        # Folded into get_background_cache_version() so the parse-cache token
        # changes on EVERY write, immune to CURRENT_TIMESTAMP's 1s resolution
        # (a same-second in-place upsert would otherwise serve stale blobs).
        self._bg_cache_rev = 0
        self._dismissed_cache = None  # lazily-populated set[str], kept in sync by mutators
        self.init_db()

    # ── Core helpers ──────────────────────────────────────────────────

    @contextmanager
    def transaction(self):
        """Context manager providing a locked, auto-committed database connection.

        Commits on clean exit, rolls back on exception.

        Use for external code that needs direct SQL access:
            with db.transaction() as conn:
                if conn:
                    conn.execute("DELETE FROM ...")
        """
        with self._lock:
            conn = self.get_connection()
            try:
                yield conn
                if conn:
                    conn.commit()
            except Exception:
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise

    def close(self):
        """Close the database connection and release resources."""
        with self._lock:
            if self.conn:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
                self.conn = None

    def get_connection(self):
        """Get or create a database connection (thread-safe).

        Uses WAL journal mode for better concurrent read/write performance, a
        5-second busy timeout to handle contention gracefully, and
        synchronous=NORMAL (safe — and the recommended setting — under WAL:
        SQLite still fsyncs at every checkpoint, so a NORMAL-mode DB can't be
        corrupted by an application crash; only a power loss/OS crash on a
        non-durable filesystem/volume can lose the last few committed
        transactions, which is an acceptable, documented trade-off for the
        write-throughput win).
        """
        with self._lock:
            if not self.conn:
                try:
                    self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    self.conn.row_factory = sqlite3.Row
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    self.conn.execute("PRAGMA synchronous=NORMAL")
                    self.conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.Error as e:
                    logger.error("Database connection failed: %s", e)
            return self.conn

    def checkpoint(self):
        """Fold the WAL back into the main DB file (PRAGMA wal_checkpoint(TRUNCATE)).

        Keeps the -wal sidecar from growing unbounded and minimizes the
        window of data that only exists in the WAL (relevant on a
        non-durable bind-mounted filesystem). Called once after startup
        init; periodic scheduling is a follow-up (see db-reliability report
        — there's no existing periodic-task hook this layer can reach
        without introducing a scheduler dependency here).
        """
        with self._lock:
            conn = self.get_connection()
            if not conn:
                return False
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return True
            except sqlite3.Error as e:
                logger.error("WAL checkpoint failed: %s", e)
                return False

    def _query(self, sql, params=(), *, one=False, default=None):
        """Execute a read query under lock.

        Args:
            sql: SQL SELECT statement.
            params: Query parameters.
            one: If True, return a single row instead of all rows.
            default: Value to return on failure.

        Returns:
            Query results, a single row, or default on failure.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return default
                cursor = conn.cursor()
                cursor.execute(sql, params)
                if one:
                    return cursor.fetchone()
                return cursor.fetchall()
        except Exception as e:
            logger.error("DB query error: %s", e)
            return default

    def _query_dicts(self, sql, params=(), *, default=None):
        """Execute a read query and return results as a list of dicts.

        Convenience wrapper around _query for methods that need dict rows.
        """
        rows = self._query(sql, params, default=default if default is not None else [])
        if rows is None:
            return default if default is not None else []
        try:
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error("DB row conversion error: %s", e)
            return default if default is not None else []

    def _mutate(self, sql, params=(), *, label="mutate"):
        """Execute a write query under lock with commit.

        Args:
            sql: SQL INSERT/UPDATE/DELETE statement.
            params: Query parameters.
            label: Human-readable label for error logging.

        Returns:
            True on success, False on failure.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.execute(sql, params)
                conn.commit()
            return True
        except Exception as e:
            logger.error("DB Error (%s): %s", label, e)
            return False

    def _insert_returning_id(self, sql, params=(), *, label="insert"):
        """Execute an INSERT and return the new row's ID, or None on failure.

        Args:
            sql: SQL INSERT statement.
            params: Query parameters.
            label: Human-readable label for error logging.

        Returns:
            The lastrowid on success, None on failure.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return None
                cursor = conn.cursor()
                cursor.execute(sql, params)
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error("DB Error (%s): %s", label, e)
            return None

    # ── Schema initialization ────────────────────────────────────────

    # Schema version — increment when migrations are added.
    SCHEMA_VERSION = 9

    @staticmethod
    def _mark_existing_challenge_pauses_held(cursor):
        """v9: put pre-existing INTERACTIVE-CHALLENGE pauses under the hold.

        NARROWED on round-2 review (finding 3). The first version also swept
        `reveal_verification_stalled` batches and took the held source from the
        first deferred child by sequence. Both were wrong:

          * `reveal_verification_stalled` is explicitly NOT Turnstile evidence —
            it is the runtime classifier's fallback for a not-ready reveal with
            NO active challenge — so retro-labelling it a human challenge
            contradicts the very rule this change adds. A `user_version` gate
            bounds how OFTEN the inference runs, not WHICH rows it is valid for.
          * the first deferred child can be a different source than the one that
            hit the challenge (a mixed batch: seq0 DDLBase, seq1 HDEncode), so
            the wrong source could be held.

        So this migrates ONLY a batch that carries a genuine challenge TRIGGER
        row — an item in `verification_required` with
        `queue_reason='interactive_challenge'` — and takes the held source from
        THAT row, which is by construction the source that hit the challenge.
        A batch without such a row is left alone; if its reveal later stalls on
        a live Turnstile, the runtime classifier holds it then, on real
        evidence. This is the schema migration doing only what the schema can
        prove. It is written at the batch (the level the resume machinery reads)
        so old item cooldowns and auto-resume flags cannot reschedule the rows.
        """
        cursor.execute(
            """
            UPDATE download_queue_batches
            SET verification_hold_source = (
                SELECT i.source FROM download_queue_items i
                WHERE i.batch_uuid = download_queue_batches.batch_uuid
                  AND i.state = 'verification_required'
                  AND i.queue_reason = 'interactive_challenge'
                ORDER BY i.sequence_number LIMIT 1
            )
            WHERE state = 'paused_source'
              AND verification_hold_source IS NULL
              AND EXISTS (
                SELECT 1 FROM download_queue_items i
                WHERE i.batch_uuid = download_queue_batches.batch_uuid
                  AND i.state = 'verification_required'
                  AND i.queue_reason = 'interactive_challenge'
              )
            """
        )

    def init_db(self):
        """Initialize database tables and run schema migrations.

        Handles corrupt databases by backing up the file and creating a
        fresh database automatically.
        """
        # Hold RLock for entire init to prevent concurrent migrations.
        # RLock is reentrant so nested get_connection() and recovery init_db() work.
        with self._lock:
            if self._init_depth > 1:
                logger.critical("Database init recursion limit reached. Giving up.")
                return
            self._init_depth += 1
            try:
                conn = self.get_connection()
                if not conn:
                    return

                cursor = conn.cursor()

                # ── Startup integrity check ──────────────────────────────
                # Explicit check (not just relying on a CREATE TABLE happening
                # to raise) so a corrupt DB is caught even if every table
                # already exists and no DDL runs this session.
                cursor.execute("PRAGMA integrity_check")
                integrity_result = cursor.fetchone()[0]
                if integrity_result != "ok":
                    raise sqlite3.DatabaseError(
                        f"integrity_check failed: {integrity_result}")

                # ── Read current schema version ──────────────────────────
                cursor.execute("PRAGMA user_version")
                current_version = cursor.fetchone()[0]

                # ── Base tables (idempotent) ─────────────────────────────

                # 1. Downloads history
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS downloads (
                        url TEXT PRIMARY KEY,
                        title TEXT,
                        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Pipeline-tracker reconcile verdicts — one row per grab url,
                # persisted so 'verified' is terminal and Dismiss survives
                # even after the underlying stage rows age out. See
                # docs/superpowers/specs/2026-07-10-pipeline-tracker-design.md.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pipeline_verdicts (
                        url TEXT PRIMARY KEY REFERENCES downloads(url),
                        category TEXT,
                        detail TEXT,
                        package_uuid TEXT,
                        excluded_uuid TEXT,
                        plex_rating_key TEXT,
                        checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        dismissed INTEGER DEFAULT 0
                    )
                ''')

                # 2. Plex cache
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS plex_cache (
                        key TEXT PRIMARY KEY,
                        title TEXT,
                        original_title TEXT,
                        year INTEGER,
                        res TEXT,
                        size REAL,
                        imdb_id TEXT,
                        rating_key TEXT,
                        media_id TEXT,
                        is_tv BOOLEAN,
                        season INTEGER,
                        episode_count INTEGER,
                        content_type TEXT,
                        dovi BOOLEAN,
                        hdr BOOLEAN,
                        last_updated TIMESTAMP,
                        library_name TEXT,
                        file_path TEXT
                    )
                ''')

                # 3. Scan history
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS scan_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        scan_type TEXT NOT NULL,
                        items_scanned INTEGER DEFAULT 0,
                        missing_count INTEGER DEFAULT 0,
                        upgrade_count INTEGER DEFAULT 0,
                        dv_upgrade_count INTEGER DEFAULT 0,
                        in_library_count INTEGER DEFAULT 0,
                        duration_seconds REAL DEFAULT 0,
                        sources_scanned TEXT,
                        plex_items_cached INTEGER DEFAULT 0
                    )
                ''')

                # 4. Scanned URLs — for incremental scan tracking
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS scanned_urls (
                        url TEXT PRIMARY KEY,
                        title TEXT,
                        source TEXT,
                        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Maps each scraped file-host link (rapidgator/etc) to the
                # movie/show it belongs to, so a broken/blocked link in
                # JDownloader can be traced back to its title.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS scraped_link_map (
                        link TEXT PRIMARY KEY,
                        title TEXT,
                        resolution TEXT,
                        source_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Items the user swiped away ("skip") in the mobile deck. Kept
                # so dismissed releases stay hidden on future scans. Keyed by
                # release URL.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS dismissed_items (
                        url TEXT PRIMARY KEY,
                        title TEXT,
                        dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Title-level skip context: group_key + the skipped release's
                # quality, so a same-or-lower version of a skipped title stays
                # hidden while a genuine upgrade (higher res / DV gain) can still
                # surface. Added via idempotent ALTERs for existing DBs.
                for _col, _decl in (("group_key", "TEXT"), ("resolution", "TEXT"), ("dovi", "INTEGER")):
                    try:
                        cursor.execute(f"ALTER TABLE dismissed_items ADD COLUMN {_col} {_decl}")
                    except sqlite3.OperationalError as e:
                        # Only tolerate "already exists"; re-raise a real failure
                        # (locked / disk I/O) so we don't leave the column missing
                        # and then blow up later in add_dismissed_items.
                        if "duplicate column" not in str(e).lower():
                            raise

                # Durable per-package download + extraction outcome, polled from
                # JDownloader. Keyed by JD package name so the row survives even
                # after the package is cleared from JDownloader's list.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS download_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        package_uuid TEXT,
                        name TEXT,
                        title TEXT,
                        host TEXT,
                        bytes_total INTEGER DEFAULT 0,
                        bytes_loaded INTEGER DEFAULT 0,
                        downloaded INTEGER DEFAULT 0,
                        extraction TEXT DEFAULT 'na',
                        state TEXT DEFAULT 'queued',
                        error TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # ── download_results: name-PK → surrogate-id rebuild (once) ──
                # Guarded, crash-safe, and self-contained: a failure raises
                # RuntimeError (NOT sqlite3.*Error), so it can never reach the
                # corrupt-DB quarantine below (which would wipe the whole DB).
                dr_cols = {r[1] for r in cursor.execute("PRAGMA table_info(download_results)")}
                if dr_cols and "id" not in dr_cols:
                    try:
                        cursor.execute("DROP TABLE IF EXISTS download_results_new")
                        if conn.in_transaction:
                            conn.commit()
                        cursor.execute("BEGIN IMMEDIATE")
                        cursor.execute('''
                            CREATE TABLE download_results_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                package_uuid TEXT, name TEXT, title TEXT, host TEXT,
                                bytes_total INTEGER DEFAULT 0, bytes_loaded INTEGER DEFAULT 0,
                                downloaded INTEGER DEFAULT 0, extraction TEXT DEFAULT 'na',
                                state TEXT DEFAULT 'queued', error TEXT,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
                        ''')
                        cursor.execute('''
                            INSERT INTO download_results_new
                                (package_uuid, name, title, host, bytes_total, bytes_loaded,
                                 downloaded, extraction, state, error, updated_at)
                            SELECT NULL, name, title, host, bytes_total, bytes_loaded,
                                   downloaded, extraction, state, error, updated_at
                            FROM download_results
                        ''')
                        cursor.execute("DROP TABLE download_results")
                        cursor.execute("ALTER TABLE download_results_new RENAME TO download_results")
                        conn.commit()
                    except Exception as e:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        logger.exception("download_results rebuild failed")
                        raise RuntimeError("download_results migration failed") from e

                # Admin password (single row) for browser / self-hosted auth.
                # bcrypt hash only — never the plaintext. Absent row = no
                # password set, so password auth is off.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS auth_credentials (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        password_hash TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Issued login sessions, keyed by the SHA-256 hash of the
                # bearer token (never the token itself). Rows are purged on
                # expiry and wiped wholesale when the password changes.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        token_hash TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TEXT NOT NULL
                    )
                ''')

                # Pre-cached scrape results from the background scanner, so the
                # app can open with results already populated (they survive a
                # restart, unlike the in-memory live scan). Keyed by release
                # URL; ``data`` is the full serialized result dict so cached
                # rows render identically to live ones.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS background_scan_cache (
                        url TEXT PRIMARY KEY,
                        title TEXT,
                        year INTEGER,
                        status TEXT,
                        source_category TEXT,
                        data TEXT,
                        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Current source-health snapshot — one row per source.
                # Detailed request events remain intentionally out of scope.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS source_health (
                        source TEXT PRIMARY KEY,
                        state TEXT NOT NULL DEFAULT 'unknown',
                        reason_code TEXT,
                        updated_at TEXT NOT NULL,
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        cooldown_until TEXT
                    )
                ''')

                # Auto-rename tracking: one row per extracted media file, with
                # the identified match, confidence, and rename/move outcome.
                # Modeled on Nomen's file_manager table. Statuses: pending,
                # matched, needs_review, applied, failed, reverted.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS rename_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        package_name TEXT,
                        original_path TEXT NOT NULL,
                        original_filename TEXT,
                        new_filename TEXT,
                        destination_path TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        media_type TEXT,
                        title TEXT,
                        year INTEGER,
                        season INTEGER,
                        episode INTEGER,
                        tmdb_id INTEGER,
                        imdb_id TEXT,
                        poster_path TEXT,
                        resolution TEXT,
                        match_confidence REAL,
                        match_source TEXT,
                        move_method TEXT,
                        proposed_match TEXT,
                        plex_sort_title TEXT,
                        warning_message TEXT,
                        suggested_correction TEXT,
                        combined_episode TEXT,
                        split_file TEXT,
                        error_message TEXT,
                        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        processed_at TIMESTAMP,
                        reverted_at TIMESTAMP
                    )
                ''')

                # Dolby Vision layer inventory: one row per scanned file with
                # its detected enhancement-layer type (fel/mel/profile5/...).
                # Independent of rename_jobs so files that already live in the
                # library (no rename job) can be recorded and badged. Keyed by
                # container-view path; (sig_mtime, sig_size) is the change-signal
                # that lets a re-scan skip unchanged files.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS dv_scan (
                        path TEXT PRIMARY KEY,
                        title TEXT,
                        dv_layer TEXT,
                        sig_mtime REAL,
                        sig_size INTEGER,
                        source TEXT,
                        rating_key TEXT,
                        imdb_id TEXT,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # ffprobe result cache, keyed by path with a (mtime, size)
                # change-signal — mirrors dv_scan's invalidation shape exactly.
                # A cache MISS or STALE row means re-probe; a probe FAILURE is
                # never written here (the caller retries next time rather than
                # wedging a file into permanent "unknown").
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS media_probe (
                        path TEXT PRIMARY KEY,
                        sig_mtime REAL,
                        sig_size INTEGER,
                        probe_json TEXT,
                        probed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Durable 4K metadata inventory.  ``dv_scan`` and
                # ``media_probe`` remain compatibility caches; these tables
                # preserve the run/item history and the evidence needed to
                # distinguish a known negative from an unscanned or failed
                # file.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS dv_seed_baseline (
                        path TEXT PRIMARY KEY,
                        seed_layer TEXT NOT NULL,
                        title TEXT,
                        sig_mtime REAL,
                        sig_size INTEGER,
                        rating_key TEXT,
                        imdb_id TEXT,
                        seed_scanned_at TEXT,
                        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS metadata_scan_runs (
                        run_uuid TEXT PRIMARY KEY,
                        scope TEXT NOT NULL CHECK(scope IN ('pilot', 'full', 'targeted')),
                        status TEXT NOT NULL CHECK(status IN
                            ('queued', 'running', 'paused', 'cancelled',
                             'completed', 'failed', 'interrupted')),
                        expected_count INTEGER NOT NULL DEFAULT 0 CHECK(expected_count >= 0),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        started_at TEXT,
                        completed_at TEXT,
                        cancelled_at TEXT,
                        error_code TEXT,
                        error_message TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS metadata_scan_items (
                        run_uuid TEXT NOT NULL REFERENCES metadata_scan_runs(run_uuid)
                            ON DELETE CASCADE,
                        path TEXT NOT NULL,
                        library_name TEXT,
                        rating_key TEXT,
                        title TEXT,
                        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
                            ('pending', 'running', 'current', 'failed', 'skipped',
                             'cancelled', 'interrupted')),
                        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                        sig_mtime REAL,
                        sig_size INTEGER,
                        failure_stage TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        started_at TEXT,
                        completed_at TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (run_uuid, path)
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS media_inventory (
                        path TEXT PRIMARY KEY,
                        library_name TEXT,
                        rating_key TEXT,
                        title TEXT,
                        year INTEGER,
                        resolution TEXT,
                        hdr TEXT,
                        hdr10plus_state TEXT NOT NULL DEFAULT 'unknown' CHECK(
                            hdr10plus_state IN ('present', 'absent', 'unknown')),
                        dv_layer TEXT,
                        dv_profile TEXT,
                        scan_state TEXT NOT NULL DEFAULT 'unscanned' CHECK(scan_state IN
                            ('unscanned', 'current', 'stale', 'failed', 'source_changed')),
                        sig_mtime REAL,
                        sig_size INTEGER,
                        scan_run_uuid TEXT REFERENCES metadata_scan_runs(run_uuid),
                        probe_json TEXT,
                        last_scanned_at TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # The historic imported FEL/MEL list must never be overwritten
                # when a real local-file scan replaces a dv_scan compatibility
                # row for the same path.
                cursor.execute('''
                    INSERT OR IGNORE INTO dv_seed_baseline
                        (path, seed_layer, title, sig_mtime, sig_size,
                         rating_key, imdb_id, seed_scanned_at)
                    SELECT path, dv_layer, title, sig_mtime, sig_size,
                           rating_key, imdb_id, scanned_at
                    FROM dv_scan
                    WHERE source = 'seed'
                ''')

                # Per-title bookmarks (distinct from watchlist -- this is for
                # titles the user HAS already found and wants to remember, not
                # titles being searched-for). title_key is normalize_title(title),
                # stored so the fallback unique index doesn't need SQLite
                # expression-index support across all deployed versions.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS bookmarks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        imdb_id TEXT,
                        title TEXT NOT NULL,
                        title_key TEXT NOT NULL,
                        year INTEGER,
                        media_type TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_imdb '
                    'ON bookmarks(imdb_id) WHERE imdb_id IS NOT NULL')
                cursor.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmarks_title_key '
                    'ON bookmarks(title_key, year, media_type) WHERE imdb_id IS NULL')

                # ── Performance indexes (idempotent) ─────────────────────
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_imdb_id ON plex_cache(imdb_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_title ON plex_cache(title)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_tv_season ON plex_cache(is_tv, season)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_year ON plex_cache(year)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_res ON plex_cache(res)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_updated ON plex_cache(last_updated)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_date ON downloads(date_added)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_download_results_updated ON download_results(updated_at DESC)')
                cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_download_results_uuid '
                               'ON download_results(package_uuid) WHERE package_uuid IS NOT NULL')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_download_results_name '
                               'ON download_results(name)')
                # PACKAGE PROVENANCE, PERSISTED (peer review Finding 1, part 2).
                # The release a live package was PROVEN to belong to, by matching
                # the file-host links ScanHound recorded submitting. Persisted
                # rather than recomputed per request because the REST endpoint
                # reads this table, not the live JD poll -- only the poller holds
                # the child links, so without a column the two transports could
                # not agree. NULL means unproven, which renders as no link.
                try:
                    cursor.execute('ALTER TABLE download_results '
                                   'ADD COLUMN provenance_url TEXT')
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp ON scan_history(timestamp DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_bg_cache_last_seen ON background_scan_cache(last_seen_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_dv_scan_layer ON dv_scan(dv_layer)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_metadata_scan_runs_status '
                               'ON metadata_scan_runs(status, created_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_metadata_scan_items_status '
                               'ON metadata_scan_items(run_uuid, status, path)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_inventory_filters '
                               'ON media_inventory(library_name, resolution, hdr10plus_state, '
                               'dv_layer, scan_state)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_status ON rename_jobs(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_detected ON rename_jobs(detected_at DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pipeline_verdicts_category '
                               'ON pipeline_verdicts(category)')

                # ── Column migrations (guarded by "duplicate column name") ─
                _column_migrations = [
                    # R-4 derived-state versioning (round-10 model): version
                    # stamps + staleness live in DEDICATED columns, never only
                    # inside a JSON blob.
                    'ALTER TABLE background_scan_cache ADD COLUMN parse_version TEXT',
                    "ALTER TABLE background_scan_cache ADD COLUMN derived_state TEXT NOT NULL DEFAULT 'current'",
                    'ALTER TABLE downloads ADD COLUMN normalized_title TEXT',
                    'ALTER TABLE downloads ADD COLUMN season INTEGER',
                    'ALTER TABLE downloads ADD COLUMN resolution TEXT',
                    'ALTER TABLE downloads ADD COLUMN size TEXT',
                    "ALTER TABLE downloads ADD COLUMN status TEXT DEFAULT 'completed'",
                    'ALTER TABLE plex_cache ADD COLUMN library_name TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN suggested_correction TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN combined_episode TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN split_file TEXT',
                    # WHAT KIND OF THING THIS RELEASE IS, as RECORDED at grab
                    # time rather than inferred later. ScanHound already knows:
                    # its scan sources declare type movie/tv and MediaItem
                    # carries the resulting `category`. That fact was dropped at
                    # the download request and the annotator had to guess from
                    # `season is None`, which is not the same question -- a TV
                    # grab whose season never parsed reads as a movie, and two
                    # of its seasons then share one identity.
                    # 'movie' | 'tv' | NULL, where NULL means NOT RECORDED and
                    # must never be read as either.
                    'ALTER TABLE downloads ADD COLUMN media_kind TEXT',
                    'ALTER TABLE downloads ADD COLUMN hdr TEXT',
                    'ALTER TABLE downloads ADD COLUMN dovi INTEGER DEFAULT 0',
                    'ALTER TABLE rename_jobs ADD COLUMN poster_path TEXT',
                    # Human-readable reasons a match is < 100% (JSON list of
                    # strings) — surfaced in the Renames UI so a low-confidence
                    # match explains itself.
                    'ALTER TABLE rename_jobs ADD COLUMN match_reasons TEXT',
                    # Status a job had just before it was flipped to the transient
                    # 'applying' — so crash recovery restores needs_review (not a
                    # blanket 'matched' that would bypass the review gate).
                    'ALTER TABLE rename_jobs ADD COLUMN prior_status TEXT',
                    # Year makes the grab key year-aware (normalized|year|season)
                    # for send-time duplicate protection + the read-time overlay,
                    # so a 2021 remake never blocks/marks the 1984 original.
                    'ALTER TABLE downloads ADD COLUMN year INTEGER',
                    # Pipeline tracker join key + bookkeeping — the canonical
                    # JDownloader package-name string (see compute_package_name
                    # in download_service.py), the timestamp of the most recent
                    # grab attempt (bumped on every add_to_history call, success
                    # or not), and the source host used for that attempt.
                    'ALTER TABLE downloads ADD COLUMN package_name TEXT',
                    'ALTER TABLE downloads ADD COLUMN last_grabbed_at TIMESTAMP',
                    'ALTER TABLE downloads ADD COLUMN service_type TEXT',
                    # Structured conflict info for the desktop Renames "file
                    # already exists" resolution UI — kind of conflict
                    # detected and whether source/destination are same-size
                    # (drives the recommended action), instead of stuffing
                    # this into the free-text warning_message.
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_kind TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_same_size INTEGER',
                    # Raw byte sizes of the two files involved in a
                    # 'destination_exists' collision — lets the desktop Renames
                    # row render GB size chips instead of parsing them back out
                    # of warning_message's free-text byte counts.
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_existing_size INTEGER',
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_incoming_size INTEGER',
                    # Duplicate-quality-comparison feature: the full computed
                    # diff (existing vs incoming specs, recommendation) for
                    # BOTH same-path and library-wide duplicates — supersedes
                    # the three conflict_*_size columns above for row display
                    # (they're still written by service.py's execution-time
                    # collision handling, just no longer read by the UI).
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_analysis TEXT',
                    # The served path Plex reports for a movie (part.file) —
                    # plex_service.py already computes this per item; this
                    # column just stops discarding it, so a library-wide
                    # duplicate match (a different path than the incoming
                    # job's own destination) can be ffprobed directly.
                    'ALTER TABLE plex_cache ADD COLUMN file_path TEXT',
                    # Archiving is orthogonal to status: a nullable timestamp,
                    # not a new status value. NULL = active (default,
                    # excluded-by-default list_rename_jobs behavior); non-NULL
                    # = archived (set automatically on apply success, or
                    # manually via bulk archive/unarchive).
                    'ALTER TABLE rename_jobs ADD COLUMN archived_at TIMESTAMP',
                    # Two-pass confirmation timer for detect_moved_source_files():
                    # NULL normally; set to CURRENT_TIMESTAMP on the first
                    # maintenance pass that finds a needs_review/matched job's
                    # original_path missing, cleared if the file reappears, and
                    # left permanently set (for audit) once a SECOND consecutive
                    # miss confirms the file is genuinely gone and the job is
                    # archived. See detect_moved_source_files in rename/service.py.
                    'ALTER TABLE rename_jobs ADD COLUMN source_missing_since TIMESTAMP',
                    # Set by a 'replace_library_dup' apply: the (translated,
                    # container-local) path of the existing library file that
                    # was moved to recoverable trash so the downloaded copy
                    # could take its place. NULL for every other apply. Read by
                    # undo() to restore that exact file — it lives at a
                    # different path than dst, so the dst-keyed overwrite
                    # restore can't find it otherwise.
                    'ALTER TABLE rename_jobs ADD COLUMN conflict_replaced_path TEXT',
                ]
                for col_sql in _column_migrations:
                    try:
                        cursor.execute(col_sql)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" in str(e).lower():
                            pass  # Already exists — expected
                        else:
                            logger.warning("Migration failed: %s — %s", col_sql, e)

                # jd_confirmed_name: own guarded block (not the shared list
                # above) because its FIRST creation triggers a one-time
                # best-effort backfill from download_results history. JD
                # sanitizes punctuation (':' -> ';', etc.) before reporting a
                # package name, so this — not our computed package_name — is
                # the string download_results.name and rename_jobs.package_name
                # actually carry; matching prefers it when present. Fold-match
                # each legacy downloads row against download_results.name;
                # capture only unique matches (ambiguous legacy season-less
                # names are left NULL — they resolve via Re-grab, which now
                # sends season-aware names). NULL until captured; captured at
                # most once per row (see capture_jd_confirmed_names below,
                # which handles ongoing/post-backfill capture).
                try:
                    cursor.execute('ALTER TABLE downloads ADD COLUMN jd_confirmed_name TEXT')
                    from backend.download_service import fold_name
                    cursor.execute("SELECT url, package_name FROM downloads "
                                   "WHERE package_name IS NOT NULL")
                    dl_rows = cursor.fetchall()
                    cursor.execute("SELECT DISTINCT name FROM download_results "
                                   "WHERE name IS NOT NULL")
                    jd_names = [r[0] for r in cursor.fetchall()]
                    by_fold = {}
                    for url, pkg in dl_rows:
                        by_fold.setdefault(fold_name(pkg), []).append(url)
                    for jd_name in jd_names:
                        hits = by_fold.get(fold_name(jd_name), [])
                        if len(hits) == 1:
                            cursor.execute(
                                "UPDATE downloads SET jd_confirmed_name = ? "
                                "WHERE url = ? AND jd_confirmed_name IS NULL",
                                (jd_name, hits[0]))
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise

                # Must come AFTER the package_name / jd_confirmed_name column
                # migrations above — on a fresh database `downloads` is created
                # with only (url, title, date_added), so indexing these columns
                # any earlier fails with "no such column" and takes startup with
                # it. The live source-link resolver that first needed these was
                # retired in favour of recorded link provenance; they stay for
                # the remaining name queries, chiefly the jd_confirmed_name
                # backfill, which scans package_name on every startup.
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_package_name '
                               'ON downloads(package_name) WHERE package_name IS NOT NULL')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_jd_confirmed_name '
                               'ON downloads(jd_confirmed_name) WHERE jd_confirmed_name IS NOT NULL')

                # PACKAGE PROVENANCE (peer review Finding 1, 2026-08-12).
                # The file-host links ScanHound actually submitted for a release.
                # poll_results() enumerates JDownloader's ENTIRE package list, so
                # a package added by hand is in scope; matching it to a release by
                # display name is a coincidence, not evidence, and produced a
                # confident link to the wrong release page. A link IS the release,
                # so this is provenance by construction — and both send paths (API
                # and .crawljob) know the links, which a package-uuid scheme could
                # not say (JD assigns uuids asynchronously, and the folder path has
                # nothing to ask).
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS download_package_links (
                        url TEXT NOT NULL,
                        link TEXT NOT NULL,
                        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (url, link)
                    )
                ''')
                # Resolution goes link -> release, so `link` is the lookup key.
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_package_links_link '
                               'ON download_package_links(link)')

                # HDEncode RSS evidence tables (v3, additive-only).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_feed_state (
                        feed_key TEXT PRIMARY KEY,
                        feed_url TEXT NOT NULL,
                        last_modified TEXT,
                        last_checked_at TEXT,
                        last_changed_at TEXT,
                        last_status INTEGER,
                        body_sha256 TEXT,
                        channel_last_build_date TEXT,
                        newest_entry_at TEXT,
                        oldest_entry_at TEXT,
                        observed_depth_seconds INTEGER,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_error_code TEXT
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_candidates (
                        canonical_url TEXT PRIMARY KEY,
                        guid TEXT NOT NULL,
                        title TEXT NOT NULL,
                        pub_date TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        clean_title TEXT,
                        title_year INTEGER,
                        description_year INTEGER,
                        season INTEGER,
                        episode INTEGER,
                        episode_end INTEGER,
                        resolution TEXT,
                        size_text TEXT,
                        size_gb REAL,
                        dv_evidence TEXT NOT NULL DEFAULT 'unknown',
                        hdr_evidence TEXT NOT NULL DEFAULT 'unknown',
                        hevc_evidence TEXT NOT NULL DEFAULT 'unknown',
                        hdr_formats TEXT NOT NULL DEFAULT '[]',
                        categories TEXT NOT NULL DEFAULT '[]',
                        raw_description TEXT NOT NULL DEFAULT '',
                        raw_hash TEXT NOT NULL,
                        description_complete INTEGER NOT NULL DEFAULT 0,
                        parse_state TEXT NOT NULL DEFAULT 'parsed',
                        identity_state TEXT NOT NULL DEFAULT 'unknown',
                        relevance_state TEXT NOT NULL DEFAULT 'unclassified',
                        detail_reason TEXT,
                        hydration_state TEXT NOT NULL DEFAULT 'not_requested',
                        action_state TEXT NOT NULL DEFAULT 'none',
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_hdencode_candidates_guid
                    ON hdencode_candidates(guid)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_hdencode_candidates_state
                    ON hdencode_candidates(relevance_state, hydration_state, pub_date)
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_candidate_feeds (
                        feed_key TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY (feed_key, canonical_url),
                        FOREIGN KEY (canonical_url)
                            REFERENCES hdencode_candidates(canonical_url)
                            ON DELETE CASCADE
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_ingest_cycles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feed_key TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        http_status INTEGER NOT NULL,
                        changed INTEGER NOT NULL,
                        candidate_count INTEGER NOT NULL DEFAULT 0,
                        body_sha256 TEXT,
                        last_modified TEXT,
                        outcome TEXT NOT NULL,
                        error_code TEXT
                    )
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_hydration_queue (
                        canonical_url TEXT PRIMARY KEY,
                        reason TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'queued',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        queued_at TEXT NOT NULL,
                        claimed_at TEXT,
                        completed_at TEXT,
                        last_error_code TEXT,
                        FOREIGN KEY (canonical_url)
                            REFERENCES hdencode_candidates(canonical_url)
                            ON DELETE CASCADE
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_hdencode_hydration_priority
                    ON hdencode_hydration_queue(state, priority DESC, queued_at)
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_candidate_details (
                        canonical_url TEXT PRIMARY KEY,
                        hydrated_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY (canonical_url)
                            REFERENCES hdencode_candidates(canonical_url)
                            ON DELETE CASCADE
                    )
                """)

                if current_version < 2:
                    # v2: Drop legacy tables from removed subsystems
                    for table in ('file_manager', 'schema_version', 'app_config'):
                        try:
                            cursor.execute(f"DROP TABLE IF EXISTS {table}")
                        except sqlite3.OperationalError:
                            pass

                # RSS comparison evidence and identity additions (v5).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_shadow_cycles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cycle_uuid TEXT NOT NULL UNIQUE,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        normal_feeds_complete INTEGER NOT NULL,
                        rss_requests INTEGER NOT NULL,
                        listing_requests INTEGER NOT NULL,
                        rss_count INTEGER NOT NULL,
                        listing_count INTEGER NOT NULL,
                        duplicate_count INTEGER NOT NULL,
                        feed_only_count INTEGER NOT NULL,
                        listing_only_count INTEGER NOT NULL,
                        relevant_miss_count INTEGER NOT NULL,
                        request_reduction_pct REAL NOT NULL,
                        catchup_used INTEGER NOT NULL DEFAULT 0,
                        restart_recovery INTEGER NOT NULL DEFAULT 0,
                        outcome TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        normal_feed_outcomes TEXT,
                        -- Listing-arm trustworthiness, recorded SEPARATELY from
                        -- feed health. NULL on cycles written before 2026-08-07.
                        -- normal_feeds_complete conflates a failed feed with a
                        -- failed listing crawl, so resolution cannot tell them
                        -- apart from it; see cycle_is_valid_evidence_for().
                        listing_complete INTEGER
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_shadow_misses (
                        cycle_uuid TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        title TEXT,
                        status TEXT,
                        media_type TEXT,
                        attribution_basis TEXT,
                        PRIMARY KEY (cycle_uuid, canonical_url),
                        FOREIGN KEY (cycle_uuid)
                            REFERENCES hdencode_shadow_cycles(cycle_uuid)
                            ON DELETE CASCADE
                    )
                """)
                # Additive migrations for the two tables above, kept HERE rather
                # than in the shared _column_migrations list several hundred
                # lines earlier. That list runs before these CREATE statements,
                # so an ALTER placed there fails with "no such table", and the
                # guard only swallows "duplicate column" -- it logs the failure
                # and carries on, leaving the column absent. Which is exactly
                # what happened on the first attempt: every new test failed with
                # "table hdencode_shadow_cycles has no column named
                # normal_feed_outcomes" while the migration warning scrolled past
                # in the log.
                for _shadow_alter in (
                    "ALTER TABLE hdencode_shadow_cycles "
                    "ADD COLUMN normal_feed_outcomes TEXT",
                    "ALTER TABLE hdencode_shadow_misses ADD COLUMN media_type TEXT",
                    # The signals that decided the attribution, so a counted
                    # miss can be audited rather than re-derived by guesswork.
                    "ALTER TABLE hdencode_shadow_misses "
                    "ADD COLUMN attribution_basis TEXT",
                    # Listing-arm authority, so a mixed-feed cycle can resolve a
                    # miss for the feed that DID succeed without also trusting a
                    # listing crawl that failed.
                    "ALTER TABLE hdencode_shadow_cycles "
                    "ADD COLUMN listing_complete INTEGER",
                ):
                    try:
                        cursor.execute(_shadow_alter)
                    except sqlite3.OperationalError as _e:
                        if "duplicate column" not in str(_e).lower():
                            logger.warning("Shadow migration failed: %s — %s",
                                           _shadow_alter, _e)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_hdencode_shadow_completed
                    ON hdencode_shadow_cycles(completed_at, outcome)
                """)
                # Listing URLs excluded by operator policy before any detail
                # fetch. Durable ON PURPOSE: an in-memory skip stops the wasted
                # downloads but leaves the URL looking new every cycle, which
                # keeps blocking early-stop forever.
                #
                # Deliberately NOT named as a general listing ledger — it holds
                # ONLY policy exclusions. A half-populated table called a ledger
                # would invite a later reader to assume it is complete.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS listing_policy_exclusions (
                        canonical_url TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        category TEXT,
                        listing_title TEXT,
                        policy_reason TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listing_policy_excl_source
                    ON listing_policy_exclusions(source, policy_reason)
                """)
                for _column, _declaration in (
                    ("imdb_id", "TEXT"),
                    ("tmdb_id", "TEXT"),
                    ("discovery_source", "TEXT NOT NULL DEFAULT 'rss'"),
                    # WHICH protected fields detail actually supplied for THIS
                    # row (JSON array), recorded at hydration completion.
                    #
                    # Authority is per row, not per field. _candidate_updates
                    # omits any field the detail payload did not carry, and the
                    # sink COALESCEs, so a completed row is a MIXTURE: fields
                    # detail supplied are detail-authoritative, and the rest are
                    # still the feed's. Nothing recorded which was which, so the
                    # feed-repair pass had no way to know what it was allowed to
                    # re-derive and repaired exactly one hardcoded field.
                    #
                    # NULL means "recorded before this column existed". That is
                    # deliberately NOT the same as '[]' (detail supplied
                    # nothing): an unknown claim set must not be read as an
                    # empty one, or the repair would overwrite detail facts it
                    # cannot see.
                    ("detail_authority_fields", "TEXT"),
                    # Media-type CONFIDENCE and PROVENANCE, additive.
                    #
                    # The resolver produced both and the parser object carried
                    # both, but there was nowhere to put them, so they died at
                    # this boundary — the claim that weak route-only evidence
                    # stayed distinguishable to a downstream decision was false
                    # for every actionable path.
                    #
                    # Defaults are the CAUTIOUS values: an existing row that
                    # predates this column has an unknown provenance, so it is
                    # treated as provisional (1) until re-parsed, which blocks
                    # it from autonomous action rather than grandfathering it in.
                    ("media_type_provisional", "INTEGER NOT NULL DEFAULT 1"),
                    ("media_type_because", "TEXT NOT NULL DEFAULT '[]'"),
                    # R-4 versioning: NULL version on a pre-existing row means
                    # "stamped by nothing" -- the reconciler treats it exactly
                    # like a version mismatch (stale), never as current.
                    ("feed_parse_version", "TEXT"),
                    ("detail_parse_version", "TEXT"),
                    ("derived_state", "TEXT NOT NULL DEFAULT 'current'"),
                ):
                    try:
                        cursor.execute(
                            f"ALTER TABLE hdencode_candidates "
                            f"ADD COLUMN {_column} {_declaration}"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise

                # Persistent RSS candidate actions (v6, additive-only).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_actions (
                        action_uuid TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        canonical_url TEXT NOT NULL,
                        action_kind TEXT NOT NULL,
                        requested_by TEXT NOT NULL,
                        service_type TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        package_name TEXT,
                        destination TEXT,
                        links_json TEXT NOT NULL DEFAULT '[]',
                        link_count INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        queued_at TEXT NOT NULL,
                        claimed_at TEXT,
                        links_ready_at TEXT,
                        submitted_at TEXT,
                        completed_at TEXT,
                        cancelled_at TEXT,
                        updated_at TEXT NOT NULL,
                        last_error_code TEXT,
                        correlation_id TEXT,
                        authorized_evidence_json TEXT NOT NULL DEFAULT '{}',
                        lifespan_generation INTEGER,
                        FOREIGN KEY (canonical_url)
                            REFERENCES hdencode_candidates(canonical_url)
                            ON DELETE CASCADE
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_hdencode_actions_queue
                    ON hdencode_actions(state, priority DESC, queued_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_hdencode_actions_candidate
                    ON hdencode_actions(canonical_url, updated_at DESC)
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_hdencode_actions_active
                    ON hdencode_actions(canonical_url)
                    WHERE state IN (
                        'queued', 'retrieving_links', 'links_ready', 'submitting'
                    )
                """)

                # Durable download scheduling and verification-retry queue (v8).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS download_queue_batches (
                        batch_uuid TEXT PRIMARY KEY,
                        mode TEXT NOT NULL CHECK(mode IN (
                            'immediate', 'staggered', 'verification_retry'
                        )),
                        interval_seconds INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL CHECK(state IN (
                            'scheduled', 'running', 'paused_source',
                            'waiting_user', 'completed', 'cancelled'
                        )),
                        source TEXT,
                        total_items INTEGER NOT NULL DEFAULT 0,
                        completed_items INTEGER NOT NULL DEFAULT 0,
                        failed_items INTEGER NOT NULL DEFAULT 0,
                        deferred_items INTEGER NOT NULL DEFAULT 0,
                        auto_resume_after_cooldown INTEGER NOT NULL DEFAULT 0,
                        auto_resume_used INTEGER NOT NULL DEFAULT 0,
                        -- Completed-item count at the moment of the last
                        -- automatic resume. Lets the retry budget be REFUNDED
                        -- when a resume actually delivered something, so a batch
                        -- that keeps making progress is not cut off after N
                        -- attempts. See _maybe_auto_resume.
                        auto_resume_progress_mark INTEGER NOT NULL DEFAULT 0,
                        source_delivery_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        paused_at TEXT,
                        cooldown_until TEXT,
                        last_reason_code TEXT,
                        last_cause_code TEXT,
                        -- Non-NULL = this batch is under a human-verification
                        -- hold for that source: an interactive challenge our
                        -- automated browser could not complete. A timer never
                        -- clears it; only a probe that genuinely delivers from
                        -- this source does (DownloadQueueService._complete).
                        verification_hold_source TEXT
                    )
                """)
                # Additive migration for the table above, placed HERE and not in
                # the shared _column_migrations list several hundred lines
                # earlier. That list runs BEFORE this CREATE, so an ALTER there
                # fails with "no such table", and the guard only swallows
                # "duplicate column" -- it logs the failure and carries on,
                # leaving the column absent. That exact mistake cost a round of
                # confusing test failures on the shadow tables; see the note
                # beside their migrations.
                for _batch_alter in (
                    "ALTER TABLE download_queue_batches "
                    "ADD COLUMN auto_resume_progress_mark INTEGER "
                    "NOT NULL DEFAULT 0",
                    # Incremented ONLY when a completion genuinely crossed the
                    # source boundary -- see DownloadQueueService._complete.
                    # Generic 'completed' cannot be used: download_item() returns
                    # success with method='duplicate' BEFORE scraping when the
                    # release was already grabbed, so counting completions would
                    # refund retry budget for work the source never did.
                    "ALTER TABLE download_queue_batches "
                    "ADD COLUMN source_delivery_count INTEGER "
                    "NOT NULL DEFAULT 0",
                    # See the column comment in the CREATE above (v9).
                    "ALTER TABLE download_queue_batches "
                    "ADD COLUMN verification_hold_source TEXT",
                ):
                    try:
                        cursor.execute(_batch_alter)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS download_queue_items (
                        item_uuid TEXT PRIMARY KEY,
                        batch_uuid TEXT NOT NULL REFERENCES download_queue_batches(batch_uuid)
                            ON DELETE CASCADE,
                        sequence_number INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        year INTEGER,
                        season INTEGER,
                        resolution TEXT,
                        size_text TEXT,
                        hdr TEXT,
                        dovi INTEGER NOT NULL DEFAULT 0,
                        service_type TEXT NOT NULL,
                        -- The scan source's category for this queued grab, carried so a
                        -- BATCHED download records the same media kind an interactive one
                        -- does. Without it every queued grab reached save_to_history with
                        -- no category and its row got media_kind NULL, so the dupe-compare
                        -- feature was dark for the 398 items that have completed this way.
                        -- Nullable on purpose: an old row, or a client that does not send
                        -- one, records NOTHING rather than a guess.
                        category TEXT,
                        queue_reason TEXT NOT NULL CHECK(queue_reason IN (
                            'user_batch', 'interactive_challenge',
                            'source_deferred', 'manual_retry'
                        )),
                        state TEXT NOT NULL CHECK(state IN (
                            'scheduled', 'ready', 'claimed', 'waiting_source',
                            'verification_required', 'completed', 'failed',
                            'cancelled'
                        )),
                        scheduled_for TEXT,
                        cooldown_until TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        automated_retry_count INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_reason_code TEXT,
                        last_cause_code TEXT,
                        last_message TEXT,
                        transport_attempted INTEGER,
                        claimed_by TEXT,
                        claim_expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        cancelled_at TEXT
                    )
                """)
                # Additive migration for the table above. It must live HERE, after
                # the CREATE, for the reason documented beside the batches ALTERs.
                try:
                    cursor.execute(
                        "ALTER TABLE download_queue_items ADD COLUMN category TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                # APPEND-ONLY attempt history. The queue's item/batch rows carry
                # only CURRENT state, which is why the 2026-08-13 incident could
                # not be diagnosed: after a container restart there was no way
                # to tell "the source was attempted repeatedly and every attempt
                # failed" from "nothing was ever attempted". Both look identical
                # in a durable row that just says waiting_source.
                #
                # transport_attempted is the load-bearing column. _pause_for_source
                # rewrites every same-source sibling with a source_temporarily_blocked
                # reason and transport_attempted=0, so a COUNT of reason codes
                # measures policy consequences, not observations. Of 62 such rows
                # in that incident exactly ONE had transport_attempted=1. Any
                # source-health decision must read observations only.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS download_queue_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        item_uuid TEXT NOT NULL,
                        batch_uuid TEXT NOT NULL,
                        source TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        -- IN_PROGRESS until closed. An attempt that outlives its
                        -- deadline while still IN_PROGRESS is STALE, which is the
                        -- signal a blocked worker cannot otherwise produce.
                        terminal_status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                        reason_code TEXT,
                        affected_scope TEXT,
                        -- 1 only when a request actually reached the source.
                        transport_attempted INTEGER NOT NULL DEFAULT 0,
                        -- 1 when the source affirmatively delivered. This, not
                        -- item completion, is the source-liveness signal.
                        source_progress INTEGER NOT NULL DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_queue_attempts_source_time
                    ON download_queue_attempts(source, started_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_queue_attempts_open
                    ON download_queue_attempts(terminal_status, started_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_download_queue_due
                    ON download_queue_items(state, scheduled_for, sequence_number)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_download_queue_batch
                    ON download_queue_items(batch_uuid, sequence_number)
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_download_queue_active_item
                    ON download_queue_items(source, canonical_url, service_type)
                    WHERE state IN (
                        'scheduled', 'ready', 'claimed', 'waiting_source',
                        'verification_required'
                    )
                """)

                if current_version < 9:
                    # v9 ONE-TIME DATA MIGRATION: place the challenge episode
                    # that predates the verification-hold column under the
                    # hold. AFTER both queue tables exist — the UPDATE's
                    # subqueries read download_queue_items, and on a fresh
                    # database (current_version=0) that table is only created
                    # a few statements above; running earlier broke every
                    # fresh init with "no such table".
                    self._mark_existing_challenge_pauses_held(cursor)
                # ── Hybrid listing sweep (v9) ────────────────────────────
                # Three tables, deliberately separate, because conflating them
                # is what broke the earlier design:
                #
                #  listing_source_ledger  proves LISTING TRAVERSAL history.
                #      A URL known only through RSS proves nothing about how
                #      deep we crawled a listing source, so candidate storage
                #      cannot serve as the known-URL frontier.
                #  hdencode_sweep_sessions  one logical session per attempt
                #      chain. `started_at` is preserved across continuation,
                #      restart and lease reacquisition - it is what
                #      coverage_through commits to, so resetting it would let a
                #      long crawl claim coverage it never proved.
                #  hdencode_source_coverage  the durable per-source watermark
                #      plus lease. Advanced ONLY on conjunctive completion.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS listing_source_ledger (
                        source_key TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        raw_url TEXT NOT NULL,
                        canonicalizer_version TEXT NOT NULL,
                        first_observed_at TEXT NOT NULL,
                        last_observed_at TEXT NOT NULL,
                        displayed_published_at TEXT,
                        first_page_index INTEGER,
                        last_page_index INTEGER,
                        title_snapshot TEXT,
                        status_snapshot TEXT,
                        sweep_session_uuid TEXT,
                        persisted INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (source_key, canonical_url)
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listing_ledger_recency
                    ON listing_source_ledger(source_key, displayed_published_at)
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_sweep_sessions (
                        sweep_session_uuid TEXT PRIMARY KEY,
                        source_key TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        prior_coverage_through TEXT,
                        overlap_hours REAL NOT NULL,
                        stop_target TEXT,
                        continuation_frontier_at TEXT,
                        continuation_anchor_url TEXT,
                        pages_crawled INTEGER NOT NULL DEFAULT 0,
                        oldest_reached_at TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 1,
                        lease_owner TEXT,
                        lease_expires_at TEXT,
                        terminal_status TEXT,
                        failure_reason TEXT,
                        completed_at TEXT,
                        discoveries INTEGER NOT NULL DEFAULT 0,
                        rss_missing_recovered INTEGER NOT NULL DEFAULT 0,
                        request_count INTEGER NOT NULL DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sweep_sessions_source
                    ON hdencode_sweep_sessions(source_key, started_at)
                """)
                # One ACTIVE session per source. A partial unique index is the
                # enforcement, not a convention someone has to remember.
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_sweep_session_active
                    ON hdencode_sweep_sessions(source_key)
                    WHERE terminal_status IS NULL
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_qualification_window (
                        -- The qualification window boundary as DURABLE SAFETY
                        -- STATE, not operator configuration.
                        --
                        -- A runbook rule is not sufficient. Moving the boundary
                        -- backward imports evidence produced by an earlier
                        -- build; moving it forward can erase a relevant miss or
                        -- restart the duration clock without declaring a new
                        -- window; repeated edits destroy any proof of which
                        -- evidence was actually reviewed. The zero-miss
                        -- requirement can be defeated simply by sliding the
                        -- boundary past an observed miss, which makes the
                        -- boundary part of what the gate protects.
                        --
                        -- Correcting the value BEFORE any cycle has accumulated
                        -- inside it is legitimate setup. Once one cycle exists
                        -- at or after it, it LOCKS. Starting another window is
                        -- an explicit action that SUPERSEDES this row rather
                        -- than overwriting it, so the sequence stays auditable.
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        window_start_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        build_ref TEXT,
                        operator_note TEXT,
                        previous_window_start_at TEXT,
                        superseded_at TEXT
                    )
                """)
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_qual_window_active "
                    "ON hdencode_qualification_window(superseded_at) "
                    "WHERE superseded_at IS NULL")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS hdencode_source_coverage (
                        source_key TEXT PRIMARY KEY,
                        coverage_through TEXT,
                        last_success_session_uuid TEXT,
                        last_success_started_at TEXT,
                        last_success_completed_at TEXT,
                        last_attempt_at TEXT,
                        bootstrap_complete INTEGER NOT NULL DEFAULT 0,
                        interval_state TEXT NOT NULL DEFAULT 'unknown',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT
                    )
                """)

                # ── Stamp current version ────────────────────────────────
                cursor.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

                conn.commit()

            except sqlite3.OperationalError as e:
                # sqlite3.OperationalError is a SUBCLASS of DatabaseError, but
                # it covers transient conditions ("database is locked" after
                # busy_timeout expires, "disk I/O error" from a flaky
                # bind-mounted filesystem) that are NOT corruption. Quarantining
                # here would nuke a perfectly healthy DB on a transient hiccup
                # — exactly the failure mode this hardening pass exists to
                # eliminate, and it's still reachable pre-migration on a
                # bind-mounted volume. Only treat it as corruption if the
                # message itself says so; otherwise log loudly and re-raise so
                # startup fails fast (and can be retried) instead of silently
                # discarding data.
                msg = str(e).lower()
                if any(marker in msg for marker in ("malformed", "not a database", "corrupt")):
                    self._quarantine_corrupt_db(e)
                else:
                    logger.warning(
                        "Transient DB operational error during init at %s "
                        "(not corruption — not quarantining): %s", self.db_path, e)
                    raise
            except sqlite3.DatabaseError as e:
                # Genuine corruption (or an integrity_check failure we raised
                # ourselves above as a plain DatabaseError). LOUD by design: DB
                # corruption + auto-quarantine is a data-loss event (every row
                # not yet reflected elsewhere is gone), so this must never be a
                # quiet log line. ERROR-level log with a grep-able marker, a
                # best-effort user notification, and a persisted flag file
                # (survives past the log) that ops/UI code can check for after
                # the fact.
                self._quarantine_corrupt_db(e)
            finally:
                self._init_depth = 0

        # One-time WAL checkpoint after a successful (non-corrupt) init, so a
        # freshly-opened DB doesn't carry forward an unbounded WAL. Best-effort
        # — never let a checkpoint failure block startup. Periodic scheduling
        # beyond this one call is a follow-up (see db-reliability report).
        if self.conn:
            try:
                self.checkpoint()
            except Exception:
                logger.exception("Post-init WAL checkpoint failed")

    def _quarantine_corrupt_db(self, e) -> None:
        """Back up a genuinely corrupt DB file and rebuild fresh in its place.

        Shared by the true-corruption branches of init_db() (plain
        DatabaseError, and OperationalError whose message indicates real
        corruption rather than a transient lock/I-O condition).
        """
        logger.error(
            "DATABASE CORRUPTION DETECTED at %s — quarantining and "
            "rebuilding a fresh database: %s", self.db_path, e)
        self._notify_corruption(e)
        if self.conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

        # Auto-recovery: back up corrupt file and start fresh
        if os.path.exists(self.db_path):
            backup_name = f"{self.db_path}.corrupt.{int(time.time())}"
            try:
                os.rename(self.db_path, backup_name)
                logger.warning("Renamed corrupt DB to %s. Creating fresh DB.", backup_name)
                self._write_corruption_flag(backup_name, e)
                self.init_db()
            except OSError as os_err:
                logger.critical("Failed to recover DB: %s", os_err)

    def _notify_corruption(self, error) -> None:
        """Best-effort loud alert for a DB quarantine event.

        Tries the app's notification bridge if one is reachable; falls back
        silently (the ERROR log line above is always emitted regardless, so
        this is a bonus channel, not the primary signal).
        """
        try:
            from backend.notification_bridge import NotificationBridge
            import backend.app_service as _app_service
            bridge = getattr(_app_service, "notification_bridge", None)
            if isinstance(bridge, NotificationBridge):
                bridge.notify_error(
                    f"ScanHound database corruption detected at {self.db_path} — "
                    f"quarantined and rebuilt a fresh database. Error: {error}")
        except Exception:
            logger.debug("Corruption notification unavailable (non-fatal)", exc_info=True)

    def _write_corruption_flag(self, backup_name: str, error) -> None:
        """Persist a marker file recording the quarantine, independent of logs."""
        try:
            flag_path = f"{self.db_path}.corrupt_flag.json"
            with open(flag_path, "w", encoding="utf-8") as f:
                json.dump({
                    "detected_at": datetime.datetime.now().isoformat(),
                    "db_path": self.db_path,
                    "backup_path": backup_name,
                    "error": str(error),
                }, f, indent=2)
        except OSError:
            logger.exception("Failed to write DB corruption flag file")

    # ── HDEncode RSS evidence ──────────────────────────────────────────

    def get_hdencode_feed_state(self, feed_key):
        row = self._query(
            "SELECT * FROM hdencode_feed_state WHERE feed_key = ?",
            (feed_key,), one=True, default=None,
        )
        return dict(row) if row is not None else None

    def list_hdencode_feed_states(self):
        return self._query_dicts(
            "SELECT * FROM hdencode_feed_state ORDER BY feed_key",
            default=[],
        )

    def ingest_hdencode_feed(
        self, *, feed_key, feed_url, last_modified, http_status,
        body_sha256, channel_last_build_date, entries, started_at,
        completed_at, _test_fail_after_step=None,
    ):
        """Commit feed evidence and its validator as one SQLite transaction."""
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("Database unavailable")
            step = 0

            def executed():
                nonlocal step
                step += 1
                if _test_fail_after_step == step:
                    raise RuntimeError(
                        f"injected ingest failure after step {step}"
                    )

            try:
                conn.execute("BEGIN IMMEDIATE")
                executed()
                newest = max((row["pub_date"] for row in entries), default=None)
                oldest = min((row["pub_date"] for row in entries), default=None)
                now = completed_at
                for row in entries:
                    conn.execute(
                        """
                        INSERT INTO hdencode_candidates (
                            canonical_url, guid, title, pub_date, media_type,
                            clean_title, title_year, description_year,
                            season, episode, episode_end, resolution,
                            size_text, size_gb, dv_evidence, hdr_evidence,
                            hevc_evidence, hdr_formats, categories,
                            raw_description, raw_hash, description_complete,
                            media_type_provisional, media_type_because,
                            feed_parse_version,
                            first_seen_at, last_seen_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(canonical_url) DO UPDATE SET
                            -- Round-10 Q3 P0: a changed poll must NOT revert
                            -- DETAIL-authority facts. Once hydration completed,
                            -- the feed is the LOWER authority for these fields;
                            -- raw feed facts below still always update.
                            guid = excluded.guid,
                            title = excluded.title,
                            pub_date = excluded.pub_date,
                            media_type = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.media_type ELSE excluded.media_type END,
                            clean_title = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.clean_title ELSE excluded.clean_title END,
                            title_year = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.title_year ELSE excluded.title_year END,
                            description_year = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.description_year ELSE excluded.description_year END,
                            season = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.season ELSE excluded.season END,
                            episode = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.episode ELSE excluded.episode END,
                            episode_end = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.episode_end ELSE excluded.episode_end END,
                            resolution = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.resolution ELSE excluded.resolution END,
                            size_text = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.size_text ELSE excluded.size_text END,
                            size_gb = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.size_gb ELSE excluded.size_gb END,
                            dv_evidence = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.dv_evidence ELSE excluded.dv_evidence END,
                            hdr_evidence = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.hdr_evidence ELSE excluded.hdr_evidence END,
                            hevc_evidence = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.hevc_evidence ELSE excluded.hevc_evidence END,
                            hdr_formats = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.hdr_formats ELSE excluded.hdr_formats END,
                            categories = excluded.categories,
                            raw_description = excluded.raw_description,
                            raw_hash = excluded.raw_hash,
                            description_complete = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.description_complete ELSE excluded.description_complete END,
                            media_type_provisional = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.media_type_provisional ELSE excluded.media_type_provisional END,
                            media_type_because = CASE WHEN hdencode_candidates.hydration_state = 'completed' THEN hdencode_candidates.media_type_because ELSE excluded.media_type_because END,
                            feed_parse_version = excluded.feed_parse_version,
                            last_seen_at = excluded.last_seen_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            row["canonical_url"], row["guid"], row["title"],
                            row["pub_date"], row["media_type"],
                            row.get("clean_title"), row.get("title_year"),
                            row.get("description_year"), row.get("season"),
                            row.get("episode"), row.get("episode_end"),
                            row.get("resolution"), row.get("size_text"),
                            row.get("size_gb"), row.get("dv", "unknown"),
                            row.get("hdr", "unknown"),
                            row.get("hevc", "unknown"),
                            json.dumps(row.get("hdr_formats") or []),
                            json.dumps(row.get("categories") or []),
                            row.get("raw_description") or "",
                            row["raw_hash"],
                            1 if row.get("description_complete") else 0,
                            # Absent provenance defaults to PROVISIONAL, not
                            # confirmed: a row whose confidence we cannot read
                            # must not be treated as strongly evidenced.
                            0 if row.get("media_type_provisional") is False else 1,
                            json.dumps(list(row.get("media_type_because") or [])),
                            release_grammar.GRAMMAR_VERSION,
                            now, now, now,
                        ),
                    )
                    executed()
                    conn.execute(
                        """
                        INSERT INTO hdencode_candidate_feeds (
                            feed_key, canonical_url, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(feed_key, canonical_url) DO UPDATE SET
                            last_seen_at = excluded.last_seen_at
                        """,
                        (feed_key, row["canonical_url"], now, now),
                    )
                    executed()

                conn.execute(
                    """
                    INSERT INTO hdencode_ingest_cycles (
                        feed_key, started_at, completed_at, http_status, changed,
                        candidate_count, body_sha256, last_modified, outcome
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'changed')
                    """,
                    (
                        feed_key, started_at, completed_at, int(http_status),
                        len(entries), body_sha256, last_modified,
                    ),
                )
                executed()
                conn.execute(
                    """
                    INSERT INTO hdencode_feed_state (
                        feed_key, feed_url, last_modified, last_checked_at,
                        last_changed_at, last_status, body_sha256,
                        channel_last_build_date, newest_entry_at,
                        oldest_entry_at, consecutive_failures, last_error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                    ON CONFLICT(feed_key) DO UPDATE SET
                        feed_url = excluded.feed_url,
                        last_modified = excluded.last_modified,
                        last_checked_at = excluded.last_checked_at,
                        last_changed_at = excluded.last_changed_at,
                        last_status = excluded.last_status,
                        body_sha256 = excluded.body_sha256,
                        channel_last_build_date =
                            excluded.channel_last_build_date,
                        newest_entry_at = excluded.newest_entry_at,
                        oldest_entry_at = excluded.oldest_entry_at,
                        consecutive_failures = 0,
                        last_error_code = NULL
                    """,
                    (
                        feed_key, feed_url, last_modified, completed_at,
                        completed_at, int(http_status), body_sha256,
                        channel_last_build_date, newest, oldest,
                    ),
                )
                executed()
                conn.commit()
                return len(entries)
            except Exception:
                conn.rollback()
                raise

    def record_hdencode_feed_not_modified(
        self, *, feed_key, feed_url, last_modified, checked_at
    ):
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                """
                INSERT INTO hdencode_ingest_cycles (
                    feed_key, started_at, completed_at, http_status, changed,
                    candidate_count, last_modified, outcome
                ) VALUES (?, ?, ?, 304, 0, 0, ?, 'not_modified')
                """,
                (feed_key, checked_at, checked_at, last_modified),
            )
            conn.execute(
                """
                INSERT INTO hdencode_feed_state (
                    feed_key, feed_url, last_modified, last_checked_at,
                    last_status, consecutive_failures
                ) VALUES (?, ?, ?, ?, 304, 0)
                ON CONFLICT(feed_key) DO UPDATE SET
                    feed_url = excluded.feed_url,
                    last_checked_at = excluded.last_checked_at,
                    last_status = 304,
                    consecutive_failures = 0,
                    last_error_code = NULL
                """,
                (feed_key, feed_url, last_modified, checked_at),
            )

    def record_hdencode_feed_failure(
        self, *, feed_key, feed_url, checked_at, status, error_code
    ):
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                """
                INSERT INTO hdencode_ingest_cycles (
                    feed_key, started_at, completed_at, http_status, changed,
                    candidate_count, outcome, error_code
                ) VALUES (?, ?, ?, ?, 0, 0, 'failed', ?)
                """,
                (feed_key, checked_at, checked_at, int(status or 0), error_code),
            )
            conn.execute(
                """
                INSERT INTO hdencode_feed_state (
                    feed_key, feed_url, last_checked_at, last_status,
                    consecutive_failures, last_error_code
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(feed_key) DO UPDATE SET
                    feed_url = excluded.feed_url,
                    last_checked_at = excluded.last_checked_at,
                    last_status = excluded.last_status,
                    consecutive_failures =
                        hdencode_feed_state.consecutive_failures + 1,
                    last_error_code = excluded.last_error_code
                """,
                (feed_key, feed_url, checked_at, int(status or 0), error_code),
            )

    def list_hdencode_candidates(
        self, *, relevance_state=None, hydration_state=None, limit=500
    ):
        clauses = []
        params = []
        if relevance_state:
            clauses.append("relevance_state = ?")
            params.append(relevance_state)
        if hydration_state:
            clauses.append("hydration_state = ?")
            params.append(hydration_state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        return self._query_dicts(
            "SELECT * FROM hdencode_candidates" + where
            + " ORDER BY pub_date DESC LIMIT ?",
            tuple(params), default=[],
        )

    def update_hdencode_candidate_state(
        self, canonical_url, *, identity_state=None,
        relevance_state=None, detail_reason=None,
        hydration_state=None, action_state=None,
    ):
        values = {
            "identity_state": identity_state,
            "relevance_state": relevance_state,
            "detail_reason": detail_reason,
            "hydration_state": hydration_state,
            "action_state": action_state,
        }
        assignments = []
        params = []
        for column, value in values.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if not assignments:
            return True
        assignments.append("updated_at = ?")
        params.append(datetime.datetime.now(datetime.timezone.utc).isoformat())
        params.append(canonical_url)
        return self._mutate(
            "UPDATE hdencode_candidates SET "
            + ", ".join(assignments)
            + " WHERE canonical_url = ?",
            tuple(params),
            label="update_hdencode_candidate_state",
        )

    def update_hdencode_feed_depth(self, feed_key, depth_seconds):
        return self._mutate(
            "UPDATE hdencode_feed_state "
            "SET observed_depth_seconds = ? WHERE feed_key = ?",
            (depth_seconds, feed_key),
            label="update_hdencode_feed_depth",
        )

    def get_hdencode_candidate(self, canonical_url):
        row = self._query(
            "SELECT * FROM hdencode_candidates WHERE canonical_url = ?",
            (canonical_url,),
            one=True,
            default=None,
        )
        return dict(row) if row is not None else None

    def get_hdencode_candidate_context(
        self, *, canonical_url, clean_title, media_type, years, season,
        imdb_id=None, tmdb_id=None,
    ):
        exact_url_downloaded = bool(self._query(
            """
            SELECT 1
            FROM scraped_link_map AS mapping
            JOIN downloads AS history ON history.url = mapping.link
            WHERE RTRIM(mapping.source_url, '/') = RTRIM(?, '/')
            LIMIT 1
            """,
            (canonical_url,), one=True, default=None,
        ))
        if not clean_title and not imdb_id and not tmdb_id:
            return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": []}

        select = (
            "SELECT title, year, res AS resolution, size AS size_gb, "
            "dovi, hdr, season, rating_key, file_path, imdb_id, media_id "
            "FROM plex_cache WHERE "
        )
        if imdb_id:
            matches = self._query_dicts(select + "imdb_id = ?", (imdb_id,), default=[])
            if matches:
                return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": matches, "identity_basis": "imdb_id"}
        if tmdb_id:
            matches = self._query_dicts(select + "media_id = ?", (str(tmdb_id),), default=[])
            if matches:
                return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": matches, "identity_basis": "tmdb_id"}
        if not clean_title:
            return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": []}

        base_clauses=["LOWER(title) = LOWER(?)"]; base_params=[clean_title]
        if media_type == "tv":
            base_clauses.append("content_type = 'TV Shows'")
            if season is not None: base_clauses.append("season = ?"); base_params.append(season)
        elif media_type == "movie":
            base_clauses.append("content_type = 'Movies'")
        else:
            # AMBIGUOUS, or anything unrecognised: match NOTHING.
            #
            # This branch used to be a bare `else` that searched the MOVIES
            # library, so an unresolved media type silently became a movie
            # query — the fail-open direction, and it would have quietly
            # undone the tri-state the resolver produces upstream.
            #
            # Returning no matches leaves the candidate identity-unresolved,
            # which routes it to hydration and blocks it in the gate. That is
            # the intended outcome: we do not know which library this belongs
            # to, so we must not answer as though we do.
            return {
                "exact_url_downloaded": exact_url_downloaded,
                "plex_matches": [],
                "identity_basis": None,
                "media_type_unresolved": True,
            }
        ordered_years=[]
        for year in years or ():
            try: value=int(year)
            except (TypeError,ValueError): continue
            if value not in ordered_years: ordered_years.append(value)
        for year in ordered_years:
            matches=self._query_dicts(select+" AND ".join(base_clauses+["year = ?"]),tuple(base_params+[year]),default=[])
            if matches:
                return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": matches, "identity_basis": f"year:{year}"}
        matches=self._query_dicts(select+" AND ".join(base_clauses+["year IS NULL"]),tuple(base_params),default=[])
        return {"exact_url_downloaded": exact_url_downloaded, "plex_matches": matches, "identity_basis": "title_only" if matches else None}

    def enqueue_hdencode_hydration(self, canonical_url, *, reason, priority):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self._mutate(
            """
            INSERT INTO hdencode_hydration_queue (
                canonical_url, reason, priority, state, queued_at
            ) VALUES (?, ?, ?, 'queued', ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                reason = excluded.reason,
                priority = MAX(
                    hdencode_hydration_queue.priority,
                    excluded.priority
                ),
                state = CASE
                    WHEN hdencode_hydration_queue.state = 'completed'
                        THEN hdencode_hydration_queue.state
                    ELSE 'queued'
                END,
                claimed_at = CASE
                    WHEN hdencode_hydration_queue.state = 'completed'
                        THEN hdencode_hydration_queue.claimed_at
                    ELSE NULL
                END,
                queued_at = CASE
                    WHEN hdencode_hydration_queue.state = 'completed'
                        THEN hdencode_hydration_queue.queued_at
                    ELSE excluded.queued_at
                END,
                last_error_code = CASE
                    WHEN hdencode_hydration_queue.state = 'completed'
                        THEN hdencode_hydration_queue.last_error_code
                    ELSE NULL
                END
            """,
            (canonical_url, reason, int(priority), now),
            label="enqueue_hdencode_hydration",
        )

    def requeue_hdencode_hydration(self, canonical_url, *, reason, priority):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            changed = conn.execute(
                """
                UPDATE hdencode_hydration_queue
                SET reason = ?, priority = ?, state = 'queued',
                    attempts = 0, queued_at = ?, claimed_at = NULL,
                    completed_at = NULL, last_error_code = NULL
                WHERE canonical_url = ?
                """,
                (reason, int(priority), now, canonical_url),
            ).rowcount
            if changed == 0:
                conn.execute(
                    """INSERT INTO hdencode_hydration_queue
                       (canonical_url, reason, priority, state, queued_at)
                       VALUES (?, ?, ?, 'queued', ?)""",
                    (canonical_url, reason, int(priority), now),
                )
            conn.execute(
                """UPDATE hdencode_candidates
                   SET hydration_state='queued', detail_reason=?, updated_at=?
                   WHERE canonical_url=?""",
                (reason, now, canonical_url),
            )

    def resolve_hdencode_hydration(self, canonical_url, *, reason):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                """
                UPDATE hdencode_hydration_queue
                SET state = CASE
                        WHEN state = 'completed' THEN state
                        WHEN state = 'running' THEN state
                        ELSE 'cancelled'
                    END,
                    last_error_code = CASE
                        WHEN state IN ('completed', 'running')
                            THEN last_error_code
                        ELSE ?
                    END
                WHERE canonical_url = ?
                """,
                (reason, canonical_url),
            )
            conn.execute(
                """
                UPDATE hdencode_candidates
                SET hydration_state = CASE
                        WHEN hydration_state = 'completed' THEN hydration_state
                        WHEN hydration_state = 'running' THEN hydration_state
                        ELSE 'not_requested'
                    END,
                    updated_at = ?
                WHERE canonical_url = ?
                """,
                (now, canonical_url),
            )

    def claim_hdencode_hydration(self, *, limit=10):
        limit = max(0, min(int(limit), 50))
        if limit == 0:
            return []
        with self._lock:
            conn = self.get_connection()
            if not conn:
                return []
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT q.canonical_url, q.reason, q.priority,
                           c.title, c.pub_date, c.media_type,
                           c.clean_title, c.title_year, c.description_year,
                           c.season, c.episode, c.resolution, c.size_gb,
                           c.dv_evidence, c.hdr_evidence, c.hevc_evidence,
                           c.hdr_formats, c.description_complete
                    FROM hdencode_hydration_queue q
                    JOIN hdencode_candidates c
                      ON c.canonical_url = q.canonical_url
                    WHERE q.state = 'queued'
                    ORDER BY q.priority DESC, q.queued_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                claimed = []
                for row in rows:
                    changed = conn.execute(
                        """
                        UPDATE hdencode_hydration_queue
                        SET state = 'running',
                            claimed_at = ?,
                            attempts = attempts + 1
                        WHERE canonical_url = ? AND state = 'queued'
                        """,
                        (now, row["canonical_url"]),
                    ).rowcount
                    if changed != 1:
                        continue
                    conn.execute(
                        """
                        UPDATE hdencode_candidates
                        SET hydration_state = 'running', updated_at = ?
                        WHERE canonical_url = ?
                        """,
                        (now, row["canonical_url"]),
                    )
                    claimed.append(dict(row))
                conn.commit()
                return claimed
            except Exception:
                conn.rollback()
                raise

    #: Fields the DETAIL adapter can never supply, so on a COMPLETED row they
    #: remain the feed's parse and go stale with the grammar like any other
    #: feed fact. The feed upsert's CASE guards freeze all 17 protected fields
    #: once hydration completes — correct against a later poll, but it also
    #: means nothing re-derives these after a grammar change. Verified against
    #: _candidate_updates' emitted key list (round-14): title_year is the one
    #: protected field it never writes; description_year is where the detail
    #: page's year goes. Keep in sync if that adapter starts emitting more.
    #: Every column the hydration sink COALESCEs, i.e. every field whose
    #: authority can differ ROW BY ROW. A field is detail-authoritative on a
    #: row only if that row's detail payload actually supplied it.
    _PROTECTED_FIELDS = (
        "clean_title", "title_year", "description_year",
        "season", "episode", "episode_end",
        "resolution", "size_text", "size_gb",
        "dv_evidence", "hdr_evidence", "hevc_evidence", "hdr_formats",
        "media_type", "media_type_provisional", "media_type_because",
    )

    #: Fields that must move together or not at all. Re-deriving one member
    #: from a new grammar while another stays at the old parse produces a row
    #: that is internally inconsistent -- "HDR10+ formats" beside "no HDR
    #: evidence", or a size_text that disagrees with size_gb -- which is worse
    #: than either value alone being stale. If ANY member is detail-claimed the
    #: whole group is treated as detail-owned.
    _COUPLED_FIELD_GROUPS = (
        ("media_type", "media_type_provisional", "media_type_because"),
        ("size_text", "size_gb"),
        ("hdr_evidence", "hdr_formats"),
        ("season", "episode", "episode_end"),
    )

    #: Superseded by per-row authority (round-15). Kept only as the historical
    #: name in migration notes; the repair no longer reads it.
    _FEED_ONLY_ON_COMPLETED = ("title_year",)

    @staticmethod
    def _detail_parse_version():
        """Lazy import: detail_scraper pulls bs4/HTTP-transport modules at
        import time and database.py must stay importable without them —
        matches the function-local GRAMMAR_VERSION import idiom below."""
        from backend.detail_scraper import DETAIL_PARSE_VERSION
        return DETAIL_PARSE_VERSION

    def reconcile_derived_versions(self):
        """R-4: turn version mismatches into visible staleness (round-10 model).

        Runs at startup. NULL stamps on pre-existing rows read as
        stamped-by-nothing = mismatched. Hydrated rows whose detail parse is
        outdated need a REFETCH (the payload never kept the source filename);
        non-hydrated rows are merely STALE (their feed facts are offline-
        reparseable -- commit 3 re-derives them in place). Dependent
        classification is invalidated in the same statement so nothing keeps
        trusting a verdict computed under the old grammar. Cache rows carry
        their own dedicated column, and the parse-cache generation counter is
        bumped so /results/cached cannot serve pre-reconciliation parses.
        Returns the three affected-row counts for the startup log."""
        from backend.release_grammar import GRAMMAR_VERSION
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("Database unavailable")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE hdencode_candidates
                SET derived_state = 'refetch_required',
                    relevance_state = 'unclassified'
                WHERE hydration_state = 'completed'
                  AND COALESCE(detail_parse_version, '') != ?
                  AND derived_state != 'refetch_required'
                """, (self._detail_parse_version(),))
            refetch = cur.rowcount
            cur.execute(
                """
                UPDATE hdencode_candidates
                SET derived_state = 'stale',
                    relevance_state = 'unclassified'
                WHERE hydration_state != 'completed'
                  AND COALESCE(feed_parse_version, '') != ?
                  AND derived_state != 'stale'
                """, (GRAMMAR_VERSION,))
            stale = cur.rowcount
            cur.execute(
                """
                UPDATE background_scan_cache
                SET derived_state = 'stale'
                WHERE COALESCE(parse_version, '') != ?
                  AND derived_state != 'stale'
                """, (GRAMMAR_VERSION,))
            cache_stale = cur.rowcount
            cur.execute(
                """SELECT canonical_url FROM hdencode_candidates
                   WHERE derived_state = 'refetch_required'
                     AND hydration_state = 'completed'""")
            refetch_urls = [r[0] for r in cur.fetchall()]
            conn.commit()
            if cache_stale:
                # the in-process parse-cache generation gate (R-5 finding:
                # any blob-adjacent mutation that skips this bump makes
                # /results/cached serve stale parses indefinitely)
                self._bg_cache_rev += 1
        healed = self._reparse_stale_candidates()
        backfilled = self._backfill_detail_authority_fields()
        feed_healed = self._reparse_completed_feed_only()
        for url in refetch_urls:
            self.requeue_hdencode_hydration(
                url, reason="stale_derived_refetch", priority=80)
        return {"candidates_refetch_required": refetch,
                "candidates_stale": stale, "cache_stale": cache_stale,
                "candidates_reparsed": healed,
                "detail_authority_backfilled": backfilled,
                "completed_feed_facts_reparsed": feed_healed}

    def _backfill_detail_authority_fields(self):
        """Reconstruct the per-row detail claim set for rows hydrated before
        the column existed. Returns the number reconstructed.

        Rows written before ``detail_authority_fields`` carry NULL, and
        :meth:`_feed_owned_fields` deliberately repairs NOTHING for those --
        an unknown claim set must not be read as an empty one. Correct, but on
        its own it strands every pre-existing completed row: measured against
        the live database, 2466 of 3431 candidate rows are completed, so a
        NULL-only policy would leave all 2466 unable to heal their feed-owned
        facts until each happened to be re-hydrated.

        They do not have to wait. ``hdencode_candidate_details`` retains the
        exact payload each hydration consumed -- all 2466 of them, verified --
        so re-running the SAME ``_candidate_updates`` over the stored payload
        reproduces precisely which protected fields that row's detail supplied.
        This is reconstruction from the original input, not a guess.

        Idempotent (only NULL rows are considered) and no-throw per row: a
        payload that will not decode is left NULL, which keeps that row in the
        safe "repair nothing" state rather than inventing a claim set for it.
        """
        from backend.hdencode_candidate_service import _candidate_updates
        protected = set(self._PROTECTED_FIELDS)
        done = 0
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("Database unavailable")
            cur = conn.cursor()
            cur.execute(
                """SELECT hc.canonical_url, d.payload
                   FROM hdencode_candidates hc
                   JOIN hdencode_candidate_details d
                     ON d.canonical_url = hc.canonical_url
                   WHERE hc.hydration_state = 'completed'
                     AND hc.detail_authority_fields IS NULL""")
            for url, payload_json in cur.fetchall():
                try:
                    claimed = set(_candidate_updates(
                        json.loads(payload_json or "{}"))) & protected
                except Exception:
                    # Undecodable or unparseable: leave NULL. "Unknown" is the
                    # conservative state; a fabricated claim set is not.
                    logger.debug("authority backfill skipped %s", url,
                                 exc_info=True)
                    continue
                cur.execute(
                    "UPDATE hdencode_candidates SET detail_authority_fields = ? "
                    "WHERE canonical_url = ?", (json.dumps(sorted(claimed)), url))
                done += 1
            conn.commit()
        if done:
            logger.info("Backfilled detail authority for %d completed row(s)", done)
        return done

    def _reparse_completed_feed_only(self):
        """Re-derive the FEED-authority facts of COMPLETED rows (round-14).

        A completed row is excluded from the ordinary stale sweep — that pass
        overwrites every parsed field, which would destroy the detail facts
        the whole authority model exists to protect. But excluding it entirely
        left a gap: on a grammar change its detail leg is refetched (the
        composite DETAIL_PARSE_VERSION now guarantees that), while the fields
        detail never supplies stayed frozen at the OLD grammar's parse
        forever, because the feed upsert's CASE guards also stop a later poll
        from touching them.

        So this pass re-derives exactly ``_FEED_ONLY_ON_COMPLETED`` from the
        retained feed inputs — offline, through the same shared composition —
        and re-stamps feed_parse_version. Detail-authority fields are not in
        the statement at all, so a refetch in flight cannot be clobbered.
        """
        from backend.sources.hdencode_feed_parser import reparse_feed_facts
        from backend.release_grammar import GRAMMAR_VERSION
        healed = 0
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("Database unavailable")
            cur = conn.cursor()
            cur.execute(
                """SELECT canonical_url, title, categories, raw_description,
                          detail_authority_fields
                   FROM hdencode_candidates
                   WHERE hydration_state = 'completed'
                     AND COALESCE(feed_parse_version, '') != ?""",
                (GRAMMAR_VERSION,))
            for (url, title, categories_json, raw_description,
                 claimed_json) in cur.fetchall():
                try:
                    categories = json.loads(categories_json or "[]")
                except (TypeError, ValueError):
                    categories = []

                feed_owned = self._feed_owned_fields(claimed_json)
                if not feed_owned:
                    # Detail owns every protected field on this row, so there
                    # is nothing the feed may re-derive. Do NOT stamp
                    # feed_parse_version: the stamp certifies that the feed
                    # facts were re-derived under the current grammar, and
                    # here none were. Claiming otherwise is exactly the
                    # over-certification this pass is being fixed for.
                    continue

                facts = reparse_feed_facts(title or "", categories,
                                           raw_description or "")
                assignments = ", ".join(f"{f} = ?" for f in feed_owned)
                params = [self._feed_fact_value(facts, f) for f in feed_owned]
                params += [GRAMMAR_VERSION, url]
                cur.execute(
                    f"""UPDATE hdencode_candidates
                        SET {assignments}, feed_parse_version = ?
                        WHERE canonical_url = ?""", params)
                healed += 1
            conn.commit()
        return healed

    #: reparse_feed_facts names three facts without the ``_evidence`` suffix
    #: its columns carry. A pure KEY rename: the values are already the stored
    #: verdict strings ('asserted' / 'negated' / 'unknown'), not tri-state
    #: booleans, and those columns are NOT NULL DEFAULT 'unknown' -- so passing
    #: a None through here violates the constraint rather than meaning
    #: "absent". ('unknown' IS how absence is spelled.)
    _FEED_FACT_KEY = {
        "dv_evidence": "dv",
        "hdr_evidence": "hdr",
        "hevc_evidence": "hevc",
    }

    def _feed_owned_fields(self, claimed_json):
        """Protected fields the FEED still owns on one completed row.

        ``claimed_json`` is that row's recorded detail claim set. A field is
        feed-owned when detail did not supply it, with two guards:

        - ``None`` (a row written before the column existed) yields NOTHING.
          An unknown claim set is not an empty one; treating it as empty would
          let the repair overwrite detail facts it cannot see. Those rows heal
          on their next hydration, which records a claim set.
        - Coupled groups move together. If detail claimed any member, the
          whole group stays detail-owned, so a new-grammar HDR format list can
          never land beside an old-grammar HDR verdict.
        """
        if claimed_json is None:
            return ()
        try:
            claimed = set(json.loads(claimed_json))
        except (TypeError, ValueError):
            # Undecodable is unknown, not empty -- same reasoning as None.
            return ()
        for group in self._COUPLED_FIELD_GROUPS:
            if claimed & set(group):
                claimed |= set(group)
        return tuple(f for f in self._PROTECTED_FIELDS if f not in claimed)

    @staticmethod
    def _feed_fact_value(facts, field):
        """One feed fact, converted to what its column stores."""
        key = DatabaseManager._FEED_FACT_KEY.get(field, field)
        value = facts.get(key)
        if field in DatabaseManager._FEED_FACT_KEY:
            # NOT NULL DEFAULT 'unknown': absence is spelled, never NULL.
            return value or "unknown"
        if field == "media_type_provisional":
            return 1 if value else 0
        if field in ("media_type_because", "hdr_formats"):
            return json.dumps(list(value or []))
        return value

    def _reparse_stale_candidates(self):
        """Offline re-derivation (round-10 ratified: candidate rows retain
        title/categories/raw_description, so the feed grammar can reparse
        them without any network). Uses THE shared composition -- the same
        function live ingest uses -- then re-stamps and returns the row to
        'current'. relevance_state stays 'unclassified': classification is a
        downstream verdict and re-runs on the corrected facts."""
        from backend.sources.hdencode_feed_parser import reparse_feed_facts
        from backend.release_grammar import GRAMMAR_VERSION
        healed = 0
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("Database unavailable")
            cur = conn.cursor()
            cur.execute(
                """SELECT canonical_url, title, categories, raw_description
                   FROM hdencode_candidates
                   WHERE derived_state = 'stale'
                     AND hydration_state != 'completed'""")
            rows = cur.fetchall()
            # NOTE the exclusion above is load-bearing: this pass overwrites
            # every parsed field wholesale, which is right for a row with no
            # detail facts and destructive for one that has them. Completed
            # rows get the narrow pass in _reparse_completed_feed_only().
            for url, title, categories_json, raw_description in rows:
                try:
                    categories = json.loads(categories_json or "[]")
                except (TypeError, ValueError):
                    categories = []
                facts = reparse_feed_facts(title or "", categories,
                                           raw_description or "")
                cur.execute(
                    """UPDATE hdencode_candidates SET
                           media_type = ?, media_type_provisional = ?,
                           media_type_because = ?, clean_title = ?,
                           title_year = ?, description_year = ?,
                           season = ?, episode = ?, episode_end = ?,
                           resolution = ?, size_text = ?, size_gb = ?,
                           dv_evidence = ?, hdr_evidence = ?,
                           hevc_evidence = ?, hdr_formats = ?,
                           description_complete = ?,
                           feed_parse_version = ?, derived_state = 'current'
                       WHERE canonical_url = ?""",
                    (facts["media_type"],
                     0 if facts["media_type_provisional"] is False else 1,
                     json.dumps(list(facts["media_type_because"])),
                     facts["clean_title"], facts["title_year"],
                     facts["description_year"], facts["season"],
                     facts["episode"], facts["episode_end"],
                     facts["resolution"], facts["size_text"],
                     facts["size_gb"], facts["dv"], facts["hdr"],
                     facts["hevc"], json.dumps(list(facts["hdr_formats"])),
                     1 if facts["description_complete"] else 0,
                     GRAMMAR_VERSION, url))
                healed += 1
            conn.commit()
        return healed

    def complete_hdencode_hydration(
        self,
        canonical_url,
        *,
        payload,
        candidate_updates,
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        updates = dict(candidate_updates or {})
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")

            # The detail claim set is CUMULATIVE, not per-payload.
            #
            # Every protected column is written with COALESCE, so a value
            # stays whatever the last non-NULL write left there. If an earlier
            # detail payload supplied `resolution` and a later refetch omits
            # it, the STORED resolution is still detail-derived -- COALESCE
            # kept it. Recording only this payload's keys would then mark that
            # column feed-owned and let the feed repair overwrite a detail
            # fact with a lower-authority one, which is the exact downgrade
            # the authority model exists to prevent. Verified against the real
            # sink before this was added: rich hydration then sparse refetch
            # left resolution='2160P' with 'resolution' absent from a
            # per-payload claim set.
            #
            # Union is therefore the correct accumulation. It only ever grows,
            # which is conservative in the safe direction: the repair declines
            # to touch a field rather than risking a downgrade.
            prior = conn.execute(
                "SELECT detail_authority_fields FROM hdencode_candidates "
                "WHERE canonical_url = ?", (canonical_url,)).fetchone()
            claimed = set(updates) & set(self._PROTECTED_FIELDS)
            if prior and prior[0]:
                try:
                    claimed |= set(json.loads(prior[0]))
                except (TypeError, ValueError):
                    pass    # undecodable prior: keep this payload's claims
            conn.execute(
                """
                INSERT INTO hdencode_candidate_details (
                    canonical_url, hydrated_at, payload
                ) VALUES (?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    hydrated_at = excluded.hydrated_at,
                    payload = excluded.payload
                """,
                (canonical_url, now, json.dumps(payload, default=str)),
            )
            conn.execute(
                """
                UPDATE hdencode_hydration_queue
                SET state = 'completed',
                    completed_at = ?,
                    claimed_at = NULL,
                    last_error_code = NULL
                WHERE canonical_url = ?
                """,
                (now, canonical_url),
            )
            conn.execute(
                """
                UPDATE hdencode_candidates
                SET clean_title = COALESCE(?, clean_title),
                    title_year = COALESCE(?, title_year),
                    description_year = COALESCE(?, description_year),
                    season = COALESCE(?, season),
                    episode = COALESCE(?, episode),
                    episode_end = COALESCE(?, episode_end),
                    resolution = COALESCE(?, resolution),
                    size_text = COALESCE(?, size_text),
                    size_gb = COALESCE(?, size_gb),
                    dv_evidence = COALESCE(?, dv_evidence),
                    hdr_evidence = COALESCE(?, hdr_evidence),
                    hevc_evidence = COALESCE(?, hevc_evidence),
                    hdr_formats = COALESCE(?, hdr_formats),
                    imdb_id = COALESCE(?, imdb_id),
                    -- Hydrated detail outranks the title, so if it resolves the
                    -- media type that verdict must land. Without these three
                    -- columns the values _candidate_updates now produces would
                    -- be computed and silently dropped at this boundary, which
                    -- is the same failure this whole change exists to fix.
                    media_type = COALESCE(?, media_type),
                    media_type_provisional = COALESCE(?, media_type_provisional),
                    media_type_because = COALESCE(?, media_type_because),
                    description_complete = CASE
                        WHEN ? THEN 1 ELSE description_complete
                    END,
                    detail_parse_version = ?,
                    -- Exactly which protected fields THIS payload supplied.
                    -- Without it the feed-repair pass cannot tell a
                    -- detail-owned field from a feed-owned one on a completed
                    -- row, because COALESCE erases the distinction.
                    detail_authority_fields = ?,
                    derived_state = 'current',
                    hydration_state = 'completed',
                    identity_state = COALESCE(?, identity_state),
                    relevance_state = 'unclassified',
                    detail_reason = NULL,
                    updated_at = ?
                WHERE canonical_url = ?
                """,
                (
                    updates.get("clean_title"),
                    updates.get("title_year"),
                    updates.get("description_year"),
                    updates.get("season"),
                    updates.get("episode"),
                    updates.get("episode_end"),
                    updates.get("resolution"),
                    updates.get("size_text"),
                    updates.get("size_gb"),
                    updates.get("dv_evidence"),
                    updates.get("hdr_evidence"),
                    updates.get("hevc_evidence"),
                    (
                        json.dumps(updates["hdr_formats"])
                        if "hdr_formats" in updates
                        else None
                    ),
                    updates.get("imdb_id"),
                    updates.get("media_type"),
                    (
                        (1 if updates["media_type_provisional"] else 0)
                        if "media_type_provisional" in updates
                        else None
                    ),
                    (
                        json.dumps(updates["media_type_because"])
                        if "media_type_because" in updates
                        else None
                    ),
                    1 if updates.get("description_complete") else 0,
                    # The DETAIL extraction's own version, not the grammar's:
                    # what this stamp certifies is "the scraper that produced
                    # these facts" (see DETAIL_PARSE_VERSION's doc comment).
                    self._detail_parse_version(),
                    json.dumps(sorted(claimed)),
                    updates.get("identity_state"),
                    now,
                    canonical_url,
                ),
            )

    def fail_hdencode_hydration(self, canonical_url, *, error_code):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                """
                UPDATE hdencode_hydration_queue
                SET state = CASE
                        WHEN attempts >= 3 THEN 'failed'
                        ELSE 'queued'
                    END,
                    last_error_code = ?,
                    claimed_at = NULL
                WHERE canonical_url = ?
                """,
                (error_code, canonical_url),
            )
            conn.execute(
                """
                UPDATE hdencode_candidates
                SET hydration_state = CASE
                        WHEN (
                            SELECT attempts
                            FROM hdencode_hydration_queue
                            WHERE canonical_url = ?
                        ) >= 3
                        THEN 'failed'
                        ELSE 'queued'
                    END,
                    updated_at = ?
                WHERE canonical_url = ?
                """,
                (canonical_url, now, canonical_url),
            )

    def release_hdencode_hydration(self, canonical_url, *, reason):
        """Requeue lifecycle cancellation without consuming a failure attempt."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                """
                UPDATE hdencode_hydration_queue
                SET state = 'queued',
                    attempts = MAX(attempts - 1, 0),
                    claimed_at = NULL,
                    last_error_code = ?
                WHERE canonical_url = ? AND state = 'running'
                """,
                (reason, canonical_url),
            )
            conn.execute(
                """
                UPDATE hdencode_candidates
                SET hydration_state = 'queued',
                    updated_at = ?
                WHERE canonical_url = ?
                  AND hydration_state = 'running'
                """,
                (now, canonical_url),
            )

    def recover_hdencode_hydration_queue(self, *, stale_after_minutes=30):
        """Requeue claims left running by a crashed process."""
        try:
            minutes = max(1, int(stale_after_minutes))
        except (TypeError, ValueError):
            minutes = 30
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        cutoff = (now_dt - datetime.timedelta(minutes=minutes)).isoformat()
        now = now_dt.isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            urls = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT canonical_url
                    FROM hdencode_hydration_queue
                    WHERE state = 'running'
                      AND (claimed_at IS NULL OR claimed_at < ?)
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            for canonical_url in urls:
                conn.execute(
                    """
                    UPDATE hdencode_hydration_queue
                    SET state = 'queued',
                        attempts = MAX(attempts - 1, 0),
                        claimed_at = NULL,
                        last_error_code = 'recovered_after_restart'
                    WHERE canonical_url = ?
                    """,
                    (canonical_url,),
                )
                conn.execute(
                    """
                    UPDATE hdencode_candidates
                    SET hydration_state = 'queued',
                        updated_at = ?
                    WHERE canonical_url = ?
                    """,
                    (now, canonical_url),
                )
            return len(urls)

    def list_hdencode_hydration_queue(self, *, limit=500):
        return self._query_dicts(
            """
            SELECT q.*, c.title, c.pub_date, c.media_type,
                   c.resolution, c.dv_evidence
            FROM hdencode_hydration_queue q
            JOIN hdencode_candidates c
              ON c.canonical_url = q.canonical_url
            ORDER BY
                CASE q.state
                    WHEN 'running' THEN 0
                    WHEN 'queued' THEN 1
                    WHEN 'failed' THEN 2
                    WHEN 'cancelled' THEN 3
                    ELSE 4
                END,
                q.priority DESC,
                q.queued_at ASC
            LIMIT ?
            """,
            (max(1, min(int(limit), 5000)),),
            default=[],
        )

    def list_hdencode_current_feed_urls(self, feed_keys=("movies_all", "tv_all")):
        keys=tuple(feed_keys or ())
        if not keys: return []
        placeholders=",".join("?" for _ in keys)
        rows=self._query(
            "SELECT DISTINCT membership.canonical_url "
            "FROM hdencode_candidate_feeds membership "
            "JOIN hdencode_feed_state state ON state.feed_key=membership.feed_key "
            f"WHERE membership.feed_key IN ({placeholders}) "
            "AND state.last_changed_at IS NOT NULL "
            "AND membership.last_seen_at >= state.last_changed_at",
            keys, default=[],
        )
        return [row[0] for row in rows]

    def record_hdencode_shadow_comparison(self, *, cycle_uuid, started_at, completed_at, metrics, catchup_used=False, restart_recovery=False):
        details=dict(metrics)
        misses=list(details.pop("relevant_misses",[]) or [])
        with self.transaction() as conn:
            if not conn: raise RuntimeError("Database unavailable")
            conn.execute(
                """INSERT INTO hdencode_shadow_cycles (
                    cycle_uuid, started_at, completed_at, normal_feeds_complete,
                    rss_requests, listing_requests, rss_count, listing_count,
                    duplicate_count, feed_only_count, listing_only_count,
                    relevant_miss_count, request_reduction_pct, catchup_used,
                    restart_recovery, outcome, details_json, normal_feed_outcomes,
                    listing_complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_uuid,started_at,completed_at,1 if metrics.get("normal_feeds_complete") else 0,
                 int(metrics.get("rss_requests") or 0),int(metrics.get("listing_requests") or 0),
                 int(metrics.get("rss_count") or 0),int(metrics.get("listing_count") or 0),
                 int(metrics.get("duplicate_count") or 0),int(metrics.get("feed_only_count") or 0),
                 int(metrics.get("listing_only_count") or 0),int(metrics.get("relevant_miss_count") or 0),
                 float(metrics.get("request_reduction_pct") or 0),1 if catchup_used else 0,
                 1 if restart_recovery else 0,str(metrics.get("outcome") or "unknown"),
                 json.dumps(details,default=str),
                 # Written NON-NULL by every attribution-aware caller, including
                 # the empty-dict case. Only pre-attribution rows are NULL, and
                 # get_hdencode_shadow_summary depends on that distinction.
                 json.dumps(dict(metrics.get("normal_feed_outcomes") or {}),default=str),
                 # Three-state, matching the column: None when the caller did not
                 # record it (so resolution falls back to the aggregate rule), else
                 # an explicit 0/1.
                 (None if metrics.get("listing_complete") is None
                  else (1 if metrics.get("listing_complete") else 0))),
            )
            for miss in misses:
                conn.execute(
                    "INSERT OR REPLACE INTO hdencode_shadow_misses "
                    "(cycle_uuid, canonical_url, title, status, media_type, "
                    " attribution_basis) VALUES (?, ?, ?, ?, ?, ?)",
                    (cycle_uuid,miss.get("canonical_url"),miss.get("title"),
                     miss.get("status"),miss.get("media_type"),
                     miss.get("attribution_basis")),
                )

    def get_hdencode_rss_dashboard_counts(self):
        candidate_rows=self._query_dicts(
            "SELECT relevance_state AS name, COUNT(*) AS count "
            "FROM hdencode_candidates GROUP BY relevance_state",default=[])
        hydration_rows=self._query_dicts(
            "SELECT state AS name, COUNT(*) AS count "
            "FROM hdencode_hydration_queue GROUP BY state",default=[])
        unknown=self._query(
            """SELECT
                SUM(CASE WHEN dv_evidence='unknown' THEN 1 ELSE 0 END) AS dv,
                SUM(CASE WHEN hdr_evidence='unknown' THEN 1 ELSE 0 END) AS hdr,
                SUM(CASE WHEN identity_state IN ('unknown','ambiguous','hydrated') OR identity_state IS NULL THEN 1 ELSE 0 END) AS identity,
                SUM(CASE WHEN COALESCE(derived_state,'current') != 'current' THEN 1 ELSE 0 END) AS stale_derived,
                        SUM(CASE WHEN title_year IS NOT NULL AND description_year IS NOT NULL AND title_year != description_year THEN 1 ELSE 0 END) AS year_conflict
               FROM hdencode_candidates""",one=True,default=None)
        return {
            "candidate_counts":{row["name"]:int(row["count"]) for row in candidate_rows},
            "hydration_counts":{row["name"]:int(row["count"]) for row in hydration_rows},
            "unknown_counts":{key:int((unknown[key] if unknown else 0) or 0) for key in ("dv","hdr","identity","year_conflict")},
        }

    def get_hdencode_shadow_summary(self, *, window_start_at=None):
        """Aggregate shadow-cycle evidence, optionally scoped to one window.

        ``window_start_at`` is an ISO timestamp; only cycles completed at or
        after it count. Without it this aggregates EVERY row ever recorded,
        which is what made "start a completely fresh qualification window"
        impossible to satisfy: on 2026-08-01 the live table held 206 rows going
        back to 07-22, so a freshly deployed build would have reported
        observed_days=10.67 and successful_cycles=148 from pre-fix evidence,
        while 101 pre-fix relevant misses blocked the gate permanently. Both
        directions wrong at once.

        The rule the plan repeats — never pool pre- and post-change cycles —
        needs a mechanism, not just a policy. This is it. Old rows are kept, so
        the previous window stays available for forensics; it simply stops
        counting toward the current one.
        """
        # EVERY aggregate below must carry this. The merge grafted the window
        # parameter onto main's richer body, and three of the four queries
        # kept main's unscoped WHERE -- so successful_cycles, observed_days,
        # request_reduction_pct, recovery_cycles and relevant_misses were all
        # still computed over EVERY row ever recorded. A window that scopes
        # one query out of four is not a window.
        # Orphan miss rows are deliberately NOT scoped: an orphan has no cycle
        # to take a date from, which is precisely what makes it an orphan.
        scope, params = "", []
        if window_start_at:
            scope = " AND completed_at >= ?"
            params = [str(window_start_at)]
        # Readiness evidence is derived only from structurally eligible cycles:
        # both normal feeds completed, listing membership uncontradicted, and both
        # comparison sides made at least one request.  Incomplete/degenerate rows
        # must not stretch the observation window or improve the request-reduction
        # percentage.
        #
        # LISTING AUTHORITY WAS MISSING FROM THIS WINDOW UNTIL ROUND 7.
        # compare_shadow() withholds `listing_complete` when detail attribution
        # contradicts raw membership, and the per-release resolver honours that --
        # but this query did not, so a cycle the resolver refused to trust still
        # incremented `cycles`, stretched first/last_completed_at, improved the
        # request-reduction figure and counted as restart/catch-up recovery
        # evidence. The top-level qualification claim was therefore calling cycles
        # successful on evidence its own resolver had rejected.
        #
        # NULL is admitted for cycles written before the column existed; those
        # fall back to the aggregate rule, the same legacy compatibility used by
        # cycle_is_valid_evidence_for(). A cycle recorded SINCE the column exists
        # must be an explicit 1.
        eligible=self._query(
            """SELECT COUNT(*) AS cycles,
                      MIN(completed_at) AS first_completed_at,
                      MAX(completed_at) AS last_completed_at,
                      SUM(rss_requests) AS rss_requests,
                      SUM(listing_requests) AS listing_requests,
                      SUM(CASE WHEN restart_recovery=1 OR catchup_used=1 THEN 1 ELSE 0 END) AS recovery_cycles
               FROM hdencode_shadow_cycles
               WHERE outcome IN ('success','relevant_miss')
                 AND normal_feeds_complete=1
                 AND (listing_complete IS NULL OR listing_complete=1)
                 AND rss_requests>0
                 AND listing_requests>0""" + scope,
            tuple(params),one=True,default=None)
        # MISS ACCOUNTING. The 2026-07-21 adversarial audit (f5e3c6e) established
        # that a degraded cycle must not be able to HIDE a real gap. That rule is
        # preserved by ATTRIBUTION rather than any cycle-level proxy: a real movie
        # miss still blocks when tv_all failed, and vice versa.
        #
        # An earlier attempt used `WHERE rss_requests>0`. Peer review refuted it:
        # that count spans catch-up feeds and counts attempted-but-failed
        # requests, so it admitted the stale comparisons it was meant to exclude.
        # Do not reintroduce a request-count proxy here.
        #
        # WHAT THIS CHECK IS, precisely. It is a COUNT- AND EVIDENCE-INTEGRITY
        # check, not independent validation of the producer. A 2026-08-06 review
        # corrected an overstatement on exactly this point: recomputing from the
        # stored rows catches a lying aggregate count, but it reads only the rows
        # the writer chose to insert and trusts the media_type the writer stored.
        # It therefore CANNOT detect a classifier bug, a wrong stored media_type,
        # or a suppressed row -- semantic correctness is established by the
        # adversarial tests over real MediaItem inputs, not here.
        #
        # What it must do is FAIL CLOSED. The first version silently converted
        # malformed provenance to {} and counted zero, and the writer stores a
        # missing normal_feed_outcomes as {} rather than NULL -- so a forgetful
        # caller could file misses that this reader then suppressed. Both
        # deflated the gate, the opposite of the claim made for it. Impossible
        # states are now integrity blockers surfaced in readiness reasons.
        from backend.hdencode_shadow import feed_observation_valid
        integrity=[]
        # EVERY ROW IS ACCOUNTED FOR. The Round 3 version incremented on a valid
        # observation and did nothing otherwise, and reconciled counts only where
        # relevant_miss_count > 0. So a row unsupported by its own provenance was
        # silently dropped whenever the stored count happened to equal the row
        # count -- another route from contradictory evidence to ready=true. My own
        # test asserted that behaviour was correct, which is how it survived.
        #
        # Now each row is sorted into supported / unsupported / corrupt, and
        # anything that is not supported is either counted or flagged. Nothing
        # falls off the end.
        _VALID_MEDIA_TYPES={"movie","tv","unknown"}
        attributed=0
        per_cycle={}

        def _flag(slot,bucket,finding):
            """Count a bad row AND explain it, in one action that cannot split.

            ROUND 6, replacing a backstop that matched strings. Every branch below
            used to do `integrity.append(...)` and `slot[bucket]+=1` as two
            separate statements, and twice a branch did the second without the
            first -- a row counted as bad while the gate reported nothing. The old
            backstop caught that by asking whether ANY finding mentioned the
            cycle, which one unrelated finding satisfies for any number of
            unreported rows in that same cycle. I recorded that limitation rather
            than claiming it closed.

            Now the two are inseparable here, and the reported_* counters make the
            invariant checkable: a future branch that increments a bucket directly
            leaves reported_* behind, and the reconciliation at the end names the
            bucket, the cycle, and the exact shortfall.
            """
            slot[bucket]+=1
            slot["reported_"+bucket]+=1
            integrity.append(finding)

        rows=self._query_dicts(
            "SELECT m.cycle_uuid AS cycle_uuid, m.canonical_url AS url, "
            "       m.media_type AS media_type, "
            "       c.normal_feed_outcomes AS provenance, "
            "       c.normal_feeds_complete AS complete, "
            "       c.relevant_miss_count AS stored_count "
            "FROM hdencode_shadow_misses m "
            "JOIN hdencode_shadow_cycles c ON c.cycle_uuid=m.cycle_uuid "
            # Aliased scope: the join makes a bare `completed_at` ambiguous to a
            # reader even though only c has it. THIS is the query that produces
            # `attributed`, so an unscoped version here silently carried every
            # historical miss into a fresh window -- the exact permanent block
            # the window exists to clear.
            "WHERE c.normal_feed_outcomes IS NOT NULL"
            + scope.replace("completed_at", "c.completed_at"),
            tuple(params),default=[])
        for row in rows:
            cycle=str(row.get("cycle_uuid") or "")
            slot=per_cycle.setdefault(cycle,{"total":0,"supported":0,
                                             "unsupported":0,"corrupt":0,
                                             "reported_unsupported":0,
                                             "reported_corrupt":0})
            slot["total"]+=1
            media_type=row.get("media_type")
            # A persisted media_type outside the vocabulary is corrupt evidence.
            # "unknown" is a legitimate classifier result; NULL or arbitrary text
            # is not, and must not be silently coerced into it.
            if media_type is None or str(media_type) not in _VALID_MEDIA_TYPES:
                _flag(slot,"corrupt",
                      f"media_type_invalid:{cycle}:{media_type!r}")
                continue
            raw=row.get("provenance")
            try:
                provenance=json.loads(raw or "{}")
            except (TypeError,ValueError):
                _flag(slot,"corrupt",f"provenance_unparseable:{cycle}")
                continue
            if not isinstance(provenance,dict):
                _flag(slot,"corrupt",f"provenance_not_an_object:{cycle}")
                continue
            if "_derived_from" in provenance:
                # Written by a caller that supplied no per-feed provenance. The
                # marker deliberately does not fabricate feed outcomes, so the
                # decision falls back to the cycle-level rule it came from.
                #
                # EXACT SCHEMA. Round 4 checked only the marker value and a
                # truthiness comparison, which a 2026-08-06 review showed accepts
                # a missing normal_feeds_complete key, arbitrary extra keys, and
                # pseudo-booleans -- the string "false" is truthy in Python, so a
                # contradictory marker could pass the consistency check and then
                # take the silent-discard path below.
                if set(provenance) != {"_derived_from", "normal_feeds_complete"}:
                    _flag(slot,"corrupt",
                          f"derived_marker_schema:{cycle}:"
                          f"{sorted(provenance)!r}")
                    continue
                if provenance.get("_derived_from")!="cycle_level_completeness":
                    _flag(slot,"corrupt",
                          f"derived_marker_unknown:{cycle}:"
                          f"{provenance.get('_derived_from')!r}")
                    continue
                recorded=provenance.get("normal_feeds_complete")
                if not isinstance(recorded,bool):
                    # bool() would accept "false", 0.0, [] and friends.
                    _flag(slot,"corrupt",
                          f"derived_marker_not_a_boolean:{cycle}:{recorded!r}")
                    continue
                if recorded!=bool(row.get("complete")):
                    _flag(slot,"corrupt",
                          f"derived_marker_contradicts_cycle:{cycle}")
                    continue
                if row.get("complete"):
                    attributed+=1
                    slot["supported"]+=1
                    continue
                # THE ROUND 4 HOLE, one branch over from the one Round 4 closed.
                #
                # This previously incremented "unsupported" and continued with no
                # integrity finding, and the reconciliation below compares the
                # stored count against TOTAL rows rather than SUPPORTED rows -- so
                # stored=1, total=1, supported=0 went completely silent. A comment
                # here claimed "the reconciliation below sees it". It did not.
                #
                # compare_shadow cannot legitimately produce this store: with no
                # per-feed provenance and cycle completeness false, its derived
                # observation set is empty and it cannot emit a miss row at all. A
                # row attached to this marker is therefore writer drift, direct
                # insertion, or corruption -- exactly what this layer exists for.
                _flag(slot,"unsupported",
                      f"miss_row_unsupported_by_derived_completeness:{cycle}")
                continue
            if not provenance:
                # Supplied-empty provenance with a miss row attached is
                # contradictory: compare_shadow cannot attribute anything with no
                # observed feed, so it would never have written this row.
                _flag(slot,"corrupt",f"miss_row_with_empty_provenance:{cycle}")
                continue
            if feed_observation_valid(str(media_type),provenance):
                attributed+=1
                slot["supported"]+=1
            else:
                # THE ROUND 3 HOLE. compare_shadow would never persist a row
                # whose own feed was not observed, so this row should not exist.
                # Dropping it quietly is exactly the fail-open that was supposed
                # to have been removed.
                _flag(slot,"unsupported",
                      f"miss_row_unsupported_by_provenance:{cycle}:{media_type}")
        # Orphan rows are invisible to the join above, and the declared foreign
        # key is not proof they cannot exist: this connection does not enable
        # PRAGMA foreign_keys, so it is not enforced.
        orphans=self._query(
            "SELECT COUNT(*) AS n FROM hdencode_shadow_misses m "
            "WHERE NOT EXISTS (SELECT 1 FROM hdencode_shadow_cycles c "
            "                  WHERE c.cycle_uuid=m.cycle_uuid)",
            one=True,default=None)
        orphan_count=int((orphans["n"] if orphans else 0) or 0)
        if orphan_count:
            integrity.append(f"orphan_miss_rows:{orphan_count}")
        # Reconcile EVERY provenance-aware cycle, including stored zero. The
        # previous query filtered relevant_miss_count > 0, so a cycle claiming
        # zero while carrying rows was never checked -- and would either invent a
        # count or discard the rows, depending on whether they validated.
        for claim in self._query_dicts(
                "SELECT cycle_uuid, relevant_miss_count AS n "
                "FROM hdencode_shadow_cycles "
                "WHERE normal_feed_outcomes IS NOT NULL" + scope,
                tuple(params),default=[]):
            cycle=str(claim.get("cycle_uuid") or "")
            stored=int(claim.get("n") or 0)
            slot=per_cycle.get(cycle)
            total=slot["total"] if slot else 0
            if stored==0 and total==0:
                continue
            if total==0:
                integrity.append(f"count_without_rows:{cycle}:{stored}")
            elif stored!=total:
                integrity.append(
                    f"count_row_disagreement:{cycle}:{stored}!={total}")
        # RECONCILIATION, round 6. Every row counted into a diagnostic bucket must
        # have produced its own finding. Twice a branch incremented a bucket and
        # fell off the end reporting nothing, and both times a test certified the
        # silence as correct.
        #
        # WHAT CHANGED FROM ROUND 5. The previous version asked whether ANY
        # integrity finding mentioned the cycle. That is string association, not
        # accounting: one finding satisfied the check for any number of unreported
        # bad rows in the same cycle, and a test could pass with the backstop
        # deleted. Now each bucket is compared against its own reported counter,
        # so the check is per bucket, per cycle, and reports the exact shortfall.
        #
        # This can only fire if a future branch increments a bucket without going
        # through _flag, which is precisely the mistake being guarded. Readiness
        # stays fail-closed: any finding here is in the blocking list.
        integrity.extend(reconcile_bucket_reporting(per_cycle))
        # Pre-attribution rows cannot be re-derived at all: nothing recorded
        # which feed succeeded, and they carry no media_type to attribute. They
        # are bounded CONSERVATIVELY -- counted only when both normal feeds
        # completed.
        #
        # DIRECTION OF THAT BOUND, corrected 2026-08-06. Because
        # conservative_admitted is a SUBSET of attribution_admitted, it follows
        # that blocking(conservative) <= blocking(attribution). So this bound is
        # safe against FALSELY ACCUSING the feed of a miss, and NOT safe as
        # evidence of overall health: finding zero blockers in the smaller set
        # does not establish zero in the larger one, because an omitted
        # mixed-cycle row could itself be permanently missing. Earlier revisions
        # of this comment claimed it "cannot overstate health", which is
        # backwards. It supports only the admitted-record claim.
        legacy=self._query(
            "SELECT SUM(relevant_miss_count) AS n "
            "FROM hdencode_shadow_cycles "
            "WHERE normal_feed_outcomes IS NULL AND normal_feeds_complete=1"
            + scope,
            tuple(params),one=True,default=None)
        # Categorised so an operator can tell "coverage miss" from "evidence
        # store corrupt" without the gate ever reporting ready.
        findings=sorted(set(integrity))
        by_category={}
        for finding in findings:
            by_category[finding.split(":",1)[0]]=by_category.get(
                finding.split(":",1)[0],0)+1
        misses={"relevant_misses":attributed+int((legacy["n"] if legacy else 0) or 0),
                "miss_evidence_integrity":findings,
                "miss_evidence_integrity_by_category":by_category}
        latest=self._query(
            "SELECT * FROM hdencode_shadow_cycles WHERE 1=1" + scope +
            " ORDER BY completed_at DESC LIMIT 1",
            tuple(params),one=True,default=None)
        listing=int((eligible["listing_requests"] if eligible else 0) or 0)
        rss=int((eligible["rss_requests"] if eligible else 0) or 0)
        reduction=(100.0*(listing-rss)/listing) if listing>0 else 0.0
        return {
            "successful_cycles":int((eligible["cycles"] if eligible else 0) or 0),
            "first_completed_at":eligible["first_completed_at"] if eligible else None,
            "last_completed_at":eligible["last_completed_at"] if eligible else None,
            "relevant_misses":int((misses["relevant_misses"] if misses else 0) or 0),
            # Impossible states found while re-deriving the miss count. Non-empty
            # means the stored evidence contradicts itself, which blocks
            # readiness rather than being silently resolved to zero.
            "miss_evidence_integrity":list((misses or {}).get("miss_evidence_integrity") or []),
            "miss_evidence_integrity_by_category":dict(
                (misses or {}).get("miss_evidence_integrity_by_category") or {}),
            "rss_requests":rss,
            "listing_requests":listing,
            "request_reduction_pct":round(reduction,2),
            "recovery_cycles":int((eligible["recovery_cycles"] if eligible else 0) or 0),
            "latest":dict(latest) if latest is not None else None,
            "window_start_at":str(window_start_at) if window_start_at else None,
        }

    def get_hdencode_miss_resolution(self):
        """Classify every recorded miss as acquired / never acquired / unresolved.

        PER-FEED AUTHORITY, restored 2026-08-07 on peer review. The first version
        sourced misses with `WHERE c.normal_feeds_complete = 1` and admitted
        observation cycles on the same condition. That is the CYCLE-LEVEL rule
        this project spent five review rounds replacing. compare_shadow emits a
        miss when the feed responsible for THAT release was observed -- so a movie
        miss is legitimately recorded in a cycle where movies_all validated and
        tv_all failed, i.e. with normal_feeds_complete = 0. My filter dropped
        exactly those rows out of the gate: a real movie gap stopped blocking
        because an unrelated TV feed had failed. That was a false-ready path.

        Now both halves use `feed_observation_valid`, the same predicate that
        governs miss creation:

          * a miss is ADMITTED if its own feed was observed in its source cycle;
          * a later cycle may RESOLVE it only if that cycle observed its feed.

        Rows whose source cycle predates per-feed provenance keep the older,
        conservative cycle-complete rule -- there is nothing finer to use.

        Malformed evidence is reported, never silently skipped: dropping a cycle
        because its JSON will not parse can remove the only observation after a
        miss, which would quietly turn a decidable row into an unresolved one.
        """
        from backend.hdencode_shadow import (
            feed_observation_valid, summarise_miss_resolutions,
        )

        def _at(value):
            """ISO -> aware UTC datetime, or None. Never raises."""
            try:
                parsed=datetime.datetime.fromisoformat(str(value))
            except (TypeError,ValueError):
                return None
            if parsed.tzinfo is None:
                parsed=parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)

        def _urlset(value,label,cycle,problems):
            """A URL container, or a recorded problem. `set(5)` would raise."""
            if value is None:
                return set()
            if isinstance(value,(str,bytes)) or not isinstance(value,(list,tuple,set)):
                problems.append(f"{label}_not_a_list:{cycle}:{type(value).__name__}")
                return None
            return {str(v) for v in value}

        problems=[]
        cycles=[]
        # UNRESOLVED LISTING-ONLY CANDIDATES, keyed by URL. Replaces a running
        # total of every historical detail failure, which round 7 showed was the
        # wrong granularity in two directions:
        #
        #   * A URL present in BOTH the RSS feed and the raw listing whose detail
        #     scrape failed is a DUPLICATE -- the thing RSS is supposed to find,
        #     found. It cannot be an RSS miss, and it was blocking readiness.
        #   * A genuinely listing-only URL that failed detail once and was
        #     attributed fine on the next cycle blocked FOREVER, because a sum over
        #     history has no way to subtract. Readiness could never recover from a
        #     single transient scrape failure, which is not a property anyone chose.
        #
        # So candidacy is `detail_failed & listing_only` -- a listing row we could
        # not attribute AND that RSS did not carry -- and later evidence clears it.
        # The dict is keyed by URL rather than counted so that the same URL failing
        # in three cycles is one unresolved candidate, not three.
        candidate_state={}
        for row in self._query_dicts(
                "SELECT cycle_uuid, completed_at, outcome, normal_feeds_complete, "
                "       rss_requests, listing_requests, details_json, "
                "       normal_feed_outcomes, listing_complete "
                "FROM hdencode_shadow_cycles "
                "WHERE details_json IS NOT NULL ORDER BY completed_at",
                default=[]):
            cycle=str(row.get("cycle_uuid") or "")
            # ADMIT incomplete_feeds, corrected 2026-08-07 on peer review.
            #
            # This filter used to require outcome in ("success","relevant_miss"),
            # and compare_shadow writes "incomplete_feeds" whenever
            # normal_feeds_complete is false -- which is exactly the mixed-feed
            # case the per-feed rule exists to handle. So a cycle with
            # movies_all=changed and tv_all=failed was discarded HERE, before
            # cycle_is_valid_evidence_for() could ever see it. The helper was
            # right and unreachable.
            #
            # Admitting it is only safe because listing trustworthiness is now a
            # separate authority: cycle_is_valid_evidence_for() requires the
            # listing arm AND the relevant feed, so an "incomplete_feeds" cycle
            # whose LISTING failed still cannot resolve anything.
            # ADMIT THE INCONCLUSIVE OUTCOMES TOO, 2026-08-19. The hybrid-sweep
            # merge added three fail-closed guards to compare_shadow -- 
            # no_listing_baseline, no_rss_observations, disjoint_identity_sets --
            # which OVERRIDE the ordinary outcome. None was in this filter, so a
            # guarded cycle was discarded here and its unattributed candidates
            # stopped blocking: readiness could go clean on a cycle the guard had
            # just declared unusable. That is the exact failure the note above
            # records for incomplete_feeds -- "the helper was right and
            # unreachable" -- reintroduced by a different route.
            #
            # Admitting them is safe for the same reason admitting incomplete_feeds
            # was: cycle_is_valid_evidence_for() requires the listing arm AND the
            # relevant feed, so an inconclusive cycle still cannot RESOLVE anything.
            # It can only continue to BLOCK, which is what a guard should do.
            if not (row.get("outcome") in ("success","relevant_miss",
                                           "incomplete_feeds",
                                           "no_listing_baseline",
                                           "no_rss_observations",
                                           "disjoint_identity_sets")
                    and int(row.get("rss_requests") or 0)>0
                    and int(row.get("listing_requests") or 0)>0):
                continue
            at=_at(row.get("completed_at"))
            if at is None:
                problems.append(f"cycle_completed_at_unparseable:{cycle}")
                continue
            try:
                details=json.loads(row.get("details_json") or "{}")
            except (TypeError,ValueError):
                problems.append(f"details_json_unparseable:{cycle}")
                continue
            if not isinstance(details,dict):
                problems.append(f"details_json_not_an_object:{cycle}")
                continue
            # Genuine attribution failures recorded by the crawl. Distinct from
            # detail_dropped, which mixes in cached skips and policy exclusions.
            failed=details.get("detail_failed")
            if isinstance(failed,(list,tuple)):
                failed_urls={str(u) for u in failed if u}
            else:
                if failed:
                    problems.append(f"detail_failed_not_a_list:{cycle}")
                failed_urls=set()
            listing=_urlset(details.get("listing_only"),"listing_only",cycle,problems)
            feed=_urlset(details.get("feed_only"),"feed_only",cycle,problems)
            # Cycles written before round 8 have no duplicate_urls; _urlset returns
            # an empty set for a missing key, which is the conservative reading --
            # absent evidence clears nothing.
            duplicates=_urlset(
                details.get("duplicate_urls"),"duplicate_urls",cycle,problems)
            if listing is None or feed is None or duplicates is None:
                continue
            outcomes=self._normal_feed_outcomes(row, cycle, problems)
            # STRICTLY NULL/0/1. The column is an unconstrained INTEGER, and
            # bool() would turn 2 or -1 into True -- i.e. corrupt data would grant
            # listing authority. Anything else is an evidence problem.
            raw_listing_ok=row.get("listing_complete")
            if raw_listing_ok is None:
                listing_ok=None
            elif raw_listing_ok in (0,1,True,False):
                # Identity against the allowed values, NOT int() coercion. Round 5
                # pointed out that int() accepts "1" and RAISES on "garbage" --
                # taking readiness down with a ValueError instead of recording an
                # evidence problem. SQLite affinity does not prevent stored text.
                listing_ok=bool(raw_listing_ok)
            else:
                problems.append(
                    f"listing_complete_invalid:{cycle}:{raw_listing_ok!r}")
                listing_ok=None
            # CANDIDATE BOOKKEEPING. Chronological -- the query above is
            # ORDER BY completed_at -- so a later cycle's evidence overwrites an
            # earlier cycle's verdict for the same URL.
            #
            # The asymmetry is deliberate. CREATING a candidate is conservative
            # (it blocks), so an untrusted cycle may still raise one. CLEARING is
            # permissive, so a cycle whose listing membership is contradicted
            # (listing_complete=False) must not clear anything -- otherwise a
            # cycle the resolver refuses to trust could still be the thing that
            # unblocks readiness, which is the same fail-open shape as HIGH 2.
            # Legacy NULL is allowed to clear: those cycles predate the column and
            # are governed by the aggregate rule everywhere else.
            for url in failed_urls & listing:
                candidate_state[url]=True
            # CLEARING REQUIRES AFFIRMATIVE RSS CARRIAGE. Corrected on peer review
            # round 8, which found the previous rule fail-open -- and it was worse
            # than the review described. It cleared on
            #
            #     (listing_only | feed_only) - detail_failed
            #
            # but `listing_only` MEANS RSS DID NOT CARRY THE URL. That is the
            # miss-candidate set. So a later cycle where the release was still
            # missing from RSS -- and merely had a working detail scrape -- deleted
            # the blocker. I was clearing an RSS-coverage blocker using evidence of
            # an RSS coverage gap, and if the relevant feed had not been validly
            # observed that cycle, no graded miss row was created to take over. The
            # blocker vanished and nothing replaced it.
            #
            # The only affirmative evidence that RSS carried a URL is:
            #   feed_only      -- in RSS, not in the listing
            #   duplicate_urls -- in RSS AND in the listing  <-- see Finding 2
            # Their union is exactly "this cycle's RSS set, as far as it concerns
            # URLs we know about". Nothing else in a persisted cycle establishes it.
            #
            # The second legitimate exit is OWNERSHIP TRANSFER to a graded miss row,
            # applied after the miss loop below, since that is where admission by
            # feed validity is decided.
            rss_carried=feed | duplicates
            if listing_ok is not False:
                for url in rss_carried:
                    if url in candidate_state:
                        candidate_state[url]=False
            cycles.append({"at":at,"listing_only":listing,"feed_only":feed,
                           # CARRIED TO THE RESOLVER TOO, added on peer review
                           # round 9. I parsed `duplicates` above, used it for
                           # candidate clearing, and then did not put it in the
                           # records the miss resolver reads -- so the resolver
                           # could not see the evidence even in principle. Sixth
                           # time in this effort that I have wired new evidence to
                           # ONE consumer and left another consumer of the SAME
                           # function blind to it.
                           "duplicate_urls":duplicates,
                           "outcomes":outcomes,
                           "listing_complete":listing_ok,
                           "cycle_complete":bool(row.get("normal_feeds_complete")),
                           # THE OUTCOME TRAVELS WITH THE CYCLE. Peer review round 11 (I2):
                           # admitting the three inconclusive outcomes into this resolver let
                           # them become ORDINARY observations, because the validity predicate
                           # checks listing_complete and per-feed outcomes and never looks at
                           # `outcome`. So a guard cycle could CLEAR a candidate and resolve a
                           # prior miss -- the exact opposite of the comment I wrote claiming
                           # they "can only continue to BLOCK". They could not block if they
                           # were excluded, and they could do far more than block once admitted.
                           "outcome":str(row.get("outcome") or "")})

        misses=[]
        for row in self._query_dicts(
                "SELECT m.canonical_url AS url, m.media_type AS media_type, "
                "       c.cycle_uuid AS cycle_uuid, c.completed_at AS at, "
                "       c.normal_feeds_complete AS complete, "
                "       c.normal_feed_outcomes AS provenance "
                "FROM hdencode_shadow_misses m "
                "JOIN hdencode_shadow_cycles c ON c.cycle_uuid=m.cycle_uuid",
                default=[]):
            cycle=str(row.get("cycle_uuid") or "")
            media_type=row.get("media_type")
            source_outcomes=self._normal_feed_outcomes(row, cycle, problems)
            if source_outcomes is None:
                # Pre-provenance row: fall back to the conservative cycle rule.
                admitted=bool(row.get("complete"))
            else:
                # NULL media_type is a pre-attribution legacy row, not a
                # movie: read it as "unknown", which requires both feeds.
                admitted=feed_observation_valid(
                    str(media_type) if media_type is not None else "unknown",
                    source_outcomes)
            if not admitted:
                continue
            misses.append({"url":row.get("url"),"media_type":media_type,
                           "at":_at(row.get("at"))})
        # OWNERSHIP TRANSFER, the second and only other legitimate way out of
        # candidate state. Applied HERE and not in the cycles loop because this is
        # where admission is decided: a miss row only lands in `misses` if its
        # relevant feed observation was valid (or, for a pre-provenance row, the
        # conservative cycle rule held).
        #
        # The distinction round 8 required: a later detail success must not merely
        # DELETE the blocker, it must hand the URL over to something that still
        # blocks. Once an admitted miss row exists, the normal miss-resolution
        # machinery owns that URL -- it will be graded acquired / never_acquired /
        # undetermined / not_yet_assessable, and every one of those states except
        # `acquired` blocks readiness on its own. So dropping it from the
        # unattributed set is a genuine transfer of responsibility rather than an
        # erasure.
        #
        # Note this deliberately does NOT check whether the miss row's own verdict
        # is favourable. That is not this function's job, and making candidacy
        # depend on the outcome would double-count the same URL in two blockers.
        for url in {str(m.get("url") or "") for m in misses}:
            if url in candidate_state:
                candidate_state[url]=False
        summary=summarise_miss_resolutions(misses,cycles)
        summary["evidence_problems"]=problems
        unresolved=sorted(u for u,pending in candidate_state.items() if pending)
        summary["unattributed_candidates"]=len(unresolved)
        # The URLs themselves, so the readiness reason can be acted on instead of
        # only counted. A bare "3 candidates" cannot be investigated.
        summary["unattributed_candidate_urls"]=unresolved
        return summary

    def _normal_feed_outcomes(self, row, cycle, problems):
        """Parse a cycle's per-feed outcome map. None means 'not recorded'.

        Distinguishes "this cycle predates provenance" (None -> caller falls back
        to the cycle-level rule) from "provenance is present but unreadable"
        (recorded as a problem, treated as no observation) -- because silently
        reading corrupt provenance as an empty map would make every miss look
        unobservable, which is not the same thing as absent.
        """
        raw=row.get("provenance") if "provenance" in row else row.get("normal_feed_outcomes")
        if raw is None:
            return None
        try:
            parsed=json.loads(raw or "{}")
        except (TypeError,ValueError):
            problems.append(f"normal_feed_outcomes_unparseable:{cycle}")
            return {}
        if not isinstance(parsed,dict):
            problems.append(f"normal_feed_outcomes_not_an_object:{cycle}")
            return {}
        if "_derived_from" in parsed:
            # A CYCLE-LEVEL FALLBACK MARKER. Corrected 2026-08-07 on peer review.
            #
            # This returned {} -- an explicit "no feed observed" -- which meant a
            # miss recorded under such a marker was never ADMITTED (admission tests
            # `is None` to decide the legacy fallback, and {} is not None). But the
            # marker's whole purpose is to say "no per-feed data was recorded here,
            # use the cycle-level rule", which is exactly the legacy case. So it
            # must read as None, not as an empty observation.
            #
            # Validate the schema before granting that fallback: an unrecognised or
            # malformed marker is corrupt evidence, not a licence to fall back.
            if (set(parsed) == {"_derived_from", "normal_feeds_complete"}
                    and parsed.get("_derived_from") == "cycle_level_completeness"
                    and isinstance(parsed.get("normal_feeds_complete"), bool)):
                return None
            problems.append(f"derived_marker_invalid:{cycle}:{sorted(parsed)!r}")
            return {}
        return {str(k):str(v) for k,v in parsed.items()}

    def get_hdencode_rss_readiness(self, *, min_cycles=20, min_days=7, max_stale_minutes=180,
                                   window_start_at=None):
        required_cycles=max(1,int(min_cycles)); required_days=max(1,int(min_days))
        # The PERSISTED boundary is the authority; `window_start_at` is what
        # configuration currently claims. They must agree exactly. Without this
        # the zero-miss requirement could be defeated by editing a file to slide
        # the boundary past an observed miss.
        active = self.get_active_qualification_window()
        configured = self.normalize_window_start(window_start_at)
        boundary_changed = False
        if active:
            if configured and configured != active["window_start_at"]:
                boundary_changed = True
            elif not configured:
                # Configuration cleared while a window is live — treat as a
                # change, not as "fall back to the persisted value".
                boundary_changed = True
        window_start_at = active["window_start_at"] if active else None
        if window_start_at and boundary_changed:
            summary = self.get_hdencode_shadow_summary(window_start_at=window_start_at)
            return {
                "ready": False, "window_start_at": window_start_at,
                "required_cycles": required_cycles,
                "successful_cycles": summary["successful_cycles"],
                "required_days": required_days, "observed_days": 0.0,
                "normal_feeds_healthy": False,
                "relevant_misses": summary["relevant_misses"],
                "request_reduction_pct": summary["request_reduction_pct"],
                "recovery_cycles": summary["recovery_cycles"],
                "first_completed_at": summary["first_completed_at"],
                "last_completed_at": summary["last_completed_at"],
                "reasons": ["qualification_window_boundary_changed"],
                "configured_window_start_at": configured,
            }
        if not window_start_at:
            # NO WINDOW: return an EMPTY current-window summary.
            #
            # Returning the unscoped historical totals here — even alongside a
            # blocking reason — is not merely untidy, it is actively dangerous.
            # The qualification collector reads `relevant_misses` on its own and
            # converts any nonzero value into a MANDATORY STOP with a priority-8
            # push alert and a "stop and roll back" instruction. With 102 void
            # misses in the table, that alert would fire from the previous
            # window's evidence before the new one had even started.
            #
            # Historical totals are still available, but under an explicitly
            # named diagnostic key that no gate consumes. Caught in review.
            historical = self.get_hdencode_shadow_summary()
            # EXACTLY ONE REASON HERE. Asserted by name in
            # test_the_only_reason_is_that_no_window_has_started, and the rationale
            # is about what the COLLECTOR consumes: it reads relevant_misses
            # independently and turns any nonzero value into a mandatory stop with a
            # priority-8 push. With 102 void misses live that fired before the new
            # window existed.
            #
            # NARROWED on peer review round 11 (T1). My first wording here was
            # "integrity is meaningful INSIDE a window", and that is too broad: the
            # independent mirror deliberately leaves shadow_miss_count_mismatch
            # UNSCOPED, because count-vs-row corruption is database integrity and
            # does not stop being true outside a window.
            #
            # The real distinction is about EFFECT, not meaning:
            #
            #   qualification-readiness effects  -> scoped to the active window
            #   historical evidence corruption   -> still true at all times, but
            #                                       must not masquerade as a
            #                                       current-window miss
            #
            # So the no-window response carries exactly one READINESS REASON while
            # global integrity stays available diagnostically -- it simply does not
            # gate, and does not fire the mandatory-stop path that reads counts.
            # main's integrity tests declare a window because they assert readiness
            # EFFECTS, not because integrity stops existing without one.
            no_window_reasons = ["qualification_window_not_started"]
            return {
                "ready": False,
                "window_start_at": None,
                "required_cycles": required_cycles, "successful_cycles": 0,
                "required_days": required_days, "observed_days": 0.0,
                "normal_feeds_healthy": False,
                "relevant_misses": 0, "request_reduction_pct": 0.0,
                "recovery_cycles": 0,
                "first_completed_at": None, "last_completed_at": None,
                "reasons": no_window_reasons,
                "historical_evidence_not_counted": {
                    "successful_cycles": historical["successful_cycles"],
                    "relevant_misses": historical["relevant_misses"],
                    "first_completed_at": historical["first_completed_at"],
                    "last_completed_at": historical["last_completed_at"],
                },
            }
        required_cycles=max(1,int(min_cycles)); required_days=max(1,int(min_days)); summary=self.get_hdencode_shadow_summary(window_start_at=window_start_at)
        first=summary.get("first_completed_at"); last=summary.get("last_completed_at"); observed_days=0.0
        try:
            first_dt=datetime.datetime.fromisoformat(first); last_dt=datetime.datetime.fromisoformat(last)
            if first_dt.tzinfo is None: first_dt=first_dt.replace(tzinfo=datetime.timezone.utc)
            if last_dt.tzinfo is None: last_dt=last_dt.replace(tzinfo=datetime.timezone.utc)
            observed_days=max(0.0,(last_dt-first_dt).total_seconds()/86400.0)
        except (TypeError,ValueError): pass
        feed_rows=self._query_dicts(
            "SELECT feed_key,last_status,consecutive_failures,last_checked_at FROM hdencode_feed_state "
            "WHERE feed_key IN ('movies_all','tv_all')",default=[])
        by_key={row["feed_key"]:row for row in feed_rows}; now=datetime.datetime.now(datetime.timezone.utc)
        def fresh(row):
            try:
                value=datetime.datetime.fromisoformat(row.get("last_checked_at"))
                if value.tzinfo is None: value=value.replace(tzinfo=datetime.timezone.utc)
                return (now-value.astimezone(datetime.timezone.utc)).total_seconds() <= max(15,int(max_stale_minutes))*60
            except (TypeError,ValueError): return False
        feeds_healthy=all(key in by_key and by_key[key].get("last_status") in (200,304) and int(by_key[key].get("consecutive_failures") or 0)==0 and fresh(by_key[key]) for key in ("movies_all","tv_all"))
        reasons=[]
        if summary["successful_cycles"]<required_cycles: reasons.append("insufficient_comparison_cycles")
        if observed_days<required_days: reasons.append("insufficient_observation_days")
        # THE MISS RULE, changed 2026-08-07 on Jesse's decision. This used to be
        # `if summary["relevant_misses"] > 0`, which blocked on ANY listing-only
        # observation ever recorded. That could never pass: 99 of 100 such
        # releases were acquired anyway, median about an hour, all via the normal
        # feeds, so the gate treated ordinary polling latency as permanent
        # coverage loss and RSS would have stayed in shadow mode indefinitely.
        #
        # Now only a release that was NEVER acquired counts, with no deadline.
        # UNDETERMINED rows -- ones that left the listing without ever appearing
        # in the feed -- still block, because "cannot be proven either way" is not
        # evidence of health, and calling it health is the fail-open shape that
        # produced two HIGH findings in this same subsystem.
        resolution=self.get_hdencode_miss_resolution()
        if int(resolution.get("never_acquired") or 0)>0:
            reasons.append("unacquired_misses_detected")
        if int(resolution.get("undetermined") or 0)>0:
            reasons.append("miss_resolution_undetermined")
        # PENDING BLOCKS, reversed 2026-08-07 on peer review. I had excluded
        # not_yet_assessable so the gate could pass. The review showed why that is
        # unsafe rather than merely optimistic: the shadow comparison is recorded
        # only while discovery_mode == "rss_shadow", so promoting to rss_primary
        # stops producing the very observations a pending row needs. The gate
        # would open on evidence its own promoted mode destroys.
        if int(resolution.get("not_yet_assessable") or 0)>0:
            reasons.append("miss_resolution_pending")
        # Unreadable evidence is not the same as clean evidence. Skipping a
        # malformed cycle can remove the only observation after a miss, so it is
        # reported rather than absorbed.
        if resolution.get("evidence_problems"):
            reasons.append("miss_resolution_evidence_unreadable")
        # UNATTRIBUTED IN-SCOPE CANDIDATES BLOCK. Round 6: a listing-only release
        # whose detail scrape failed is not booked as a miss (it has no media type,
        # so it cannot be attributed to a feed) -- and it was therefore vanishing
        # from readiness entirely. A false-health under-count. It must block the
        # claim that no unacquired misses exist, without invalidating the cycle's
        # membership evidence for resolving OTHER misses.
        # Counted STRUCTURALLY by the loader, which already parses details_json.
        # My first attempt here used `details_json LIKE '%detail_failed%'` -- string
        # matching against JSON, which is the exact anti-pattern the RSS
        # round-6 work removed from this same file. A schema change or a key
        # appearing inside a URL would break it silently.
        if int(resolution.get("unattributed_candidates") or 0) > 0:
            reasons.append("unattributed_listing_candidates")
        # An integrity failure is not "zero misses". Malformed provenance, a
        # count that disagrees with the rows on disk, a nonzero count with no
        # rows, or a miss row filed against supplied-empty provenance all mean
        # the evidence contradicts itself. Before 2026-08-06 each of these
        # silently contributed zero, which DEFLATED the gate -- the opposite of
        # the protection claimed for it. They must block instead.
        if summary.get("miss_evidence_integrity"):
            reasons.append("miss_evidence_integrity_failed")
        if summary["request_reduction_pct"]<=0: reasons.append("request_reduction_not_proven")
        if summary["recovery_cycles"]<1: reasons.append("restart_or_catchup_recovery_not_proven")
        if not feeds_healthy: reasons.append("normal_feeds_unhealthy_or_stale")
        return {"ready":not reasons,"window_start_at":summary.get("window_start_at"),"required_cycles":required_cycles,"successful_cycles":summary["successful_cycles"],"required_days":required_days,"observed_days":observed_days,"normal_feeds_healthy":feeds_healthy,"relevant_misses":summary["relevant_misses"],"misses_acquired":int(resolution.get("acquired") or 0),"misses_never_acquired":int(resolution.get("never_acquired") or 0),"misses_undetermined":int(resolution.get("undetermined") or 0),"misses_not_yet_assessable":int(resolution.get("not_yet_assessable") or 0),"miss_evidence_problems":list(resolution.get("evidence_problems") or []),"worst_acquisition_lag_hours":resolution.get("worst_acquisition_lag_hours"),"request_reduction_pct":summary["request_reduction_pct"],"recovery_cycles":summary["recovery_cycles"],"first_completed_at":first,"last_completed_at":last,"reasons":reasons}

    # ── HDEncode candidate actions ─────────────────────────────────────

    def get_active_qualification_window(self):
        """The persisted boundary, or None if no window has been started.

        This — not configuration — is the authority. Configuration is compared
        against it and a mismatch blocks, so editing a file cannot silently move
        the line that decides which evidence counts.
        """
        row = self._query(
            "SELECT * FROM hdencode_qualification_window "
            "WHERE superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            one=True, default=None)
        return dict(row) if row is not None else None

    def count_cycles_in_window(self, window_start_at):
        """Cycles recorded at or after a boundary — what makes it immutable."""
        row = self._query(
            "SELECT COUNT(*) AS n FROM hdencode_shadow_cycles WHERE completed_at >= ?",
            (str(window_start_at),), one=True, default=None)
        return int(row["n"]) if row else 0

    def start_qualification_window(self, window_start_at, *, build_ref=None,
                                   operator_note=None, supersede=False):
        """Persist a qualification boundary. Returns the stored row.

        Raises ValueError when the boundary is unusable, or when changing it
        would rewrite history:

        * an existing window with NO cycles inside it may be corrected freely —
          that is setup, not revision;
        * once ONE cycle exists at or after it, the boundary is locked and only
          an explicit ``supersede=True`` may start a new window, which records
          the previous boundary rather than overwriting it.
        """
        normalized = self.normalize_window_start(window_start_at)
        if not normalized:
            raise ValueError(
                f"unusable qualification window boundary: {window_start_at!r} "
                "(must be a parseable, non-future ISO timestamp)")

        active = self.get_active_qualification_window()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if active:
            if active["window_start_at"] == normalized:
                return active
            accumulated = self.count_cycles_in_window(active["window_start_at"])
            if accumulated and not supersede:
                raise ValueError(
                    f"qualification window is LOCKED: {accumulated} cycle(s) have "
                    f"accumulated since {active['window_start_at']}. Moving the "
                    "boundary now would rewrite which evidence the gate reviewed. "
                    "Start a new window explicitly (supersede=True) if that is "
                    "the intent.")
            if accumulated:
                with self.transaction() as conn:
                    if not conn:
                        raise RuntimeError("Database unavailable")
                    conn.execute(
                        "UPDATE hdencode_qualification_window SET superseded_at=? "
                        "WHERE id=?", (now, active["id"]))
            else:
                # No evidence yet — correct the boundary in place.
                with self.transaction() as conn:
                    if not conn:
                        raise RuntimeError("Database unavailable")
                    conn.execute(
                        "DELETE FROM hdencode_qualification_window WHERE id=?",
                        (active["id"],))

        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            conn.execute(
                "INSERT INTO hdencode_qualification_window "
                "(window_start_at, created_at, build_ref, operator_note, "
                " previous_window_start_at) VALUES (?,?,?,?,?)",
                (normalized, now, build_ref, operator_note,
                 active["window_start_at"] if active else None))
        return self.get_active_qualification_window()

    @staticmethod
    def normalize_window_start(value):
        """Parse and normalise a window boundary, or return None if unusable.

        The boundary is compared as TEXT against `completed_at`, which is stored
        as ISO-8601 with a ``+00:00`` offset. A caller-supplied string in any
        other shape ('...Z', a bare date, a local offset) would compare
        lexicographically against a different format and silently select the
        wrong rows — so it is parsed and re-emitted in the stored form here.

        FAIL-CLOSED: anything unparseable, or in the future, returns None. None
        means "no window", which blocks — never "count everything".
        """
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        parsed = parsed.astimezone(datetime.timezone.utc)
        # A future boundary would exclude every cycle forever, so the window
        # could never accumulate evidence — indistinguishable from a stalled
        # collector until someone checked the timestamp.
        if parsed > datetime.datetime.now(datetime.timezone.utc):
            return None
        return parsed.isoformat()

        first=summary.get("first_completed_at"); last=summary.get("last_completed_at"); observed_days=0.0
        try:
            first_dt=datetime.datetime.fromisoformat(first); last_dt=datetime.datetime.fromisoformat(last)
            if first_dt.tzinfo is None: first_dt=first_dt.replace(tzinfo=datetime.timezone.utc)
            if last_dt.tzinfo is None: last_dt=last_dt.replace(tzinfo=datetime.timezone.utc)
            observed_days=max(0.0,(last_dt-first_dt).total_seconds()/86400.0)
        except (TypeError,ValueError): pass
        feed_rows=self._query_dicts(
            "SELECT feed_key,last_status,consecutive_failures,last_checked_at FROM hdencode_feed_state "
            "WHERE feed_key IN ('movies_all','tv_all')",default=[])
        by_key={row["feed_key"]:row for row in feed_rows}; now=datetime.datetime.now(datetime.timezone.utc)
        def fresh(row):
            try:
                value=datetime.datetime.fromisoformat(row.get("last_checked_at"))
                if value.tzinfo is None: value=value.replace(tzinfo=datetime.timezone.utc)
                return (now-value.astimezone(datetime.timezone.utc)).total_seconds() <= max(15,int(max_stale_minutes))*60
            except (TypeError,ValueError): return False
        feeds_healthy=all(key in by_key and by_key[key].get("last_status") in (200,304) and int(by_key[key].get("consecutive_failures") or 0)==0 and fresh(by_key[key]) for key in ("movies_all","tv_all"))
        reasons=[]
        if summary["successful_cycles"]<required_cycles: reasons.append("insufficient_comparison_cycles")
        if observed_days<required_days: reasons.append("insufficient_observation_days")
        # THE MISS RULE, changed 2026-08-07 on Jesse's decision. This used to be
        # `if summary["relevant_misses"] > 0`, which blocked on ANY listing-only
        # observation ever recorded. That could never pass: 99 of 100 such
        # releases were acquired anyway, median about an hour, all via the normal
        # feeds, so the gate treated ordinary polling latency as permanent
        # coverage loss and RSS would have stayed in shadow mode indefinitely.
        #
        # Now only a release that was NEVER acquired counts, with no deadline.
        # UNDETERMINED rows -- ones that left the listing without ever appearing
        # in the feed -- still block, because "cannot be proven either way" is not
        # evidence of health, and calling it health is the fail-open shape that
        # produced two HIGH findings in this same subsystem.
        resolution=self.get_hdencode_miss_resolution()
        if int(resolution.get("never_acquired") or 0)>0:
            reasons.append("unacquired_misses_detected")
        if int(resolution.get("undetermined") or 0)>0:
            reasons.append("miss_resolution_undetermined")
        # PENDING BLOCKS, reversed 2026-08-07 on peer review. I had excluded
        # not_yet_assessable so the gate could pass. The review showed why that is
        # unsafe rather than merely optimistic: the shadow comparison is recorded
        # only while discovery_mode == "rss_shadow", so promoting to rss_primary
        # stops producing the very observations a pending row needs. The gate
        # would open on evidence its own promoted mode destroys.
        if int(resolution.get("not_yet_assessable") or 0)>0:
            reasons.append("miss_resolution_pending")
        # Unreadable evidence is not the same as clean evidence. Skipping a
        # malformed cycle can remove the only observation after a miss, so it is
        # reported rather than absorbed.
        if resolution.get("evidence_problems"):
            reasons.append("miss_resolution_evidence_unreadable")
        # UNATTRIBUTED IN-SCOPE CANDIDATES BLOCK. Round 6: a listing-only release
        # whose detail scrape failed is not booked as a miss (it has no media type,
        # so it cannot be attributed to a feed) -- and it was therefore vanishing
        # from readiness entirely. A false-health under-count. It must block the
        # claim that no unacquired misses exist, without invalidating the cycle's
        # membership evidence for resolving OTHER misses.
        # Counted STRUCTURALLY by the loader, which already parses details_json.
        # My first attempt here used `details_json LIKE '%detail_failed%'` -- string
        # matching against JSON, which is the exact anti-pattern the RSS
        # round-6 work removed from this same file. A schema change or a key
        # appearing inside a URL would break it silently.
        if int(resolution.get("unattributed_candidates") or 0) > 0:
            reasons.append("unattributed_listing_candidates")
        # An integrity failure is not "zero misses". Malformed provenance, a
        # count that disagrees with the rows on disk, a nonzero count with no
        # rows, or a miss row filed against supplied-empty provenance all mean
        # the evidence contradicts itself. Before 2026-08-06 each of these
        # silently contributed zero, which DEFLATED the gate -- the opposite of
        # the protection claimed for it. They must block instead.
        if summary.get("miss_evidence_integrity"):
            reasons.append("miss_evidence_integrity_failed")
        if summary["request_reduction_pct"]<=0: reasons.append("request_reduction_not_proven")
        if summary["recovery_cycles"]<1: reasons.append("restart_or_catchup_recovery_not_proven")
        if not feeds_healthy: reasons.append("normal_feeds_unhealthy_or_stale")
        return {"ready":not reasons,"window_start_at":summary.get("window_start_at"),"required_cycles":required_cycles,"successful_cycles":summary["successful_cycles"],"required_days":required_days,"observed_days":observed_days,"normal_feeds_healthy":feeds_healthy,"relevant_misses":summary["relevant_misses"],"misses_acquired":int(resolution.get("acquired") or 0),"misses_never_acquired":int(resolution.get("never_acquired") or 0),"misses_undetermined":int(resolution.get("undetermined") or 0),"misses_not_yet_assessable":int(resolution.get("not_yet_assessable") or 0),"miss_evidence_problems":list(resolution.get("evidence_problems") or []),"worst_acquisition_lag_hours":resolution.get("worst_acquisition_lag_hours"),"request_reduction_pct":summary["request_reduction_pct"],"recovery_cycles":summary["recovery_cycles"],"first_completed_at":first,"last_completed_at":last,"reasons":reasons}

    # ── HDEncode candidate actions ─────────────────────────────────────

    def get_hdencode_action(self, action_uuid):
        row = self._query(
            "SELECT * FROM hdencode_actions WHERE action_uuid = ?",
            (action_uuid,), one=True, default=None,
        )
        return dict(row) if row is not None else None

    def list_hdencode_actions(self, *, state=None, limit=250):
        params = []
        where = ""
        if state:
            where = " WHERE a.state = ?"
            params.append(state)
        params.append(max(1, min(int(limit), 1000)))
        return self._query_dicts(
            "SELECT a.*, c.title, c.clean_title, c.resolution, "
            "c.dv_evidence, c.hdr_evidence, c.discovery_source "
            "FROM hdencode_actions a JOIN hdencode_candidates c "
            "ON c.canonical_url = a.canonical_url"
            + where
            + " ORDER BY a.updated_at DESC LIMIT ?",
            tuple(params), default=[],
        )

    def create_hdencode_action(
        self, *, action_uuid, idempotency_key, canonical_url, action_kind,
        requested_by, service_type, priority, package_name, destination,
        lifespan_generation, authorized_evidence,
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            existing = conn.execute(
                "SELECT * FROM hdencode_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result["created"] = False
                result["idempotent"] = True
                return result
            active = conn.execute(
                "SELECT * FROM hdencode_actions WHERE canonical_url = ? "
                "AND state IN ('queued','retrieving_links','links_ready','submitting')",
                (canonical_url,),
            ).fetchone()
            if active is not None:
                result = dict(active)
                result["created"] = False
                result["conflict"] = True
                return result
            conn.execute(
                """
                INSERT INTO hdencode_actions (
                    action_uuid, idempotency_key, canonical_url, action_kind,
                    requested_by, service_type, priority, state, package_name,
                    destination, queued_at, updated_at,
                    authorized_evidence_json, lifespan_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_uuid, idempotency_key, canonical_url, action_kind,
                    requested_by, service_type, int(priority), package_name,
                    destination, now, now,
                    json.dumps(authorized_evidence or {}, sort_keys=True),
                    lifespan_generation,
                ),
            )
            conn.execute(
                "UPDATE hdencode_candidates SET action_state='queued', "
                "updated_at=? WHERE canonical_url=?",
                (now, canonical_url),
            )
            result = dict(conn.execute(
                "SELECT * FROM hdencode_actions WHERE action_uuid = ?",
                (action_uuid,),
            ).fetchone())
            result["created"] = True
            return result

    def claim_hdencode_action(self, action_uuid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            changed = conn.execute(
                """UPDATE hdencode_actions
                   SET state='retrieving_links', attempts=attempts+1,
                       claimed_at=?, cancel_requested=0, updated_at=?,
                       last_error_code=NULL, correlation_id=NULL
                   WHERE action_uuid=? AND state='queued'""",
                (now, now, action_uuid),
            ).rowcount
            if changed != 1:
                return None
            row = conn.execute(
                """SELECT a.*, c.title, c.clean_title, c.resolution,
                          c.size_text, c.season, c.title_year,
                          c.description_year, c.dv_evidence, c.hdr_formats
                   FROM hdencode_actions a
                   JOIN hdencode_candidates c
                     ON c.canonical_url=a.canonical_url
                   WHERE a.action_uuid=?""",
                (action_uuid,),
            ).fetchone()
            conn.execute(
                "UPDATE hdencode_candidates SET action_state='retrieving_links', "
                "updated_at=? WHERE canonical_url=?",
                (now, row["canonical_url"]),
            )
            return dict(row)

    def hdencode_action_cancel_requested(self, action_uuid):
        row = self._query(
            "SELECT cancel_requested, state FROM hdencode_actions "
            "WHERE action_uuid=?",
            (action_uuid,), one=True, default=None,
        )
        return bool(row and (row[0] or row[1] == "cancelled"))

    def mark_hdencode_action_links_ready(self, action_uuid, *, links):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return False
            changed = conn.execute(
                """UPDATE hdencode_actions
                   SET state='links_ready', links_json=?, link_count=?,
                       links_ready_at=?, claimed_at=NULL, updated_at=?
                   WHERE action_uuid=? AND state='retrieving_links'""",
                (json.dumps(list(links)), len(list(links)), now, now, action_uuid),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE hdencode_candidates SET action_state='links_ready', "
                    "updated_at=? WHERE canonical_url=?",
                    (now, row["canonical_url"]),
                )
            return changed == 1

    def mark_hdencode_action_submitting(self, action_uuid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return False
            changed = conn.execute(
                """UPDATE hdencode_actions
                   SET state='submitting', updated_at=?
                   WHERE action_uuid=? AND state='links_ready'
                     AND cancel_requested=0""",
                (now, action_uuid),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE hdencode_candidates SET action_state='submitting', "
                    "updated_at=? WHERE canonical_url=?",
                    (now, row["canonical_url"]),
                )
            return changed == 1

    def complete_hdencode_action_submitted(self, action_uuid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return False
            changed = conn.execute(
                """UPDATE hdencode_actions
                   SET state='submitted', submitted_at=?, completed_at=?,
                       claimed_at=NULL, updated_at=?
                   WHERE action_uuid=? AND state='submitting'""",
                (now, now, now, action_uuid),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE hdencode_candidates SET action_state='submitted', "
                    "updated_at=? WHERE canonical_url=?",
                    (now, row["canonical_url"]),
                )
            return changed == 1

    def fail_hdencode_action(self, action_uuid, *, error_code, correlation_id=None):
        return self._finish_hdencode_action(
            action_uuid, state="failed", error_code=error_code,
            correlation_id=correlation_id,
        )

    def mark_hdencode_action_needs_review(
        self, action_uuid, *, error_code, correlation_id=None
    ):
        return self._finish_hdencode_action(
            action_uuid, state="needs_review", error_code=error_code,
            correlation_id=correlation_id,
        )

    def _finish_hdencode_action(
        self, action_uuid, *, state, error_code=None, correlation_id=None
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """UPDATE hdencode_actions
                   SET state=?, claimed_at=NULL, updated_at=?,
                       last_error_code=?, correlation_id=?
                   WHERE action_uuid=?""",
                (state, now, error_code, correlation_id, action_uuid),
            )
            conn.execute(
                "UPDATE hdencode_candidates SET action_state=?, updated_at=? "
                "WHERE canonical_url=?",
                (state, now, row["canonical_url"]),
            )
            return True

    def request_cancel_hdencode_action(self, action_uuid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url, state FROM hdencode_actions "
                "WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return None
            state = row["state"]
            if state in {"submitted", "needs_review"}:
                return dict(conn.execute(
                    "SELECT * FROM hdencode_actions WHERE action_uuid=?",
                    (action_uuid,),
                ).fetchone())
            immediate = state in {"queued", "links_ready", "failed"}
            new_state = "cancelled" if immediate else state
            conn.execute(
                """UPDATE hdencode_actions
                   SET cancel_requested=1, state=?, cancelled_at=CASE
                         WHEN ? THEN ? ELSE cancelled_at END,
                       updated_at=? WHERE action_uuid=?""",
                (new_state, 1 if immediate else 0, now, now, action_uuid),
            )
            if immediate:
                conn.execute(
                    "UPDATE hdencode_candidates SET action_state='cancelled', "
                    "updated_at=? WHERE canonical_url=?",
                    (now, row["canonical_url"]),
                )
            return dict(conn.execute(
                "SELECT * FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone())

    def cancel_hdencode_action(self, action_uuid, *, reason):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """UPDATE hdencode_actions
                   SET state='cancelled', cancel_requested=1,
                       cancelled_at=?, claimed_at=NULL, updated_at=?,
                       last_error_code=? WHERE action_uuid=?""",
                (now, now, reason, action_uuid),
            )
            conn.execute(
                "UPDATE hdencode_candidates SET action_state='cancelled', "
                "updated_at=? WHERE canonical_url=?",
                (now, row["canonical_url"]),
            )
            return True

    def retry_hdencode_action(self, action_uuid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            row = conn.execute(
                "SELECT canonical_url, state FROM hdencode_actions "
                "WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] not in {"failed", "cancelled"}:
                return dict(conn.execute(
                    "SELECT * FROM hdencode_actions WHERE action_uuid=?",
                    (action_uuid,),
                ).fetchone())
            conn.execute(
                """UPDATE hdencode_actions
                   SET state='queued', cancel_requested=0,
                       queued_at=?, claimed_at=NULL, links_json='[]',
                       link_count=0, links_ready_at=NULL, submitted_at=NULL,
                       completed_at=NULL, cancelled_at=NULL, updated_at=?,
                       last_error_code=NULL, correlation_id=NULL
                   WHERE action_uuid=?""",
                (now, now, action_uuid),
            )
            conn.execute(
                "UPDATE hdencode_candidates SET action_state='queued', "
                "updated_at=? WHERE canonical_url=?",
                (now, row["canonical_url"]),
            )
            return dict(conn.execute(
                "SELECT * FROM hdencode_actions WHERE action_uuid=?",
                (action_uuid,),
            ).fetchone())

    def recover_hdencode_actions(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.transaction() as conn:
            if not conn:
                return {"requeued": 0, "needs_review": 0}
            requeued = conn.execute(
                """UPDATE hdencode_actions
                   SET state='queued', claimed_at=NULL, updated_at=?,
                       last_error_code='recovered_after_restart'
                   WHERE state='retrieving_links'""",
                (now,),
            ).rowcount
            needs_review = conn.execute(
                """UPDATE hdencode_actions
                   SET state='needs_review', claimed_at=NULL, updated_at=?,
                       last_error_code='submission_interrupted'
                   WHERE state='submitting'""",
                (now,),
            ).rowcount
            conn.execute(
                """UPDATE hdencode_candidates
                   SET action_state=(
                       SELECT a.state FROM hdencode_actions a
                       WHERE a.canonical_url=hdencode_candidates.canonical_url
                       ORDER BY a.updated_at DESC LIMIT 1
                   ), updated_at=?
                   WHERE canonical_url IN (
                       SELECT canonical_url FROM hdencode_actions
                       WHERE state IN ('queued','needs_review')
                   )""",
                (now,),
            )
            return {"requeued": requeued, "needs_review": needs_review}

    # ── Plex cache ───────────────────────────────────────────────────

    def clear_plex_cache(self):
        """Delete all entries from the Plex cache table."""
        return self._mutate("DELETE FROM plex_cache", label="clear_cache")

    @staticmethod
    def _plex_cache_key(item, is_tv):
        """The cache primary key for a Plex item. Insert and full_replace-prune
        MUST agree on this or the prune deletes freshly-inserted rows (the TV
        "all shows Missing" bug, fixed 2026-07-10). Kept as one helper so the
        two call sites in save_plex_cache can never drift apart again.

        Honors a pre-set item['key'] (e.g. movies' per-part key from
        plex_service.py) if truthy; otherwise falls back to rating_key alone
        for TV, or rating_key + "_" + media_id for movies.
        """
        k = item.get('key')
        if k:
            return k
        return (f"{item.get('rating_key')}" if is_tv
                else f"{item.get('rating_key')}_{item.get('media_id')}")

    def save_plex_cache(self, items, mode, library_name=None, full_replace=False):
        """Upsert Plex library items into the cache for the given mode.

        Args:
            items: List of dicts with Plex media metadata.
            mode: "Movies" or "TV Shows" — stored as content_type.
            library_name: Optional library name to tag items with.
            full_replace: If True, prune stale rows not in the fresh set.
                Defaults to False (safe upsert-only).
        """
        if not items:
            return

        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return
                cursor = conn.cursor()
                is_tv = (mode == "TV Shows")
                timestamp = time.time()

                for item in items:
                    item = dict(item)  # Shallow copy to avoid mutating caller's dict
                    item['key'] = self._plex_cache_key(item, is_tv)

                    cursor.execute('''
                        INSERT OR REPLACE INTO plex_cache (
                            key, title, original_title, year, res, size, imdb_id,
                            rating_key, media_id, is_tv, season, episode_count,
                            content_type, dovi, hdr, last_updated, library_name,
                            file_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['key'],
                        item.get('clean_title'),
                        item.get('original_title'),
                        item.get('year'),
                        item.get('res'),
                        item.get('size'),
                        item.get('imdb_id'),
                        item.get('rating_key'),
                        item.get('media_id'),
                        1 if is_tv else 0,
                        item.get('season', 0),
                        item.get('episode_count', 0),
                        mode,
                        1 if item.get('dovi') else 0,
                        1 if item.get('hdr') else 0,
                        timestamp,
                        item.get('library_name') or library_name,
                        item.get('file'),
                    ))

                # Remove stale rows when doing a full library refresh.
                # The INSERT OR REPLACE above already inserted fresh data;
                # now delete any old rows for this content_type that weren't
                # part of the fresh set (they have stale keys).
                if full_replace:
                    # Built with the SAME _plex_cache_key() helper the insert
                    # loop used above, so this "keep" set is structurally
                    # guaranteed to match the keys actually stored -- see the
                    # helper's docstring for the bug this prevents.
                    fresh_db_keys = {self._plex_cache_key(item, is_tv) for item in items}
                    # Delete in batches to avoid SQLite placeholder limits
                    all_existing = cursor.execute(
                        "SELECT key FROM plex_cache WHERE content_type = ?", (mode,)
                    ).fetchall()
                    stale_keys = [row[0] for row in all_existing if row[0] not in fresh_db_keys]
                    for i in range(0, len(stale_keys), 500):
                        batch = stale_keys[i:i+500]
                        placeholders = ','.join('?' for _ in batch)
                        cursor.execute(
                            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
                            batch,
                        )
                    deleted = cursor.rowcount
                    if deleted:
                        logger.info("Pruned %d stale rows from plex_cache (%s)", deleted, mode)

                conn.commit()
                logger.info("Saved %d items to DB cache (%s)", len(items), mode)
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception as rb_err:
                logger.debug("Rollback failed: %s", rb_err)
            logger.error("DB Error (save_cache): %s", e)

    #: ONE projection, two error contracts. Shared so the strict reader's claim
    #: to return "the same rows" cannot quietly stop being true: `media_id`
    #: became load-bearing for the version badges, and a column added to one
    #: SELECT but not the other is the kind of schema/consumer drift that
    #: could RECREATE H1's failure class. It is not what caused H1 -- that was
    #: count_versions() counting rows instead of distinct media_id, with the
    #: column already present in both readers.
    _PLEX_CACHE_MOVIES_SQL = (
        "SELECT key, title, original_title, year, res, size, imdb_id, "
        "rating_key, media_id, is_tv, dovi, hdr, library_name, file_path "
        "FROM plex_cache WHERE content_type = 'Movies'"
    )

    def list_plex_cache_movies(self):
        """Return every plex_cache row for content_type='Movies' (dicts) — the
        candidate pool for find_library_duplicate().

        FAIL-SOFT: a read error becomes []. Correct for a best-effort/display
        caller; see `list_plex_cache_movies_strict` for callers that need to
        tell an empty table from a failed read."""
        return self._query_dicts(self._PLEX_CACHE_MOVIES_SQL, default=[])

    def list_plex_cache_movies_strict(self):
        """Same rows as ``list_plex_cache_movies``, but RAISES on a read
        failure instead of returning ``[]``.

        WHY A SECOND METHOD. `_query_dicts(default=[])` converts a database
        error into an empty list, which is the right call for a display query
        and the wrong one for evidence. The version-badge sync derives its
        counts from these rows: an empty result makes every live movie
        "unknown", the reconciler correctly touches nothing, no counter records
        a failure, and the pass reports COMPLETE -- so the scheduler marks that
        cache generation reconciled and never retries it. The badges are then
        stale until an unrelated refresh (peer review 2026-08-19, M2/B --
        M2/A was the separate library-returns-None path).

        An empty table is still a valid empty answer here; only a failed READ
        raises. The caller must not try to tell those apart by inspecting the
        result, which is exactly the inference this method exists to remove.
        """
        with self._lock:
            conn = self.get_connection()
            if not conn:
                raise RuntimeError("plex_cache read failed: no database connection")
            cur = conn.execute(self._PLEX_CACHE_MOVIES_SQL)
            return [dict(r) for r in cur.fetchall()]

    def load_plex_cache(self, mode):
        """Load cached Plex items for the given content type.

        Args:
            mode: "Movies" or "TV Shows".

        Returns:
            List of dicts with boolean fields properly converted.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return []
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM plex_cache WHERE content_type = ?', (mode,))
                rows = cursor.fetchall()

            items = []
            for row in rows:
                item = dict(row)
                # SQLite stores booleans as 0/1 — convert back
                item['dovi'] = bool(item['dovi'])
                item['hdr'] = bool(item['hdr'])
                item['is_tv'] = bool(item['is_tv'])
                # Map DB column 'title' to 'clean_title' for matching engine compatibility
                if 'title' in item and item['title']:
                    item['clean_title'] = item['title']
                items.append(item)
            return items
        except Exception as e:
            logger.error("DB Error (load_cache): %s", e)
            return []

    def plex_cache_counts(self) -> dict:
        """Return unique item counts from the Plex cache.

        Movies are deduplicated across libraries (e.g. 4K + 1080p) using
        IMDb ID when available, falling back to title+year.

        Returns:
            dict with 'movies' and 'tv_seasons' integer counts.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return {"movies": 0, "tv_seasons": 0}
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT "
                    "  (SELECT COUNT(DISTINCT COALESCE(NULLIF(imdb_id, ''), title || '|' || COALESCE(year, 0)))"
                    "   FROM plex_cache WHERE content_type = 'Movies') AS movies,"
                    "  (SELECT COUNT(DISTINCT COALESCE(NULLIF(imdb_id, ''), title || '|' || COALESCE(year, 0))"
                    "          || '|S' || COALESCE(season, 0))"
                    "   FROM plex_cache WHERE content_type = 'TV Shows') AS tv_seasons"
                )
                row = cursor.fetchone()
            return {
                "movies": row[0] if row else 0,
                "tv_seasons": row[1] if row else 0,
            }
        except Exception as e:
            logger.error("DB Error (plex_cache_counts): %s", e)
            return {"movies": 0, "tv_seasons": 0}

    def get_plex_cache_max_timestamp(self) -> dict:
        """Return max last_updated timestamp per content_type without loading all rows.

        Returns:
            dict mapping content_type → max last_updated float, e.g.
            {"Movies": 1740000000.0, "TV Shows": 1740001234.5}.
            Empty dict if cache is empty or on error.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return {}
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT content_type, MAX(last_updated) FROM plex_cache"
                    " GROUP BY content_type"
                )
                return {row[0]: row[1] for row in cursor.fetchall() if row[1] is not None}
        except Exception as e:
            logger.error("DB Error (get_plex_cache_max_timestamp): %s", e)
            return {}

    def plex_cache_counts_per_library(self) -> list:
        """Return item counts broken down by library name and content type.

        Returns:
            List of dicts: [{library_name, content_type, count}] sorted by
            content_type then library_name. Items with no library_name are
            grouped under the content_type value (e.g., "Movies").
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return []
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(library_name, content_type) AS lib, "
                    "content_type, COUNT(*) AS cnt "
                    "FROM plex_cache "
                    "GROUP BY lib, content_type "
                    "ORDER BY content_type, lib"
                )
                return [
                    {"library_name": row[0], "content_type": row[1], "count": row[2]}
                    for row in cursor.fetchall()
                ]
        except Exception as e:
            logger.error("DB Error (plex_cache_counts_per_library): %s", e)
            return []

    # ── Download history ─────────────────────────────────────────────

    def clear_history(self):
        """Delete all download history records (and their pipeline verdicts)."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.execute("DELETE FROM pipeline_verdicts")
                conn.execute("DELETE FROM downloads")
                conn.commit()
            return True
        except Exception as e:
            logger.error("DB Error (clear_history): %s", e)
            return False

    def add_to_history(self, url, title, normalized_title=None, season=None,
                       resolution=None, size=None, status="completed",
                       hdr=None, dovi=False, year=None, package_name=None,
                       service_type=None, media_kind=None):
        """Record a downloaded URL with optional metadata for title-based matching.

        Uses ON CONFLICT to preserve the original date_added when re-downloading.
        ``package_name``/``service_type`` are COALESCEd so a later status-only
        update never nulls out an already-known value. ``last_grabbed_at`` is
        bumped unconditionally on every call — every call that reaches this
        method (success, clipboard, browser, failed-send) is a genuine new
        attempt, and this is what the pipeline reconcile's matching window
        keys off for a regrab.
        """
        return self._mutate('''
            INSERT INTO downloads (url, title, normalized_title, season, resolution, size, status, hdr, dovi, year, package_name, service_type, media_kind, last_grabbed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                season = excluded.season,
                resolution = excluded.resolution,
                size = excluded.size,
                status = excluded.status,
                hdr = excluded.hdr,
                dovi = excluded.dovi,
                year = COALESCE(excluded.year, downloads.year),
                package_name = COALESCE(excluded.package_name, downloads.package_name),
                service_type = COALESCE(excluded.service_type, downloads.service_type),
                -- COALESCEd like package_name/service_type, and for the same
                -- reason: a later status-only update passes media_kind=None and
                -- must not erase a kind an earlier grab recorded. NULL means
                -- "not recorded", so overwriting a real value with it would
                -- destroy evidence rather than update it.
                media_kind = COALESCE(excluded.media_kind, downloads.media_kind),
                last_grabbed_at = CURRENT_TIMESTAMP
        ''', (url, title, normalized_title, season, resolution, size, status,
              hdr or None, 1 if dovi else 0, year, package_name, service_type,
              media_kind),
            label="add_history")

    # ── Pipeline tracker verdicts ────────────────────────────────────

    def get_pipeline_verdicts(self, category=None, include_dismissed=False):
        """Return pipeline verdicts, joined with their downloads
        display fields, most-recently-checked first."""
        clauses = []
        params = []
        if not include_dismissed:
            clauses.append("v.dismissed = 0")
        if category:
            clauses.append("v.category = ?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._query_dicts(f'''
            SELECT v.url, v.category, v.detail, v.package_uuid, v.excluded_uuid,
                   v.plex_rating_key, v.checked_at, v.dismissed,
                   d.title, d.year, d.season, d.resolution, d.package_name,
                   d.last_grabbed_at AS grabbed_at,
                   CASE
                     WHEN v.category = 'pending_rename'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('pending', 'matched', 'applying')
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     WHEN v.category = 'rename_failed'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('failed', 'needs_review', 'reverted')
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     WHEN v.category IN ('awaiting_plex_refresh', 'verified', 'not_in_plex')
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status = 'applied'
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     ELSE NULL
                   END AS poster_path,
                   CASE
                     WHEN v.category = 'pending_rename'
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('pending', 'matched', 'applying')
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     WHEN v.category = 'rename_failed'
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('failed', 'needs_review', 'reverted')
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     WHEN v.category IN ('awaiting_plex_refresh', 'verified', 'not_in_plex')
                     THEN (SELECT COALESCE(r.processed_at, r.detected_at) FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status = 'applied'
                           ORDER BY (r.poster_path IS NOT NULL) DESC, r.id DESC LIMIT 1)
                     ELSE NULL
                   END AS renamed_at
            FROM pipeline_verdicts v
            JOIN downloads d ON d.url = v.url
            {where}
            ORDER BY v.checked_at DESC
        ''', tuple(params))

    def upsert_pipeline_verdict(self, url, category, detail=None, package_uuid=None,
                                plex_rating_key=None, dismissed=False):
        """Insert/update a verdict for url. checked_at is always refreshed
        explicitly — the column DEFAULT only fires on INSERT, never UPDATE."""
        return self._mutate('''
            INSERT INTO pipeline_verdicts (url, category, detail, package_uuid, plex_rating_key, dismissed, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(url) DO UPDATE SET
                category = excluded.category,
                detail = excluded.detail,
                package_uuid = excluded.package_uuid,
                plex_rating_key = excluded.plex_rating_key,
                dismissed = excluded.dismissed,
                checked_at = CURRENT_TIMESTAMP
        ''', (url, category, detail, package_uuid, plex_rating_key, 1 if dismissed else 0),
            label="upsert_pipeline_verdict")

    def dismiss_pipeline_verdict(self, url):
        return self._mutate(
            "UPDATE pipeline_verdicts SET dismissed = 1, checked_at = CURRENT_TIMESTAMP WHERE url = ?",
            (url,), label="dismiss_pipeline_verdict")

    def clear_pipeline_verdict(self, url):
        """Called by regrab only (grab-alternative does NOT call this — see
        below): move any confirmed package_uuid into excluded_uuid
        (accumulating — comma-joined, never overwritten, so a
        second-in-a-row regrab can't un-exclude the first's stale package),
        clear package_uuid, and reset category to NULL ('pending
        re-evaluation' — always reconcile-eligible).

        This is correct for regrab because it's re-grabbing the *same* url:
        the existing verdict's evidence should be re-evaluated against the
        new package once it lands.

        grab-alternative is different: it grabs a *different* url, and the
        original url's verdict needs to be resolved separately. Clearing it
        (this method) would be wrong there — resetting to NULL leaves it
        'pending re-evaluation', so the reconcile pass could re-examine the
        original's own (unrelated) download_results/rename_jobs evidence and
        miscategorize it, e.g. as never_started if the original's package
        never got a download_results row past its failure point. Since the
        user has explicitly moved on by grabbing a different release, the
        original grab is simply done, not pending — so grab-alternative
        instead calls dismiss_pipeline_verdict(original_url) on the original
        url once the alternative grab is backgrounded (see grab_alternative
        in backend/api/routes/pipeline.py)."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                cur = conn.cursor()
                cur.execute("SELECT package_uuid, excluded_uuid FROM pipeline_verdicts WHERE url = ?", (url,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO pipeline_verdicts (url, category, checked_at) "
                        "VALUES (?, NULL, CURRENT_TIMESTAMP)", (url,))
                    conn.commit()
                    return True
                pkg_uuid, excluded = row[0], row[1]
                if pkg_uuid is None:
                    new_excluded = excluded
                elif excluded is None:
                    new_excluded = pkg_uuid
                else:
                    new_excluded = f"{excluded},{pkg_uuid}"
                cur.execute(
                    "UPDATE pipeline_verdicts SET excluded_uuid = ?, package_uuid = NULL, "
                    "category = NULL, dismissed = 0, checked_at = CURRENT_TIMESTAMP WHERE url = ?",
                    (new_excluded, url))
                conn.commit()
                return True
        except Exception as e:
            logger.error("DB Error (clear_pipeline_verdict): %s", e)
            return False

    def get_downloads_needing_reconcile(self, limit=500):
        """Grabs eligible for the pipeline reconcile pass: have a package_name,
        are past the 30-minute too-soon-to-judge window, and are not yet
        dismissed/verified (terminal). Uses IS NOT (not !=) so a just-cleared
        verdict — category IS NULL — is correctly re-included: SQL NULL != 'x'
        is NULL/falsy, which would otherwise permanently freeze a regrab.
        Ordered oldest-checked-first for round-robin fairness under a large
        backlog (NULLs — never checked — sort first)."""
        return self._query_dicts('''
            SELECT d.url, d.title, d.year, d.season, d.resolution, d.size, d.hdr, d.dovi,
                   d.package_name, d.jd_confirmed_name, d.service_type, d.last_grabbed_at,
                   d.status,
                   v.category AS verdict_category, v.dismissed AS verdict_dismissed,
                   v.package_uuid, v.excluded_uuid
            FROM downloads d
            LEFT JOIN pipeline_verdicts v ON v.url = d.url
            WHERE d.package_name IS NOT NULL
              AND d.last_grabbed_at <= datetime('now', '-30 minutes')
              AND (v.url IS NULL OR (v.dismissed = 0 AND v.category IS NOT 'verified'))
            ORDER BY v.checked_at ASC
            LIMIT ?
        ''', (limit,))

    def capture_jd_confirmed_names(self, jd_names):
        """Empirical capture of JD's reported package names (pipeline matching).

        For each name JD reports, find downloads rows still awaiting capture
        (jd_confirmed_name IS NULL, grabbed within the last 7 days) whose
        computed package_name FOLDS equal to it; persist only on a UNIQUE
        match — an ambiguous fold (legacy season-less names) is skipped
        rather than guessed. Returns the number of rows captured."""
        from backend.download_service import fold_name
        if not jd_names:
            return 0
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    "SELECT url, package_name FROM downloads "
                    "WHERE jd_confirmed_name IS NULL AND package_name IS NOT NULL "
                    "AND last_grabbed_at >= datetime('now', '-7 days')")
                pending = [(r[0], r[1]) for r in cur.fetchall()]
                if not pending:
                    return 0
                captured = 0
                for jd_name in set(jd_names):
                    key = fold_name(jd_name)
                    hits = [url for url, pkg in pending if fold_name(pkg) == key]
                    if len(hits) != 1:
                        continue  # 0 = unrelated package; >1 = ambiguous, skip
                    cur.execute(
                        "UPDATE downloads SET jd_confirmed_name = ? "
                        "WHERE url = ? AND jd_confirmed_name IS NULL",
                        (jd_name, hits[0]))
                    captured += cur.rowcount
                    pending = [(u, p) for u, p in pending if u != hits[0]]
                conn.commit()
                return captured
        except Exception as e:
            logger.error("DB Error (capture_jd_confirmed_names): %s", e)
            return 0

    def get_downloaded_title_quality(self):
        """Per non-failed grab: (normalized_title, year, season, resolution, dovi).

        Powers send-time duplicate protection and the read-time overlay's
        title-keyed sibling matching — both need to know what quality of a
        title was already grabbed, independent of whether the grabbed URL is
        still in the background cache."""
        return self._query(
            "SELECT normalized_title, year, season, resolution, dovi FROM downloads "
            "WHERE COALESCE(status, 'completed') != 'failed' "
            "AND normalized_title IS NOT NULL AND normalized_title != ''",
            default=[])

    def get_downloaded_titles(self):
        """Get all downloaded items with their normalized titles and seasons."""
        return self._query('''
            SELECT normalized_title, season, resolution, size, url
            FROM downloads WHERE normalized_title IS NOT NULL
        ''', default=[])

    def is_in_history(self, url):
        """Check whether a URL exists in the download history."""
        return self._query('SELECT 1 FROM downloads WHERE url = ?', (url,),
                           one=True, default=None) is not None

    def is_downloaded(self, url):
        """True if this URL was already grabbed SUCCESSFULLY (non-failed) — used
        to skip re-sending a duplicate to JDownloader. A prior 'failed' row does
        not count, so a failed grab can still be retried."""
        return self._query(
            "SELECT 1 FROM downloads WHERE url = ? AND COALESCE(status, 'completed') != 'failed'",
            (url,), one=True, default=None) is not None

    def get_downloaded_urls(self):
        """Set of every URL grabbed successfully (non-failed) — the central,
        authoritative record of what's been downloaded. Used to overlay
        'downloaded' status onto results at read time so a grab is remembered
        across reloads / app + web without waiting for a re-scan. Mirrors the
        scanner's _load_download_history query."""
        rows = self._query(
            "SELECT url FROM downloads WHERE COALESCE(status, 'completed') != 'failed'",
            default=[])
        return {r[0] for r in rows if r and r[0]}

    def get_history_count(self):
        """Return the total number of downloaded URLs."""
        row = self._query('SELECT COUNT(*) FROM downloads', one=True, default=None)
        return row[0] if row else 0

    def get_download_history(self, limit=100):
        """Return recent download history as a list of dicts."""
        return self._query_dicts(
            "SELECT url, title, resolution, size, date_added AS downloaded_at, "
            "COALESCE(status, 'completed') AS status "
            "FROM downloads ORDER BY date_added DESC LIMIT ?",
            (limit,),
        )

    # ── Download results (live JDownloader outcome tracking) ─────────────

    def upsert_download_result(self, name, package_uuid=None, title=None, host=None,
                               bytes_total=0, bytes_loaded=0, downloaded=0,
                               extraction="na", state="queued", error=None,
                               provenance_url=None, provenance_observed=False):
        """Insert/update a JD package's download outcome; returns the row id (int)
        or None on failure. Identity is package_uuid when present, else the row is
        adopted-by-name (a legacy NULL-uuid row) or inserted. Runs the whole
        lookup-then-write under one lock hold to avoid poller-vs-remove races.

        ``provenance_url`` is written under ``provenance_observed``, which is the
        difference between an absence of observation and an observation of
        absence (peer review 2026-08-13):

            observed=True   write the value AS GIVEN, including NULL. The caller
                            looked, and either proved one release or proved there
                            is no honest unique answer. The second RETRACTS.
            observed=False  COALESCE. The caller could not look -- JDownloader's
                            link query failed, or the lookup threw -- so whatever
                            was proven earlier stands.

        Unconditional COALESCE was the earlier behaviour and it let a stale proof
        outlive its evidence: once a second release recorded the same host link,
        the resolver correctly said "ambiguous", and that None was read as
        "nothing to say" rather than "this is no longer authorised"."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return None
                cur = conn.cursor()
                row = None
                adopted_by_name = False
                if package_uuid is not None:
                    cur.execute("SELECT id FROM download_results WHERE package_uuid = ?",
                                (package_uuid,))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute("SELECT id FROM download_results "
                                    "WHERE package_uuid IS NULL AND name = ? "
                                    "ORDER BY updated_at DESC LIMIT 1", (name,))
                        row = cur.fetchone()
                        # ADOPTION: this row was matched by NAME, so the
                        # package it belongs to is about to CHANGE.
                        adopted_by_name = row is not None
                else:
                    cur.execute("SELECT id FROM download_results WHERE name = ? "
                                "ORDER BY (package_uuid IS NULL) DESC, updated_at DESC LIMIT 1",
                                (name,))
                    row = cur.fetchone()
                if row is not None:
                    rid = row[0]
                    cur.execute(
                        "UPDATE download_results SET "
                        "package_uuid = COALESCE(?, package_uuid), name = ?, title = ?, "
                        "host = ?, bytes_total = ?, bytes_loaded = ?, downloaded = ?, "
                        "extraction = ?, state = ?, error = ?, "
                        # Observed -> take the value as given (NULL retracts).
                        # Unobserved -> keep what is stored, MECHANICALLY.
                        #
                        # The unobserved branch ignores the passed value entirely
                        # rather than COALESCEing it (peer review follow-up 2).
                        # With COALESCE, a caller passing observed=False WITH a
                        # url would still overwrite the stored one -- so the
                        # docstring's promise ("the previous proof stands") held
                        # only because the production caller never emits that
                        # combination. An invariant that depends on callers
                        # behaving is not an invariant; this makes it structural.
                        # ...UNLESS this update also changes which package
                        # the row belongs to. A legacy NULL-uuid row adopted
                        # by NAME keeps its id, so "the previous proof stands"
                        # would hand the OLD package's proof to the NEW one --
                        # and identical names across releases are the exact
                        # collision this whole feature exists to remove. Proof
                        # does not transfer across a name-based ownership change
                        # without current evidence (peer review round 3).
                        #
                        # ...NOR when the current package has no uuid at all.
                        # That row was selected BY NAME too, so which package
                        # it refers to cannot be identified stably across
                        # polls. Kept as a SEPARATE flag from adoption because
                        # the reasons differ: adoption is "ownership changed
                        # based on a name", this is "ownership cannot be pinned
                        # at all". Enforcing it HERE rather than only in the
                        # annotator is the point -- REST reads this column
                        # directly and never reaches the annotator's recovery
                        # gate, so a caller-side rule left the two transports
                        # disagreeing exactly as before (peer review round 4).
                        "provenance_url = CASE WHEN ? = 1 THEN ? "
                        "                      WHEN ? = 1 THEN NULL "
                        "                      WHEN ? = 1 THEN NULL "
                        "                      ELSE provenance_url END, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (package_uuid, name, title, host, bytes_total, bytes_loaded,
                         downloaded, extraction, state, error,
                         1 if provenance_observed else 0, provenance_url,
                         1 if (adopted_by_name and not provenance_observed) else 0,
                         1 if (package_uuid is None and not provenance_observed) else 0,
                         rid))
                    conn.commit()
                    return rid
                cur.execute(
                    "INSERT INTO download_results (package_uuid, name, title, host, "
                    "bytes_total, bytes_loaded, downloaded, extraction, state, error, "
                    "provenance_url, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (package_uuid, name, title, host, bytes_total, bytes_loaded,
                     downloaded, extraction, state, error, provenance_url))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.error("DB Error (upsert_download_result): %s", e)
            return None

    def get_download_results(self, limit=200):
        """Return tracked download/extraction outcomes, most recent first."""
        return self._query_dicts(
            "SELECT id, package_uuid, name, title, host, bytes_total, bytes_loaded, "
            "downloaded, extraction, state, error, updated_at, provenance_url "
            "FROM download_results ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

    def record_submitted_links(self, url, links):
        """Remember the file-host links submitted to JDownloader for `url`.

        Called for BOTH send paths. The folder/.crawljob path is the one that
        matters most here: it has no API to read a package back from, so any
        scheme that depended on asking JDownloader what it created would leave
        that path with no provenance at all.

        Idempotent (INSERT OR IGNORE on the natural key), so a regrab of the
        same release re-affirms the same rows rather than duplicating them, and
        a partial failure can be retried. Never raises: failing to record
        provenance must not fail the grab itself -- the cost is a link that
        stays unresolved, which is the safe direction.
        """
        rows = [(str(url), str(link)) for link in (links or []) if link]
        if not url or not rows:
            return 0
        try:
            with self.transaction() as conn:
                if conn is None:
                    return 0
                conn.executemany(
                    "INSERT OR IGNORE INTO download_package_links (url, link) "
                    "VALUES (?, ?)", rows)
            return len(rows)
        except Exception:
            logger.exception("failed to record submitted links for %s", url)
            return 0

    def resolve_release_by_links(self, link_urls):
        """The release these live JDownloader links provably belong to, or None.

        Provenance, not inference: a link resolves only because ScanHound
        recorded submitting it. A package JDownloader shows that ScanHound never
        sent contributes no rows and therefore resolves to nothing, which is the
        whole point of Finding 1.

        Returns None when the links map to more than one release, too. That is a
        real possibility -- a hand-built package can mix links from two releases,
        and a regrab at a different URL can reuse a host link -- and there is no
        honest answer to "which release is this?" in that case.
        """
        wanted = [str(u) for u in dict.fromkeys(link_urls or []) if u]
        if not wanted:
            return None
        found = set()
        for start in range(0, len(wanted), 300):
            chunk = wanted[start:start + 300]
            rows = self._query_dicts(
                "SELECT DISTINCT url FROM download_package_links WHERE link IN (%s)"
                % ",".join("?" * len(chunk)),
                tuple(chunk), default=[]) or []
            found.update(r["url"] for r in rows)
            if len(found) > 1:
                return None   # ambiguous; no honest answer
        return next(iter(found)) if len(found) == 1 else None

    def mark_scan_category_conflict(self, urls):
        """Record that two listings disagreed about a release's media type.

        Peer review round 11 (M1b). The crawl marks in-flight posts directly,
        but a release it SKIPS as already cached is never rewritten -- so a
        conflict observed about the deployed corpus was discovered and then
        discarded. This writes it to the cached row itself.

        Returns the number of rows marked.
        """
        marked = 0
        for url in {str(u) for u in (urls or ()) if u}:
            row = self._query(
                "SELECT data FROM background_scan_cache WHERE url = ?",
                (url,), one=True, default=None)
            if not row:
                continue
            try:
                payload = json.loads(dict(row).get("data") or "{}")
            except (TypeError, ValueError):
                logger.warning("cannot mark conflict on %s: undecodable data", url)
                continue
            if payload.get("category_conflict"):
                continue
            payload["category_conflict"] = True
            with self.transaction() as conn:
                if not conn:
                    return marked
                conn.execute(
                    "UPDATE background_scan_cache SET data = ? WHERE url = ?",
                    (json.dumps(payload, default=str), url))
                # An in-place blob mutation changes neither COUNT(*) nor
                # MAX(last_seen_at), so without this bump
                # get_background_cache_version() is unchanged and
                # /results/cached serves its memoised PRE-MARK parse
                # indefinitely -- the same defect rematch_cache (:7499) and
                # the reparse pass (:2384) already carry this bump for.
                #
                # Found by the V6/V7 bridge's end-to-end test: with the
                # read-side normalisation in place and this bump missing, the
                # endpoint still answered 'movie' for a row the matcher had
                # started calling 'ambiguous'. The fix reached the code and
                # not the consumer.
                self._bg_cache_rev += 1
            marked += 1
        if marked:
            logger.info("marked %d cached release(s) as classification-conflicted",
                        marked)
        return marked

    def attest_scan_categories(self, urls):
        """Record that a conflict-aware crawl observed these releases cleanly.

        Peer review round 11 (M1b). Absence of `category_conflict` used to mean
        the same thing as an explicit False, so every row written by the old
        first-source-wins crawler read as positively unconflicted -- including
        any release that genuinely appeared in two listings before conflict
        detection existed. The state Round 10 identified could survive the fix
        that was supposed to remove it.

        Three states now, not two:

            attestation absent   -> UNKNOWN (never checked by a crawl that could
                                    have seen a conflict)
            attested, no conflict-> the recorded category is usable
            conflict recorded    -> UNKNOWN

        Written ONLY where the key is absent, so this is a one-time backfill as
        each release is next observed, not a write on every crawl.

        Returns the number of rows newly attested.
        """
        attested = 0
        for url in {str(u) for u in (urls or ()) if u}:
            row = self._query(
                "SELECT data FROM background_scan_cache WHERE url = ?",
                (url,), one=True, default=None)
            if not row:
                continue
            try:
                payload = json.loads(dict(row).get("data") or "{}")
            except (TypeError, ValueError):
                continue
            if "category_attested" in payload or payload.get("category_conflict"):
                continue
            payload["category_attested"] = True
            with self.transaction() as conn:
                if not conn:
                    return attested
                conn.execute(
                    "UPDATE background_scan_cache SET data = ? WHERE url = ?",
                    (json.dumps(payload, default=str), url))
                # Same in-place-blob staleness as mark_scan_category_conflict
                # above: attestation is served to the API inside the blob, and
                # without the bump the parse cache keeps handing out the
                # pre-attestation copy. Fixed here rather than left as the one
                # remaining instance of a defect being fixed one line up.
                self._bg_cache_rev += 1
            attested += 1
        if attested:
            logger.info("attested %d cached release(s) as conflict-checked", attested)
        return attested

    def retract_download_media_kind(self, urls, *, reason):
        """Erase a recorded media kind that is no longer supported by evidence.

        Peer review round 11 (M1a). `verified_media_kind()` refuses to RECORD a
        kind once a conflict appears, but the destructive identity does not read
        the cache -- it reads the already-persisted `downloads.media_kind` via
        get_release_identity(). So a kind written before the conflict was
        discovered stayed authoritative, and Keep-best stayed available on it.

        This CANNOT go through add_to_history(media_kind=None). That path
        deliberately COALESCEs, because there None means "this write carries no
        media-kind observation, keep what you had". Round 11 introduced a second
        meaning -- "the evidence that justified the old value has been
        withdrawn" -- and one value cannot carry both. Hence a named operation
        that only ever erases.

        Returns the number of rows retracted.
        """
        targets = {str(u) for u in (urls or ()) if u}
        if not targets:
            return 0
        retracted = 0
        with self.transaction() as conn:
            if not conn:
                return 0
            for url in targets:
                cur = conn.execute(
                    "UPDATE downloads SET media_kind = NULL "
                    "WHERE url = ? AND media_kind IS NOT NULL", (url,))
                retracted += max(0, int(cur.rowcount or 0))
        if retracted:
            logger.warning(
                "retracted media_kind on %d download row(s): %s. Any semantic "
                "identity built on those rows is withdrawn.", retracted, reason)
        return retracted

    def get_scan_category(self, url):
        """The crawl category THIS SERVER recorded for a release URL.

        Peer review round 10, M1: the media kind was being taken from
        `DownloadRequest.category`, which is unvalidated and arrives from the
        client. The server scanned the release itself and already knows which
        listing it came from, so it should answer this question rather than
        accept an answer back.

        Read from the cached scan row's JSON, not from `source_category` --
        that column holds the SOURCE NAME ('HDEncode' on every one of the
        4,084 live rows), while the crawl category ('4k' | 'remux' | 'tv') is
        inside `data`. Verified before relying on it.

        Returns None when the URL was never scanned by this server, which is
        NOT the same as a category of ''. The caller must treat it as
        "cannot verify" and record nothing.
        """
        if not url:
            return None
        row = self._query(
            "SELECT data FROM background_scan_cache WHERE url = ?",
            (str(url),), one=True, default=None)
        if not row:
            return None
        try:
            payload = json.loads(dict(row).get("data") or "{}")
        except (TypeError, ValueError):
            # Unreadable evidence is not absent evidence, but it is not usable
            # either. None here means the caller records nothing.
            logger.warning("scan cache row for %s has undecodable data", url)
            return None
        if not payload.get("category_attested"):
            # NEVER CHECKED is not CHECKED AND CLEAN. A row written by the old
            # first-source-wins crawler carries no attestation, and reading its
            # absence as 'no conflict' would let the exact pre-fix state survive
            # the fix -- a release that appeared in BOTH listings before conflict
            # detection existed still looks unconflicted. It becomes usable the
            # next time a conflict-aware crawl observes it.
            logger.debug("no media kind for %s: classification never attested", url)
            return None
        if payload.get("category_conflict"):
            # Two listings classified this release differently and the crawl
            # recorded that rather than picking the one it happened to see
            # first. There is no server-owned answer here, so there is no
            # answer -- returning the first-seen category would be exactly the
            # silent movie-wins outcome M1 is about.
            logger.info("no media kind for %s: listings disagree about its type", url)
            return None
        category = str(payload.get("category") or "").strip().lower()
        return category or None

    def get_release_identity(self, urls):
        """Map release url -> the SEMANTIC identity recorded when it was grabbed:
        ``{"date_added", "title", "year", "season"}``.

        `date_added` is NOT "first grabbed": download_item() writes a history row
        for FAILED attempts too, so this can be the moment a grab was first tried
        rather than the moment one succeeded. The UI labels it "first seen" for
        exactly that reason (peer review Finding 2).

        WHY TITLE/YEAR/SEASON BELONG HERE RATHER THAN IN A NAME PARSER. These
        columns are what the caller PASSED to download_item() from the scraped
        listing, so they are the identity itself, not a reading of a display
        string. The JDownloader package name cannot substitute for them: it is
        capped at 50 characters, and 17 live rows carry a name that spans more
        than one season -- `Law & Order: LA (2010) [1080p]` alone covers 13
        distinct seasons (8, 9, and 11 through 21). No parser can recover a
        season from a string that does not contain it.

        Returns only urls that matched a row. A caller must treat a missing key
        as UNKNOWN identity, never as "no season" -- the difference is what
        stops a whole-series pack being cancelled against one season.
        """
        wanted = [str(u) for u in dict.fromkeys(urls or []) if u]
        if not wanted:
            return {}
        out = {}
        for start in range(0, len(wanted), 300):
            chunk = wanted[start:start + 300]
            rows = self._query_dicts(
                "SELECT url, date_added, title, year, season, media_kind "
                "FROM downloads WHERE url IN (%s)" % ",".join("?" * len(chunk)),
                tuple(chunk), default=[]) or []
            for row in rows:
                out[row["url"]] = {
                    "date_added": row.get("date_added"),
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "season": row.get("season"),
                    "media_kind": row.get("media_kind"),
                }
        return out

    def get_persisted_provenance(self, ids):
        """Map ``download_results.id`` -> its PERSISTED ``provenance_url``.

        KEYED BY THE DURABLE ROW ID, not by package name. An earlier version
        also matched on ``name`` for rows without a uuid, and that was wrong in
        the one way this whole feature exists to prevent: the query carried no
        ``package_uuid IS NULL`` predicate and no ``ORDER BY``, so a DIFFERENT
        same-named row -- including a uuid-backed one -- could donate its
        provenance, and the "last-write-wins" the comment claimed was really
        whichever row SQLite happened to return last. Identical package names
        across distinct seasons are precisely why identity is being moved off
        names, so recovering by name reintroduced the collision through the back
        door, for both `source_url` and identity (peer review round 2).

        ``poll_results()`` attaches this id to every row it emits -- from its
        uuid->id cache, else via ``get_download_result_id()``, writing the row
        first if need be, explicitly so it never emits an id-less row -- and
        REST rows carry the same persisted id. A row that still has no id is
        left unrecovered, which is the safe direction.

        WHY THIS EXISTS. The poller's in-memory row carries
        ``provenance_url=None`` whenever it could not observe a package's links,
        while this table deliberately KEEPS the previous proof in that case --
        ``upsert_download_result`` writes the new value only when
        ``provenance_observed`` is true, because "could not look" is not
        "no longer ours". So the WebSocket row and the persisted row disagree
        for the length of an unobserved poll. That was accepted while the only
        consequence was a source link blinking; it is not acceptable for
        identity, which is meant to authorise cancelling other downloads
        (peer review 2026-08-18, M2).

        ONE batched query, and only for the rows that need it -- the caller
        filters to unobserved-and-urlless rows first, which is normally none.
        """
        wanted = []
        for value in (ids or []):
            # STRICT, not coercive. Production ids are SQLite integers, so
            # int() bought nothing and quietly widened the contract -- True
            # became 1 and 1.0 became 1, either of which would look up a real
            # row. `type(value) is int` rather than isinstance() because bool
            # IS an int subclass. Fail closed and log: annotation is
            # deliberately non-fatal, so a malformed id must not take down the
            # downloads view, but it should not pass silently either.
            if type(value) is int and value > 0:
                wanted.append(value)
            elif value is not None:
                logger.warning(
                    "download results: ignoring malformed row id %r (%s) in "
                    "provenance recovery", value, type(value).__name__)
        wanted = list(dict.fromkeys(wanted))
        if not wanted:
            return {}
        out = {}
        for start in range(0, len(wanted), 300):
            chunk = wanted[start:start + 300]
            rows = self._query_dicts(
                "SELECT id, provenance_url FROM download_results WHERE id IN (%s)"
                % ",".join("?" * len(chunk)),
                tuple(chunk), default=[]) or []
            for row in rows:
                out[row["id"]] = row.get("provenance_url")
        return out

    def get_download_result_id(self, package_uuid, name):
        """Resolve a download_results row id for a package: by ``package_uuid``
        when present, else the most recent legacy NULL-uuid row with the same
        ``name``. Returns None if no matching row exists.

        Used by the poller to recover an id for a row whose write the
        in-memory uuid->id cache doesn't (yet) know about — e.g. after a
        process restart — without re-inserting a duplicate row.
        """
        try:
            if package_uuid is not None:
                row = self._query(
                    "SELECT id FROM download_results WHERE package_uuid = ?",
                    (package_uuid,), one=True, default=None)
                if row:
                    return row[0]
            row = self._query(
                "SELECT id FROM download_results WHERE package_uuid IS NULL AND name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (name,), one=True, default=None)
            return row[0] if row else None
        except Exception as e:
            logger.error("DB Error (get_download_result_id): %s", e)
            return None

    def clear_download_results(self):
        """Delete all tracked download/extraction outcomes."""
        return self._mutate("DELETE FROM download_results", label="clear_download_results")

    def delete_download_result(self, id_):
        """Delete the tracked download/extraction outcome for a single package
        by its row ``id``. Returns rows affected (0 if none).

        Unlike ``_mutate`` (which returns True/False), this needs the actual
        row count for the caller to distinguish "deleted" from "already gone",
        so it talks to the connection directly under the same lock pattern.
        """
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cursor = conn.execute(
                    "DELETE FROM download_results WHERE id = ?", (id_,))
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error("DB Error (delete_download_result): %s", e)
            return 0


    @staticmethod
    def _backup_file(path: str) -> None:
        """Move a migrated legacy file aside, replacing any older backup."""
        backup_path = path + ".bak"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.replace(path, backup_path)

    # ── Legacy migration ─────────────────────────────────────────────

    def migrate_json_data(self, history_file, cache_file):
        """Migrate data from legacy JSON files (history.json, cache.json).

        Imported files are renamed to .bak after successful migration.

        Returns:
            Tuple of (migrated_history_count, migrated_cache_count).
        """
        migrated_history = 0
        migrated_cache = 0

        # 1. History file
        if history_file and os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    urls = data if isinstance(data, list) else data.get("downloaded_urls", [])

                    with self.transaction() as conn:
                        if conn:
                            for url in urls:
                                conn.execute(
                                    "INSERT OR IGNORE INTO downloads (url, title) VALUES (?, ?)",
                                    (url, "Unknown (Migrated)"))
                    migrated_history = len(urls)

                self._backup_file(history_file)
                logger.info("Migrated %d history items.", migrated_history)
            except Exception as e:
                logger.error("Migration Error (History): %s", e)

        # 2. Cache file
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    items = cache_data.get("items", [])
                    mode = cache_data.get("mode", "Movies")
                    self.save_plex_cache(items, mode)
                    migrated_cache = len(items)

                self._backup_file(cache_file)
                logger.info("Migrated %d cache items.", migrated_cache)
            except Exception as e:
                logger.error("Migration Error (Cache): %s", e)

        return migrated_history, migrated_cache

    # ── Scan history ─────────────────────────────────────────────────

    def save_scan_history(self, scan_data):
        """Persist a scan run's summary statistics."""
        return self._mutate('''
            INSERT INTO scan_history (
                timestamp, scan_type, items_scanned, missing_count,
                upgrade_count, dv_upgrade_count, in_library_count,
                duration_seconds, sources_scanned, plex_items_cached
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            scan_data.get('timestamp'),
            scan_data.get('scan_type', 'Full Scan'),
            scan_data.get('items_scanned', 0),
            scan_data.get('missing_count', 0),
            scan_data.get('upgrade_count', 0),
            scan_data.get('dv_upgrade_count', 0),
            scan_data.get('in_library_count', 0),
            scan_data.get('duration_seconds', 0),
            scan_data.get('sources_scanned', ''),
            scan_data.get('plex_items_cached', 0)
        ), label="save_scan_history")

    def get_scan_history(self, limit=50):
        """Get recent scan history records, newest first."""
        return self._query_dicts(
            'SELECT * FROM scan_history ORDER BY timestamp DESC LIMIT ?',
            (limit,))

    def get_scan_stats(self):
        """Get aggregate statistics across all scans."""
        row = self._query('''
            SELECT
                COUNT(*) as total_scans,
                AVG(duration_seconds) as avg_duration,
                SUM(items_scanned) as total_items_scanned,
                SUM(missing_count) as total_missing,
                SUM(upgrade_count) as total_upgrades,
                MAX(timestamp) as last_scan
            FROM scan_history
        ''', one=True, default=None)
        if not row:
            return {}
        return {
            'total_scans': row['total_scans'],
            'avg_duration': round(row['avg_duration'] or 0, 2),
            'total_items_scanned': row['total_items_scanned'] or 0,
            'total_missing': row['total_missing'] or 0,
            'total_upgrades': row['total_upgrades'] or 0,
            'last_scan': row['last_scan']
        }

    def clear_scan_history(self):
        """Delete all scan history records."""
        return self._mutate("DELETE FROM scan_history", label="clear_scan_history")

    # ── Scanned URLs (incremental scan tracking) ─────────────────────

    def is_url_scanned(self, url):
        """Check if a URL has been seen in a previous scan."""
        return self._query('SELECT 1 FROM scanned_urls WHERE url = ?', (url,),
                           one=True, default=None) is not None

    def get_policy_excluded_urls(self, source: str = "hdencode") -> set:
        """URLs already known to be excluded by policy, for skip decisions."""
        rows = self._query_dicts(
            "SELECT canonical_url FROM listing_policy_exclusions WHERE source = ?",
            (source,), default=[])
        # Canonicalise on read as well as write, so a row written before this
        # invariant existed still matches.
        return {canonicalize_listing_url(row["canonical_url"]) for row in rows}

    def record_policy_exclusions(self, rows) -> int:
        """Upsert observed policy exclusions. Returns the number written.

        first_seen_at is preserved on re-observation; last_seen_at advances, so
        an exclusion that leaves the listing can be aged out later without
        losing when it was first seen.
        """
        rows = list(rows or [])
        if not rows:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        written = 0
        seen = set()
        with self.transaction() as conn:
            if not conn:
                raise RuntimeError("Database unavailable")
            for row in rows:
                # Canonicalise HERE, at the storage boundary, so the store is
                # canonical BY CONSTRUCTION rather than because one caller
                # remembered to normalise first. A second writer (RSS) must not
                # be able to break the invariant.
                url = canonicalize_listing_url((row or {}).get("url"))
                if not url or url in seen:
                    continue
                seen.add(url)
                written += 1
                conn.execute(
                    """INSERT INTO listing_policy_exclusions (
                        canonical_url, source, category, listing_title,
                        policy_reason, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_url) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        listing_title = excluded.listing_title""",
                    (url, str(row.get("source") or "hdencode"),
                     row.get("category"), row.get("title"),
                     str(row.get("reason") or "listing_policy_excluded_full_disc"),
                     now, now),
                )
        # The number actually written, not len(rows): empty and duplicate
        # identities would otherwise overstate it to any caller that trusts it.
        return written

    def count_policy_exclusions(self, source: str = "hdencode") -> int:
        row = self._query(
            "SELECT COUNT(*) AS n FROM listing_policy_exclusions WHERE source = ?",
            (source,), one=True, default=None)
        return int(row["n"]) if row else 0

    def get_scanned_urls(self):
        """Get all previously scanned URLs as a set for fast membership testing."""
        rows = self._query('SELECT url FROM scanned_urls', default=[])
        return {row[0] for row in rows}

    def add_scanned_url(self, url, title=None, source=None):
        """Record a single URL as scanned."""
        return self._mutate('''
            INSERT OR IGNORE INTO scanned_urls (url, title, source) VALUES (?, ?, ?)
        ''', (url, title, source), label="add_scanned_url")

    def add_scanned_urls_batch(self, urls_data):
        """Record multiple scanned URLs in a single transaction.

        Args:
            urls_data: List of dicts with 'url', 'title', 'source' keys.
        """
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany('''
                    INSERT OR IGNORE INTO scanned_urls (url, title, source)
                    VALUES (:url, :title, :source)
                ''', urls_data)
                conn.commit()
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (add_scanned_urls_batch): %s", e)
            return False

    def clear_scanned_urls(self):
        """Clear all scanned URL records (used before deep scans)."""
        return self._mutate("DELETE FROM scanned_urls", label="clear_scanned_urls")

    def get_scanned_url_count(self):
        """Return the total number of scanned URLs."""
        row = self._query('SELECT COUNT(*) FROM scanned_urls', one=True, default=None)
        return row[0] if row else 0

    # ── Dismissed items (mobile swipe-to-skip) ───────────────────────────

    def _dismissed_urls_set(self):
        """Return the live in-memory cache, lazily loading it from disk once.

        Must be called while holding ``self._lock``. Callers that mutate the
        table update this same set so it never goes stale without a re-query.
        """
        if self._dismissed_cache is None:
            rows = self._query('SELECT url FROM dismissed_items', default=[])
            self._dismissed_cache = {row[0] for row in rows}
        return self._dismissed_cache

    def add_dismissed_items(self, items):
        """Dismiss multiple URLs in one transaction.

        Args:
            items: Iterable of (url, title) OR (url, title, group_key,
                resolution, dovi) tuples. The extra fields power title-level
                skip: a same-or-lower release of a skipped title stays hidden
                while a genuine upgrade can resurface. Re-dismissing updates
                the stored fields when non-null values are supplied.
        """
        rows = []
        for it in items:
            url = it[0]
            if not url:
                continue
            title = it[1] if len(it) > 1 else None
            group_key = it[2] if len(it) > 2 else None
            resolution = it[3] if len(it) > 3 else None
            dovi = (1 if it[4] else 0) if len(it) > 4 else None
            rows.append({"url": url, "title": title, "group_key": group_key,
                         "resolution": resolution, "dovi": dovi})
        if not rows:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany('''
                    INSERT INTO dismissed_items (url, title, group_key, resolution, dovi)
                    VALUES (:url, :title, :group_key, :resolution, :dovi)
                    ON CONFLICT(url) DO UPDATE SET
                        title = COALESCE(excluded.title, dismissed_items.title),
                        group_key = COALESCE(excluded.group_key, dismissed_items.group_key),
                        resolution = COALESCE(excluded.resolution, dismissed_items.resolution),
                        dovi = COALESCE(excluded.dovi, dismissed_items.dovi)
                ''', rows)
                conn.commit()
                self._dismissed_urls_set().update(r["url"] for r in rows)
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (add_dismissed_items): %s", e)
            return False

    def add_dismissed_item(self, url, title=None):
        """Record a single release URL as dismissed (swiped away)."""
        return self.add_dismissed_items([(url, title)])

    def remove_dismissed_items(self, urls):
        """Un-dismiss multiple URLs in one transaction so they can reappear."""
        urls = [u for u in urls if u]
        if not urls:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany(
                    'DELETE FROM dismissed_items WHERE url = ?', [(u,) for u in urls])
                conn.commit()
                self._dismissed_urls_set().difference_update(urls)
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (remove_dismissed_items): %s", e)
            return False

    def remove_dismissed_item(self, url):
        """Un-dismiss a previously dismissed URL so it can reappear."""
        return self.remove_dismissed_items([url])

    def get_dismissed_urls(self):
        """Get all dismissed URLs as a set for fast membership testing."""
        with self._lock:
            return set(self._dismissed_urls_set())

    def get_dismissed_items(self, limit=1000):
        """Return dismissed items (url, title, dismissed_at), newest first."""
        return self._query_dicts(
            'SELECT url, title, dismissed_at FROM dismissed_items '
            'ORDER BY dismissed_at DESC LIMIT ?', (limit,), default=[])

    def get_dismissed_title_quality(self):
        """Per dismissed group_key, the (resolution, dovi) of the BEST release
        that was skipped — so the read path can hide same-or-lower releases of
        a skipped title while letting a genuine upgrade resurface. Rows without
        a group_key (legacy per-URL dismissals) are ignored here; those still
        hide by exact URL."""
        return self._query(
            "SELECT group_key, resolution, dovi FROM dismissed_items "
            "WHERE group_key IS NOT NULL AND group_key != ''",
            default=[])

    def clear_dismissed_items(self):
        """Clear all dismissed-item records."""
        ok = self._mutate("DELETE FROM dismissed_items", label="clear_dismissed_items")
        if ok:
            with self._lock:
                self._dismissed_cache = set()
        return ok

    def get_dismissed_count(self):
        """Return the total number of dismissed items."""
        row = self._query('SELECT COUNT(*) FROM dismissed_items', one=True, default=None)
        return row[0] if row else 0

    # ── Bookmarks (per-title, distinct from watchlist) ────────────────────

    def add_bookmark(self, imdb_id, title, year, media_type):
        """Add a per-title bookmark. Idempotent: bookmarking the same
        identity (imdb_id, or normalized-title+year+media_type when no
        imdb_id) twice is a no-op, not a duplicate row. Returns True on
        success."""
        from backend.app_service import normalize_title
        title_key = normalize_title(title or "")
        if imdb_id:
            return self._mutate('''
                INSERT INTO bookmarks (imdb_id, title, title_key, year, media_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(imdb_id) WHERE imdb_id IS NOT NULL DO NOTHING
            ''', (imdb_id, title, title_key, year, media_type), label="add_bookmark")
        return self._mutate('''
            INSERT INTO bookmarks (imdb_id, title, title_key, year, media_type)
            VALUES (NULL, ?, ?, ?, ?)
            ON CONFLICT(title_key, year, media_type) WHERE imdb_id IS NULL DO NOTHING
        ''', (title, title_key, year, media_type), label="add_bookmark")

    def remove_bookmark(self, imdb_id, title, year, media_type):
        """Remove a bookmark by the same identity resolution add_bookmark uses.
        Returns True if a row was actually deleted, False if nothing matched."""
        from backend.app_service import normalize_title
        title_key = normalize_title(title or "")
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                cur = conn.cursor()
                if imdb_id:
                    cur.execute('DELETE FROM bookmarks WHERE imdb_id = ?', (imdb_id,))
                else:
                    cur.execute(
                        'DELETE FROM bookmarks WHERE imdb_id IS NULL '
                        'AND title_key = ? AND year IS ? AND media_type = ?',
                        (title_key, year, media_type))
                deleted = cur.rowcount > 0
                conn.commit()
            return deleted
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (remove_bookmark): %s", e)
            return False

    def list_bookmarks(self):
        """Return every bookmark row (dicts), newest first."""
        return self._query_dicts(
            'SELECT id, imdb_id, title, year, media_type, created_at '
            'FROM bookmarks ORDER BY created_at DESC', default=[])

    def list_bookmark_keys(self):
        """Return the full set of bookmark identity keys in one query, for
        bulk per-item matching (avoids an N+1 query per result row). Each key
        is ('imdb', imdb_id) or ('title', title_key, year, media_type)."""
        rows = self._query_dicts(
            'SELECT imdb_id, title_key, year, media_type FROM bookmarks', default=[])
        keys = set()
        for r in rows:
            if r.get("imdb_id"):
                keys.add(("imdb", r["imdb_id"]))
            else:
                keys.add(("title", r.get("title_key"), r.get("year"), r.get("media_type")))
        return keys

    # ── Auth: admin password (single row) ─────────────────────────────

    def get_password_hash(self):
        """Return the stored bcrypt password hash, or None if unset."""
        row = self._query(
            'SELECT password_hash FROM auth_credentials WHERE id = 1',
            one=True, default=None)
        return row[0] if row else None

    def has_password(self):
        """Whether an admin password has been configured."""
        return self.get_password_hash() is not None

    def set_password_hash(self, password_hash):
        """Set or replace the admin password hash."""
        return self._mutate('''
            INSERT INTO auth_credentials (id, password_hash, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                password_hash = excluded.password_hash,
                updated_at = excluded.updated_at
        ''', (password_hash,), label="set_password_hash")

    def clear_password(self):
        """Remove the admin password (reverts to nonce-only / open auth)."""
        return self._mutate(
            "DELETE FROM auth_credentials WHERE id = 1", label="clear_password")

    # ── Auth: login sessions ──────────────────────────────────────────

    def create_session(self, token_hash, expires_at):
        """Persist a session by its token hash + ISO-8601 expiry."""
        return self._mutate('''
            INSERT INTO auth_sessions (token_hash, expires_at)
            VALUES (?, ?)
            ON CONFLICT(token_hash) DO UPDATE SET expires_at = excluded.expires_at
        ''', (token_hash, expires_at), label="create_session")

    def get_session_expiry(self, token_hash):
        """Return a session's ISO-8601 expiry, or None if it doesn't exist."""
        row = self._query(
            'SELECT expires_at FROM auth_sessions WHERE token_hash = ?',
            (token_hash,), one=True, default=None)
        return row[0] if row else None

    def delete_session(self, token_hash):
        """Invalidate a single session (logout)."""
        return self._mutate(
            "DELETE FROM auth_sessions WHERE token_hash = ?",
            (token_hash,), label="delete_session")

    def delete_all_sessions(self):
        """Invalidate every session (e.g. after a password change)."""
        return self._mutate("DELETE FROM auth_sessions", label="delete_all_sessions")

    def purge_expired_sessions(self, now_iso):
        """Delete sessions whose expiry is at or before ``now_iso``."""
        return self._mutate(
            "DELETE FROM auth_sessions WHERE expires_at <= ?",
            (now_iso,), label="purge_expired_sessions")

    def count_sessions(self):
        """Return the number of stored sessions (expired or not)."""
        row = self._query('SELECT COUNT(*) FROM auth_sessions', one=True, default=None)
        return row[0] if row else 0

    # ── Background scan cache ─────────────────────────────────────────

    def upsert_background_cache(self, items):
        """Insert/update cached background-scan results, keyed by URL.

        Keeps each row's original ``scraped_at`` and refreshes ``last_seen_at``
        plus any changed fields on re-scrape.

        Args:
            items: iterable of dicts with keys url, title, year, status,
                source_category, data (a JSON string of the full result dict).
        """
        rows = [it for it in items if it.get("url")]
        if not rows:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany('''
                    INSERT INTO background_scan_cache
                        (url, title, year, status, source_category, data,
                         parse_version, scraped_at, last_seen_at)
                    VALUES
                        (:url, :title, :year, :status, :source_category, :data,
                         :parse_version, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        year = excluded.year,
                        status = excluded.status,
                        source_category = COALESCE(
                            NULLIF(background_scan_cache.source_category, ''),
                            excluded.source_category),
                        data = excluded.data,
                        parse_version = excluded.parse_version,
                        -- Round-11 Finding 2: the cache write IS the
                        -- successful re-derivation boundary -- a re-scraped
                        -- stale row heals here or re-scrapes forever.
                        derived_state = 'current',
                        last_seen_at = CURRENT_TIMESTAMP
                ''', [{
                    "url": it.get("url"),
                    "title": it.get("title"),
                    "year": it.get("year"),
                    "status": it.get("status"),
                    "source_category": it.get("source_category"),
                    "data": it.get("data"),
                    "parse_version": release_grammar.GRAMMAR_VERSION,
                } for it in rows])
                conn.commit()
                self._bg_cache_rev += 1
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (upsert_background_cache): %s", e)
            return False

    def get_background_cache(self, limit=2000):
        """Return cached background-scan rows, most recently seen first."""
        return self._query_dicts(
            'SELECT url, title, year, status, source_category, data, '
            'scraped_at, last_seen_at FROM background_scan_cache '
            'ORDER BY last_seen_at DESC LIMIT ?', (limit,), default=[])

    def get_background_cache_by_url(self, url):
        """Return one cached background-scan row by URL, or None."""
        rows = self._query_dicts(
            'SELECT url, title, year, status, source_category, data, '
            'scraped_at, last_seen_at FROM background_scan_cache '
            'WHERE url = ? LIMIT 1', (url,), default=[])
        return rows[0] if rows else None

    def enrich_downloads_from_cache(self):
        """Backfill empty resolution/size/hdr/dovi on download-history rows from
        the background scan cache, matched by URL.

        Accurate because the URL identifies the exact release that was grabbed.
        Idempotent — only touches rows that are still missing the data and have a
        matching cached release. Returns the number of rows updated."""
        import json as _json
        # Fetch candidates under the lock, then parse JSON outside it so we
        # don't hold the lock while doing CPU-bound work on potentially many rows.
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                raw_rows = conn.cursor().execute(
                    "SELECT d.url, c.data FROM downloads d "
                    "JOIN background_scan_cache c ON c.url = d.url "
                    "WHERE (d.resolution IS NULL OR d.resolution = '') "
                    "AND d.url IS NOT NULL"
                ).fetchall()
        except Exception as e:
            logger.error("DB Error (enrich_downloads_from_cache fetch): %s", e)
            return 0

        to_update = []
        for url, data in raw_rows:
            try:
                rel = _json.loads(data) if data else {}
            except Exception:
                continue
            res = rel.get('resolution') or ''
            size = rel.get('size') or ''
            hdr = rel.get('hdr') or None
            dovi = 1 if rel.get('dovi') else 0
            if not (res or size or hdr or dovi):
                continue
            to_update.append((res, size, hdr, dovi, url))

        if not to_update:
            return 0
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                conn.cursor().executemany(
                    "UPDATE downloads SET resolution=?, size=?, hdr=?, dovi=? "
                    "WHERE url=? AND (resolution IS NULL OR resolution = '')",
                    to_update)
                conn.commit()
            updated = len(to_update)
            logger.info("Enriched %d download-history row(s) from scan cache", updated)
            return updated
        except Exception as e:
            logger.error("DB Error (enrich_downloads_from_cache write): %s", e)
            return 0

    # ── Dolby Vision layer inventory (dv_scan) ────────────────────────────

    # Durable 4K metadata inventory. ``dv_scan`` and ``media_probe`` remain
    # compatibility caches; these helpers preserve run/item history and the
    # evidence needed to distinguish an unknown from a known negative.

    _METADATA_SCAN_SCOPES = frozenset({"pilot", "full", "targeted"})
    _METADATA_SCAN_RUN_STATUSES = frozenset({
        "queued", "running", "paused", "cancelled", "completed", "failed", "interrupted",
    })
    _METADATA_SCAN_ITEM_STATUSES = frozenset({
        "pending", "running", "current", "failed", "skipped", "cancelled", "interrupted",
    })

    def backfill_dv_seed_baseline(self):
        """Copy legacy imported seed evidence into its immutable baseline table.

        ``dv_scan`` is a compatibility cache where a local scan deliberately
        replaces a same-path seed row. The baseline table is append-only for a
        path, so this operation is safe to run at startup and during imports.
        """
        try:
            with self.transaction() as conn:
                if not conn:
                    return 0
                cursor = conn.execute('''
                    INSERT OR IGNORE INTO dv_seed_baseline
                        (path, seed_layer, title, sig_mtime, sig_size,
                         rating_key, imdb_id, seed_scanned_at)
                    SELECT path, dv_layer, title, sig_mtime, sig_size,
                           rating_key, imdb_id, scanned_at
                    FROM dv_scan
                    WHERE source = 'seed'
                ''')
                return max(cursor.rowcount, 0)
        except Exception as exc:
            logger.error("DB Error (backfill_dv_seed_baseline): %s", exc)
            return 0

    def get_dv_seed_baseline(self, path):
        """Return preserved imported FEL/MEL evidence for *path*, if any."""
        rows = self._query_dicts(
            'SELECT path, seed_layer, title, sig_mtime, sig_size, rating_key, '
            'imdb_id, seed_scanned_at, imported_at FROM dv_seed_baseline WHERE path = ?',
            (path,),
        )
        return rows[0] if rows else None

    def list_dv_seed_baseline(self, *, limit=1000000):
        """Return immutable imported seed evidence for reconciliation reports."""
        try:
            limit = max(1, min(int(limit), 1000000))
        except (TypeError, ValueError):
            limit = 1000000
        return self._query_dicts(
            'SELECT path, seed_layer, title, sig_mtime, sig_size, rating_key, '
            'imdb_id, seed_scanned_at, imported_at FROM dv_seed_baseline '
            'ORDER BY path ASC LIMIT ?',
            (limit,), default=[],
        )

    def create_metadata_scan_run(self, *, scope, expected_count=0):
        """Create a durable scan run before any file analysis begins."""
        if scope not in self._METADATA_SCAN_SCOPES:
            raise ValueError(f"Unsupported metadata scan scope: {scope!r}")
        try:
            expected_count = int(expected_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_count must be a non-negative integer") from exc
        if expected_count < 0:
            raise ValueError("expected_count must be a non-negative integer")

        run_uuid = str(uuid.uuid4())
        try:
            with self.transaction() as conn:
                if not conn:
                    return None
                conn.execute('''
                    INSERT INTO metadata_scan_runs (run_uuid, scope, status, expected_count)
                    VALUES (?, ?, 'queued', ?)
                ''', (run_uuid, scope, expected_count))
            return self.get_metadata_scan_run(run_uuid)
        except Exception as exc:
            logger.error("DB Error (create_metadata_scan_run): %s", exc)
            return None

    def get_metadata_scan_run(self, run_uuid):
        """Return the durable run record for *run_uuid*, if present."""
        rows = self._query_dicts(
            'SELECT run_uuid, scope, status, expected_count, created_at, started_at, '
            'completed_at, cancelled_at, error_code, error_message '
            'FROM metadata_scan_runs WHERE run_uuid = ?',
            (run_uuid,),
        )
        return rows[0] if rows else None

    def update_metadata_scan_run(self, run_uuid, *, status, error_code=None, error_message=None):
        """Transition one durable scan run through its explicit status vocabulary."""
        if status not in self._METADATA_SCAN_RUN_STATUSES or not run_uuid:
            return False
        terminal = status in {"cancelled", "completed", "failed", "interrupted"}
        try:
            with self.transaction() as conn:
                if not conn:
                    return False
                cursor = conn.execute('''
                    UPDATE metadata_scan_runs
                    SET status = ?, error_code = ?, error_message = ?,
                        started_at = CASE WHEN ? = 'running' AND started_at IS NULL
                            THEN CURRENT_TIMESTAMP ELSE started_at END,
                        cancelled_at = CASE WHEN ? = 'cancelled' THEN CURRENT_TIMESTAMP
                            ELSE cancelled_at END,
                        completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP
                            ELSE completed_at END
                    WHERE run_uuid = ?
                ''', (status, error_code, error_message, status, status, terminal, run_uuid))
                return cursor.rowcount == 1
        except Exception as exc:
            logger.error("DB Error (update_metadata_scan_run): %s", exc)
            return False

    def create_metadata_scan_items(self, run_uuid, items):
        """Persist a scan manifest before scheduling workers.

        Duplicate paths in the same run are ignored. No filesystem state is
        observed here; callers provide only read-only Plex manifest facts.
        """
        rows = []
        for item in items or []:
            path = (item or {}).get("path")
            if not path:
                continue
            rows.append((
                run_uuid, path, item.get("library_name"), item.get("rating_key"),
                item.get("title"), item.get("sig_mtime"), item.get("sig_size"),
            ))
        if not rows:
            return 0
        try:
            with self.transaction() as conn:
                if not conn or not conn.execute(
                    'SELECT 1 FROM metadata_scan_runs WHERE run_uuid = ?', (run_uuid,)
                ).fetchone():
                    return 0
                created = 0
                for row in rows:
                    cursor = conn.execute('''
                        INSERT OR IGNORE INTO metadata_scan_items
                            (run_uuid, path, library_name, rating_key, title, sig_mtime, sig_size)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', row)
                    created += max(cursor.rowcount, 0)
                return created
        except Exception as exc:
            logger.error("DB Error (create_metadata_scan_items): %s", exc)
            return 0

    def list_metadata_scan_items(self, run_uuid, *, status=None, limit=100000):
        """Return persisted manifest rows in stable path order."""
        clauses = ['run_uuid = ?']
        params = [run_uuid]
        if status is not None:
            if status not in self._METADATA_SCAN_ITEM_STATUSES:
                return []
            clauses.append('status = ?')
            params.append(status)
        try:
            limit = max(1, min(int(limit), 100000))
        except (TypeError, ValueError):
            limit = 100000
        params.append(limit)
        return self._query_dicts(
            'SELECT run_uuid, path, library_name, rating_key, title, status, attempt_count, '
            'sig_mtime, sig_size, failure_stage, error_code, error_message, started_at, '
            'completed_at, updated_at FROM metadata_scan_items WHERE ' +
            ' AND '.join(clauses) + ' ORDER BY path ASC LIMIT ?',
            tuple(params),
            default=[],
        )

    def update_metadata_scan_item(self, run_uuid, path, *, status, failure_stage=None,
                                  error_code=None, error_message=None):
        """Transition one manifest row using the explicit item status vocabulary."""
        if status not in self._METADATA_SCAN_ITEM_STATUSES or not run_uuid or not path:
            return False
        terminal = status in {"current", "failed", "skipped", "cancelled", "interrupted"}
        try:
            with self.transaction() as conn:
                if not conn:
                    return False
                cursor = conn.execute('''
                    UPDATE metadata_scan_items
                    SET status = ?,
                        attempt_count = attempt_count + CASE WHEN ? = 'running' THEN 1 ELSE 0 END,
                        failure_stage = ?, error_code = ?, error_message = ?,
                        started_at = CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP ELSE started_at END,
                        completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_uuid = ? AND path = ?
                ''', (status, status, failure_stage, error_code, error_message,
                      status, terminal, run_uuid, path))
                return cursor.rowcount == 1
        except Exception as exc:
            logger.error("DB Error (update_metadata_scan_item): %s", exc)
            return False

    def interrupt_abandoned_metadata_scans(self):
        """Atomically preserve work left running by an earlier process.

        This is intentionally limited to ``running`` state. A user-paused run
        remains paused across restart, while an in-flight item becomes
        explicitly retryable rather than silently returning to pending.
        """
        try:
            with self.transaction() as conn:
                if not conn:
                    return 0
                run_count = conn.execute(
                    "SELECT COUNT(*) FROM metadata_scan_runs WHERE status = 'running'"
                ).fetchone()[0]
                conn.execute('''
                    UPDATE metadata_scan_items
                    SET status = 'interrupted', failure_stage = 'process',
                        error_code = 'process_interrupted',
                        error_message = 'Scan process ended before this item completed',
                        completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'running'
                      AND run_uuid IN (
                          SELECT run_uuid FROM metadata_scan_runs WHERE status = 'running'
                      )
                ''')
                conn.execute('''
                    UPDATE metadata_scan_runs
                    SET status = 'interrupted', error_code = 'process_interrupted',
                        error_message = 'Scan process ended before the run completed',
                        completed_at = CURRENT_TIMESTAMP
                    WHERE status = 'running'
                ''')
                return int(run_count)
        except Exception as exc:
            logger.error("DB Error (interrupt_abandoned_metadata_scans): %s", exc)
            return 0

    def prepare_metadata_scan_resume(self, run_uuid, *, retry_failed=False):
        """Reset repairable manifest rows and queue an existing run.

        Successfully scanned items are immutable for the resumed attempt. A
        normal resume retries interrupted/cancelled work; ``retry_failed`` also
        includes terminal probe failures selected by the operator.

        Returns the count of PENDING work the resumed run has to do — which
        includes rows a pause left pending and never needed repairing, not just
        the rows this call reset. The caller treats <= 0 as "nothing to resume",
        so counting only repaired rows made a paused run unresumable.
        """
        if not run_uuid:
            return 0
        statuses = ["interrupted", "cancelled"]
        if retry_failed:
            statuses.append("failed")
        placeholders = ",".join("?" for _ in statuses)
        try:
            with self.transaction() as conn:
                if not conn:
                    return 0
                run = conn.execute(
                    "SELECT status FROM metadata_scan_runs WHERE run_uuid = ?", (run_uuid,)
                ).fetchone()
                if not run or run[0] == "running":
                    return 0
                conn.execute(f'''
                    UPDATE metadata_scan_items
                    SET status = 'pending', failure_stage = NULL, error_code = NULL,
                        error_message = NULL, completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE run_uuid = ? AND status IN ({placeholders})
                ''', (run_uuid, *statuses))
                # RESUMABILITY IS "IS THERE PENDING WORK", NOT "DID THIS UPDATE
                # CHANGE ROWS". Gating on the UPDATE's rowcount was the bug: a
                # user PAUSE leaves every unprocessed row in 'pending' (the
                # worker writes that state deliberately — see
                # plex_metadata_scan._run_durable), and 'pending' is not in the
                # reset set because such a row needs no repair. So a paused run
                # reset 0 rows, returned 0 here, and the caller raised "metadata
                # scan has no retryable items" — the Resume button was dead for
                # exactly the state it exists to serve, and the only way forward
                # was discarding a multi-hour manifest and rescanning from
                # scratch with no cached reuse.
                #
                # Count the pending work instead: it covers both the rows just
                # reset and the ones a pause left already pending, so the run is
                # requeued whenever real work remains and left alone when it
                # genuinely has none (e.g. a fully completed run).
                pending_row = conn.execute('''
                    SELECT COUNT(*) FROM metadata_scan_items
                    WHERE run_uuid = ? AND status = 'pending'
                ''', (run_uuid,)).fetchone()
                pending_count = int(pending_row[0]) if pending_row else 0
                if pending_count == 0:
                    return 0
                conn.execute('''
                    UPDATE metadata_scan_runs
                    SET status = 'queued', completed_at = NULL, cancelled_at = NULL,
                        error_code = NULL, error_message = NULL
                    WHERE run_uuid = ?
                ''', (run_uuid,))
                return pending_count
        except Exception as exc:
            logger.error("DB Error (prepare_metadata_scan_resume): %s", exc)
            return 0

    def upsert_media_inventory(self, item):
        """Upsert a current, searchable technical-metadata projection."""
        item = item or {}
        path = item.get("path")
        if not path:
            return False
        hdr10plus_state = item.get("hdr10plus_state", "unknown")
        scan_state = item.get("scan_state", "unscanned")
        if hdr10plus_state not in {"present", "absent", "unknown"}:
            return False
        if scan_state not in {"unscanned", "current", "stale", "failed", "source_changed"}:
            return False
        return self._mutate('''
            INSERT INTO media_inventory
                (path, library_name, rating_key, title, year, resolution, hdr,
                 hdr10plus_state, dv_layer, dv_profile, scan_state, sig_mtime,
                 sig_size, scan_run_uuid, probe_json, last_scanned_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? = 'current' THEN CURRENT_TIMESTAMP ELSE NULL END,
                    CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                library_name = excluded.library_name,
                rating_key = excluded.rating_key,
                title = excluded.title,
                year = excluded.year,
                resolution = excluded.resolution,
                hdr = excluded.hdr,
                hdr10plus_state = excluded.hdr10plus_state,
                dv_layer = excluded.dv_layer,
                dv_profile = excluded.dv_profile,
                scan_state = excluded.scan_state,
                sig_mtime = excluded.sig_mtime,
                sig_size = excluded.sig_size,
                scan_run_uuid = excluded.scan_run_uuid,
                probe_json = excluded.probe_json,
                last_scanned_at = CASE WHEN excluded.scan_state = 'current'
                    THEN CURRENT_TIMESTAMP ELSE media_inventory.last_scanned_at END,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            path, item.get("library_name"), item.get("rating_key"), item.get("title"),
            item.get("year"), item.get("resolution"), item.get("hdr"), hdr10plus_state,
            item.get("dv_layer"), item.get("dv_profile"), scan_state,
            item.get("sig_mtime"), item.get("sig_size"), item.get("scan_run_uuid"),
            item.get("probe_json"), scan_state,
        ), label="upsert_media_inventory")

    _MEDIA_INVENTORY_EVIDENCE_CTE = '''
        WITH cached_unscanned_4k AS (
            SELECT DISTINCT
                pc.file_path AS path,
                pc.library_name,
                pc.rating_key,
                pc.title,
                pc.year,
                pc.res AS resolution,
                NULL AS hdr,
                'unknown' AS hdr10plus_state,
                NULL AS dv_layer,
                NULL AS dv_profile,
                'unscanned' AS scan_state,
                NULL AS sig_mtime,
                NULL AS sig_size,
                NULL AS scan_run_uuid,
                NULL AS probe_json,
                NULL AS last_scanned_at,
                pc.last_updated AS updated_at
            FROM plex_cache AS pc
            WHERE pc.content_type = 'Movies'
              AND lower(COALESCE(pc.res, '')) IN ('2160p', '4k', 'uhd')
              AND pc.file_path IS NOT NULL
              AND pc.file_path != ''
              AND NOT EXISTS (
                  SELECT 1 FROM media_inventory AS existing
                  WHERE existing.path = pc.file_path
                     OR (
                         pc.rating_key IS NOT NULL
                         AND existing.rating_key = pc.rating_key
                     )
              )
        ),
        inventory_candidates AS (
            SELECT path, library_name, rating_key, title, year, resolution, hdr,
                   hdr10plus_state, dv_layer, dv_profile, scan_state, sig_mtime,
                   sig_size, scan_run_uuid, probe_json, last_scanned_at, updated_at
            FROM media_inventory
            UNION ALL
            SELECT path, library_name, rating_key, title, year, resolution, hdr,
                   hdr10plus_state, dv_layer, dv_profile, scan_state, sig_mtime,
                   sig_size, scan_run_uuid, probe_json, last_scanned_at, updated_at
            FROM cached_unscanned_4k
        ),
        seed_by_rating AS (
            SELECT rating_key,
                   CASE WHEN COUNT(DISTINCT lower(seed_layer)) = 1
                        THEN MIN(lower(seed_layer)) ELSE 'conflict' END AS seed_layer
            FROM dv_seed_baseline
            WHERE rating_key IS NOT NULL AND seed_layer IS NOT NULL
            GROUP BY rating_key
        ),
        live_by_rating AS (
            SELECT rating_key,
                   CASE WHEN COUNT(DISTINCT lower(dv_layer)) = 1
                        THEN MIN(lower(dv_layer)) ELSE 'conflict' END AS scan_layer
            FROM dv_scan
            WHERE source = 'scan' AND rating_key IS NOT NULL AND dv_layer IS NOT NULL
            GROUP BY rating_key
        ),
        evidence_base AS (
            SELECT candidate.*,
                   COALESCE(seed_path.seed_layer, seed_rating.seed_layer) AS seed_layer,
                   COALESCE(live_path.dv_layer, live_rating.scan_layer) AS scan_layer
            FROM inventory_candidates AS candidate
            LEFT JOIN dv_seed_baseline AS seed_path ON seed_path.path = candidate.path
            LEFT JOIN seed_by_rating AS seed_rating
                   ON seed_rating.rating_key = candidate.rating_key
            LEFT JOIN dv_scan AS live_path
                   ON live_path.path = candidate.path AND live_path.source = 'scan'
            LEFT JOIN live_by_rating AS live_rating
                   ON live_rating.rating_key = candidate.rating_key
        ),
        inventory_evidence AS (
            SELECT evidence_base.*,
                CASE
                    WHEN seed_layer IS NOT NULL AND scan_layer IS NULL
                        THEN 'seed_unverified'
                    WHEN seed_layer IS NOT NULL AND scan_layer IS NOT NULL
                         AND lower(seed_layer) != lower(scan_layer)
                        THEN 'seed_' || lower(seed_layer) || '_live_' || lower(scan_layer)
                    WHEN seed_layer IS NOT NULL AND scan_layer IS NOT NULL
                        THEN 'verified'
                    WHEN seed_layer IS NULL AND scan_layer IS NOT NULL
                        THEN 'live_only'
                    ELSE 'none'
                END AS discrepancy
            FROM evidence_base
        )
    '''

    def search_media_inventory(self, *, q=None, library=None, resolution=None, hdr=None,
                               hdr10plus_state=None, dv_layer=None, dv_profile=None,
                               scan_state=None, discrepancy=None, page=1, page_size=100,
                               sort="title"):
        """Search the inventory through a fixed, indexed filter vocabulary.

        ``sort`` is allowlisted rather than interpolated from caller input;
        all values are bound parameters. The stable path tiebreaker makes CSV,
        API pagination, and later Kometa reconciliation deterministic.
        """
        filter_columns = {
            "library": ("library_name", library),
            "resolution": ("resolution", resolution),
            "hdr": ("hdr", hdr),
            "hdr10plus_state": ("hdr10plus_state", hdr10plus_state),
            "dv_layer": ("dv_layer", dv_layer),
            "dv_profile": ("dv_profile", dv_profile),
            "scan_state": ("scan_state", scan_state),
            "discrepancy": ("discrepancy", discrepancy),
        }
        clauses, params = [], []
        for _name, (column, value) in filter_columns.items():
            if value is not None and value != "":
                clauses.append(f"{column} = ?")
                params.append(value)
        if q:
            clauses.append("(title LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')")
            escaped = str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sort_columns = {
            "title": "title COLLATE NOCASE",
            "updated": "updated_at DESC",
            "resolution": "resolution COLLATE NOCASE",
            "scan_state": "scan_state COLLATE NOCASE",
        }
        order = sort_columns.get(sort, sort_columns["title"])
        try:
            page = max(1, int(page))
            page_size = max(1, min(int(page_size), 500))
        except (TypeError, ValueError):
            page, page_size = 1, 100
        count_row = self._query(
            self._MEDIA_INVENTORY_EVIDENCE_CTE +
            " SELECT COUNT(*) FROM inventory_evidence" + where,
            tuple(params), one=True, default=None
        )
        total = int(count_row[0]) if count_row else 0
        rows = self._query_dicts(
            self._MEDIA_INVENTORY_EVIDENCE_CTE +
            " SELECT path, library_name, rating_key, title, year, resolution, hdr, "
            "hdr10plus_state, dv_layer, dv_profile, scan_state, sig_mtime, sig_size, "
            "scan_run_uuid, last_scanned_at, updated_at, seed_layer, scan_layer, discrepancy "
            "FROM inventory_evidence" + where +
            f" ORDER BY {order}, path ASC LIMIT ? OFFSET ?",
            tuple(params + [page_size, (page - 1) * page_size]),
            default=[],
        )
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def list_metadata_discrepancies(self, run_uuid=None):
        """Return seed/live disagreements and unverified historic seed rows."""
        clauses = ["discrepancy NOT IN ('none', 'verified', 'live_only')"]
        params = []
        if run_uuid:
            clauses.append("scan_run_uuid = ?")
            params.append(run_uuid)
        return self._query_dicts(
            self._MEDIA_INVENTORY_EVIDENCE_CTE +
            " SELECT path, title, rating_key, seed_layer, scan_layer, discrepancy "
            "FROM inventory_evidence WHERE " + " AND ".join(clauses) +
            " ORDER BY title COLLATE NOCASE, path ASC",
            tuple(params), default=[],
        )

    def media_inventory_facets(self):
        """Return safe facet counts for the inventory filter controls."""
        facets = {}
        for column in ("library_name", "resolution", "hdr", "hdr10plus_state",
                       "dv_layer", "dv_profile", "scan_state"):
            rows = self._query_dicts(
                self._MEDIA_INVENTORY_EVIDENCE_CTE +
                f" SELECT {column} AS value, COUNT(*) AS count FROM inventory_evidence "
                f"WHERE {column} IS NOT NULL AND {column} != '' GROUP BY {column} "
                "ORDER BY value COLLATE NOCASE",
                default=[],
            )
            facets[column] = rows
        facets["discrepancy"] = self._query_dicts(
            self._MEDIA_INVENTORY_EVIDENCE_CTE +
            " SELECT discrepancy AS value, COUNT(*) AS count FROM inventory_evidence "
            "GROUP BY discrepancy ORDER BY value COLLATE NOCASE",
            default=[],
        )
        return facets

    def upsert_dv_scan(self, path, dv_layer, *, title=None, sig_mtime=None,
                       sig_size=None, source="scan", rating_key=None, imdb_id=None,
                       observed=True):
        """Insert/update a DV-layer record for ``path``. Refreshes last_seen_at;
        preserves scanned_at on update. Returns True on success.

        A dv_layer of 'unknown' records that detection FAILED (dv_detect
        resolves every error to it). It must not destroy a known-good layer:
        the sig columns still take the incoming values, so the NULL signature
        a failed host scan writes keeps forcing a retry on the next run, but
        the last real finding survives to keep the Kometa labels correct in
        the meantime. Same preserve-on-worse rule the title/rating_key/imdb_id
        COALESCEs in this statement already apply.

        ``observed=False`` marks a write that LOOKED AT NO FILE — currently the
        label sync's rating_key back-write, which annotates a row from Plex and
        never touches the media. Such a write must not claim freshness:

        * ``last_seen_at`` means "when a scanner last saw this file". The
          scheduled sync gates itself on MAX(last_seen_at) for source='scan'
          (see get_latest_dv_scan_at) and records a PRE-sync watermark, so a
          sync that bumped this column re-armed its own trigger and ran a full
          library pass EVERY hour forever — 11 runs in 14 hours against a
          detector that produces new rows about twice a day, which is exactly
          the "pure waste" the gate exists to prevent.
        * the sig columns are the change-signal. Taking a caller's NULLs here
          is deliberate for a FAILED host scan (above), but the back-write
          passes no signature at all and would blank a healthy row, making
          dv_scan_is_current permanently False for it.

        Both are preserved when observed is False; everything the caller
        genuinely supplies (rating_key, layer, title) still applies."""
        if not path:
            return False
        obs = 1 if observed else 0
        return self._mutate('''
            INSERT INTO dv_scan
                (path, title, dv_layer, sig_mtime, sig_size, source,
                 rating_key, imdb_id, scanned_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                title = COALESCE(excluded.title, dv_scan.title),
                dv_layer = CASE
                    WHEN excluded.dv_layer = 'unknown'
                     AND dv_scan.dv_layer IS NOT NULL
                     AND dv_scan.dv_layer != 'unknown'
                    THEN dv_scan.dv_layer
                    ELSE excluded.dv_layer
                END,
                sig_mtime = CASE WHEN ? = 1 THEN excluded.sig_mtime
                                 ELSE dv_scan.sig_mtime END,
                sig_size = CASE WHEN ? = 1 THEN excluded.sig_size
                                ELSE dv_scan.sig_size END,
                source = excluded.source,
                rating_key = COALESCE(excluded.rating_key, dv_scan.rating_key),
                imdb_id = COALESCE(excluded.imdb_id, dv_scan.imdb_id),
                last_seen_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP
                                    ELSE dv_scan.last_seen_at END
        ''', (path, title, dv_layer, sig_mtime, sig_size, source,
              rating_key, imdb_id, obs, obs, obs), label="upsert_dv_scan")

    def annotate_dv_scan_rating_key(self, path, rating_key):
        """Attach a Plex rating_key to an EXISTING dv_scan row. Nothing else.

        The label sync is a CONSUMER of scan observations, not a producer of
        them, and this is the only write it is entitled to make. Authority
        splits cleanly:

            detector / import : dv_layer, signature, observation freshness
            Plex labeler      : Plex identity, i.e. this column

        Using upsert_dv_scan for this was wrong even with observed=False. The
        sync snapshots path->layer once at the start and back-writes that
        SNAPSHOT later, so a detector import landing in between was partially
        overwritten (peer review 2026-08-15):

            T0  sync snapshots  P = FEL / sig1 / t0
            T1  detector writes P = MEL / sig2 / t1
            T2  sync annotates  P with the stale FEL

        leaving dv_layer=FEL beside signature=sig2 and last_seen_at=t1 --
        contradictory evidence, with a consumer having erased part of a newer
        producer observation. Preserving the timestamp and signature actually
        SHARPENED that contradiction, which is why the annotation had to become
        UPDATE-only rather than merely gentler.

        UPDATE-only also means it never inserts: a row that no producer has
        written is not something the labeler may create, and silently inserting
        one would invent an observation with no layer and no signature.

        Returns True only if a row was ACTUALLY updated. _mutate reports
        statement success, which an UPDATE matching zero rows also satisfies --
        so it cannot answer "did this path exist", and callers that need to know
        an annotation landed would silently believe it had.
        """
        if not path or rating_key is None:
            return False
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                cur = conn.execute(
                    'UPDATE dv_scan SET rating_key = ? WHERE path = ?',
                    (str(rating_key), path))
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.error("DB Error (annotate_dv_scan_rating_key): %s", e)
            return False

    # --- queue attempt history (append-only) ---------------------------------

    @staticmethod
    def _attempt_stamp(when=None):
        """The ONE timestamp format the attempts table may hold.

        `datetime.now()` -- what this table used until 2026-08-16 -- is naive
        LOCAL time, and every window over this table compares it against
        sqlite's `datetime('now')`, which is UTC. On the production host that is
        a 4-hour skew, and the separator differs too ('T' vs ' '), so the
        comparison does not even fail in one consistent direction: a same-day
        row sorts AFTER any same-day UTC cutoff because 'T' > ' '.

        What it cost: _scope_is_earned asks distinct_items_failing for the same
        failure on 2 distinct items within 3600s. A row written seconds earlier
        never matched that window, so the answer was permanently 0, so an
        ambiguous reveal stall was ALWAYS treated as item-local -- and the
        item-local path was itself raising on a CHECK constraint. Two bugs, each
        of which hid the other.

        Format is sqlite's own so the existing `datetime('now', ?)` comparisons
        are correct as written. Rows from before this fix stay in the old shape;
        they carry reason_code 'attempt_not_closed', match no structural or
        source reason, and age out of every window within 24h.
        """
        when = when or datetime.datetime.now(datetime.timezone.utc)
        if isinstance(when, str):
            return when
        if when.tzinfo is not None:
            when = when.astimezone(datetime.timezone.utc)
        return when.strftime("%Y-%m-%d %H:%M:%S")

    def begin_queue_attempt(self, attempt_id, item_uuid, batch_uuid, source,
                            started_at=None):
        """Open an attempt row BEFORE the work starts. Returns True on success.

        ``started_at`` lets the caller supply its OWN clock. The queue's clock
        is injectable and its integration tests advance it; if this row were
        stamped from the real clock while the pacing window was computed from
        the injected one, the gate would be unfalsifiable in exactly the tests
        written to falsify it.

        Opened first and closed in a finally, so an attempt that never returns
        leaves an IN_PROGRESS row behind. That row IS the evidence a blocked
        worker cannot otherwise produce: the 2026-08-13 incident could not
        distinguish "attempted repeatedly, all failed" from "never attempted",
        because only current state was durable and both look like
        waiting_source after a restart.
        """
        if not attempt_id or not item_uuid:
            return False
        return self._mutate(
            "INSERT INTO download_queue_attempts "
            "(attempt_id, item_uuid, batch_uuid, source, started_at, terminal_status) "
            "VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS')",
            (str(attempt_id), str(item_uuid), str(batch_uuid or ""),
             str(source or ""), self._attempt_stamp(started_at)),
            label="begin_queue_attempt")

    def close_queue_attempt(self, attempt_id, terminal_status, *, reason_code=None,
                            affected_scope=None, transport_attempted=False,
                            source_progress=False, only_if_open=False):
        """Close an attempt with a POSITIVELY evidenced terminal state.

        terminal_status is one of SUCCESS / EXPECTED_EMPTY / FAILED /
        INTENTIONALLY_SKIPPED. Silence is never success: an attempt left open
        stays IN_PROGRESS and ages into stale_queue_attempts().

        ``transport_attempted`` must reflect whether a request actually reached
        the source. A policy deferral -- a sibling parked because some OTHER
        item hit a source-wide outcome -- is INTENTIONALLY_SKIPPED with
        transport_attempted False, and must never be counted as an observed
        source failure.

        ``only_if_open`` is for the caller's finally-backstop: it closes an
        attempt ONLY if it is still IN_PROGRESS, so a backstop that always runs
        cannot overwrite the real outcome recorded moments earlier. Without it
        every successful attempt would be rewritten to FAILED by its own
        cleanup.
        """
        allowed = ("SUCCESS", "EXPECTED_EMPTY", "FAILED", "INTENTIONALLY_SKIPPED")
        if terminal_status not in allowed:
            logger.error("close_queue_attempt: refusing unknown terminal status %r "
                         "(expected one of %s)", terminal_status, allowed)
            return False
        sql = ("UPDATE download_queue_attempts SET finished_at = ?, terminal_status = ?, "
               "reason_code = ?, affected_scope = ?, transport_attempted = ?, "
               "source_progress = ? WHERE attempt_id = ?")
        if only_if_open:
            sql += " AND terminal_status = 'IN_PROGRESS'"
        return self._mutate(
            sql,
            (self._attempt_stamp(), terminal_status,
             reason_code, affected_scope, 1 if transport_attempted else 0,
             1 if source_progress else 0, str(attempt_id)),
            label="close_queue_attempt")

    def stale_queue_attempts(self, older_than_seconds=1800):
        """Attempts still IN_PROGRESS past their deadline.

        A non-empty result means a worker started something and never finished
        it -- blocked, killed, or crashed. That is the state no amount of
        current-state inspection reveals, and the one the 48-hour gap needed.
        """
        return self._query_dicts(
            "SELECT attempt_id, item_uuid, batch_uuid, source, started_at "
            "FROM download_queue_attempts WHERE terminal_status = 'IN_PROGRESS' "
            "AND started_at < datetime('now', ?) ORDER BY started_at",
            ("-%d seconds" % int(older_than_seconds),), default=[])

    def distinct_items_failing(self, source, reason_code, within_seconds=3600,
                               now=None, including_item=None):
        """How many DISTINCT items hit `reason_code` on `source` recently.

        The promotion evidence for source-wide scope. "Scope must be earned by
        evidence" (design review): one item failing proves something about that
        item; several DIFFERENT items failing the same way in a window is what
        suggests the source itself is the problem.

        DISTINCT is the load-bearing word. Retrying one stubborn page ten times
        must not manufacture ten pieces of evidence -- that is how a single bad
        release convinces the system an entire source is refusing.

        Counts only transport_attempted=1: a sibling parked by policy never
        asked the source anything and is not evidence about it.

        ``now`` is the CALLER'S clock. The queue stamps these rows from its own
        injectable clock, so a cutoff taken from sqlite's `datetime('now')`
        here would be comparing two different clocks -- which is precisely the
        defect this window already suffered in the other direction. Defaults to
        real UTC for the monitoring callers, which have no injected clock.

        ``including_item`` is the item being classified RIGHT NOW. Its attempt
        is still open -- it has no reason_code yet, because the reason is what
        the caller is currently deciding about -- so without this it is invisible
        to its own promotion check and the constant means N+1 items, not N. That
        made AMBIGUOUS_PROMOTION_DISTINCT_ITEMS = 2 require three stalls.
        """
        cutoff = self._attempt_stamp(
            (now or datetime.datetime.now(datetime.timezone.utc))
            - datetime.timedelta(seconds=int(within_seconds)))
        rows = self._query_dicts(
            "SELECT DISTINCT item_uuid FROM download_queue_attempts "
            "WHERE source = ? AND reason_code = ? AND transport_attempted = 1 "
            "  AND started_at > ?",
            (str(source), str(reason_code), cutoff),
            default=[])
        seen = {str(r.get("item_uuid")) for r in rows}
        if including_item:
            seen.add(str(including_item))
        return len(seen)

    #: Distinct items failing structurally within the window before it reads as
    #: scraper drift rather than bad individual releases. Three, because one or
    #: two pulled releases are ordinary and a genuine template change breaks
    #: everything at once.
    SCRAPER_DRIFT_DISTINCT_ITEMS = 3
    #: Structural failures: the page did not look the way the scraper expects.
    #: Explicitly NOT source gating -- a changed template and a blocked source
    #: need opposite responses (fix the selector vs. back off), and today they
    #: are indistinguishable in the UI.
    _STRUCTURAL_REASONS = ("layout_changed", "reveal_control_absent")

    def scraper_drift_report(self, within_seconds=86400):
        """Structural scrape failures, surfaced APART from source gating.

        Design review F10. Seven items were cancelled for `layout_changed` and
        sat in the same bucket as "the source blocked us" -- but a broken
        selector and a hostile source want opposite responses, and drift
        absorbed into a gating bucket is how a scraper stays broken for weeks.

        Counts DISTINCT items with transport_attempted = 1: a page we never
        fetched says nothing about the template, and one stubborn release must
        not look like a site-wide redesign.
        """
        out = {"drifting": False, "by_reason": {}, "distinct_items": 0}
        try:
            # One canonical cutoff string, in the attempts table's own shape --
            # never a bare datetime('now', ?), which is a DIFFERENT shape from
            # what _attempt_stamp writes and compares wrong on the same day.
            cutoff = self._attempt_stamp(
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(seconds=int(within_seconds)))
            rows = self._query_dicts(
                "SELECT reason_code, COUNT(DISTINCT item_uuid) AS n "
                "FROM download_queue_attempts "
                "WHERE reason_code IN (%s) AND transport_attempted = 1 "
                "  AND started_at > ? "
                "GROUP BY reason_code"
                % ",".join("?" for _ in self._STRUCTURAL_REASONS),
                tuple(self._STRUCTURAL_REASONS) + (cutoff,),
                default=[])
            for r in rows:
                out["by_reason"][str(r.get("reason_code"))] = int(r.get("n") or 0)
            # GLOBALLY distinct, not the sum of the per-reason counts. Summing
            # them double-counts an item that failed once with layout_changed
            # and once with reveal_control_absent, so ONE stubborn release could
            # contribute 2 toward a threshold documented as three DISTINCT items
            # -- the exact "one page manufactures its own evidence" failure the
            # DISTINCT was there to prevent. by_reason stays per-reason; only the
            # threshold input changes.
            total = self._query_dicts(
                "SELECT COUNT(DISTINCT item_uuid) AS n "
                "FROM download_queue_attempts "
                "WHERE reason_code IN (%s) AND transport_attempted = 1 "
                "  AND started_at > ?"
                % ",".join("?" for _ in self._STRUCTURAL_REASONS),
                tuple(self._STRUCTURAL_REASONS) + (cutoff,),
                default=[])
            out["distinct_items"] = int((total[0] if total else {}).get("n") or 0)
            out["drifting"] = out["distinct_items"] >= self.SCRAPER_DRIFT_DISTINCT_ITEMS
        except Exception as e:  # noqa: BLE001
            logger.error("scraper_drift_report failed: %s", e)
            out["error"] = str(e)[:120]
        return out

    def queue_source_observations(self, source, within_seconds=86400):
        """OBSERVED source outcomes only -- never policy deferrals.

        Returns {attempted, failed, progressed, last_attempt_at,
        last_progress_at}. Only rows with transport_attempted = 1 count, because
        a source-health classifier that consumes synthetic sibling deferrals
        will conclude a source is refusing when it was asked exactly once.
        """
        row = self._query_dicts(
            "SELECT COUNT(*) AS attempted, "
            "       SUM(CASE WHEN terminal_status = 'FAILED' THEN 1 ELSE 0 END) AS failed, "
            "       SUM(source_progress) AS progressed, "
            "       MAX(started_at) AS last_attempt_at, "
            "       MAX(CASE WHEN source_progress = 1 THEN started_at END) AS last_progress_at "
            "FROM download_queue_attempts "
            "WHERE source = ? AND transport_attempted = 1 "
            "  AND started_at > datetime('now', ?)",
            (str(source), "-%d seconds" % int(within_seconds)), default=[])
        out = row[0] if row else {}
        return {
            "attempted": int(out.get("attempted") or 0),
            "failed": int(out.get("failed") or 0),
            "progressed": int(out.get("progressed") or 0),
            "last_attempt_at": out.get("last_attempt_at"),
            "last_progress_at": out.get("last_progress_at"),
        }

    #: Grace after an item becomes due before "nothing started" is a fault.
    #: Generous: the worker polls every couple of seconds, so 15 minutes of a
    #: due item with no attempt is not scheduling jitter.
    QUEUE_EXECUTOR_GRACE_SECONDS = 900
    #: Floor for the no-progress deadline, and the multiple of the pacing
    #: interval above it. At 600s pacing this is 2h; at 3600s it is 6h. The
    #: multiplier means the deadline scales with how slowly we deliberately
    #: chose to go, instead of a hardcoded number that is wrong at both ends.
    QUEUE_PROGRESS_FLOOR_SECONDS = 7200
    QUEUE_PROGRESS_INTERVAL_MULTIPLE = 6

    def queue_stall_report(self):
        """Three DISTINCT stall conditions, because one timer cannot separate
        the two histories the 2026-08-13 incident could not distinguish.

        "No completion in N hours" conflates "nothing was attempted" with
        "everything attempted failed" -- and those want different diagnoses.
        So this reports:

          executor_starved   work is due, and nothing has even STARTED.
                             A scheduler/ownership/liveness fault.
          source_no_progress attempts are happening, but the source has
                             delivered nothing for longer than the pacing
                             justifies. A source fault.
          human_required     a state no automatic action can leave:
                             verification hold, or deferred work with
                             auto-resume switched off.

        A verification hold is reported as human_required and NEVER as a
        scheduler stall -- mislabelling it would send someone hunting a worker
        bug when the truth is that a person must complete a challenge.

        Completion is deliberately NOT the progress signal: a queue can make
        real source progress without an item completing, and an item can
        complete without any new source reveal.
        """
        now_expr = "datetime('now')"
        report = {"executor_starved": False, "source_no_progress": False,
                  "human_required": False, "evidence": {}}
        try:
            # EVERY TIMESTAMP PREDICATE IN THIS REPORT GOES THROUGH julianday().
            #
            # This one decided whether ANY work is due, and it gates the whole
            # starvation branch below. scheduled_for is written by
            # download_queue._iso() as "2026-08-16T09:00:00+00:00"; datetime('now')
            # is "2026-08-16 14:00:00". 'T' (0x54) sorts after ' ' (0x20), so on
            # the same calendar day the ISO string is always the larger and
            # `scheduled_for <= now` is FALSE for everything. due_now has
            # therefore read 0 for every same-day item since this report was
            # written, which is why the stall detector it was built for could
            # never fire -- three separate predicates here had the same defect.
            #
            # julianday() parses both shapes to a number. The rule for this file:
            # if two timestamps meet in SQL and they might not share a shape,
            # they meet inside julianday().
            due = self._query_dicts(
                "SELECT COUNT(*) AS n, "
                "       (SELECT scheduled_for FROM download_queue_items "
                "        WHERE state IN ('scheduled','ready') "
                "          AND scheduled_for IS NOT NULL "
                f"          AND julianday(scheduled_for) <= julianday({now_expr}) "
                "        ORDER BY julianday(scheduled_for) LIMIT 1) AS oldest "
                "FROM download_queue_items "
                "WHERE state IN ('scheduled','ready') AND scheduled_for IS NOT NULL "
                f"  AND julianday(scheduled_for) <= julianday({now_expr})",
                default=[])
            due_n = int((due[0] if due else {}).get("n") or 0)
            oldest_due = (due[0] if due else {}).get("oldest")

            # By TIME, not by spelling: this column still holds pre-2026-08-16
            # rows in the old ISO shape beside the canonical one, and a lexical
            # MAX() would hand back a stale legacy row as "most recent".
            last_attempt = self._query_dicts(
                "SELECT started_at AS t FROM download_queue_attempts "
                "ORDER BY julianday(started_at) DESC LIMIT 1", default=[])
            last_attempt_at = (last_attempt[0] if last_attempt else {}).get("t")

            held = self._query_dicts(
                "SELECT COUNT(*) AS n FROM download_queue_batches "
                "WHERE verification_hold_source IS NOT NULL "
                "  AND verification_hold_source <> ''", default=[])
            held_n = int((held[0] if held else {}).get("n") or 0)

            stuck = self._query_dicts(
                "SELECT COUNT(*) AS n FROM download_queue_batches b "
                "WHERE b.auto_resume_after_cooldown = 0 AND EXISTS ("
                "  SELECT 1 FROM download_queue_items i WHERE i.batch_uuid = b.batch_uuid"
                "    AND i.state IN ('waiting_source','verification_required'))",
                default=[])
            stuck_n = int((stuck[0] if stuck else {}).get("n") or 0)

            # Scraper drift is reported ALONGSIDE, never folded into the
            # source buckets: a broken selector and a hostile source need
            # opposite responses (design review F10).
            drift = self.scraper_drift_report()
            report["scraper_drift"] = drift
            report["human_required"] = bool(held_n or stuck_n or drift.get("drifting"))
            report["evidence"] = {
                "due_now": due_n, "oldest_due_at": oldest_due,
                "last_attempt_at": last_attempt_at,
                "verification_holds": held_n,
                "batches_deferred_without_auto_resume": stuck_n,
            }

            # 1. EXECUTOR STARVATION -- work is due and nothing has started.
            #    Suppressed while a verification hold is active: not attempting
            #    is then CORRECT, and calling it a scheduler fault would send
            #    someone after the wrong bug.
            if due_n and not held_n and oldest_due:
                # julianday() ON BOTH SIDES, and it is not a style preference.
                #
                # This compared timestamp STRINGS in two different shapes:
                # `oldest_due` comes from download_queue_items.scheduled_for,
                # written by download_queue._iso() as "2026-08-16T09:00:00+00:00",
                # while datetime('now') and download_queue_attempts.started_at are
                # "2026-08-16 14:00:00". 'T' (0x54) sorts after ' ' (0x20), so on
                # the SAME calendar day the ISO string always compares as the
                # larger one whatever the real times are.
                #
                # Both halves were therefore wrong. `oldest_due < cutoff` was
                # false all day, so THIS ALERT COULD NEVER FIRE -- present on main
                # since the alert was written, which is why the 2026-08-13
                # starvation it was built for stayed invisible. And an attempt
                # that really did start hours after the item came due compared as
                # "has not started", so once the first half was fixed the second
                # would have reported starvation while work was running.
                #
                # julianday() parses both shapes to a number, so the comparison is
                # about time again rather than about ASCII.
                starved = self._query_dicts(
                    "SELECT 1 AS x WHERE julianday(?) < julianday(datetime('now', ?)) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM download_queue_attempts "
                    "  WHERE julianday(started_at) > julianday(?))",
                    (oldest_due, "-%d seconds" % self.QUEUE_EXECUTOR_GRACE_SECONDS,
                     oldest_due), default=[])
                report["executor_starved"] = bool(starved)

            # 2. SOURCE NO PROGRESS -- attempts happen, nothing comes back.
            eligible = self._query_dicts(
                "SELECT COUNT(*) AS n FROM download_queue_items "
                "WHERE state IN ('scheduled','ready','claimed','waiting_source')",
                default=[])
            if int((eligible[0] if eligible else {}).get("n") or 0) and not held_n:
                pace = self._query_dicts(
                    "SELECT MAX(interval_seconds) AS s FROM download_queue_batches "
                    "WHERE state NOT IN ('completed','cancelled')", default=[])
                interval = int((pace[0] if pace else {}).get("s") or 600)
                deadline = max(self.QUEUE_PROGRESS_FLOOR_SECONDS,
                               interval * self.QUEUE_PROGRESS_INTERVAL_MULTIPLE)
                # MAX() BY TIME, NOT BY SPELLING. A bare MAX(started_at) is a
                # lexical max, and this column still holds rows written before
                # 2026-08-16 in the old "2026-08-16T03:51:41" shape alongside the
                # canonical "2026-08-16 03:51:41". 'T' sorts after ' ', so a
                # stale legacy row wins MAX() against every same-day real one --
                # and this value is what decides whether the source is declared
                # dead. Ordering by julianday picks the genuinely newest whatever
                # shape it is, so the legacy rows are harmless rather than
                # actively misleading.
                prog = self._query_dicts(
                    "SELECT started_at AS t FROM download_queue_attempts "
                    "WHERE source_progress = 1 "
                    "ORDER BY julianday(started_at) DESC LIMIT 1", default=[])
                last_progress = (prog[0] if prog else {}).get("t")
                report["evidence"]["last_source_progress_at"] = last_progress
                report["evidence"]["progress_deadline_seconds"] = deadline

                # A NO-PROGRESS EPISODE, NOT "ANY ATTEMPT EVER PLUS THE EPOCH".
                #
                # This used to read: if any attempt row exists at all, is
                # COALESCE(last_progress, '1970-01-01') older than the deadline?
                # With no progress ever recorded the fallback is the epoch, which
                # is older than every conceivable deadline -- so THE FIRST FAILED
                # ATTEMPT IN A FRESH HISTORY set source_no_progress immediately,
                # flatly contradicting the contract this key states ("attempts
                # are happening, but the source has delivered nothing for longer
                # than the pacing justifies"). And because `last_attempt_at` was
                # tested only for EXISTENCE, a months-old attempt satisfied it
                # during a current starvation, so executor_starved and
                # source_no_progress could both be true at once -- the two
                # diagnoses this report exists to keep apart. (2026-08-16 peer
                # review round 2.)
                #
                # The episode is defined positively instead:
                #   start   the EARLIEST source-spending attempt since the last
                #           delivery (or ever, if the source has never delivered)
                #   open    that start is older than the deadline
                #   live    we are still ASKING -- a source-spending attempt
                #           inside the deadline window. Without this, "we gave up
                #           hours ago" would read as a source fault when it is a
                #           scheduler one.
                # Only transport_attempted = 1 counts: a policy deferral never
                # asked the source anything and is not evidence about it.
                window = "-%d seconds" % deadline
                episode = self._query_dicts(
                    "SELECT started_at AS t FROM download_queue_attempts "
                    "WHERE transport_attempted = 1 "
                    "  AND (? IS NULL OR julianday(started_at) > julianday(?)) "
                    "ORDER BY julianday(started_at) ASC LIMIT 1",
                    (last_progress, last_progress), default=[])
                episode_start = (episode[0] if episode else {}).get("t")
                recent = self._query_dicts(
                    "SELECT 1 AS x FROM download_queue_attempts "
                    "WHERE transport_attempted = 1 "
                    "  AND julianday(started_at) >= julianday(datetime('now', ?)) "
                    "LIMIT 1", (window,), default=[])
                report["evidence"]["no_progress_episode_since"] = episode_start
                if episode_start and recent:
                    open_long_enough = self._query_dicts(
                        "SELECT 1 AS x WHERE julianday(?) "
                        "  < julianday(datetime('now', ?))",
                        (episode_start, window), default=[])
                    report["source_no_progress"] = bool(open_long_enough)
        except Exception as e:  # noqa: BLE001
            # A health report that throws must not take its caller down, but it
            # must not read as healthy either.
            logger.error("queue_stall_report failed: %s", e)
            report["evidence"]["error"] = str(e)[:120]
            report["human_required"] = True
        return report

    def get_latest_plex_cache_at(self, content_type="Movies"):
        """Newest ``last_updated`` among plex_cache rows for *content_type*,
        else None.
        
        Change-detector for the scheduled version-label sync, mirroring
        ``get_latest_dv_scan_at``. A higher value means the Plex cache was
        rewritten, so the per-title file counts the badges are derived from may
        have moved.
        
        SAFE TO ``MAX()`` HERE, which is not true of every timestamp in this
        schema: ``plex_cache.last_updated`` is a FLOAT epoch, so the comparison
        is numeric. The string-shaped columns elsewhere mix `T`-separated and
        space-separated forms, where `MAX()` compares lexically and can order
        same-day values backwards. Measured: all 16,332 Movies rows carry one
        identical float, because the cache is rewritten wholesale each refresh --
        so this advances exactly once per refresh rather than per row.
        
        Fail-safe: any error returns None, which the caller reads as "nothing
        new", so a DB hiccup can never trigger a spurious full-library pass.
        """
        try:
            rows = self._query_dicts(
                "SELECT MAX(last_updated) AS latest FROM plex_cache "
                "WHERE content_type = ?", (content_type,))
            return (rows[0].get("latest") if rows else None) or None
        except Exception as e:
            logger.error("DB Error (get_latest_plex_cache_at): %s", e)
            return None

    def get_latest_dv_scan_at(self, source="scan"):
        """Newest ``last_seen_at`` among dv_scan rows for *source*, else None.

        Cheap change-detector for the scheduled DV label sync: a value higher
        than the last one seen means fresh DV detections landed or an existing
        file's layer changed, so a sync is worth running. Fail-safe — any error
        returns
        None, which the caller reads as "nothing new", so a DB hiccup can never
        trigger a spurious full-library label pass.
        """
        try:
            rows = self._query_dicts(
                'SELECT MAX(last_seen_at) AS latest FROM dv_scan WHERE source = ?',
                (source,))
            return (rows[0].get("latest") if rows else None) or None
        except Exception as e:
            logger.error("DB Error (get_latest_dv_scan_at): %s", e)
            return None

    def get_dv_scan(self, path):
        """Return the DV-scan row for ``path`` (dict) or None."""
        rows = self._query_dicts(
            'SELECT path, title, dv_layer, sig_mtime, sig_size, source, '
            'rating_key, imdb_id, scanned_at, last_seen_at '
            'FROM dv_scan WHERE path = ?', (path,))
        return rows[0] if rows else None

    def get_dv_scans_by_paths(self, paths):
        """Return a ``{path: row_dict}`` map for all *paths* found in dv_scan.

        Runs a single parameterised ``IN`` query instead of one call per path.
        An empty/falsy *paths* input returns ``{}`` without touching the DB.
        Fail-safe: returns ``{}`` on any error (mirrors the single-row helpers).
        """
        if not paths:
            return {}
        try:
            placeholders = ",".join("?" * len(paths))
            rows = self._query_dicts(
                f'SELECT path, title, dv_layer, sig_mtime, sig_size, source, '
                f'rating_key, imdb_id, scanned_at, last_seen_at '
                f'FROM dv_scan WHERE path IN ({placeholders})',
                tuple(paths))
            return {row["path"]: row for row in (rows or [])}
        except Exception as e:
            logger.error("get_dv_scans_by_paths error: %s", e)
            return {}

    def get_dv_scans(self, dv_layer=None, limit=100000, source=None):
        """Return DV-scan rows, optionally filtered by layer and/or source.

        ``source`` (e.g. 'scan') restricts the list to that origin, so the DV
        panel can show real detected rows instead of dead seed rows.
        """
        clauses = []
        params = []
        if dv_layer:
            clauses.append("dv_layer = ?")
            params.append(dv_layer)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self._query_dicts(
            'SELECT path, title, dv_layer, rating_key, imdb_id, '
            'scanned_at, last_seen_at FROM dv_scan'
            f'{where} ORDER BY last_seen_at DESC LIMIT ?', tuple(params), default=[])

    def get_dv_layer_rows(self, source="scan"):
        """Just ``path`` + ``dv_layer`` for every row of *source*. No limit.

        The DV conflict state is a property of ALL scan rows -- a page cannot
        answer it -- but computing it needs only these two columns. get_dv_scans
        returns seven and is paged for the inventory view, so this exists to let
        the conflict endpoint be refreshed cheaply on reconnect and panel-open
        without dragging the inventory payload along (peer review round 3:
        prefer query shape over caching, because a cache would put an
        invalidation problem into state whose best property is that it can
        always be recomputed).
        """
        return self._query_dicts(
            "SELECT path, dv_layer FROM dv_scan WHERE source = ?",
            (source,), default=[])

    def get_plex_hdr_by_rating_key(self):
        """``{rating_key: bool}`` — whether Plex sees wide-gamut video.

        The HDR10 label needs an HDR axis, and dv_scan has none: 'none' means
        "dovi_tool found no Dolby Vision", which is equally true of an HDR10
        remux and a plain SDR 4K file. Plex already records the distinction
        (plex_service sets ``hdr`` when a video stream's colorPrimaries carry
        bt2020), so the labeler joins on that rather than inventing a second
        detector.

        A rating_key ABSENT from this map is UNKNOWN, not False. The two must
        stay distinguishable: treating "no cached row" as "not HDR" would let a
        cache gap strip a correct HDR10 label, which is the same shape as every
        other silent-removal bug in this module. Callers get a plain dict, so
        ``.get(rk)`` returns None for unknown.

        Note plex_service stops scanning streams at the first Dolby Vision hit,
        so a DV title records hdr=0. That is correct for this consumer: HDR10
        here means "HDR and no DV", which is exactly what the label says.

        AGGREGATION IS EXPLICIT, and it has to be. plex_cache holds one row per
        media PART/version, so a title with several versions has several rows:
        1,032 rating_keys have more than one row here, and 225 of those have
        rows that DISAGREE about hdr (a 4K HDR version beside a 4K SDR one).
        A plain dict comprehension over the rows lets whichever duplicate SQLite
        happened to return last decide the title, which is nondeterministic --
        and a False is destructive, because it authorises removing HDR10.

        MAX(hdr) encodes the intended rule: **any served version being HDR makes
        the title HDR**. That matches the label's meaning ("this title is
        available in HDR") and fails in the safe direction, since the failure
        mode that matters is wrongly concluding "not HDR" and stripping a
        correct label. Found in peer review 2026-08-15.
        """
        rows = self._query_dicts(
            'SELECT rating_key, MAX(hdr) AS hdr FROM plex_cache '
            'WHERE rating_key IS NOT NULL AND hdr IS NOT NULL '
            'GROUP BY rating_key', default=[])
        return {str(r["rating_key"]): bool(r["hdr"]) for r in rows}

    def count_dv_scans_by_layer(self, source=None):
        """Return ``{layer: count}`` over the dv_scan table.

        ``source`` (e.g. 'scan') restricts the count to that origin, so the DV
        panel can show real detected counts instead of dead seed rows.
        """
        if source is not None:
            rows = self._query(
                'SELECT dv_layer, COUNT(*) FROM dv_scan WHERE source = ? '
                'GROUP BY dv_layer', (source,), default=[])
        else:
            rows = self._query(
                'SELECT dv_layer, COUNT(*) FROM dv_scan GROUP BY dv_layer', default=[])
        return {r[0]: r[1] for r in (rows or [])}

    def dv_scan_is_current(self, path, sig_mtime, sig_size):
        """Whether ``path`` is already scanned with a matching change-signal, so an
        expensive RPU re-scan can skip it. A None stored signature never matches
        (forces a scan).

        Size must match exactly; mtime is matched within 1s to absorb filesystem
        mtime-granularity differences (FAT 2s, some network mounts 1s) that would
        otherwise force needless re-scans of unchanged files. Size is the primary
        guard — an in-place re-rip changes the byte count, so the 1s mtime slack
        can't mask a real content change."""
        row = self.get_dv_scan(path)
        if not row or row.get("sig_mtime") is None or row.get("sig_size") is None:
            return False
        try:
            return (abs(float(row["sig_mtime"]) - float(sig_mtime)) < 1.0
                    and int(row["sig_size"]) == int(sig_size))
        except (TypeError, ValueError):
            return False

    def clear_dv_scans(self):
        """Remove all DV-scan rows (test/maintenance helper)."""
        return self._mutate('DELETE FROM dv_scan', label="clear_dv_scans")

    # ── ffprobe result cache (media_probe) ─────────────────────────────

    def upsert_media_probe(self, path, probe_json, *, sig_mtime=None, sig_size=None):
        """Insert/update the cached ffprobe result for ``path``. Returns True on success."""
        if not path:
            return False
        return self._mutate('''
            INSERT INTO media_probe (path, sig_mtime, sig_size, probe_json, probed_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                sig_mtime = excluded.sig_mtime,
                sig_size = excluded.sig_size,
                probe_json = excluded.probe_json,
                probed_at = CURRENT_TIMESTAMP
        ''', (path, sig_mtime, sig_size, probe_json), label="upsert_media_probe")

    def get_media_probe(self, path):
        """Return the cached probe row for ``path`` (dict, probe_json still a raw
        JSON string) or None."""
        rows = self._query_dicts(
            'SELECT path, sig_mtime, sig_size, probe_json, probed_at '
            'FROM media_probe WHERE path = ?', (path,))
        return rows[0] if rows else None

    def media_probe_is_current(self, path, sig_mtime, sig_size):
        """Whether ``path``'s cached probe still matches the on-disk signature —
        mirrors dv_scan_is_current's 1s mtime tolerance / exact size match."""
        row = self.get_media_probe(path)
        if not row or row.get("sig_mtime") is None or row.get("sig_size") is None:
            return False
        try:
            return (abs(float(row["sig_mtime"]) - float(sig_mtime)) < 1.0
                    and int(row["sig_size"]) == int(sig_size))
        except (TypeError, ValueError):
            return False

    def update_background_status(self, updates):
        """Update status + data JSON for cached rows WITHOUT touching last_seen,
        so a status re-match (Plex/download re-check) doesn't reset retention.

        Args:
            updates: iterable of dicts with keys url, status, data.
        """
        rows = [u for u in updates if u.get('url')]
        if not rows:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany(
                    "UPDATE background_scan_cache SET status = :status, data = :data "
                    "WHERE url = :url",
                    [{'url': u['url'], 'status': u.get('status', ''), 'data': u.get('data')} for u in rows])
                conn.commit()
                # Bump the cache revision: this is an in-place blob mutation that
                # changes neither COUNT(*) nor MAX(last_seen_at), so without this
                # the read-side parse-cache version (get_background_cache_version)
                # would be unchanged and serve stale, pre-re-match items.
                self._bg_cache_rev += 1
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (update_background_status): %s", e)
            return False

    def get_background_cache_urls(self):
        """Return the set of URLs currently in the background cache."""
        rows = self._query("SELECT url FROM background_scan_cache WHERE COALESCE(derived_state,'current') != 'stale'", default=[])
        return {row[0] for row in rows} if rows else set()

    def touch_background_cache(self, urls):
        """Refresh ``last_seen_at`` for still-listed cached URLs without
        re-scraping them — keeps them from being purged while still on the site."""
        urls = [u for u in (urls or []) if u]
        if not urls:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany(
                    "UPDATE background_scan_cache SET last_seen_at = CURRENT_TIMESTAMP "
                    "WHERE url = ?", [(u,) for u in urls])
                conn.commit()
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (touch_background_cache): %s", e)
            return False

    def purge_background_cache(self, retain_days):
        """Delete cached rows last seen more than ``retain_days`` ago."""
        return self._mutate(
            "DELETE FROM background_scan_cache WHERE last_seen_at < datetime('now', ?)",
            (f"-{int(retain_days)} days",), label="purge_background_cache")

    def count_background_cache(self):
        """Return the number of cached background-scan rows."""
        row = self._query(
            'SELECT COUNT(*) FROM background_scan_cache', one=True, default=None)
        return row[0] if row else 0

    def get_background_cache_version(self):
        """Return a cheap, monotonic-ish ``(count, max_last_seen_at)`` tuple
        that changes whenever the background cache's row set or any row's
        ``last_seen_at`` changes (every upsert refreshes it — see
        upsert_background_cache). Callers use this as a cache-invalidation
        key for expensive per-row JSON parsing (see
        backend/api/routes/results.py) without re-reading and re-parsing
        every row on each request.
        """
        row = self._query(
            'SELECT COUNT(*), MAX(last_seen_at) FROM background_scan_cache',
            one=True, default=None)
        if not row:
            return (0, None, self._bg_cache_rev)
        return (row[0] or 0, row[1], self._bg_cache_rev)

    def clear_background_cache(self):
        """Remove all cached background-scan rows."""
        result = self._mutate(
            "DELETE FROM background_scan_cache", label="clear_background_cache")
        with self._lock:
            self._bg_cache_rev += 1
        return result

    # ── Auto-rename jobs ──────────────────────────────────────────────

    _RENAME_FIELDS = (
        "package_name", "original_path", "original_filename", "new_filename",
        "destination_path", "status", "media_type", "title", "year", "season",
        "episode", "tmdb_id", "imdb_id", "resolution", "match_confidence",
        "match_source", "move_method", "proposed_match", "plex_sort_title",
        "warning_message", "error_message", "processed_at", "reverted_at",
        "suggested_correction", "combined_episode", "split_file", "poster_path",
        "match_reasons", "prior_status", "conflict_kind", "conflict_same_size",
        "conflict_existing_size", "conflict_incoming_size", "conflict_analysis",
        "archived_at", "source_missing_since", "conflict_replaced_path",
    )

    # Fields stored as JSON TEXT in SQLite — auto-serialized/deserialized.
    _JSON_RENAME_FIELDS = frozenset({"suggested_correction", "combined_episode",
                                     "split_file", "match_reasons", "conflict_analysis"})

    def _serialize_rename_row(self, row: dict) -> dict:
        """JSON-encode dict/list values for _JSON_RENAME_FIELDS before DB write."""
        out = {}
        for k, v in row.items():
            if k in self._JSON_RENAME_FIELDS:
                out[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
            else:
                out[k] = v
        return out

    def _deserialize_rename_row(self, row: dict) -> dict:
        """JSON-decode TEXT values for _JSON_RENAME_FIELDS after DB read."""
        for field in self._JSON_RENAME_FIELDS:
            raw = row.get(field)
            if raw and isinstance(raw, str):
                try:
                    row[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    row[field] = None
        if row.get("conflict_same_size") is not None:
            row["conflict_same_size"] = bool(row["conflict_same_size"])
        return row

    def create_rename_job(self, job):
        """Insert a rename job (dict of column→value); return the new id.

        Returns None only for a malformed ``job`` (missing original_path) —
        that's a caller bug, not a DB failure. A genuine DB-layer failure
        (no connection, disk error, constraint violation, etc.) raises
        RenameJobDBError instead of returning None, so callers can tell
        "silently dropped because the DB failed" apart from a legitimate
        no-op and surface it (see RenameService._create / process_folder's
        ``failed_db`` count).
        """
        job = self._serialize_rename_row(job)
        cols = [k for k in self._RENAME_FIELDS if k in job]
        if "original_path" not in cols:
            return None
        placeholders = ", ".join(f":{c}" for c in cols)
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    raise RenameJobDBError("No database connection available")
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO rename_jobs ({', '.join(cols)}) VALUES ({placeholders})",
                    {c: job.get(c) for c in cols})
                conn.commit()
                return cur.lastrowid
        except RenameJobDBError:
            raise
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (create_rename_job): %s", e)
            raise RenameJobDBError(str(e)) from e

    def update_rename_job(self, job_id, **fields):
        """Update arbitrary columns on a rename job."""
        fields = self._serialize_rename_row(fields)
        cols = [k for k in fields if k in self._RENAME_FIELDS]
        if not cols:
            return False
        assignments = ", ".join(f"{c} = :{c}" for c in cols)
        params = {c: fields[c] for c in cols}
        params["id"] = job_id
        return self._mutate(
            f"UPDATE rename_jobs SET {assignments} WHERE id = :id",
            params, label="update_rename_job")

    def get_rename_job(self, job_id):
        """Return a rename job as a dict, or None."""
        rows = self._query_dicts(
            "SELECT * FROM rename_jobs WHERE id = ?", (job_id,), default=[])
        return self._deserialize_rename_row(rows[0]) if rows else None

    def list_rename_jobs(self, status=None, limit=200, archived=False):
        """Return rename jobs (optionally filtered by status), newest first.

        ``archived`` defaults to False so every existing/not-yet-updated
        caller keeps excluding archived rows exactly as before this column
        existed. Archiving is orthogonal to status: archived=True returns
        archived rows of ANY status when no status filter is also given.
        """
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        conditions.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
        where = " WHERE " + " AND ".join(conditions)
        params.append(limit)
        rows = self._query_dicts(
            f"SELECT * FROM rename_jobs{where} ORDER BY detected_at DESC LIMIT ?",
            tuple(params), default=[])
        return [self._deserialize_rename_row(r) for r in (rows or [])]

    def reset_applying_rename_jobs(self):
        """Reset jobs stuck in the transient 'applying' state back to 'matched'.

        'applying' is set just before a queued move runs; if the process
        crashed or the box lost power mid-apply, the job would otherwise be
        stuck there forever (queue_apply skips 'applying'). Called once at
        startup so orphaned applies become retriable again. The move itself is
        crash-safe (verified copy to a .part sidecar, atomic rename, source kept
        until verified), so re-applying is always safe. Returns the row count."""
        n = self._query(
            "SELECT COUNT(*) FROM rename_jobs WHERE status = 'applying'",
            one=True, default=[0])
        count = (n[0] if n else 0) or 0
        if count:
            # Restore the pre-apply status (needs_review stays needs_review, so a
            # human-gated match isn't silently promoted to auto-appliable);
            # fall back to 'matched' for legacy rows with no prior_status.
            self._mutate(
                "UPDATE rename_jobs SET status = COALESCE(prior_status, 'matched'), "
                "prior_status = NULL WHERE status = 'applying'",
                label="reset_applying_rename_jobs")
        return count

    def count_rename_jobs_by_status(self, include_archived=False):
        """Return a ``{status: count}`` map over rename jobs.

        Excludes archived jobs by DEFAULT, because list_rename_jobs excludes
        them by default too, and these counts label the cards above that very
        list. Counting archived rows here made every card disagree with what
        clicking it showed -- "Applied 89" opening a list of 78 -- while the
        separate Archived card counted those same jobs a second time. A
        number you cannot reconcile with the screen teaches you to distrust
        the screen.
        """
        sql = "SELECT status, COUNT(*) FROM rename_jobs"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        rows = self._query(sql + " GROUP BY status", default=[])
        return {r[0]: r[1] for r in (rows or [])}

    def package_has_rename_job(self, package_name):
        """Whether any rename job already exists for a JD package (dedup)."""
        if not package_name:
            return False
        row = self._query(
            "SELECT 1 FROM rename_jobs WHERE package_name = ? LIMIT 1",
            (package_name,), one=True, default=None)
        return row is not None

    def path_has_rename_job(self, original_path):
        """Whether a rename job already exists for a given source file — dedup for
        manual folder processing, which has no JD package name."""
        if not original_path:
            return False
        row = self._query(
            "SELECT 1 FROM rename_jobs WHERE original_path = ? LIMIT 1",
            (original_path,), one=True, default=None)
        return row is not None

    def archive_rename_jobs(self, job_ids):
        """Archive the given jobs (set archived_at to now), skipping any job
        whose status is 'applying' (the transient mid-move state) and any
        already-archived job. One in-flight job in the batch never blocks
        archiving the rest. Returns the number of rows actually archived."""
        ids = [int(i) for i in (job_ids or []) if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE rename_jobs SET archived_at = ? "
                    f"WHERE id IN ({placeholders}) AND status != 'applying' "
                    f"AND archived_at IS NULL",
                    (now, *ids))
                archived = cur.rowcount
                conn.commit()
            return archived
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (archive_rename_jobs): %s", e)
            return 0

    def unarchive_rename_jobs(self, job_ids):
        """Clear archived_at for the given jobs. Returns the number of rows
        actually unarchived."""
        ids = [int(i) for i in (job_ids or []) if i is not None]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return 0
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE rename_jobs SET archived_at = NULL "
                    f"WHERE id IN ({placeholders}) AND archived_at IS NOT NULL",
                    tuple(ids))
                unarchived = cur.rowcount
                conn.commit()
            return unarchived
        except Exception as e:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            logger.error("DB Error (unarchive_rename_jobs): %s", e)
            return 0

    def delete_rename_job(self, job_id):
        """Delete a rename job row."""
        return self._mutate(
            "DELETE FROM rename_jobs WHERE id = ?", (job_id,), label="delete_rename_job")

    def clear_rename_jobs(self):
        """Remove all rename jobs (used by tests)."""
        return self._mutate("DELETE FROM rename_jobs", label="clear_rename_jobs")

    def record_scraped_links(self, links, title, resolution="", source_url=""):
        """Map scraped file-host links to the movie/show they belong to.

        Lets a broken/blocked link in JDownloader be traced back to its title
        even when JD named the package from the filename (clipboard adds).
        """
        if not links or not title:
            return False
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany(
                    '''INSERT OR REPLACE INTO scraped_link_map (link, title, resolution, source_url)
                       VALUES (:link, :title, :resolution, :source_url)''',
                    [{"link": l, "title": title, "resolution": resolution, "source_url": source_url}
                     for l in links if l],
                )
                conn.commit()
            return True
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (record_scraped_links): %s", e)
            return False

    def get_scraped_link_titles(self) -> dict:
        """Return {link: {'title': ..., 'resolution': ...}} for JD cross-reference."""
        rows = self._query('SELECT link, title, resolution FROM scraped_link_map', default=[])
        return {row[0]: {"title": row[1], "resolution": row[2]} for row in rows}


    # ── Source health ──────────────────────────────────────────────────

    def get_source_health(self, source=None):
        """Return one health row, or all rows keyed by source."""
        if source is not None:
            row = self._query(
                "SELECT * FROM source_health WHERE source = ?",
                (source,),
                one=True,
                default=None,
            )
            return dict(row) if row is not None else None
        rows = self._query("SELECT * FROM source_health", default=[])
        return {row["source"]: dict(row) for row in rows}

    def record_source_success(self, source):
        """Mark a source healthy and clear its failure/cooldown streak."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self._mutate(
            """INSERT INTO source_health (
                   source, state, reason_code, updated_at, last_success_at,
                   last_failure_at, consecutive_failures, cooldown_until
               ) VALUES (?, 'healthy', NULL, ?, ?, NULL, 0, NULL)
               ON CONFLICT(source) DO UPDATE SET
                   state = 'healthy', reason_code = NULL, updated_at = excluded.updated_at,
                   last_success_at = excluded.last_success_at,
                   consecutive_failures = 0, cooldown_until = NULL""",
            (source, now, now),
            label="record_source_success",
        )

    def record_source_failure(
        self, source, state, reason_code, *, cooldown_seconds=None
    ):
        """Persist one health-affecting failure and increment its streak."""
        allowed = {"unknown", "healthy", "degraded", "blocked", "cooldown"}
        if state not in allowed:
            raise ValueError(f"Invalid source health state: {state}")
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now = now_dt.isoformat()
        cooldown_until = None
        if cooldown_seconds:
            cooldown_until = (
                now_dt + datetime.timedelta(seconds=max(0, int(cooldown_seconds)))
            ).isoformat()
        return self._mutate(
            """INSERT INTO source_health (
                   source, state, reason_code, updated_at, last_success_at,
                   last_failure_at, consecutive_failures, cooldown_until
               ) VALUES (?, ?, ?, ?, NULL, ?, 1, ?)
               ON CONFLICT(source) DO UPDATE SET
                   state = excluded.state,
                   reason_code = excluded.reason_code,
                   updated_at = excluded.updated_at,
                   last_failure_at = excluded.last_failure_at,
                    consecutive_failures = source_health.consecutive_failures + 1,
                    cooldown_until = COALESCE(
                        excluded.cooldown_until,
                        source_health.cooldown_until
                    )""",
            (source, state, reason_code, now, now, cooldown_until),
            label="record_source_failure",
        )

    def clear_source_health(self, source=None):
        if source is None:
            return self._mutate("DELETE FROM source_health", label="clear_source_health")
        return self._mutate(
            "DELETE FROM source_health WHERE source = ?",
            (source,),
            label="clear_source_health",
        )

# ── Startup-time corruption surfacing ─────────────────────────────────────

def corruption_flag_path(db_path: str) -> str:
    """Path to the persisted corruption marker for ``db_path`` (see
    DatabaseManager._write_corruption_flag)."""
    return f"{db_path}.corrupt_flag.json"


def db_corruption_flag_present(db_path: str) -> bool:
    """Whether an un-acknowledged corruption flag exists for ``db_path``.

    True only for the not-yet-notified flag — once notify_db_corruption_once
    renames it to .notified.json, this returns False again.
    """
    return os.path.exists(corruption_flag_path(db_path))


def notify_db_corruption_once(db_path: str, bridge) -> bool:
    """If a corruption flag exists for ``db_path``, notify once and rename it.

    Called at the END of startup (after the notification bridge exists,
    unlike DatabaseManager._notify_corruption's best-effort attempt during
    init_db, which usually fires before the bridge is wired up and is a
    bonus channel, not the primary signal). Renaming the flag to
    ``.corrupt_flag.notified.json`` after a successful notify means this
    fires exactly once per corruption event, even across many restarts,
    while still leaving a permanent on-disk record of the incident.

    Returns True if a (previously un-notified) flag was found and processed
    (regardless of whether the notification itself succeeded — the rename
    only happens if we got as far as attempting notification, so the
    "fire once" behavior holds even when the bridge silently fails).
    """
    flag_path = corruption_flag_path(db_path)
    if not os.path.exists(flag_path):
        return False
    try:
        if bridge is not None:
            bridge.notify_error(
                "Database corruption was detected and quarantined — check logs")
    except Exception:
        logger.warning("DB corruption notification failed (non-fatal)", exc_info=True)
    notified_path = f"{db_path}.corrupt_flag.notified.json"
    try:
        os.replace(flag_path, notified_path)
    except OSError:
        logger.exception("Failed to rename corruption flag to %s", notified_path)
    return True


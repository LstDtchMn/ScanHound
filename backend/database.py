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
import time
import threading
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
    SCHEMA_VERSION = 2

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
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp ON scan_history(timestamp DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_bg_cache_last_seen ON background_scan_cache(last_seen_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_dv_scan_layer ON dv_scan(dv_layer)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_status ON rename_jobs(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_detected ON rename_jobs(detected_at DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_pipeline_verdicts_category '
                               'ON pipeline_verdicts(category)')

                # ── Column migrations (guarded by "duplicate column name") ─
                _column_migrations = [
                    'ALTER TABLE downloads ADD COLUMN normalized_title TEXT',
                    'ALTER TABLE downloads ADD COLUMN season INTEGER',
                    'ALTER TABLE downloads ADD COLUMN resolution TEXT',
                    'ALTER TABLE downloads ADD COLUMN size TEXT',
                    "ALTER TABLE downloads ADD COLUMN status TEXT DEFAULT 'completed'",
                    'ALTER TABLE plex_cache ADD COLUMN library_name TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN suggested_correction TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN combined_episode TEXT',
                    'ALTER TABLE rename_jobs ADD COLUMN split_file TEXT',
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

                # ── Versioned migrations ─────────────────────────────────
                if current_version < 2:
                    # v2: Drop legacy tables from removed subsystems
                    for table in ('file_manager', 'schema_version', 'app_config'):
                        try:
                            cursor.execute(f"DROP TABLE IF EXISTS {table}")
                        except sqlite3.OperationalError:
                            pass

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

    def list_plex_cache_movies(self):
        """Return every plex_cache row for content_type='Movies' (dicts) — the
        candidate pool for find_library_duplicate()."""
        return self._query_dicts(
            "SELECT key, title, original_title, year, res, size, imdb_id, "
            "rating_key, media_id, is_tv, dovi, hdr, file_path "
            "FROM plex_cache WHERE content_type = 'Movies'", default=[])

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
                       service_type=None):
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
            INSERT INTO downloads (url, title, normalized_title, season, resolution, size, status, hdr, dovi, year, package_name, service_type, last_grabbed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
                last_grabbed_at = CURRENT_TIMESTAMP
        ''', (url, title, normalized_title, season, resolution, size, status,
              hdr or None, 1 if dovi else 0, year, package_name, service_type),
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
                   CASE
                     WHEN v.category = 'pending_rename'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('pending', 'matched', 'applying')
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category = 'rename_failed'
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status IN ('failed', 'needs_review', 'reverted')
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     WHEN v.category IN ('awaiting_plex_refresh', 'verified', 'not_in_plex')
                     THEN (SELECT r.poster_path FROM rename_jobs r
                           WHERE r.package_name = COALESCE(d.jd_confirmed_name, d.package_name)
                             AND r.status = 'applied'
                             AND r.poster_path IS NOT NULL
                           ORDER BY r.id DESC LIMIT 1)
                     ELSE NULL
                   END AS poster_path
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
                               extraction="na", state="queued", error=None):
        """Insert/update a JD package's download outcome; returns the row id (int)
        or None on failure. Identity is package_uuid when present, else the row is
        adopted-by-name (a legacy NULL-uuid row) or inserted. Runs the whole
        lookup-then-write under one lock hold to avoid poller-vs-remove races."""
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return None
                cur = conn.cursor()
                row = None
                if package_uuid is not None:
                    cur.execute("SELECT id FROM download_results WHERE package_uuid = ?",
                                (package_uuid,))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute("SELECT id FROM download_results "
                                    "WHERE package_uuid IS NULL AND name = ? "
                                    "ORDER BY updated_at DESC LIMIT 1", (name,))
                        row = cur.fetchone()
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
                        "extraction = ?, state = ?, error = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (package_uuid, name, title, host, bytes_total, bytes_loaded,
                         downloaded, extraction, state, error, rid))
                    conn.commit()
                    return rid
                cur.execute(
                    "INSERT INTO download_results (package_uuid, name, title, host, "
                    "bytes_total, bytes_loaded, downloaded, extraction, state, error, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (package_uuid, name, title, host, bytes_total, bytes_loaded,
                     downloaded, extraction, state, error))
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.error("DB Error (upsert_download_result): %s", e)
            return None

    def get_download_results(self, limit=200):
        """Return tracked download/extraction outcomes, most recent first."""
        return self._query_dicts(
            "SELECT id, package_uuid, name, title, host, bytes_total, bytes_loaded, "
            "downloaded, extraction, state, error, updated_at "
            "FROM download_results ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

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
                         scraped_at, last_seen_at)
                    VALUES
                        (:url, :title, :year, :status, :source_category, :data,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        year = excluded.year,
                        status = excluded.status,
                        source_category = COALESCE(
                            NULLIF(background_scan_cache.source_category, ''),
                            excluded.source_category),
                        data = excluded.data,
                        last_seen_at = CURRENT_TIMESTAMP
                ''', [{
                    "url": it.get("url"),
                    "title": it.get("title"),
                    "year": it.get("year"),
                    "status": it.get("status"),
                    "source_category": it.get("source_category"),
                    "data": it.get("data"),
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

    def upsert_dv_scan(self, path, dv_layer, *, title=None, sig_mtime=None,
                       sig_size=None, source="scan", rating_key=None, imdb_id=None):
        """Insert/update a DV-layer record for ``path``. Refreshes last_seen_at;
        preserves scanned_at on update. Returns True on success."""
        if not path:
            return False
        return self._mutate('''
            INSERT INTO dv_scan
                (path, title, dv_layer, sig_mtime, sig_size, source,
                 rating_key, imdb_id, scanned_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                title = COALESCE(excluded.title, dv_scan.title),
                dv_layer = excluded.dv_layer,
                sig_mtime = excluded.sig_mtime,
                sig_size = excluded.sig_size,
                source = excluded.source,
                rating_key = COALESCE(excluded.rating_key, dv_scan.rating_key),
                imdb_id = COALESCE(excluded.imdb_id, dv_scan.imdb_id),
                last_seen_at = CURRENT_TIMESTAMP
        ''', (path, title, dv_layer, sig_mtime, sig_size, source,
              rating_key, imdb_id), label="upsert_dv_scan") is not None

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
        ''', (path, sig_mtime, sig_size, probe_json), label="upsert_media_probe") is not None

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
        rows = self._query('SELECT url FROM background_scan_cache', default=[])
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
        "archived_at",
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

    def count_rename_jobs_by_status(self):
        """Return a ``{status: count}`` map over all rename jobs."""
        rows = self._query(
            "SELECT status, COUNT(*) FROM rename_jobs GROUP BY status", default=[])
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


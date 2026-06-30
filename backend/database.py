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


class DatabaseManager:
    """Thread-safe SQLite database manager with connection pooling and auto-recovery."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = None
        self._lock = threading.RLock()  # Reentrant lock for thread-safe DB access
        self._init_depth = 0  # Guard against infinite recursion during recovery
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

        Uses WAL journal mode for better concurrent read/write performance
        and a 5-second busy timeout to handle contention gracefully.
        """
        with self._lock:
            if not self.conn:
                try:
                    self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    self.conn.row_factory = sqlite3.Row
                    self.conn.execute("PRAGMA journal_mode=WAL")
                    self.conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.Error as e:
                    logger.error("Database connection failed: %s", e)
            return self.conn

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
                        library_name TEXT
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

                # Durable per-package download + extraction outcome, polled from
                # JDownloader. Keyed by JD package name so the row survives even
                # after the package is cleared from JDownloader's list.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS download_results (
                        name TEXT PRIMARY KEY,
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

                # ── Performance indexes (idempotent) ─────────────────────
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_imdb_id ON plex_cache(imdb_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_title ON plex_cache(title)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_tv_season ON plex_cache(is_tv, season)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_year ON plex_cache(year)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_res ON plex_cache(res)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_plex_cache_updated ON plex_cache(last_updated)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_date ON downloads(date_added)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_download_results_updated ON download_results(updated_at DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp ON scan_history(timestamp DESC)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_bg_cache_last_seen ON background_scan_cache(last_seen_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_dv_scan_layer ON dv_scan(dv_layer)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_status ON rename_jobs(status)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_rename_jobs_detected ON rename_jobs(detected_at DESC)')

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
                ]
                for col_sql in _column_migrations:
                    try:
                        cursor.execute(col_sql)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" in str(e).lower():
                            pass  # Already exists — expected
                        else:
                            logger.warning("Migration failed: %s — %s", col_sql, e)

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

            except sqlite3.DatabaseError as e:
                logger.error("Database corruption detected: %s", e)
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
                        self.init_db()
                    except OSError as os_err:
                        logger.critical("Failed to recover DB: %s", os_err)
            finally:
                self._init_depth = 0

    # ── Plex cache ───────────────────────────────────────────────────

    def clear_plex_cache(self):
        """Delete all entries from the Plex cache table."""
        return self._mutate("DELETE FROM plex_cache", label="clear_cache")

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
                    # Generate a stable fallback key from rating_key + media_id
                    if is_tv:
                        fallback_key = f"{item.get('rating_key')}"
                    else:
                        fallback_key = f"{item.get('rating_key')}_{item.get('media_id')}"

                    if not item.get('key'):
                        item['key'] = fallback_key

                    cursor.execute('''
                        INSERT OR REPLACE INTO plex_cache (
                            key, title, original_title, year, res, size, imdb_id,
                            rating_key, media_id, is_tv, season, episode_count,
                            content_type, dovi, hdr, last_updated, library_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item.get('key', fallback_key),
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
                    ))

                # Remove stale rows when doing a full library refresh.
                # The INSERT OR REPLACE above already inserted fresh data;
                # now delete any old rows for this content_type that weren't
                # part of the fresh set (they have stale keys).
                if full_replace:
                    fresh_db_keys = {
                        item.get('key') or f"{item.get('rating_key')}_{item.get('media_id')}"
                        for item in items
                    }
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
        """Delete all download history records."""
        return self._mutate("DELETE FROM downloads", label="clear_history")

    def add_to_history(self, url, title, normalized_title=None, season=None,
                       resolution=None, size=None, status="completed",
                       hdr=None, dovi=False):
        """Record a downloaded URL with optional metadata for title-based matching.

        Uses ON CONFLICT to preserve the original date_added when re-downloading.
        """
        return self._mutate('''
            INSERT INTO downloads (url, title, normalized_title, season, resolution, size, status, hdr, dovi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                season = excluded.season,
                resolution = excluded.resolution,
                size = excluded.size,
                status = excluded.status,
                hdr = excluded.hdr,
                dovi = excluded.dovi
        ''', (url, title, normalized_title, season, resolution, size, status,
              hdr or None, 1 if dovi else 0),
            label="add_history")

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

    def upsert_download_result(self, name, title=None, host=None,
                               bytes_total=0, bytes_loaded=0, downloaded=0,
                               extraction="na", state="queued", error=None):
        """Insert or update the download/extraction outcome for a JD package.

        Keyed by JD package name. Refreshes updated_at on every poll so the
        list can be ordered by most-recent activity.
        """
        return self._mutate('''
            INSERT INTO download_results (
                name, title, host, bytes_total, bytes_loaded,
                downloaded, extraction, state, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                title = excluded.title,
                host = excluded.host,
                bytes_total = excluded.bytes_total,
                bytes_loaded = excluded.bytes_loaded,
                downloaded = excluded.downloaded,
                extraction = excluded.extraction,
                state = excluded.state,
                error = excluded.error,
                updated_at = CURRENT_TIMESTAMP
        ''', (name, title, host, bytes_total, bytes_loaded,
              downloaded, extraction, state, error),
            label="upsert_download_result")

    def get_download_results(self, limit=200):
        """Return tracked download/extraction outcomes, most recent first."""
        return self._query_dicts(
            "SELECT name, title, host, bytes_total, bytes_loaded, "
            "downloaded, extraction, state, error, updated_at "
            "FROM download_results ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

    def clear_download_results(self):
        """Delete all tracked download/extraction outcomes."""
        return self._mutate("DELETE FROM download_results", label="clear_download_results")


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
            items: Iterable of (url, title) pairs. Re-dismissing an
                already-dismissed URL updates its title when a non-null
                title is supplied, instead of silently keeping the old one.
        """
        pairs = [(url, title) for url, title in items if url]
        if not pairs:
            return True
        conn = None
        try:
            with self._lock:
                conn = self.get_connection()
                if not conn:
                    return False
                conn.cursor().executemany('''
                    INSERT INTO dismissed_items (url, title) VALUES (:url, :title)
                    ON CONFLICT(url) DO UPDATE SET
                        title = COALESCE(excluded.title, dismissed_items.title)
                ''', [{"url": u, "title": t} for u, t in pairs])
                conn.commit()
                self._dismissed_urls_set().update(u for u, _ in pairs)
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

    def get_dv_scans(self, dv_layer=None, limit=100000):
        """Return DV-scan rows, optionally filtered by layer (e.g. 'fel')."""
        if dv_layer:
            return self._query_dicts(
                'SELECT path, title, dv_layer, rating_key, imdb_id, '
                'scanned_at, last_seen_at FROM dv_scan WHERE dv_layer = ? '
                'ORDER BY last_seen_at DESC LIMIT ?', (dv_layer, limit), default=[])
        return self._query_dicts(
            'SELECT path, title, dv_layer, rating_key, imdb_id, '
            'scanned_at, last_seen_at FROM dv_scan '
            'ORDER BY last_seen_at DESC LIMIT ?', (limit,), default=[])

    def count_dv_scans_by_layer(self):
        """Return ``{layer: count}`` over the dv_scan table."""
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

    def clear_background_cache(self):
        """Remove all cached background-scan rows."""
        return self._mutate(
            "DELETE FROM background_scan_cache", label="clear_background_cache")

    # ── Auto-rename jobs ──────────────────────────────────────────────

    _RENAME_FIELDS = (
        "package_name", "original_path", "original_filename", "new_filename",
        "destination_path", "status", "media_type", "title", "year", "season",
        "episode", "tmdb_id", "imdb_id", "resolution", "match_confidence",
        "match_source", "move_method", "proposed_match", "plex_sort_title",
        "warning_message", "error_message", "processed_at", "reverted_at",
        "suggested_correction", "combined_episode", "split_file",
    )

    # Fields stored as JSON TEXT in SQLite — auto-serialized/deserialized.
    _JSON_RENAME_FIELDS = frozenset({"suggested_correction", "combined_episode", "split_file"})

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
        return row

    def create_rename_job(self, job):
        """Insert a rename job (dict of column→value); return the new id or None."""
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
                    return None
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO rename_jobs ({', '.join(cols)}) VALUES ({placeholders})",
                    {c: job.get(c) for c in cols})
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error("DB Error (create_rename_job): %s", e)
            return None

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

    def list_rename_jobs(self, status=None, limit=200):
        """Return rename jobs (optionally filtered by status), newest first."""
        if status:
            rows = self._query_dicts(
                "SELECT * FROM rename_jobs WHERE status = ? "
                "ORDER BY detected_at DESC LIMIT ?", (status, limit), default=[])
        else:
            rows = self._query_dicts(
                "SELECT * FROM rename_jobs ORDER BY detected_at DESC LIMIT ?",
                (limit,), default=[])
        return [self._deserialize_rename_row(r) for r in (rows or [])]

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


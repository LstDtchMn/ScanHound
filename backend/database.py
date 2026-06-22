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

                # ── Column migrations (guarded by "duplicate column name") ─
                _column_migrations = [
                    'ALTER TABLE downloads ADD COLUMN normalized_title TEXT',
                    'ALTER TABLE downloads ADD COLUMN season INTEGER',
                    'ALTER TABLE downloads ADD COLUMN resolution TEXT',
                    'ALTER TABLE downloads ADD COLUMN size TEXT',
                    "ALTER TABLE downloads ADD COLUMN status TEXT DEFAULT 'completed'",
                    'ALTER TABLE plex_cache ADD COLUMN library_name TEXT',
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
                       resolution=None, size=None, status="completed"):
        """Record a downloaded URL with optional metadata for title-based matching.

        Uses ON CONFLICT to preserve the original date_added when re-downloading.
        """
        return self._mutate('''
            INSERT INTO downloads (url, title, normalized_title, season, resolution, size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                season = excluded.season,
                resolution = excluded.resolution,
                size = excluded.size,
                status = excluded.status
        ''', (url, title, normalized_title, season, resolution, size, status),
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

    def add_dismissed_item(self, url, title=None):
        """Record a single release URL as dismissed (swiped away)."""
        return self._mutate('''
            INSERT OR IGNORE INTO dismissed_items (url, title) VALUES (?, ?)
        ''', (url, title), label="add_dismissed_item")

    def remove_dismissed_item(self, url):
        """Un-dismiss a previously dismissed URL so it can reappear."""
        return self._mutate(
            'DELETE FROM dismissed_items WHERE url = ?', (url,),
            label="remove_dismissed_item")

    def get_dismissed_urls(self):
        """Get all dismissed URLs as a set for fast membership testing."""
        rows = self._query('SELECT url FROM dismissed_items', default=[])
        return {row[0] for row in rows}

    def get_dismissed_items(self, limit=1000):
        """Return dismissed items (url, title, dismissed_at), newest first."""
        return self._query_dicts(
            'SELECT url, title, dismissed_at FROM dismissed_items '
            'ORDER BY dismissed_at DESC LIMIT ?', (limit,), default=[])

    def clear_dismissed_items(self):
        """Clear all dismissed-item records."""
        return self._mutate("DELETE FROM dismissed_items", label="clear_dismissed_items")

    def get_dismissed_count(self):
        """Return the total number of dismissed items."""
        row = self._query('SELECT COUNT(*) FROM dismissed_items', one=True, default=None)
        return row[0] if row else 0

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


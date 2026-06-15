"""Watchlist Module - Track wanted titles and get alerts when found.

Features:
- Add/remove titles to watchlist
- Import from Trakt, Letterboxd, IMDb lists
- Match against scan results
- Notify when found
"""

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple
from thefuzz import fuzz

from backend.config import DB_PATH

logger = logging.getLogger(__name__)


class WatchlistItemType(Enum):
    """Type of watchlist item."""
    MOVIE = "movie"
    TV_SHOW = "tv_show"
    TV_SEASON = "tv_season"


class WatchlistItemStatus(Enum):
    """Status of watchlist item."""
    WANTED = "wanted"
    FOUND = "found"
    DOWNLOADED = "downloaded"
    IN_LIBRARY = "in_library"


@dataclass
class WatchlistItem:
    """A watchlist item."""
    id: int = 0
    title: str = ""
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    item_type: WatchlistItemType = WatchlistItemType.MOVIE
    status: WatchlistItemStatus = WatchlistItemStatus.WANTED
    season: Optional[int] = None  # For TV seasons
    min_resolution: Optional[str] = None  # e.g., "1080p", "4K"
    prefer_dovi: bool = False
    notes: str = ""
    added_date: datetime = field(default_factory=datetime.now)
    found_date: Optional[datetime] = None
    found_url: Optional[str] = None
    priority: int = 1  # 1=low, 2=normal, 3=high

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'id': self.id,
            'title': self.title,
            'year': self.year,
            'imdb_id': self.imdb_id,
            'tmdb_id': self.tmdb_id,
            'item_type': self.item_type.value,
            'status': self.status.value,
            'season': self.season,
            'min_resolution': self.min_resolution,
            'prefer_dovi': self.prefer_dovi,
            'notes': self.notes,
            'added_date': self.added_date.isoformat() if self.added_date else None,
            'found_date': self.found_date.isoformat() if self.found_date else None,
            'found_url': self.found_url,
            'priority': self.priority
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WatchlistItem':
        """Create from dictionary."""
        return cls(
            id=data.get('id', 0),
            title=data.get('title', ''),
            year=data.get('year'),
            imdb_id=data.get('imdb_id'),
            tmdb_id=data.get('tmdb_id'),
            item_type=WatchlistItemType(data.get('item_type', 'movie')),
            status=WatchlistItemStatus(data.get('status', 'wanted')),
            season=data.get('season'),
            min_resolution=data.get('min_resolution'),
            prefer_dovi=data.get('prefer_dovi', False),
            notes=data.get('notes', ''),
            added_date=datetime.fromisoformat(data['added_date']) if data.get('added_date') else datetime.now(),
            found_date=datetime.fromisoformat(data['found_date']) if data.get('found_date') else None,
            found_url=data.get('found_url'),
            priority=data.get('priority', 1)
        )


class WatchlistManager:
    """Manages the watchlist with database persistence."""

    RESOLUTION_ORDER = {'720p': 1, '1080p': 2, '4K': 3}

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[str, WatchlistItem], None]] = []
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection (thread-safe)."""
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            return self._conn

    def _init_db(self):
        """Initialize watchlist table, migrating from legacy schema if needed."""
        with self._lock:
            try:
                conn = self._get_connection()
                cursor = conn.cursor()

                # Check if watchlist table exists and needs migration
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'")
                if cursor.fetchone():
                    cursor.execute("PRAGMA table_info(watchlist)")
                    col_info = cursor.fetchall()
                    columns = {row[1] for row in col_info}

                    # Detect legacy TMDB-based schema (has media_type column)
                    if 'media_type' in columns:
                        logger.warning("Migrating legacy TMDB-based watchlist to v2 schema")
                        # Build SELECT dynamically — only fetch columns that exist
                        v2_columns = [
                            "title", "year", "tmdb_id", "item_type", "status", "season",
                            "min_resolution", "prefer_dovi", "notes", "added_date", "priority",
                            "found_date", "found_url", "imdb_id",
                        ]
                        available = [c for c in v2_columns if c in columns]
                        cursor.execute(f"SELECT {', '.join(available)} FROM watchlist")
                        raw_rows = cursor.fetchall()
                        # Normalize into dicts with defaults for missing columns
                        col_defaults = {
                            "title": "", "year": None, "tmdb_id": None, "item_type": "movie",
                            "status": "wanted", "season": None, "min_resolution": None,
                            "prefer_dovi": 0, "notes": None, "added_date": None,
                            "priority": 1, "found_date": None, "found_url": None, "imdb_id": None,
                        }
                        rows = []
                        for raw in raw_rows:
                            row_dict = dict(zip(available, raw))
                            rows.append({c: row_dict.get(c, col_defaults.get(c)) for c in v2_columns})
                        cursor.execute("DROP TABLE watchlist")
                        # Recreate with new schema (done below by CREATE TABLE IF NOT EXISTS)
                        conn.commit()
                        # Will re-insert after table creation below
                    else:
                        rows = None
                        # Add any missing columns for non-legacy schemas
                        missing_cols = [
                            ("status", "'wanted'"),
                            ("season", "NULL"),
                            ("min_resolution", "NULL"),
                            ("prefer_dovi", "0"),
                            ("notes", "NULL"),
                            ("item_type", "'movie'"),
                            ("priority", "1"),
                            ("tmdb_id", "NULL"),
                            ("imdb_id", "NULL"),
                            ("found_date", "NULL"),
                            ("found_url", "NULL"),
                            ("added_date", "NULL"),
                        ]
                        added_any = False
                        for col, default in missing_cols:
                            if col not in columns:
                                try:
                                    cursor.execute(f"ALTER TABLE watchlist ADD COLUMN {col} TEXT DEFAULT {default}")
                                    added_any = True
                                except sqlite3.OperationalError:
                                    pass
                        if added_any:
                            logger.warning("Watchlist table upgraded — added missing columns")
                            conn.commit()
                else:
                    rows = None

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS watchlist (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        year INTEGER,
                        imdb_id TEXT,
                        tmdb_id TEXT,
                        item_type TEXT DEFAULT 'movie',
                        status TEXT DEFAULT 'wanted',
                        season INTEGER,
                        min_resolution TEXT,
                        prefer_dovi BOOLEAN DEFAULT 0,
                        notes TEXT,
                        added_date TEXT,
                        found_date TEXT,
                        found_url TEXT,
                        priority INTEGER DEFAULT 1
                    )
                ''')

                # Create indexes
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_watchlist_status
                    ON watchlist(status)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_watchlist_imdb
                    ON watchlist(imdb_id)
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_watchlist_title
                    ON watchlist(title)
                ''')

                # Re-insert migrated rows from legacy schema
                if rows:
                    insert_cols = [
                        "title", "year", "tmdb_id", "item_type", "status", "season",
                        "min_resolution", "prefer_dovi", "notes", "added_date",
                        "priority", "found_date", "found_url", "imdb_id",
                    ]
                    placeholders = ", ".join(["?"] * len(insert_cols))
                    col_names = ", ".join(insert_cols)
                    for row in rows:
                        try:
                            values = tuple(row[c] for c in insert_cols) if isinstance(row, dict) else row
                            cursor.execute(f'''
                                INSERT INTO watchlist ({col_names})
                                VALUES ({placeholders})
                            ''', values)
                        except Exception as e:
                            logger.warning("Failed to migrate watchlist row: %s", e)
                    conn.commit()
                    logger.info("Migrated %d watchlist items to v2 schema", len(rows))
                else:
                    conn.commit()
            except Exception as e:
                logger.warning("Watchlist initialization failed: %s", e)

    def add_callback(self, callback: Callable[[str, WatchlistItem], None]):
        """Add callback for watchlist events (action, item)."""
        self._callbacks.append(callback)

    def _notify(self, action: str, item: WatchlistItem):
        """Notify callbacks of an event."""
        for callback in self._callbacks:
            try:
                callback(action, item)
            except Exception as e:
                logger.error(f"Watchlist callback error: {e}")

    def add(self, item: WatchlistItem) -> int:
        """Add item to watchlist. Returns item ID."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Check for duplicates — IMDb ID first, then title+year+type+season fallback
            if item.imdb_id:
                cursor.execute('SELECT id FROM watchlist WHERE imdb_id = ?', (item.imdb_id,))
                existing = cursor.fetchone()
                if existing:
                    logger.warning(f"Item with IMDb ID {item.imdb_id} already in watchlist")
                    return existing['id']
            elif item.title:
                cursor.execute(
                    'SELECT id FROM watchlist WHERE title = ? AND year = ? '
                    'AND item_type = ? AND season IS ? AND imdb_id IS NULL',
                    (item.title, item.year, item.item_type.value, item.season)
                )
                existing = cursor.fetchone()
                if existing:
                    logger.warning(f"Item '{item.title} ({item.year})' already in watchlist")
                    return existing['id']

            try:
                cursor.execute('''
                    INSERT INTO watchlist (
                        title, year, imdb_id, tmdb_id, item_type, status,
                        season, min_resolution, prefer_dovi, notes,
                        added_date, found_date, found_url, priority
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item.title,
                    item.year,
                    item.imdb_id,
                    item.tmdb_id,
                    item.item_type.value,
                    item.status.value,
                    item.season,
                    item.min_resolution,
                    item.prefer_dovi,
                    item.notes,
                    item.added_date.isoformat() if item.added_date else None,
                    item.found_date.isoformat() if item.found_date else None,
                    item.found_url,
                    item.priority
                ))
                conn.commit()
                item.id = cursor.lastrowid
            except Exception:
                conn.rollback()
                raise

        self._notify('added', item)
        logger.info(f"Added to watchlist: {item.title} ({item.year})")

        return item.id

    def update(self, item: WatchlistItem):
        """Update a watchlist item."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    UPDATE watchlist SET
                        title = ?, year = ?, imdb_id = ?, tmdb_id = ?,
                        item_type = ?, status = ?, season = ?,
                        min_resolution = ?, prefer_dovi = ?, notes = ?,
                        found_date = ?, found_url = ?, priority = ?
                    WHERE id = ?
                ''', (
                    item.title,
                    item.year,
                    item.imdb_id,
                    item.tmdb_id,
                    item.item_type.value,
                    item.status.value,
                    item.season,
                    item.min_resolution,
                    item.prefer_dovi,
                    item.notes,
                    item.found_date.isoformat() if item.found_date else None,
                    item.found_url,
                    item.priority,
                    item.id
                ))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._notify('updated', item)

    def remove(self, item_id: int):
        """Remove item from watchlist."""
        item = None
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Get item for notification
            cursor.execute('SELECT * FROM watchlist WHERE id = ?', (item_id,))
            row = cursor.fetchone()

            if row:
                item = self._row_to_item(row)
                try:
                    cursor.execute('DELETE FROM watchlist WHERE id = ?', (item_id,))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

        if item:
            self._notify('removed', item)
            logger.info(f"Removed from watchlist: {item.title}")

    def get(self, item_id: int) -> Optional[WatchlistItem]:
        """Get a watchlist item by ID."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM watchlist WHERE id = ?', (item_id,))
            row = cursor.fetchone()
            return self._row_to_item(row) if row else None

    def get_all(
        self,
        status: Optional[WatchlistItemStatus] = None,
        item_type: Optional[WatchlistItemType] = None
    ) -> List[WatchlistItem]:
        """Get all watchlist items, optionally filtered."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            query = 'SELECT * FROM watchlist WHERE 1=1'
            params = []

            if status:
                query += ' AND status = ?'
                params.append(status.value)

            if item_type:
                query += ' AND item_type = ?'
                params.append(item_type.value)

            query += ' ORDER BY priority DESC, added_date DESC'

            cursor.execute(query, params)
            return [self._row_to_item(row) for row in cursor.fetchall()]

    def get_wanted(self) -> List[WatchlistItem]:
        """Get all wanted items."""
        return self.get_all(status=WatchlistItemStatus.WANTED)

    def _row_to_item(self, row: sqlite3.Row) -> WatchlistItem:
        """Convert database row to WatchlistItem."""
        return WatchlistItem(
            id=row['id'],
            title=row['title'],
            year=row['year'],
            imdb_id=row['imdb_id'],
            tmdb_id=row['tmdb_id'],
            item_type=WatchlistItemType(row['item_type']),
            status=WatchlistItemStatus(row['status']),
            season=row['season'],
            min_resolution=row['min_resolution'],
            prefer_dovi=bool(row['prefer_dovi']),
            notes=row['notes'] or '',
            added_date=datetime.fromisoformat(row['added_date']) if row['added_date'] else datetime.now(),
            found_date=datetime.fromisoformat(row['found_date']) if row['found_date'] else None,
            found_url=row['found_url'],
            priority=row['priority']
        )

    def search(self, query: str) -> List[WatchlistItem]:
        """Search watchlist by title (escapes SQL LIKE wildcards)."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            # Escape SQL LIKE wildcards in user input
            escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            cursor.execute(
                "SELECT * FROM watchlist WHERE title LIKE ? ESCAPE '\\' ORDER BY priority DESC",
                (f'%{escaped}%',)
            )
            return [self._row_to_item(row) for row in cursor.fetchall()]

    def find_by_imdb(self, imdb_id: str) -> Optional[WatchlistItem]:
        """Find item by IMDb ID."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM watchlist WHERE imdb_id = ?', (imdb_id,))
            row = cursor.fetchone()
            return self._row_to_item(row) if row else None

    def mark_found(
        self,
        item_id: int,
        url: str,
        auto_status: WatchlistItemStatus = WatchlistItemStatus.FOUND
    ):
        """Mark item as found (atomic get+update under lock, single notification)."""
        item = None
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM watchlist WHERE id = ?', (item_id,))
            row = cursor.fetchone()
            if row:
                item = self._row_to_item(row)
                item.status = auto_status
                item.found_date = datetime.now()
                item.found_url = url
                try:
                    cursor.execute('''
                        UPDATE watchlist SET status = ?, found_date = ?, found_url = ?
                        WHERE id = ?
                    ''', (item.status.value,
                          item.found_date.isoformat(),
                          item.found_url,
                          item.id))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        if item:
            self._notify('found', item)

    def check_against_scan_results(
        self,
        scan_items: List[Dict[str, Any]],
        fuzzy_threshold: int = 85
    ) -> List[Tuple[WatchlistItem, Dict[str, Any]]]:
        """Check scan results against watchlist.

        Returns list of (watchlist_item, matched_scan_item) tuples.
        """
        wanted = self.get_wanted()
        if not wanted:
            return []

        # Pre-index wanted items by IMDb ID for O(1) lookup on the hot path.
        # Items without an IMDb ID fall back to the O(n) title scan below.
        wanted_by_imdb: Dict[str, List[WatchlistItem]] = {}
        wanted_no_imdb: List[WatchlistItem] = []
        for w in wanted:
            if w.imdb_id:
                wanted_by_imdb.setdefault(w.imdb_id, []).append(w)
            else:
                wanted_no_imdb.append(w)

        matches = []

        def _check_constraints(wanted_item: WatchlistItem, scan_item: Dict[str, Any]) -> bool:
            """Return True if scan_item satisfies wanted_item's constraints."""
            scan_res = scan_item.get('res', '')
            scan_dovi = scan_item.get('dovi', False)
            scan_season = scan_item.get('season')

            # Season constraint for TV_SEASON type
            if wanted_item.item_type == WatchlistItemType.TV_SEASON:
                if wanted_item.season and scan_season != wanted_item.season:
                    return False

            # Resolution requirement
            if wanted_item.min_resolution:
                min_order = self.RESOLUTION_ORDER.get(wanted_item.min_resolution, 0)
                scan_order = self.RESOLUTION_ORDER.get(scan_res, 0)
                if scan_order < min_order:
                    return False

            return True

        for scan_item in scan_items:
            scan_imdb = scan_item.get('imdb_id')
            scan_title = scan_item.get('display_title', '').lower()
            scan_year = scan_item.get('year', 0)

            # 1. O(1) IMDb ID lookup
            if scan_imdb and scan_imdb in wanted_by_imdb:
                for wanted_item in wanted_by_imdb[scan_imdb]:
                    if _check_constraints(wanted_item, scan_item):
                        matches.append((wanted_item, scan_item))
                continue  # IMDb match is definitive — skip title scan

            # 2. Title + Year scan — check all wanted items (including those with
            # IMDb IDs) when the scan item lacks an IMDb ID for direct matching.
            title_candidates = wanted_no_imdb if scan_imdb else wanted
            for wanted_item in title_candidates:
                wanted_title = wanted_item.title.lower()
                year_match = (
                    not wanted_item.year or
                    not scan_year or
                    abs(scan_year - wanted_item.year) <= 1
                )
                if not year_match:
                    continue

                if scan_title == wanted_title or fuzz.token_sort_ratio(scan_title, wanted_title) >= fuzzy_threshold:
                    if _check_constraints(wanted_item, scan_item):
                        matches.append((wanted_item, scan_item))

        return matches

    def import_from_json(self, json_data: str) -> int:
        """Import watchlist from JSON. Returns count imported."""
        try:
            data = json.loads(json_data)
            items = data if isinstance(data, list) else data.get('items', [])

            count = 0
            for item_data in items:
                item = WatchlistItem.from_dict(item_data)
                self.add(item)
                count += 1

            return count
        except Exception as e:
            logger.error(f"Failed to import watchlist: {e}")
            return 0

    def export_to_json(self) -> str:
        """Export watchlist to JSON."""
        items = self.get_all()
        return json.dumps({
            'exported_at': datetime.now().isoformat(),
            'count': len(items),
            'items': [item.to_dict() for item in items]
        }, indent=2)

    def import_from_imdb_list(self, csv_content: str) -> int:
        """Import from IMDb list export (CSV format).

        Expected columns: Position, Const, Created, Modified, Description, Title, URL,
                         Title Type, IMDb Rating, Runtime (mins), Year, Genres,
                         Num Votes, Release Date, Directors
        """
        import csv
        from io import StringIO

        count = 0
        reader = csv.DictReader(StringIO(csv_content))

        for row in reader:
            try:
                imdb_id = row.get('Const', '')
                title = row.get('Title', '')
                year_str = row.get('Year', '')
                title_type = row.get('Title Type', '').lower()

                if not title:
                    continue

                year = int(year_str) if year_str.isdigit() else None

                item_type = WatchlistItemType.MOVIE
                if 'series' in title_type or 'tv' in title_type:
                    item_type = WatchlistItemType.TV_SHOW

                item = WatchlistItem(
                    title=title,
                    year=year,
                    imdb_id=imdb_id if imdb_id.startswith('tt') else None,
                    item_type=item_type,
                    notes=f"Imported from IMDb list"
                )

                self.add(item)
                count += 1

            except Exception as e:
                logger.warning(f"Failed to import row: {e}")
                continue

        return count

    def import_from_letterboxd(self, csv_content: str) -> int:
        """Import from Letterboxd export (CSV format).

        Expected columns: Date, Name, Year, Letterboxd URI, Rating
        """
        import csv
        from io import StringIO

        count = 0
        reader = csv.DictReader(StringIO(csv_content))

        for row in reader:
            try:
                title = row.get('Name', '')
                year_str = row.get('Year', '')

                if not title:
                    continue

                year = int(year_str) if year_str and year_str.isdigit() else None

                item = WatchlistItem(
                    title=title,
                    year=year,
                    item_type=WatchlistItemType.MOVIE,
                    notes=f"Imported from Letterboxd"
                )

                self.add(item)
                count += 1

            except Exception as e:
                logger.warning(f"Failed to import row: {e}")
                continue

        return count

    def get_stats(self) -> Dict[str, Any]:
        """Get watchlist statistics."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            stats = {}

            # Total count
            cursor.execute('SELECT COUNT(*) FROM watchlist')
            stats['total'] = cursor.fetchone()[0]

            # By status
            cursor.execute('''
                SELECT status, COUNT(*) as count
                FROM watchlist
                GROUP BY status
            ''')
            stats['by_status'] = {row['status']: row['count'] for row in cursor.fetchall()}

            # By type
            cursor.execute('''
                SELECT item_type, COUNT(*) as count
                FROM watchlist
                GROUP BY item_type
            ''')
            stats['by_type'] = {row['item_type']: row['count'] for row in cursor.fetchall()}

            # Recent additions (last 7 days) — use local time to match stored added_date
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            cursor.execute('''
                SELECT COUNT(*) FROM watchlist
                WHERE added_date > ?
            ''', (cutoff,))
            stats['recent_additions'] = cursor.fetchone()[0]

            # Recently found (last 7 days)
            cursor.execute('''
                SELECT COUNT(*) FROM watchlist
                WHERE found_date > ?
            ''', (cutoff,))
            stats['recently_found'] = cursor.fetchone()[0]

            return stats

    def clear(self, status: Optional[WatchlistItemStatus] = None):
        """Clear watchlist, optionally only items with specific status."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            try:
                if status:
                    cursor.execute('DELETE FROM watchlist WHERE status = ?', (status.value,))
                else:
                    cursor.execute('DELETE FROM watchlist')

                conn.commit()
            except Exception:
                conn.rollback()
                raise
        logger.info(f"Cleared watchlist{f' (status={status.value})' if status else ''}")

    def close(self):
        """Close database connection and cleanup resources."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception as e:
                    logger.debug(f"Error closing watchlist connection: {e}")
                finally:
                    self._conn = None
                logger.info("Watchlist database connection closed")

    def __del__(self):
        """Destructor to ensure connection is closed."""
        self.close()


# Global watchlist manager instance
_watchlist_manager: Optional[WatchlistManager] = None
_watchlist_lock = threading.Lock()


def get_watchlist_manager(db_path: str = None) -> WatchlistManager:
    """Get the global watchlist manager (thread-safe)."""
    global _watchlist_manager
    if _watchlist_manager is None:
        with _watchlist_lock:
            if _watchlist_manager is None:
                _watchlist_manager = WatchlistManager(db_path)
    return _watchlist_manager

"""Analytics Module - Library health metrics and advanced scan analytics.

Extends the basic scan history in database.py with:
- Library health overview (resolution, HDR, codec breakdown)
- Quality scoring (0-100 based on media quality)
- Upgrade potential analysis
- Storage projections
- Trend data for visualization
- HTML/JSON report export
"""

import html as html_lib
import json
import logging
import os
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.config import DB_PATH

logger = logging.getLogger(__name__)

# NOTE: StatsDashboard is normally constructed with a shared DatabaseManager
# (see ``db_manager=``) so all reads go through that manager's single locked
# connection — the same connection backend.database.DatabaseManager uses for
# every other subsystem. This avoids a second sqlite3 connection to the same
# file racing the primary one outside its RLock (and outside its
# synchronous=NORMAL / busy_timeout / WAL setup). The standalone
# ``db_path=``-only mode below is kept only for isolated unit tests that spin
# up their own throwaway SQLite file with no DatabaseManager in the picture.


@dataclass
class LibraryStats:
    """Statistics for a media library."""
    total_items: int = 0
    total_size_gb: float = 0.0

    # Resolution breakdown
    resolution_counts: Dict[str, int] = field(default_factory=dict)
    resolution_sizes: Dict[str, float] = field(default_factory=dict)

    # HDR breakdown
    hdr_count: int = 0
    dovi_count: int = 0
    sdr_count: int = 0

    # Codec breakdown
    codec_counts: Dict[str, int] = field(default_factory=dict)

    # Quality scores
    quality_score: float = 0.0  # 0-100 based on resolution/HDR/codec
    upgrade_potential: float = 0.0  # Percentage that could be upgraded

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'total_items': self.total_items,
            'total_size_gb': round(self.total_size_gb, 2),
            'resolution_counts': self.resolution_counts,
            'resolution_sizes': {k: round(v, 2) for k, v in self.resolution_sizes.items()},
            'hdr_count': self.hdr_count,
            'dovi_count': self.dovi_count,
            'sdr_count': self.sdr_count,
            'codec_counts': self.codec_counts,
            'quality_score': round(self.quality_score, 1),
            'upgrade_potential': round(self.upgrade_potential, 1)
        }


@dataclass
class ScanStats:
    """Statistics from scan history."""
    total_scans: int = 0
    avg_duration: float = 0.0
    total_items_scanned: int = 0
    total_missing_found: int = 0
    total_upgrades_found: int = 0
    last_scan_time: Optional[datetime] = None
    scans_per_day: Dict[str, int] = field(default_factory=dict)
    items_per_scan: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'total_scans': self.total_scans,
            'avg_duration': round(self.avg_duration, 2),
            'total_items_scanned': self.total_items_scanned,
            'total_missing_found': self.total_missing_found,
            'total_upgrades_found': self.total_upgrades_found,
            'last_scan_time': self.last_scan_time.isoformat() if self.last_scan_time else None,
            'scans_per_day': self.scans_per_day,
            'avg_items_per_scan': round(sum(self.items_per_scan) / len(self.items_per_scan), 1) if self.items_per_scan else 0
        }


@dataclass
class UpgradeAnalysis:
    """Analysis of upgrade potential."""
    total_upgradeable: int = 0
    resolution_upgrades: int = 0  # 1080p -> 4K
    hdr_upgrades: int = 0  # SDR -> HDR/DV
    size_upgrades: int = 0  # Better quality same resolution
    estimated_size_increase_gb: float = 0.0
    top_upgrade_candidates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'total_upgradeable': self.total_upgradeable,
            'resolution_upgrades': self.resolution_upgrades,
            'hdr_upgrades': self.hdr_upgrades,
            'size_upgrades': self.size_upgrades,
            'estimated_size_increase_gb': round(self.estimated_size_increase_gb, 2),
            'top_upgrade_candidates': self.top_upgrade_candidates[:20]
        }


class StatsDashboard:
    """Dashboard for library and scan statistics."""

    # Quality score weights
    RESOLUTION_SCORES = {'720p': 30, '1080p': 60, '4K': 100}
    HDR_BONUS = 15
    DOVI_BONUS = 25
    CODEC_SCORES = {'x264': 0, 'x265': 10, 'AV1': 15}

    def __init__(self, db_path: str = None, db_manager=None):
        """Args:
            db_path: Only used for the standalone fallback connection (no
                ``db_manager`` supplied) — typically an isolated test DB.
            db_manager: Shared ``backend.database.DatabaseManager`` instance.
                When supplied, all reads use its single locked connection
                instead of opening a second connection to the same file.
        """
        self.db_path = db_path or DB_PATH
        self._db_manager = db_manager
        self._conn: Optional[sqlite3.Connection] = None
        # RLock (not Lock) — _db_lock() is held for the duration of each method
        # while _get_connection() below re-acquires it internally, so it must
        # be reentrant to avoid self-deadlock.
        self._conn_lock = threading.RLock()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection.

        Delegates to the shared DatabaseManager's locked connection when one
        was supplied; otherwise falls back to a standalone connection (used
        only by isolated tests that pass a bare ``db_path``).
        """
        if self._db_manager is not None:
            return self._db_manager.get_connection()
        with self._conn_lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            return self._conn

    def _db_lock(self):
        """Return the lock to hold while using the connection.

        The shared DatabaseManager's RLock when routed through it, otherwise
        this instance's own lock.
        """
        return self._db_manager._lock if self._db_manager is not None else self._conn_lock

    def get_library_stats(self, mode: str = "Movies") -> LibraryStats:
        """Get statistics for a library.

        Args:
            mode: "Movies" or "TV Shows"

        Returns:
            LibraryStats object
        """
        stats = LibraryStats()
        try:
            with self._db_lock():
                conn = self._get_connection()
                cursor = conn.cursor()
                # Get all items for this mode
                cursor.execute(
                    'SELECT * FROM plex_cache WHERE content_type = ?',
                    (mode,)
                )
                rows = cursor.fetchall()

                if not rows:
                    return stats

                # Count unique titles (deduplicate multi-version items)
                cursor.execute(
                    "SELECT COUNT(DISTINCT COALESCE(NULLIF(imdb_id, ''), title || '|' || COALESCE(year, 0))) "
                    "FROM plex_cache WHERE content_type = ?",
                    (mode,)
                )
                stats.total_items = cursor.fetchone()[0] or 0
                resolution_sizes = defaultdict(float)
                codec_counts = defaultdict(int)

                # Resolution priority: higher index = better
                RES_PRIORITY = {'Unknown': 0, '480p': 1, '720p': 2, '1080p': 3, '4K': 4, '2160p': 4}
                # HDR priority: higher = better (SDR=0, HDR=1, DV=2)
                def hdr_rank(dovi, hdr):
                    if dovi: return 2
                    if hdr: return 1
                    return 0

                # First pass: accumulate total size (all versions — correct for disk usage)
                # and find each unique title's best resolution + HDR format
                seen_best = {}  # uid -> { res, hdr_rank, is_4k }
                for row in rows:
                    size = row['size'] or 0
                    res = row['res'] or 'Unknown'
                    is_dovi = row['dovi']
                    is_hdr = row['hdr']

                    stats.total_size_gb += size
                    resolution_sizes[res] += size

                    uid = row['imdb_id'] if row['imdb_id'] else f"{row['title']}|{row['year'] or 0}"
                    rank = hdr_rank(is_dovi, is_hdr)
                    res_pri = RES_PRIORITY.get(res, 0)

                    if uid not in seen_best:
                        seen_best[uid] = {'res': res, 'res_pri': res_pri, 'hdr': rank, 'dovi': is_dovi, 'is_hdr': is_hdr}
                    else:
                        prev = seen_best[uid]
                        # Keep best resolution
                        if res_pri > prev['res_pri']:
                            prev['res'] = res
                            prev['res_pri'] = res_pri
                        # Keep best HDR format
                        if rank > prev['hdr']:
                            prev['hdr'] = rank
                            prev['dovi'] = is_dovi
                            prev['is_hdr'] = is_hdr

                # Second pass: count per-unique-title using best version
                resolution_counts = defaultdict(int)
                quality_scores = []
                stats.dovi_count = 0
                stats.hdr_count = 0
                stats.sdr_count = 0

                for best in seen_best.values():
                    resolution_counts[best['res']] += 1

                    if best['hdr'] == 2:
                        stats.dovi_count += 1
                    elif best['hdr'] == 1:
                        stats.hdr_count += 1
                    else:
                        stats.sdr_count += 1

                    score = self.RESOLUTION_SCORES.get(best['res'], 50)
                    if best['dovi']:
                        score += self.DOVI_BONUS
                    elif best['is_hdr']:
                        score += self.HDR_BONUS
                    quality_scores.append(min(score, 100))

                stats.resolution_counts = dict(resolution_counts)
                stats.resolution_sizes = dict(resolution_sizes)
                stats.codec_counts = dict(codec_counts)

                # Calculate overall quality score
                if quality_scores:
                    stats.quality_score = sum(quality_scores) / len(quality_scores)

                # Calculate upgrade potential — unique items whose best resolution is not 4K
                non_4k_unique = sum(1 for b in seen_best.values() if b['res_pri'] < 4)
                total_unique = len(seen_best)
                stats.upgrade_potential = (non_4k_unique / total_unique * 100) if total_unique > 0 else 0

        except Exception as e:
            logger.error(f"Error calculating library stats: {e}")

        return stats

    def get_scan_stats(self, days: int = 30) -> ScanStats:
        """Get scan history statistics.

        Args:
            days: Number of days to analyze

        Returns:
            ScanStats object
        """
        stats = ScanStats()
        try:
            with self._db_lock():
                conn = self._get_connection()
                cursor = conn.cursor()
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()

                cursor.execute('''
                    SELECT * FROM scan_history
                    WHERE timestamp > ?
                    ORDER BY timestamp DESC
                ''', (cutoff,))
                rows = cursor.fetchall()

                if not rows:
                    return stats

                stats.total_scans = len(rows)
                total_duration = 0
                scans_by_day = defaultdict(int)

                for row in rows:
                    stats.total_items_scanned += row['items_scanned'] or 0
                    stats.total_missing_found += row['missing_count'] or 0
                    stats.total_upgrades_found += row['upgrade_count'] or 0
                    total_duration += row['duration_seconds'] or 0
                    stats.items_per_scan.append(row['items_scanned'] or 0)

                    # Group by day
                    try:
                        scan_date = datetime.fromisoformat(row['timestamp']).strftime('%Y-%m-%d')
                        scans_by_day[scan_date] += 1
                    except (ValueError, TypeError):
                        pass

                stats.avg_duration = total_duration / stats.total_scans if stats.total_scans > 0 else 0
                stats.scans_per_day = dict(scans_by_day)

                # Get last scan time
                try:
                    stats.last_scan_time = datetime.fromisoformat(rows[0]['timestamp'])
                except (ValueError, TypeError, IndexError):
                    pass

        except Exception as e:
            logger.error(f"Error calculating scan stats: {e}")

        return stats

    def get_upgrade_analysis(
        self,
        plex_items: List[Dict[str, Any]],
        scan_results: List[Dict[str, Any]]
    ) -> UpgradeAnalysis:
        """Analyze upgrade potential from scan results.

        Args:
            plex_items: Current Plex library items
            scan_results: Recent scan results

        Returns:
            UpgradeAnalysis object
        """
        analysis = UpgradeAnalysis()

        # Build lookup by IMDb ID
        plex_by_imdb = {}
        for item in plex_items:
            imdb_id = item.get('imdb_id')
            if imdb_id:
                plex_by_imdb[imdb_id] = item

        # Analyze scan results for upgrades
        candidates = []

        for result in scan_results:
            status = result.get('status', '')

            if 'UPGRADE' in status or 'MISSING' in status:
                analysis.total_upgradeable += 1

                if '4K' in status:
                    analysis.resolution_upgrades += 1
                elif 'DV' in status:
                    analysis.hdr_upgrades += 1
                elif '+' in status:
                    analysis.size_upgrades += 1

                # Calculate size increase
                web_size = self._parse_size(result.get('size', '0'))
                local_size = 0

                imdb_id = result.get('imdb_id')
                if imdb_id and imdb_id in plex_by_imdb:
                    local_size = plex_by_imdb[imdb_id].get('size', 0)

                if web_size > local_size:
                    analysis.estimated_size_increase_gb += (web_size - local_size)

                # Add to candidates
                candidates.append({
                    'title': result.get('display_title', 'Unknown'),
                    'year': result.get('year'),
                    'current_res': plex_by_imdb.get(imdb_id, {}).get('res', 'Unknown') if imdb_id else 'N/A',
                    'available_res': result.get('res', 'Unknown'),
                    'status': status,
                    'size_increase': web_size - local_size if web_size > local_size else 0
                })

        # Sort candidates by size increase (biggest improvements first)
        candidates.sort(key=lambda x: x.get('size_increase', 0), reverse=True)
        analysis.top_upgrade_candidates = candidates

        return analysis

    def _parse_size(self, size_str: str) -> float:
        """Parse size string to GB."""
        import re
        match = re.search(r'(\d+(?:\.\d+)?)\s*(GB|MB|TB)', str(size_str), re.IGNORECASE)
        if match:
            value = float(match.group(1))
            unit = match.group(2).upper()
            if unit == 'MB':
                return value / 1024
            elif unit == 'TB':
                return value * 1024
            return value
        return 0.0

    def get_storage_projection(
        self,
        current_stats: LibraryStats,
        upgrade_analysis: UpgradeAnalysis,
        growth_rate: float = 0.05  # 5% monthly growth
    ) -> Dict[str, Any]:
        """Project storage needs.

        Args:
            current_stats: Current library statistics
            upgrade_analysis: Upgrade analysis results
            growth_rate: Expected monthly growth rate

        Returns:
            Storage projection data
        """
        current_size = current_stats.total_size_gb
        upgrade_size = upgrade_analysis.estimated_size_increase_gb

        projections = {
            'current_size_gb': round(current_size, 2),
            'upgrade_size_gb': round(upgrade_size, 2),
            'total_after_upgrades_gb': round(current_size + upgrade_size, 2),
            'monthly_projections': []
        }

        # Project 12 months
        size = current_size
        for month in range(1, 13):
            size = size * (1 + growth_rate)
            projections['monthly_projections'].append({
                'month': month,
                'projected_size_gb': round(size, 2),
                'with_upgrades_gb': round(size + upgrade_size, 2)
            })

        return projections

    def get_trend_data(self, days: int = 30) -> Dict[str, Any]:
        """Get trend data for charts.

        Args:
            days: Number of days to analyze

        Returns:
            Trend data for visualization
        """
        try:
            with self._db_lock():
                conn = self._get_connection()
                cursor = conn.cursor()
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()

                cursor.execute('''
                    SELECT
                        date(timestamp) as scan_date,
                        SUM(items_scanned) as items,
                        SUM(missing_count) as missing,
                        SUM(upgrade_count) as upgrades,
                        AVG(duration_seconds) as avg_duration,
                        COUNT(*) as scan_count
                    FROM scan_history
                    WHERE timestamp > ?
                    GROUP BY date(timestamp)
                    ORDER BY scan_date
                ''', (cutoff,))

                rows = cursor.fetchall()

                trends = {
                    'dates': [],
                    'items_scanned': [],
                    'missing_found': [],
                    'upgrades_found': [],
                    'avg_duration': [],
                    'scan_count': []
                }

                for row in rows:
                    trends['dates'].append(row['scan_date'])
                    trends['items_scanned'].append(row['items'] or 0)
                    trends['missing_found'].append(row['missing'] or 0)
                    trends['upgrades_found'].append(row['upgrades'] or 0)
                    trends['avg_duration'].append(round(row['avg_duration'] or 0, 2))
                    trends['scan_count'].append(row['scan_count'] or 0)

                return trends

        except Exception as e:
            logger.error(f"Error getting trend data: {e}")
            return {
                'dates': [],
                'items_scanned': [],
                'missing_found': [],
                'upgrades_found': [],
                'avg_duration': [],
                'scan_count': []
            }

    def get_quality_breakdown(self, mode: str = "Movies") -> Dict[str, Any]:
        """Get detailed quality breakdown for visualization.

        Args:
            mode: "Movies" or "TV Shows"

        Returns:
            Quality breakdown data
        """
        try:
            with self._db_lock():
                conn = self._get_connection()
                cursor = conn.cursor()
                # Resolution distribution
                cursor.execute('''
                    SELECT res, COUNT(*) as count, SUM(size) as total_size
                    FROM plex_cache
                    WHERE content_type = ?
                    GROUP BY res
                ''', (mode,))

                resolution_data = {
                    'labels': [],
                    'counts': [],
                    'sizes': []
                }

                for row in cursor.fetchall():
                    resolution_data['labels'].append(row['res'] or 'Unknown')
                    resolution_data['counts'].append(row['count'])
                    resolution_data['sizes'].append(round(row['total_size'] or 0, 2))

                # HDR distribution
                cursor.execute('''
                    SELECT
                        SUM(CASE WHEN dovi = 1 THEN 1 ELSE 0 END) as dovi,
                        SUM(CASE WHEN hdr = 1 AND dovi = 0 THEN 1 ELSE 0 END) as hdr,
                        SUM(CASE WHEN hdr = 0 AND dovi = 0 THEN 1 ELSE 0 END) as sdr
                    FROM plex_cache
                    WHERE content_type = ?
                ''', (mode,))

                row = cursor.fetchone()
                hdr_data = {
                    'labels': ['Dolby Vision', 'HDR', 'SDR'],
                    'counts': [row['dovi'] or 0, row['hdr'] or 0, row['sdr'] or 0]
                }

                return {
                    'resolution': resolution_data,
                    'hdr': hdr_data
                }

        except Exception as e:
            logger.error(f"Error getting quality breakdown: {e}")
            return {
                'resolution': {'labels': [], 'counts': [], 'sizes': []},
                'hdr': {'labels': [], 'counts': []}
            }

    @staticmethod
    def _bucket_destination(dest: Optional[str], roots: Dict[str, str]) -> str:
        """Bucket an applied rename's destination under a configured library root
        (by path prefix), falling back to its parent directory name."""
        if not dest:
            return "Unknown"
        norm = dest.replace("\\", "/").rstrip("/")
        # Pick the LONGEST matching root with a path boundary, so /library/movies
        # does not swallow /library/movies-4k (a prefix of neither the other) —
        # matching the first prefix would attribute every 4K rename to Movies.
        best_label, best_len = None, -1
        for label, root in roots.items():
            if not root:
                continue
            r = str(root).replace("\\", "/").rstrip("/")
            if r and (norm == r or norm.startswith(r + "/")) and len(r) > best_len:
                best_label, best_len = label, len(r)
        if best_label is not None:
            return best_label
        parent = os.path.dirname(norm)
        return os.path.basename(parent) or parent or "Other"

    def get_rename_stats(self, roots: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Auto-rename outcomes for the Stats page.

        Returns how many files were renamed and successfully placed, a per-status
        breakdown, the move method used, and how many files landed in each
        library directory (bucketed by the configured roots when provided).
        """
        roots = roots or {}
        by_status: Dict[str, int] = {}
        by_directory: Dict[str, int] = {}
        by_method: Dict[str, int] = {}
        try:
            with self._db_lock():
                conn = self._get_connection()
                cursor = conn.cursor()
                for row in cursor.execute(
                    "SELECT status, COUNT(*) AS n FROM rename_jobs GROUP BY status"
                ):
                    by_status[row["status"] or "unknown"] = row["n"]
                for row in cursor.execute(
                    "SELECT destination_path, move_method FROM rename_jobs WHERE status = 'applied'"
                ):
                    method = row["move_method"] or "unknown"
                    by_method[method] = by_method.get(method, 0) + 1
                    bucket = self._bucket_destination(row["destination_path"], roots)
                    by_directory[bucket] = by_directory.get(bucket, 0) + 1
        except sqlite3.Error as e:
            logger.warning("rename stats query failed: %s", e)

        return {
            "applied": by_status.get("applied", 0),
            "total_jobs": sum(by_status.values()),
            "by_status": by_status,
            "by_directory": by_directory,
            "by_method": by_method,
        }

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Get complete dashboard summary.

        Returns:
            Complete dashboard data
        """
        movie_stats = self.get_library_stats("Movies")
        tv_stats = self.get_library_stats("TV Shows")
        scan_stats = self.get_scan_stats(30)

        return {
            'generated_at': datetime.now().isoformat(),
            'library': {
                'movies': movie_stats.to_dict(),
                'tv_shows': tv_stats.to_dict(),
                'total_items': movie_stats.total_items + tv_stats.total_items,
                'total_size_gb': round(movie_stats.total_size_gb + tv_stats.total_size_gb, 2),
                'overall_quality_score': round(
                    (movie_stats.quality_score + tv_stats.quality_score) / 2, 1
                ) if movie_stats.total_items and tv_stats.total_items else (
                    movie_stats.quality_score or tv_stats.quality_score
                )
            },
            'scans': scan_stats.to_dict(),
            'trends': self.get_trend_data(30),
            'quality_breakdown': {
                'movies': self.get_quality_breakdown("Movies"),
                'tv_shows': self.get_quality_breakdown("TV Shows")
            }
        }

    def export_report(self, format: str = "json") -> str:
        """Export dashboard report.

        Args:
            format: "json" or "html"

        Returns:
            Report string
        """
        summary = self.get_dashboard_summary()

        if format == "json":
            return json.dumps(summary, indent=2)

        elif format == "html":
            return self._generate_html_report(summary)

        return json.dumps(summary)

    def _generate_html_report(self, summary: Dict[str, Any]) -> str:
        """Generate HTML report."""
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>ScanHound - Statistics Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #eee; }}
        h1 {{ color: #e94560; }}
        h2 {{ color: #0f3460; background: #e94560; padding: 10px; border-radius: 5px; }}
        .card {{ background: #16213e; padding: 20px; margin: 10px 0; border-radius: 10px; }}
        .stat {{ display: inline-block; margin: 10px 20px; text-align: center; }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #e94560; }}
        .stat-label {{ color: #aaa; }}
        table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
        th, td {{ padding: 10px; border: 1px solid #0f3460; text-align: left; }}
        th {{ background: #0f3460; }}
        .good {{ color: #2ecc71; }}
        .warning {{ color: #f39c12; }}
        .bad {{ color: #e74c3c; }}
    </style>
</head>
<body>
    <h1>ScanHound Statistics Report</h1>
    <p>Generated: {summary['generated_at']}</p>

    <div class="card">
        <h2>Library Overview</h2>
        <div class="stat">
            <div class="stat-value">{summary['library']['total_items']}</div>
            <div class="stat-label">Total Items</div>
        </div>
        <div class="stat">
            <div class="stat-value">{summary['library']['total_size_gb']} GB</div>
            <div class="stat-label">Total Size</div>
        </div>
        <div class="stat">
            <div class="stat-value">{summary['library']['overall_quality_score']}</div>
            <div class="stat-label">Quality Score</div>
        </div>
    </div>

    <div class="card">
        <h2>Movies ({summary['library']['movies']['total_items']} items)</h2>
        <table>
            <tr><th>Resolution</th><th>Count</th><th>Size (GB)</th></tr>
"""
        # Add movie resolution rows
        movies = summary['library']['movies']
        for res, count in movies['resolution_counts'].items():
            size = movies['resolution_sizes'].get(res, 0)
            html += f"<tr><td>{html_lib.escape(str(res))}</td><td>{html_lib.escape(str(count))}</td><td>{html_lib.escape(str(size))}</td></tr>"

        html += f"""
        </table>
        <p>HDR: {movies['hdr_count']} | Dolby Vision: {movies['dovi_count']} | SDR: {movies['sdr_count']}</p>
    </div>

    <div class="card">
        <h2>Scan Statistics (Last 30 Days)</h2>
        <div class="stat">
            <div class="stat-value">{summary['scans']['total_scans']}</div>
            <div class="stat-label">Total Scans</div>
        </div>
        <div class="stat">
            <div class="stat-value">{summary['scans']['total_items_scanned']}</div>
            <div class="stat-label">Items Scanned</div>
        </div>
        <div class="stat">
            <div class="stat-value">{summary['scans']['total_missing_found']}</div>
            <div class="stat-label">Missing Found</div>
        </div>
        <div class="stat">
            <div class="stat-value">{summary['scans']['total_upgrades_found']}</div>
            <div class="stat-label">Upgrades Found</div>
        </div>
    </div>
</body>
</html>
"""
        return html


# Global analytics instance
_analytics: Optional[StatsDashboard] = None
_analytics_lock = threading.Lock()


def get_analytics(db_path: str = None) -> StatsDashboard:
    """Get the global analytics instance (thread-safe)."""
    global _analytics
    if _analytics is None:
        with _analytics_lock:
            if _analytics is None:
                _analytics = StatsDashboard(db_path)
    return _analytics


# Alias for backwards compatibility
get_dashboard = get_analytics

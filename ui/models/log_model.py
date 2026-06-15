"""LogModel — QAbstractListModel for log/history records."""

from datetime import datetime, timezone
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, Qt, Slot, Signal, Property, QObject,
)
from enum import IntEnum
from typing import List, Optional


class LogRoles(IntEnum):
    """Custom roles for QML data binding."""
    FileId = Qt.UserRole + 1
    OriginalFilename = Qt.UserRole + 2
    NewFilename = Qt.UserRole + 3
    DestinationPath = Qt.UserRole + 4
    Status = Qt.UserRole + 5
    StatusColor = Qt.UserRole + 6
    Title = Qt.UserRole + 7
    Year = Qt.UserRole + 8
    MediaType = Qt.UserRole + 9
    Resolution = Qt.UserRole + 10
    ErrorMessage = Qt.UserRole + 11
    ProcessedAt = Qt.UserRole + 12
    OriginalPath = Qt.UserRole + 13
    FileSize = Qt.UserRole + 14
    FileSizeStr = Qt.UserRole + 15
    MatchConfidence = Qt.UserRole + 16
    Season = Qt.UserRole + 17
    Episode = Qt.UserRole + 18
    EpisodeTitle = Qt.UserRole + 19
    DetectedAt = Qt.UserRole + 20
    ImdbId = Qt.UserRole + 21
    TmdbId = Qt.UserRole + 22
    MoveMethod = Qt.UserRole + 23
    LogGroupKey = Qt.UserRole + 24
    MatchDetails = Qt.UserRole + 25
    PosterPath = Qt.UserRole + 26
    CommonPrefix = Qt.UserRole + 27
    CanUndo = Qt.UserRole + 28


_STATUS_COLORS = {
    "completed": "#27ae60",
    "error": "#e74c3c",
    "quarantined": "#e67e22",
    "reverted": "#3498db",
}


def _format_size(size_bytes):
    """Format file size to human-readable string."""
    if not size_bytes:
        return ""
    gb = size_bytes / (1024 * 1024 * 1024)
    if gb >= 1.0:
        return f"{gb:.2f} GB"
    mb = size_bytes / (1024 * 1024)
    return f"{mb:.1f} MB"


def _utc_to_local(utc_str):
    """Convert a UTC timestamp string to local time display string."""
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %I:%M %p")
    except (ValueError, TypeError):
        return utc_str


def _group_key(rec):
    """Compute a section group key for the record.

    TV episodes of the same show+season processed on the same day get
    the same key, so QML ListView sections can group them together.
    Movies and non-TV items get a unique key per record.
    """
    media_type = rec.get("media_type", "")
    title = rec.get("title", "")
    season = rec.get("season")
    ts = rec.get("processed_at") or rec.get("detected_at") or ""

    # Extract date portion for time bucketing
    date_bucket = ts[:10] if len(ts) >= 10 else ""

    if media_type == "tv" and title and season is not None:
        return f"tv|{title}|{season}|{date_bucket}"
    # Movies / non-TV: unique per record
    return f"_|{rec.get('id', 0)}"


class LogModel(QAbstractListModel):
    """Flat list model for completed/error/quarantined records."""

    countChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[dict] = []
        self._group_prefixes: dict = {}

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def roleNames(self):
        return {
            LogRoles.FileId: b"fileId",
            LogRoles.OriginalFilename: b"originalFilename",
            LogRoles.NewFilename: b"newFilename",
            LogRoles.DestinationPath: b"destinationPath",
            LogRoles.Status: b"status",
            LogRoles.StatusColor: b"statusColor",
            LogRoles.Title: b"title",
            LogRoles.Year: b"year",
            LogRoles.MediaType: b"mediaType",
            LogRoles.Resolution: b"resolution",
            LogRoles.ErrorMessage: b"errorMessage",
            LogRoles.ProcessedAt: b"processedAt",
            LogRoles.OriginalPath: b"originalPath",
            LogRoles.FileSize: b"fileSize",
            LogRoles.FileSizeStr: b"fileSizeStr",
            LogRoles.MatchConfidence: b"matchConfidence",
            LogRoles.Season: b"season",
            LogRoles.Episode: b"episode",
            LogRoles.EpisodeTitle: b"episodeTitle",
            LogRoles.DetectedAt: b"detectedAt",
            LogRoles.ImdbId: b"imdbId",
            LogRoles.TmdbId: b"tmdbId",
            LogRoles.MoveMethod: b"moveMethod",
            LogRoles.LogGroupKey: b"logGroupKey",
            LogRoles.MatchDetails: b"matchDetails",
            LogRoles.PosterPath: b"posterPath",
            LogRoles.CommonPrefix: b"commonPrefix",
            LogRoles.CanUndo: b"canUndo",
        }

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None

        rec = self._items[index.row()]

        if role == LogRoles.FileId:
            return rec.get("id", 0)
        if role == LogRoles.OriginalFilename:
            return rec.get("original_filename", "")
        if role == LogRoles.NewFilename:
            return rec.get("new_filename", "")
        if role == LogRoles.DestinationPath:
            return rec.get("destination_path", "")
        if role == LogRoles.Status:
            return rec.get("status", "")
        if role == LogRoles.StatusColor:
            return _STATUS_COLORS.get(rec.get("status", ""), "#888888")
        if role == LogRoles.Title:
            return rec.get("title", "")
        if role == LogRoles.Year:
            return rec.get("year", 0) or 0
        if role == LogRoles.MediaType:
            return rec.get("media_type", "")
        if role == LogRoles.Resolution:
            return rec.get("resolution", "")
        if role == LogRoles.ErrorMessage:
            return rec.get("error_message", "")
        if role == LogRoles.ProcessedAt:
            return _utc_to_local(rec.get("processed_at", ""))
        if role == LogRoles.OriginalPath:
            return rec.get("original_path", "")
        if role == LogRoles.FileSize:
            return rec.get("file_size", 0) or 0
        if role == LogRoles.FileSizeStr:
            return _format_size(rec.get("file_size"))
        if role == LogRoles.MatchConfidence:
            c = rec.get("match_confidence")
            return round(c * 100) if c is not None else -1
        if role == LogRoles.Season:
            return rec.get("season") or -1
        if role == LogRoles.Episode:
            return rec.get("episode") or -1
        if role == LogRoles.EpisodeTitle:
            return rec.get("episode_title", "")
        if role == LogRoles.DetectedAt:
            return _utc_to_local(rec.get("detected_at", ""))
        if role == LogRoles.ImdbId:
            return rec.get("imdb_id", "")
        if role == LogRoles.TmdbId:
            return rec.get("tmdb_id", 0) or 0
        if role == LogRoles.MoveMethod:
            m = rec.get("move_method", "")
            if m == "copy_then_delete":
                return "Copy + Verify + Delete"
            if m == "direct":
                return "Direct Move"
            return m or ""
        if role == LogRoles.LogGroupKey:
            return _group_key(rec)
        if role == LogRoles.MatchDetails:
            return rec.get("match_details", "")
        if role == LogRoles.PosterPath:
            return rec.get("poster_path", "") or ""
        if role == LogRoles.CommonPrefix:
            return self._group_prefixes.get(_group_key(rec), "")
        if role == LogRoles.CanUndo:
            return rec.get("status") == "completed"

        return None

    @Slot(str, result=str)
    def getGroupPoster(self, group_key: str) -> str:
        """Return the poster_path for the first item matching group_key."""
        for item in self._items:
            if _group_key(item) == group_key:
                return item.get("poster_path", "") or ""
        return ""

    @Slot(str, result=str)
    def getGroupCommonPrefix(self, group_key: str) -> str:
        """Return the cached common filename prefix for the group."""
        return self._group_prefixes.get(group_key, "")

    @Slot(str, result=int)
    def getGroupCount(self, group_key: str) -> int:
        """Return the number of records belonging to a section group."""
        return sum(1 for item in self._items if _group_key(item) == group_key)

    @Property(int, notify=countChanged)
    def count(self):
        return len(self._items)

    def setRecords(self, records: List[dict]):
        self.beginResetModel()
        self._items = list(records)
        self._group_prefixes = self._compute_group_prefixes()
        self.endResetModel()
        self.countChanged.emit()

    def clear(self):
        self.beginResetModel()
        self._items.clear()
        self._group_prefixes.clear()
        self.endResetModel()
        self.countChanged.emit()

    @staticmethod
    def _common_prefix(filenames: List[str]) -> str:
        """Find common filename prefix trimmed to a dot boundary."""
        if len(filenames) < 2:
            return ""
        prefix = filenames[0]
        for fn in filenames[1:]:
            i = 0
            while i < len(prefix) and i < len(fn) and prefix[i] == fn[i]:
                i += 1
            prefix = prefix[:i]
            if not prefix:
                return ""
        # Trim to the last dot so we end on a clean word boundary
        dot = prefix.rfind(".")
        if dot >= 0:
            prefix = prefix[:dot + 1]  # keep trailing dot
        # Only useful if it's non-trivial and shorter than every filename
        if len(prefix) >= 4 and all(prefix != fn for fn in filenames):
            return prefix
        return ""

    def _compute_group_prefixes(self) -> dict:
        """Compute common original filename prefix per TV season group."""
        groups: dict = {}
        for rec in self._items:
            key = _group_key(rec)
            if not key.startswith("tv|"):
                continue
            fn = rec.get("original_filename", "")
            if fn:
                groups.setdefault(key, []).append(fn)
        return {key: self._common_prefix(fns) for key, fns in groups.items()}

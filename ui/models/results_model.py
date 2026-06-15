"""ResultsModel — QAbstractListModel exposing scan results to QML."""

import json
from collections import defaultdict
from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, Qt, Slot, Signal, Property, QEnum,
)
from enum import IntEnum
from typing import List, Optional

from backend.scanner_service import MediaItem, ScanStatus


class ResultRoles(IntEnum):
    """Custom roles for QML data binding."""
    ItemId = Qt.UserRole + 1
    Title = Qt.UserRole + 2
    Year = Qt.UserRole + 3
    Season = Qt.UserRole + 4
    Episodes = Qt.UserRole + 5
    Rating = Qt.UserRole + 6
    Votes = Qt.UserRole + 7
    VotesSource = Qt.UserRole + 8
    RtScore = Qt.UserRole + 9
    Status = Qt.UserRole + 10
    StatusText = Qt.UserRole + 11
    StatusColor = Qt.UserRole + 12
    Resolution = Qt.UserRole + 13
    Size = Qt.UserRole + 14
    Hdr = Qt.UserRole + 15
    Dovi = Qt.UserRole + 16
    Genres = Qt.UserRole + 17
    Language = Qt.UserRole + 18
    Url = Qt.UserRole + 19
    PlexInfo = Qt.UserRole + 20
    Selected = Qt.UserRole + 21
    HostPref = Qt.UserRole + 22
    PosterPath = Qt.UserRole + 23
    ImdbId = Qt.UserRole + 24
    Description = Qt.UserRole + 25
    GroupKey = Qt.UserRole + 26
    IsDuplicateGroup = Qt.UserRole + 27
    ShowGroupHeader = Qt.UserRole + 28
    DownloadedSiblings = Qt.UserRole + 29
    HasDuplicateSelected = Qt.UserRole + 30
    GroupCollapsed = Qt.UserRole + 31
    GroupItemCount = Qt.UserRole + 32
    GroupIsCollapsed = Qt.UserRole + 33
    PlexVersions = Qt.UserRole + 34
    GroupSummary = Qt.UserRole + 35
    DuplicateDetails = Qt.UserRole + 36
    DownloadedSiblingsText = Qt.UserRole + 37
    GroupEnd = Qt.UserRole + 38
    PostedDate = Qt.UserRole + 39


# Map status enum to color/text
_STATUS_COLORS = {
    ScanStatus.MISSING: "#e74c3c",
    ScanStatus.MISSING_SEASON: "#d35400",
    ScanStatus.DOWNLOADED: "#17a2b8",
    ScanStatus.IN_LIBRARY: "#27ae60",
    ScanStatus.UPGRADE: "#f39c12",
    ScanStatus.DV_UPGRADE: "#9b59b6",
}

_STATUS_TEXTS = {
    ScanStatus.MISSING: "Missing",
    ScanStatus.MISSING_SEASON: "Missing Season!",
    ScanStatus.DOWNLOADED: "Downloaded",
    ScanStatus.IN_LIBRARY: "In Library",
    ScanStatus.UPGRADE: "Upgrade",
    ScanStatus.DV_UPGRADE: "DV Upgrade",
}


class ResultsModel(QAbstractListModel):
    """Exposes a list of MediaItem objects to QML via roles."""

    countChanged = Signal()
    selectedCountChanged = Signal()
    duplicateWarning = Signal(str)  # warning message when duplicate selected

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[MediaItem] = []
        self._selected_count: int = 0
        # Tracks which rows currently have the duplicate-selected flag
        self._dup_flags: dict[int, bool] = {}
        # Tracks which group_keys have already shown a warning (avoid repeats)
        self._warned_groups: set = set()
        # Collapsed group keys
        self._collapsed_groups: set[str] = set()
        # Cache: group_key -> item count (rebuilt on setItems)
        self._group_counts: dict[str, int] = {}
        # Cache: group_key -> JSON summary string
        self._group_summaries: dict[str, str] = {}
        # Cache: row index -> precomputed DuplicateDetails text
        self._dup_details_cache: dict[int, str] = {}

    # ── QAbstractListModel overrides ──────────────────────────────────

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def roleNames(self):
        return {
            ResultRoles.ItemId: b"itemId",
            ResultRoles.Title: b"title",
            ResultRoles.Year: b"year",
            ResultRoles.Season: b"season",
            ResultRoles.Episodes: b"episodes",
            ResultRoles.Rating: b"rating",
            ResultRoles.Votes: b"votes",
            ResultRoles.VotesSource: b"votesSource",
            ResultRoles.RtScore: b"rtScore",
            ResultRoles.Status: b"status",
            ResultRoles.StatusText: b"statusText",
            ResultRoles.StatusColor: b"statusColor",
            ResultRoles.Resolution: b"resolution",
            ResultRoles.Size: b"size",
            ResultRoles.Hdr: b"hdr",
            ResultRoles.Dovi: b"dovi",
            ResultRoles.Genres: b"genres",
            ResultRoles.Language: b"language",
            ResultRoles.Url: b"url",
            ResultRoles.PlexInfo: b"plexInfo",
            ResultRoles.Selected: b"selected",
            ResultRoles.HostPref: b"hostPref",
            ResultRoles.PosterPath: b"posterPath",
            ResultRoles.ImdbId: b"imdbId",
            ResultRoles.Description: b"description",
            ResultRoles.GroupKey: b"groupKey",
            ResultRoles.IsDuplicateGroup: b"isDuplicateGroup",
            ResultRoles.ShowGroupHeader: b"showGroupHeader",
            ResultRoles.DownloadedSiblings: b"downloadedSiblings",
            ResultRoles.HasDuplicateSelected: b"hasDuplicateSelected",
            ResultRoles.GroupCollapsed: b"groupCollapsed",
            ResultRoles.GroupItemCount: b"groupItemCount",
            ResultRoles.GroupIsCollapsed: b"groupIsCollapsed",
            ResultRoles.PlexVersions: b"plexVersions",
            ResultRoles.GroupSummary: b"groupSummary",
            ResultRoles.DuplicateDetails: b"duplicateDetails",
            ResultRoles.DownloadedSiblingsText: b"downloadedSiblingsText",
            ResultRoles.GroupEnd: b"groupEnd",
            ResultRoles.PostedDate: b"postedDate",
        }

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None

        row = index.row()
        item = self._items[row]

        if role == ResultRoles.ItemId:
            return item.id
        elif role == ResultRoles.Title:
            return item.title
        elif role == ResultRoles.Year:
            return item.year
        elif role == ResultRoles.Season:
            return item.season if item.season is not None else -1
        elif role == ResultRoles.Episodes:
            return item.episodes if item.episodes is not None else -1
        elif role == ResultRoles.Rating:
            return item.rating
        elif role == ResultRoles.Votes:
            return item.votes
        elif role == ResultRoles.VotesSource:
            return item.votes_source
        elif role == ResultRoles.RtScore:
            return item.rt_score if item.rt_score is not None else -1
        elif role == ResultRoles.Status:
            return item.status.value
        elif role == ResultRoles.StatusText:
            return item.status_text or _STATUS_TEXTS.get(item.status, "Unknown")
        elif role == ResultRoles.StatusColor:
            return item.color or _STATUS_COLORS.get(item.status, "#888888")
        elif role == ResultRoles.Resolution:
            return item.resolution
        elif role == ResultRoles.Size:
            return item.size
        elif role == ResultRoles.Hdr:
            return item.hdr
        elif role == ResultRoles.Dovi:
            return item.dovi
        elif role == ResultRoles.Genres:
            return ", ".join(item.genres) if item.genres else ""
        elif role == ResultRoles.Language:
            return item.language
        elif role == ResultRoles.Url:
            return item.url
        elif role == ResultRoles.PlexInfo:
            return item.plex_info
        elif role == ResultRoles.Selected:
            return item.selected
        elif role == ResultRoles.HostPref:
            return item.host_pref
        elif role == ResultRoles.PosterPath:
            return item.poster_path or ""
        elif role == ResultRoles.ImdbId:
            return item.imdb_id or ""
        elif role == ResultRoles.Description:
            return item.description
        elif role == ResultRoles.GroupKey:
            return item.group_key
        elif role == ResultRoles.IsDuplicateGroup:
            return item.is_duplicate_group
        elif role == ResultRoles.ShowGroupHeader:
            if not item.group_key:
                return False
            if self._group_counts.get(item.group_key, 1) < 2:
                return False
            if row == 0:
                return True
            prev = self._items[row - 1]
            return item.group_key != prev.group_key
        elif role == ResultRoles.DownloadedSiblings:
            siblings = getattr(item, "downloaded_siblings", [])
            return len(siblings) if isinstance(siblings, list) else 0
        elif role == ResultRoles.HasDuplicateSelected:
            return self._dup_flags.get(row, False)
        elif role == ResultRoles.GroupCollapsed:
            if not item.group_key:
                return False
            if self._group_counts.get(item.group_key, 1) < 2:
                return False
            if item.group_key not in self._collapsed_groups:
                return False
            if row == 0:
                return False
            prev = self._items[row - 1]
            return item.group_key == prev.group_key  # not first → hide
        elif role == ResultRoles.GroupItemCount:
            return self._group_counts.get(item.group_key, 1)
        elif role == ResultRoles.GroupIsCollapsed:
            return item.group_key in self._collapsed_groups
        elif role == ResultRoles.PlexVersions:
            return item.plex_versions
        elif role == ResultRoles.GroupSummary:
            return self._group_summaries.get(item.group_key, "{}")
        elif role == ResultRoles.DuplicateDetails:
            return self._dup_details_cache.get(row, "")
        elif role == ResultRoles.DownloadedSiblingsText:
            siblings = getattr(item, "downloaded_siblings", [])
            if not isinstance(siblings, list) or not siblings:
                return ""
            return "\n".join(siblings)
        elif role == ResultRoles.GroupEnd:
            if not item.group_key or self._group_counts.get(item.group_key, 1) < 2:
                return False
            if row == len(self._items) - 1:
                return True
            return self._items[row + 1].group_key != item.group_key
        elif role == ResultRoles.PostedDate:
            return item.posted_date or ""

        return None

    # ── Properties ────────────────────────────────────────────────────

    @Property(int, notify=countChanged)
    def count(self):
        return len(self._items)

    @Property(int, notify=selectedCountChanged)
    def selectedCount(self):
        return self._selected_count

    # ── Public API ────────────────────────────────────────────────────

    def setItems(self, items: List[MediaItem]):
        """Replace all items (full refresh)."""
        self.beginResetModel()
        self._items = list(items)
        self._dup_flags.clear()
        self._warned_groups.clear()
        self._rebuild_group_counts()
        self.endResetModel()
        # Emit selection count after reset completes so QML reads consistent state
        self._recalc_selected()
        self.countChanged.emit()

    def getItems(self) -> List[MediaItem]:
        """Return current item list (for export/download)."""
        return list(self._items)

    def clear(self):
        """Remove all items."""
        self.beginResetModel()
        self._items.clear()
        self._selected_count = 0
        self._dup_flags.clear()
        self._warned_groups.clear()
        self._collapsed_groups.clear()
        self._group_counts.clear()
        self._group_summaries.clear()
        self.endResetModel()
        self.countChanged.emit()
        self.selectedCountChanged.emit()

    @Slot(int)
    def toggleSelection(self, row: int):
        """Toggle selection state of a single item."""
        if 0 <= row < len(self._items):
            self._items[row].selected = not self._items[row].selected
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [ResultRoles.Selected])
            self._recalc_selected()
            self._recalc_duplicate_flags()

    @Slot(int)
    def toggleHostPref(self, row: int):
        """Toggle host preference NF <-> RG for a single item."""
        if 0 <= row < len(self._items):
            item = self._items[row]
            item.host_pref = "NF" if item.host_pref == "RG" else "RG"
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [ResultRoles.HostPref])

    @Slot(bool)
    def selectAll(self, selected: bool):
        """Select or deselect all items."""
        for item in self._items:
            item.selected = selected
        if self._items:
            self.dataChanged.emit(
                self.index(0),
                self.index(len(self._items) - 1),
                [ResultRoles.Selected],
            )
        self._recalc_selected()
        self._recalc_duplicate_flags()

    def getItem(self, row: int) -> Optional[MediaItem]:
        """Get MediaItem at row index."""
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    @Slot(list)
    def markDownloadedUrls(self, urls: list[str]):
        """Mark any current-page items with matching URLs as downloaded."""
        if not urls or not self._items:
            return

        url_set = {url for url in urls if url}
        if not url_set:
            return

        changed = False
        for item in self._items:
            if item.url in url_set:
                self._apply_downloaded_state(item)
                changed = True

        if not changed:
            return

        self._rebuild_group_counts()
        self._recalc_duplicate_flags()
        self.dataChanged.emit(
            self.index(0),
            self.index(len(self._items) - 1),
            [
                ResultRoles.Status,
                ResultRoles.StatusText,
                ResultRoles.StatusColor,
                ResultRoles.GroupSummary,
                ResultRoles.DuplicateDetails,
                ResultRoles.HasDuplicateSelected,
                ResultRoles.DownloadedSiblings,
                ResultRoles.DownloadedSiblingsText,
            ],
        )

    def _rebuild_group_counts(self):
        """Rebuild group_key -> item count cache and group summaries."""
        counts: dict[str, int] = defaultdict(int)
        groups: dict[str, list] = defaultdict(list)
        for item in self._items:
            if item.group_key:
                counts[item.group_key] += 1
                groups[item.group_key].append(item)
        self._group_counts = dict(counts)

        # Build summary JSON per group
        summaries: dict[str, str] = {}
        for key, items in groups.items():
            if len(items) < 2:
                continue
            resolutions = list(dict.fromkeys(
                it.resolution for it in items if it.resolution
            ))
            has_dovi = any(it.dovi for it in items)
            has_hdr = any(it.hdr and it.hdr not in ("SDR", "") for it in items)
            statuses: dict[str, int] = defaultdict(int)
            for it in items:
                statuses[_STATUS_TEXTS.get(it.status, "Unknown")] += 1
            sizes = []
            for it in items:
                try:
                    s = it.size.replace(" GB", "").replace("GB", "").strip()
                    if s:
                        sizes.append(float(s))
                except (ValueError, AttributeError):
                    pass
            size_range = ""
            if sizes:
                mn, mx = min(sizes), max(sizes)
                if mn == mx:
                    size_range = f"{mn:.1f} GB"
                else:
                    size_range = f"{mn:.1f} – {mx:.1f} GB"
            summaries[key] = json.dumps({
                "resolutions": resolutions,
                "hasDovi": has_dovi,
                "hasHdr": has_hdr,
                "sizeRange": size_range,
                "statuses": dict(statuses),
            })
        self._group_summaries = summaries

        # Precompute DuplicateDetails text per row to avoid O(n×m) in data()
        dup_details: dict[int, str] = {}
        for row_idx, item in enumerate(self._items):
            if not item.group_key:
                continue
            group = groups.get(item.group_key, [])
            if len(group) < 2:
                continue
            lines = []
            for other in group:
                if other is item:
                    continue
                if other.selected:
                    desc = other.title
                    if other.season is not None and other.season >= 0:
                        desc += f" S{other.season:02d}"
                    parts = []
                    if other.resolution:
                        parts.append(other.resolution)
                    if other.dovi:
                        parts.append("DV")
                    elif other.hdr and other.hdr not in ("SDR", ""):
                        parts.append("HDR")
                    if other.size:
                        parts.append(other.size)
                    if parts:
                        desc += f"  ({', '.join(parts)})"
                    lines.append(desc)
            for other in group:
                if other is item or other.selected:
                    continue
                st = other.status.value if other.status else ""
                if st in ("downloaded", "in_library"):
                    desc = f"[{_STATUS_TEXTS.get(other.status, st)}] {other.title}"
                    if other.season is not None and other.season >= 0:
                        desc += f" S{other.season:02d}"
                    parts = []
                    if other.resolution:
                        parts.append(other.resolution)
                    if other.size:
                        parts.append(other.size)
                    if parts:
                        desc += f"  ({', '.join(parts)})"
                    lines.append(desc)
            if lines:
                dup_details[row_idx] = "\n".join(lines)
        self._dup_details_cache = dup_details

    @staticmethod
    def _apply_downloaded_state(item: MediaItem):
        """Apply the standard downloaded presentation to a result item."""
        item.status = ScanStatus.DOWNLOADED
        item.status_text = _STATUS_TEXTS[ScanStatus.DOWNLOADED]
        item.color = _STATUS_COLORS[ScanStatus.DOWNLOADED]
        item.downloaded_siblings = []

    @Slot(str)
    def toggleGroupCollapse(self, groupKey: str):
        """Toggle collapse state for a group."""
        if groupKey in self._collapsed_groups:
            self._collapsed_groups.discard(groupKey)
        else:
            self._collapsed_groups.add(groupKey)
        # Notify all rows in this group
        if self._items:
            self.dataChanged.emit(
                self.index(0),
                self.index(len(self._items) - 1),
                [ResultRoles.GroupCollapsed, ResultRoles.GroupIsCollapsed],
            )

    def _recalc_selected(self):
        count = sum(1 for i in self._items if i.selected)
        if count != self._selected_count:
            self._selected_count = count
            self.selectedCountChanged.emit()

    def _recalc_duplicate_flags(self):
        """Recalculate which selected items share a group_key with another
        selected item or a downloaded/in-library sibling, and emit warnings."""
        selected_groups: dict[tuple, list[int]] = defaultdict(list)
        downloaded_groups: dict[tuple, list[int]] = defaultdict(list)
        for row, item in enumerate(self._items):
            if not item.group_key:
                continue
            # Qualify by season so different seasons in a |TV group don't
            # falsely conflict with each other.
            conflict_key = (item.group_key, item.season)
            if item.selected:
                selected_groups[conflict_key].append(row)
            if item.status in (ScanStatus.DOWNLOADED, ScanStatus.IN_LIBRARY):
                downloaded_groups[conflict_key].append(row)

        new_flags: dict[int, bool] = {}
        active_conflict_keys: set = set()
        warned_this_call: set = set()

        for key, sel_rows in selected_groups.items():
            item0 = self._items[sel_rows[0]]
            label = item0.title
            if item0.season is not None and item0.season >= 0:
                label += f" S{item0.season:02d}"

            if len(sel_rows) > 1:
                for r in sel_rows:
                    new_flags[r] = True
                active_conflict_keys.add(key)
                if key not in self._warned_groups:
                    self.duplicateWarning.emit(
                        f"Multiple versions selected: {label}"
                    )
                    warned_this_call.add(key)

            if key in downloaded_groups:
                dl_rows = downloaded_groups[key]
                has_conflict = False
                for r in sel_rows:
                    if r not in dl_rows:
                        new_flags[r] = True
                        has_conflict = True
                for r in dl_rows:
                    if r not in sel_rows:
                        new_flags[r] = True
                        has_conflict = True
                if has_conflict:
                    active_conflict_keys.add(key)
                    if key not in self._warned_groups and key not in warned_this_call:
                        self.duplicateWarning.emit(
                            f"Already downloaded: {label}"
                        )
                        warned_this_call.add(key)

        for row, item in enumerate(self._items):
            if not item.selected:
                continue
            siblings = getattr(item, "downloaded_siblings", [])
            if not siblings or not isinstance(siblings, list) or len(siblings) == 0:
                continue
            hist_key = f"hist|{item.group_key or item.title}"
            new_flags[row] = True
            active_conflict_keys.add(hist_key)
            base_key = item.group_key or item.title
            if hist_key not in self._warned_groups and base_key not in warned_this_call:
                item_label = item.title
                if item.season is not None and item.season >= 0:
                    item_label += f" S{item.season:02d}"
                details = ", ".join(siblings[:3])
                self.duplicateWarning.emit(
                    f"Previously downloaded: {item_label} — have: {details}"
                )
                warned_this_call.add(base_key)

        self._warned_groups = active_conflict_keys

        all_rows = set(new_flags.keys()) | set(self._dup_flags.keys())
        changed_rows = [
            r for r in all_rows
            if new_flags.get(r, False) != self._dup_flags.get(r, False)
        ]
        self._dup_flags = new_flags

        for r in changed_rows:
            idx = self.index(r)
            self.dataChanged.emit(idx, idx, [ResultRoles.HasDuplicateSelected])

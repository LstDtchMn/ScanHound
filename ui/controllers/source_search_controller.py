"""SourceSearchController - Isolated per-item source search popup state."""

import asyncio
import json
import logging
import re
from typing import Dict, List
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from backend.app_service import normalize_title
from backend.scanner_service import MediaItem, ScanStatus, STATUS_TEXTS, STATUS_COLORS
from backend.sources.base import SourceCapability
from backend.sources.registry import get_registry
from backend.tmdb_client import TmdbClient
from ui.controllers.download_controller import DownloadController
from ui.models.results_model import ResultsModel

logger = logging.getLogger(__name__)

# Sentinel for the "search all sources" option
_ALL_SOURCES = "__all__"

# Sort options
SORT_OPTIONS = ["Date", "Title", "Size", "Resolution"]
_RES_ORDER = {"2160p": 4, "4k": 4, "1080p": 3, "720p": 2, "480p": 1}

_MAX_HISTORY = 10


class SourceSearchWorker(QThread):
    """Background worker for source-specific searches."""

    finished = Signal(int, str, object, str)  # token, source_name, releases, error

    def __init__(self, token: int, source_name: str, query: str, mode: str, parent=None):
        super().__init__(parent)
        self._token = token
        self._source_name = source_name
        self._query = query
        self._mode = mode

    def run(self):
        try:
            registry = get_registry()
            source = registry.get_source(self._source_name)
            if source is None:
                self.finished.emit(self._token, self._source_name, [],
                                   f"Source '{self._source_name}' is unavailable.")
                return

            result = asyncio.run(source.search(self._query, self._mode))
            message = result.errors[0] if result.errors else ""
            self.finished.emit(self._token, self._source_name,
                               list(result.releases), message)
        except Exception as e:
            logger.warning("Source search failed for %s: %s", self._source_name, e)
            self.finished.emit(self._token, self._source_name, [], str(e))


class PosterEnrichWorker(QThread):
    """Background worker that fetches TMDB poster paths for search results."""

    # Maps title -> poster_path
    finished = Signal(dict)  # {title: poster_path}

    def __init__(self, titles_with_years: list[tuple[str, int, str]], api_key: str, parent=None):
        super().__init__(parent)
        self._titles = titles_with_years  # [(title, year, media_type), ...]
        self._api_key = api_key

    def run(self):
        posters = {}
        try:
            tmdb = TmdbClient(self._api_key, timeout=8)
            for title, year, media_type in self._titles:
                try:
                    results = tmdb.search(title, media_type=media_type, year=year if year else None)
                    if results:
                        poster = results[0].get("poster_path")
                        if poster:
                            posters[title] = poster
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Poster enrichment failed: %s", e)
        self.finished.emit(posters)


class SourceSearchController(QObject):
    """Owns the popup search model so the main scanner state stays untouched."""

    openRequested = Signal()
    searchingChanged = Signal()
    windowTitleChanged = Signal()
    contextTextChanged = Signal()
    statusMessageChanged = Signal()
    selectedCountChanged = Signal()
    queryTextChanged = Signal()
    sourceNamesChanged = Signal()
    currentSourceChanged = Signal()
    currentModeChanged = Signal()
    hasMoreChanged = Signal()
    currentSortChanged = Signal()
    activeFiltersChanged = Signal()
    searchHistoryChanged = Signal()
    sourceCountsTextChanged = Signal()
    logMessage = Signal(str, str)
    scrapeProgress = Signal(int, int, str)
    scrapeDone = Signal()

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._results_model = ResultsModel(self)
        self._results_model.selectedCountChanged.connect(self.selectedCountChanged)
        self._downloads = DownloadController(
            backend,
            self._results_model,
            all_items_getter=self._results_model.getItems,
            parent=self,
        )
        self._downloads.logMessage.connect(self.logMessage)
        self._downloads.scrapeProgress.connect(self.scrapeProgress)
        self._downloads.scrapeDone.connect(self.scrapeDone)

        self._searching = False
        self._window_title = "Source Search"
        self._context_text = ""
        self._status_message = "Choose a result from the main list to search the same source."
        self._query_text = ""
        self._source_names: list[str] = []
        self._current_source = ""
        self._current_mode = "all"
        self._has_more = False
        self._search_token = 0
        self._workers: set[SourceSearchWorker] = set()

        # Download history lookup (built once per search)
        self._downloaded_urls: set[str] = set()
        self._downloaded_titles: Dict[str, List[Dict]] = {}

        # Pagination state
        self._last_query = ""
        self._last_source = ""
        self._last_mode = ""
        self._current_page = 1

        # All-sources aggregation
        self._pending_sources = 0
        self._aggregated_releases: list = []
        self._aggregated_errors: list[str] = []

        # Poster enrichment
        self._poster_worker: PosterEnrichWorker | None = None

        # Sort & filter
        self._current_sort = "Date"
        self._active_filters: list[str] = []  # e.g. ["4K", "HDR", "DV"]
        self._raw_items: list[MediaItem] = []  # unfiltered/unsorted results

        # Search history (session-level)
        self._search_history: list[dict] = []  # [{query, source, mode}, ...]

        # Per-source result counts (for all-sources mode)
        self._source_counts: dict[str, int] = {}
        self._source_counts_text = ""

    # ── Properties ────────────────────────────────────────────────────

    @Property(QObject, constant=True)
    def resultsModel(self):
        return self._results_model

    @Property(bool, notify=searchingChanged)
    def searching(self):
        return self._searching

    @Property(str, notify=windowTitleChanged)
    def windowTitle(self):
        return self._window_title

    @Property(str, notify=contextTextChanged)
    def contextText(self):
        return self._context_text

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self):
        return self._status_message

    @Property(str, notify=queryTextChanged)
    def queryText(self):
        return self._query_text

    @Property(list, notify=sourceNamesChanged)
    def sourceNames(self):
        return self._source_names

    @Property(str, notify=currentSourceChanged)
    def currentSource(self):
        return self._current_source

    @Property(str, notify=currentModeChanged)
    def currentMode(self):
        return self._current_mode

    @Property(bool, notify=hasMoreChanged)
    def hasMore(self):
        return self._has_more

    @Property(str, notify=currentSortChanged)
    def currentSort(self):
        return self._current_sort

    @Property(list, notify=activeFiltersChanged)
    def activeFilters(self):
        return self._active_filters

    @Property(list, notify=searchHistoryChanged)
    def searchHistory(self):
        return self._search_history

    @Property(str, notify=sourceCountsTextChanged)
    def sourceCountsText(self):
        return self._source_counts_text

    @Property(list, constant=True)
    def sortOptions(self):
        return list(SORT_OPTIONS)

    # ── Slots ─────────────────────────────────────────────────────────

    @Slot(str, result=str)
    def sourceDisplayName(self, name: str) -> str:
        """Return display name for a source internal name."""
        if name == _ALL_SOURCES:
            return "All Sources"
        registry = get_registry()
        config = registry.get_config(name)
        return config.display_name if config else name

    @Slot(str, result=bool)
    def supportsSearchUrl(self, url: str) -> bool:
        source_name = self._infer_source_name(url)
        if not source_name:
            return False
        registry = get_registry()
        registry.sync_from_config(self._backend.config)
        config = registry.get_config(source_name)
        return bool(config and (config.capabilities & SourceCapability.SEARCH))

    @Slot(str, int, int, str)
    def searchForItem(self, title: str, year: int, season: int, url: str):
        """Primary entry: auto-detect source from URL and search."""
        self._refresh_source_names()
        source_name = self._infer_source_name(url)
        if not source_name:
            source_name = self._source_names[0] if self._source_names else ""

        if not source_name:
            self._set_window_state(
                title=f"Source Search - {title}",
                context="No searchable sources are available.",
                message="Enable a source with search support in Settings."
            )
            self._results_model.clear()
            self._raw_items.clear()
            self.openRequested.emit()
            return

        query = self._build_query(title, year, season)
        mode = "tv" if season is not None and season >= 0 else "movies"
        self._run_search(query, source_name, mode, title, page=1)

    @Slot(str, str, str)
    def customSearch(self, query: str, source_name: str, mode: str):
        """User-driven search with custom query, source, and mode."""
        query = query.strip()
        if not query or not source_name:
            return
        self._run_search(query, source_name, mode, query, page=1)

    @Slot()
    def loadMore(self):
        """Load next page of results for the last search."""
        if not self._has_more or self._searching:
            return
        self._run_search(
            self._last_query, self._last_source, self._last_mode,
            self._last_query, page=self._current_page + 1, append=True
        )

    @Slot(int)
    def toggleSelection(self, row: int):
        self._results_model.toggleSelection(row)

    @Slot(int)
    def toggleHostPref(self, row: int):
        self._results_model.toggleHostPref(row)

    @Slot(str)
    def toggleGroupCollapse(self, group_key: str):
        self._results_model.toggleGroupCollapse(group_key)

    @Slot(int)
    def downloadItem(self, row: int):
        self._downloads.downloadItem(row)

    @Slot()
    def downloadSelected(self):
        self._downloads.downloadSelected()

    @Slot()
    def sendSelectedToJD(self):
        self._downloads.sendSelectedToJD()

    @Slot()
    def copySelectedToClipboard(self):
        self._downloads.copySelectedToClipboard()

    @Slot(str)
    def openUrl(self, url: str):
        self._downloads.openUrl(url)

    @Slot(int)
    def openInPlex(self, _row: int):
        """Shared row delegate expects this method, but popup results have no Plex context."""
        return

    @Slot(str)
    def setSort(self, sort_key: str):
        """Change sort order and re-sort results."""
        if sort_key == self._current_sort:
            return
        self._current_sort = sort_key
        self.currentSortChanged.emit()
        self._apply_sort_and_filter()

    @Slot(str)
    def toggleFilter(self, filter_name: str):
        """Toggle a resolution/HDR filter on or off."""
        if filter_name in self._active_filters:
            self._active_filters.remove(filter_name)
        else:
            self._active_filters.append(filter_name)
        self.activeFiltersChanged.emit()
        self._apply_sort_and_filter()

    @Slot()
    def clearFilters(self):
        """Remove all active filters."""
        if self._active_filters:
            self._active_filters.clear()
            self.activeFiltersChanged.emit()
            self._apply_sort_and_filter()

    @Slot(bool)
    def selectAll(self, selected: bool):
        """Select or deselect all visible items."""
        self._results_model.selectAll(selected)

    @Slot(int)
    def applyHistoryEntry(self, index: int):
        """Re-run a search from history."""
        if 0 <= index < len(self._search_history):
            entry = self._search_history[index]
            self._run_search(entry["query"], entry["source"], entry["mode"],
                             entry["query"], page=1)

    @Slot()
    def clearHistory(self):
        """Clear search history."""
        if self._search_history:
            self._search_history.clear()
            self.searchHistoryChanged.emit()

    # ── Internals ─────────────────────────────────────────────────────

    def _run_search(self, query: str, source_name: str, mode: str,
                    display_title: str, page: int = 1, append: bool = False):
        """Execute a search — shared by searchForItem, customSearch, loadMore."""
        registry = get_registry()
        registry.sync_from_config(self._backend.config)

        # Build download history lookup (once per fresh search)
        if not append:
            self._build_download_lookup()
            self._add_to_history(query, source_name, mode)
            # Reset filters on new search (keep sort)
            if self._active_filters:
                self._active_filters.clear()
                self.activeFiltersChanged.emit()
            self._source_counts.clear()
            if self._source_counts_text:
                self._source_counts_text = ""
                self.sourceCountsTextChanged.emit()

        # Store pagination state
        self._last_query = query
        self._last_source = source_name
        self._last_mode = mode
        self._current_page = page

        # Update exposed state for QML bindings
        if self._query_text != query:
            self._query_text = query
            self.queryTextChanged.emit()
        if self._current_source != source_name:
            self._current_source = source_name
            self.currentSourceChanged.emit()
        if self._current_mode != mode:
            self._current_mode = mode
            self.currentModeChanged.emit()

        self._search_token += 1
        token = self._search_token

        self._set_searching(True)
        if self._has_more:
            self._has_more = False
            self.hasMoreChanged.emit()

        # Determine which sources to query
        if source_name == _ALL_SOURCES:
            sources_to_search = [
                n for n in self._source_names if n != _ALL_SOURCES
            ]
            display_name = "All Sources"
        else:
            config = registry.get_config(source_name)
            if not config or not (config.capabilities & SourceCapability.SEARCH):
                dn = config.display_name if config else source_name
                self._set_window_state(
                    title=f"{dn} Search",
                    context=f"{dn} does not support search.",
                    message="Search is unavailable for this source."
                )
                self._results_model.clear()
                self._raw_items.clear()
                self._set_searching(False)
                self.openRequested.emit()
                return
            sources_to_search = [source_name]
            display_name = config.display_name

        page_label = f" (page {page})" if page > 1 else ""
        self._set_window_state(
            title=f"{display_name} Search - {display_title}",
            context=f"Searching {display_name} for \"{query}\"{page_label}",
            message="Searching..."
        )
        if not append:
            self._results_model.clear()
            self._raw_items.clear()
        self.openRequested.emit()

        # Launch workers
        if len(sources_to_search) > 1:
            # All-sources mode: aggregate results from all workers
            self._pending_sources = len(sources_to_search)
            self._aggregated_releases = []
            self._aggregated_errors = []
            for src_name in sources_to_search:
                worker = SourceSearchWorker(token, src_name, query, mode, self)
                worker.finished.connect(self._on_multi_search_finished)
                worker.finished.connect(lambda *_a, w=worker: self._finalize_worker(w))
                self._workers.add(worker)
                worker.start()
        else:
            worker = SourceSearchWorker(token, sources_to_search[0], query, mode, self)
            worker.finished.connect(self._on_search_finished)
            worker.finished.connect(lambda *_a, w=worker: self._finalize_worker(w))
            self._workers.add(worker)
            worker.start()

    def _build_download_lookup(self):
        """Build download history and title lookup for status cross-referencing."""
        db = self._backend.db
        self._downloaded_urls.clear()
        self._downloaded_titles.clear()
        try:
            rows = db.get_downloaded_titles()
            for row in rows:
                norm_title = row[0]
                season = row[1]
                resolution = row[2] or '?'
                size = row[3] or '?'
                url = row[4] if len(row) > 4 else ''
                if url:
                    self._downloaded_urls.add(url)
                key = f"{norm_title}|S{season}" if season else norm_title
                self._downloaded_titles.setdefault(key, []).append({
                    'resolution': resolution,
                    'size': size,
                })
        except Exception as e:
            logger.debug("Failed to build download lookup: %s", e)

    def _resolve_status(self, title: str, year: int, season, url: str):
        """Check download history to determine status, siblings."""
        if url and url in self._downloaded_urls:
            return ScanStatus.DOWNLOADED, []

        normalized = normalize_title(title)
        if normalized:
            lookup_key = f"{normalized}|S{season}" if season else normalized
            entries = self._downloaded_titles.get(lookup_key, [])
            if entries:
                siblings = [f"{e.get('resolution', '?')} - {e.get('size', '?')}" for e in entries]
                return ScanStatus.DOWNLOADED, siblings

        return ScanStatus.MISSING, []

    def _refresh_source_names(self):
        """Rebuild the list of searchable source names, with 'All' prepended."""
        registry = get_registry()
        registry.sync_from_config(self._backend.config)
        names = []
        for info in registry.list_sources():
            config = registry.get_config(info["name"])
            if config and (config.capabilities & SourceCapability.SEARCH):
                names.append(info["name"])
        # Prepend "All Sources" if there are multiple
        full = [_ALL_SOURCES] + names if len(names) > 1 else names
        if full != self._source_names:
            self._source_names = full
            self.sourceNamesChanged.emit()

    def _infer_source_name(self, url: str) -> str:
        host = (urlparse(url).netloc or "").lower()
        if "hdencode." in host:
            return "hdencode"
        if "adit-hd." in host:
            return "adithd"
        if "ddlbase." in host:
            return "ddlbase"
        return ""

    @staticmethod
    def _clean_title(title: str) -> str:
        """Strip punctuation and year from a display title for search."""
        title = re.sub(r'\(?(19|20)\d{2}\)?', '', title)
        title = re.sub(r'[":;,.\'\!\?]', ' ', title)
        return re.sub(r'\s+', ' ', title).strip()

    def _build_query(self, title: str, year: int, season: int) -> str:
        title = self._clean_title(title or "")
        if season is not None and season >= 0:
            return f"{title} S{season:02d}"
        return title

    def _set_searching(self, searching: bool):
        if self._searching != searching:
            self._searching = searching
            self.searchingChanged.emit()

    def _set_window_state(self, title: str, context: str, message: str):
        if self._window_title != title:
            self._window_title = title
            self.windowTitleChanged.emit()
        if self._context_text != context:
            self._context_text = context
            self.contextTextChanged.emit()
        if self._status_message != message:
            self._status_message = message
            self.statusMessageChanged.emit()

    # ── Single-source finish ──────────────────────────────────────────

    @Slot(int, str, object, str)
    def _on_search_finished(self, token: int, source_name: str,
                            releases: object, error_message: str):
        if token != self._search_token:
            return

        release_list = list(releases or [])
        existing = list(self._raw_items) if self._current_page > 1 else []
        offset = len(existing)
        items = [self._release_to_item(offset + idx, r) for idx, r in enumerate(release_list)]

        self._raw_items = existing + items

        # Track per-source count
        if release_list:
            self._source_counts[source_name] = \
                self._source_counts.get(source_name, 0) + len(release_list)

        # Apply sort/filter to populate model
        self._apply_sort_and_filter()

        # Pagination: if we got a full page, there might be more
        has_more = len(release_list) >= 20
        if self._has_more != has_more:
            self._has_more = has_more
            self.hasMoreChanged.emit()

        if error_message:
            message = error_message
        elif self._raw_items:
            message = f"Found {len(self._raw_items)} result(s)."
        else:
            message = "No results found for this title."
        self._set_window_state(self._window_title, self._context_text, message)
        self._set_searching(False)
        self._start_poster_enrichment()

    # ── All-sources aggregation finish ────────────────────────────────

    @Slot(int, str, object, str)
    def _on_multi_search_finished(self, token: int, source_name: str,
                                  releases: object, error_message: str):
        if token != self._search_token:
            return

        release_list = list(releases or [])
        self._aggregated_releases.extend(release_list)
        if error_message:
            self._aggregated_errors.append(f"{source_name}: {error_message}")

        # Track per-source count
        if release_list:
            dn = self.sourceDisplayName(source_name)
            self._source_counts[dn] = len(release_list)

        self._pending_sources -= 1

        if self._pending_sources <= 0:
            # All workers done — present aggregated results
            items = [self._release_to_item(idx, r)
                     for idx, r in enumerate(self._aggregated_releases)]
            self._raw_items = items

            # Build per-source counts text
            if self._source_counts:
                parts = [f"{name}: {count}" for name, count in self._source_counts.items()]
                self._source_counts_text = ", ".join(parts)
                self.sourceCountsTextChanged.emit()

            self._apply_sort_and_filter()

            if self._has_more:
                self._has_more = False
                self.hasMoreChanged.emit()

            if self._aggregated_errors and not items:
                message = "; ".join(self._aggregated_errors)
            elif items:
                message = f"Found {len(items)} result(s) across all sources."
                if self._source_counts_text:
                    message += f"  [{self._source_counts_text}]"
            else:
                message = "No results found for this title."
            self._set_window_state(self._window_title, self._context_text, message)
            self._set_searching(False)
            self._start_poster_enrichment()

    def _finalize_worker(self, worker: SourceSearchWorker):
        self._workers.discard(worker)
        worker.deleteLater()

    def _release_to_item(self, idx: int, release) -> MediaItem:
        details = release.to_dict()
        posted_date = ""
        if release.release_date:
            posted_date = release.release_date.strftime("%b %d %Y %I:%M %p")
        title = details.get("display_title") or release.display_title or release.title
        year = details.get("year", 0) or 0
        season = details.get("season")
        url = details.get("url", release.url)

        status, downloaded_siblings = self._resolve_status(title, year, season, url)

        normalized = f"{title.lower()}|{year}|S{season or 0}"
        return MediaItem(
            id=f"source-search-{idx}",
            title=title,
            year=year,
            season=season if season is not None else None,
            episodes=details.get("episodes"),
            status=status,
            status_text=STATUS_TEXTS[status],
            color=STATUS_COLORS[status],
            url=url,
            resolution=details.get("res", "") or "?",
            size=details.get("size", "") or "?",
            hdr=details.get("hdr", "") or "SDR",
            dovi=bool(details.get("dovi", False)),
            language="",
            plex_info="-",
            host_pref="RG",
            imdb_id=details.get("imdb_id") or release.imdb_id,
            description=getattr(release, "description", "") or "",
            posted_date=posted_date,
            web_data=details,
            group_key=normalized,
            downloaded_siblings=downloaded_siblings,
        )

    def _apply_sort_and_filter(self):
        """Re-apply current sort and filter to raw items, update model."""
        items = list(self._raw_items)

        # Apply filters
        for f in self._active_filters:
            if f == "4K":
                items = [i for i in items if i.resolution and
                         i.resolution.lower() in ("2160p", "4k")]
            elif f == "1080p":
                items = [i for i in items if i.resolution and
                         i.resolution.lower() == "1080p"]
            elif f == "HDR":
                items = [i for i in items if i.hdr and
                         i.hdr not in ("SDR", "", "?")]
            elif f == "DV":
                items = [i for i in items if i.dovi]

        # Apply sort
        if self._current_sort == "Title":
            items.sort(key=lambda i: (i.title or "").lower())
        elif self._current_sort == "Size":
            items.sort(key=lambda i: self._parse_size(i.size), reverse=True)
        elif self._current_sort == "Resolution":
            items.sort(key=lambda i: _RES_ORDER.get(
                (i.resolution or "").lower(), 0), reverse=True)
        # "Date" = original order (as returned by source)

        self._results_model.setItems(items)
        total_raw = len(self._raw_items)
        shown = len(items)
        if self._active_filters and shown < total_raw:
            msg = f"Showing {shown} of {total_raw} result(s) (filtered)."
        elif shown:
            msg = f"Found {total_raw} result(s)."
        else:
            msg = "No results match the current filters."
        if self._source_counts_text:
            msg += f"  [{self._source_counts_text}]"
        self._set_window_state(self._window_title, self._context_text, msg)

    @staticmethod
    def _parse_size(size_str: str) -> float:
        """Parse size string like '4.5 GB', '500 MB', or '2.5 TB' to float GB for sorting."""
        try:
            upper = (size_str or "").upper()
            s = upper.replace("TB", "").replace("GB", "").replace("MB", "").strip()
            if not s:
                return 0.0
            val = float(s)
            if "TB" in upper:
                val *= 1024
            elif "MB" in upper:
                val /= 1024
            return val
        except (ValueError, AttributeError):
            return 0.0

    def _add_to_history(self, query: str, source: str, mode: str):
        """Add a search to history, deduplicating and capping at max."""
        entry = {"query": query, "source": source, "mode": mode}
        # Remove duplicate if exists
        self._search_history = [
            h for h in self._search_history
            if not (h["query"] == query and h["source"] == source and h["mode"] == mode)
        ]
        self._search_history.insert(0, entry)
        if len(self._search_history) > _MAX_HISTORY:
            self._search_history = self._search_history[:_MAX_HISTORY]
        self.searchHistoryChanged.emit()

    def _start_poster_enrichment(self):
        """Launch background TMDB poster lookup for unique titles in results."""
        api_key = self._backend.config.get("tmdb_api_key")
        if not api_key:
            return

        items = self._results_model.getItems()
        if not items:
            return

        # Deduplicate by title — one TMDB call per unique title
        seen = set()
        lookups = []
        for item in items:
            if item.poster_path:
                continue
            if item.title in seen:
                continue
            seen.add(item.title)
            media_type = "tv" if (item.season is not None and item.season >= 0) else "movie"
            lookups.append((item.title, item.year, media_type))

        if not lookups:
            return

        # Stop any previous poster worker
        if self._poster_worker is not None:
            try:
                self._poster_worker.finished.disconnect()
            except RuntimeError:
                pass
            if self._poster_worker.isRunning():
                self._poster_worker.quit()
                self._poster_worker.wait(2000)
            self._poster_worker = None

        self._poster_worker = PosterEnrichWorker(lookups, api_key, self)
        self._poster_worker.finished.connect(self._on_posters_fetched)
        self._poster_worker.finished.connect(
            lambda *_a: self._finalize_poster_worker())
        self._poster_worker.start()

    def _on_posters_fetched(self, posters: dict):
        """Apply fetched poster paths to matching items in the model."""
        if not posters:
            return

        items = self._results_model.getItems()
        changed = False
        for item in items:
            if not item.poster_path and item.title in posters:
                item.poster_path = posters[item.title]
                changed = True

        if changed:
            count = self._results_model.rowCount()
            if count > 0:
                from ui.models.results_model import ResultRoles
                self._results_model.dataChanged.emit(
                    self._results_model.index(0),
                    self._results_model.index(count - 1),
                    [ResultRoles.PosterPath],
                )

    def _finalize_poster_worker(self):
        if self._poster_worker:
            self._poster_worker.deleteLater()
            self._poster_worker = None

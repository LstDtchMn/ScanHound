"""AutoGrabService — Automatically grab links for items matching user criteria.

After a scan completes, this service filters results by rating, votes, genre,
language, and status, then sends qualifying items to JDownloader or clipboard.

Framework-agnostic: communicates via callbacks, no UI dependencies.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from backend.scanner_service import MediaItem, ScanStatus

logger = logging.getLogger(__name__)


@dataclass
class AutoGrabReport:
    """Summary of an auto-grab run."""
    evaluated: int = 0
    grabbed: int = 0
    skipped_rating: int = 0
    skipped_votes: int = 0
    skipped_genre: int = 0
    skipped_language: int = 0
    skipped_status: int = 0
    skipped_already_downloaded: int = 0
    failed: int = 0
    grabbed_items: List[MediaItem] = field(default_factory=list)


class AutoGrabService:
    """Evaluates scan results against user criteria and auto-grabs qualifying items."""

    def __init__(self, config: Dict[str, Any], download_service):
        self.config = config
        self.download_service = download_service
        self._log_fn: Optional[Callable[[str, str], None]] = None

    def set_log_callback(self, fn: Callable[[str, str], None]):
        self._log_fn = fn

    def _log(self, msg: str, level: str = "info"):
        getattr(logger, level if level != "success" else "info", logger.info)(msg)
        if self._log_fn:
            try:
                self._log_fn(msg, level)
            except Exception as e:
                logger.debug("AutoGrabService log callback failed: %s", e)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("auto_grab_enabled", False))

    def _parse_csv(self, key: str) -> List[str]:
        """Parse a comma-separated config value into a lowercase list."""
        raw = self.config.get(key, "")
        if not raw or not raw.strip():
            return []
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    def _get_allowed_statuses(self) -> Set[ScanStatus]:
        """Get the set of ScanStatus values the user wants to auto-grab."""
        raw_statuses = self._parse_csv("auto_grab_statuses")
        if not raw_statuses:
            return {ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE}

        status_map = {
            "missing": ScanStatus.MISSING,
            "upgrade": ScanStatus.UPGRADE,
            "dv_upgrade": ScanStatus.DV_UPGRADE,
        }
        result = set()
        for s in raw_statuses:
            if s in status_map:
                result.add(status_map[s])
        return result or {ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE}

    def evaluate_item(self, item: MediaItem) -> str:
        """Check if an item meets auto-grab criteria.

        Returns:
            Empty string if the item qualifies, otherwise a reason string
            explaining why it was skipped.
        """
        # Already downloaded — check before the status gate below, otherwise a
        # DOWNLOADED item is swallowed as "status" and never attributed here.
        if item.status == ScanStatus.DOWNLOADED:
            return "already_downloaded"

        # Status check
        allowed_statuses = self._get_allowed_statuses()
        if item.status not in allowed_statuses:
            return "status"

        # Rating check
        min_rating = float(self.config.get("auto_grab_min_rating", 0.0))
        if min_rating > 0 and item.rating < min_rating:
            return "rating"

        # Votes check
        min_votes = int(self.config.get("auto_grab_min_votes", 0))
        if min_votes > 0 and item.votes < min_votes:
            return "votes"

        # Genre include check
        include_genres = self._parse_csv("auto_grab_genres")
        if include_genres:
            item_genres = [g.lower() for g in (item.genres or [])]
            if not any(g in item_genres for g in include_genres):
                return "genre"

        # Genre exclude check
        exclude_genres = self._parse_csv("auto_grab_exclude_genres")
        if exclude_genres:
            item_genres = [g.lower() for g in (item.genres or [])]
            if any(g in item_genres for g in exclude_genres):
                return "genre"

        # Language check
        include_languages = self._parse_csv("auto_grab_languages")
        if include_languages:
            item_lang = (item.language or "").lower()
            if item_lang and item_lang not in include_languages:
                return "language"

        return ""

    def process_items(self, items: List[MediaItem]) -> AutoGrabReport:
        """Filter items by criteria and auto-grab qualifying ones.

        Args:
            items: Scan results to evaluate.

        Returns:
            AutoGrabReport with counts and list of grabbed items.
        """
        report = AutoGrabReport()

        if not self.enabled:
            return report

        self._log("Auto-Grab: Evaluating scan results...")

        for item in items:
            report.evaluated += 1
            reason = self.evaluate_item(item)

            if reason:
                if reason == "status":
                    report.skipped_status += 1
                elif reason == "already_downloaded":
                    report.skipped_already_downloaded += 1
                elif reason == "rating":
                    report.skipped_rating += 1
                elif reason == "votes":
                    report.skipped_votes += 1
                elif reason == "genre":
                    report.skipped_genre += 1
                elif reason == "language":
                    report.skipped_language += 1
                continue

            # Item qualifies — attempt to grab
            try:
                service_type = "Rapidgator"
                preferred_host = self.config.get("adithd_preferred_host", "rapidgator")
                if preferred_host == "nitroflare":
                    service_type = "Nitroflare"

                result = self.download_service.download_item(
                    url=item.url,
                    title=item.title,
                    season=item.season,
                    resolution=item.resolution,
                    size=item.size,
                    service_type=service_type,
                )

                if result.get("success"):
                    report.grabbed += 1
                    report.grabbed_items.append(item)
                    self._log(
                        f"Auto-Grab: Grabbed '{item.title}' ({item.resolution}) "
                        f"via {result.get('method', '?')}",
                        "success",
                    )
                else:
                    report.failed += 1
                    self._log(
                        f"Auto-Grab: Failed to grab '{item.title}': "
                        f"{result.get('message', 'unknown error')}",
                        "warning",
                    )
            except Exception as e:
                report.failed += 1
                self._log(f"Auto-Grab: Error grabbing '{item.title}': {e}", "error")

        # Summary
        self._log(
            f"Auto-Grab complete: {report.grabbed} grabbed, "
            f"{report.evaluated - report.grabbed - report.failed} skipped, "
            f"{report.failed} failed "
            f"(rating: {report.skipped_rating}, votes: {report.skipped_votes}, "
            f"genre: {report.skipped_genre}, language: {report.skipped_language}, "
            f"status: {report.skipped_status})",
            "info",
        )

        return report

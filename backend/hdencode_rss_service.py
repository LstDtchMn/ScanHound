"""RSS shadow discovery, conditional polling, and coverage metrics."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Callable, Optional

from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficDenied,
    configure_hdencode_coordinator,
)
from backend.sources.hdencode_feed_client import HDEncodeFeedClient
from backend.sources.hdencode_feed_parser import parse_feed
from backend.sources.hdencode_feeds import catchup_feeds, normal_feeds
from backend.hdencode_shadow import catchup_required


logger = logging.getLogger(__name__)


def _cancelled(observer: Optional[Callable[[], bool]]) -> bool:
    if observer is None:
        return False
    try:
        return bool(observer())
    except Exception:
        # A broken ownership/cancellation observer must fail closed.
        return True


class HDEncodeRSSService:
    """Poll qualified feeds without surfacing or downloading candidates."""

    def __init__(self, config, db, *, client=None):
        self.config = config if isinstance(config, dict) else {}
        self.db = db
        self.client = client or HDEncodeFeedClient()
        # The service owns its coordinator context instead of depending on an
        # unrelated caller or test to have configured the process singleton.
        self.coordinator = configure_hdencode_coordinator(self.config, db)
        self._last_cycle = None
        self._first_cycle = True

    def _enabled(self) -> bool:
        return (
            self.config.get("hdencode_enabled", True) is True
            and self.config.get("hdencode_discovery_mode")
            in {"rss_shadow", "rss_primary"}
        )

    def _poll_interval_seconds(self) -> int:
        try:
            minutes = int(self.config.get("hdencode_rss_poll_minutes", 60))
        except (TypeError, ValueError):
            minutes = 60
        return max(15, min(minutes, 360)) * 60

    def _catchup_interval_seconds(self) -> int:
        try:
            hours = int(self.config.get("hdencode_rss_catchup_hours", 4))
        except (TypeError, ValueError):
            hours = 4
        return max(1, min(hours, 48)) * 3600

    def _due(self, state, interval_seconds) -> bool:
        if not state or not state.get("last_checked_at"):
            return True
        try:
            checked = datetime.fromisoformat(state["last_checked_at"])
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - checked.astimezone(timezone.utc)
            return age.total_seconds() >= interval_seconds
        except (TypeError, ValueError):
            return True

    def poll_cycle(
        self,
        *,
        stop_requested: Optional[Callable[[], bool]] = None,
        include_catchup: Optional[bool] = None,
    ) -> dict:
        mode = self.config.get("hdencode_discovery_mode", "listing")
        if not self._enabled():
            return {
                "mode": mode,
                "skipped": True,
                "reason": "rss_discovery_disabled",
                "feeds": [],
                "listing_fallback_started": False,
                "downloads_started": 0,
            }

        readiness = self.db.get_hdencode_rss_readiness(
            min_cycles=self.config.get(
                "hdencode_rss_shadow_min_cycles", 20
            ),
            min_days=self.config.get(
                "hdencode_rss_shadow_min_days", 7
            ),
        )
        if mode == "rss_primary" and not readiness["ready"]:
            cycle = {
                "mode": mode,
                "skipped": True,
                "reason": "primary_not_ready",
                "readiness": readiness,
                "feeds": [],
                "changed": 0,
                "candidates": 0,
                "requests": 0,
                "coverage_uncertain": True,
                "fallback_qualified": False,
                "listing_fallback_started": False,
                "downloads_started": 0,
            }
            self._last_cycle = cycle
            return cycle

        normal = list(normal_feeds())
        if include_catchup is None:
            include_catchup = catchup_required(
                [
                    self.db.get_hdencode_feed_state(feed.key) or {}
                    for feed in normal_feeds()
                ],
                fallback_hours=self.config.get(
                    "hdencode_rss_catchup_hours", 4
                ),
            )
        feeds = normal + (list(catchup_feeds()) if include_catchup else [])

        results = []
        for feed in feeds:
            if _cancelled(stop_requested):
                results.append(
                    {"feed": feed.key, "outcome": "cancelled_before_start"}
                )
                break
            state = self.db.get_hdencode_feed_state(feed.key) or {}
            interval = (
                self._poll_interval_seconds()
                if feed.role == "normal"
                else self._catchup_interval_seconds()
            )
            if not self._due(state, interval):
                results.append({"feed": feed.key, "outcome": "not_due"})
                continue
            results.append(
                self.poll_feed(feed, stop_requested=stop_requested)
            )

        normal_outcomes = {
            result.get("outcome")
            for result in results
            if result.get("feed") in {"movies_all", "tv_all"}
        }
        coverage_uncertain = bool(
            normal_outcomes
            & {"failed", "http_error", "parse_failed", "denied"}
        )
        coordinator_blocked = bool(
            self.coordinator.snapshot().get("blocked")
        )
        fallback_qualified = bool(
            mode == "rss_primary"
            and readiness["ready"]
            and coverage_uncertain
            and self.config.get(
                "hdencode_rss_listing_fallback_enabled"
            ) is True
            and not coordinator_blocked
        )
        candidate_urls = self.db.list_hdencode_current_feed_urls()
        cycle = {
            "mode": mode,
            "at": time.time(),
            "feeds": results,
            "changed": sum(r.get("changed", 0) for r in results),
            "candidates": len(candidate_urls),
            "candidate_urls": candidate_urls,
            "catchup_used": bool(include_catchup),
            "restart_recovery": self._first_cycle,
            "requests": sum(1 for r in results if r.get("requested")),
            "readiness": readiness,
            "coverage_uncertain": coverage_uncertain,
            "fallback_qualified": fallback_qualified,
            "listing_fallback_started": False,
            "downloads_started": 0,
        }
        self._first_cycle = False
        self._last_cycle = cycle
        return cycle

    def poll_feed(self, feed, *, stop_requested=None) -> dict:
        if _cancelled(stop_requested):
            return {
                "feed": feed.key,
                "outcome": "cancelled_before_start",
                "requested": False,
            }

        state = self.db.get_hdencode_feed_state(feed.key) or {}
        last_modified = state.get("last_modified")
        started = datetime.now(timezone.utc).isoformat()

        try:
            with self.coordinator.request(
                "rss",
                stop_requested=stop_requested,
                priority=10,
            ):
                response = self.client.fetch(
                    feed.url,
                    last_modified=last_modified,
                )
        except (HDEncodeTrafficDenied, HDEncodeRequestCancelled) as exc:
            return {
                "feed": feed.key,
                "outcome": "denied",
                "reason": exc.code,
                "requested": False,
            }
        except Exception as exc:
            if _cancelled(stop_requested):
                return {
                    "feed": feed.key,
                    "outcome": "cancelled_after_error",
                    "requested": True,
                }
            checked = datetime.now(timezone.utc).isoformat()
            self.db.record_hdencode_feed_failure(
                feed_key=feed.key,
                feed_url=feed.url,
                checked_at=checked,
                status=0,
                error_code=type(exc).__name__,
            )
            self.coordinator.observe_network_failure(type(exc).__name__)
            return {
                "feed": feed.key,
                "outcome": "failed",
                "reason": type(exc).__name__,
                "requested": True,
            }

        # A late response from an expired lifespan must not publish through the
        # captured DB object or advance a conditional validator.
        if _cancelled(stop_requested):
            return {
                "feed": feed.key,
                "outcome": "cancelled_after_response",
                "requested": True,
            }

        checked = datetime.now(timezone.utc).isoformat()
        decision = self.coordinator.observe_http_status(response.status)
        if response.status == 304:
            self.db.record_hdencode_feed_not_modified(
                feed_key=feed.key,
                feed_url=feed.url,
                last_modified=last_modified,
                checked_at=checked,
            )
            return {
                "feed": feed.key,
                "outcome": "not_modified",
                "status": 304,
                "requested": True,
                "changed": 0,
                "candidate_count": 0,
            }

        if response.status != 200:
            self.db.record_hdencode_feed_failure(
                feed_key=feed.key,
                feed_url=feed.url,
                checked_at=checked,
                status=response.status,
                error_code=f"http_{response.status}",
            )
            return {
                "feed": feed.key,
                "outcome": "http_error",
                "status": response.status,
                "requested": True,
                "blocked": decision.blocked,
            }

        try:
            parsed = parse_feed(response.body, feed.key)
        except Exception as exc:
            if _cancelled(stop_requested):
                return {
                    "feed": feed.key,
                    "outcome": "cancelled_after_parse",
                    "requested": True,
                }
            self.db.record_hdencode_feed_failure(
                feed_key=feed.key,
                feed_url=feed.url,
                checked_at=checked,
                status=response.status,
                error_code=type(exc).__name__,
            )
            return {
                "feed": feed.key,
                "outcome": "parse_failed",
                "reason": type(exc).__name__,
                "requested": True,
            }

        if _cancelled(stop_requested):
            return {
                "feed": feed.key,
                "outcome": "cancelled_before_commit",
                "requested": True,
            }

        count = self.db.ingest_hdencode_feed(
            feed_key=feed.key,
            feed_url=feed.url,
            last_modified=response.last_modified,
            http_status=response.status,
            body_sha256=parsed.body_sha256,
            channel_last_build_date=parsed.channel_last_build_date,
            entries=[entry.as_database_row() for entry in parsed.entries],
            started_at=started,
            completed_at=checked,
        )
        depth = _observed_depth_seconds(parsed.entries)
        try:
            self.db.update_hdencode_feed_depth(feed.key, depth)
        except Exception:
            logger.warning(
                "RSS depth update failed after committed ingest for %s",
                feed.key,
                exc_info=True,
            )
        return {
            "feed": feed.key,
            "outcome": "changed",
            "status": 200,
            "requested": True,
            "changed": 1,
            "candidate_count": count,
            "observed_depth_seconds": depth,
            "body_sha256": parsed.body_sha256,
        }

    def status(self) -> dict:
        return {
            "mode": self.config.get("hdencode_discovery_mode", "listing"),
            "feeds": self.db.list_hdencode_feed_states(),
            "last_cycle": self._last_cycle,
            "coordinator": self.coordinator.snapshot(),
            "readiness": self.db.get_hdencode_rss_readiness(
                min_cycles=self.config.get(
                    "hdencode_rss_shadow_min_cycles", 20
                ),
                min_days=self.config.get(
                    "hdencode_rss_shadow_min_days", 7
                ),
            ),
        }


def _observed_depth_seconds(entries) -> Optional[int]:
    if not entries:
        return None
    try:
        dates = [
            datetime.fromisoformat(entry.pub_date).astimezone(timezone.utc)
            for entry in entries
        ]
    except (TypeError, ValueError):
        return None
    return max(0, int((max(dates) - min(dates)).total_seconds()))

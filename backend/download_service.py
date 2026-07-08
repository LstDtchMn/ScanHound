"""DownloadService — JDownloader integration, link scraping, and download management.

Framework-agnostic: communicates via callbacks, no UI dependencies.
"""

import csv
import logging
import os
import re
import subprocess
import sys
import time
import threading
import webbrowser
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from backend.database import DatabaseManager
from backend.app_service import normalize_title
from backend.sources.ddlbase import decode_ddlbase_link

logger = logging.getLogger(__name__)

# Lazy imports for optional heavy dependencies
_uc = None
_By = None
_WebDriverWait = None
_EC = None

_DDLBASE_SHORTLINK_DOMAINS = (
    "cuty.io",
    "cuttlinks.com",
    "cutt.ly",
    "fc.lc",
    "fc-lc.xyz",
    "ouo.io",
    "exe.io",
    "gplinks.co",
    "shrinkme.io",
    "linkvertise.com",
)
# Only these domains go through the automated cuttlinks resolution flow
_AUTOMATABLE_SHORTLINK_DOMAINS = ("cuty.io", "cuttlinks.com")
_SUPPORTED_DOWNLOAD_HOSTS = (
    "1fichier.com",
    "rapidgator.net",
    "nitroflare.com",
    "ddownload.com",
)


def _url_matches_domain(url: str, domains: tuple) -> bool:
    """Check if a URL's host matches any of the given domains (netloc-based)."""
    try:
        netloc = urlparse(url).netloc.lower()
        return any(netloc == d or netloc.endswith("." + d) for d in domains)
    except Exception:
        return False


def _normalize_link_url(url: str) -> str:
    """Canonicalize a file-host URL so ScanHound's scrape map and JDownloader's
    stored links match despite cosmetic differences.

    JDownloader frequently stores a link with a different scheme, a ``www.``
    prefix, or a trailing slash than the URL ScanHound recorded when it
    scraped the source page. Matching on the bare ``host/path`` recovers those
    near-miss cases — except for hosts that put the file id in the QUERY rather
    than the path (e.g. ``1fichier.com/?abc123``), where the query is kept so
    every such link doesn't collapse to the bare host and cross-wire titles.

    Returns ``""`` for falsy input.
    """
    if not url:
        return ""
    try:
        raw = url.strip()
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "").rstrip("/")
        if path:
            ident = f"{host}{path}"
        else:
            # No distinguishing path (e.g. 1fichier) — fall back to the query.
            query = parsed.query or ""
            ident = f"{host}?{query}" if query else host
        return ident.lower()
    except Exception:
        return url.strip().lower()


_ARCHIVE_RE = re.compile(r'\.(rar|zip|7z|tar|gz|bz2|tgz|r\d\d|z\d\d|001)$', re.IGNORECASE)


def _is_archive_name(name: str) -> bool:
    """True if a filename looks like an archive JDownloader would extract.

    Direct media files (.mkv/.mp4/...) have nothing to extract, so a package
    made only of those is *complete* once downloaded — it should not sit at
    "downloaded" forever waiting for an extraction that never happens.
    """
    return bool(_ARCHIVE_RE.search((name or "").strip()))


def _ensure_selenium():
    """Lazy-load Selenium and undetected-chromedriver."""
    global _uc, _By, _WebDriverWait, _EC
    if _uc is None:
        import undetected_chromedriver as uc_mod
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        _uc = uc_mod
        _By = By
        _WebDriverWait = WebDriverWait
        _EC = EC


class DownloadService:
    """Manages download operations, JDownloader, and WebDriver link scraping."""

    def __init__(self, config: Dict[str, Any], db: DatabaseManager, server_mode: bool = False):
        self.config = config
        self.db = db
        # In server/headless mode (the FastAPI/Docker deployment) there is no
        # user-facing browser, so the browser fallback is meaningless and must
        # not be reported as a successful delivery.
        self.server_mode = server_mode

        # WebDriver
        self.cached_driver = None
        self._driver_lock = threading.RLock()
        self._active_scrapes = 0
        self._scrape_count_lock = threading.Lock()
        self._scrapes_done = threading.Condition(self._scrape_count_lock)

        # Download tracking (protected by _history_lock)
        self._history_lock = threading.Lock()
        self.download_history: Set[str] = set()
        self._downloaded_titles_lookup: Dict[str, List[Dict]] = {}

        # Cached MyJDownloader connection — avoids re-authenticating on every
        # call (the results poller hits this every few seconds).
        self._jd_lock = threading.Lock()
        self._jd = None
        self._jd_device = None
        self._jd_conn_ts = 0.0
        self._JD_CONN_TTL = 90.0
        # Per-package last-recorded signature so the poller only writes rows
        # that actually changed (avoids re-upserting a large stable queue).
        self._results_cache: Dict[str, tuple] = {}
        # Best real title ever resolved for a JD package name. Lets a transient
        # scrape-map miss keep the previously-resolved title instead of
        # regressing the display back to the raw (often obfuscated) JD name.
        self._best_titles: Dict[str, str] = {}

        # Callbacks
        self._log_fn: Optional[Callable[[str, str], None]] = None

    # ── Callbacks ─────────────────────────────────────────────────────

    def set_log_callback(self, fn: Callable[[str, str], None]):
        self._log_fn = fn

    def _log(self, msg: str, level: str = "info"):
        getattr(logger, level if level != "success" else "info", logger.info)(msg)
        if self._log_fn:
            try:
                self._log_fn(msg, level)
            except Exception:
                pass

    @staticmethod
    def _progress(event: str, data: dict, _cb: Optional[Callable] = None):
        if _cb:
            try:
                _cb(event, data)
            except Exception:
                pass

    # ── Download history ──────────────────────────────────────────────

    def load_download_history(self) -> Set[str]:
        """Load download history from DB."""
        try:
            with self.db.transaction() as conn:
                if not conn:
                    return set()
                rows = conn.execute(
                    "SELECT url FROM downloads WHERE COALESCE(status, 'completed') != 'failed'"
                ).fetchall()
                return {row[0] for row in rows}
        except Exception:
            return set()

    def save_to_history(self, url: str, title: str, season: Optional[int],
                        resolution: str, size: str, status: str = "completed",
                        hdr: str = "", dovi: bool = False):
        """Save a downloaded item to history."""
        try:
            normalized = normalize_title(title)
            self.db.add_to_history(
                url=url, title=title, normalized_title=normalized,
                season=season, resolution=resolution, size=size,
                status=status, hdr=hdr or None, dovi=dovi,
            )
            with self._history_lock:
                self.download_history.add(url)
                key = f"{normalized}|S{season}" if season is not None else normalized
                self._downloaded_titles_lookup.setdefault(key, []).append({
                    'resolution': resolution,
                    'size': size,
                    'hdr': hdr or '',
                    'dovi': dovi,
                })
            return True
        except Exception as e:
            logger.error(f"Failed to save to history: {e}")
            return False

    # ── JDownloader ───────────────────────────────────────────────────

    def send_to_jdownloader(self, links: List[str], package_name: str,
                              destination: str = "",
                              progress_callback: Optional[Callable] = None) -> bool:
        """Send links to JDownloader. Returns True on success.

        ``destination`` optionally pins the download folder (per-type routing,
        e.g. a movies vs TV path); JDownloader extracts into it.
        """
        jd_method = self.config.get("jd_method", "folder")

        if jd_method == "folder":
            folder = self.config.get("jd_folder", "")
            if folder and os.path.isdir(folder):
                try:
                    for i, link in enumerate(links):
                        filename = f"{package_name.replace(':', '-')[:50]}_{int(time.time())}_{i}.crawljob"
                        filepath = os.path.join(folder, filename)
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(f"text={link}\n")
                            f.write(f"packageName={package_name[:50]}\n")
                            if destination:
                                f.write(f"downloadFolder={destination}\n")
                            # autoConfirm moves it out of the linkgrabber without a
                            # manual confirm; forcedStart begins the download even if
                            # JD's queue is paused — together they make a grab
                            # actually start, not just sit queued.
                            f.write("autoConfirm=TRUE\n")
                            f.write("autoStart=TRUE\n")
                            f.write("forcedStart=TRUE\n\n")
                    self._log(f"Sent {len(links)} links to JDownloader folder", "success")
                    return True
                except Exception as e:
                    self._log(f"JD folder error: {e}", "error")
                    return False
            else:
                self._log("JDownloader folder not configured", "warning")
                return False

        elif jd_method == "api":
            pkg = {
                "autostart": True,
                "links": "\n".join(links),
                "packageName": package_name[:50],
            }
            if destination:
                pkg["destinationFolder"] = destination
            payload = [pkg]
            # Try the cached connection first; if it fails (e.g. a stale device
            # handle after JD restarted or the session expired), drop the cache
            # and retry once with a fresh forced reconnect so a single grab can
            # self-heal instead of failing for the whole connection TTL.
            for attempt in (1, 2):
                try:
                    device = self._connect_jd_device(force=(attempt == 2))
                    device.linkgrabber.add_links(payload)
                    self._log(
                        f"Sent to JDownloader API: package {package_name[:50]!r}, "
                        f"{len(links)} link(s) (attempt {attempt})",
                        "success",
                    )
                    return True
                except Exception as e:
                    self._invalidate_jd_cache()
                    if attempt == 2:
                        self._log(f"JD API error: {e}", "error")
                        return False
                    self._log(f"JD API send failed ({e}); reconnecting and retrying", "warning")

        return False

    def _connect_jd_device(self, *, force: bool = False):
        """Connect to MyJDownloader and return the configured device object.

        Reuses a cached connection for up to ``_JD_CONN_TTL`` seconds so the
        background results poller doesn't re-authenticate on every cycle. Pass
        ``force=True`` to bypass the cache (e.g. an explicit connection test).

        Raises on failure (missing creds, bad login, or device not found).
        """
        with self._jd_lock:
            if (not force and self._jd_device is not None
                    and (time.monotonic() - self._jd_conn_ts) < self._JD_CONN_TTL):
                return self._jd_device

            import myjdapi
            email = self.config.get("jd_email", "")
            password = self.config.get("jd_password", "")
            if not email or not password:
                raise RuntimeError("MyJDownloader email/password not configured")
            jd = myjdapi.Myjdapi()
            jd.connect(email, password)
            jd.update_devices()
            device_name = self.config.get("jd_device", "")
            if device_name:
                device = jd.get_device(device_name)
            else:
                devices = jd.list_devices()
                if not devices:
                    raise RuntimeError("No JDownloader devices found on this account")
                first = devices[0]
                name = first.get("name") if isinstance(first, dict) else getattr(first, "name", None)
                device = jd.get_device(name)

            self._jd = jd
            self._jd_device = device
            self._jd_conn_ts = time.monotonic()
            return device

    def _invalidate_jd_cache(self):
        """Drop the cached MyJDownloader connection so the next call reconnects."""
        with self._jd_lock:
            self._jd = None
            self._jd_device = None
            self._jd_conn_ts = 0.0

    def test_jd_connection(self) -> dict:
        """Quick MyJDownloader connectivity check for the UI status indicator."""
        if self.config.get("jd_method") != "api":
            return {"connected": False, "error": "JDownloader method is not set to MyJDownloader API"}
        try:
            device = self._connect_jd_device(force=True)
            name = getattr(device, "name", None) or self.config.get("jd_device", "")
            return {"connected": True, "device": name}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ── Title resolution (shared by status + results poller) ───────────

    def _scraped_titles_normalized(self) -> Dict[str, dict]:
        """Return the scrape map keyed by *normalized* URL for robust matching.

        Falls back to an empty map if the DB lookup fails so a transient DB
        error never blanks out every title.
        """
        try:
            raw = self.db.get_scraped_link_titles() if self.db else {}
        except Exception as e:
            logger.warning("scraped_link_map lookup failed: %s", e)
            return {}
        out: Dict[str, dict] = {}
        for link, meta in (raw or {}).items():
            key = _normalize_link_url(link)
            if key:
                out[key] = meta
        return out

    @staticmethod
    def _resolve_title(pkg_name: str, child_links: List[dict], norm_titles: Dict[str, dict]) -> str:
        """Resolve a package's real movie/show title.

        Prefers ScanHound's scrape map (URL → real title, matched on the
        normalized URL); otherwise falls back to the raw JD package name. JD
        package names are frequently the obfuscated archive filename, which
        cannot be reverse-engineered, so the raw name is the honest fallback.
        """
        for link in child_links:
            mapped = norm_titles.get(_normalize_link_url(link.get("url") or ""))
            if mapped and mapped.get("title"):
                res = mapped.get("resolution")
                return f"{mapped['title']} [{res}]" if res else mapped["title"]
        return pkg_name

    def get_jd_status(self) -> dict:
        """Live snapshot of the JDownloader LinkGrabber + Downloads list,
        grouped into packages (mirroring JDownloader's own package view).

        Each package carries its real title, aggregate online/broken/byte
        counts, and its child links (availability + stage) so the UI can show a
        collapsible package with its parts inside.
        """
        try:
            device = self._connect_jd_device()
        except Exception as e:
            return {
                "connected": False, "error": str(e), "links": [], "packages": [],
                "online": 0, "offline": 0, "total": 0, "package_count": 0,
            }

        # Map packageUUID -> package name. The app sends links with the package
        # named after the movie/show (e.g. "Magellan [4K]"), so this tells us
        # which title a broken/blocked link belongs to.
        pkg_names: Dict[Any, str] = {}
        for grabber in (device.linkgrabber, device.downloads):
            try:
                for pkg in (grabber.query_packages([{"name": True, "uuid": True}]) or []):
                    pkg_names[pkg.get("uuid")] = pkg.get("name", "")
            except Exception as e:
                logger.warning("JD package query failed: %s", e)

        norm_titles = self._scraped_titles_normalized()

        # Collect raw child links per package UUID, preserving first-seen order.
        raw_by_pkg: Dict[Any, List[dict]] = {}
        order: List[Any] = []

        def _bucket(uuid) -> List[dict]:
            bucket = raw_by_pkg.get(uuid)
            if bucket is None:
                bucket = []
                raw_by_pkg[uuid] = bucket
                order.append(uuid)
            return bucket

        try:
            for link in (device.linkgrabber.query_links([{
                "availability": True, "name": True, "host": True,
                "bytesTotal": True, "packageUUID": True, "url": True,
            }]) or []):
                _bucket(link.get("packageUUID")).append({**link, "_origin": "linkgrabber"})
        except Exception as e:
            logger.warning("JD linkgrabber query failed: %s", e)
        try:
            for link in (device.downloads.query_links([{
                "name": True, "host": True, "bytesTotal": True, "bytesLoaded": True,
                "finished": True, "status": True, "packageUUID": True, "url": True,
            }]) or []):
                _bucket(link.get("packageUUID")).append({**link, "_origin": "downloads"})
        except Exception as e:
            logger.warning("JD downloads query failed: %s", e)

        packages: List[dict] = []
        total = online = offline = 0
        for uuid in order:
            raw = raw_by_pkg[uuid]
            disp_links: List[dict] = []
            p_online = p_offline = 0
            bytes_total = bytes_loaded = 0
            host = ""
            for link in raw:
                if link["_origin"] == "downloads":
                    status = link.get("status") or ""
                    low = status.lower()
                    broken = any(k in low for k in ("offline", "not found", "blocked", "error", "failed"))
                    availability = "OFFLINE" if broken else "ONLINE"
                    stage = "finished" if link.get("finished") else "downloading"
                else:
                    status = ""
                    availability = link.get("availability", "UNKNOWN")
                    stage = "linkgrabber"
                bt = link.get("bytesTotal", 0) or 0
                bl = link.get("bytesLoaded", 0) or 0
                bytes_total += bt
                bytes_loaded += bl
                host = host or link.get("host", "")
                if availability == "ONLINE":
                    p_online += 1
                elif availability == "OFFLINE":
                    p_offline += 1
                disp_links.append({
                    "name": link.get("name", ""),
                    "host": link.get("host", ""),
                    "availability": availability,
                    "bytes": bt,
                    "bytesLoaded": bl,
                    "stage": stage,
                    "status": status,
                })

            # Broken links first within the package.
            disp_links.sort(key=lambda l: 0 if l["availability"] == "OFFLINE" else (1 if l["availability"] != "ONLINE" else 2))
            stages = {l["stage"] for l in disp_links}
            if stages == {"finished"}:
                agg_stage = "finished"
            elif "downloading" in stages:
                agg_stage = "downloading"
            elif stages == {"linkgrabber"}:
                agg_stage = "linkgrabber"
            else:
                agg_stage = "mixed"

            packages.append({
                "uuid": str(uuid),
                "name": pkg_names.get(uuid, "") or "(unnamed package)",
                "title": self._resolve_title(pkg_names.get(uuid, ""), raw, norm_titles),
                "host": host,
                "total": len(disp_links),
                "online": p_online,
                "offline": p_offline,
                "bytes_total": bytes_total,
                "bytes_loaded": bytes_loaded,
                "stage": agg_stage,
                "links": disp_links,
            })
            total += len(disp_links)
            online += p_online
            offline += p_offline

        # Surface packages with broken links first, then alphabetically.
        packages.sort(key=lambda p: (0 if p["offline"] > 0 else 1, (p["title"] or p["name"]).lower()))

        MAX_PACKAGES = 300
        truncated = len(packages) > MAX_PACKAGES
        state = self._normalize_run_state_from(device)
        return {
            "connected": True, "state": state,
            "total": total, "online": online, "offline": offline,
            "package_count": len(packages), "truncated": truncated,
            "packages": packages[:MAX_PACKAGES],
        }

    @staticmethod
    def _normalize_run_state_from(device) -> str:
        """Map JDownloader's raw download-controller state to running/paused/stopped."""
        try:
            raw = str(device.downloadcontroller.get_current_state() or "").upper()
        except Exception as e:
            logger.warning("JD state query failed: %s", e)
            return "unknown"
        if "RUN" in raw:
            return "running"
        if "PAUSE" in raw:
            return "paused"
        if "STOP" in raw or "IDLE" in raw:
            return "stopped"
        return raw.lower() or "unknown"

    def get_jd_state(self) -> dict:
        """Lightweight connectivity + download-queue run-state check.

        A cheap alternative to get_jd_status() for frequent polling: a single
        downloadcontroller RPC instead of fetching the full linkgrabber/downloads
        link lists, which can be megabytes on accounts with a large history.
        """
        try:
            device = self._connect_jd_device()
        except Exception as e:
            return {"connected": False, "error": str(e), "state": "unknown"}
        return {"connected": True, "state": self._normalize_run_state_from(device)}

    def jd_control(self, action: str) -> dict:
        """Control JDownloader's global download queue.

        action: 'start' | 'stop' | 'pause' | 'resume'. Returns {ok, state} or {ok: False, error}.
        """
        action = (action or "").lower().strip()
        try:
            device = self._connect_jd_device()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        try:
            dc = device.downloadcontroller
            if action == "start":
                dc.start_downloads()
            elif action == "stop":
                dc.stop_downloads()
            elif action == "pause":
                dc.pause_downloads(True)
            elif action == "resume":
                dc.pause_downloads(False)
            else:
                return {"ok": False, "error": f"Unknown action: {action}"}
            self._log(f"JDownloader: {action} downloads", "info")
            return {"ok": True, "action": action, "state": self._normalize_run_state_from(device)}
        except Exception as e:
            self._log(f"JD control ({action}) failed: {e}", "error")
            self._invalidate_jd_cache()
            return {"ok": False, "error": str(e)}

    def poll_results(self, record: bool = True) -> List[Dict[str, Any]]:
        """Poll JDownloader's Downloads list, derive each package's download +
        extraction outcome, and optionally persist it to the DB.

        Returns a list of per-package result dicts. Safe to call when JD is
        unreachable (returns []).
        """
        try:
            device = self._connect_jd_device()
        except Exception:
            return []

        # Title cross-reference: clipboard adds get JD's filename-based package
        # name, but our scrape map knows the real movie/show title.
        norm_titles = self._scraped_titles_normalized()

        try:
            packages = device.downloads.query_packages([{
                "name": True, "uuid": True, "bytesLoaded": True,
                "bytesTotal": True, "finished": True, "status": True,
                "saveTo": True,
            }]) or []
        except Exception as e:
            logger.warning("JD package poll failed: %s", e)
            self._invalidate_jd_cache()
            return []

        try:
            links = device.downloads.query_links([{
                "packageUUID": True, "host": True, "url": True, "name": True,
                "finished": True, "status": True, "extractionStatus": True,
                "bytesTotal": True, "bytesLoaded": True,
            }]) or []
        except Exception as e:
            logger.warning("JD link poll failed: %s", e)
            links = []

        by_pkg: Dict[Any, List[dict]] = {}
        for link in links:
            by_pkg.setdefault(link.get("packageUUID"), []).append(link)

        def _agg_extraction(child_links) -> str:
            statuses = [str(l.get("extractionStatus") or "").upper() for l in child_links]
            statuses = [s for s in statuses if s]
            if not statuses:
                return "na"
            if any("ERROR" in s for s in statuses):
                return "error"
            if any(s in ("RUNNING", "EXTRACTING", "QUEUED") for s in statuses):
                return "running"
            if all("SUCCESS" in s for s in statuses):
                return "success"
            return "running"

        results: List[Dict[str, Any]] = []
        for pkg in packages:
            name = pkg.get("name") or "(unnamed package)"
            child_links = by_pkg.get(pkg.get("uuid"), [])
            bytes_total = pkg.get("bytesTotal") or 0
            bytes_loaded = pkg.get("bytesLoaded") or 0
            downloaded = bool(pkg.get("finished")) or (bytes_total > 0 and bytes_loaded >= bytes_total)
            host = next((l.get("host", "") for l in child_links if l.get("host")), "")
            title = self._resolve_title(name, child_links, norm_titles)
            # Keep a once-resolved real title even if the scrape map transiently
            # misses on a later poll (don't regress to the raw JD package name).
            if title and title != name:
                self._best_titles[name] = title
            elif name in self._best_titles:
                title = self._best_titles[name]

            statuses = [str(l.get("status") or "").lower() for l in child_links]
            all_status = " ".join(statuses + [str(pkg.get("status") or "").lower()])
            error = None
            if any(k in all_status for k in ("offline", "not found", "error", "failed", "blocked")):
                error = pkg.get("status") or next(
                    (l.get("status") for l in child_links if l.get("status")), "Download error"
                )

            extraction = _agg_extraction(child_links)
            # A package of only direct media files (no .rar/.zip/...) has nothing
            # to extract, so once downloaded it is complete — otherwise it sits at
            # "downloaded" forever waiting for an extraction that never runs.
            has_archive = any(_is_archive_name(l.get("name") or "") for l in child_links)
            if extraction == "error":
                state = "failed"
            elif error and not downloaded:
                state = "failed"
            elif extraction == "success":
                state = "extracted"
            elif extraction == "running":
                state = "extracting"
            elif downloaded and child_links and not has_archive:
                state = "extracted"
            elif downloaded:
                state = "downloaded"
            elif bytes_loaded > 0:
                state = "downloading"
            else:
                state = "queued"

            row = {
                "name": name, "title": title, "host": host,
                "bytes_total": bytes_total, "bytes_loaded": bytes_loaded,
                "downloaded": 1 if downloaded else 0,
                "extraction": extraction, "state": state, "error": error,
                # saveTo (extracted output folder) — consumed by the auto-rename
                # hook when the package reaches the "extracted" state.
                "save_to": pkg.get("saveTo") or "",
            }
            results.append(row)

            if record and self.db:
                change_key = (state, bytes_loaded, extraction, row["downloaded"], error, title)
                if self._results_cache.get(name) != change_key:
                    self._results_cache[name] = change_key
                    try:
                        self.db.upsert_download_result(**row)
                    except Exception as e:
                        logger.debug("upsert_download_result failed: %s", e)

        # Bound the per-package caches to packages currently in JD's list so they
        # don't grow without limit over the long-lived poller's lifetime. Only
        # prunes after a successful poll (early returns above skip this), so a
        # transient JD blip never discards resolved titles.
        live_names = {r["name"] for r in results}
        self._results_cache = {k: v for k, v in self._results_cache.items() if k in live_names}
        self._best_titles = {k: v for k, v in self._best_titles.items() if k in live_names}

        return results

    # ── WebDriver ─────────────────────────────────────────────────────

    def _detect_chrome_major(self) -> Optional[int]:
        """Detect the installed Chrome/Chromium major version, cross-platform.

        Returns the major version int (e.g. 149) or None if undetermined.
        Windows reads the registry; Linux/macOS query the browser binary's
        ``--version`` output. Passing this to undetected-chromedriver as
        ``version_main`` keeps it from fetching a newer (mismatched) driver.
        """
        if sys.platform.startswith("win"):
            try:
                import winreg
                reg_path = r"SOFTWARE\Google\Chrome\BLBeacon"
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, reg_path) as key:
                            ver_str, _ = winreg.QueryValueEx(key, "version")
                            return int(ver_str.split(".")[0])
                    except OSError:
                        continue
            except Exception:
                pass
            return None
        # Linux/macOS: ask the browser binary directly.
        import re
        import shutil
        candidates = [
            os.environ.get("CHROME_BIN"),
            "chromium", "chromium-browser",
            "google-chrome", "google-chrome-stable", "chrome",
        ]
        for cand in candidates:
            if not cand:
                continue
            binary = shutil.which(cand) or (cand if os.path.exists(cand) else None)
            if not binary:
                continue
            try:
                out = subprocess.run(
                    [binary, "--version"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                match = re.search(r"(\d+)\.\d+\.\d+", out)
                if match:
                    return int(match.group(1))
            except Exception:
                continue
        return None

    def driver_preflight(self) -> None:
        """Log the detected browser version at startup.

        A Chrome/Chromium <-> ChromeDriver version drift silently breaks ALL
        scraping (SessionNotCreatedException on every grab). Surfacing the
        detected version — or a warning when it can't be detected — at boot
        makes that class of failure visible immediately instead of only when a
        grab is attempted.
        """
        major = self._detect_chrome_major()
        if major:
            self._log(f"Scraper preflight: detected browser major version {major}", "info")
        else:
            self._log(
                "Scraper preflight: could NOT detect the browser version — "
                "undetected-chromedriver may fetch a mismatched driver and break scraping.",
                "warning",
            )

    def get_driver(self):
        """Get or create a cached WebDriver instance (thread-safe)."""
        _ensure_selenium()
        with self._driver_lock:
            if self.cached_driver:
                try:
                    _ = self.cached_driver.title
                    return self.cached_driver
                except Exception:
                    try:
                        self.cached_driver.quit()
                    except Exception:
                        pass
                    self.cached_driver = None

            options = _uc.ChromeOptions()
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--start-minimized")
            # Detect the installed Chrome/Chromium major version so
            # undetected-chromedriver fetches a *matching* driver. Without it,
            # uc grabs the latest driver, which fails when the installed browser
            # lags (e.g. the container pins Chromium 149 but uc pulled
            # ChromeDriver 150 -> SessionNotCreatedException, breaking all scrapes).
            chrome_ver = self._detect_chrome_major()
            if chrome_ver:
                logger.debug("Detected Chrome major version %s", chrome_ver)
            else:
                logger.warning(
                    "Could not detect Chrome version; undetected-chromedriver "
                    "will guess a driver and may mismatch the browser."
                )
            # Target the bundled/pinned browser binary when present (Linux/Docker).
            chrome_bin = os.environ.get("CHROME_BIN")
            if chrome_bin and os.path.exists(chrome_bin):
                options.binary_location = chrome_bin
            # On Linux/Docker, use the apt-installed chromedriver — always
            # version-matched to the apt chromium, so uc never downloads a
            # mismatched driver (belt-and-suspenders behind version_main
            # detection). Windows desktop keeps uc's auto-managed driver: no
            # such system path exists there.
            uc_kwargs: Dict[str, Any] = {"options": options, "version_main": chrome_ver}
            system_driver = "/usr/bin/chromedriver"
            if os.path.exists(system_driver):
                uc_kwargs["driver_executable_path"] = system_driver
            self.cached_driver = _uc.Chrome(**uc_kwargs)
            try:
                # Cosmetic on the Windows desktop app; unsupported under the
                # container's headless Xvfb display, so make it best-effort.
                self.cached_driver.minimize_window()
            except Exception:
                pass
            return self.cached_driver

    def cleanup_driver(self):
        """Quit and clean up the cached Chrome driver (thread-safe).

        Waits for any active scrape operations to finish before quitting.
        """
        # Wait for active scrapes using the count lock (not driver lock)
        with self._scrape_count_lock:
            deadline = time.monotonic() + 180
            while self._active_scrapes > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("Timed out waiting for %d active scrape(s)", self._active_scrapes)
                    break
                self._scrapes_done.wait(timeout=remaining)
        # Now acquire driver lock to safely quit
        with self._driver_lock:
            if self.cached_driver:
                try:
                    self.cached_driver.quit()
                except Exception:
                    pass
                finally:
                    self.cached_driver = None

    def _recycle_driver(self) -> None:
        """Quit and drop the cached browser so the next get_driver() builds a fresh one.

        Unlike cleanup_driver() this does NOT wait for active scrapes to finish —
        it is called from *inside* a scrape that already holds the (reentrant)
        driver lock, so waiting would deadlock.
        """
        with self._driver_lock:
            if self.cached_driver:
                try:
                    self.cached_driver.quit()
                except Exception:
                    pass
                finally:
                    self.cached_driver = None

    def _browser_error_code(self, driver, url: str) -> Optional[str]:
        """Return Chrome's ``ERR_*`` code if the browser is showing its OWN
        network-error page rather than the site, else ``None``.

        Chromium in the container intermittently cannot resolve/connect (Docker's
        embedded DNS), and renders an instant error page whose <title> is the bare
        hostname with zero anchors. That page contains no Cloudflare markers, so
        the old code mistook it for "a Cloudflare wall or changed layout" and
        reported "no links found" — failing every grab until the container was
        restarted. Detecting it lets us recycle the browser and retry.
        """
        _ensure_selenium()
        try:
            if not driver.find_elements(_By.CSS_SELECTOR, "#main-frame-error"):
                # Fallback signature: Chrome titles a neterror page with the bare
                # host. Require zero anchors so a real page can't false-positive.
                host = (urlparse(url).netloc or "").lower()
                title = (driver.title or "").strip().lower()
                if not (host and title == host
                        and not driver.find_elements(_By.CSS_SELECTOR, "a[href]")):
                    return None
        except Exception:
            return None
        try:
            text = driver.find_element(_By.TAG_NAME, "body").text
        except Exception:
            text = ""
        if not isinstance(text, str):
            text = ""
        match = re.search(r"ERR_[A-Z_]+", text)
        return match.group(0) if match else "ERR_UNKNOWN"

    def _navigate(self, url: str, tag: str = "Scrape", attempts: int = 3):
        """Load ``url``, healing the browser if it serves its own error page.

        Returns the live driver, or ``None`` if the host is genuinely unreachable
        after ``attempts`` tries. Each retry recycles the browser, because a fresh
        Chrome usually resolves the host fine even when the previous one couldn't.
        """
        last: Optional[str] = None
        for attempt in range(1, attempts + 1):
            driver = self.get_driver()
            try:
                driver.get(url)
            except Exception as e:
                last = str(e)
                self._log(f"[{tag}] navigation raised (attempt {attempt}/{attempts}): {e}", "warning")
            else:
                code = self._browser_error_code(driver, url)
                if not code:
                    return driver
                last = code
                self._log(
                    f"[{tag}] browser could not reach the site ({code}) — a network/DNS "
                    f"error, NOT a Cloudflare wall. Recycling the browser and retrying "
                    f"({attempt}/{attempts}).",
                    "warning",
                )
            self._recycle_driver()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 5))
        self._log(f"[{tag}] giving up — {url} unreachable from the container ({last})", "error")
        return None

    def _log_page_diagnostics(self, driver, keyword: Optional[str] = None) -> None:
        """Emit detailed diagnostics about the current page to debug empty scrapes."""
        try:
            from bs4 import BeautifulSoup
            html = driver.page_source or ""
            soup = BeautifulSoup(html, 'html.parser')
            anchors = soup.find_all('a', href=True)
            self._log(f"[HDEncode][diag] {len(anchors)} links, {len(html)} bytes of HTML")

            # The visible text is the single most useful signal for *why* a scrape
            # came back empty — it distinguishes a Cloudflare/bot challenge, a
            # login wall, and a Chrome network-error page ("This site can't be
            # reached") from an actual layout change. Log a short snippet.
            body_text = " ".join((soup.get_text(" ") or "").split())[:240]
            self._log(f"[HDEncode][diag] visible text: {body_text!r}")

            # Chrome's own error page (host unreachable / timed out) renders with a
            # real <title> of the host but no usable content — call it out plainly
            # so it isn't mistaken for a site layout change.
            err_low = body_text.lower()
            if any(m in err_low for m in (
                "site can't be reached", "site can’t be reached", "took too long to respond",
                "err_", "no internet", "dns_probe", "connection was reset",
            )):
                self._log(
                    "[HDEncode][diag] this is a browser NETWORK-ERROR page, not the site — "
                    "the source host was unreachable from the container (DNS/blocked/down).",
                    "warning",
                )
            elif len(anchors) == 0 and len(html) > 40000:
                # A large document with zero anchors is the signature of an
                # unsolved Cloudflare/bot challenge (JS-only shell), NOT a
                # renamed button — say so instead of guessing "layout change".
                self._log(
                    "[HDEncode][diag] large HTML but ZERO anchors — almost certainly an "
                    "unsolved Cloudflare/bot challenge (undetected-chromedriver didn't clear it).",
                    "warning",
                )

            hosts = ("rapidgator", "nitroflare", "1fichier", "ddownload")
            host_links = [a['href'] for a in anchors if any(h in a['href'].lower() for h in hosts)]
            if host_links:
                self._log(f"[HDEncode][diag] file-host links on page ({len(host_links)}): {host_links[:5]}")
            else:
                sample = [a['href'] for a in anchors[:15]]
                self._log(f"[HDEncode][diag] no file-host links; sample hrefs: {sample}")

            # Candidate "access the links" controls (buttons/inputs/anchors)
            candidates = []
            for el in soup.find_all(['button', 'input', 'a']):
                label = (el.get('value') or el.get_text() or '').strip()
                if not label:
                    continue
                low_l = label.lower()
                if any(k in low_l for k in (
                    'access', 'download', 'link', 'get ', 'show', 'reveal', 'unlock', 'continue',
                )):
                    candidates.append(f"{el.name}={label[:40]!r}")
            if candidates:
                self._log(f"[HDEncode][diag] possible access controls: {candidates[:10]}")
            else:
                self._log("[HDEncode][diag] no access/download/link controls found on page", "warning")

            forms = [(f.get('action') or '(no action)') for f in soup.find_all('form')]
            if forms:
                self._log(f"[HDEncode][diag] forms: {forms[:5]}")
            iframes = [i.get('src', '') for i in soup.find_all('iframe') if i.get('src')]
            captcha_frames = [s for s in iframes if any(
                k in s.lower() for k in ('turnstile', 'challenges.cloudflare', 'recaptcha', 'hcaptcha', 'captcha')
            )]
            if captcha_frames:
                self._log(f"[HDEncode][diag] CAPTCHA/Turnstile iframes present: {captcha_frames}", "warning")

            low = html.lower()
            markers = [m for m in (
                "just a moment", "cf-chl", "cloudflare", "checking your browser",
                "log in", "sign in", "captcha", "verify you are human", "access denied",
            ) if m in low]
            if markers:
                self._log(f"[HDEncode][diag] page markers detected: {markers}", "warning")
            if keyword:
                self._log(f"[HDEncode][diag] keyword '{keyword}' present in HTML: {keyword in low}")
        except Exception as e:
            self._log(f"[HDEncode][diag] failed to gather diagnostics: {e}", "warning")

    def _wait_past_cloudflare(self, driver, timeout: int = 30) -> None:
        """Wait for a Cloudflare interstitial ("Just a moment…") to clear.

        undetected_chromedriver solves the JS/Turnstile challenge on its own,
        but it needs a few seconds.  We poll until the challenge markers are
        gone (or the timeout elapses) so the subsequent element search doesn't
        fire while the challenge page is still up — which previously made the
        scrape return zero links the moment Cloudflare engaged.
        """
        deadline = time.monotonic() + timeout
        announced = False
        while time.monotonic() < deadline:
            try:
                title = (driver.title or "").lower()
                src = (driver.page_source or "").lower()
            except Exception:
                return
            challenge = (
                "just a moment" in title
                or "attention required" in title
                or "checking your browser" in src
                or "cf-chl" in src
                or "challenges.cloudflare.com" in src
            )
            if not challenge:
                return
            if not announced:
                self._log("[Scrape] Cloudflare challenge detected — waiting for it to clear...")
                announced = True
            time.sleep(1)
        self._log("[Scrape] Cloudflare challenge did not clear within timeout", "warning")

    def scrape_links(self, url: str, service_type: str, progress_callback: Optional[Callable] = None) -> List[str]:
        """Scrape download links from a page using WebDriver.

        Args:
            url: Page URL to scrape
            service_type: "Rapidgator" or "Nitroflare"

        Returns:
            List of download link URLs.
        """
        _ensure_selenium()
        from bs4 import BeautifulSoup

        # Track active scrapes separately from driver access
        with self._scrape_count_lock:
            self._active_scrapes += 1

        try:
            with self._driver_lock:
                if "ddlbase.com" in url:
                    return self._scrape_ddlbase_links(url, progress_callback=progress_callback)
                if "adit-hd.com" in url:
                    return self._scrape_adithd_links(url, service_type)

                # Default: HDEncode. Map the requested host to its link keyword.
                # The old `== "Rapidgator" else "nitroflare"` silently searched
                # nitroflare for ANY other value (1fichier/ddownload/lowercase).
                _host_keywords = {"rapidgator": "rapidgator", "nitroflare": "nitroflare",
                                  "1fichier": "1fichier", "ddownload": "ddownload"}
                keyword = _host_keywords.get((service_type or "").strip().lower())
                if keyword is None:
                    self._log(f"[HDEncode] Unknown host '{service_type}', defaulting to rapidgator", "warning")
                    keyword = "rapidgator"
                try:
                    self._log(f"[HDEncode] Loading page ({service_type}): {url}")
                    # Navigates with browser-error-page detection + recycle/retry,
                    # so a transient container DNS/connect failure doesn't silently
                    # look like "no links on the page".
                    driver = self._navigate(url, tag="HDEncode")
                    if driver is None:
                        return []

                    # Let any Cloudflare "Just a moment…" challenge resolve
                    # before we look for page elements.
                    self._wait_past_cloudflare(driver)

                    try:
                        page_title = driver.title
                    except Exception:
                        page_title = "?"
                    self._log(f"[HDEncode] Page loaded (title: {page_title!r})")

                    # HDEncode renamed the reveal button to "View links" (it was
                    # "Access the links"); clicking it POSTs a form that unlocks
                    # the file-host links on the same page (#unlocked).
                    # The generic //input[@type='submit'] fallback is deliberately
                    # gone — it matched the unrelated "Report content" button.
                    access_xpath = (
                        "//input[@value='View links'] | "
                        "//input[contains(@value, 'View link')] | "
                        "//input[@value='Access the links'] | "
                        "//input[contains(@value, 'Access')] | "
                        "//button[contains(text(), 'View link')] | "
                        "//button[contains(text(), 'Access')]"
                    )
                    self._log("[HDEncode] Looking for the 'View links' button...")
                    access_btn = None
                    try:
                        access_btn = _WebDriverWait(driver, 10).until(
                            _EC.element_to_be_clickable((_By.XPATH, access_xpath))
                        )
                    except Exception:
                        # Fallback: any submit/button whose label mentions "link"
                        # but is NOT the "Report content" button.
                        try:
                            for el in driver.find_elements(
                                _By.CSS_SELECTOR,
                                "form input[type='submit'], form button[type='submit'], form button",
                            ):
                                label = (el.get_attribute("value") or el.text or "").lower()
                                if "link" in label and "report" not in label:
                                    access_btn = el
                                    self._log(f"[HDEncode] Fallback matched control: {label!r}")
                                    break
                        except Exception:
                            access_btn = None

                    if not access_btn:
                        self._log(
                            f"[HDEncode] No 'View links' button found (title: {page_title!r}). "
                            "Page may be a Cloudflare wall, login gate, or changed layout.",
                            "warning",
                        )
                        self._log_page_diagnostics(driver)
                        return []

                    try:
                        btn_desc = access_btn.get_attribute("value") or access_btn.text or access_btn.tag_name
                    except Exception:
                        btn_desc = "?"
                    self._log(f"[HDEncode] Access control found ({btn_desc!r}) — clicking")
                    driver.execute_script("arguments[0].scrollIntoView();", access_btn)
                    time.sleep(0.3)
                    driver.execute_script("arguments[0].click();", access_btn)

                    self._log(f"[HDEncode] Clicked — waiting up to 8s for '{keyword}' links to appear")
                    try:
                        _WebDriverWait(driver, 8).until(
                            _EC.presence_of_element_located((_By.XPATH, f"//a[contains(@href, '{keyword}')]"))
                        )
                    except Exception:
                        self._log(f"[HDEncode] No {service_type} links appeared after clicking", "warning")
                        self._log_page_diagnostics(driver, keyword=keyword)
                        return []

                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                    seen: Set[str] = set()
                    links: List[str] = []
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if keyword in href.lower() and href not in seen:
                            seen.add(href)
                            links.append(href)

                    if links:
                        self._log(f"[HDEncode] Found {len(links)} {service_type} link(s); first: {links[0]}", "success")
                    else:
                        self._log(f"[HDEncode] 0 {service_type} links parsed from the page", "warning")
                        self._log_page_diagnostics(driver, keyword=keyword)
                    return links

                except Exception as e:
                    self._log(f"[HDEncode] Error scraping {url}: {e}", "error")
                    return []
        finally:
            with self._scrape_count_lock:
                self._active_scrapes -= 1
                self._scrapes_done.notify_all()

    def _scrape_ddlbase_links(self, url: str, progress_callback: Optional[Callable] = None) -> List[str]:
        """Scrape download links from DDLBase post page.

        DDLBase encodes shortlinks in ``ddllk`` attributes on ``a.boolk``
        elements using XOR encryption (key: ``mySecret123``) + base64.
        Mirror 1 links (cuty.io/cuttlinks.com) resolve to 1fichier.com.
        """
        _ensure_selenium()
        from bs4 import BeautifulSoup

        try:
            self._log(f"[DDLBase] Scraping links from: {url}")
            driver = self._navigate(url, tag="DDLBase")
            if driver is None:
                return []
            # DDLBase is Cloudflare-protected; wait for any "Just a moment…"
            # challenge to clear before parsing (the HDEncode path does the
            # same), then let the page JS render the boolk shortlink tags.
            self._wait_past_cloudflare(driver)
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')

            # DDLBase uses <a class="boolk" ddllk="..."> with XOR-encrypted URLs
            shortlinks = []
            boolk_tags = soup.select('a.boolk[ddllk]')

            if boolk_tags:
                for tag in boolk_tags:
                    encoded = tag.get('ddllk', '')
                    if not encoded:
                        continue
                    decoded_url = decode_ddlbase_link(encoded)
                    if decoded_url:
                        self._log(f"[DDLBase] Decoded {tag.get('id', '?')}: {decoded_url}")
                        shortlinks.append(decoded_url)
                        self._progress("download:resolving", {"title": url, "resolved": len(shortlinks), "total": len(boolk_tags)}, _cb=progress_callback)

            # Fallback: look for standard <a href> shortlinks
            if not shortlinks:
                body = soup.body or soup
                for a_tag in body.find_all('a', href=True):
                    href = a_tag['href']
                    if self._is_ddlbase_shortlink(href):
                        shortlinks.append(href)

            # Also check for direct 1fichier/rapidgator links
            direct_links = self._extract_supported_download_links(str(soup.body or soup))

            if not shortlinks and not direct_links:
                self._log("[DDLBase] No shortlinks or download links found", "warning")
                self._log_page_diagnostics(driver)
                return []

            self._log(f"[DDLBase] Found {len(shortlinks)} shortlinks, {len(direct_links)} direct links")

            # Only resolve Mirror 1 (cuty.io / cuttlinks.com) — others can't be auto-resolved
            resolvable = [s for s in shortlinks if _url_matches_domain(s, _AUTOMATABLE_SHORTLINK_DOMAINS)]
            resolved = list(direct_links)
            if shortlinks and not resolvable and not direct_links:
                self._log(
                    f"[DDLBase] Decoded {len(shortlinks)} shortlink(s) but none are "
                    "auto-resolvable (only cuty.io / cuttlinks.com are) — no links delivered",
                    "warning",
                )

            for short_url in dict.fromkeys(resolvable):
                try:
                    final_url = self._resolve_cuttlinks_shortlink(
                        driver, short_url, progress_callback=progress_callback
                    )
                    if final_url and final_url not in resolved:
                        resolved.append(final_url)
                except Exception as e:
                    self._log(f"[DDLBase] Failed to resolve shortlink {short_url}: {e}", "warning")

            if resolvable and not resolved:
                self._log(
                    f"[DDLBase] All {len(resolvable)} resolvable shortlink(s) failed "
                    "(timeout/captcha) — no links delivered",
                    "warning",
                )
            return resolved

        except Exception as e:
            self._log(f"[DDLBase] Error scraping links: {e}", "error")
            return []

    @staticmethod
    def _is_ddlbase_shortlink(url: str) -> bool:
        if not url:
            return False
        return _url_matches_domain(url, _DDLBASE_SHORTLINK_DOMAINS)

    @staticmethod
    def _is_supported_download_link(url: str) -> bool:
        if not url:
            return False
        if _url_matches_domain(url, _DDLBASE_SHORTLINK_DOMAINS):
            return False
        return _url_matches_domain(url, _SUPPORTED_DOWNLOAD_HOSTS)

    def _extract_supported_download_links(self, html: str) -> List[str]:
        """Extract known file-host URLs from HTML while preserving order."""
        from bs4 import BeautifulSoup

        if not html:
            return []

        soup = BeautifulSoup(html, 'html.parser')
        links = []
        seen = set()

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if href not in seen and self._is_supported_download_link(href):
                seen.add(href)
                links.append(href)

        return links

    def _resolve_cuttlinks_shortlink(self, driver, short_url: str, progress_callback: Optional[Callable] = None) -> Optional[str]:
        """Automate cuttlinks.com shortlink resolution to final 1fichier.com URL.

        Flow: Navigate → Click "Continue" → Cloudflare Turnstile → Wait countdown
        → Click "Go →" → Capture 1fichier.com redirect.

        Falls back to passive polling if automation fails at any step.
        """
        _ensure_selenium()
        from selenium.common.exceptions import (
            NoSuchElementException, TimeoutException, WebDriverException,
        )

        self._log(f"[Shortlink] Resolving: {short_url}")
        try:
            driver.get(short_url)
            time.sleep(2)
        except Exception as e:
            self._log(f"[Shortlink] Failed to navigate: {e}", "error")
            return None

        # The cuty.io / cuttlinks.com flow has up to 3 pages, each with a
        # #submit-button that starts disabled ("Please Wait ...") and gets
        # enabled by the vhit.js ad script.  After each submit the form POSTs
        # to the next step.  The final step has a countdown timer + "Go" btn.

        for step in range(1, 4):
            self._progress("download:shortlink_step", {"url": short_url, "step": step}, _cb=progress_callback)
            result = self._check_for_final_url(driver)
            if result:
                return result

            # --- Wait for #submit-button to become enabled (up to 30s) ---
            submit_btn = self._wait_for_submit_button(driver, timeout=30)
            if submit_btn:
                self._log(f"[Shortlink] Step {step}: clicking submit button")
                driver.execute_script("arguments[0].scrollIntoView();", submit_btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", submit_btn)
                time.sleep(3)
                continue

            # --- Look for "I am not a robot" button ---
            robot_btn = self._find_clickable_button(driver, [
                "//button[contains(text(),'not a robot')]",
                "//a[contains(text(),'not a robot')]",
                "//button[contains(text(),'Verify')]",
            ])
            if robot_btn:
                self._log(f"[Shortlink] Step {step}: clicking 'I am not a robot'")
                driver.execute_script("arguments[0].scrollIntoView();", robot_btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", robot_btn)
                time.sleep(3)
                continue

            # --- Wait for countdown timer + "Go" button ---
            go_btn = self._wait_for_go_button(driver, timeout=25)
            if go_btn:
                self._log(f"[Shortlink] Step {step}: clicking 'Go'")
                driver.execute_script("arguments[0].scrollIntoView();", go_btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", go_btn)
                time.sleep(3)
                continue

            self._log(f"[Shortlink] Step {step}: no actionable button found", "debug")
            break

        # --- Step 4: Wait for final redirect to 1fichier.com ---
        self._log("[Shortlink] Waiting for redirect to file host...")
        for _ in range(15):
            result = self._check_for_final_url(driver)
            if result:
                return result
            time.sleep(1)

        # --- Fallback: passive polling (manual completion) ---
        self._log("[Shortlink] Automation incomplete, waiting for manual completion...", "warning")
        fallback_seconds = max(5, int(self.config.get("ddlbase_manual_resolution_timeout", 60) or 60))
        for _ in range(fallback_seconds):
            result = self._check_for_final_url(driver)
            if result:
                return result
            time.sleep(1)

        self._log(f"[Shortlink] Timed out resolving: {short_url}", "warning")
        return None

    def _wait_for_submit_button(self, driver, timeout: int = 30):
        """Wait for #submit-button to become enabled (clickable).

        cuty.io / cuttlinks.com pages start the button as disabled with
        "Please Wait ..." text.  The vhit.js ad script enables it after
        verification passes.
        """
        _ensure_selenium()
        from selenium.common.exceptions import (
            NoSuchElementException, TimeoutException,
        )
        try:
            wait = _WebDriverWait(driver, timeout)
            btn = wait.until(_EC.element_to_be_clickable((_By.CSS_SELECTOR, "#submit-button")))
            if btn:
                self._log("[Shortlink] Submit button is now clickable")
                return btn
        except (TimeoutException, NoSuchElementException):
            pass

        # Fallback: any enabled submit button
        try:
            wait = _WebDriverWait(driver, 3)
            btn = wait.until(_EC.element_to_be_clickable((_By.CSS_SELECTOR, "form button[type='submit']:not([disabled])")))
            return btn
        except (TimeoutException, NoSuchElementException):
            pass

        return None

    def _find_clickable_button(self, driver, selectors: list):
        """Find the first visible, clickable button matching any selector."""
        from selenium.common.exceptions import NoSuchElementException
        for selector in selectors:
            try:
                btn = driver.find_element(_By.XPATH, selector)
                if btn and btn.is_displayed():
                    return btn
            except NoSuchElementException:
                continue
        return None

    def _wait_for_go_button(self, driver, timeout: int = 25):
        """Wait for the countdown timer to finish and the 'Go' button to appear."""
        from selenium.common.exceptions import NoSuchElementException
        self._log("[Shortlink] Waiting for countdown timer...")
        for _ in range(timeout):
            for selector in [
                "//button[normalize-space()='Go →']",
                "//a[normalize-space()='Go →']",
                "//button[normalize-space()='Go']",
                "//a[normalize-space()='Go']",
            ]:
                try:
                    btn = driver.find_element(_By.XPATH, selector)
                    if btn and btn.is_displayed():
                        btn_text = btn.text.strip().lower()
                        if 'wait' not in btn_text:
                            return btn
                except NoSuchElementException:
                    continue

            result = self._check_for_final_url(driver)
            if result:
                return None  # caller will detect via _check_for_final_url

            time.sleep(1)
        return None

    def _check_for_final_url(self, driver) -> Optional[str]:
        """Check if the browser has reached a supported download host."""
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""

        if self._is_supported_download_link(current_url):
            self._log(f"[Shortlink] Resolved to: {current_url}")
            return current_url

        # Also check page source for visible download links
        try:
            page_source = driver.page_source
        except Exception:
            page_source = ""

        visible_links = self._extract_supported_download_links(page_source)
        if visible_links:
            self._log(f"[Shortlink] Found link in page: {visible_links[0]}")
            return visible_links[0]

        return None

    def _scrape_adithd_links(self, url: str, service_type: str) -> List[str]:
        """Scrape download links from Adit-HD forum thread."""
        _ensure_selenium()

        try:
            self._log(f"[Adit-HD] Scraping links from: {url}")
            driver = self.get_driver()

            # Try to use the adithd source from registry
            try:
                from backend.sources.registry import get_registry
                import asyncio

                registry = get_registry()
                adithd = registry.get_source("adithd")
                if adithd:
                    if self.config.get("adithd_username") and self.config.get("adithd_password"):
                        adithd.set_credentials(
                            username=self.config.get("adithd_username", ""),
                            password=self.config.get("adithd_password", ""),
                            auto_reply=self.config.get("adithd_auto_reply", False),
                        )
                    adithd.set_driver(driver)

                    loop = asyncio.new_event_loop()
                    try:
                        try:
                            loop.run_until_complete(adithd.login())
                        except Exception as e:
                            self._log(f"[Adit-HD] Login error: {e}", "warning")

                        _, links = loop.run_until_complete(adithd.fetch_thread_content(url))
                    finally:
                        try:
                            loop.run_until_complete(loop.shutdown_asyncgens())
                        except Exception:
                            pass
                        loop.close()

                    if links:
                        # Filter by service type
                        raw_count = len(links)
                        keyword = service_type.lower() if service_type else ""
                        if keyword:
                            links = [l for l in links if keyword in l.lower()]

                        if links:
                            self._log(f"[Adit-HD] Found {len(links)} {service_type} links")
                            return links

                        # Plugin DID return links, just none for the requested host —
                        # say so accurately instead of "returned no links".
                        self._log(
                            f"[Adit-HD] Plugin returned {raw_count} link(s) but none for "
                            f"{service_type}; trying broad fallback scrape",
                            "warning",
                        )
                    else:
                        self._log("[Adit-HD] Plugin returned no links, trying fallback scrape")

            except ImportError:
                logger.debug("Adit-HD source registry not available")

            # Fallback: direct page scraping
            from bs4 import BeautifulSoup
            driver.get(url)
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            keyword = service_type.lower() if service_type else ""
            found = []
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if keyword and keyword in href.lower():
                    found.append(href)
                elif not keyword and ('rapidgator' in href.lower() or 'nitroflare' in href.lower() or '1fichier' in href.lower()):
                    found.append(href)

            self._log(f"[Adit-HD] Found {len(found)} links (fallback scrape)")
            return found

        except Exception as e:
            self._log(f"[Adit-HD] Error scraping links: {e}", "error")
            return []

    # ── Export ─────────────────────────────────────────────────────────

    @staticmethod
    def _csv_safe(value) -> str:
        """Sanitize a value for CSV export to prevent formula injection.

        Fields that start with =, +, -, @, tab, or CR are prefixed with a
        single quote so spreadsheet applications treat them as plain text.
        """
        s = str(value) if value is not None else ""
        if s and s[0] in ('=', '+', '-', '@', '\t', '\r'):
            return "'" + s
        return s

    def export_results_csv(self, items, filepath: Optional[str] = None) -> str:
        """Export scan results to CSV. Returns filepath."""
        if not filepath:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(os.getcwd(), f"scanhound_results_{timestamp}.csv")

        safe = self._csv_safe
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Status', 'Title', 'Year', 'Season', 'Resolution', 'Size', 'HDR', 'Plex Info', 'URL'])
            for item in items:
                writer.writerow([
                    safe(item.status.value), safe(item.title), item.year,
                    f"S{item.season:02d}" if item.season is not None else "-",
                    safe(item.resolution), safe(item.size), safe(item.hdr),
                    safe(item.plex_info), safe(item.url),
                ])

        return filepath

    # ── URL helpers ────────────────────────────────────────────────────

    @staticmethod
    def open_url(url: str) -> bool:
        """Open URL in the default browser. Returns True if a browser launched."""
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False

    @staticmethod
    def _build_plex_url(plex_url: str, server_id: str, rating_key: Any) -> Optional[str]:
        """Build a Plex Web details URL for a specific metadata rating key."""
        if not plex_url or rating_key is None:
            return None

        rating_key = str(rating_key).strip()
        if not rating_key:
            return None

        if server_id:
            return (
                f"{plex_url}/web/index.html#!/server/{server_id}/details"
                f"?key=%2Flibrary%2Fmetadata%2F{rating_key}"
            )
        return f"{plex_url}/web/index.html#!/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"

    @staticmethod
    def copy_to_clipboard(links: List[str]) -> bool:
        """Copy download links to clipboard. Returns True on success."""
        if not links:
            return False
        text = "\n".join(links)
        # Use Qt clipboard only from the main thread (COM requires it on Windows)
        import threading
        from PySide6.QtCore import QThread
        on_main = threading.current_thread() is threading.main_thread()
        if on_main:
            try:
                from PySide6.QtWidgets import QApplication
                clipboard = QApplication.clipboard()
                if clipboard:
                    clipboard.setText(text)
                    return True
            except Exception as e:
                logger.warning("Qt clipboard failed: %s", e)
        # clip.exe works from any thread (no COM needed)
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                ["clip.exe"] if sys.platform == "win32" else ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE, **kwargs
            )
            proc.communicate(input=text.encode("utf-8"), timeout=5)
            return proc.returncode == 0
        except Exception as e:
            logger.warning("Clipboard command failed: %s", e)
        # Last resort: try Qt clipboard even from background thread
        if not on_main:
            try:
                from PySide6.QtWidgets import QApplication
                clipboard = QApplication.clipboard()
                if clipboard:
                    clipboard.setText(text)
                    return True
            except Exception:
                pass
        return False

    def download_item(self, url: str, title: str, season: Optional[int],
                      resolution: str, size: str, service_type: str = "Rapidgator",
                      year: Optional[int] = None, hdr: str = "", dovi: bool = False,
                      progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
        """Download a single item: scrape links, send to JD or clipboard.

        Returns dict with 'success', 'method', 'link_count', 'message'.
        """
        result = {
            "success": False,
            "method": "",
            "link_count": 0,
            "message": "",
            "history_saved": False,
        }

        if not url:
            result["message"] = "No URL provided"
            return result

        # Dedup: if this exact release was already grabbed successfully, don't
        # scrape or re-send it — that just creates a duplicate JDownloader entry.
        # (A prior *failed* grab doesn't count, so retries still work.)
        if self.db is not None:
            try:
                already = self.db.is_downloaded(url)
            except Exception:
                already = False
            if already:
                result["success"] = True
                result["method"] = "duplicate"
                result["message"] = f"Already grabbed — skipped: {title}"
                self._log(f"[Download] skip duplicate: {title}", "info")
                self._progress("download:complete",
                               {"title": title, "url": url, "method": "duplicate", "link_count": 0},
                               _cb=progress_callback)
                return result

        _cb = progress_callback
        self._progress("download:started", {"title": title, "url": url}, _cb=_cb)

        # Step 1: Scrape links from page
        scrape_failed = False
        try:
            links = self.scrape_links(url, service_type, progress_callback=_cb)
        except Exception as e:
            links = []
            scrape_failed = True
            self._log(f"Scrape error: {e}", "warning")
            self._progress("download:fallback", {"title": title, "reason": str(e)}, _cb=_cb)

        if not links:
            # Only fall back to the URL itself if it is *already* a file-host
            # link (e.g. user pasted a rapidgator URL). Sending a source page
            # URL (hdencode/ddlbase) to JDownloader just yields a
            # "Blocked by Cloudflare" entry, so refuse it instead.
            if self._is_supported_download_link(url):
                links = [url]
            else:
                msg = (
                    "No download links found — the source page is protected "
                    "(Cloudflare/captcha) or has no links for this host."
                    if not scrape_failed else
                    "Scrape failed — could not retrieve download links."
                )
                self._log(f"[Download] {title}: {msg}", "warning")
                result["message"] = msg
                self._progress("download:no_links", {"title": title, "url": url}, _cb=_cb)
                self._progress("download:failed", {"title": title, "url": url, "message": msg}, _cb=_cb)
                return result

        self._progress("download:links_found", {"title": title, "link_count": len(links)}, _cb=_cb)
        result["link_count"] = len(links)
        # Remember which movie/show these links belong to (for broken-link tracing)
        if self.db and title:
            try:
                self.db.record_scraped_links(links, title, resolution, url)
            except Exception:
                pass

        # Step 2: Try JDownloader first
        jd_folder = self.config.get("jd_folder", "")
        jd_method = self.config.get("jd_method", "folder")
        if title:
            name = f"{title} ({year})" if year else title
            package_name = f"{name} [{resolution}]" if resolution else name
        else:
            package_name = "ScanHound Download"

        # Per-type download folder: TV (has a season) vs movies, when configured.
        if season is not None:
            destination = (self.config.get("jd_tv_folder") or "").strip()
        else:
            destination = (self.config.get("jd_movies_folder") or "").strip()

        if self.config.get("jd_enabled", False) and (jd_folder or jd_method == "api"):
            if self.send_to_jdownloader(links, package_name, destination=destination, progress_callback=_cb):
                result["success"] = True
                result["method"] = "jdownloader"
                result["message"] = f"Sent {len(links)} links to JDownloader"
                result["history_saved"] = self.save_to_history(
                    url, title, season, resolution, size, status="completed", hdr=hdr, dovi=dovi
                )
                self._log(
                    f"[Download] {title}: delivered to JDownloader "
                    f"({len(links)} link(s)) — archived as grabbed", "info")
                self._progress("download:complete", {"title": title, "url": url, "method": result["method"], "link_count": result["link_count"]}, _cb=_cb)
                return result

        # Step 3: Fallback to clipboard — but ONLY on the desktop app. In
        # server/headless mode (Docker) there is no user clipboard, so a
        # "success" here would be a phantom grab: the item gets archived as
        # delivered even though nothing reached JDownloader. Skip it (same
        # reasoning as the browser fallback below) so a failed JD send stays an
        # honest failure and the item is NOT archived.
        if not self.server_mode and self.copy_to_clipboard(links):
            result["success"] = True
            result["method"] = "clipboard"
            result["message"] = f"Copied {len(links)} links to clipboard"
            result["history_saved"] = self.save_to_history(
                url, title, season, resolution, size, status="clipboard", hdr=hdr, dovi=dovi
            )
            self._progress("download:complete", {"title": title, "url": url, "method": result["method"], "link_count": result["link_count"]}, _cb=_cb)
            return result

        # Step 4: Last resort — open in the user's browser. Only meaningful on
        # the desktop app; in server/headless mode there is no user browser, so
        # skip it rather than report a phantom success.
        if not self.server_mode and self.open_url(url):
            result["success"] = True
            result["method"] = "browser"
            result["message"] = "Opened URL in browser"
            result["history_saved"] = self.save_to_history(
                url, title, season, resolution, size, status="browser", hdr=hdr, dovi=dovi
            )
            self._progress("download:complete", {"title": title, "url": url, "method": result["method"], "link_count": result["link_count"]}, _cb=_cb)
            return result

        # Nothing delivered the links — report an honest failure.
        if self.config.get("jd_enabled", False):
            result["message"] = "JDownloader send failed and no clipboard/browser is available."
        else:
            result["message"] = "JDownloader is disabled and no clipboard/browser is available."
        self._log(f"[Download] {title}: {result['message']}", "warning")
        try:
            self.save_to_history(url, title, season, resolution, size, status="failed", hdr=hdr, dovi=dovi)
        except Exception:
            pass
        self._progress("download:failed", {"title": title, "url": url, "message": result["message"]}, _cb=_cb)
        return result

    def open_in_plex(
        self,
        title: str,
        plex_movies: list,
        plex_tv: list,
        *,
        year: Optional[int] = None,
        season: Optional[int] = None,
        imdb_id: Optional[str] = None,
        plex_rating_key: Optional[str] = None,
    ):
        """Open item in Plex Web interface. Returns URL or None."""
        plex_url = self.config.get("plex_url", "").rstrip("/")
        server_id = self.config.get("plex_server_id", "")
        if not plex_url:
            return None

        direct_url = self._build_plex_url(plex_url, server_id, plex_rating_key)
        if direct_url:
            webbrowser.open(direct_url)
            return direct_url

        norm = normalize_title(title)
        search_pools = [plex_tv] if season is not None else [plex_movies, plex_movies + plex_tv]

        for pool in search_pools:
            ranked_matches = []
            for plex_item in pool:
                rating_key = plex_item.get("rating_key")
                if rating_key is None:
                    continue

                score = 0

                if imdb_id:
                    if plex_item.get("imdb_id") != imdb_id:
                        continue
                    score += 100

                plex_title = normalize_title(plex_item.get("clean_title", ""))
                plex_original = normalize_title(plex_item.get("original_title", ""))
                if norm:
                    if norm not in (plex_title, plex_original):
                        continue
                    score += 50

                if season is not None:
                    if plex_item.get("season") != season:
                        continue
                    score += 25
                elif plex_item.get("season") is None:
                    score += 5

                plex_year = plex_item.get("year") or 0
                if year:
                    if plex_year == year:
                        score += 20
                    elif plex_year:
                        continue

                ranked_matches.append(
                    (
                        score,
                        bool(plex_item.get("dovi", False)),
                        plex_item.get("size", 0),
                        str(rating_key),
                    )
                )

            if ranked_matches:
                ranked_matches.sort(reverse=True)
                url = self._build_plex_url(plex_url, server_id, ranked_matches[0][3])
                if url:
                    webbrowser.open(url)
                    return url
        return None

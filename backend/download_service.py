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
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from backend.database import DatabaseManager
from backend.source_identity import (
    source_kind as _shared_source_kind,
    url_matches_domain as _shared_url_matches_domain,
)
from backend.config import source_enabled
from backend.hdencode_coordinator import (
    HDEncodeTrafficDenied,
    configure_hdencode_coordinator,
    get_hdencode_coordinator,
    require_transport_authorization,
)
from backend.app_service import normalize_title
from backend.sources.ddlbase import decode_ddlbase_link
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic, ScrapedLinks
from backend.download_outcome import (
    CF_MITIGATED_CHALLENGE,
    CHALLENGE_IFRAME_MARKERS,
    TURNSTILE_CAUSE_CODE,
    cf_mitigated_from_perf_log,
    challenge_iframe_srcs,
    diagnostic_from_traffic_denial,
    strong_challenge_markers,
    turnstile_challenge_evidence,
)
from backend.browser_adapter import (
    browser_plan,
    launch_browser,
    safe_status_without_driver,
)
from backend.source_health import record_scrape_outcome

logger = logging.getLogger(__name__)


def compute_package_name(title: str, year: Optional[int], resolution: str,
                         season: Optional[int] = None) -> str:
    """Canonical JDownloader package-name string — the join key used by the
    pipeline tracker across downloads/download_results/rename_jobs. Must match
    send_to_jdownloader's truncation exactly (both its delivery paths truncate
    to 50 chars before JD ever sees the name) — this is the single place that
    string is computed, so the persisted value and the sent value can never
    drift apart. Season is embedded for TV so multiple seasons of one show
    never collapse onto the same join key; the 50-char cap trims the TITLE,
    never the year/season/resolution suffix (a tail-truncation could chop
    'S03' off a long title and silently recreate the collision)."""
    if not title:
        return "ScanHound Download"[:50]
    suffix = f" ({year})" if year else ""
    if season is not None:
        suffix += f" S{season:02d}"
    if resolution:
        suffix += f" [{resolution}]"
    max_title = 50 - len(suffix)
    return f"{title[:max_title]}{suffix}" if max_title > 0 else (title + suffix)[:50]


def fold_name(name: str) -> str:
    """Punctuation-folded comparison key: JDownloader sanitizes package names
    character-for-character (':' -> ';', etc.) before reporting them back, so
    exact comparison of our computed name against JD's reported name fails for
    any title containing such a character. Folding both sides — drop every
    non-alphanumeric, casefold — is immune to any substitution JD performs."""
    return "".join(ch for ch in name if ch.isalnum()).casefold()


# Lazy imports for optional heavy dependencies
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
#: The narrower question of which direct hosts the downloader can hand off, as
#: distinct from identity. Kept separate on purpose -- "is this a direct file host"
#: and "can we send it to JDownloader" are different questions -- but a test asserts
#: this stays a SUBSET of source_identity.DIRECT_FILE_HOSTS so the two lists cannot
#: drift apart again, which is what peer review found had already happened.
_SUPPORTED_DOWNLOAD_HOSTS = (
    "1fichier.com",
    "rapidgator.net",
    "nitroflare.com",
    "ddownload.com",
)


def _url_matches_domain(url: str, domains: tuple) -> bool:
    """Check a URL's parsed hostname against one or more registrable domains.

    Delegates to the shared implementation so hostname parsing cannot diverge
    between the two modules that classify sources.
    """
    return _shared_url_matches_domain(url, domains)


def _source_page_kind(url: str, hdencode_host: str = "hdencode.org") -> str:
    """Classify a source-page URL. Delegates to the ONE shared classifier.

    UNIFIED 2026-08-07 on peer review. This used to default every page that was not
    DDLBase or Adit-HD to ``"hdencode"``. That decided three things:

      * whether the request goes through the HDEncode traffic coordinator;
      * whether the HDEncode off switch refuses it;
      * whether its scrape outcome is recorded against HDEncode's health.

    All three were therefore being applied to direct file-host URLs, which the batch
    API accepts. Each is a CORRECTION rather than a regression: a Rapidgator link
    should not consume HDEncode's rate budget, should not be refused when HDEncode is
    switched off, and should not pollute HDEncode's scrape statistics. Direct hosts
    already bypass ``scrape_links`` entirely, so dispatch is unaffected.

    Returns a value from :data:`backend.source_identity.SOURCE_KINDS`, so callers
    comparing against ``"hdencode"`` keep working and everything else is now named
    instead of assumed.
    """
    return _shared_source_kind(url, hdencode_host)


def _challenge_iframe_signal(src: str) -> str:
    """Return a closed, non-sensitive challenge-frame signal.

    Full iframe URLs may contain site keys, return URLs, state, or tokens in
    their path/query. Public diagnostics expose only a closed challenge marker
    and a syntactically valid ASCII hostname.
    """
    raw = (src or "").strip()
    low = raw.lower()
    marker = next(
        (name for name in CHALLENGE_IFRAME_MARKERS if name in low),
        "challenge",
    )

    if raw.startswith("//"):
        parse_target = "https:" + raw
    elif "://" in raw:
        parse_target = raw
    else:
        parse_target = ""

    host = "unknown"
    if parse_target:
        try:
            candidate = (urlparse(parse_target).hostname or "").lower().rstrip(".")
            if (
                candidate
                and len(candidate) <= 253
                and ".." not in candidate
                and re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",
                    candidate,
                )
            ):
                host = candidate
        except Exception:
            pass

    return f"iframe:{marker}@{host}"


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


def _extract_requested_host_links(html: str, keyword: str) -> List[str]:
    """Return unique requested-host links already visible in the page HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    seen: Set[str] = set()
    links: List[str] = []
    needle = (keyword or "").lower()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if needle in href.lower() and href not in seen:
            seen.add(href)
            links.append(href)
    return links


_ARCHIVE_RE = re.compile(r'\.(rar|zip|7z|tar|gz|bz2|tgz|r\d\d|z\d\d|001)$', re.IGNORECASE)


def _is_archive_name(name: str) -> bool:
    """True if a filename looks like an archive JDownloader would extract.

    Direct media files (.mkv/.mp4/...) have nothing to extract, so a package
    made only of those is *complete* once downloaded — it should not sit at
    "downloaded" forever waiting for an extraction that never happens.
    """
    return bool(_ARCHIVE_RE.search((name or "").strip()))


# HDEncode's link-reveal control can render late when the site is slow — page
# loads over 60s have been observed in production, and a release page that
# reported "no View links button" served the control normally on a later retry.
# One bounded CLICKABLE budget: a merely-present control may be hidden, disabled
# or not yet interactive, and the caller JS-clicks whatever this returns, so
# activating a non-clickable control could fire a handler prematurely.
#
# PRODUCTION EVIDENCE (2026-07-24): HDEncode serves the unlock form in a
# not-ready state — its submit reads "Verifying… Please wait" — and only later
# swaps to "View links". When the real control is present it is clickable in
# under a second and the links follow; when the countdown is running a 15s
# budget expired, so the wait must outlast the countdown rather than fall
# through to a control that cannot reveal anything.
_REVEAL_CLICKABLE_TIMEOUT = 60

# A control in this state is a placeholder for the real one. Clicking it cannot
# reveal links, and doing so masks the countdown as a layout failure, so it is
# never an acceptable fallback target.
#
# BEST-EFFORT OBSERVABILITY ONLY — never a safety gate. The allowlist below is
# what decides whether a control may be clicked; these markers exist so a
# countdown can be told apart from a genuinely absent control (and, later, so
# its real duration can be measured). They are English-only and will miss a
# localized placeholder; that is harmless, because a missed placeholder simply
# fails the allowlist and is not clicked either way.
_REVEAL_NOT_READY_MARKERS = ("verifying", "please wait")

# A control is clicked only when its label positively identifies it as a links
# control. "access" alone was too broad — it matches "Access denied" and other
# unrelated copy — so the phrase must be the links wording HDEncode actually
# uses.
# Word-aware: a substring test also accepted "Preview links" and "Review links",
# which is the same broad-matching mistake this lookup exists to eliminate.
_REVEAL_LABEL_PATTERNS = (
    re.compile(r"\bview\s+links?\b", re.IGNORECASE),
    re.compile(r"\baccess\s+the\s+links?\b", re.IGNORECASE),
)


def _reveal_control_not_ready(label: str) -> bool:
    """True when a label looks like a countdown placeholder (diagnostics only)."""
    low = (label or "").lower()
    return any(marker in low for marker in _REVEAL_NOT_READY_MARKERS)


def _reveal_label_is_links_control(label: str) -> bool:
    """True when a label positively identifies a link-reveal control."""
    text = label or ""
    return any(pattern.search(text) for pattern in _REVEAL_LABEL_PATTERNS)


def _control_is_interactive(element) -> bool:
    """True when a control is displayed and enabled.

    The caller JS-clicks whatever the lookup returns, so a control that is
    present but hidden or disabled must not be treated as ready.
    """
    try:
        return bool(element.is_displayed() and element.is_enabled())
    except Exception:
        return False

# Only real submit controls can post the unlock form. A bare `button` selector
# would also match type="button" and other non-submit controls.
_REVEAL_SUBMIT_SELECTOR = (
    "input[type='submit'], button[type='submit'], button:not([type])"
)
_UNLOCK_FRAGMENT = "unlocked"


def _resolves_to_unlock_target(candidate: str, base: str) -> bool:
    """True when ``candidate`` resolves to *this document's* unlock endpoint.

    A submit control may override its form's destination via ``formaction``, so
    the *effective* destination is what must be validated — not the parent
    form's action. Requires the same origin **and the same document path** as
    the current page, plus a fragment of exactly ``unlocked``: a substring test
    would accept ``#unlocked-other``, and origin alone would accept some other
    page's unlock endpoint.
    """
    if not candidate or not base:
        return False
    try:
        resolved = urlparse(urljoin(base, candidate))
        current = urlparse(base)
    except Exception:
        return False
    if resolved.fragment != _UNLOCK_FRAGMENT:
        return False

    def _norm(path: str) -> str:
        return (path or "/").rstrip("/") or "/"

    return (
        resolved.scheme,
        resolved.hostname,
        resolved.port,
        _norm(resolved.path),
    ) == (
        current.scheme,
        current.hostname,
        current.port,
        _norm(current.path),
    )


def _ensure_selenium():
    """Lazy-load Selenium primitives without importing a specific adapter."""
    global _By, _WebDriverWait, _EC
    if _By is None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        _By = By
        _WebDriverWait = WebDriverWait
        _EC = EC


class DownloadService:
    """Manages download operations, JDownloader, and WebDriver link scraping."""

    def _source_kind_of(self, url: str) -> str:
        """This service's source identity for a URL, under the CONFIGURED host.

        WHY THIS EXISTS AS A METHOD. `_source_page_kind` grew an `hdencode_host`
        parameter so identity could follow configuration, and then none of its
        three call sites passed it -- a parameter added in the very commit whose
        message said identity should follow configuration, which is the fourth
        time a review has caught me adding a signal nothing consumes.

        The consequence was a split brain with the queue, which does pass its
        configured host. With `base_url = https://hdencode.example.net`:

            download_queue:  hdencode
            DownloadService: other

        so a configured mirror skipped the HDEncode traffic coordinator, the
        service-level off switch, and HDEncode scrape-outcome health ownership --
        while still being paused, resumed and refunded as an HDEncode row. The
        disagreement runs the other way too: once a mirror is configured, a
        leftover `hdencode.org` URL is `hdencode` here and not in the queue.

        Reading the config through one accessor is the point. Three call sites each
        reaching for `base_url` themselves is how the two classifiers drifted in
        the first place.
        """
        cfg = getattr(self, "config", None) or {}
        return _source_page_kind(url, cfg.get("base_url") or "https://hdencode.org")

    def source_kind(self, url: str) -> str:
        """PUBLIC source identity under this service's configuration.

        ADDED round 8. I claimed in the round-8 package that "no production call
        site passes a URL alone any more". That was FALSE, and the way I got it
        wrong matters more than the fact: I grepped `backend/download_service.py`
        only, printed "remaining bare calls (should be only the def)", saw the def
        plus my new private helper, and wrote a REPO-WIDE claim from a single-file
        search. `backend/api/routes/downloads.py` imports the module-level
        `_source_page_kind` and calls it bare at two sites, so a configured mirror's
        scrape health was not persisted as HDEncode while a stale `hdencode.org`
        still was.

        This exists so routes never reclassify independently. Private helpers get
        imported anyway -- that is exactly what happened -- so the config-aware
        answer needs a public front door.
        """
        return self._source_kind_of(url)

    def owns_source_health(self, url: str, source: str = "hdencode") -> bool:
        """Whether a scrape outcome for ``url`` belongs to ``source``'s health.

        The routes were each deciding two things: how to classify, and whether to
        persist. Centralising the pair means a future caller cannot get the second
        right while getting the first wrong, which is the drift this review has now
        caught twice.
        """
        return self.source_kind(url) == source

    def __init__(self, config: Dict[str, Any], db: DatabaseManager, server_mode: bool = False):
        self.config = config
        self.db = db
        configure_hdencode_coordinator(config, db)
        # In server/headless mode (the FastAPI/Docker deployment) there is no
        # user-facing browser, so the browser fallback is meaningless and must
        # not be reported as a successful delivery.
        self.server_mode = server_mode

        # WebDriver
        self.cached_driver = None
        # Latest navigation's Cloudflare cf-mitigated value (None = no signal).
        self._last_cf_mitigated: Optional[str] = None
        self._browser_status: Dict[str, Any] = safe_status_without_driver(
            self.config,
            chrome_bin=os.environ.get("CHROME_BIN"),
            system_driver="/usr/bin/chromedriver",
        )
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
        # Keyed by cache_key = str(package uuid) when JD reports one, else the
        # package name (legacy/uuid-less fallback).
        self._results_cache: Dict[str, tuple] = {}
        # Best real title ever resolved for a JD package (by cache_key). Lets a
        # transient scrape-map miss keep the previously-resolved title instead
        # of regressing the display back to the raw (often obfuscated) JD name.
        self._best_titles: Dict[str, str] = {}
        # cache_key -> download_results row id, so poll_results can attach the
        # durable DB id to each returned row without a query on every poll.
        self._uuid_id: Dict[str, int] = {}

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
                        hdr: str = "", dovi: bool = False,
                        year: Optional[int] = None,
                        package_name: Optional[str] = None,
                        service_type: Optional[str] = None):
        """Save a downloaded item to history."""
        try:
            normalized = normalize_title(title)
            extra = {}
            if package_name is not None:
                extra["package_name"] = package_name
            if service_type is not None:
                extra["service_type"] = service_type
            self.db.add_to_history(
                url=url, title=title, normalized_title=normalized,
                season=season, resolution=resolution, size=size,
                status=status, hdr=hdr or None, dovi=dovi, year=year,
                **extra,
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

    # Resolution ranking for the title-level dedup — mirrors the scanner's
    # upgrade rule (_RES_RANK in api/routes/results.py; keep in sync).
    _RES_RANK = {"2160p": 4, "4k": 4, "uhd": 4, "1080p": 3, "720p": 2, "480p": 1}

    @classmethod
    def _res_rank(cls, res) -> int:
        return cls._RES_RANK.get((res or "").strip().lower(), 0)

    def _best_prior_grab(self, title: str, year: Optional[int],
                         season: Optional[int]) -> Optional[Dict[str, Any]]:
        """The best-quality non-failed grab already recorded for this title.

        Key: normalized title + year + season. A stored NULL year matches any
        requested year (legacy rows predate the year column); season matches
        strictly (None only matches None) so one season pack never blocks
        another. Returns {'resolution', 'dovi'} or None.
        """
        if self.db is None or not title:
            return None
        try:
            rows = self.db.get_downloaded_title_quality()
        except Exception:
            return None
        if not isinstance(rows, list):
            return None  # e.g. a MagicMock db in tests
        want = normalize_title(title)
        best: Optional[Dict[str, Any]] = None
        for row in rows:
            try:
                nt, yr, se, res, dv = row[0], row[1], row[2], row[3], row[4]
            except Exception:
                continue
            if nt != want:
                continue
            if se != season:
                continue
            if yr is not None and year is not None and int(yr) != int(year):
                continue
            cand = {"resolution": res, "dovi": bool(dv)}
            if best is None or self._is_quality_upgrade(
                    cand["resolution"], cand["dovi"], best):
                best = cand
        return best

    def _is_quality_upgrade(self, resolution: str, dovi: bool,
                            prior: Dict[str, Any]) -> bool:
        """Higher resolution, or DV gain at the same resolution — the same
        rule the scanner + read-time overlay use for 'worth grabbing again'."""
        new_rank, old_rank = self._res_rank(resolution), self._res_rank(prior.get("resolution"))
        return new_rank > old_rank or (
            new_rank == old_rank and bool(dovi) and not prior.get("dovi"))

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

    def remove_package(self, id_: int) -> dict:
        """Remove a single tracked download by its row id: remove ONLY that
        package from JD (by its uuid) and delete its result row. Idempotent:
        succeeds even when the package is already gone from JD, has no known
        uuid, or JD is unreachable — the DB row is always cleared so the UI
        reflects the removal."""
        row = None
        try:
            rows = self.db.get_download_results(limit=100000) if self.db else []
            row = next((r for r in rows if r.get("id") == id_), None)
        except Exception:
            row = None
        uuid = (row or {}).get("package_uuid")
        name = (row or {}).get("name")
        if uuid:
            try:
                device = self._connect_jd_device()
                device.downloads.remove_links([], [int(uuid)])  # JD expects the native int64
                self._log(f"JDownloader: removed package uuid {uuid}", "info")
            except Exception as e:
                logger.warning("remove_package JD step failed for id %s (uuid %s): %s", id_, uuid, e)
                self._invalidate_jd_cache()
        removed = 0
        try:
            removed = self.db.delete_download_result(id_) if self.db else 0
        except Exception as e:
            logger.warning("remove_package DB delete failed for id %s: %s", id_, e)
        # Evict this package from the poller's in-memory caches (keyed by
        # cache_key = package_uuid or name — pop both, since a legacy row may
        # be name-keyed). Without this, an unchanged package still present in
        # JD (e.g. the JD-side removal above failed) hits poll_results()'s
        # unchanged-state skip branch on the next poll and re-emits the id we
        # just deleted from the DB (ghost-id resurrection). Evicting forces
        # that poll to treat it as a fresh row instead.
        for key in (uuid, name):
            if key:
                self._results_cache.pop(key, None)
                self._uuid_id.pop(key, None)
                self._best_titles.pop(key, None)
        return {"ok": True, "removed": removed}

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
            u = pkg.get("uuid")
            # JD's uuid is a native int64; stringify it so it's a stable dict/DB
            # key (JSON round-trips int keys as strings anyway) and so callers
            # comparing package_uuid values don't have to care about type.
            package_uuid = str(u) if u is not None else None
            # Identity for the per-poll caches below: the package's durable JD
            # uuid when known, else its (legacy/uuid-less) name.
            cache_key = package_uuid or name
            child_links = by_pkg.get(pkg.get("uuid"), [])
            bytes_total = pkg.get("bytesTotal") or 0
            bytes_loaded = pkg.get("bytesLoaded") or 0
            downloaded = bool(pkg.get("finished")) or (bytes_total > 0 and bytes_loaded >= bytes_total)
            host = next((l.get("host", "") for l in child_links if l.get("host")), "")
            title = self._resolve_title(name, child_links, norm_titles)
            # Keep a once-resolved real title even if the scrape map transiently
            # misses on a later poll (don't regress to the raw JD package name).
            if title and title != name:
                self._best_titles[cache_key] = title
            elif cache_key in self._best_titles:
                title = self._best_titles[cache_key]

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
                "id": None,
                "name": name, "title": title, "host": host,
                "bytes_total": bytes_total, "bytes_loaded": bytes_loaded,
                "downloaded": 1 if downloaded else 0,
                "extraction": extraction, "state": state, "error": error,
                "package_uuid": package_uuid,
                # saveTo (extracted output folder) — consumed by the auto-rename
                # hook when the package reaches the "extracted" state.
                "save_to": pkg.get("saveTo") or "",
            }
            results.append(row)

            if record and self.db:
                # 'save_to' is for the returned dict (auto-rename hook) and
                # 'id' is derived, not stored — passing either would TypeError
                # and the whole row would (silently) never persist.
                db_fields = {k: v for k, v in row.items() if k not in ("save_to", "id")}
                change_key = (state, bytes_loaded, extraction, row["downloaded"], error, title)
                if self._results_cache.get(cache_key) != change_key:
                    try:
                        rid = self.db.upsert_download_result(**db_fields)
                    except Exception as e:
                        logger.debug("upsert_download_result failed: %s", e)
                        rid = None
                    # Only prime the change-cache once the write actually
                    # landed — a failed/exception'd write must NOT be marked
                    # "recorded", or the next poll would wrongly skip retrying it.
                    if rid is not None:
                        self._results_cache[cache_key] = change_key
                        self._uuid_id[cache_key] = rid

                row["id"] = self._uuid_id.get(cache_key)
                if row["id"] is None:
                    # cache-suppressed row (unchanged since a prior process's
                    # run) whose id this in-memory map never learned — recover
                    # it from the DB rather than emit an id-less row.
                    try:
                        row["id"] = self.db.get_download_result_id(package_uuid, name)
                    except Exception as e:
                        logger.debug("get_download_result_id failed: %s", e)
                    if row["id"] is not None:
                        self._uuid_id[cache_key] = row["id"]
                    else:
                        # No row exists at all yet — write one now instead of
                        # emitting an id-less row.
                        try:
                            rid = self.db.upsert_download_result(**db_fields)
                        except Exception as e:
                            logger.debug("upsert_download_result retry failed: %s", e)
                            rid = None
                        if rid is not None:
                            self._results_cache[cache_key] = change_key
                            self._uuid_id[cache_key] = rid
                            row["id"] = rid

        # Bound the per-package caches to packages currently in JD's list so they
        # don't grow without limit over the long-lived poller's lifetime. Only
        # prunes after a successful poll (early returns above skip this), so a
        # transient JD blip never discards resolved titles.
        live_keys = {r["package_uuid"] or r["name"] for r in results}
        self._results_cache = {k: v for k, v in self._results_cache.items() if k in live_keys}
        self._best_titles = {k: v for k, v in self._best_titles.items() if k in live_keys}
        self._uuid_id = {k: v for k, v in self._uuid_id.items() if k in live_keys}

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
        plan = browser_plan(
            self.config,
            chrome_bin=os.environ.get("CHROME_BIN"),
            system_driver="/usr/bin/chromedriver",
        )
        self._log(
            f"Scraper preflight: adapter={plan.adapter}, "
            f"profile={plan.profile_mode}",
            "info",
        )
        if major:
            self._log(f"Scraper preflight: detected browser major version {major}", "info")
        else:
            warning = (
                "Scraper preflight: could NOT detect the browser version — "
                "undetected-chromedriver may fetch a mismatched driver and break scraping."
                if plan.adapter == "uc_chromium"
                else
                "Scraper preflight: could NOT detect the browser version — "
                "standard Selenium will rely on the installed Chromium/ChromeDriver pairing."
            )
            self._log(warning, "warning")

    def get_driver(self, *, require_hdencode_authorization: bool = False):
        """Get or create a cached WebDriver instance (thread-safe)."""
        if require_hdencode_authorization:
            require_transport_authorization("selenium")
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

            chrome_bin = os.environ.get("CHROME_BIN")
            system_driver = "/usr/bin/chromedriver"
            plan = browser_plan(
                self.config,
                chrome_bin=chrome_bin,
                system_driver=system_driver,
            )

            # UC needs a matching major when it downloads a driver. Standard
            # Selenium uses the installed system pairing and only needs a clear
            # diagnostic when version discovery is unavailable.
            chrome_ver = self._detect_chrome_major()
            if chrome_ver:
                logger.debug("Detected Chrome major version %s", chrome_ver)
            elif plan.adapter == "uc_chromium":
                logger.warning(
                    "Could not detect Chrome version; undetected-chromedriver "
                    "will guess a driver and may mismatch the browser."
                )
            else:
                logger.warning(
                    "Could not detect Chrome version; standard Selenium will "
                    "rely on the installed Chromium/ChromeDriver pairing."
                )

            # Launch with a bounded retry. The selected adapter is explicit:
            # standard Selenium + persistent Chromium profile by default, with
            # the historical UC path retained only as a configured rollback.
            last_err: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    self.cached_driver, self._browser_status = launch_browser(
                        self.config,
                        chrome_ver=chrome_ver,
                        chrome_bin=chrome_bin,
                        system_driver=system_driver,
                    )
                except Exception as e:
                    last_err = e
                    self.cached_driver = None
                    self._browser_status = safe_status_without_driver(
                        self.config,
                        chrome_bin=chrome_bin,
                        system_driver=system_driver,
                        launch_error=type(e).__name__,
                    )
                    self._log(
                        f"[Scrape] Chrome launch failed "
                        f"(attempt {attempt}/3): {type(e).__name__}",
                        "warning",
                    )
                    self._kill_stale_chrome()
                    time.sleep(min(2 * attempt, 5))
                    continue
                return self.cached_driver
            # All attempts failed — surface the real error to the caller so the
            # scrape reports an honest failure (not a silent empty result).
            raise last_err if last_err else RuntimeError("Chrome could not be launched")

    def get_browser_status(self) -> dict:
        """Return a public-safe browser/adapter snapshot for diagnostics."""
        status = dict(self._browser_status or {})
        status["session_active"] = self.cached_driver is not None
        status["detected_browser_major"] = self._detect_chrome_major()
        return status

    def _kill_stale_chrome(self) -> None:
        """Best-effort reap of orphaned chrome/chromedriver processes that can
        wedge a fresh launch. Linux/container only; a no-op on Windows and safe
        to call under _driver_lock (no other scrape owns a live driver then)."""
        if sys.platform.startswith("win"):
            return
        for pat in ("chromedriver", "chrome", "chromium"):
            try:
                subprocess.run(["pkill", "-9", "-f", pat],
                               capture_output=True, timeout=5)
            except Exception:
                pass

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

    def _navigate_with_diagnostic(
        self, url: str, tag: str = "Scrape", attempts: int = 3
    ):
        """Load a source page through the appropriate traffic policy."""
        last_diag: Optional[ScrapeDiagnostic] = None
        hdencode_request = self._source_kind_of(url) == "hdencode"

        for attempt in range(1, attempts + 1):
            try:
                request_context = (
                    get_hdencode_coordinator().request("selenium")
                    if hdencode_request
                    else nullcontext()
                )
                with request_context:
                    try:
                        driver = self.get_driver(
                            require_hdencode_authorization=hdencode_request
                        )
                    except Exception as exc:
                        diag = ScrapeDiagnostic(
                            ScrapeCode.BROWSER_LAUNCH_FAILED,
                            retryable=True,
                            affects_source_health=False,
                            signals=(type(exc).__name__,),
                            detail="The browser could not start.",
                        )
                        self._log(
                            f"[{tag}] browser launch failed: {type(exc).__name__}",
                            "error",
                        )
                        return None, diag

                    try:
                        driver.get(url)
                    except Exception as exc:
                        if hdencode_request:
                            get_hdencode_coordinator().observe_network_failure(
                                type(exc).__name__
                            )
                        last_diag = ScrapeDiagnostic(
                            ScrapeCode.BROWSER_NAVIGATION_FAILED,
                            retryable=True,
                            affects_source_health=False,
                            signals=(type(exc).__name__,),
                            detail="Browser navigation failed.",
                        )
                        self._log(
                            f"[{tag}] navigation raised "
                            f"(attempt {attempt}/{attempts}): {type(exc).__name__}",
                            "warning",
                        )
                    else:
                        code = self._browser_error_code(driver, url)
                        if not code:
                            if hdencode_request:
                                get_hdencode_coordinator().observe_http_status(200)
                            return driver, None
                        if hdencode_request:
                            get_hdencode_coordinator().observe_network_failure(code)
                        last_diag = ScrapeDiagnostic(
                            ScrapeCode.BROWSER_NETWORK_ERROR,
                            retryable=True,
                            affects_source_health=False,
                            signals=(code,),
                            detail=f"Chromium could not reach the source ({code}).",
                        )
                        self._log(
                            f"[{tag}] browser network error {code}; recycling "
                            f"({attempt}/{attempts}).",
                            "warning",
                        )
            except HDEncodeTrafficDenied as exc:
                return None, diagnostic_from_traffic_denial(exc)

            self._recycle_driver()
            if attempt < attempts:
                time.sleep(min(2 * attempt, 5))

        self._log(f"[{tag}] giving up after {attempts} navigation attempt(s)", "error")
        return None, last_diag or ScrapeDiagnostic(
            ScrapeCode.BROWSER_NETWORK_ERROR,
            retryable=True,
            affects_source_health=False,
        )

    def _navigate(self, url: str, tag: str = "Scrape", attempts: int = 3):
        """Backward-compatible driver-only wrapper used by non-HDEncode paths."""
        driver, _diagnostic = self._navigate_with_diagnostic(url, tag=tag, attempts=attempts)
        return driver

    def _log_page_diagnostics(
        self,
        driver,
        keyword: Optional[str] = None,
        *,
        stage: str = "page",
        source_kind: str = "hdencode",
        # The reveal-control tier from _find_reveal_control. "not-ready" means the
        # control existed but had not finished verifying when our 60s window
        # expired. That rules OUT a changed page -- the control was there -- but it
        # does not establish source rate limiting, which this comment used to
        # assert. Cause is unresolved; see the note at the emission site.
        reveal_tier: Optional[str] = None,
    ) -> ScrapeDiagnostic:
        """Log page evidence and return a structured operation classification."""
        try:
            from bs4 import BeautifulSoup

            html = driver.page_source or ""
            soup = BeautifulSoup(html, "html.parser")
            anchors = soup.find_all("a", href=True)
            signals: List[str] = []
            self._log(f"[HDEncode][diag] {len(anchors)} links, {len(html)} bytes of HTML")

            body_text = " ".join((soup.get_text(" ") or "").split())[:240]
            self._log(f"[HDEncode][diag] visible text: {body_text!r}")
            body_low = body_text.lower()
            try:
                raw_title = driver.title
                page_title = raw_title if isinstance(raw_title, str) else ""
            except Exception:
                page_title = ""
            network_markers = (
                "site can't be reached",
                "site can’t be reached",
                "took too long to respond",
                "err_",
                "no internet",
                "dns_probe",
                "connection was reset",
            )
            matched_network = [m for m in network_markers if m in body_low]
            if matched_network:
                signals.extend(matched_network)
                self._log(
                    "[HDEncode][diag] browser NETWORK-ERROR page detected; this is not a site challenge.",
                    "warning",
                )

            if len(anchors) == 0 and len(html) > 40000:
                signals.append("large_zero_anchor_document")
                self._log(
                    "[HDEncode][diag] large HTML document with zero anchors; treating this as a supporting signal only.",
                    "warning",
                )

            hosts = ("rapidgator", "nitroflare", "1fichier", "ddownload")
            host_links = [a["href"] for a in anchors if any(h in a["href"].lower() for h in hosts)]
            if host_links:
                self._log(f"[HDEncode][diag] file-host links on page ({len(host_links)}): {host_links[:5]}")
            else:
                sample = [a["href"] for a in anchors[:15]]
                self._log(f"[HDEncode][diag] no file-host links; sample hrefs: {sample}")

            candidates = []
            for el in soup.find_all(["button", "input", "a"]):
                label = (el.get("value") or el.get_text() or "").strip()
                if not label:
                    continue
                low_label = label.lower()
                if any(k in low_label for k in (
                    "access", "download", "link", "get ", "show", "reveal", "unlock", "continue",
                )):
                    candidates.append(f"{el.name}={label[:40]!r}")
            if candidates:
                signals.append("access_control_present")
                self._log(f"[HDEncode][diag] possible access controls: {candidates[:10]}")
            else:
                signals.append("access_control_absent")
                self._log("[HDEncode][diag] no access/download/link controls found on page", "warning")

            forms = [(f.get("action") or "(no action)") for f in soup.find_all("form")]
            if forms:
                self._log(f"[HDEncode][diag] forms: {forms[:5]}")

            captcha_frames = list(challenge_iframe_srcs(html))
            if captcha_frames:
                safe_iframe_signals = [
                    _challenge_iframe_signal(src)
                    for src in captcha_frames[:5]
                ]
                signals.extend(safe_iframe_signals)
                self._log(
                    "[HDEncode][diag] CAPTCHA/Turnstile iframe signal(s): %s"
                    % safe_iframe_signals,
                    "warning",
                )

            low = html.lower()
            # One shared active-evidence classifier (challenge iframe / title /
            # visible text). Dormant Turnstile/Cloudflare/reCAPTCHA references in
            # scripts, preload URLs, JS config, or comments are intentionally NOT
            # evidence — see backend/download_outcome.strong_challenge_markers.
            challenge_markers = list(strong_challenge_markers(html, page_title))
            if challenge_markers:
                signals.extend(challenge_markers)
                self._log(
                    f"[HDEncode][diag] strong challenge evidence: "
                    f"{challenge_markers}",
                    "warning",
                )

            # Widget-embedded Turnstile evidence: the response field, the
            # .cf-turnstile container, a challenges.cloudflare.com iframe, or a
            # navigation-scoped 600*-family console error. Every check above is
            # shaped for an interstitial that REPLACES the page; HDEncode embeds
            # the challenge inside the reveal widget, which is how a failing
            # challenge was read as a source throttle for two weeks.
            turnstile_markers = list(turnstile_challenge_evidence(
                html, self._browser_console_lines(driver)
            ))
            if turnstile_markers:
                signals.extend(
                    m for m in turnstile_markers if m not in signals
                )
                self._log(
                    f"[HDEncode][diag] Turnstile evidence: {turnstile_markers}",
                    "warning",
                )

            if keyword:
                keyword_present = keyword.lower() in low
                signals.append(f"requested_host_present:{str(keyword_present).lower()}")
                self._log(f"[HDEncode][diag] keyword '{keyword}' present in HTML: {keyword_present}")

            # cf-mitigated is authoritative: Cloudflare sets it on every
            # Challenge Page type, so it outranks the page heuristics and is
            # never overridden by them (a custom/localized challenge may carry
            # no recognised phrase at all). Absence is only "no signal".
            header_challenge = (
                getattr(self, "_last_cf_mitigated", None) == CF_MITIGATED_CHALLENGE
            )
            if header_challenge:
                signals.append("cf-mitigated:challenge")

            if matched_network and not header_challenge:
                return ScrapeDiagnostic(
                    ScrapeCode.BROWSER_NETWORK_ERROR,
                    retryable=True,
                    affects_source_health=False,
                    signals=tuple(signals),
                )
            if (header_challenge or captcha_frames or challenge_markers
                    or (stage == "access_control"
                        and reveal_tier == "not-ready" and turnstile_markers)):
                # THE CONJUNCTION, 2026-08-09. A not-ready reveal control plus
                # ACTIVE Turnstile evidence is an interactive challenge, not a
                # source throttle — measured on the live stall: Turnstile logged
                # `Error: 600010.` while the reveal sat at "Verifying…" and the
                # user's own browser passed the same challenge in under a
                # second. Turnstile evidence alone deliberately does NOT
                # classify outside the not-ready reveal state: a dormant widget
                # on a page whose reveal works fine proves nothing.
                decision = None
                if source_kind == "hdencode":
                    decision = get_hdencode_coordinator().observe_challenge()
                if stage == "access_control" and reveal_tier == "not-ready":
                    signals.append("reveal-tier:not-ready")
                return ScrapeDiagnostic(
                    ScrapeCode.INTERACTIVE_CHALLENGE,
                    retryable=False,
                    affects_source_health=True,
                    signals=tuple(signals),
                    stage="verification",
                    # The MECHANISM, when proven; the generic label otherwise.
                    # last_cause_code is one of only three fields the queue
                    # persists, so this is where "it was Turnstile" survives
                    # log rotation.
                    cause_code=(
                        TURNSTILE_CAUSE_CODE
                        if turnstile_markers
                        else "interactive_challenge"
                    ),
                    cooldown_until=(
                        decision.cooldown_until if decision is not None else None
                    ),
                    transport_attempted=True,
                    affected_scope="source",
                    retry_mode="manual_verification",
                    action_code="verification_required",
                    health_owner=(
                        "coordinator"
                        if source_kind == "hdencode"
                        else "outcome_recorder"
                    ),
                )
            if stage == "access_control" and reveal_tier == "not-ready":
                # A STALLED VERIFY IS A THROTTLE, NOT A BROKEN PAGE.
                #
                # This previously fell through to LAYOUT_CHANGED: retryable=False,
                # no cooldown, no coordinator notification. The consequence was
                # not one lost item. The batch never paused, the queue kept
                # marching at its configured spacing, and every remaining item
                # hit the same closed door and became permanently terminal. 78
                # items accumulated that way, with automated_retry_count 0 on
                # every one.
                #
                # observe_challenge sets the shared cooldown and returns the
                # cooldown_until the queue needs to pause the batch and later
                # auto-resume.
                decision = None
                if source_kind == "hdencode":
                    # observe_reveal_stall, not observe_challenge: the latter
                    # hard-codes the 1h Cloudflare value, which measurement on
                    # 2026-08-06 showed is far shorter than a real reveal
                    # throttle (~5h and still active at the first probe).
                    decision = get_hdencode_coordinator().observe_reveal_stall(
                        "reveal_verification_stalled")
                signals.append("reveal-tier:not-ready")
                return ScrapeDiagnostic(
                    ScrapeCode.REVEAL_VERIFICATION_STALLED,
                    # RETRYABLE: the release is fine, the source was busy.
                    retryable=True,
                    affects_source_health=True,
                    signals=tuple(signals),
                    stage="access_control",
                    cause_code="reveal_verification_stalled",
                    cooldown_until=(
                        decision.cooldown_until if decision is not None else None
                    ),
                    transport_attempted=True,
                    # The SOURCE is throttled, not this one item, so the batch
                    # must pause rather than burn the rest of the queue.
                    affected_scope="source",
                    retry_mode="after_cooldown",
                    action_code="wait_for_cooldown",
                    health_owner=(
                        "coordinator" if source_kind == "hdencode"
                        else "outcome_recorder"
                    ),
                )
            if stage == "access_control":
                # Genuinely absent or wrong-destination: tier "none",
                # "ambiguous", "destination-rejected". A real layout change.
                return ScrapeDiagnostic(
                    ScrapeCode.LAYOUT_CHANGED,
                    retryable=False,
                    affects_source_health=True,
                    signals=tuple(signals),
                )
            if stage == "requested_host":
                code = ScrapeCode.REQUESTED_HOST_MISSING if host_links else ScrapeCode.NO_FILE_HOST_LINKS
                return ScrapeDiagnostic(
                    code,
                    retryable=False,
                    affects_source_health=False,
                    signals=tuple(signals),
                )
            return ScrapeDiagnostic(
                ScrapeCode.NO_FILE_HOST_LINKS,
                retryable=False,
                affects_source_health=False,
                signals=tuple(signals),
            )
        except Exception as e:
            self._log(f"[HDEncode][diag] failed to gather diagnostics: {e}", "warning")
            return ScrapeDiagnostic(
                ScrapeCode.SCRAPE_EXCEPTION,
                retryable=True,
                affects_source_health=False,
                signals=(type(e).__name__,),
                detail=f"Failed to classify the loaded page: {e}",
            )

    def _find_reveal_control(self, driver):
        """Locate HDEncode's link-reveal control, or return ``None``.

        ONE rule decides every candidate — there is no fast path that skips it.
        The control must be a real **submit**, its label must positively
        identify it as a links control, it must not be a "report" control, and
        its *effective* destination (its ``formaction`` when present, else its
        form's ``action``) must resolve to this page's ``#unlocked`` endpoint.
        Anything ambiguous or unproven yields ``None``.

        Production evidence (2026-07-24) for why the rule is this strict:
        HDEncode serves the unlock form in a not-ready state whose submit reads
        "Verifying… Please wait" and only later swaps to "View links". That
        placeholder posts the same endpoint, so accepting "the single safe
        submit" selected it — 0 link retrievals in 14 attempts. Requiring a
        positive links label makes a re-worded or localized placeholder fail
        closed instead of being clicked.

        The wait polls this predicate, so a control rendering late is taken as
        soon as it becomes valid and interactive, while a page that never
        produces one costs the budget and fails item-level.
        """
        started = time.monotonic()
        state = {
            "forms": 0,
            "labels": [],
            "not_ready": False,
            "rejected_destination": False,
            "ambiguous": False,
        }

        try:
            control = _WebDriverWait(driver, _REVEAL_CLICKABLE_TIMEOUT).until(
                lambda d: self._reveal_candidate(d, state)
            )
        except Exception:
            control = None

        if control is not None:
            tier = "links-control"
        elif state["ambiguous"]:
            tier = "ambiguous"
        elif state["rejected_destination"]:
            # A links-labelled control exists but does not post this page's
            # unlock endpoint: the page shape changed, rather than the control
            # being absent or still counting down.
            tier = "destination-rejected"
        elif state["not_ready"]:
            tier = "not-ready"
        else:
            tier = "none"
        # not_ready_seen separates "countdown ran then finished" from "the page
        # was simply slow", so the real countdown duration can be measured and
        # the temporary 60s ceiling replaced with a tuned value.
        # The tier IS the diagnosis and was previously logged then discarded, so
        # the caller could only report "no button found". Stored on the instance,
        # matching the existing _last_cf_mitigated pattern, so no signatures move.
        self._last_reveal_tier = tier
        self._log(
            f"[HDEncode] reveal-control tier={tier} "
            f"elapsed={time.monotonic() - started:.1f}s "
            f"found={control is not None} forms={state['forms']} "
            f"not_ready_seen={state['not_ready']} "
            f"candidates={state['labels'][:6]}"
        )
        return control

    def _reveal_candidate(self, driver, state):
        """Return the one control safe to click on this page, else ``None``.

        Applied on every poll to every candidate with no exceptions — this is
        the whole safety boundary.
        """
        try:
            base = driver.current_url or ""
        except Exception:
            base = ""
        try:
            forms = list(driver.find_elements(_By.CSS_SELECTOR, "form"))
        except Exception:
            forms = []

        state["forms"] = len(forms)
        labels, matches = [], []
        for form in forms:
            try:
                form_action = form.get_attribute("action") or ""
                controls = form.find_elements(
                    _By.CSS_SELECTOR, _REVEAL_SUBMIT_SELECTOR
                )
            except Exception:
                continue
            for el in controls:
                try:
                    label = (
                        el.get_attribute("value") or el.text or ""
                    ).strip().lower()
                    # A submit may override its form's destination.
                    target = el.get_attribute("formaction") or form_action
                except Exception:
                    continue
                labels.append((label or "<unlabelled>")[:20])
                if "report" in label:
                    continue
                if _reveal_control_not_ready(label):
                    state["not_ready"] = True
                    continue
                if not _reveal_label_is_links_control(label):
                    continue
                if not _resolves_to_unlock_target(target, base):
                    state["rejected_destination"] = True
                    continue
                if not _control_is_interactive(el):
                    # Present but hidden/disabled. The caller JS-clicks whatever
                    # is returned, so it must not be activated yet.
                    state["not_ready"] = True
                    continue
                matches.append(el)

        state["labels"] = labels
        if len(matches) == 1:
            return matches[0]
        if matches:
            state["ambiguous"] = True
        return None

    def _capture_cf_mitigated(self, driver) -> Optional[str]:
        """Drain the navigation's performance log and record its cf-mitigated value.

        Called once per navigation. Draining also bounds the log: the browser
        session is persistent, so entries would otherwise accumulate across
        every grab. Returns ``None`` when the log is unavailable (adapter
        without performance logging, older driver) — that is "no signal", not
        "no challenge", so page evidence still decides.
        """
        value = None
        try:
            entries = driver.get_log("performance")
        except Exception:
            self._last_cf_mitigated = None
            return None
        try:
            page_url = driver.current_url or ""
        except Exception:
            page_url = ""
        observation: dict = {}
        try:
            value = cf_mitigated_from_perf_log(
                entries, page_url=page_url, observation=observation
            )
        except Exception:
            value = None
        self._last_cf_mitigated = value
        if value:
            self._log(f"[HDEncode] cf-mitigated header: {value!r}", "warning")
        elif observation.get("unmatched_challenges"):
            # A challenge header was seen, but on a document that was NOT the
            # displayed page, so it cannot be attributed to it (that was the
            # iframe blocker). Worth surfacing — it is the one scenario that
            # would otherwise leave the signal permanently inert — but it is
            # deliberately NOT treated as a challenge.
            #
            # `matched` comes from the parser rather than being re-derived
            # here: the parser normalizes URLs, and testing a raw page_url
            # (which carries `#unlocked` after the reveal form navigates)
            # against those would report "nothing matched" on ordinary grabs.
            self._log(
                "[HDEncode] cf-mitigated: challenge header on "
                f"{observation['unmatched_challenges']} non-displayed "
                f"document(s); displayed page "
                f"{'matched' if observation.get('matched') else 'not seen'} "
                f"({len(observation.get('documents') or ())} document(s) seen)",
                "warning",
            )
        return value

    def _drain_browser_console(self, driver) -> None:
        """Mark a navigation boundary in the browser (console) log.

        ``get_log`` DRAINS, so whatever this call discards belonged to the
        previous page. Called at the top of ``_wait_past_cloudflare``, which
        runs once per navigation (including the post-click one), so a Turnstile
        error the OLD page logged can never classify the next page — the
        navigation scoping ``is_turnstile_console_failure`` requires of its
        caller lives here.
        """
        try:
            driver.get_log("browser")
        except Exception:
            pass

    def _browser_console_lines(self, driver) -> list:
        """Console messages logged since the last navigation boundary.

        Empty on any failure — an adapter without console logging means "no
        signal", never "no challenge", so page evidence still decides.
        """
        try:
            entries = driver.get_log("browser")
        except Exception:
            return []
        return [str(entry.get("message") or "") for entry in entries or ()]

    def _wait_past_cloudflare(
        self,
        driver,
        timeout: int = 20,
        *,
        source_kind: str = "other",
    ) -> Optional[ScrapeDiagnostic]:
        """Passively wait for a transient browser check without solving it."""
        # A new navigation starts a new console scope — see _drain_browser_console.
        self._drain_browser_console(driver)
        # Read the navigation's response headers once, before polling the page.
        # cf-mitigated is authoritative and language-independent, so a custom or
        # localized Challenge Page is recognised even with no English phrase and
        # no iframe rendered yet.
        if self._capture_cf_mitigated(driver) == CF_MITIGATED_CHALLENGE:
            return self._log_page_diagnostics(
                driver,
                stage="access_control",
                source_kind=source_kind,
            )
        deadline = time.monotonic() + max(0, int(timeout))
        while True:
            try:
                current_url = driver.current_url or ""
            except Exception:
                current_url = ""
            if current_url:
                network_code = self._browser_error_code(driver, current_url)
                if network_code:
                    return ScrapeDiagnostic(
                        ScrapeCode.BROWSER_NETWORK_ERROR,
                        retryable=True,
                        affects_source_health=False,
                        signals=(network_code,),
                        stage="navigation",
                        transport_attempted=True,
                        retry_mode="immediate",
                    )
            try:
                html = driver.page_source or ""
            except Exception:
                html = ""
            try:
                title = driver.title or ""
            except Exception:
                title = ""
            if not strong_challenge_markers(html, title):
                return None
            if time.monotonic() >= deadline:
                return self._log_page_diagnostics(
                    driver,
                    stage="access_control",
                    source_kind=source_kind,
                )
            time.sleep(0.5)

    def scrape_links(self, url: str, service_type: str, progress_callback: Optional[Callable] = None) -> ScrapedLinks:
        """Scrape download links from a page using WebDriver.

        Args:
            url: Page URL to scrape
            service_type: "Rapidgator" or "Nitroflare"

        Returns:
            List of download link URLs.
        """
        # Classify once by parsed hostname. Query/path text such as
        # `?next=https://ddlbase.com` must not bypass the HDEncode off switch.
        source_kind = self._source_kind_of(url)
        if source_kind == "hdencode" and not source_enabled(
            self.config,
            "hdencode_enabled",
            missing_default=True,
        ):
            diagnostic = ScrapeDiagnostic(
                ScrapeCode.SOURCE_DISABLED,
                retryable=False,
                affects_source_health=False,
                stage="source_gate",
                cause_code="source_disabled",
                transport_attempted=False,
                affected_scope="source",
                retry_mode="configuration_change",
                action_code="open_settings",
                health_owner="coordinator",
            )
            self._log(f"[HDEncode] {diagnostic.message}", "warning")
            return ScrapedLinks(diagnostic=diagnostic)

        # EXHAUSTIVE DISPATCH over source_identity.SOURCE_KINDS. Handled here,
        # before the browser is started, because neither of these kinds has a
        # source page to read and launching Chromium for them is pure waste.
        #
        # ROUND 7 CORRECTED A CLAIM I MADE TWICE. I wrote -- in a commit message
        # and again in a review package -- that direct file hosts "already bypass
        # scrape_links entirely", and used that to argue the classifier change was
        # safe for dispatch. It is false. download_item() calls scrape_links()
        # FIRST and only falls back to `links = [url]` after it returns nothing.
        # So a pasted Rapidgator URL was being run through HDEncode reveal-page
        # logic: it clicked for a reveal control that cannot exist, then reported
        # layout_changed or a reveal stall -- attributing the failure to HDEncode's
        # source health, and on the throttle path putting the whole source into a
        # cooldown, because of a URL that has nothing to do with HDEncode.
        #
        # There is deliberately NO `else` here. When a sixth kind is added, the
        # branch below raises on the unhandled value rather than silently treating
        # it as HDEncode, which is how `other` came to mean `hdencode` in the first
        # place.
        if source_kind == "direct_file":
            # THE URL IS THE LINK. Return it.
            #
            # CORRECTED ON ROUND 8. My first version returned an EMPTY result plus a
            # diagnostic, designed so download_item()'s existing
            # `if not links: ... links = [url]` fallback would fire. That works for
            # exactly one caller, and I validated it against exactly that caller.
            # There are FIVE production consumers of scrape_links():
            #
            #   api/routes/downloads.py:361   POST /download/scrape
            #   api/routes/downloads.py:419   /download/copy-links
            #   download_service.py           download_item()      <- the only one
            #   hdencode_action_service.py    RSS action retrieval     with a
            #   ui/controllers/download_controller.py  batch scrape     fallback
            #
            # The other four treat "no links" as failure, so a pasted Rapidgator URL
            # returned nothing from /download/scrape, was filed as a FAILURE by
            # copy-links, and silently produced nothing through the RSS action
            # service and the UI batch scrape. A caller-level regression that no
            # amount of dispatch testing inside this module could see.
            #
            # "Give me downloadable links" should return the downloadable link to
            # every caller. A success-with-passthrough side channel would instead
            # make all five learn a new convention, which is how this class of bug
            # keeps happening.
            #
            # THE SUPPORTED-HOST GATE IS NOT OPTIONAL. Identity knows 13 direct
            # hosts (source_identity.DIRECT_FILE_HOSTS); the downloader can only
            # hand off 4 (_SUPPORTED_DOWNLOAD_HOSTS). Returning [url] for the other
            # 9 would hand download_item a host it currently refuses -- a silent
            # behaviour change smuggled in as a bug fix. For those, the diagnostic
            # is the honest answer: we know exactly what this link is and cannot
            # take it.
            if self._is_supported_download_link(url):
                return ScrapedLinks([url])
            return ScrapedLinks(diagnostic=ScrapeDiagnostic(
                ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE,
                retryable=False,
                affects_source_health=False,
                stage="source_gate",
                cause_code="direct_link_unsupported_host",
                transport_attempted=False,
                affected_scope="item",
                retry_mode="none",
                health_owner="none",
            ))
        if source_kind == "other":
            return ScrapedLinks(diagnostic=ScrapeDiagnostic(
                ScrapeCode.UNSUPPORTED_SOURCE,
                retryable=False,
                affects_source_health=False,
                stage="source_gate",
                cause_code="unsupported_source",
                transport_attempted=False,
                affected_scope="item",
                retry_mode="none",
                action_code="remove_item",
                health_owner="none",
            ))
        if source_kind not in ("hdencode", "ddlbase", "adithd"):
            raise AssertionError(
                f"unhandled source kind {source_kind!r} in scrape_links dispatch; "
                "add an explicit branch rather than letting it reach the HDEncode "
                "implementation"
            )

        try:
            if source_kind != "hdencode":
                _ensure_selenium()
        except Exception as e:
            diagnostic = ScrapeDiagnostic(
                ScrapeCode.BROWSER_LAUNCH_FAILED,
                retryable=True,
                affects_source_health=False,
                signals=(type(e).__name__,),
                detail=f"Selenium/Chromium could not initialize: {e}",
            )
            self._log(f"[HDEncode] {diagnostic.message}", "error")
            return ScrapedLinks(diagnostic=diagnostic)
        from bs4 import BeautifulSoup

        # Track active scrapes separately from driver access
        with self._scrape_count_lock:
            self._active_scrapes += 1

        try:
            with self._driver_lock:
                if source_kind == "ddlbase":
                    return ScrapedLinks(
                        self._scrape_ddlbase_links(
                            url,
                            progress_callback=progress_callback,
                        )
                    )
                if source_kind == "adithd":
                    return ScrapedLinks(
                        self._scrape_adithd_links(url, service_type)
                    )

                # HDEncode -- reached only for source_kind == "hdencode" now that
                # the four other kinds are dispatched explicitly above. This was
                # the `default` branch, i.e. the semantic default-to-HDEncode the
                # affirmative classifier was built to remove.
                # Map the requested host to its link keyword.
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
                    driver, navigation_diagnostic = self._navigate_with_diagnostic(
                        url, tag="HDEncode"
                    )
                    if driver is None:
                        return ScrapedLinks(diagnostic=navigation_diagnostic)

                    wait_diagnostic = self._wait_past_cloudflare(
                        driver,
                        source_kind=source_kind,
                    )
                    if wait_diagnostic is not None:
                        return ScrapedLinks(diagnostic=wait_diagnostic)

                    try:
                        page_title = driver.title
                    except Exception:
                        page_title = "?"
                    self._log(f"[HDEncode] Page loaded (title: {page_title!r})")

                    visible_links = _extract_requested_host_links(
                        driver.page_source,
                        keyword,
                    )
                    if visible_links:
                        self._log(
                            f"[HDEncode] Found {len(visible_links)} already-visible "
                            f"{service_type} link(s)",
                            "success",
                        )
                        # SECOND success path, and I only found it because the
                        # wiring test for the reset came back with the links
                        # present and the coordinator never called. I had put the
                        # reset on the post-click branch alone and would have
                        # reported the reset "wired" on a diff read.
                        #
                        # It counts as success for the same reason the other one
                        # does: HDEncode delivered file-host links. The rule is
                        # deliberately "the source served links", not "the reveal
                        # control worked" -- a page that needs no reveal is still
                        # a page HDEncode is not throttling, and leaving the streak
                        # inflated here would half-fix the escalation ratchet.
                        if source_kind == "hdencode":
                            get_hdencode_coordinator().observe_reveal_success()
                        return ScrapedLinks(visible_links)

                    self._log("[HDEncode] Looking for the 'View links' button...")
                    access_btn = self._find_reveal_control(driver)

                    if not access_btn:
                        tier = getattr(self, "_last_reveal_tier", None)
                        if tier == "not-ready":
                            # Not a missing button. HDEncode served the unlock
                            # form and never finished verifying it. Production
                            # evidence 2026-08-06: three reveals succeeded in
                            # 0.1-0.8s, then five consecutive attempts sat at
                            # "Verifying... Please wait" for the full 60s
                            # ceiling, with the page shape identical throughout
                            # (6 forms, same #unlocked action, 92-94 links).
                            #
                            # THAT IS A STATE TRANSITION, NOT A PROVEN CAUSE.
                            # Corrected 2026-08-08 on peer review round 9. This
                            # comment used to end "That is rate-limiting." and the
                            # log asserted it as fact. What is actually observed is
                            # only: a not-ready reveal state was seen, and no usable
                            # links control appeared before OUR 60-second deadline.
                            #
                            # Every 60s observation is RIGHT-CENSORED -- we stop
                            # measuring at 60s, so the data cannot distinguish a
                            # widget that would have finished at 62s from one that
                            # never finishes. And source-side limiting is
                            # indistinguishable here from browser/session state:
                            # ScanHound reuses a PERSISTENT Chromium profile
                            # (--user-data-dir=/data/browser-profiles/hdencode), so
                            # cookies and site state survive process restarts.
                            #
                            # Naming an unproven cause in the log is not harmless:
                            # it is what a reader (including me, for days) treats as
                            # the finding, and it justified an expensive source-wide
                            # cooldown. See docs/reviews/peer-rounds/
                            # reveal-stall-root-cause.md.
                            self._log(
                                "[HDEncode] The reveal control did not finish "
                                "verifying within the 60s observation window "
                                f"(title: {page_title!r}); cause is not yet "
                                "established. Cooling down.",
                                "warning",
                            )
                        else:
                            self._log(
                                f"[HDEncode] No 'View links' button found (title: {page_title!r}). "
                                "Page may be a Cloudflare wall, login gate, or changed layout.",
                                "warning",
                            )
                        diagnostic = self._log_page_diagnostics(
                            driver, stage="access_control", reveal_tier=tier
                        )
                        return ScrapedLinks(diagnostic=diagnostic)

                    try:
                        btn_desc = access_btn.get_attribute("value") or access_btn.text or access_btn.tag_name
                    except Exception:
                        btn_desc = "?"
                    self._log(f"[HDEncode] Access control found ({btn_desc!r}) — clicking")
                    driver.execute_script("arguments[0].scrollIntoView();", access_btn)
                    try:
                        access_btn.click()
                    except Exception as click_exc:
                        self._log(
                            f"[HDEncode] Native click failed "
                            f"({type(click_exc).__name__}); using JS fallback",
                            "warning",
                        )
                        driver.execute_script("arguments[0].click();", access_btn)

                    # The click submits the unlock form, which is a NEW top-level
                    # navigation — and Cloudflare can challenge that POST even
                    # when the initial page load was clean. Without re-running the
                    # capture here, `_last_cf_mitigated` stays at whatever the
                    # FIRST navigation set (usually None) and the diagnostics
                    # below report "no links found" for what is actually a
                    # source-wide interstitial. This is the normal operating path,
                    # not an edge case: submitting the form is the whole point.
                    #
                    # `_wait_past_cloudflare` is reused rather than a bare
                    # `_capture_cf_mitigated` so the page-evidence fallback still
                    # applies to the post-click page. It returns immediately when
                    # the page carries no challenge markers.
                    post_click_diagnostic = self._wait_past_cloudflare(
                        driver,
                        source_kind=source_kind,
                    )
                    if post_click_diagnostic is not None:
                        return ScrapedLinks(diagnostic=post_click_diagnostic)

                    self._log(f"[HDEncode] Clicked — waiting up to 8s for '{keyword}' links to appear")
                    try:
                        _WebDriverWait(driver, 8).until(
                            _EC.presence_of_element_located((_By.XPATH, f"//a[contains(@href, '{keyword}')]"))
                        )
                    except Exception:
                        self._log(f"[HDEncode] No {service_type} links appeared after clicking", "warning")
                        diagnostic = self._log_page_diagnostics(
                            driver, keyword=keyword, stage="requested_host"
                        )
                        return ScrapedLinks(diagnostic=diagnostic)

                    links = _extract_requested_host_links(
                        driver.page_source,
                        keyword,
                    )

                    if links:
                        self._log(f"[HDEncode] Found {len(links)} {service_type} link(s); first: {links[0]}", "success")
                        # THE MIRROR OF observe_reveal_stall, wired 2026-08-07.
                        #
                        # observe_reveal_success() existed with NO production
                        # caller -- the fifth "signal nothing consumes" of this
                        # effort, and its own docstring named the consequence:
                        # the stall streak ratchets up and every later stall draws
                        # the maximum cooldown regardless of how healthy the source
                        # had been in between. The streak is in-memory with no
                        # persistence, so the only thing that cleared it was a
                        # container restart -- a throttle dial reset by process
                        # lifetime instead of by evidence.
                        #
                        # HERE, and not at tier == "links-control", because a
                        # control being present is not proof the reveal completed:
                        # the click can still be challenged, time out, or yield
                        # nothing. Delivered file-host links are unambiguous. The
                        # `source_kind` gate matches observe_reveal_stall's exactly
                        # so the two cannot drift.
                        if source_kind == "hdencode":
                            get_hdencode_coordinator().observe_reveal_success()
                    else:
                        self._log(f"[HDEncode] 0 {service_type} links parsed from the page", "warning")
                        diagnostic = self._log_page_diagnostics(
                            driver, keyword=keyword, stage="requested_host"
                        )
                        return ScrapedLinks(diagnostic=diagnostic)
                    return ScrapedLinks(links)

                except Exception as e:
                    diagnostic = ScrapeDiagnostic(
                        ScrapeCode.SCRAPE_EXCEPTION,
                        retryable=True,
                        affects_source_health=False,
                        signals=(type(e).__name__,),
                        detail=f"Link scrape failed: {e}",
                    )
                    self._log(f"[HDEncode] Error scraping {url}: {e}", "error")
                    return ScrapedLinks(diagnostic=diagnostic)
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
                self._log_page_diagnostics(
                    driver, source_kind="ddlbase"
                )
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
                      progress_callback: Optional[Callable] = None,
                      force: bool = False) -> Dict[str, Any]:
        """Download a single item: scrape links, send to JD or clipboard.

        Returns dict with 'success', 'method', 'link_count', 'message'.
        """
        result = {
            "success": False,
            "method": "",
            "link_count": 0,
            "message": "",
            "history_saved": False,
            "reason_code": None,
            "cause_code": None,
            "stage": None,
            "retryable": False,
            "retry_mode": "none",
            "cooldown_until": None,
            "transport_attempted": None,
            # AFFIRMATIVE SOURCE-PROGRESS SIGNAL, added 2026-08-07 on peer review.
            # Set to True ONLY by a path that genuinely crossed the source
            # boundary. The pre-scrape dedup returns success without contacting
            # the source and leaves this False, which is the distinction the
            # queue's retry-budget refund depends on.
            #
            # A previous attempt inferred this from transport_attempted, which
            # does not work: that field is initialised None here and NONE of the
            # real success paths set it, so the inference silently never fired.
            # Only an explicit signal, set where the delivery actually happens,
            # is trustworthy.
            "source_progress": False,
            "affected_scope": "item",
            "action_code": None,
            "deferred": False,
            "signals": [],
        }

        if not url:
            result["message"] = "No URL provided"
            return result

        # Dedup: if this exact release was already grabbed successfully, don't
        # scrape or re-send it — that just creates a duplicate JDownloader entry.
        # (A prior *failed* grab doesn't count, so retries still work.)
        # `force=True` (used only by the pipeline tracker's regrab/grab-alternative
        # actions) skips both gates entirely — that's the user explicitly
        # overriding "don't re-grab," not an accident to guard against.
        if self.db is not None and not force:
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
            # Title-level dedup: a DIFFERENT release URL of the same title
            # (same year + season) that is the same-or-lower quality than a
            # copy already grabbed is a duplicate too — that's how "grab both
            # 4K remuxes of the same movie" slipped through. Only a genuine
            # upgrade (higher resolution, or DV gain at the same resolution)
            # passes. Legacy rows without a recorded year match on title+season
            # alone; season must match exactly so S01 never blocks S02.
            prior = self._best_prior_grab(title, year, season)
            if prior is not None and not self._is_quality_upgrade(
                    resolution, dovi, prior):
                result["success"] = True
                result["method"] = "duplicate_similar"
                result["message"] = (
                    f"Already grabbed {prior.get('resolution') or '?'} of "
                    f"{title} — skipped (this is not an upgrade)")
                self._log(f"[Download] skip same-title duplicate: {title} "
                          f"({resolution or '?'} vs grabbed {prior.get('resolution') or '?'})",
                          "info")
                self._progress("download:complete",
                               {"title": title, "url": url,
                                "method": "duplicate_similar", "link_count": 0},
                               _cb=progress_callback)
                return result

        _cb = progress_callback
        self._progress("download:started", {"title": title, "url": url}, _cb=_cb)

        # Step 1: Scrape links from page
        scrape_failed = False
        diagnostic = None
        try:
            links = self.scrape_links(url, service_type, progress_callback=_cb)
            diagnostic = getattr(links, "diagnostic", None)
            if self._source_kind_of(url) == "hdencode":
                record_scrape_outcome(self.db, "hdencode", links)
        except Exception as e:
            links = []
            scrape_failed = True
            self._log(f"Scrape error: {e}", "warning")
            self._progress(
                "download:fallback",
                {
                    "title": title,
                    "reason": "scrape_exception",
                    "signal": type(e).__name__,
                },
                _cb=_cb,
            )

        if not links:
            # Only fall back to the URL itself if it is *already* a file-host
            # link (e.g. user pasted a rapidgator URL). Sending a source page
            # URL (hdencode/ddlbase) to JDownloader just yields a
            # "Blocked by Cloudflare" entry, so refuse it instead.
            if self._is_supported_download_link(url):
                links = [url]
                diagnostic = None
            else:
                if diagnostic is not None:
                    # API/WS-facing result must not expose diagnostic.detail,
                    # which may contain local paths or driver internals.
                    diagnostic_payload = diagnostic.to_dict()
                    result.update(diagnostic_payload)
                    msg = diagnostic_payload["message"]
                else:
                    msg = (
                        "No download links found on the source page."
                        if not scrape_failed else
                        "Scrape failed — could not retrieve download links."
                    )
                self._log(f"[Download] {title}: {msg}", "warning")
                result["message"] = msg
                self._progress("download:no_links", {"title": title, "url": url}, _cb=_cb)
                self._progress(
                    "download:failed",
                    {"title": title, "url": url, **result},
                    _cb=_cb,
                )
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
        package_name = compute_package_name(title, year, resolution, season=season)

        # Per-type download folder: TV (has a season) vs movies, when
        # configured. 4K movies get their OWN folder when set, so they can be
        # downloaded/extracted straight onto the same physical drive as the 4K
        # library — turning the post-download rename from a slow cross-drive
        # copy into an instant same-volume move. Falls back to jd_movies_folder.
        if season is not None:
            destination = (self.config.get("jd_tv_folder") or "").strip()
        elif self._res_rank(resolution) >= self._res_rank("2160p"):
            destination = ((self.config.get("jd_movies_folder_4k") or "").strip()
                           or (self.config.get("jd_movies_folder") or "").strip())
        else:
            destination = (self.config.get("jd_movies_folder") or "").strip()

        if self.config.get("jd_enabled", False) and (jd_folder or jd_method == "api"):
            if self.send_to_jdownloader(links, package_name, destination=destination, progress_callback=_cb):
                result["success"] = True
                result["method"] = "jdownloader"
                result["source_progress"] = True
                result["message"] = f"Sent {len(links)} links to JDownloader"
                result["history_saved"] = self.save_to_history(
                    url, title, season, resolution, size, status="completed",
                    hdr=hdr, dovi=dovi, year=year,
                    package_name=package_name, service_type=service_type
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
            result["source_progress"] = True
            result["message"] = f"Copied {len(links)} links to clipboard"
            result["history_saved"] = self.save_to_history(
                url, title, season, resolution, size, status="clipboard",
                hdr=hdr, dovi=dovi, year=year,
                package_name=package_name, service_type=service_type
            )
            self._progress("download:complete", {"title": title, "url": url, "method": result["method"], "link_count": result["link_count"]}, _cb=_cb)
            return result

        # Step 4: Last resort — open in the user's browser. Only meaningful on
        # the desktop app; in server/headless mode there is no user browser, so
        # skip it rather than report a phantom success.
        if not self.server_mode and self.open_url(url):
            result["success"] = True
            result["method"] = "browser"
            result["source_progress"] = True
            result["message"] = "Opened URL in browser"
            result["history_saved"] = self.save_to_history(
                url, title, season, resolution, size, status="browser",
                hdr=hdr, dovi=dovi, year=year,
                package_name=package_name, service_type=service_type
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
            self.save_to_history(url, title, season, resolution, size,
                                 status="failed", hdr=hdr, dovi=dovi, year=year,
                                 package_name=package_name, service_type=service_type)
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

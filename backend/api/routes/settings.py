"""Settings endpoints: get/update configuration."""
import ipaddress
import logging
import smtplib
import socket
import threading
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies import ServiceRegistry, get_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

def _validate_outbound_url(url: str) -> None:
    """Validate a user-supplied URL for SSRF safety.

    Rejects non-HTTP(S) schemes, loopback, link-local, and private IPs.
    Raises HTTPException(400) on violation.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"URL scheme '{parsed.scheme}' not allowed; use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL has no hostname")
    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"Cannot resolve hostname: {hostname}")
    for family, _type, _proto, _canon, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            raise HTTPException(
                status_code=400,
                detail=f"URL resolves to a non-public address ({ip}); outbound requests to internal networks are blocked",
            )


SENSITIVE_KEYS = {
    "plex_token", "plex_password", "tmdb_api_key", "omdb_api_key", "cuty_password",
    "adithd_password", "jd_password", "discord_webhook", "smtp_password",
    "pushover_token", "slack_webhook", "webhook_url",
}


class SettingsUpdate(BaseModel):
    """Validated settings model. All fields optional for partial updates.

    Uses ``extra="forbid"`` so that misspelled / unknown keys are rejected
    with a 422 instead of silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    # Plex Connection
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None
    plex_server_id: Optional[str] = None
    plex_connection_mode: Optional[str] = None
    plex_username: Optional[str] = None
    plex_password: Optional[str] = None
    plex_server_name: Optional[str] = None

    # API Keys
    tmdb_api_key: Optional[str] = None
    omdb_api_key: Optional[str] = None
    use_tmdb: Optional[bool] = None

    # Size & Resolution
    min_size_mb: Optional[int] = None
    pref_res: Optional[str] = None

    # Display Options
    show_rating: Optional[bool] = None
    show_votes: Optional[bool] = None
    show_rt: Optional[bool] = None
    show_rg: Optional[bool] = None
    show_nf: Optional[bool] = None
    show_links: Optional[bool] = None
    show_genres: Optional[bool] = None

    # Download
    download_dir: Optional[str] = None
    download_service_type: Optional[str] = None

    # Cache Settings
    cache_duration: Optional[int] = None
    plex_refresh_mode: Optional[str] = None
    plex_invalidate_on_new_content: Optional[bool] = None

    # Filtering
    ignore_keywords: Optional[str] = None

    # Upgrade Rules
    upgrade_sensitivity: Optional[int] = None

    # Background pre-cache scanning
    background_scan_enabled: Optional[bool] = None
    background_scan_interval_hours: Optional[int] = None
    background_scan_pages: Optional[int] = None
    background_scan_sources: Optional[List[str]] = None
    background_scan_retain_days: Optional[int] = None

    # Auto-rename + Plex sort + optional Ollama assist
    auto_rename_enabled: Optional[bool] = None
    auto_rename_confidence_threshold: Optional[int] = None
    auto_rename_require_confirmation: Optional[bool] = None
    auto_rename_move_method: Optional[str] = None
    auto_rename_movie_library: Optional[str] = None
    auto_rename_tv_library: Optional[str] = None
    auto_rename_template_movie: Optional[str] = None
    auto_rename_template_tv: Optional[str] = None
    auto_rename_plex_sort_titles: Optional[bool] = None
    auto_rename_llm_enabled: Optional[bool] = None
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None
    rule_1080_4k: Optional[bool] = None
    rule_1080_4k_size: Optional[bool] = None
    rule_1080_1080: Optional[bool] = None
    rule_4k_4k: Optional[bool] = None
    rule_dv: Optional[bool] = None
    strict_resolution: Optional[bool] = None

    # Libraries
    movie_libs: Optional[List[str]] = None
    tv_libs: Optional[List[str]] = None
    known_libraries: Optional[List[str]] = None

    # JDownloader Integration
    jd_enabled: Optional[bool] = None
    jd_method: Optional[str] = None
    jd_folder: Optional[str] = None
    jd_movies_folder: Optional[str] = None
    jd_tv_folder: Optional[str] = None
    jd_email: Optional[str] = None
    jd_password: Optional[str] = None
    jd_device: Optional[str] = None

    # Filtering
    exclude_720p: Optional[bool] = None

    # Sources
    source_2160p: Optional[bool] = None
    source_remux: Optional[bool] = None
    source_tv_packs: Optional[bool] = None

    # DDLBase / Cuty.io
    ddlbase_enabled: Optional[bool] = None
    ddlbase_manual_resolution_timeout: Optional[int] = None
    cuty_email: Optional[str] = None
    cuty_password: Optional[str] = None

    # Adit-HD Forum
    adithd_enabled: Optional[bool] = None
    adithd_username: Optional[str] = None
    adithd_password: Optional[str] = None
    adithd_auto_reply: Optional[bool] = None
    adithd_preferred_host: Optional[str] = None

    # Scheduler
    scheduler_enabled: Optional[bool] = None
    scheduler_interval: Optional[int] = None
    last_scan_time: Optional[float] = None

    # Debug & Logging
    debug_mode: Optional[bool] = None
    clear_logs_startup: Optional[bool] = None
    scan_threads: Optional[int] = None
    verbose_logging: Optional[bool] = None

    # Matching thresholds
    tv_match_threshold: Optional[int] = None
    low_match_threshold: Optional[int] = None
    movie_match_threshold: Optional[int] = None
    year_tolerance: Optional[int] = None

    # Scanner
    base_url: Optional[str] = None
    scheduler_only_when_idle: Optional[bool] = None

    # Display
    tile_columns: Optional[int] = None

    # Appearance
    theme_mode: Optional[str] = None

    # System Tray & Startup
    enable_system_tray: Optional[bool] = None
    minimize_to_tray: Optional[bool] = None
    start_minimized: Optional[bool] = None
    auto_connect_plex: Optional[bool] = None

    # Plex Account (remote)
    plex_selected_server: Optional[str] = None

    # Auto-Grab
    auto_grab_enabled: Optional[bool] = None
    auto_grab_min_rating: Optional[float] = None
    auto_grab_min_votes: Optional[int] = None
    auto_grab_genres: Optional[str] = None
    auto_grab_exclude_genres: Optional[str] = None
    auto_grab_languages: Optional[str] = None
    auto_grab_statuses: Optional[str] = None

    # Notifications
    desktop_notifications: Optional[bool] = None
    discord_webhook: Optional[str] = None
    discord_username: Optional[str] = None
    slack_webhook: Optional[str] = None
    pushover_user: Optional[str] = None
    pushover_token: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_method: Optional[str] = None
    email_enabled: Optional[bool] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    email_from: Optional[str] = None
    email_to: Optional[str] = None
    smtp_tls: Optional[bool] = None


def _mask_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return config with sensitive values masked."""
    masked = dict(config)
    for key in SENSITIVE_KEYS:
        if key in masked and masked[key]:
            masked[key] = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
    return masked


@router.get("")
def get_settings(reg: ServiceRegistry = Depends(get_registry)):
    return _mask_config(reg.config)


@router.put("")
def update_settings(
    updates: SettingsUpdate,
    reg: ServiceRegistry = Depends(get_registry),
):
    # Only include fields the caller actually sent (exclude_unset=True)
    raw = updates.model_dump(exclude_unset=True)
    # Filter out masked values (user didn't change them)
    real_updates = {
        k: v for k, v in raw.items()
        if v != "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
    }
    # Track explicitly cleared sensitive keys so save_config won't restore them
    if reg.backend:
        cleared = getattr(reg.backend, '_cleared_keys', set())
        for k, v in real_updates.items():
            if k in SENSITIVE_KEYS and not v:
                cleared.add(k)
            elif k in SENSITIVE_KEYS and v:
                cleared.discard(k)
        reg.backend._cleared_keys = cleared
    reg.config.update(real_updates)
    if reg.backend:
        reg.backend.save_config()
    return {"status": "ok", "updated_keys": list(real_updates.keys())}


@router.post("/test/{channel}")
def test_notification(
    channel: str,
    reg: ServiceRegistry = Depends(get_registry),
):
    """Send a test notification on the specified channel."""
    cfg = reg.config
    try:
        if channel == "desktop":
            from plyer import notification as plyer_notif
            plyer_notif.notify(
                title="ScanHound Test",
                message="Desktop notifications are working!",
                timeout=5,
            )
            return {"success": True, "message": "Desktop notification sent"}

        elif channel == "discord":
            url = cfg.get("discord_webhook", "")
            username = cfg.get("discord_username", "ScanHound")
            if not url:
                raise HTTPException(status_code=400, detail="No Discord webhook URL configured")
            _validate_outbound_url(url)
            resp = requests.post(url, json={
                "username": username,
                "content": "ScanHound test notification - Discord webhook is working!",
            }, timeout=10)
            if resp.status_code in (200, 204):
                return {"success": True, "message": "Discord test sent"}
            raise HTTPException(status_code=502, detail=f"Discord returned HTTP {resp.status_code}")

        elif channel == "slack":
            url = cfg.get("slack_webhook", "")
            if not url:
                raise HTTPException(status_code=400, detail="No Slack webhook URL configured")
            _validate_outbound_url(url)
            resp = requests.post(url, json={
                "text": "ScanHound test notification - Slack webhook is working!",
            }, timeout=10)
            if resp.status_code == 200:
                return {"success": True, "message": "Slack test sent"}
            raise HTTPException(status_code=502, detail=f"Slack returned HTTP {resp.status_code}")

        elif channel == "pushover":
            user = cfg.get("pushover_user", "")
            token = cfg.get("pushover_token", "")
            if not user or not token:
                raise HTTPException(status_code=400, detail="Pushover user key and API token required")
            resp = requests.post("https://api.pushover.net/1/messages.json", data={
                "token": token,
                "user": user,
                "title": "ScanHound Test",
                "message": "Pushover notifications are working!",
            }, timeout=10)
            data = resp.json()
            if data.get("status") == 1:
                return {"success": True, "message": "Pushover test sent"}
            raise HTTPException(status_code=502, detail=str(data.get("errors", "Unknown error")))

        elif channel == "webhook":
            url = cfg.get("webhook_url", "")
            method = cfg.get("webhook_method", "POST")
            if not url:
                raise HTTPException(status_code=400, detail="No webhook URL configured")
            _validate_outbound_url(url)
            payload = {"event": "test", "message": "ScanHound webhook test"}
            if method.upper() == "GET":
                resp = requests.get(url, params=payload, timeout=10)
            elif method.upper() == "PUT":
                resp = requests.put(url, json=payload, timeout=10)
            else:
                resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code < 400:
                return {"success": True, "message": f"Webhook test sent (HTTP {resp.status_code})"}
            raise HTTPException(status_code=502, detail=f"Webhook returned HTTP {resp.status_code}")

        elif channel == "email":
            host = cfg.get("smtp_host", "")
            if not host:
                raise HTTPException(status_code=400, detail="No SMTP host configured")
            port = cfg.get("smtp_port", 587)
            msg = MIMEText("ScanHound email notifications are working!")
            msg["Subject"] = "ScanHound Test Email"
            msg["From"] = cfg.get("email_from", "")
            msg["To"] = cfg.get("email_to", "")
            with smtplib.SMTP(host, port, timeout=10) as server:
                if cfg.get("smtp_tls", True):
                    server.starttls()
                username = cfg.get("smtp_username", "")
                password = cfg.get("smtp_password", "")
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
            return {"success": True, "message": "Test email sent"}

        elif channel == "tmdb":
            key = cfg.get("tmdb_api_key", "")
            if not key:
                raise HTTPException(status_code=400, detail="No TMDB API key configured")
            resp = requests.get(
                f"https://api.themoviedb.org/3/configuration?api_key={key}",
                timeout=10,
            )
            if resp.status_code == 200:
                return {"success": True, "message": "TMDB API key is valid"}
            raise HTTPException(status_code=502, detail=f"TMDB returned HTTP {resp.status_code}")

        elif channel == "omdb":
            key = cfg.get("omdb_api_key", "")
            if not key:
                raise HTTPException(status_code=400, detail="No OMDb API key configured")
            resp = requests.get(
                f"https://www.omdbapi.com/?apikey={key}&t=test",
                timeout=10,
            )
            data = resp.json()
            if data.get("Response") == "True" or resp.status_code == 200:
                return {"success": True, "message": "OMDb API key is valid"}
            raise HTTPException(status_code=502, detail=data.get("Error", f"HTTP {resp.status_code}"))

        elif channel == "plex":
            url = cfg.get("plex_url", "")
            token = cfg.get("plex_token", "")
            if not url or not token:
                raise HTTPException(status_code=400, detail="Plex URL and token required")
            resp = requests.get(
                f"{url.rstrip('/')}/identity",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Plex connection successful"}
            raise HTTPException(status_code=502, detail=f"Plex returned HTTP {resp.status_code}")

        else:
            raise HTTPException(status_code=400, detail=f"Unknown channel: {channel}")

    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {e}")
    except Exception as e:
        logger.warning("Test notification failed for %s: %s", channel, e)
        raise HTTPException(status_code=502, detail=str(e))

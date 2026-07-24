"""Browser adapter boundary for HDEncode link retrieval.

The production default is standard Selenium controlling the apt-matched
Chromium/ChromeDriver pair with a dedicated persistent profile.  The historical
undetected-chromedriver path remains available as an explicit rollback adapter.

This module does not attempt to solve or bypass interactive challenges.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Dict, Optional, Tuple


_ALLOWED_ADAPTERS = {"selenium_chromium", "uc_chromium"}
_ALLOWED_PROFILE_MODES = {"persistent", "temporary"}


@dataclass(frozen=True)
class BrowserPlan:
    adapter: str
    profile_mode: str
    profile_dir: Optional[str]
    chrome_bin: Optional[str]
    system_driver: Optional[str]


def _adapter(config: Dict[str, Any]) -> str:
    value = (
        os.environ.get("SCANHOUND_HDENCODE_BROWSER_ADAPTER")
        or config.get("hdencode_browser_adapter")
        or "selenium_chromium"
    )
    value = str(value).strip().lower()
    return value if value in _ALLOWED_ADAPTERS else "selenium_chromium"


def _profile_mode(config: Dict[str, Any]) -> str:
    value = (
        os.environ.get("SCANHOUND_HDENCODE_BROWSER_PROFILE_MODE")
        or config.get("hdencode_browser_profile_mode")
        or "persistent"
    )
    value = str(value).strip().lower()
    return value if value in _ALLOWED_PROFILE_MODES else "persistent"


def _profile_dir(config: Dict[str, Any], mode: str) -> Optional[str]:
    if mode != "persistent":
        return None
    configured = (
        os.environ.get("SCANHOUND_BROWSER_PROFILE_DIR")
        or config.get("hdencode_browser_profile_dir")
    )
    if configured:
        path = Path(str(configured)).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        path = base / "ScanHound" / "browser-profiles" / "hdencode"
    else:
        # Docker sets HOME=/data, so this lands on the durable /data volume.
        base = Path(os.environ.get("HOME") or "/data")
        path = base / "browser-profiles" / "hdencode"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return str(path)


_PROFILE_LOCK_NAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
)

_PROFILE_CACHE_LIMIT_BYTES = 256 * 1024 * 1024
_PROFILE_CACHE_RELATIVE_PATHS = (
    Path("Default") / "Cache",
    Path("Default") / "Code Cache",
    Path("Default") / "GPUCache",
    Path("Default") / "Media Cache",
    Path("Default") / "Service Worker" / "CacheStorage",
    Path("Default") / "Service Worker" / "ScriptCache",
    Path("ShaderCache"),
    Path("GrShaderCache"),
    Path("GraphiteDawnCache"),
    Path("DawnCache"),
)


def profile_lock_paths(
    config: Dict[str, Any],
    *,
    chrome_bin: Optional[str] = None,
    system_driver: Optional[str] = None,
) -> tuple[Path, ...]:
    """Return only Chromium's process-lock artifacts for the active profile.

    The browser launcher and Docker startup cleanup must resolve the profile
    through this same module so a configured path cannot drift from cleanup.
    Temporary profiles intentionally return no persistent lock paths.
    """
    plan = browser_plan(
        config,
        chrome_bin=chrome_bin,
        system_driver=system_driver,
    )
    if plan.profile_mode != "persistent" or not plan.profile_dir:
        return ()
    root = Path(plan.profile_dir)
    return tuple(root / name for name in _PROFILE_LOCK_NAMES)


def clear_stale_profile_locks(
    config: Dict[str, Any],
    *,
    chrome_bin: Optional[str] = None,
    system_driver: Optional[str] = None,
) -> tuple[str, ...]:
    """Remove only stale Singleton* artifacts from the resolved profile.

    This is safe only before a browser process exists. Docker invokes it from
    the entrypoint before ScanHound starts; normal browser launches do not
    remove locks from a potentially live profile.
    """
    removed: list[str] = []
    for path in profile_lock_paths(
        config,
        chrome_bin=chrome_bin,
        system_driver=system_driver,
    ):
        try:
            if os.path.lexists(path):
                os.unlink(path)
                removed.append(str(path))
        except FileNotFoundError:
            continue
    return tuple(removed)


def _path_size(path: Path) -> int:
    try:
        if path.is_symlink() or path.is_file():
            return int(path.stat().st_size)
    except OSError:
        return 0
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += int(child.stat().st_size)
            except OSError:
                continue
    except OSError:
        return total
    return total


def prune_profile_caches(
    config: Dict[str, Any],
    *,
    max_bytes: int = _PROFILE_CACHE_LIMIT_BYTES,
) -> tuple[str, ...]:
    """Bound known cache-only profile data while retaining cookies/site data.

    This runs only at process startup, before a browser can own the profile.
    Cookies, Local Storage, IndexedDB, and service-worker registrations are not
    touched.
    """
    plan = browser_plan(config)
    if plan.profile_mode != "persistent" or not plan.profile_dir:
        return ()
    root = Path(plan.profile_dir)
    candidates = [root / relative for relative in _PROFILE_CACHE_RELATIVE_PATHS]
    cache_bytes = sum(_path_size(path) for path in candidates if path.exists())
    if cache_bytes <= max(0, int(max_bytes)):
        return ()

    removed: list[str] = []
    for path in candidates:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                continue
            removed.append(str(path))
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return tuple(removed)


def _runtime_profile_config() -> Dict[str, Any]:
    """Load the same persisted profile settings used by the application."""
    from backend.config import CONFIG_FILE, get_default_config, validate_config

    config: Dict[str, Any] = dict(get_default_config())
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            config.update(saved)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        # Startup cleanup remains best-effort, matching the historical shell
        # behavior. The entrypoint reports a warning if this command fails.
        pass
    return validate_config(config)


def browser_plan(
    config: Dict[str, Any],
    *,
    chrome_bin: Optional[str] = None,
    system_driver: Optional[str] = None,
) -> BrowserPlan:
    mode = _profile_mode(config)
    return BrowserPlan(
        adapter=_adapter(config),
        profile_mode=mode,
        profile_dir=_profile_dir(config, mode),
        chrome_bin=chrome_bin,
        system_driver=system_driver,
    )


def _arguments(plan: BrowserPlan) -> list[str]:
    args = [
        "--window-size=1920,1080",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    if plan.profile_dir:
        args.append(f"--user-data-dir={plan.profile_dir}")
        args.append("--profile-directory=Default")
    return args


def _status(
    driver,
    plan: BrowserPlan,
    *,
    launch_error: Optional[str] = None,
) -> dict:
    capabilities = getattr(driver, "capabilities", {}) if driver is not None else {}
    chrome = capabilities.get("chrome") or {}
    driver_version = str(chrome.get("chromedriverVersion") or "").split(" ", 1)[0]
    profile_id = None
    if plan.profile_dir:
        profile_id = hashlib.sha256(plan.profile_dir.encode("utf-8")).hexdigest()[:12]
    return {
        "adapter": plan.adapter,
        "profile_mode": plan.profile_mode,
        "profile_id": profile_id,
        "browser_name": capabilities.get("browserName") if capabilities else None,
        "browser_version": capabilities.get("browserVersion") if capabilities else None,
        "driver_version": driver_version or None,
        "headed": True,
        "display": "xvfb" if os.environ.get("DISPLAY") else "native",
        "launch_error": launch_error,
    }


def launch_browser(
    config: Dict[str, Any],
    *,
    chrome_ver: Optional[int],
    chrome_bin: Optional[str],
    system_driver: Optional[str],
) -> Tuple[Any, dict]:
    """Launch the selected adapter and return ``(driver, safe_status)``."""
    plan = browser_plan(
        config,
        chrome_bin=chrome_bin,
        system_driver=system_driver,
    )
    args = _arguments(plan)

    if plan.adapter == "uc_chromium":
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        for arg in args:
            options.add_argument(arg)
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin
        kwargs: Dict[str, Any] = {
            "options": options,
            "version_main": chrome_ver,
        }
        if system_driver and os.path.exists(system_driver):
            kwargs["driver_executable_path"] = system_driver
        driver = uc.Chrome(**kwargs)
        return driver, _status(driver, plan)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()
    for arg in args:
        options.add_argument(arg)
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin
    service = (
        Service(executable_path=system_driver)
        if system_driver and os.path.exists(system_driver)
        else Service()
    )
    driver = webdriver.Chrome(service=service, options=options)
    return driver, _status(driver, plan)


def safe_status_without_driver(
    config: Dict[str, Any],
    *,
    chrome_bin: Optional[str] = None,
    system_driver: Optional[str] = None,
    launch_error: Optional[str] = None,
) -> dict:
    return _status(
        None,
        browser_plan(
            config,
            chrome_bin=chrome_bin,
            system_driver=system_driver,
        ),
        launch_error=launch_error,
    )


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ScanHound browser lifecycle helper")
    parser.add_argument(
        "--cleanup-stale-profile-locks",
        action="store_true",
        help="remove only Singleton* files from the configured persistent profile",
    )
    args = parser.parse_args(argv)
    if not args.cleanup_stale_profile_locks:
        parser.error("no action requested")
    config = _runtime_profile_config()
    paths = profile_lock_paths(config)
    removed = clear_stale_profile_locks(config)
    pruned = prune_profile_caches(config)
    if paths:
        print(
            "[browser-profile] checked "
            f"{paths[0].parent}; removed {len(removed)} stale lock artifact(s), "
            f"pruned {len(pruned)} cache path(s)"
        )
    else:
        print("[browser-profile] temporary profile mode; no persistent locks to clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Browser adapter boundary for HDEncode link retrieval.

The production default is standard Selenium controlling the apt-matched
Chromium/ChromeDriver pair with a dedicated persistent profile.  The historical
undetected-chromedriver path remains available as an explicit rollback adapter.

This module does not attempt to solve or bypass interactive challenges.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
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

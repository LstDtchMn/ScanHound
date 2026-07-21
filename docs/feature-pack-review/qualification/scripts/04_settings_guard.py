#!/usr/bin/env python3
"""Stage ScanHound's automation/RSS flags for production qualification.

Repaired 2026-07-21 (Claude, git+validation lane) against the REAL ScanHound
API. The original revision PUT the entire desired dict -- including RSS-only
keys -- to ``/settings``; because ``SettingsUpdate`` is declared
``extra="forbid"`` (backend/api/routes/settings.py) that 422s the WHOLE
request. The correct control surfaces are:

  * ``PUT /settings`` accepts ONLY these automation toggles from this tool's
    desired state (they are the sole ``SettingsUpdate`` fields we touch):
        auto_rename_enabled, auto_grab_enabled, hdencode_enabled,
        background_scan_enabled

  * RSS discovery mode is set through ``POST /rss/mode`` with
    ``{"mode": "listing" | "rss_shadow" | "rss_primary"}``
    (backend/api/routes/rss.py::set_rss_mode). ``rss_primary`` additionally
    requires completed shadow readiness (409 otherwise); ``listing`` and
    ``rss_shadow`` do not. This tool never selects ``rss_primary``.

  * The remaining RSS booleans --
        hdencode_rss_shadow_compare_enabled,
        hdencode_rss_auto_grab_enabled,
        hdencode_rss_listing_fallback_enabled
    -- have NO write endpoint. They are deploy-time config
    (backend/config.py DEFAULT_CONFIG) whose defaults already equal the
    qualification-required values (shadow_compare=True, auto_grab=False,
    listing_fallback=False). This tool VERIFIES them via ``GET /settings``
    (which returns the full masked config) and reports a distinct
    ``unsettable_mismatch`` if the running config diverges, instead of
    issuing a PUT that would 422.

Verification reads ``GET /settings`` after applying and compares every desired
key. Exit code 0 only when all desired keys match.
"""
from __future__ import annotations
import argparse, datetime as dt, json, urllib.error, urllib.request
from pathlib import Path

# Keys writable through PUT /settings -- MUST stay a subset of SettingsUpdate.
SETTINGS_WRITABLE = (
    "auto_rename_enabled",
    "auto_grab_enabled",
    "hdencode_enabled",
    "background_scan_enabled",
)
# RSS booleans with no write endpoint; verify-only against config defaults.
VERIFY_ONLY = (
    "hdencode_rss_shadow_compare_enabled",
    "hdencode_rss_auto_grab_enabled",
    "hdencode_rss_listing_fallback_enabled",
)
# Managed-key universe for the restore stage (extracted from the snapshot).
MANAGED = SETTINGS_WRITABLE + ("hdencode_discovery_mode",) + VERIFY_ONLY

# Desired end-states. ``hdencode_discovery_mode`` is applied via POST /rss/mode;
# the VERIFY_ONLY keys are checked, never written.
DISABLED = {
    "auto_rename_enabled": False,
    "auto_grab_enabled": False,
    "hdencode_enabled": False,
    "background_scan_enabled": False,
    "hdencode_discovery_mode": "listing",
    "hdencode_rss_auto_grab_enabled": False,
}
SHADOW = {
    "auto_rename_enabled": False,
    "auto_grab_enabled": False,
    "hdencode_enabled": True,
    "hdencode_discovery_mode": "rss_shadow",
    "hdencode_rss_shadow_compare_enabled": True,
    "hdencode_rss_auto_grab_enabled": False,
    "hdencode_rss_listing_fallback_enabled": False,
}


class ApiError(Exception):
    def __init__(self, method, path, status, detail):
        super().__init__(f"{method} {path} -> {status}")
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail


def req(base, token, method, path, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")
        except Exception:
            detail = ""
        raise ApiError(method, path, e.code, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--stage", choices=("disabled", "shadow", "restore"), required=True)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--snapshot")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    ev = Path(a.evidence_dir).resolve()
    ev.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    try:
        before = req(a.base_url, a.token, "GET", "/settings")
    except ApiError as e:
        print(json.dumps({"ok": False, "error": "read_before_failed",
                          "status": e.status, "detail": e.detail}, indent=2))
        return 2
    snap = ev / f"settings-before-{a.stage}-{stamp}.json"
    snap.write_text(json.dumps(before, indent=2) + "\n")

    if a.stage == "disabled":
        desired = dict(DISABLED)
    elif a.stage == "shadow":
        desired = dict(SHADOW)
    else:  # restore
        if not a.snapshot:
            print(json.dumps({"ok": False, "error": "restore_requires_snapshot"}, indent=2))
            return 2
        snap_cfg = json.loads(Path(a.snapshot).read_text())
        desired = {k: snap_cfg[k] for k in MANAGED if k in snap_cfg}

    put_payload = {k: v for k, v in desired.items() if k in SETTINGS_WRITABLE}
    mode = desired.get("hdencode_discovery_mode")

    if not a.execute:
        print(json.dumps({
            "dry_run": True, "stage": a.stage, "snapshot": str(snap),
            "desired": desired, "settings_put": put_payload, "rss_mode": mode,
            "verify_only": {k: desired[k] for k in VERIFY_ONLY if k in desired},
        }, indent=2))
        return 0

    try:
        if put_payload:
            req(a.base_url, a.token, "PUT", "/settings", put_payload)
        if mode is not None:
            req(a.base_url, a.token, "POST", "/rss/mode", {"mode": mode})
    except ApiError as e:
        result = {"ok": False, "stage": a.stage, "error": "apply_failed",
                  "failed_call": {"method": e.method, "path": e.path,
                                  "status": e.status, "detail": e.detail},
                  "before_snapshot": str(snap),
                  "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        (ev / f"04_settings_{a.stage}_{stamp}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 2

    after = req(a.base_url, a.token, "GET", "/settings")
    mismatches = {}
    unsettable = {}
    for k, v in desired.items():
        if after.get(k) != v:
            entry = {"expected": v, "actual": after.get(k)}
            (unsettable if k in VERIFY_ONLY else mismatches)[k] = entry
    ok = not mismatches and not unsettable
    result = {
        "ok": ok, "stage": a.stage,
        "settings_put": put_payload, "rss_mode_set": mode,
        "before_snapshot": str(snap),
        "mismatches": mismatches,
        "unsettable_mismatches": unsettable,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (ev / f"04_settings_{a.stage}_{stamp}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

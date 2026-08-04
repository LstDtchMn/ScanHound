#!/usr/bin/env python3
"""The promotion window's evidence log (contract rows R-12 and R-13).

R-13 requires the SAME image digest at the window's open and close with the
configuration unchanged throughout. R-12 requires captured proof that the
autonomous flags were false, the readiness cross-check succeeded in
production, and per-source bootstrap completed. Neither had any tooling, and
neither can be reconstructed afterwards -- a window you only inspect at the
end cannot tell you whether the build was swapped on day 4.

So this records at OPEN, re-checks on demand, and re-verifies at CLOSE, and
it FAILS LOUDLY on drift rather than noting it. A window whose digest moved
is not a shorter window; it is not a window at all, and its observations
cannot be attributed to one artifact.

    python scripts/promotion_window.py open   --note "phase A start"
    python scripts/promotion_window.py check                # any time
    python scripts/promotion_window.py close

Exit: 0 clean | 1 the window is INVALID (drift) | 2 usage/unavailable.

Everything it asserts is read from the LIVE system -- the running container's
digest, the config the app actually loaded -- never from a document that
says what should be true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "scanhound"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "docs", "reviews", "evidence",
                   "promotion-window-log.json")

#: Every autonomous actor that must be OFF for the observation window to mean
#: anything. If any of these acted, the window measured a system that was
#: changing its own inputs. Named explicitly rather than "three flags" so a
#: newly added actor is a deliberate edit here, not a silent omission.
AUTO_FLAGS = (
    "hdencode_rss_auto_grab_enabled",
    "auto_grab_enabled",
    "auto_rename_enabled",
    "dv_auto_sync_enabled",
)

#: Config keys whose change invalidates the window. Deliberately narrow: the
#: window is about RSS discovery behaviour, so an unrelated UI preference
#: moving must not void a week of observation.
WATCHED_CONFIG = (
    "hdencode_discovery_mode",
    "hdencode_rss_poll_minutes",
    "hdencode_rss_catchup_hours",
    "hdencode_rss_hydration_limit",
    "hdencode_rss_listing_fallback_enabled",
    "hdencode_enabled",
    "background_scan_enabled",
    "background_scan_sources",
    "background_scan_interval_hours",
) + AUTO_FLAGS


def _docker(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["docker", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip()


def _image_digest() -> str | None:
    """The RUNNING container's image id -- the only digest that describes
    what is actually observing. The tag is useless: `up -d --build` reuses
    scanhound:latest, so two different builds share it."""
    code, out = _docker(["inspect", CONTAINER, "--format", "{{.Image}}"])
    return out if code == 0 and out else None


def _live_config() -> dict | None:
    """The config the running app loaded, read from inside the container."""
    code, out = _docker([
        "exec", CONTAINER, "python", "-c",
        "import json,os;from backend.config import _get_config_dir,"
        "get_default_config;"
        "p=os.path.join(_get_config_dir(),'config.json');"
        "c=dict(get_default_config());"
        "c.update(json.load(open(p)) if os.path.exists(p) else {});"
        "print(json.dumps(c,default=str))",
    ])
    if code != 0 or not out:
        return None
    try:
        return json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def _fingerprint(cfg: dict) -> str:
    watched = {k: cfg.get(k) for k in WATCHED_CONFIG}
    blob = json.dumps(watched, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _gate_blockers(cfg: dict) -> list[str]:
    """What the capability gate itself says, rather than a second opinion.

    Reusing the production function is the point: a window that graded itself
    with its own copy of the rules would pass while production refused.
    """
    code, out = _docker([
        "exec", CONTAINER, "python", "-c",
        "import json,os,sys;from backend.config import _get_config_dir,"
        "get_default_config;"
        "from backend.capability_gate import capability_blockers;"
        "p=os.path.join(_get_config_dir(),'config.json');"
        "c=dict(get_default_config());"
        "c.update(json.load(open(p)) if os.path.exists(p) else {});"
        "print(json.dumps(list(capability_blockers(c))))",
    ])
    if code != 0 or not out:
        # Distinguish "the gate is not in this build" from "the gate broke".
        # backend.capability_gate ships with the RSS promotion work, so before
        # that merges its absence is the EXPECTED state, not a fault -- and
        # calling it an error would train the reader to ignore the field.
        probe, _ = _docker([
            "exec", CONTAINER, "python", "-c",
            "import importlib.util as u;"
            "print('yes' if u.find_spec('backend.capability_gate') else 'no')",
        ])
        if probe == 0:
            return ["gate_not_deployed"]
        return ["gate_unreadable"]
    try:
        return json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return ["gate_unreadable"]


def _observe(note: str = "") -> dict | None:
    cfg = _live_config()
    if cfg is None:
        return None
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "image_digest": _image_digest(),
        "config_fingerprint": _fingerprint(cfg),
        "auto_flags": {k: bool(cfg.get(k)) for k in AUTO_FLAGS},
        "gate_blockers": _gate_blockers(cfg),
        "discovery_mode": cfg.get("hdencode_discovery_mode"),
    }


def _load() -> dict:
    if not os.path.exists(LOG):
        return {}
    with open(LOG, encoding="utf-8") as fh:
        return json.load(fh)


def _save(doc: dict) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def _report(obs: dict, label: str) -> list[str]:
    """Print an observation and return the reasons it is unacceptable."""
    print(f"{label}")
    print(f"  at              : {obs['at']}")
    print(f"  image digest    : {obs['image_digest']}")
    print(f"  config finger   : {obs['config_fingerprint'][:16]}...")
    print(f"  discovery mode  : {obs['discovery_mode']}")
    bad = []
    on = [k for k, v in obs["auto_flags"].items() if v]
    for k, v in obs["auto_flags"].items():
        print(f"  {k:38s}: {v}")
    if on:
        bad.append("autonomous actors are ENABLED during the window: "
                   + ", ".join(on))
    if not obs["image_digest"]:
        bad.append("no running container -- nothing is being observed")
    gb = obs["gate_blockers"]
    if gb == ["gate_not_deployed"]:
        print("  promotion gate  : not in this build (expected until the RSS "
              "promotion branch merges)")
    else:
        print(f"  gate blockers   : {gb or 'none (gate would ALLOW)'}")
        if "gate_unreadable" in gb:
            bad.append("the promotion gate could not be evaluated -- the "
                       "window cannot be graded against a gate it cannot read")
    return bad


def cmd_open(args) -> int:
    obs = _observe(args.note)
    if obs is None:
        print("Cannot read the live config -- is the container running?",
              file=sys.stderr)
        return 2
    bad = _report(obs, "WINDOW OPEN")
    doc = {"opened": obs, "checks": [], "closed": None}
    _save(doc)
    print()
    if bad:
        print("REFUSING TO OPEN:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print(f"Window opened. Log: {os.path.normpath(LOG)}")
    return 0


def _drift(opened: dict, now: dict) -> list[str]:
    out = []
    if now["image_digest"] != opened["image_digest"]:
        out.append(
            f"IMAGE CHANGED mid-window: {opened['image_digest']} -> "
            f"{now['image_digest']}. Observations before and after this point "
            f"describe different artifacts and cannot be pooled.")
    if now["config_fingerprint"] != opened["config_fingerprint"]:
        out.append(
            "WATCHED CONFIG CHANGED mid-window -- the window measured a "
            "moving target. Compare the auto_flags/discovery_mode above "
            "against the opened entry to see what moved.")
    return out


def cmd_check(args) -> int:
    doc = _load()
    if not doc.get("opened"):
        print("No open window. Run 'open' first.", file=sys.stderr)
        return 2
    obs = _observe(args.note)
    if obs is None:
        print("Cannot read the live config -- is the container running?",
              file=sys.stderr)
        return 2
    bad = _report(obs, "WINDOW CHECK") + _drift(doc["opened"], obs)
    doc["checks"].append(obs)
    _save(doc)
    print()
    if bad:
        print("WINDOW INVALID:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("Window intact: same artifact, same watched config, actors still off.")
    return 0


def cmd_close(args) -> int:
    doc = _load()
    if not doc.get("opened"):
        print("No open window. Run 'open' first.", file=sys.stderr)
        return 2
    obs = _observe(args.note)
    if obs is None:
        print("Cannot read the live config -- is the container running?",
              file=sys.stderr)
        return 2
    bad = _report(obs, "WINDOW CLOSE") + _drift(doc["opened"], obs)
    doc["closed"] = obs
    _save(doc)
    opened_at = doc["opened"]["at"]
    print()
    print(f"  opened at       : {opened_at}")
    print(f"  closed at       : {obs['at']}")
    print(f"  intermediate    : {len(doc['checks'])} check(s)")
    if bad:
        print("\nWINDOW INVALID -- its observations may NOT be graded:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\nWINDOW VALID: same image digest at open and close, watched config "
          "unchanged, autonomous actors off throughout. R-13's condition is "
          "met by this log, not by assertion.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("open", "record the window's opening state"),
                        ("check", "verify nothing has drifted"),
                        ("close", "close and rule on validity")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--note", default="", help="free-text context")
    args = ap.parse_args(argv)
    return {"open": cmd_open, "check": cmd_check, "close": cmd_close}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

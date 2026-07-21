#!/usr/bin/env python3
"""Standard-library self-test proving the 04/05 tooling repairs.

Run: ``python scripts/selftest.py`` (exit 0 = all checks pass).

It requires no network, no Docker, and no ScanHound checkout -- it emulates just
enough of the real API and schema to exercise the two repaired scripts:

  * 04_settings_guard.py -- a stub HTTP server emulates ScanHound's
    ``extra="forbid"`` ``PUT /settings`` (422 on any non-SettingsUpdate key),
    ``GET /settings`` (returns the full config), and ``POST /rss/mode``. The test
    asserts the disabled/shadow stages apply with NO 422 and verify correctly,
    and that the stub would in fact 422 a naive whole-dict PUT (so the emulation
    is meaningful).

  * 05_shadow_evidence.py -- a synthetic SQLite database with the real
    ``hdencode_shadow_cycles`` / ``hdencode_shadow_misses`` / ``hdencode_feed_state``
    columns. The test asserts the collector counts completed cycles, recovery
    cycles, and relevant misses from the AUTHORITATIVE columns (the two bugs the
    repair fixed), including that a single ``relevant_miss`` flips readiness off.
"""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

# The stub accepts exactly the SettingsUpdate-writable keys this tool touches.
ALLOWED_SETTINGS_KEYS = {
    "auto_rename_enabled",
    "auto_grab_enabled",
    "hdencode_enabled",
    "background_scan_enabled",
}
RSS_MODES = {"listing", "rss_shadow", "rss_primary"}


def _make_handler(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode() or "{}")

        def do_GET(self):
            if self.path == "/settings":
                self._send(200, dict(config))
            else:
                self._send(404, {"detail": "not found"})

        def do_PUT(self):
            if self.path != "/settings":
                self._send(404, {"detail": "not found"})
                return
            payload = self._read_json()
            extra = set(payload) - ALLOWED_SETTINGS_KEYS
            if extra:  # emulate extra="forbid"
                self._send(422, {"detail": f"unexpected keys: {sorted(extra)}"})
                return
            config.update(payload)
            self._send(200, {"updated": sorted(payload)})

        def do_POST(self):
            if self.path != "/rss/mode":
                self._send(404, {"detail": "not found"})
                return
            payload = self._read_json()
            mode = payload.get("mode")
            if mode not in RSS_MODES:
                self._send(422, {"detail": "Invalid RSS mode"})
                return
            if mode == "rss_primary":  # readiness gate (never ready in this stub)
                self._send(409, {"detail": "RSS primary requires shadow validation"})
                return
            config["hdencode_discovery_mode"] = mode
            self._send(200, {"mode": mode})

    return Handler


def _run(argv):
    return subprocess.run(
        [PY, str(HERE / argv[0]), *argv[1:]],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def test_settings_guard():
    config = {
        "auto_rename_enabled": True,
        "auto_grab_enabled": True,
        "hdencode_enabled": True,
        "background_scan_enabled": True,
        "hdencode_discovery_mode": "rss_primary",
        "hdencode_rss_shadow_compare_enabled": True,
        "hdencode_rss_auto_grab_enabled": False,
        "hdencode_rss_listing_fallback_enabled": False,
        "plex_token": "secret",
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(config))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # Sanity: the stub really rejects an unknown key (emulation is meaningful).
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/settings", data=b'{"hdencode_discovery_mode":"listing"}',
                headers={"Content-Type": "application/json"}, method="PUT"), timeout=10)
            raise AssertionError("stub PUT accepted a forbidden key")
        except urllib.error.HTTPError as e:
            assert e.code == 422, f"expected 422, got {e.code}"

        with tempfile.TemporaryDirectory() as ev:
            # disabled stage
            p = _run(["04_settings_guard.py", "--base-url", base,
                      "--stage", "disabled", "--evidence-dir", ev, "--execute"])
            assert p.returncode == 0, f"disabled stage failed rc={p.returncode}\n{p.stdout}\n{p.stderr}"
            out = json.loads(p.stdout)
            assert out["ok"] is True, out
            assert not out["mismatches"] and not out["unsettable_mismatches"], out
            assert config["hdencode_enabled"] is False
            assert config["auto_rename_enabled"] is False
            assert config["auto_grab_enabled"] is False
            assert config["background_scan_enabled"] is False
            assert config["hdencode_discovery_mode"] == "listing"

            # shadow stage
            p = _run(["04_settings_guard.py", "--base-url", base,
                      "--stage", "shadow", "--evidence-dir", ev, "--execute"])
            assert p.returncode == 0, f"shadow stage failed rc={p.returncode}\n{p.stdout}\n{p.stderr}"
            out = json.loads(p.stdout)
            assert out["ok"] is True, out
            assert config["hdencode_enabled"] is True
            assert config["hdencode_discovery_mode"] == "rss_shadow"
            assert config["auto_rename_enabled"] is False
            assert config["auto_grab_enabled"] is False

            # dry-run must not require --execute and must not touch config
            snapshot_mode = config["hdencode_discovery_mode"]
            p = _run(["04_settings_guard.py", "--base-url", base,
                      "--stage", "disabled", "--evidence-dir", ev])
            assert p.returncode == 0, p.stderr
            dry = json.loads(p.stdout)
            assert dry["dry_run"] is True
            assert "hdencode_discovery_mode" not in dry["settings_put"]
            assert dry["rss_mode"] == "listing"
            assert config["hdencode_discovery_mode"] == snapshot_mode  # unchanged
    finally:
        server.shutdown()
    print("PASS  04_settings_guard: disabled/shadow apply with no 422; RSS mode via /rss/mode")


def _make_db(path, *, cycles, misses, recovery, feeds_ok, span_days):
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 6")
    con.execute("""CREATE TABLE hdencode_shadow_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_uuid TEXT NOT NULL UNIQUE,
        started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
        normal_feeds_complete INTEGER NOT NULL, rss_requests INTEGER NOT NULL,
        listing_requests INTEGER NOT NULL, rss_count INTEGER NOT NULL,
        listing_count INTEGER NOT NULL, duplicate_count INTEGER NOT NULL,
        feed_only_count INTEGER NOT NULL, listing_only_count INTEGER NOT NULL,
        relevant_miss_count INTEGER NOT NULL, request_reduction_pct REAL NOT NULL,
        catchup_used INTEGER NOT NULL DEFAULT 0, restart_recovery INTEGER NOT NULL DEFAULT 0,
        outcome TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}')""")
    con.execute("""CREATE TABLE hdencode_shadow_misses (
        cycle_uuid TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT, status TEXT,
        PRIMARY KEY (cycle_uuid, canonical_url))""")
    con.execute("""CREATE TABLE hdencode_feed_state (
        feed_key TEXT PRIMARY KEY, last_status INTEGER, consecutive_failures INTEGER,
        last_checked_at TEXT)""")
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=span_days)
    step = dt.timedelta(days=span_days / max(1, cycles))
    for i in range(cycles):
        ts = (base + step * i).isoformat()
        is_miss = i < misses
        con.execute(
            "INSERT INTO hdencode_shadow_cycles (cycle_uuid,started_at,completed_at,"
            "normal_feeds_complete,rss_requests,listing_requests,rss_count,listing_count,"
            "duplicate_count,feed_only_count,listing_only_count,relevant_miss_count,"
            "request_reduction_pct,catchup_used,restart_recovery,outcome) "
            "VALUES (?,?,?,1,4,10,4,4,0,0,0,?,60.0,?,0,?)",
            (f"cy-{i}", ts, ts, 1 if is_miss else 0,
             1 if i < recovery else 0, "relevant_miss" if is_miss else "success"),
        )
        if is_miss:
            con.execute("INSERT INTO hdencode_shadow_misses VALUES (?,?,?,?)",
                        (f"cy-{i}", f"http://x/{i}", "T", "missing"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    for key in ("movies_all", "tv_all"):
        con.execute("INSERT INTO hdencode_feed_state VALUES (?,?,?,?)",
                    (key, 200 if feeds_ok else 500, 0 if feeds_ok else 3,
                     now if feeds_ok else stale))
    con.commit()
    con.close()


def test_shadow_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        ev = Path(tmp) / "ev"
        # Healthy: 20 complete cycles, 3 with recovery, zero misses, 8-day span.
        db = Path(tmp) / "ready.sqlite3"
        _make_db(db, cycles=20, misses=0, recovery=3, feeds_ok=True, span_days=8)
        p = _run(["05_shadow_evidence.py", "--db", str(db), "--evidence-dir", str(ev)])
        assert p.returncode == 0, p.stderr
        s = json.loads(p.stdout)
        r = s["readiness"]
        assert r["successful_cycles"] == 20, r
        assert r["recovery_cycles"] == 3, r            # bug #1: was always 0
        assert r["relevant_misses"] == 0, r
        assert r["request_reduction_pct"] > 0, r
        assert r["observed_days"] >= 7, r
        assert r["normal_feeds_healthy"] is True, r
        assert r["ready"] is True, r

        # A single relevant miss (a mandatory stop condition) flips readiness off.
        db2 = Path(tmp) / "miss.sqlite3"
        _make_db(db2, cycles=20, misses=1, recovery=3, feeds_ok=True, span_days=8)
        p = _run(["05_shadow_evidence.py", "--db", str(db2), "--evidence-dir", str(ev)])
        assert p.returncode == 0, p.stderr
        r = json.loads(p.stdout)["readiness"]
        assert r["relevant_misses"] == 1, r            # bug #2: miss must be seen
        assert r["shadow_miss_rows"] == 1, r
        assert r["ready"] is False and "relevant_misses_detected" in r["reasons"], r

        # Unhealthy feeds also block readiness.
        db3 = Path(tmp) / "feeds.sqlite3"
        _make_db(db3, cycles=20, misses=0, recovery=3, feeds_ok=False, span_days=8)
        p = _run(["05_shadow_evidence.py", "--db", str(db3), "--evidence-dir", str(ev)])
        r = json.loads(p.stdout)["readiness"]
        assert r["normal_feeds_healthy"] is False, r
        assert "normal_feeds_unhealthy_or_stale" in r["reasons"], r
    print("PASS  05_shadow_evidence: cycles/recovery/misses/feeds read from real columns")


def main():
    test_settings_guard()
    test_shadow_evidence()
    print("ALL SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

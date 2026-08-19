"""Watch for an ACTIVE HDEncode reveal stall and capture Turnstile evidence.

WHY THIS EXISTS. The verification hold (2026-08-09) keys detection on a
CONJUNCTION — reveal not-ready AND active Turnstile evidence. The DOM legs of
that detection are validated against the healthy page, but the console-600 leg
has never been captured against a REAL active stall, because the stall is
intermittent and was not occurring when the one-shot diagnostic ran. This
watcher closes that gap without anyone babysitting it.

WHAT IT DOES, and does NOT. Every POLL minutes it loads the target release
page(s) in a throwaway Chromium — ScanHound's own binary/driver/flags, a fresh
temp profile, Xvfb — waits, scrolls the reveal form into view, and reads the
reveal control's label and any Turnstile evidence. It NEVER clicks the control,
NEVER attempts to pass/solve/evade the challenge, and NEVER touches the queue,
the live browser profile, or the database (read-only DB access only, to pick
which URLs to probe). It is the same read-only observation the app already does
when it loads a reveal page.

OUTCOME. Healthy observations are logged compactly so the base rate and
liveness are visible. The FIRST time a not-ready reveal coincides with active
Turnstile evidence, it writes a full CAUGHT record and EXITS 0 — the evidence is
captured, the job is done. A hard deadline (default 48h) stops it regardless.

DEPLOY. Version-controlled here; the running copy lives at
/dbvol/turnstile_watch.py so it survives image rebuilds, like the other operator
scripts. Evidence is appended to /dbvol/turnstile-watch-evidence.jsonl (also
persistent). Launch detached inside the container:

    docker cp scripts/turnstile_watch.py scanhound:/dbvol/turnstile_watch.py
    docker exec -d -e DISPLAY=:99 scanhound python3 /dbvol/turnstile_watch.py

NOTE it is NOT restart-durable on its own: a container restart stops it. To make
it survive restarts, run it from a user-level scheduled task on the host
(no elevation needed) that execs the docker command above, or add it as the
image entrypoint's sidecar. See the handoff doc.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/app")

DB = "/dbvol/crawler.db"
OUT = "/dbvol/turnstile-watch-evidence.jsonl"
POLL_MIN = int(os.environ.get("TURNSTILE_WATCH_POLL_MIN", "10"))
DEADLINE_H = float(os.environ.get("TURNSTILE_WATCH_DEADLINE_H", "48"))
SETTLE_S = 8
SCROLL_SETTLE_S = 10
CHROMIUM = "/usr/bin/chromium"
CHROMEDRIVER = "/usr/bin/chromedriver"
PROFILE = "/tmp/turnstile-watch-profile"

# The known-stalled URL plus whatever HDEncode items are parked right now.
SEED_URL = ("https://hdencode.org/"
            "being-erica-s02-1080p-nf-web-dl-dd5-1-x264-ntb-18-3-gb/")


def _now():
    return datetime.now(timezone.utc).isoformat()


def emit(record):
    record["ts"] = _now()
    line = json.dumps(record)
    try:
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def target_urls(limit=2):
    urls = [SEED_URL]
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT canonical_url FROM download_queue_items "
                "WHERE source='hdencode' AND state IN "
                "('verification_required','waiting_source') "
                "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
        for (url,) in rows:
            if url and url not in urls:
                urls.append(url)
    except Exception as exc:
        emit({"event": "db_error", "detail": repr(exc)})
    return urls[: limit + 1]


def _new_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    if os.path.isdir(PROFILE):
        import shutil
        shutil.rmtree(PROFILE, ignore_errors=True)
    opts = Options()
    opts.binary_location = CHROMIUM
    for arg in ("--window-size=1920,1080", "--disable-gpu", "--no-sandbox",
                "--disable-dev-shm-usage", f"--user-data-dir={PROFILE}",
                "--profile-directory=Default"):
        opts.add_argument(arg)
    opts.set_capability("goog:loggingPrefs",
                        {"performance": "ALL", "browser": "ALL"})
    return webdriver.Chrome(service=Service(CHROMEDRIVER), options=opts)


def _reveal_tier(driver):
    """'not-ready' | 'ready' | 'none', read from the reveal control labels.

    Deliberately simple and read-only: not-ready if a control says it is still
    verifying, ready if a links control is present, else none.
    """
    try:
        labels = driver.execute_script(
            "return Array.from(document.querySelectorAll("
            "'form input[type=submit], form button')).map(el=>("
            "(el.value||el.textContent||'').trim().toLowerCase()))") or []
    except Exception:
        labels = []
    not_ready = any(("verify" in l or "please wait" in l) for l in labels)
    ready = any(("view link" in l or "download link" in l) for l in labels)
    if not_ready and not ready:
        return "not-ready", labels
    if ready:
        return "ready", labels
    return "none", labels


def _turnstile_evidence(driver):
    """Active Turnstile evidence at this instant (DOM + console + network)."""
    ev = {}
    try:
        ev["source_has_turnstile"] = "turnstile" in (
            driver.page_source or "").lower()
    except Exception:
        ev["source_has_turnstile"] = None
    try:
        ev["response_field"] = driver.execute_script(
            "var e=document.querySelector('input[name=\"cf-turnstile-response\"]');"
            "return e?{present:true,has_value:!!(e.value&&e.value.length)}"
            ":{present:false};")
        ev["container"] = driver.execute_script(
            "return !!document.querySelector('.cf-turnstile')")
        ev["iframes"] = driver.execute_script(
            "return Array.from(document.querySelectorAll('iframe')).map(f=>("
            "{src:(f.src||'').slice(0,140),sandbox:f.getAttribute('sandbox')}))")
    except Exception as exc:
        ev["dom_error"] = repr(exc)
    # console
    try:
        console = driver.get_log("browser")
    except Exception:
        console = []
    ev["console"] = [{"ts": e.get("timestamp"), "level": e.get("level"),
                      "message": str(e.get("message") or "")[:240]}
                     for e in console]
    ev["turnstile_600_lines"] = [
        c["message"] for c in ev["console"]
        if "challenges.cloudflare.com/turnstile" in c["message"].lower()
        and any(f"600{d}" in c["message"] for d in "0123456789")]
    # network to challenges.cloudflare.com, with failures
    cf = {}
    try:
        for entry in driver.get_log("performance"):
            try:
                msg = json.loads(entry.get("message") or "{}").get("message") or {}
            except Exception:
                continue
            method, params = msg.get("method"), (msg.get("params") or {})
            if method == "Network.requestWillBeSent":
                url = (params.get("request") or {}).get("url") or ""
                if "challenges.cloudflare.com" in url:
                    cf[params.get("requestId")] = {"url": url[:140]}
            elif method == "Network.responseReceived":
                rid = params.get("requestId")
                if rid in cf:
                    cf[rid]["status"] = (params.get("response") or {}).get("status")
            elif method == "Network.loadingFailed":
                rid = params.get("requestId")
                if rid in cf:
                    cf[rid]["failed"] = params.get("errorText")
                    cf[rid]["blocked"] = params.get("blockedReason")
    except Exception:
        pass
    ev["cf_turnstile_network"] = list(cf.values())
    ev["active"] = bool(
        (ev.get("response_field") or {}).get("present")
        or ev.get("container")
        or any("challenges.cloudflare.com" in (f.get("src") or "")
               for f in (ev.get("iframes") or []))
        or ev["turnstile_600_lines"])
    return ev


def probe(url):
    driver = None
    try:
        driver = _new_driver()
        t0 = time.monotonic()
        driver.get(url)
        load_s = round(time.monotonic() - t0, 2)
        time.sleep(SETTLE_S)
        driver.execute_script(
            "var f=document.querySelector('form');"
            "if(f){f.scrollIntoView({block:'center'});}"
            "else{window.scrollTo(0,document.body.scrollHeight);}")
        time.sleep(SCROLL_SETTLE_S)
        tier, labels = _reveal_tier(driver)
        ev = _turnstile_evidence(driver)
        try:
            title = driver.title
        except Exception:
            title = None
        return {"url": url, "page_load_s": load_s, "reveal_tier": tier,
                "labels": labels[:8], "title": title, "evidence": ev}
    except Exception as exc:
        return {"url": url, "probe_error": repr(exc)}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def main():
    emit({"event": "watch_start", "poll_min": POLL_MIN,
          "deadline_h": DEADLINE_H})
    deadline = time.monotonic() + DEADLINE_H * 3600
    cycle = 0
    while time.monotonic() < deadline:
        cycle += 1
        for url in target_urls():
            result = probe(url)
            ev = result.get("evidence") or {}
            stalled = result.get("reveal_tier") == "not-ready"
            active = bool(ev.get("active"))
            if stalled and active:
                emit({"event": "CAUGHT_ACTIVE_STALL", "cycle": cycle, **result})
                emit({"event": "watch_exit", "reason": "captured"})
                return 0
            emit({"event": ("stall_no_active_evidence" if stalled
                            else "observation"),
                  "cycle": cycle, "url": url,
                  "reveal_tier": result.get("reveal_tier"),
                  "page_load_s": result.get("page_load_s"),
                  "source_has_turnstile": ev.get("source_has_turnstile"),
                  "active": active,
                  "turnstile_600_count": len(ev.get("turnstile_600_lines") or []),
                  "probe_error": result.get("probe_error")})
        time.sleep(POLL_MIN * 60)
    emit({"event": "watch_exit", "reason": "deadline"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

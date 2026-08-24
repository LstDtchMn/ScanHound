"""The N=25 discriminating test for the HDEncode Turnstile gate.

WHY THIS EXISTS
---------------
The reveal gate is INTERMITTENT: historically 22 successes at 0.1-0.9s versus
8 stalls at 60.0-60.6s, with nothing in between. It resolves instantly or never.

That intermittency is what makes single probes worthless. On 2026-08-22 two
hypotheses (cold browser session, VPN exit-IP reputation) were each "tested"
with ONE probe and each looked refuted. One failure against a gate that fails
intermittently is not evidence -- it is the expected outcome some fraction of
the time no matter which hypothesis is true.

This script produces the sample size that can actually discriminate.

WHAT IT DOES NOT DO
-------------------
  * does NOT grab anything, queue anything, or send anything to JDownloader
  * does NOT write to the live database
  * does NOT touch the running `scanhound` container or its browser

It runs in its OWN throwaway container with its OWN browser profile, so it
cannot take the live scanner's `_driver_lock` or disturb the queue. The cost is
that it also cannot inherit the live profile's accumulated state -- which is
itself a variable worth controlling, hence --profile.

USAGE (from the repo root, on the host)

    python scripts/turnstile-discriminating-test.py --plan

        Print what it WOULD do and exit. Changes nothing. Start here.

    python scripts/turnstile-discriminating-test.py --run --n 25

        Actually run it. Takes up to N x 65s in the worst case (~27 min for
        N=25) because a stall is only known after the 60s ceiling expires.

    python scripts/turnstile-discriminating-test.py --run --n 25 --profile fresh

        --profile ephemeral : a brand-new profile per attempt (coldest)
        --profile fresh     : one new profile for the whole run (default)
        --profile copy      : a COPY of the live profile (never the original)

INTERPRETING THE RESULT
-----------------------
Report the ratio, never a single outcome. Compare runs, changing exactly ONE
variable between them:

    VPN up vs VPN down          -> tests exit-IP reputation
    --profile ephemeral vs fresh -> tests the per-session warm-up theory

A hypothesis is supported only if the RATIO moves materially. If 25 attempts
stall in both arms, the variable you changed is not the trigger.

BEWARE THE VACUOUS RUN: if every attempt in BOTH arms fails, the run may be
measuring something else entirely (a pulled release, a source-wide pause, a
broken container) rather than the gate. The script therefore refuses to report
a comparison unless at least one attempt in the whole run reached a
non-`not-ready` tier -- see `--require-control`.
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "scanhound:latest"
CONTAINER = "sh-turnstile-test"

#: Deliberately several DIFFERENT releases rather than one hammered URL. One
#: URL repeated cannot distinguish "this page is broken" from "the gate is
#: closed", and a pulled release (404) would silently make every attempt fail.
DEFAULT_URLS = [
    "https://hdencode.org/midsomer-murders-s21-1080p-amzn-web-dl-ddp2-0-h-264-playweb-19-5-gb/",
    "https://hdencode.org/mars-express-2023-repack-2160p-uhd-bluray-dual-audio-truehd-5-1-dv-hdr-x265-j3rico-16-8-gb/",
    "https://hdencode.org/random-harvest-1942-1080p-bluray-remux-avc-dts-hd-ma-1-0-bluranium-30-9-gb/",
]

PROBE = r'''
import json, sys, time
sys.path.insert(0, "/app")
from unittest.mock import MagicMock
from backend.download_service import DownloadService

urls = json.loads(sys.argv[1])
n = int(sys.argv[2])
profile_mode = sys.argv[3]

svc = DownloadService.__new__(DownloadService)
svc.config = {
    "jd_enabled": False,                       # nothing can be delivered
    "hdencode_browser_profile_mode": profile_mode,
}
svc.server_mode = True
svc._log = lambda *a, **k: None
svc._progress = MagicMock()
svc.db = None

results = []
for i in range(n):
    url = urls[i % len(urls)]
    t0 = time.monotonic()
    rec = {"i": i + 1, "url": url.rsplit("/", 2)[-2][:44]}
    try:
        out = svc.scrape_links(url, "Rapidgator")
        rec["links"] = len(getattr(out, "links", []) or [])
        rec["tier"] = getattr(out, "reveal_tier", None) or getattr(out, "tier", None)
        rec["signals"] = list(getattr(out, "signals", []) or [])
    except Exception as exc:
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
    rec["elapsed"] = round(time.monotonic() - t0, 1)
    results.append(rec)
    print("PROBE " + json.dumps(rec), flush=True)

print("RESULTS " + json.dumps(results), flush=True)
'''


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True,
                          env=dict(os.environ, MSYS_NO_PATHCONV="1"), **kw)


def plan(args):
    print("PLAN — nothing will be changed\n")
    print("  container   %s (throwaway, created then removed)" % CONTAINER)
    print("  image       %s" % IMAGE)
    print("  attempts    %d" % args.n)
    print("  profile     %s" % args.profile)
    print("  urls        %d distinct release pages, cycled:" % len(DEFAULT_URLS))
    for u in DEFAULT_URLS:
        print("                %s" % u)
    print("\n  worst case  ~%d min (a stall is only known after the 60s ceiling)"
          % ((args.n * 65 + 59) // 60))
    print("\n  WILL NOT: grab, queue, deliver to JDownloader, write the live DB,")
    print("            or touch the running scanhound container or its browser.")
    print("\n  Run it with --run when you are ready.")
    return 0


def run(args):
    print("Creating throwaway container %s ..." % CONTAINER)
    sh("docker", "rm", "-f", CONTAINER)
    c = sh("docker", "create", "--name", CONTAINER, IMAGE, "sleep", "infinity")
    if c.returncode != 0:
        print("  could not create container: %s" % c.stderr.strip())
        return 1
    sh("docker", "start", CONTAINER)
    try:
        probe_path = os.path.join(REPO, "scripts", ".turnstile_probe.py")
        with open(probe_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(PROBE)
        sh("docker", "cp", probe_path, "%s:/tmp/probe.py" % CONTAINER)
        os.remove(probe_path)

        print("Running %d attempts (this is slow by design) ...\n" % args.n)
        proc = subprocess.Popen(
            ["docker", "exec", "-w", "/app", CONTAINER, "python", "/tmp/probe.py",
             json.dumps(DEFAULT_URLS), str(args.n), args.profile],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=dict(os.environ, MSYS_NO_PATHCONV="1"))
        results = []
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("PROBE "):
                rec = json.loads(line[6:])
                results.append(rec)
                print("  %3d/%d  %-46s %5.1fs  tier=%-11s links=%s%s" % (
                    rec["i"], args.n, rec["url"], rec["elapsed"],
                    rec.get("tier"), rec.get("links", "-"),
                    "  ERROR " + rec["error"] if rec.get("error") else ""))
            elif line.startswith("RESULTS "):
                results = json.loads(line[8:])
        proc.wait()
        return report(results, args)
    finally:
        sh("docker", "rm", "-f", CONTAINER)
        print("\nThrowaway container removed.")


def report(results, args):
    if not results:
        print("\nNO RESULTS. The run produced nothing — treat this as a broken")
        print("measurement, not as evidence about the gate.")
        return 1

    ok = [r for r in results if (r.get("links") or 0) > 0]
    stalled = [r for r in results if r.get("tier") == "not-ready"]
    other = [r for r in results if r not in ok and r not in stalled]
    tiers = {}
    for r in results:
        tiers[r.get("tier")] = tiers.get(r.get("tier"), 0) + 1

    print("\n" + "=" * 62)
    print("  RESULT -- report the RATIO, never a single outcome")
    print("=" * 62)
    print("  attempts            %d" % len(results))
    print("  revealed links      %d  (%.0f%%)" % (len(ok), 100.0 * len(ok) / len(results)))
    print("  stalled not-ready   %d  (%.0f%%)" % (len(stalled), 100.0 * len(stalled) / len(results)))
    print("  other/error         %d" % len(other))
    print("  tiers seen          %s" % tiers)
    if ok:
        fast = [r["elapsed"] for r in ok]
        print("  reveal times        min %.1fs  max %.1fs" % (min(fast), max(fast)))

    if args.require_control and not ok and not other:
        print("\n  ** VACUOUS RUN **")
        print("  Every attempt stalled, so this run cannot discriminate anything.")
        print("  It is equally consistent with a source-wide pause, a broken")
        print("  container, or the gate being closed the whole window. Do NOT")
        print("  cite it as evidence for or against any hypothesis. Re-run later,")
        print("  or pass --no-require-control if you accept that limitation.")
        return 2

    print("\n  To discriminate, run this AGAIN changing exactly ONE variable")
    print("  (VPN up/down, or --profile ephemeral/fresh) and compare the RATIO.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="show what would happen; change nothing")
    g.add_argument("--run", action="store_true", help="actually run the test")
    ap.add_argument("--n", type=int, default=25, help="attempts (default 25)")
    ap.add_argument("--profile", choices=["ephemeral", "fresh", "copy"],
                    default="fresh")
    ap.add_argument("--require-control", dest="require_control",
                    action="store_true", default=True)
    ap.add_argument("--no-require-control", dest="require_control",
                    action="store_false")
    args = ap.parse_args()
    return plan(args) if args.plan else run(args)


if __name__ == "__main__":
    sys.exit(main())

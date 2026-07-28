"""Direct verification of the three blockers, without pytest.

pytest could not be installed this round (the sandbox blocks the PyPI path), so
rather than claim the suite passes, this exercises the actual behaviour changes
with plain assertions.
"""
import asyncio, threading, sys
from unittest.mock import MagicMock

from backend.scanner_service import (
    ScannerService, is_full_disc_title, canonicalize_listing_url,
)

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s  %s" % (label, detail))
        FAILURES.append(label)


def page(entries):
    rows = "".join('<div class="data"><h5><a href="%s">%s</a></h5></div>' % e
                   for e in entries)
    return ("<html><body>%s</body></html>" % rows).encode()


class Resp:
    def __init__(self, body):
        self.status_code = 200
        self.content = body


class Scraper:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get(self, *a, **k):
        b = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return Resp(b)


def shell():
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._last_crawl_seen_urls = set()
    s._last_crawl_early_stopped = False
    s._last_crawl_request_count = 0
    s._last_crawl_policy_excluded_new = []
    s._last_crawl_policy_excluded_observed = []
    s._last_crawl_policy_excluded_count = 0
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = {}
    s.db = None
    return s


SRC = {"name": "4K Movies", "base": "https://hdencode.org/quality/2160p/",
       "suffix": "?tag=movies", "type": "movie", "source": "hdencode",
       "category": "4k"}


def crawl(sc, scr, pages=1, prev=None, early=False, excl=None, skip=True, src=None):
    orig = asyncio.sleep

    async def run():
        loop = asyncio.get_running_loop()
        return await sc._crawl_pages([src or SRC], pages=pages,
                                     base_url="https://hdencode.org",
                                     scraper=scr, loop=loop,
                                     previously_scanned=prev or set(),
                                     early_stop=early, policy_excluded=excl,
                                     skip_full_disc=skip)
    import backend.scanner_service as m

    async def nosleep(_s):
        return None
    m.asyncio.sleep = nosleep
    try:
        return asyncio.run(run())
    finally:
        m.asyncio.sleep = orig


print("=== detection (false-positive guards) ===")
for t, e in [("[BD]Sorority.House", True), ("[bd] x", True), ("[ BD ] x", True),
             ("BD Movie Title", False), ("Some BDRip Movie", False),
             ("Bdelloid Rotifers 2024", False), ("Movie [BD] mid", False)]:
    check("%-26r -> %s" % (t, e), is_full_disc_title(t) is e)

print()
print("=== BLOCKER 1: current-crawl duplicates are not the frontier ===")
a, b = "https://hdencode.org/a/", "https://hdencode.org/b/"
sc, scr = shell(), Scraper([page([(a, "A 2024")]), page([(a, "A 2024")]),
                            page([(b, "B 2024")])])
posts = crawl(sc, scr, pages=3, prev=set(), early=True,
              excl={canonicalize_listing_url("https://hdencode.org/bdold/")})
check("page 3 was reached", scr.calls == 3, "calls=%d" % scr.calls)
check("genuinely new release survived", b in [p["url"] for p in posts])
check("did not early-stop", sc._last_crawl_early_stopped is False)

cached = "https://hdencode.org/cached/"
sc2, scr2 = shell(), Scraper([page([(cached, "Cached")]), page([(b, "B")])])
crawl(sc2, scr2, pages=2, prev={cached}, early=True)
check("a fully-known page STILL stops", sc2._last_crawl_early_stopped is True)
check("  and did not fetch page 2", scr2.calls == 1, "calls=%d" % scr2.calls)

print()
print("=== BLOCKER 2: INFO must not promise ingestion ===")
sc3 = shell()
crawl(sc3, Scraper([page([("https://hdencode.org/bdi/", "[BD]I")])]))
lines = [c.args[0] for c in sc3._log.call_args_list if "full-disc" in str(c.args[0])]
check("exactly one aggregate line", len(lines) == 1, "got %d" % len(lines))
if lines:
    check("no 'ingest them'", "ingest them" not in lines[0], lines[0])
    check("no '=false' instruction", "=false" not in lines[0], lines[0])
    check("says unsupported", "unsupported" in lines[0], lines[0])

sc4 = shell()
crawl(sc4, Scraper([page([("https://ddlbase.com/p/x/", "Something")])]),
      skip=False, src=dict(SRC, source="ddlbase"))
check("no HDEncode warning on a ddlbase-only crawl",
      not [c for c in sc4._log.call_args_list if "full-disc" in str(c.args[0])])

print()
print("=== BLOCKER 3: canonical identity ===")
for x, y in [("https://hdencode.org/z/", "https://hdencode.org/z"),
             ("https://hdencode.org/z/", "https://hdencode.org/z/?u=1"),
             ("https://hdencode.org/z/", "https://hdencode.org/z/#f"),
             ("https://hdencode.org/z/", "https://HDEncode.org/z/")]:
    check("variants collapse: %s" % y,
          canonicalize_listing_url(x) == canonicalize_listing_url(y))
check("distinct posts stay distinct",
      canonicalize_listing_url("https://hdencode.org/a")
      != canonicalize_listing_url("https://hdencode.org/b"))

sc5 = shell()
crawl(sc5, Scraper([page([("https://hdencode.org/bdv/", "[BD]V"),
                          ("https://hdencode.org/bdv/?utm=x", "[BD]V"),
                          ("https://hdencode.org/bdv#f", "[BD]V")])]))
check("same-crawl variants count once",
      sc5._last_crawl_policy_excluded_count == 1,
      "count=%d" % sc5._last_crawl_policy_excluded_count)
check("  and persist once",
      len(sc5._last_crawl_policy_excluded_observed) == 1)

print()
print("=== storage boundary canonicalises (real sqlite) ===")
import tempfile, os
from backend.database import DatabaseManager
tmp = tempfile.mkdtemp()
os.environ["SCANHOUND_DB_PATH"] = os.path.join(tmp, "t.db")
try:
    db = DatabaseManager(os.path.join(tmp, "t.db"))
except TypeError:
    db = DatabaseManager()
variants = ["https://hdencode.org/q/", "https://hdencode.org/q",
            "https://hdencode.org/q/?u=1", "https://HDEncode.org/q/"]
written = db.record_policy_exclusions(
    [{"url": v, "title": "[BD]Q", "source": "hdencode"} for v in variants])
check("write count reflects unique rows", written == 1, "written=%s" % written)
got = db.get_policy_excluded_urls("hdencode")
check("one canonical row stored", got == {"https://hdencode.org/q"}, str(got))
check("empty urls do not inflate count",
      db.record_policy_exclusions([{"url": "", "source": "hdencode"}]) == 0)

print()
print("=" * 60)
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("   -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")

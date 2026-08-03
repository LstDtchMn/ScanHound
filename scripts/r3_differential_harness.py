"""Hermetic R-3 differential harness (round-10 Q8 evidence rule).

Compares DetailScraper behaviour between two EXACT commits using disposable
git worktrees and subprocess isolation -- no scratch copies, no reliance on
the invoking checkout's importable state. The corpus is embedded; every
difference must be listed in the committed expected-divergences file or the
run exits nonzero.

Usage:
    python scripts/r3_differential_harness.py                # OLD baseline vs HEAD
    python scripts/r3_differential_harness.py --new <sha>
    python scripts/r3_differential_harness.py --write-expected   # conscious re-baseline
Internal (invoked in a worktree by the harness itself):
    python <this file> --run-self <repo_root> <out.json>

OLD default = c1715297621128baa1253f857d929d88b854d0b7, the parent of the
first R-3 delegation commit (f172d1f) = the last pre-unification
DetailScraper on this branch.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import types

OLD_DEFAULT = "c1715297621128baa1253f857d929d88b854d0b7"
EXPECTED_PATH = os.path.join("docs", "reviews", "evidence",
                             "r3-expected-divergences.json")



def _install_stubs():
    # ─── stub the backend package graph both modules import ──────────────────────
    backend_pkg = types.ModuleType("backend")
    backend_pkg.__path__ = []

    models = types.ModuleType("backend.models")
    class ScrapeResult(dict):
        pass
    models.ScrapeResult = ScrapeResult

    coord = types.ModuleType("backend.hdencode_coordinator")
    class HDEncodeRequestCancelled(Exception): pass
    class HDEncodeTrafficDenied(Exception): pass
    class _Coordinator:
        def request(self, *a, **k):
            import contextlib
            return contextlib.nullcontext()
        def observe_http_status(self, *a, **k): pass
        def observe_network_failure(self, *a, **k): pass
    _the_coord = _Coordinator()
    def get_hdencode_coordinator(): return _the_coord
    def configure_hdencode_coordinator(*a, **k): pass
    coord.HDEncodeRequestCancelled = HDEncodeRequestCancelled
    coord.HDEncodeTrafficDenied = HDEncodeTrafficDenied
    coord.get_hdencode_coordinator = get_hdencode_coordinator
    coord.configure_hdencode_coordinator = configure_hdencode_coordinator

    transport = types.ModuleType("backend.hdencode_transport")
    def create_source_http_client(**k):
        raise RuntimeError("no network in harness")
    transport.create_source_http_client = create_source_http_client

    rename_pkg = types.ModuleType("backend.rename")
    rename_pkg.__path__ = []
    llm = types.ModuleType("backend.rename.llm_identify")
    def extract_page_hints(_text):
        return None
    llm.extract_page_hints = extract_page_hints
    rename_pkg.llm_identify = llm

    sys.modules.update({
        "backend": backend_pkg,
        "backend.models": models,
        "backend.hdencode_coordinator": coord,
        "backend.hdencode_transport": transport,
        "backend.rename": rename_pkg,
        "backend.rename.llm_identify": llm,
    })
    return backend_pkg


def _load_from(repo_root):
    """Load release_grammar + detail_scraper from ONE exact tree by path."""
    import importlib.util
    backend_pkg = _install_stubs()
    gpath = os.path.join(repo_root, "backend", "release_grammar.py")
    spec = importlib.util.spec_from_file_location("backend.release_grammar", gpath)
    grammar = importlib.util.module_from_spec(spec)
    sys.modules["backend.release_grammar"] = grammar
    backend_pkg.release_grammar = grammar
    spec.loader.exec_module(grammar)
    dpath = os.path.join(repo_root, "backend", "detail_scraper.py")
    spec = importlib.util.spec_from_file_location("r3_target", dpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ─── fake app: REAL parse_size transcribed from backend/api/dependencies.py ──
class FakeApp:
    def __init__(self):
        self.config = {}
    def safe_log(self, msg, level="info"): pass
    def clean_string(self, s):
        import re
        if not s: return ""
        n = s.lower().strip()
        n = re.sub(r"[^a-z0-9\s]", "", n)
        return re.sub(r"\s+", " ", n).strip()
    @staticmethod
    def parse_size(size_str):
        """VERBATIM logic of AppService.parse_size (backend/api/dependencies.py:76)."""
        if not size_str:
            return 0.0
        size_str = size_str.strip().upper()
        try:
            if "GB" in size_str:
                return float(size_str.replace("GB", "").strip())
            elif "MB" in size_str:
                return float(size_str.replace("MB", "").strip()) / 1024
            elif "TB" in size_str:
                return float(size_str.replace("TB", "").strip()) * 1024
            return float(size_str)
        except (ValueError, TypeError):
            return 0.0

class FakeResp:
    def __init__(self, html):
        self.status_code = 200
        self.content = html.encode("utf-8")
        self.text = html

class FakeScraper:
    def __init__(self, html): self._h = html
    def get(self, url, headers=None, timeout=None): return FakeResp(self._h)

def build_html(filename, size_label="FileSize: 15.5 GB",
               resolution="Resolution: 1920x1080", extra_filenames=None,
               extra_text="", rating="7.5"):
    lines = [f"Filename.....: {filename}"]
    for fn in (extra_filenames or []):
        lines.append(f"Filename.....: {fn}")
    lines.append(f"Rating : {rating}")
    if size_label is not None:
        lines.append(size_label)
    if resolution is not None:
        lines.append(resolution)
    if extra_text:
        lines.append(extra_text)
    block = "\n".join(lines)
    return ('<html><body><div class="entry-content"><pre>' + block +
            '\n</pre>\n<a href="https://www.imdb.com/title/tt1234567/">IMDb</a>\n'
            '</div></body></html>')

def build_html_glued(filename, resolution):
    """Adjacent tags with no whitespace: get_text() glues text nodes, which is
    how a real page can yield 'Resolution: 3840x2160Aspect...'."""
    return ('<html><body><div class="entry-content">'
            f'<div>Filename.....: {filename}</div>'
            f'<div>{resolution}</div><div>Aspect ratio: 16:9</div>'
            '<a href="https://www.imdb.com/title/tt1234567/">IMDb</a>'
            '</div></body></html>')

FIELDS = ["is_tv", "season", "episode_number", "episodes",
          "display_title", "year", "size", "res"]

def run(mod, html):
    ds = mod.DetailScraper(FakeApp())
    r = ds.scrape_details("https://example.com/detail", headers={},
                          scraper=FakeScraper(html))
    if r is None:
        return None
    return {f: r.get(f) for f in FIELDS}

CASES = [
    # ── S01E01 forms ──
    ("baseline SxxExx", dict(filename="Show.Name.S01E01.1080p.WEB.mkv")),
    ("space separators", dict(filename="Show Name S01E01 1080p WEB mkv")),
    ("hyphen separators", dict(filename="Show-Name-S01E01-1080p.mkv")),
    ("UNDERSCORE before S01E01", dict(filename="Show_Name_S01E01.1080p.mkv")),
    ("S01E01 at string start", dict(filename="S01E01.Show.Name.1080p.mkv")),
    ("S01E01 after open paren", dict(filename="Show Name (S01E01).mkv")),
    ("S01E01 glued to codec", dict(filename="Show.S01E01x265.mkv")),
    ("S01E01E02 double episode", dict(filename="Show.S01E01E02.1080p.mkv")),
    ("S01E01-E03 span", dict(filename="Show.S01E01-E03.1080p.mkv")),
    ("season only S01", dict(filename="Show.Name.S01.COMPLETE.1080p.mkv")),
    ("lowercase s01e01", dict(filename="show.name.s01e01.1080p.mkv")),
    # ── S104 / wide seasons ──
    ("S104 ambiguous", dict(filename="Show.Name.S104.2160p.mkv")),
    ("S104E01", dict(filename="Show.Name.S104E01.1080p.mkv")),
    ("S2015 four digits", dict(filename="Show.Name.S2015.1080p.mkv")),
    ("S0MEGRP group name", dict(filename="Movie.2020.1080p.x264-S0MEGRP.mkv")),
    ("S3XY letter after digit", dict(filename="Tesla.S3XY.Story.2160p.mkv")),
    # ── multi-episode packs / mirrors ──
    ("pack 3 unique eps", dict(filename="Show.S01E01.720p.mkv",
        extra_filenames=["Show.S01E02.720p.mkv", "Show.S01E03.720p.mkv"])),
    ("mirrors same ep", dict(filename="Show.S01E01.720p.mkv",
        extra_filenames=["Show.S01E01.720p.mkv", "Show.S01E01.720p.mkv"])),
    ("pack glued eps x265", dict(filename="Show.S01E01x265.mkv",
        extra_filenames=["Show.S01E02x265.mkv"])),
    ("pack E01E02 line", dict(filename="Show.S01E01E02.1080p.mkv",
        extra_filenames=["Show.S01E03E04.1080p.mkv"])),
    # ── DTS5.1 / guard cases ──
    ("DTS5.1 movie", dict(filename="Movie.2020.1080p.DTS5.1.x264.mkv")),
    ("DTS5.1 no year", dict(filename="Movie.DTS5.1.x264.mkv")),
    # ── year forms ──
    ("plain movie year", dict(filename="Movie.Title.2020.1080p.BluRay.mkv")),
    ("paren year", dict(filename="Movie Title (2020) 1080p.mkv")),
    ("hyphen year", dict(filename="Movie Title - 2020 - 1080p.mkv")),
    ("2001 A Space Odyssey 1968", dict(filename="2001.A.Space.Odyssey.1968.1080p.mkv")),
    ("2012 opening year only", dict(filename="2012.1080p.BluRay.mkv")),
    ("2012 Doomsday 2008", dict(filename="2012.Doomsday.2008.720p.mkv")),
    ("Blade Runner 2049 2017", dict(filename="Blade.Runner.2049.2017.2160p.mkv")),
    ("year glued 5 digits", dict(filename="Movie.20201.Extended.mkv")),
    ("dimension as year 1920x1080", dict(filename="Concert.Film.1920x1080.mkv", resolution=None)),
    ("year then dimension", dict(filename="Concert.Film.2019.1920x1080.mkv", resolution=None)),
    # ── sizes ──
    ("KB labelled size", dict(filename="Movie.2020.1080p.mkv", size_label="FileSize: 500 KB")),
    ("KB plus loose GB", dict(filename="Movie.2020.1080p.mkv",
        size_label="FileSize: 500 KB", extra_text="mirror listed at 2.0 GB")),
    ("TB labelled", dict(filename="Movie.2020.2160p.mkv", size_label="Total Size: 1.2 TB")),
    ("TB loose only", dict(filename="Movie.2020.2160p.mkv", size_label=None,
        extra_text="the pack weighs 1.2 TB total")),
    ("GiB labelled vs smaller GB", dict(filename="Movie.2020.1080p.mkv",
        size_label="FileSize: 15.5 GiB", extra_text="Size: 2.0 GB")),
    ("GiB alone", dict(filename="Movie.2020.1080p.mkv", size_label="FileSize: 15.5 GiB")),
    ("MiB vs GB boundary", dict(filename="Movie.2020.1080p.mkv",
        size_label="Size: 1030 MB", extra_text="Size: 1.01 GB")),
    ("loose only no label", dict(filename="Movie.2020.1080p.mkv", size_label=None,
        extra_text="rip comes in at 8.5 GB even")),
    ("oversized promotion", dict(filename="Movie.2020.1080p.mkv",
        size_label="Size: 2 GB", extra_text="the oversized bonus disc adds 9 GB")),
    ("GBps bandwidth", dict(filename="Movie.2020.1080p.mkv", size_label=None,
        extra_text="server pushes 15 GBps easily")),
    ("no size at all", dict(filename="Movie.2020.1080p.mkv", size_label=None)),
    ("size label far away", dict(filename="Movie.2020.1080p.mkv",
        size_label="Size of the encode after muxing came to 7.7 GB")),
    # ── resolution ──
    ("res line 3840x2160", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 3840x2160")),
    ("res line 1920x1080", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 1920x1080")),
    ("res line 720p token", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 720p")),
    ("res line 2160p token", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 2160p")),
    ("res line 4K token", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 4K")),
    ("res line UHD token", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: UHD")),
    ("res line 720x480", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 720x480")),
    ("res line 1440x1080", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 1440x1080")),
    ("res line dims + fps", dict(filename="Movie.Title.HDR.mkv",
        resolution="Resolution: 1920x1080 @ 23.976 fps")),
    ("res line spaced dims", dict(filename="Movie.Title.HDR.mkv",
        resolution="Resolution: 3840 x 2160")),
    ("GLUED tags res line", dict(_glued=True, filename="Movie.Title.HDR.mkv",
        resolution="Resolution: 3840x2160")),
    ("res line 1080i", dict(filename="Movie.Title.HDR.mkv", resolution="Resolution: 1080i")),
    ("no res line no fn res", dict(filename="Movie.Title.HDR.mkv", resolution=None)),
    ("1080i in filename", dict(filename="Show.S01E01.1080i.HDTV.mkv", resolution=None)),
    ("2160 inside WxH in filename", dict(filename="Movie.2020.3840x2160.Remux.mkv", resolution=None)),
    ("2160 inside odd WxH fn", dict(filename="Movie.2020.2160x1080.Ultrawide.mkv", resolution=None)),
    ("uhd word in filename", dict(filename="Movie.2020.UHD.BluRay.mkv", resolution=None)),
    ("fn 720p vs line 2160p", dict(filename="Movie.2020.720p.mkv", resolution="Resolution: 3840x2160")),
    ("bare 2160 substring beats line", dict(filename="Movie.x2160z.mkv", resolution="Resolution: 1920x1080")),
    ("1080i fn beats 2160p line", dict(filename="Show.S01E01.1080i.mkv", resolution="Resolution: 2160p")),
    ("3840x1600 scope fn only", dict(filename="Movie.2020.3840x1600.Scope.mkv", resolution=None)),
    ("1080 in group name", dict(filename="Movie.2020.WEB.x264-GRP1080.mkv", resolution="Resolution: 3840x2160")),
    # ── unicode / mojibake / empties ──
    ("unicode title", dict(filename="Am\u00e9lie.2001.1080p.mkv")),
    ("smart quote title", dict(filename="Ocean\u2019s.Eleven.2001.1080p.mkv")),
    ("empty filename value", dict(filename="")),
    ("whitespace filename", dict(filename="   ")),
    ("no year no res no size", dict(filename="Some.Random.File.mkv",
        size_label=None, resolution=None)),
]



def _materialise(name, kw):
    kw2 = {k: v for k, v in kw.items() if k != "_glued"}
    return build_html_glued(**kw2) if kw.get("_glued") else build_html(**kw)


def run_self(repo_root, out_path):
    mod = _load_from(repo_root)
    results = {}
    for name, kw in CASES:
        results[name] = run(mod, _materialise(name, kw))
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, sort_keys=True, ensure_ascii=False)


def _one_side(sha, label):
    tmp = tempfile.mkdtemp(prefix=f"r3h-{label}-")
    wt = os.path.join(tmp, "wt")
    subprocess.run(["git", "worktree", "add", "--detach", wt, sha],
                   check=True, capture_output=True)
    try:
        out = os.path.join(tmp, "out.json")
        proc = subprocess.run([sys.executable, os.path.abspath(__file__),
                               "--run-self", wt, out],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{label} runner failed: {proc.stderr[-2000:]}")
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt],
                       capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=OLD_DEFAULT)
    ap.add_argument("--new", default="HEAD")
    ap.add_argument("--write-expected", action="store_true")
    ap.add_argument("--run-self", nargs=2, metavar=("REPO_ROOT", "OUT"))
    args = ap.parse_args()
    if args.run_self:
        run_self(*args.run_self)
        return 0

    old_sha = subprocess.run(["git", "rev-parse", args.old], check=True,
                             capture_output=True, text=True).stdout.strip()
    new_sha = subprocess.run(["git", "rev-parse", args.new], check=True,
                             capture_output=True, text=True).stdout.strip()
    old_res = _one_side(old_sha, "old")
    new_res = _one_side(new_sha, "new")

    diffs = {}
    for name, _kw in CASES:
        o, n = old_res.get(name), new_res.get(name)
        if o is None or n is None:
            if o != n:
                diffs[name] = {"_none": [o is None, n is None]}
            continue
        d = {f: [o[f], n[f]] for f in FIELDS if o[f] != n[f]}
        if d:
            diffs[name] = d

    print(f"old={old_sha[:9]} new={new_sha[:9]} cases={len(CASES)} "
          f"identical={len(CASES)-len(diffs)} differing={len(diffs)}")

    if args.write_expected:
        with open(EXPECTED_PATH, "w", encoding="utf-8") as fh:
            json.dump({"old": old_sha, "new_at_baseline": new_sha,
                       "divergences": diffs}, fh, indent=1, sort_keys=True,
                      ensure_ascii=False)
            fh.write(chr(10))
        print(f"expected file written: {EXPECTED_PATH}")
        return 0

    with open(EXPECTED_PATH, encoding="utf-8") as fh:
        expected = json.load(fh)["divergences"]
    unexplained = {k: v for k, v in diffs.items() if expected.get(k) != v}
    vanished = {k: v for k, v in expected.items() if k not in diffs}
    for title, bucket in (("UNEXPLAINED (not in expected file)", unexplained),
                          ("EXPECTED BUT ABSENT", vanished)):
        if bucket:
            print(chr(10) + f"== {title} ==")
            for name, d in sorted(bucket.items()):
                print(f"  {name}: {json.dumps(d, ensure_ascii=False)}")
    if unexplained or vanished:
        return 1
    print("every divergence matches the committed expected file")
    return 0


if __name__ == "__main__":
    sys.exit(main())

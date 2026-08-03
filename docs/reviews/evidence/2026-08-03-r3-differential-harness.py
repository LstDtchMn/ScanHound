"""R-3 adversarial differential harness: OLD (f172d1f~1) vs NEW (HEAD) DetailScraper.

Loads both detail_scraper versions with stubbed backend deps, real
release_grammar (copy), the REAL AppService.parse_size transcribed verbatim
from backend/api/dependencies.py, and compares scrape_details() output across
an adversarial corpus.
"""
import importlib.util
import json
import sys
import types

sys.dont_write_bytecode = True

SCRATCH = r"C:\Users\NLSur\AppData\Local\Temp\claude\X--Docker-Apps\9dcbeea6-3110-4e75-9bb1-c8261c8c8ea0\scratchpad"

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

# real grammar, loaded from the scratchpad copy
spec = importlib.util.spec_from_file_location(
    "backend.release_grammar", SCRATCH + r"\release_grammar_real.py")
release_grammar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_grammar)
backend_pkg.release_grammar = release_grammar

sys.modules.update({
    "backend": backend_pkg,
    "backend.models": models,
    "backend.hdencode_coordinator": coord,
    "backend.hdencode_transport": transport,
    "backend.rename": rename_pkg,
    "backend.rename.llm_identify": llm,
    "backend.release_grammar": release_grammar,
})

def load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m

old_mod = load("old_ds", SCRATCH + r"\old_detail_scraper.py")
new_mod = load("new_ds", SCRATCH + r"\new_detail_scraper.py")

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

rows = []
for name, kw in CASES:
    if kw.get("_glued"):
        kw2 = {k: v for k, v in kw.items() if k != "_glued"}
        html = build_html_glued(**kw2)
    else:
        html = build_html(**kw)
    o = run(old_mod, html)
    n = run(new_mod, html)
    diff = {}
    if o is None or n is None:
        if o != n:
            diff = {"_none": (o is None, n is None)}
    else:
        for f in FIELDS:
            if o[f] != n[f]:
                diff[f] = (o[f], n[f])
    rows.append((name, kw, o, n, diff))

same = sum(1 for *_x, d in rows if not d)
print(f"cases: {len(rows)}  identical: {same}  differing: {len(rows)-same}")
print("=" * 100)
for name, kw, o, n, diff in rows:
    if diff:
        print(f"\n### {name}")
        print(f"    input: {json.dumps(kw, ensure_ascii=False)}")
        for f, (ov, nv) in diff.items():
            print(f"    {f:16s} OLD={ov!r}  NEW={nv!r}")
print("\n" + "=" * 100)
print("IDENTICAL cases (spot-check values):")
for name, kw, o, n, diff in rows:
    if not diff and o is not None:
        print(f"  {name:34s} title={o['display_title']!r} yr={o['year']} tv={o['is_tv']} "
              f"s={o['season']} e={o['episode_number']} eps={o['episodes']} size={o['size']!r} res={o['res']!r}")

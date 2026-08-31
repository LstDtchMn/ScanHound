"""Measure release_grammar vs filename_utils on the SAME strings. Read-only."""
import sys, os
sys.path.insert(0, os.getcwd())
from backend import release_grammar as g
from backend.filename_utils import parse_filename

CORPUS = [
 "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR10plus.HEVC-GRP.mkv",
 "Dune.Part.Two.2024.4K.UHD.BluRay.x265-TERMINAL.mkv",
 "The.Batman.2022.1080p.BluRay.x264-SPARKS.mkv",
 "Some.Show.S01E02.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv",
 "Another.Series.S03.1080p.WEB-DL.H.265-EDITH.mkv",
 "Old.Film.1975.720p.BluRay.x264-AMIABLE.mkv",
 "Concert.Film.1920x1080.2019.1080p.WEB-DL.mkv",
 "Movie.With.DTS5.1.Audio.2021.2160p.WEB-DL.mkv",
 "Long.Run.S104.2160p.WEB-DL.mkv",
 "Great.Show.Complete.Series.1080p.WEB-DL.mkv",
 "Docu.Mini.Series.1080p.WEB-DL.mkv",
 "Thing.Season.4.1080p.WEB-DL.mkv",
]

rows, div = [], {}
for name in CORPUS:
    fu = parse_filename(name)
    se = g.parse_season_episode(name)
    gr = {
        "year": g.parse_year(name),
        "season": se.season,
        "resolution": g.parse_resolution(name),
        "is_tv": g.title_indicates_tv(name),
    }
    fux = {
        "year": fu["year"],
        "season": fu["season"],
        "resolution": g.canonical_resolution(fu["resolution"]),
        "is_tv": fu["is_tv"],
    }
    for k in gr:
        if gr[k] != fux[k]:
            div.setdefault(k, []).append((name, gr[k], fux[k]))
            rows.append((k, name, gr[k], fux[k]))

print(f"{'FIELD':11} {'GRAMMAR':>10}  {'FILENAME_UTILS':>14}   TITLE")
print("-"*96)
for k, n, a, b in rows:
    print(f"{k:11} {str(a):>10}  {str(b):>14}   {n[:52]}")
print()
print("DIVERGENT FIELDS:", {k: len(v) for k, v in sorted(div.items())} or "none")
print(f"titles: {len(CORPUS)}   divergent observations: {len(rows)}")
print()
print("NOTE: resolution compared AFTER canonicalisation, so a pure spelling")
print("      difference is NOT counted. These are semantic disagreements.")

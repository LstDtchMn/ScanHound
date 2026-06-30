"""Parse the owner's exported FEL / MEL movie lists into dv_scan seed records.

Runs on the HOST (the lists live on the desktop; the container can't see them).
Reads the UTF-16 ``Movies_WITH_FEL.txt`` / ``Movies_WITHOUT_FEL.txt`` exports,
each line of the form::

    [FEL] A Beautiful Mind (2001).mkv - Location: \\TURTLELANDSRV2\4K HDR Colombo\Movies 2

and writes ``data/dv_seed.json`` (which is bind-mounted to /data inside the
container) for ``import_dv_seed`` to upsert into the dv_scan inventory.

Marker → layer: [FEL]→fel, [MEL]→mel, [UNKNOWN DV]→unknown.

Usage:  python scripts/parse_dv_seed.py <file1.txt> <file2.txt> ...
"""
import json
import os
import re
import sys

_LAYER = {"FEL": "fel", "MEL": "mel", "UNKNOWN DV": "unknown"}
_LINE = re.compile(r"^\s*\[([^\]]+)\]\s*(.+?)\s+-\s+Location:\s+(.+?)\s*$")
_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "dv_seed.json")


def main(paths):
    records = {}
    skipped = 0
    for fpath in paths:
        # utf-16 auto-detects the BOM + endianness of these PowerShell exports.
        with open(fpath, encoding="utf-16") as f:
            for line in f:
                m = _LINE.match(line)
                if not m:
                    continue
                marker, fname, loc = (g.strip() for g in m.groups())
                layer = _LAYER.get(marker.upper())
                if not layer:
                    skipped += 1
                    continue
                path = loc.rstrip("\\") + "\\" + fname
                title = re.sub(r"\.mkv$", "", fname, flags=re.IGNORECASE)
                records[path] = {"path": path, "title": title, "dv_layer": layer}
    out = list(records.values())
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    by_layer = {l: sum(1 for r in out if r["dv_layer"] == l)
                for l in ("fel", "mel", "unknown")}
    print(f"Wrote {len(out)} records to {_OUT}")
    print(f"  by layer: {by_layer}  (skipped {skipped} unmatched marker lines)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: parse_dv_seed.py <list.txt> [<list2.txt> ...]")
    main(sys.argv[1:])

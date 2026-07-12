"""Shared longest-prefix-match path translation, used by both
RenameService._translate_path() (JD download-path -> container path) and
the Plex-library file_path -> container path translator. A pure function --
no config/DB access -- so both callers can plug in their own mappings text
and this stays trivially unit-testable.
"""
from __future__ import annotations

from typing import Optional


def translate_plex_path(raw_path: str, mappings_text: Optional[str]) -> str:
    """Translate `raw_path` using `mappings_text` (one `host => container`
    line per line). Longest host prefix wins; a mapping only matches at a
    path boundary (exact match or the next character is '/'), so a mapping
    for 'F:/Downloads' never also matches 'F:/Downloads2/...'. Malformed
    lines (no '=>', empty host or container) are skipped. Returns
    `raw_path` unchanged if nothing matches or `raw_path`/`mappings_text`
    is empty."""
    if not raw_path:
        return raw_path
    norm = raw_path.replace("\\", "/")
    best = None  # (host_prefix_len, translated)
    for line in str(mappings_text or "").splitlines():
        if "=>" not in line:
            continue
        host, container = (p.strip() for p in line.split("=>", 1))
        if not host or not container:
            continue
        hp = host.replace("\\", "/").rstrip("/")
        nl, hl = norm.lower(), hp.lower()
        if hp and (nl == hl or nl.startswith(hl + "/")):
            rest = norm[len(hp):].lstrip("/")
            translated = container.rstrip("/") + ("/" + rest if rest else "")
            if best is None or len(hp) > best[0]:
                best = (len(hp), translated)
    return best[1] if best else raw_path


def _prefix_key(raw_path: str) -> Optional[str]:
    """Coarse top-level grouping key for a Plex-reported path, used to group
    unmapped files into one representative entry per library location
    instead of flagging every individual file.

    For a UNC path this is the server plus share name (``\\\\server\\share``)
    -- the minimum unit a mapping is ever configured against in this
    deployment (see the 23-mapping seed set, every NAS entry is exactly
    server+share). For a local drive-letter path with at least two directory
    levels below the drive -- the ``C:\\1080p Drives\\<alias>`` /
    ``C:\\4K Drives\\<alias>`` junction-folder convention the seed mappings
    also use -- it's the drive plus those first two levels (so a movie's own
    subfolder three levels down still groups under its alias). A drive
    letter with only a single directory level below it (or none) collapses
    to just the drive root, since a lone unknown subfolder isn't a specific
    enough signal to act on by itself. Returns None for anything that isn't
    a recognizable Windows/UNC path at all."""
    if not raw_path:
        return None
    if raw_path.startswith("\\\\"):
        segs = raw_path[2:].split("\\")
        if len(segs) < 2 or not segs[0] or not segs[1]:
            return None
        return f"\\\\{segs[0]}\\{segs[1]}"
    if len(raw_path) > 1 and raw_path[1] == ":":
        rest = raw_path[2:]
        segs = [s for s in rest.split("\\") if s]
        dirs = segs[:-1] if len(segs) > 1 else []
        if len(dirs) >= 2:
            return raw_path[:2] + "\\" + dirs[0] + "\\" + dirs[1]
        return raw_path[:2] + "\\"
    return None


def find_unmapped_plex_path_prefixes(plex_cache_rows: list, mappings_text: Optional[str]) -> list:
    """Return the distinct set of top-level path prefixes among
    `plex_cache_rows` (each a dict with a 'file_path' key) for which
    `translate_plex_path` is currently a no-op -- i.e. no configured mapping
    actually changes the path. Sorted for stable, testable output."""
    seen = set()
    unmapped = set()
    for row in plex_cache_rows:
        path = row.get("file_path") if isinstance(row, dict) else None
        if not path:
            continue
        key = _prefix_key(path)
        if not key or key in seen:
            continue
        seen.add(key)
        if translate_plex_path(path, mappings_text) == path:
            unmapped.add(key)
    return sorted(unmapped)

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

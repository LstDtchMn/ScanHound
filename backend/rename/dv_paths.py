"""Path canonicalization for cross-machine DV matching.

Standalone and dependency-free: does NOT use PlexManager.translate_path (which
is dead — _path_mappings is never populated and PathMapping.translate is a bare
str.replace with no case/separator handling). The host detector records
host-native paths (drive letters or UNC); Plex serves whatever letter/case/UNC
it stored. normalize_path() collapses both into one comparable string.
"""
from typing import List, Optional, Tuple

# (drive_root, unc_root) pairs, e.g. ("Y:", r"\\SRV\Share"). Both roots must
# point at the SAME physical storage. Empty by default — populated from
# dv_label_vocab/config or the dry-run sampling gate (design §7.4).
DEFAULT_DV_MAPPINGS: List[Tuple[str, str]] = []


def _unify(s: str) -> str:
    """Backslashes -> forward slashes, casefold."""
    return s.replace("\\", "/").casefold()


def _trim(s: str) -> str:
    """Collapse duplicate separators; strip trailing slashes/dots/spaces."""
    while "//" in s:
        s = s.replace("//", "/")
    return s.rstrip("/. ")


def normalize_path(p: str, mappings: Optional[List[Tuple[str, str]]] = None) -> str:
    """Canonicalize *p* for cross-machine equality.

    Steps: (1) unify separators to '/'; (2) casefold; (3) rewrite each mapped
    drive/UNC root to a single canonical form (longest matching prefix wins so a
    deeper UNC share isn't shadowed by a shorter one); (4) trim trailing junk.
    Returns '' for a falsy input.
    """
    if not p:
        return ""
    s = _unify(p)
    table = mappings if mappings is not None else DEFAULT_DV_MAPPINGS
    # Canonical target = the drive form (short + stable). Build (variant, drive)
    # rewrite pairs from BOTH the drive and UNC roots, longest-prefix first.
    rewrites: List[Tuple[str, str]] = []
    for drive_root, unc_root in table:
        canon = _unify(drive_root)
        rewrites.append((_unify(unc_root), canon))
        rewrites.append((canon, canon))
    rewrites.sort(key=lambda pair: len(pair[0]), reverse=True)
    for variant, canon in rewrites:
        if s == variant or s.startswith(variant + "/"):
            s = canon + s[len(variant):]
            break
    return _trim(s)


def same_target(a: str, b: str,
                mappings: Optional[List[Tuple[str, str]]] = None) -> bool:
    """True iff *a* and *b* normalize to the same canonical path."""
    return normalize_path(a, mappings) == normalize_path(b, mappings)

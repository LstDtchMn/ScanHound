"""DV Plex labeler: reconcile a CLOSED managed label set against dv_scan.

Reconciles ONLY within {DV FEL, DV MEL, DV P8, DV P5} — never a 'DV ' prefix
wildcard (that deleted user labels like 'DV Cut'). Uses the bulk lib.all()
objects already in memory; no per-movie fetchItem for path resolution.
"""
import json
import logging
import time

from backend.rename.dv_paths import normalize_path

logger = logging.getLogger(__name__)

MANAGED = {"DV FEL", "DV MEL", "DV P8", "DV P5"}

# highest-first preference when a title's parts disagree
_LAYER_RANK = ["fel", "mel", "profile8", "profile5"]

_THROTTLE_S = 0.05  # inter-write pause so a big library can't hammer Plex


def desired_label(layer, vocab):
    """Map a dv_layer to its managed label, or None for none/unknown/NULL."""
    if not layer or layer in ("none", "unknown"):
        return None
    label = vocab.get(layer)
    return label if label in MANAGED else None


def pick_layer(norm_paths, index):
    """Best layer among a movie's candidate normalized paths (rank fel>mel>p8>p5)."""
    found = [index[p] for p in norm_paths if p in index]
    for rank in _LAYER_RANK:
        if rank in found:
            return rank
    return found[0] if found else None


def build_index(rows, mappings=None):
    """{normalize_path(path) -> dv_layer} from scan-source rows."""
    idx = {}
    for r in rows:
        p = normalize_path(r.get("path"), mappings)
        if p:
            idx[p] = r.get("dv_layer")
    return idx


def build_index_and_paths(rows, mappings=None):
    """Single pass over rows: ({norm -> dv_layer}, {norm -> original_path}).

    Same normalization semantics as build_index, but also captures the
    original (un-normalized) row path so callers can recover it in O(1)
    instead of re-scanning all rows per lookup.
    """
    idx = {}
    norm_to_path = {}
    for r in rows:
        p = normalize_path(r.get("path"), mappings)
        if p:
            idx[p] = r.get("dv_layer")
            norm_to_path[p] = r.get("path")
    return idx, norm_to_path


def _movie_norm_paths(movie, mappings):
    paths = []
    for media in (movie.media or []):
        for part in (media.parts or []):
            f = getattr(part, "file", None)
            if f:
                paths.append(normalize_path(f, mappings))
    return paths


def _existing_labels(movie):
    out = set()
    for lab in (getattr(movie, "labels", None) or []):
        tag = getattr(lab, "tag", None) or (lab if isinstance(lab, str) else None)
        if tag:
            out.add(tag)
    return out


def reconcile_movie(movie, index, vocab, pm, *, dry_run=False, mappings=None,
                    additive_only=False):
    """Reconcile one movie's managed label. Returns {added, removed, matched}.

    ``additive_only`` adds a missing label but never removes an existing one.
    Unattended (scheduled) syncs use it: a movie whose path can't be matched
    this run yields desired=None, which in full-reconcile mode strips its
    managed labels. That's correct for a deliberate manual sync, but on a timer
    a transient matching failure (a dropped mount, a changed Plex path form, a
    mapping gap) would silently wipe DV labels library-wide — and those labels
    are what the Kometa FEL/MEL overlays key on. Removals stay manual.
    """
    norm_paths = _movie_norm_paths(movie, mappings)
    layer = pick_layer(norm_paths, index)
    desired = desired_label(layer, vocab)
    existing_managed = _existing_labels(movie) & MANAGED

    added, removed = [], []
    if desired and desired not in existing_managed:
        added.append(desired)
    if not additive_only:
        for stale in existing_managed - ({desired} if desired else set()):
            removed.append(stale)

    if not dry_run:
        for lbl in added:
            try:
                pm.add_label(movie.ratingKey, lbl)
            except Exception as e:
                logger.warning("add_label %s on %s failed: %s", lbl, movie.ratingKey, e)
        for lbl in removed:
            try:
                pm.remove_label(movie.ratingKey, lbl)
            except Exception as e:
                logger.warning("remove_label %s on %s failed: %s", lbl, movie.ratingKey, e)
        if added or removed:
            time.sleep(_THROTTLE_S)

    return {"added": added, "removed": removed, "matched": layer is not None}


_DEFAULT_VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}


def _vocab_from_config(config):
    raw = config.get("dv_label_vocab")
    if not raw:
        return dict(_DEFAULT_VOCAB)
    try:
        v = json.loads(raw)
        parsed = {k: val for k, val in v.items() if val in MANAGED}
        return parsed or dict(_DEFAULT_VOCAB)
    except (ValueError, TypeError):
        return dict(_DEFAULT_VOCAB)


def sync_labels(db, pm, config, *, dry_run=False, progress_cb=None, mappings=None,
                additive_only=False):
    """Reconcile every movie against dv_scan (source='scan'). Returns a summary.

    ``additive_only`` never removes a managed label — see reconcile_movie. The
    scheduled auto-sync passes it; the manual button does not.
    """
    vocab = _vocab_from_config(config)
    rows = db.get_dv_scans(source="scan", limit=1000000)
    index, norm_to_path = build_index_and_paths(rows, mappings)

    movie_libs = (config.get("movie_libs")
                  or config.get("known_movie_libraries") or [])
    seen = set()
    movies = []
    for lib_name in movie_libs:
        try:
            lib = pm.get_library_section(lib_name)
            if not lib:
                continue
            for mv in lib.all():
                if mv.ratingKey in seen:
                    continue
                seen.add(mv.ratingKey)
                movies.append(mv)
        except Exception as e:
            logger.warning("dv sync: library %s failed: %s", lib_name, e)

    total = len(movies)
    added_n = removed_n = matched_n = 0
    for i, mv in enumerate(movies):
        try:
            res = reconcile_movie(mv, index, vocab, pm,
                                  dry_run=dry_run, mappings=mappings,
                                  additive_only=additive_only)
            added_n += len(res["added"])
            removed_n += len(res["removed"])
            if res["matched"]:
                matched_n += 1
                if not dry_run:
                    # O(1) rating_key back-write for the matched copy
                    for p in _movie_norm_paths(mv, mappings):
                        if p in index:
                            db.upsert_dv_scan(
                                norm_to_path.get(p, p),
                                index[p], rating_key=str(mv.ratingKey),
                                source="scan")
                            break
        except Exception as e:
            logger.warning("dv sync: title %s failed: %s",
                           getattr(mv, "title", "?"), e)
        if progress_cb:
            progress_cb(i + 1, total)

    return {"total": total, "added": added_n, "removed": removed_n,
            "matched": matched_n, "dry_run": dry_run}

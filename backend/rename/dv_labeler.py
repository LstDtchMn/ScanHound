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

#: Labels the layer tags map to — one per layer, renameable via dv_label_vocab.
_LAYER_LABELS = {"DV FEL", "DV MEL", "DV8", "DV5"}

#: Labels this module used to apply and no longer wants. They stay in MANAGED
#: so the next sync REMOVES them; dropping them from MANAGED instead would make
#: the labeler blind to them and leave a stale 'DV P8' on every Profile 8 title
#: forever, unmanaged and indistinguishable from a label the user applied.
#:
#: A rename is only finished when the old name is cleaned up, so this set is
#: the migration. It can be emptied once a full sync has run against the live
#: library and the old labels are gone.
RETIRED_LABELS = {"DV P8", "DV P5"}

#: Broader tags DERIVED from the same verdict, so Kometa can key an overlay on
#: "any Profile 7" or "any Dolby Vision" without enumerating layers. A FEL title
#: carries DV FEL *and* DV7 *and* DV: they describe the same fact at three
#: widths, they are not alternatives.
#:
#: A group tag is only worth having when it spans MORE THAN ONE badge. Profile
#: 7 does (FEL and MEL), so DV7 earns its place; profiles 8 and 5 are each a
#: single badge already, so a 'DV8' group tag beside a 'DV8' badge would be a
#: pure alias — identical set of titles, no expressive gain, one more label to
#: write and to get wrong. That rule is why this map is asymmetric.
_GROUP_LABELS = {
    "fel": ("DV7", "DV"),
    "mel": ("DV7", "DV"),
    "profile8": ("DV",),
    "profile5": ("DV",),
}

#: The one tag that is NOT derived from a DV verdict alone. dv_scan has no HDR
#: axis: 'none' means "dovi_tool found no Dolby Vision", equally true of an
#: HDR10 remux and a plain SDR 4K file. HDR10 therefore requires BOTH an
#: authoritative 'none' AND Plex's own wide-gamut flag, and is withheld when
#: either is missing.
HDR10_LABEL = "HDR10"

#: THE CLOSED SET this module may remove. Everything reconcile_movie strips
#: comes from here, so a user's own label ('DV Cut' is the historical example)
#: is never touched. RETIRED_LABELS are included precisely so they CAN be
#: removed — see that constant.
#:
#: CAUTION when extending: adding a label here hands it to the labeler, which
#: will REMOVE it from any title whose verdict does not call for it. 'DV' is
#: the broadest and therefore the most likely to collide with a hand-applied
#: label of the same name.
MANAGED = _LAYER_LABELS | {"DV7", "DV", HDR10_LABEL} | RETIRED_LABELS

# highest-first preference when a title's parts disagree
_LAYER_RANK = ["fel", "mel", "profile8", "profile5"]

_THROTTLE_S = 0.05  # inter-write pause so a big library can't hammer Plex


#: A layer value that records a FAILED detection rather than a finding.
#: dv_detect resolves any error — no dovi_tool, unreadable file, timeout,
#: subprocess failure — to this, and dv_host_scan.classify_to_row stores it.
#: It is NOT evidence, and must never be treated as an authoritative answer
#: about a file. 'none' IS authoritative: it means the tool ran and found no
#: Dolby Vision.
LAYER_DETECTION_FAILED = "unknown"


def is_authoritative(layer):
    """Whether a dv_layer is a real finding we may act on destructively.

    The distinction dv_detect documents — "could not run" vs "confirmed no
    DV" — is only meaningful if it is enforced where labels are removed.
    """
    return layer is not None and layer != LAYER_DETECTION_FAILED


def desired_label(layer, vocab):
    """The single LAYER label for a dv_layer, or None for none/unknown/NULL.

    The specific badge only ('DV FEL'), never the derived group tags. Callers
    deciding what a title should carry want desired_labels(); this remains for
    reporting the one label that names the layer itself.
    """
    if not layer or layer in ("none", LAYER_DETECTION_FAILED):
        return None
    label = vocab.get(layer)
    return label if label in _LAYER_LABELS else None


def desired_labels(layer, vocab):
    """EVERY managed label a title with this layer should carry, as a set.

    One verdict yields several tags at different widths — 'fel' means the title
    is FEL, is Profile 7, and is Dolby Vision, all true at once. Returning a set
    is what lets reconcile_movie compute removals as "managed labels this title
    should not have", instead of the old "everything managed except THE label",
    which could only ever express one tag per title.

    An empty set means "carry no managed label": correct for 'none' (the tool
    ran and found no DV) and for 'unknown'/NULL — but at the removal step an
    empty set from a POSITIVE layer means a configuration gap, which
    reconcile_movie must distinguish. See its may_remove rules.
    """
    if not layer or layer in ("none", LAYER_DETECTION_FAILED):
        return set()
    out = set()
    primary = vocab.get(layer)
    if primary in _LAYER_LABELS:
        out.add(primary)
    out.update(g for g in _GROUP_LABELS.get(layer, ()) if g in MANAGED)
    return out


def pick_layer(norm_paths, index):
    """Aggregate one verdict for a title from ALL its parts.

    The contract, in order:

    1. any positive DV finding wins, by the documented rank (fel > mel > p8 > p5)
       -- one part proving Dolby Vision proves it for the title;
    2. otherwise, if ANY part is unclassified ('unknown') or has no row at all,
       the aggregate is 'unknown' -- absence has not been established;
    3. only when EVERY part is matched and authoritatively 'none' is the
       aggregate 'none'.

    Rules 2 and 3 are the fix for two unsafe behaviours. The old
    ``found[0] if found else None`` made a mixed ['none','unknown'] title
    depend on part ORDER -- filesystem/Plex ordering decided whether labels
    were deleted -- and it returned 'none' for a title whose other part had no
    row, treating an unproven part as proof of absence. Removal is destructive
    and Kometa's overlays key off these labels, so incomplete coverage must
    read as "don't know", never as "no".
    """
    found = [index[p] for p in norm_paths if p in index]
    for rank in _LAYER_RANK:
        if rank in found:
            return rank
    if not found:
        return None                      # nothing matched: not our title
    if len(found) != len(norm_paths):
        return LAYER_DETECTION_FAILED    # a part has no row -> incomplete
    if any(layer != "none" for layer in found):
        return LAYER_DETECTION_FAILED    # 'unknown' (or anything unranked)
    return "none"                        # every part authoritatively no-DV


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
                    additive_only=False, hdr_index=None):
    """Reconcile one movie's managed labels. Returns {added, removed, matched}.

    ``hdr_index`` is ``{rating_key: bool}`` from Plex (see
    ``get_plex_hdr_by_rating_key``). A key that is ABSENT means UNKNOWN, and
    unknown is not False: HDR10 is then neither added NOR removed, because a
    cache gap must not be read as "this title is not HDR" and strip a correct
    label. Passing None disables HDR10 handling entirely, which is what every
    caller that has no Plex cache to consult should do.

    ``additive_only`` leaves an unmatched movie untouched. A positive path
    match may still replace a stale managed label so unattended reconciliation
    converges after an authoritative rescan. A transient matching failure must
    never strip the labels that Kometa's FEL/MEL overlays depend on.

    "Matched" therefore means matched to a REAL finding: a row whose layer is
    'unknown' records that detection FAILED, and under additive_only it is
    treated exactly like no row at all. Reading it as a match was a
    label-stripping bug — desired_label('unknown') is None, so the removal
    loop subtracted nothing and stripped every managed DV label from the
    title during the unattended hourly sync. Any detection failure (an
    unreadable file on a network mount is the common one) could silently
    undo the FEL/MEL overlays.
    """
    norm_paths = _movie_norm_paths(movie, mappings)
    layer = pick_layer(norm_paths, index)
    desired_set = desired_labels(layer, vocab)
    desired = desired_label(layer, vocab)      # the layer badge, for reporting
    existing_managed = _existing_labels(movie) & MANAGED
    authoritative = is_authoritative(layer)

    # HDR10 needs BOTH halves: the tool ran and found no Dolby Vision, AND Plex
    # sees wide-gamut video. Either half missing means the label is withheld.
    # `hdr_state is None` is UNKNOWN — no cached Plex row, or no index supplied
    # — and unknown must not authorise removal, so HDR10 is exempted from the
    # removal set entirely in that case. Reading absent-from-cache as "not HDR"
    # is the same silent-strip shape as the vocab gap.
    hdr_state = None
    if hdr_index is not None:
        hdr_state = hdr_index.get(str(getattr(movie, "ratingKey", "")))
    if layer == "none" and hdr_state is True:
        desired_set = desired_set | {HDR10_LABEL}
    exempt = {HDR10_LABEL} if hdr_state is None else set()

    # Removal is the destructive half, so it needs its own rule per case:
    #   'unknown'      -> NEVER remove, in any mode. Classification failed;
    #                     a manual full reconcile may ask to reconcile known
    #                     evidence, but it cannot convert failed evidence into
    #                     proof of absence.
    #   authoritative  -> remove stale labels, in any mode (that is what makes
    #                     unattended reconciliation converge after a rescan).
    #   no match       -> the pre-existing policy: full reconcile removes,
    #                     additive_only leaves the title alone.
    #   unmapped layer -> NEVER remove. A POSITIVE finding with no label in the
    #                     vocab is a CONFIGURATION gap, not evidence that the
    #                     title should carry nothing. Without this, an unmapped
    #                     layer looks identical to 'none' at the removal step
    #                     (both give desired=None) and strips the correct badge.
    #                     _vocab_from_config now merges over the defaults so the
    #                     four known layers cannot go unmapped -- but a NEW
    #                     layer value (the planned DV7/DV8/HDR10-only work adds
    #                     some) would reintroduce it the moment a layer reaches
    #                     this code before its vocab entry does.
    # "Unmapped" means the LAYER BADGE is missing, not that the whole set is
    # empty. Once group tags exist a positive layer almost always yields DV7/DV,
    # so testing the set would have made this guard unreachable for the four
    # known layers -- and a vocab gap would once again strip the correct badge
    # while quietly adding the group tags. Test the badge itself.
    positive = layer is not None and layer not in ("none", LAYER_DETECTION_FAILED)
    unmapped = positive and desired is None
    if layer == LAYER_DETECTION_FAILED or unmapped:
        may_remove = False
    else:
        may_remove = authoritative or not additive_only

    # Set arithmetic, not "everything except THE label". The old form could
    # only ever express one managed tag per title, so a second correct tag
    # (DV7 beside DV FEL) was computed as stale and removed on the next pass —
    # the two writers would have fought forever. Sorted for deterministic
    # output, which the summaries and tests both rely on.
    added = sorted(desired_set - existing_managed)
    removed = sorted(existing_managed - desired_set - exempt) if may_remove else []

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

    return {
        "added": added,
        "removed": removed,
        # A failed detection is not a match. Besides the summary count, this
        # gates sync_labels' rating_key back-write -- re-persisting an
        # 'unknown' row (as source='scan') on every pass is what made a
        # single detection failure sticky instead of self-healing on the
        # next host run.
        "matched": authoritative,
        "layer": layer,
        "desired_label": desired,
        "existing_labels": sorted(existing_managed),
    }


#: The live config still stores the OLD names for profile8/profile5. Those
#: values are no longer layer labels, so _vocab_from_config filters them out
#: and the merge-over-defaults added earlier supplies these instead — the
#: rename needs no settings edit to take effect, and logs which entries it
#: ignored. That fallback existing is the only reason this rename is a
#: one-file change.
_DEFAULT_VOCAB = {"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV8", "profile5": "DV5"}


def _vocab_from_config(config):
    """Build the layer -> label map, MERGED OVER the defaults.

    A partial or partly-invalid vocab must never leave a layer unmapped. It
    used to: entries whose value was not in MANAGED were filtered out, and the
    default was restored only when NOTHING survived. So one typo -- 'DV-FEL'
    for 'DV FEL' -- silently dropped the fel mapping while the other three
    stayed, and dv_label_vocab is stored as a free-text string with no
    validation at the settings boundary.

    That is not a cosmetic gap. desired_label() then returns None for a layer
    that is still AUTHORITATIVE, so reconcile_movie treats it as "this title
    should carry no managed label" and REMOVES the correct badge, in every
    mode including the unattended additive-only hourly sync, with nothing added
    back. One character in a settings field could strip DV FEL from every FEL
    title in the library.

    Merging over the defaults means an unmapped layer is impossible for the
    four known layers: a caller can rename labels, but cannot accidentally
    delete a mapping. Dropped entries are logged, because silently ignoring
    what someone typed is how the typo stayed invisible.
    """
    vocab = dict(_DEFAULT_VOCAB)
    raw = config.get("dv_label_vocab")
    if not raw:
        return vocab
    try:
        # Only LAYER labels are renameable. Mapping a layer onto a derived tag
        # ('fel' -> 'DV7') would make the group tag the layer badge as well, so
        # the two could no longer be told apart at the removal step.
        v = json.loads(raw)
        parsed = {k: val for k, val in v.items() if val in _LAYER_LABELS}
        dropped = sorted(set(v) - set(parsed)) if isinstance(v, dict) else []
        if dropped:
            logger.warning(
                "dv_label_vocab: ignoring %d entr(y/ies) whose label is not a "
                "layer label %s: %s — the default label is used for those "
                "layers instead", len(dropped), sorted(_LAYER_LABELS), dropped)
        vocab.update(parsed)
        return vocab
    except (ValueError, TypeError):
        logger.warning("dv_label_vocab is not valid JSON; using defaults")
        return dict(_DEFAULT_VOCAB)


def sync_labels(db, pm, config, *, dry_run=False, progress_cb=None, mappings=None,
                additive_only=False):
    """Reconcile every movie against dv_scan (source='scan'). Returns a summary.

    ``additive_only`` never removes labels from an unmatched movie — see
    reconcile_movie. BOTH callers now pass it: the scheduled auto-sync always,
    and the manual endpoint by default (``DvSyncRequest.additive_only``, which
    a caller may set False to ask for destructive reconciliation explicitly).

    This function's own default stays False so the parameter keeps meaning
    "opt IN to protection" at this layer; the safe choice is made at the API
    boundary, where the request that triggers it is visible.
    """
    vocab = _vocab_from_config(config)
    rows = db.get_dv_scans(source="scan", limit=1000000)
    index, norm_to_path = build_index_and_paths(rows, mappings)

    # Built ONCE for the whole sync, like the dv index — asking Plex per movie
    # would add an API round trip per title across the entire library. A db
    # without the method (older stubs, and every test double that predates it)
    # leaves this None, which disables HDR10 rather than guessing.
    hdr_index = None
    get_hdr = getattr(db, "get_plex_hdr_by_rating_key", None)
    if callable(get_hdr):
        try:
            hdr_index = get_hdr()
        except Exception as e:  # noqa: BLE001
            # Degrade to "unknown for every title": HDR10 is then neither added
            # nor removed. A cache read failure must not strip labels.
            logger.warning("HDR index unavailable; HDR10 labels left untouched: %s", e)
            hdr_index = None
    seed_rows = []
    list_seed = getattr(db, "list_dv_seed_baseline", None)
    if callable(list_seed):
        seed_rows = list_seed(limit=1000000)
    seed_index = {
        normalize_path(row.get("path"), mappings): row.get("seed_layer")
        for row in seed_rows if normalize_path(row.get("path"), mappings)
    }

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
    details = []
    for i, mv in enumerate(movies):
        try:
            res = reconcile_movie(mv, index, vocab, pm,
                                  dry_run=dry_run, mappings=mappings,
                                  additive_only=additive_only,
                                  hdr_index=hdr_index)
            added_n += len(res["added"])
            removed_n += len(res["removed"])
            if res["matched"]:
                matched_n += 1
                if not dry_run:
                    # O(1) rating_key annotation for the matched copy.
                    #
                    # UPDATE-only, and it passes NO layer. `index` is a snapshot
                    # taken at the start of the sync, so writing index[p] back
                    # would let a stale layer overwrite a detector import that
                    # landed while the sync was running -- leaving a stale layer
                    # beside a fresh signature. The labeler consumes scan
                    # observations; it does not produce them, and the only
                    # column it owns here is the Plex identity.
                    for p in _movie_norm_paths(mv, mappings):
                        if p in index:
                            db.annotate_dv_scan_rating_key(
                                norm_to_path.get(p, p), str(mv.ratingKey))
                            break
            if dry_run:
                movie_paths = _movie_norm_paths(mv, mappings)
                matched_path = next((p for p in movie_paths if p in index), None)
                original_path = norm_to_path.get(matched_path, matched_path) if matched_path else None
                seed_layer = next((seed_index[p] for p in movie_paths if p in seed_index), None)
                scan_layer = res["layer"]
                if seed_layer and scan_layer and seed_layer != scan_layer:
                    discrepancy = f"seed_{seed_layer}_live_{scan_layer}"
                elif seed_layer and not scan_layer:
                    discrepancy = "seed_unverified"
                elif seed_layer and scan_layer:
                    discrepancy = "verified"
                elif scan_layer:
                    discrepancy = "live_only"
                else:
                    discrepancy = "none"
                details.append({
                    "rating_key": str(mv.ratingKey),
                    "title": getattr(mv, "title", None),
                    "path": original_path,
                    "seed_layer": seed_layer,
                    "scan_layer": scan_layer,
                    "discrepancy": discrepancy,
                    "desired_label": res["desired_label"],
                    "existing_labels": res["existing_labels"],
                    "added": res["added"],
                    "removed": res["removed"],
                })
        except Exception as e:
            logger.warning("dv sync: title %s failed: %s",
                           getattr(mv, "title", "?"), e)
        if progress_cb:
            progress_cb(i + 1, total)

    return {"total": total, "added": added_n, "removed": removed_n,
            "matched": matched_n, "dry_run": dry_run,
            "writes": 0 if dry_run else added_n + removed_n,
            "details": details}

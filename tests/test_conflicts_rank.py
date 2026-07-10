from backend.rename import conflicts, mediainfo


def test_explicit_dv_layer_breaks_tie_same_resolution():
    fel = {"id": 1, "original_filename": "a.mkv", "resolution": "2160p", "dv_layer": "fel"}
    mel = {"id": 2, "original_filename": "b.mkv", "resolution": "2160p", "dv_layer": "mel"}
    assert conflicts._quality_score(fel) > conflicts._quality_score(mel)


def test_absent_explicit_fields_reproduce_filename_behaviour():
    remux = {"id": 1, "original_filename": "X.2160p.BluRay.REMUX.DV.mkv", "resolution": "2160p"}
    web = {"id": 2, "original_filename": "X.2160p.WEB-DL.mkv", "resolution": "2160p"}
    assert conflicts._quality_score(remux) > conflicts._quality_score(web)


def test_rank_conflict_recommends_incoming_when_better():
    existing = {"resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "old.mkv"}
    incoming = {"id": 9, "resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "new.mkv"}
    out = conflicts.rank_conflict(existing, incoming)
    assert out["recommended"] == "incoming"
    assert "2160p" in (out["reason"] or "")


def test_rank_conflict_keeps_existing_dv_remux_over_tag_rich_lower_res():
    # The correctness trap: existing is a Plex-named 2160p DV file (tags stripped),
    # incoming is a tag-rich 1080p. Judged on probed specs, existing wins.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "Movie (2024) [2160p].mkv"}
    incoming = {"id": 9, "resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "Movie.2024.1080p.BluRay.REMUX.Atmos.mkv"}
    assert conflicts.rank_conflict(existing, incoming)["recommended"] == "existing"


def test_rank_conflict_no_existing_is_incoming():
    out = conflicts.rank_conflict(None, {"id": 9, "resolution": "2160p",
                                          "original_filename": "n.mkv"})
    assert out["recommended"] == "incoming"


# ── FIX 1: a 2.39:1-cropped 4K DV FEL master must still rank as 2160p (not a
#    height-derived "1440p") and beat a clean 1080p incoming. Also attacks the
#    companion bug: the old res-rank map had no "1440p" entry at all, so a
#    mislabeled "1440p" file scored res_rank 0 — LOWER than even a 480p rival ──

def test_cropped_4k_dv_fel_beats_1080p_incoming_via_width_based_resolution():
    cropped_4k = mediainfo._res_label(3840, 1600)
    assert cropped_4k == "2160p"  # not "1440p" — the height-based misread
    existing = {"resolution": cropped_4k, "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "Movie (2024) [2160p] [DV].mkv"}
    incoming = {"id": 9, "resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "Movie.2024.1080p.WEB-DL.mkv"}
    assert conflicts.rank_conflict(existing, incoming)["recommended"] == "existing"


def test_res_rank_map_includes_1440p_between_1080p_and_2160p():
    lo = {"id": 1, "original_filename": "x.mkv", "resolution": "1080p"}
    mid = {"id": 2, "original_filename": "x.mkv", "resolution": "1440p"}
    hi = {"id": 3, "original_filename": "x.mkv", "resolution": "2160p"}
    assert conflicts._quality_score(hi) > conflicts._quality_score(mid) > conflicts._quality_score(lo)


# ── FIX 2: a probed hdr=="Dolby Vision" (ffprobe DOVI side_data) must force
#    the `dv` bit even with no cached dv_layer (never dovi_tool-scanned yet) —
#    otherwise an uncached-but-genuine-DV library file (Plex-clean filename)
#    loses the `dv` bit outright to a rival whose filename merely CLAIMS a DV
#    tag, before the hdr index is even compared ─────────────────────────────

def test_uncached_dv_hdr_beats_tag_rich_dv_tagged_web_dl():
    existing = {"resolution": "1080p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "Movie (2024) [1080p].mkv"}
    incoming = {"id": 9, "resolution": "1080p", "hdr": None, "dv_layer": None,
                "original_filename": "Movie.2024.1080p.WEB-DL.DV.DDP5.1.mkv"}
    assert conflicts.rank_conflict(existing, incoming)["recommended"] == "existing"

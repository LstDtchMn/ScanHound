from backend.rename import conflicts


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

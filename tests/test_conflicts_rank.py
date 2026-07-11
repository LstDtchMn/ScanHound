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


def test_find_library_duplicate_matches_by_imdb_id():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt123", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt999", "title": "y", "year": 2019, "is_tv": 0, "file_path": "/a"},
            {"key": "k2", "imdb_id": "tt123", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/library/movies/X (2020)/X.mkv"}]
    match = conflicts.find_library_duplicate(job, rows)
    assert match is not None
    assert match["key"] == "k2"


def test_find_library_duplicate_falls_back_to_title_year_when_no_imdb_match():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": None, "title": "The Movie", "year": 2020,
           "destination_path": "/library/movies-4k/The Movie (2020)", "new_filename": "The Movie (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": None, "title": "the movie", "year": 2020, "is_tv": 0, "file_path": "/library/movies/The Movie (2020)/f.mkv"}]
    match = conflicts.find_library_duplicate(job, rows)
    assert match is not None and match["key"] == "k1"


def test_find_library_duplicate_no_match_returns_none():
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt2", "title": "y", "year": 2021, "is_tv": 0, "file_path": "/a"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_excludes_same_path_match():
    # If the matched Plex row's file_path IS the job's own would-be
    # destination, that's the exact-path case (already covered by
    # destination_conflict) — not a library-wide duplicate.
    job = {"id": 1, "status": "matched", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0,
             "file_path": "/library/movies-4k/X (2020)/X (2020).mkv"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_tv_job_always_none():
    job = {"id": 1, "status": "matched", "media_type": "tv", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/tv/X", "new_filename": "X S01E01.mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/a"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_find_library_duplicate_applied_job_always_none():
    # An already-applied job has nothing left to resolve — must never be
    # flagged (would waste an analysis cycle on a completed job).
    job = {"id": 1, "status": "applied", "media_type": "movie", "imdb_id": "tt1", "title": "X", "year": 2020,
           "destination_path": "/library/movies-4k/X (2020)", "new_filename": "X (2020).mkv"}
    rows = [{"key": "k1", "imdb_id": "tt1", "title": "x", "year": 2020, "is_tv": 0, "file_path": "/library/movies/X (2020)/X.mkv"}]
    assert conflicts.find_library_duplicate(job, rows) is None


def test_needs_dv_layer_scan_true_when_both_dv_and_tied_on_everything_else():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is True


def test_needs_dv_layer_scan_false_when_resolution_already_decides_it():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    incoming = {"resolution": "1080p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_neither_side_is_dv():
    existing = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_only_one_side_is_dv():
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None, "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": None, "dv_layer": None, "original_filename": "b.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_false_when_dv_layer_already_known_on_both():
    # Already resolved (e.g. a prior scan) — nothing left to gain by re-scanning.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "mel", "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel", "original_filename": "a.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is False


def test_needs_dv_layer_scan_true_when_known_side_at_max_rank_could_still_tie():
    # ASYMMETRIC CASE: existing is known at max rank ("fel", rank 3), incoming
    # unscanned. A naive "existing already unbeatable, skip" analysis is WRONG:
    # incoming could ALSO scan as "fel" (rank 3), producing a genuine TIE
    # instead of existing's current confident win — a materially different,
    # informative outcome the scan can still reveal. Everything else here is
    # tied (no audio/source/edition tags in either filename), so this is pure
    # win-vs-tie, not a full reversal (see the next test for that).
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "b.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is True


def test_needs_dv_layer_scan_true_when_known_side_below_max_rank_and_other_side_unscanned():
    # ASYMMETRIC CASE: existing is known but below max ("mel", rank 2), incoming unscanned.
    # Scanning could reveal incoming is "fel" (rank 3) and flip the winner.
    # This genuinely warrants a scan.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "mel",
                "original_filename": "a.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "b.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is True


def test_needs_dv_layer_scan_true_when_a_dv_layer_tie_would_fall_through_to_a_real_reversal():
    # The critical case a naive "index 2 alone" analysis misses: existing is
    # known at max rank ("fel") and currently wins outright — BUT incoming
    # carries an Atmos tag (audio tier, index 5), which existing lacks. If
    # incoming's scan ALSO reveals "fel" (a tie at index 2, fully reachable
    # since fel is achievable), the comparison falls through to index 5 —
    # where incoming's Atmos tag wins outright. That's a full winner
    # reversal (existing -> incoming), not just a downgrade to tie, and only
    # the scan can reveal it.
    existing = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": "fel",
                "original_filename": "Movie.2020.2160p.mkv"}
    incoming = {"resolution": "2160p", "hdr": "Dolby Vision", "dv_layer": None,
                "original_filename": "Movie.2020.2160p.TrueHD.Atmos.mkv"}
    assert conflicts.needs_dv_layer_scan(existing, incoming) is True

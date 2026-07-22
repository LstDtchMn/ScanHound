"""Historic seed evidence must remain visible beside authoritative scans."""

from backend.database import DatabaseManager
from backend.rename.dv_labeler import sync_labels


class _Part:
    def __init__(self, path): self.file = path


class _Media:
    def __init__(self, path): self.parts = [_Part(path)]


class _Movie:
    ratingKey = "42"
    title = "Example"
    labels = []
    def __init__(self, path): self.media = [_Media(path)]


class _Library:
    def __init__(self, movies): self._movies = movies
    def all(self): return self._movies


class _Plex:
    def __init__(self, movies): self._movies = movies
    def get_library_section(self, _name): return _Library(self._movies)
    def add_label(self, *_args): raise AssertionError("dry run must not write")
    def remove_label(self, *_args): raise AssertionError("dry run must not write")


def test_seed_fel_live_mel_is_searchable_as_discrepancy(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    path = "/movies/example.mkv"
    db.upsert_dv_scan(path, "fel", title="Example", source="seed")
    assert db.backfill_dv_seed_baseline() == 1
    db.upsert_dv_scan(path, "mel", title="Example", source="scan")
    db.upsert_media_inventory({
        "path": path, "title": "Example", "resolution": "2160p",
        "dv_layer": "mel", "scan_state": "current",
    })

    rows = db.list_metadata_discrepancies()
    filtered = db.search_media_inventory(discrepancy="seed_fel_live_mel")

    assert rows == [{
        "path": path,
        "title": "Example",
        "rating_key": None,
        "seed_layer": "fel",
        "scan_layer": "mel",
        "discrepancy": "seed_fel_live_mel",
    }]
    assert filtered["total"] == 1
    assert filtered["items"][0]["seed_layer"] == "fel"
    assert filtered["items"][0]["discrepancy"] == "seed_fel_live_mel"


def test_matching_seed_and_live_layers_are_verified_not_discrepant(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    path = "/movies/example.mkv"
    db.upsert_dv_scan(path, "fel", source="seed")
    db.backfill_dv_seed_baseline()
    db.upsert_dv_scan(path, "fel", source="scan")
    db.upsert_media_inventory({"path": path, "dv_layer": "fel", "scan_state": "current"})

    assert db.list_metadata_discrepancies() == []
    assert db.search_media_inventory()["items"][0]["discrepancy"] == "verified"


def test_label_dry_run_exposes_seed_live_and_desired_label_without_writes(tmp_path):
    db = DatabaseManager(str(tmp_path / "inventory.sqlite"))
    path = "/movies/example.mkv"
    db.upsert_dv_scan(path, "fel", source="seed")
    db.backfill_dv_seed_baseline()
    db.upsert_dv_scan(path, "mel", source="scan")

    report = sync_labels(
        db, _Plex([_Movie(path)]), {"movie_libs": ["Movies"]}, dry_run=True
    )

    assert report["writes"] == 0
    assert report["details"] == [{
        "rating_key": "42", "title": "Example", "path": path,
        "seed_layer": "fel", "scan_layer": "mel",
        "discrepancy": "seed_fel_live_mel", "desired_label": "DV MEL",
        "existing_labels": [], "added": ["DV MEL"], "removed": [],
    }]

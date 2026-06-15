"""Property-based tests for ScanHound using randomized input generation.

Uses Python's built-in `random` and `string` modules with fixed seeds for
reproducible, deterministic testing.  Each test class exercises invariants
that must hold for *all* inputs, not just hand-picked examples.
"""

import os
import random
import re
import string
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from backend.app_service import LRUCache, clean_string, normalize_title
from backend.config import get_default_config, validate_config
from backend.database import DatabaseManager
from backend.matching import (
    MatchingEngine,
    cached_fuzz_ratio,
    cached_token_sort_ratio,
)
from backend.plex_manager import PathMapping
from backend.watchlist import (
    WatchlistItem,
    WatchlistItemStatus,
    WatchlistItemType,
    WatchlistManager,
)

# ---------------------------------------------------------------------------
# Helpers for random data generation
# ---------------------------------------------------------------------------
_ALL_CHARS = string.printable  # ASCII letters, digits, punctuation, whitespace
_UNICODE_EXTRAS = (
    "\u00e9\u00f1\u00fc\u00df\u00e4\u00f6\u00e0\u00e8\u00ec\u00f2"
    "\u00f9\u4e2d\u6587\u0410\u0411\u0412\u2603\u2764\u00a9\u2122"
    "\U0001f600\U0001f525\U0001f4a9\u200b\u200e\u200f\ufeff\u0000"
)


def _rand_str(rng: random.Random, min_len: int = 0, max_len: int = 60) -> str:
    """Generate a random string containing ASCII + some Unicode extras."""
    length = rng.randint(min_len, max_len)
    pool = _ALL_CHARS + _UNICODE_EXTRAS
    return "".join(rng.choice(pool) for _ in range(length))


def _rand_ascii(rng: random.Random, min_len: int = 0, max_len: int = 40) -> str:
    """Generate a random pure-ASCII string."""
    length = rng.randint(min_len, max_len)
    return "".join(rng.choice(string.printable) for _ in range(length))


def _rand_title(rng: random.Random) -> str:
    """Generate something that looks vaguely like a movie/TV title."""
    words = []
    for _ in range(rng.randint(1, 6)):
        word_len = rng.randint(1, 12)
        words.append("".join(rng.choice(string.ascii_letters) for _ in range(word_len)))
    title = " ".join(words)
    if rng.random() < 0.4:
        title += f" ({rng.randint(1950, 2026)})"
    return title


def _rand_resolution(rng: random.Random) -> str:
    return rng.choice(["720p", "1080p", "4K", "?", "SD", ""])


def _rand_size_str(rng: random.Random) -> str:
    templates = [
        "{v} GB",
        "{v} MB",
        "{v} TB",
        "{v}GB",
        "{v}MB",
        "{v} GiB",
        "?",
        "",
        "unknown",
        "{v}",
    ]
    v = round(rng.uniform(0.1, 150.0), 2)
    return rng.choice(templates).format(v=v)


# ===================================================================
# 1. clean_string Properties
# ===================================================================
class TestCleanStringProperties:
    """Property-based tests for the clean_string utility."""

    SEED = 42

    def test_never_raises_on_random_input(self):
        """clean_string must never raise an exception for any input."""
        rng = random.Random(self.SEED)
        for _ in range(100):
            s = _rand_str(rng, max_len=100)
            result = clean_string(s)  # must not raise
            assert isinstance(result, str)

    def test_always_returns_lowercase(self):
        """Output of clean_string is always fully lowercase."""
        rng = random.Random(self.SEED + 1)
        for _ in range(100):
            s = _rand_str(rng)
            result = clean_string(s)
            assert result == result.lower(), f"Not lowercase: {result!r}"

    def test_no_special_characters(self):
        """Output contains only [a-z0-9\\s]."""
        rng = random.Random(self.SEED + 2)
        pattern = re.compile(r"^[a-z0-9\s]*$")
        for _ in range(100):
            s = _rand_str(rng)
            result = clean_string(s)
            assert pattern.match(result), (
                f"Special chars found in clean_string output: {result!r}"
            )

    def test_idempotent(self):
        """clean_string(clean_string(s)) == clean_string(s)."""
        rng = random.Random(self.SEED + 3)
        for _ in range(100):
            s = _rand_str(rng)
            once = clean_string(s)
            twice = clean_string(once)
            assert once == twice, (
                f"Not idempotent: {once!r} vs {twice!r} (input={s!r})"
            )

    def test_empty_string(self):
        """clean_string('') always returns ''."""
        assert clean_string("") == ""

    def test_output_bounded_length(self):
        """len(clean_string(s)) <= len(s) + small constant (space normalization)."""
        rng = random.Random(self.SEED + 4)
        for _ in range(100):
            s = _rand_str(rng, max_len=200)
            result = clean_string(s)
            # The function strips, lowercases, and removes chars. Result
            # should never be longer than the original (it only removes).
            assert len(result) <= len(s) + 1, (
                f"Output longer than input: {len(result)} > {len(s)}"
            )


# ===================================================================
# 2. LRU Cache Invariants
# ===================================================================
class TestLRUCacheInvariants:
    """Property-based tests for the LRUCache."""

    SEED = 100

    def test_len_never_exceeds_maxsize(self):
        """After any sequence of inserts, len(cache) <= maxsize."""
        rng = random.Random(self.SEED)
        for maxsize in [1, 5, 10, 50]:
            cache = LRUCache(maxsize=maxsize)
            for i in range(maxsize * 3):
                key = rng.randint(0, maxsize * 5)
                cache[key] = rng.random()
                assert len(cache) <= maxsize, (
                    f"len={len(cache)} exceeds maxsize={maxsize}"
                )

    def test_clear_empties_cache(self):
        """After clear(), len(cache) == 0."""
        cache = LRUCache(maxsize=20)
        for i in range(30):
            cache[i] = i * 2
        cache.clear()
        assert len(cache) == 0

    def test_get_returns_set_value(self):
        """If key was set (and not evicted), cache[key] returns the value."""
        rng = random.Random(self.SEED + 1)
        cache = LRUCache(maxsize=200)
        expected = {}
        for _ in range(200):
            k = rng.randint(0, 150)
            v = rng.random()
            cache[k] = v
            expected[k] = v

        # All 200 inserts fit within maxsize so nothing should be evicted
        for k, v in expected.items():
            assert cache[k] == v

    def test_random_ops_never_raise(self):
        """1000 random get/set operations never raise unexpected exceptions."""
        rng = random.Random(self.SEED + 2)
        cache = LRUCache(maxsize=50)
        for _ in range(1000):
            op = rng.choice(["get", "set", "contains", "clear"])
            key = rng.randint(0, 200)
            if op == "set":
                cache[key] = rng.random()
            elif op == "get":
                cache.get(key, None)  # must not raise
            elif op == "contains":
                _ = key in cache  # must not raise
            elif op == "clear":
                cache.clear()

    def test_old_keys_evicted(self):
        """After N >> maxsize insertions of unique keys, old keys are evicted."""
        maxsize = 10
        cache = LRUCache(maxsize=maxsize)
        # Insert 0..99
        for i in range(100):
            cache[i] = i
        # Only the last `maxsize` keys should remain
        assert len(cache) == maxsize
        # Keys 0..89 should be gone
        for i in range(90):
            assert cache.get(i) is None, f"Key {i} should have been evicted"
        # Keys 90..99 should still be present
        for i in range(90, 100):
            assert cache[i] == i


# ===================================================================
# 3. validate_config Invariants
# ===================================================================
class TestValidateConfigInvariants:
    """Property-based tests for the validate_config function."""

    SEED = 200

    def test_never_raises_on_random_dict(self):
        """validate_config must never raise for any dict input.

        After bug fixes, validate_config now safely handles None values
        and non-numeric strings via _safe_int/_safe_numeric helpers.
        """
        rng = random.Random(self.SEED)
        for _ in range(100):
            cfg = {}
            for _ in range(rng.randint(0, 10)):
                key = rng.choice([
                    "min_size_mb", "scan_threads", "cache_duration",
                    "upgrade_sensitivity", "tv_match_threshold",
                    "movie_match_threshold", "year_tolerance",
                    "scheduler_interval", "low_match_threshold",
                    "random_key",
                ])
                val = rng.choice([
                    rng.randint(-1000, 1000),
                    rng.random() * 200 - 100,
                    "not_a_number",
                    None,
                    True,
                    [],
                ])
                cfg[key] = val
            result = validate_config(cfg)
            assert isinstance(result, dict)

    def test_numeric_fields_within_bounds(self):
        """After validation, numeric fields are clamped to documented bounds."""
        rng = random.Random(self.SEED + 1)
        bounds = {
            "min_size_mb": (0, None),
            "scheduler_interval": (1, None),
            "scan_threads": (1, 50),
            "cache_duration": (0, None),
            "upgrade_sensitivity": (0, None),
            "tv_match_threshold": (0, 100),
            "low_match_threshold": (0, 100),
            "movie_match_threshold": (0, 100),
            "year_tolerance": (0, 10),
        }
        for _ in range(100):
            cfg = get_default_config()
            # Randomly mutate some numeric fields
            for key in bounds:
                if rng.random() < 0.5:
                    cfg[key] = rng.randint(-500, 500)
            result = validate_config(cfg)
            for key, (lo, hi) in bounds.items():
                val = result.get(key)
                if val is not None and isinstance(val, (int, float)):
                    if lo is not None:
                        assert val >= lo, (
                            f"{key}={val} below minimum {lo}"
                        )
                    if hi is not None:
                        assert val <= hi, (
                            f"{key}={val} above maximum {hi}"
                        )

    def test_idempotent(self):
        """validate_config(validate_config(c)) == validate_config(c)."""
        rng = random.Random(self.SEED + 2)
        for _ in range(100):
            cfg = get_default_config()
            # Randomly mutate
            for key in list(cfg.keys()):
                if rng.random() < 0.15 and isinstance(cfg[key], (int, float)):
                    cfg[key] = rng.randint(-100, 500)
            once = validate_config(cfg)
            twice = validate_config(once)
            assert once == twice, "validate_config is not idempotent"

    def test_random_mutations_produce_valid_config(self):
        """For 100 random mutations of default config, result is a valid dict."""
        rng = random.Random(self.SEED + 3)
        for _ in range(100):
            cfg = get_default_config()
            # Mutate a handful of keys
            mutations = rng.randint(1, 8)
            numeric_keys = [
                "min_size_mb", "scan_threads", "cache_duration",
                "upgrade_sensitivity", "scheduler_interval",
            ]
            for _ in range(mutations):
                key = rng.choice(numeric_keys)
                cfg[key] = rng.randint(-50, 300)
            result = validate_config(cfg)
            assert isinstance(result, dict)
            # Must still have the keys from default config
            for k in get_default_config():
                assert k in result, f"Key {k} missing after validate"


# ===================================================================
# 4. Size Parsing Robustness
# ===================================================================
class TestSizeParsingRobustness:
    """Property-based tests for the MockApp.parse_size function."""

    SEED = 300

    @pytest.fixture(autouse=True)
    def _setup_parser(self, mock_app):
        """Bind mock_app to self for convenience."""
        self.parse_size = mock_app.parse_size

    def test_never_raises_returns_nonneg_float(self):
        """parse_size never raises and always returns float >= 0."""
        rng = random.Random(self.SEED)
        for _ in range(200):
            s = _rand_str(rng, max_len=30)
            result = self.parse_size(s)
            assert isinstance(result, float)
            assert result >= 0.0, f"Negative size for input {s!r}: {result}"

    def test_known_gb_format(self):
        """'{N} GB' always parses to approximately N."""
        rng = random.Random(self.SEED + 1)
        for _ in range(50):
            n = round(rng.uniform(0.5, 500.0), 2)
            result = self.parse_size(f"{n} GB")
            assert abs(result - n) < 0.01, (
                f"Expected ~{n}, got {result} for '{n} GB'"
            )

    def test_known_mb_format(self):
        """'{N} MB' always parses to approximately N/1024."""
        rng = random.Random(self.SEED + 2)
        for _ in range(50):
            n = round(rng.uniform(100.0, 5000.0), 2)
            result = self.parse_size(f"{n} MB")
            expected = n / 1024
            assert abs(result - expected) < 0.01, (
                f"Expected ~{expected}, got {result} for '{n} MB'"
            )

    def test_known_tb_format(self):
        """'{N} TB' always parses to approximately N*1024."""
        rng = random.Random(self.SEED + 3)
        for _ in range(30):
            n = round(rng.uniform(0.1, 5.0), 2)
            result = self.parse_size(f"{n} TB")
            expected = n * 1024
            assert abs(result - expected) < 1.0, (
                f"Expected ~{expected}, got {result} for '{n} TB'"
            )

    def test_garbage_returns_zero(self):
        """Completely non-numeric garbage returns 0.0."""
        garbage_inputs = [
            "hello world", "???", "no size here",
            "!@#$%^&*()", "", "?", None, 42,
        ]
        for inp in garbage_inputs:
            result = self.parse_size(inp)
            assert result == 0.0, f"Expected 0.0 for {inp!r}, got {result}"

    def test_random_float_gb(self):
        """parse_size('X GB') where X is random float always returns X."""
        rng = random.Random(self.SEED + 4)
        for _ in range(50):
            x = round(rng.uniform(0.01, 999.99), 2)
            result = self.parse_size(f"{x} GB")
            assert abs(result - x) < 0.01


# ===================================================================
# 5. Fuzzy Matching Invariants
# ===================================================================
class TestFuzzyMatchInvariants:
    """Property-based tests for cached fuzzy matching functions."""

    SEED = 400

    def test_fuzz_ratio_range(self):
        """cached_fuzz_ratio always returns int in [0, 100]."""
        rng = random.Random(self.SEED)
        for _ in range(100):
            a = _rand_ascii(rng, max_len=30)
            b = _rand_ascii(rng, max_len=30)
            result = cached_fuzz_ratio(a, b)
            assert isinstance(result, int)
            assert 0 <= result <= 100, f"Out of range: {result}"

    def test_fuzz_ratio_identity(self):
        """cached_fuzz_ratio(s, s) == 100 for any non-empty string."""
        rng = random.Random(self.SEED + 1)
        for _ in range(50):
            s = _rand_ascii(rng, min_len=1, max_len=40)
            result = cached_fuzz_ratio(s, s)
            assert result == 100, f"Self-comparison != 100: {result} for {s!r}"

    def test_fuzz_ratio_deterministic(self):
        """cached_fuzz_ratio(a, b) is deterministic."""
        rng = random.Random(self.SEED + 2)
        for _ in range(50):
            a = _rand_ascii(rng, max_len=30)
            b = _rand_ascii(rng, max_len=30)
            r1 = cached_fuzz_ratio(a, b)
            r2 = cached_fuzz_ratio(a, b)
            assert r1 == r2, "Non-deterministic result"

    def test_fuzz_ratio_empty_strings(self):
        """cached_fuzz_ratio('', '') returns a valid result."""
        result = cached_fuzz_ratio("", "")
        assert isinstance(result, int)
        assert 0 <= result <= 100

    def test_token_sort_ratio_range(self):
        """cached_token_sort_ratio always in [0, 100]."""
        rng = random.Random(self.SEED + 3)
        for _ in range(100):
            a = _rand_ascii(rng, max_len=30)
            b = _rand_ascii(rng, max_len=30)
            result = cached_token_sort_ratio(a, b)
            assert isinstance(result, int)
            assert 0 <= result <= 100

    def test_token_sort_ratio_identity(self):
        """cached_token_sort_ratio(s, s) == 100 for non-empty string."""
        rng = random.Random(self.SEED + 4)
        for _ in range(50):
            s = _rand_ascii(rng, min_len=1, max_len=40)
            result = cached_token_sort_ratio(s, s)
            assert result == 100


# ===================================================================
# 6. Database Round-Trip Integrity
# ===================================================================
class TestDatabaseRoundTrip:
    """Property-based tests for database save/load round-trips."""

    SEED = 500

    def test_history_roundtrip(self, db_manager):
        """For 100 random titles/URLs: add -> is_in_history returns True."""
        rng = random.Random(self.SEED)
        urls = []
        for _ in range(100):
            url = f"https://example.com/{rng.randint(1, 999999)}/{_rand_ascii(rng, 5, 20)}"
            title = _rand_title(rng)
            db_manager.add_to_history(url, title)
            urls.append(url)

        for url in urls:
            assert db_manager.is_in_history(url), f"URL not found: {url}"

    def test_plex_cache_roundtrip(self, db_manager):
        """Plex cache items survive a save -> load cycle with fields intact."""
        rng = random.Random(self.SEED + 1)
        items = []
        for i in range(30):
            item = {
                "key": f"movie_{i}",
                "clean_title": _rand_title(rng).lower(),
                "original_title": _rand_title(rng),
                "year": rng.randint(1950, 2026),
                "res": _rand_resolution(rng) or "1080p",
                "size": round(rng.uniform(0.5, 100.0), 2),
                "imdb_id": f"tt{rng.randint(1000000, 9999999)}",
                "rating_key": str(rng.randint(1000, 9999)),
                "media_id": f"m{rng.randint(1000, 9999)}",
                "dovi": rng.choice([True, False]),
                "hdr": rng.choice([True, False]),
            }
            items.append(item)

        db_manager.save_plex_cache(items, "Movies")
        loaded = db_manager.load_plex_cache("Movies")

        assert len(loaded) == len(items)

        loaded_by_key = {it["key"]: it for it in loaded}
        for orig in items:
            loaded_item = loaded_by_key.get(orig["key"])
            assert loaded_item is not None, f"Missing key: {orig['key']}"
            assert loaded_item["clean_title"] == orig["clean_title"]
            assert loaded_item["year"] == orig["year"]
            assert loaded_item["res"] == orig["res"]
            assert abs(loaded_item["size"] - orig["size"]) < 0.01
            assert loaded_item["imdb_id"] == orig["imdb_id"]
            assert loaded_item["dovi"] == orig["dovi"]
            assert loaded_item["hdr"] == orig["hdr"]

    def test_scan_history_roundtrip(self, db_manager):
        """Scan history entries survive save -> retrieve."""
        rng = random.Random(self.SEED + 2)
        for i in range(20):
            scan_data = {
                "timestamp": f"2025-01-{rng.randint(1,28):02d}T12:00:00",
                "scan_type": rng.choice(["Full Scan", "Quick Scan", "Incremental"]),
                "items_scanned": rng.randint(0, 5000),
                "missing_count": rng.randint(0, 500),
                "upgrade_count": rng.randint(0, 200),
                "dv_upgrade_count": rng.randint(0, 50),
                "in_library_count": rng.randint(0, 3000),
                "duration_seconds": round(rng.uniform(1.0, 600.0), 2),
                "sources_scanned": rng.choice(["src1", "src1,src2", ""]),
                "plex_items_cached": rng.randint(0, 10000),
            }
            db_manager.save_scan_history(scan_data)

        history = db_manager.get_scan_history(limit=100)
        assert len(history) == 20
        for entry in history:
            assert "timestamp" in entry
            assert "scan_type" in entry
            assert entry["items_scanned"] >= 0

    def test_watchlist_roundtrip(self, tmp_db):
        """Watchlist items with random data: add -> get -> verify fields match."""
        rng = random.Random(self.SEED + 3)
        wm = WatchlistManager(db_path=tmp_db)
        try:
            created_items = []
            for _ in range(30):
                item = WatchlistItem(
                    title=_rand_title(rng),
                    year=rng.randint(1980, 2026) if rng.random() > 0.2 else None,
                    imdb_id=None,  # avoid duplicate detection
                    item_type=rng.choice(list(WatchlistItemType)),
                    status=WatchlistItemStatus.WANTED,
                    season=rng.randint(1, 10) if rng.random() > 0.5 else None,
                    min_resolution=rng.choice([None, "720p", "1080p", "4K"]),
                    prefer_dovi=rng.choice([True, False]),
                    notes=_rand_ascii(rng, 0, 30),
                    priority=rng.choice([1, 2, 3]),
                )
                item_id = wm.add(item)
                item.id = item_id
                created_items.append(item)

            for orig in created_items:
                loaded = wm.get(orig.id)
                assert loaded is not None, f"Watchlist item {orig.id} not found"
                assert loaded.title == orig.title
                assert loaded.year == orig.year
                assert loaded.item_type == orig.item_type
                assert loaded.status == orig.status
                assert loaded.season == orig.season
                assert loaded.min_resolution == orig.min_resolution
                assert loaded.prefer_dovi == orig.prefer_dovi
                assert loaded.priority == orig.priority
        finally:
            wm.close()


# ===================================================================
# 7. Matching Never Crashes
# ===================================================================
class TestMatchingNeverCrashes:
    """Property-based tests ensuring the matching engine never crashes."""

    SEED = 600

    @staticmethod
    def _make_mock_app(rng):
        """Create a MockApp with randomized config."""
        from tests.conftest import MockApp

        cfg = get_default_config()
        cfg["upgrade_sensitivity"] = rng.randint(0, 50)
        cfg["movie_match_threshold"] = rng.randint(50, 100)
        cfg["tv_match_threshold"] = rng.randint(50, 100)
        cfg["low_match_threshold"] = rng.randint(40, 90)
        cfg["year_tolerance"] = rng.randint(0, 5)
        cfg["rule_1080_4k"] = rng.choice([True, False])
        cfg["rule_1080_1080"] = rng.choice([True, False])
        cfg["rule_4k_4k"] = rng.choice([True, False])
        cfg["rule_dv"] = rng.choice([True, False])
        cfg["strict_resolution"] = rng.choice([True, False])
        return MockApp(config=cfg)

    @staticmethod
    def _make_web_item(rng):
        """Generate a random web item dict."""
        return {
            "display_title": _rand_title(rng),
            "year": rng.randint(1950, 2026),
            "res": _rand_resolution(rng) or "1080p",
            "size": _rand_size_str(rng),
            "dovi": rng.choice([True, False]),
            "hdr": rng.choice(["HDR10", "DV", "", "HDR10+"]),
            "url": f"https://example.com/{rng.randint(1, 999999)}",
            "imdb_id": f"tt{rng.randint(1000000, 9999999)}" if rng.random() > 0.3 else None,
            "is_tv": False,
            "search_key": _rand_title(rng).lower(),
        }

    @staticmethod
    def _make_plex_item(rng):
        """Generate a random plex match item."""
        return {
            "clean_title": _rand_title(rng).lower(),
            "original_title": _rand_title(rng),
            "year": rng.randint(1950, 2026),
            "res": _rand_resolution(rng) or "1080p",
            "size": round(rng.uniform(0.5, 100.0), 2),
            "dovi": rng.choice([True, False]),
            "hdr": rng.choice([True, False]),
            "imdb_id": f"tt{rng.randint(1000000, 9999999)}",
            "rating_key": str(rng.randint(1000, 9999)),
            "media_id": f"m{rng.randint(1000, 9999)}",
        }

    @staticmethod
    def _make_plex_tv_item(rng):
        """Generate a random TV plex item."""
        item = TestMatchingNeverCrashes._make_plex_item(rng)
        item["season"] = rng.randint(1, 10)
        item["episode_count"] = rng.randint(1, 24)
        item["is_tv"] = True
        return item

    def test_calculate_movie_upgrade_status_never_raises(self):
        """calculate_movie_upgrade_status never raises on random inputs."""
        rng = random.Random(self.SEED)
        for _ in range(200):
            app = self._make_mock_app(rng)
            engine = MatchingEngine(app)
            web = self._make_web_item(rng)
            matches = [self._make_plex_item(rng) for _ in range(rng.randint(1, 5))]
            # Must not raise
            status, color, info, plex_id = engine.calculate_movie_upgrade_status(web, matches)
            assert isinstance(status, str) and len(status) > 0
            assert isinstance(color, str) and color.startswith("#")
            assert isinstance(info, str)

    def test_calculate_tv_upgrade_status_never_raises(self):
        """calculate_tv_upgrade_status never raises on random inputs."""
        rng = random.Random(self.SEED + 1)
        for _ in range(200):
            app = self._make_mock_app(rng)
            engine = MatchingEngine(app)
            web = self._make_web_item(rng)
            web["is_tv"] = True
            web["season"] = rng.randint(1, 10)
            web["episodes"] = rng.randint(1, 24)
            match = self._make_plex_tv_item(rng)
            # Must not raise
            status, color, info, is_upgrade = engine.calculate_tv_upgrade_status(web, match)
            assert isinstance(status, str) and len(status) > 0
            assert isinstance(color, str) and color.startswith("#")
            assert isinstance(info, str)
            assert isinstance(is_upgrade, bool)

    def test_find_movie_matches_never_raises(self):
        """find_movie_matches never raises on random plex index."""
        rng = random.Random(self.SEED + 2)
        for _ in range(50):
            app = self._make_mock_app(rng)
            engine = MatchingEngine(app)
            web = self._make_web_item(rng)

            # Build a small random plex index
            items = [self._make_plex_item(rng) for _ in range(rng.randint(0, 20))]
            by_imdb = {}
            by_title = {}
            for item in items:
                imdb = item.get("imdb_id")
                if imdb:
                    by_imdb.setdefault(imdb, []).append(item)
                title = item.get("clean_title", "")
                if title:
                    by_title.setdefault(title, []).append(item)

            plex_index = {
                "by_imdb": by_imdb,
                "by_title": by_title,
                "all_items": items,
            }

            # Must not raise
            matches, uncertain = engine.find_movie_matches(web, plex_index)
            assert isinstance(matches, list)
            assert isinstance(uncertain, bool)

    def test_status_always_non_empty(self):
        """Status string from movie upgrade calculation is always non-empty."""
        rng = random.Random(self.SEED + 3)
        for _ in range(50):
            app = self._make_mock_app(rng)
            engine = MatchingEngine(app)
            web = self._make_web_item(rng)
            matches = [self._make_plex_item(rng)]
            status, color, info, _ = engine.calculate_movie_upgrade_status(web, matches)
            assert len(status) > 0, "Empty status string"

    def test_color_is_valid_hex(self):
        """Color is always a valid hex string (#rrggbb)."""
        rng = random.Random(self.SEED + 4)
        hex_pattern = re.compile(r"^#[0-9a-fA-F]{6}$")
        for _ in range(50):
            app = self._make_mock_app(rng)
            engine = MatchingEngine(app)
            web = self._make_web_item(rng)
            matches = [self._make_plex_item(rng)]
            status, color, info, _ = engine.calculate_movie_upgrade_status(web, matches)
            assert hex_pattern.match(color), f"Invalid hex color: {color!r}"


# ===================================================================
# 8. PathMapping Properties
# ===================================================================
class TestPathMappingProperties:
    """Property-based tests for PathMapping.translate()."""

    SEED = 700

    def test_translate_always_returns_string(self):
        """translate(path) always returns a string."""
        rng = random.Random(self.SEED)
        for _ in range(100):
            plex_path = f"/media/{_rand_ascii(rng, 3, 15)}"
            local_path = f"/mnt/{_rand_ascii(rng, 3, 15)}"
            mapping = PathMapping(plex_path=plex_path, local_path=local_path)
            test_path = _rand_ascii(rng, 5, 50)
            result = mapping.translate(test_path)
            assert isinstance(result, str)

    def test_matching_prefix_translated(self):
        """If path starts with plex_path, result starts with local_path."""
        rng = random.Random(self.SEED + 1)
        for _ in range(100):
            plex_path = f"/plex/{rng.randint(1, 999)}"
            local_path = f"/local/{rng.randint(1, 999)}"
            suffix = f"/movies/{_rand_ascii(rng, 3, 20)}.mkv"
            mapping = PathMapping(plex_path=plex_path, local_path=local_path)
            result = mapping.translate(plex_path + suffix)
            assert result.startswith(local_path), (
                f"Expected prefix {local_path!r} in {result!r}"
            )

    def test_non_matching_prefix_unchanged(self):
        """If path doesn't start with plex_path, translate returns it unchanged."""
        rng = random.Random(self.SEED + 2)
        for _ in range(100):
            plex_path = "/plex/data"
            local_path = "/local/data"
            mapping = PathMapping(plex_path=plex_path, local_path=local_path)
            other_path = f"/other/{_rand_ascii(rng, 3, 30)}"
            result = mapping.translate(other_path)
            assert result == other_path

    def test_disabled_mapping_returns_original(self):
        """For disabled mapping, translate always returns the original path."""
        rng = random.Random(self.SEED + 3)
        for _ in range(100):
            plex_path = "/plex/media"
            local_path = "/local/media"
            mapping = PathMapping(
                plex_path=plex_path,
                local_path=local_path,
                enabled=False,
            )
            test_path = plex_path + f"/{_rand_ascii(rng, 3, 20)}.mkv"
            result = mapping.translate(test_path)
            assert result == test_path, (
                f"Disabled mapping should not translate: {result!r} != {test_path!r}"
            )


# ===================================================================
# 9. normalize_title Properties (bonus)
# ===================================================================
class TestNormalizeTitleProperties:
    """Additional property-based tests for normalize_title."""

    SEED = 800

    def test_idempotent(self):
        """normalize_title is idempotent."""
        rng = random.Random(self.SEED)
        for _ in range(100):
            s = _rand_str(rng)
            once = normalize_title(s)
            twice = normalize_title(once)
            assert once == twice

    def test_agrees_with_clean_string(self):
        """normalize_title and clean_string produce the same output."""
        rng = random.Random(self.SEED + 1)
        for _ in range(100):
            s = _rand_str(rng)
            assert normalize_title(s) == clean_string(s)


# ===================================================================
# 10. ParsedRelease Properties (bonus)
# ===================================================================
class TestParsedReleaseProperties:
    """Property-based tests for ParsedRelease dataclass invariants."""

    SEED = 900

    def test_to_dict_never_raises(self):
        """to_dict() never raises for random inputs."""
        from backend.sources.base import ParsedRelease

        rng = random.Random(self.SEED)
        for _ in range(50):
            release = ParsedRelease(
                title=_rand_title(rng),
                url=f"https://example.com/{rng.randint(1, 999999)}",
                source=rng.choice(["source_a", "source_b", "source_c"]),
                display_title=_rand_title(rng),
                year=rng.randint(1950, 2026),
                resolution=_rand_resolution(rng),
                size=_rand_size_str(rng),
                is_hdr=rng.choice([True, False]),
                is_dovi=rng.choice([True, False]),
                is_tv=rng.choice([True, False]),
                season=rng.randint(1, 10) if rng.random() > 0.5 else None,
                episode=rng.randint(1, 24) if rng.random() > 0.5 else None,
            )
            d = release.to_dict()
            assert isinstance(d, dict)
            assert "display_title" in d
            assert "url" in d

    def test_search_key_auto_generated(self):
        """If search_key not provided, it is auto-generated and non-empty for non-empty titles."""
        from backend.sources.base import ParsedRelease

        rng = random.Random(self.SEED + 1)
        for _ in range(50):
            title = _rand_title(rng)
            release = ParsedRelease(
                title=title,
                url="https://example.com/1",
                source="test",
            )
            if title.strip():
                assert len(release.search_key) > 0

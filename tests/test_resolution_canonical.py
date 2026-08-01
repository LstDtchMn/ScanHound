"""Resolution facet canonicalisation.

Regression cover for the 2026-07-30 finding: UHD is stored under two spellings
('4K' and '2160p') and every filter compared the raw string, so the '4K' chip
could not match a '2160p' row. Measured on the production DB that day: 153
movies stored '4K' against 242 stored '2160p', i.e. 61% of 4K movies were
unreachable through the 4K facet. filename_utils normalises 4k/uhd to '2160p'
on parse, so the unreachable share grew with every new release.

The test that mattered is test_4k_facet_matches_both_spellings: the previous
suite asserted filtering behaviour only with '4K'-spelled fixtures, which is
precisely why a bug this size stayed green.
"""

import pytest

from backend.api.routes.results import _resolution_keys, canonical_resolution


class TestCanonicalResolution:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2160p", "4K"),
            ("4K", "4K"),
            ("4k", "4K"),
            ("UHD", "4K"),
            ("uhd", "4K"),
            ("1080p", "1080p"),
            ("1080i", "1080p"),
            ("720p", "720p"),
            ("480p", "480p"),
        ],
    )
    def test_known_spellings_fold_to_one_key(self, raw, expected):
        assert canonical_resolution(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", 0])
    def test_absent_resolution_is_none(self, raw):
        assert canonical_resolution(raw) is None

    def test_unknown_spelling_passes_through_unchanged(self):
        """An unrecognised value must stay filterable by its own exact value.
        Mapping it to None would reintroduce the original defect in a new
        form: silently unfilterable items."""
        assert canonical_resolution("1440p") == "1440p"
        assert canonical_resolution("?") == "?"

    def test_surrounding_whitespace_and_case_do_not_matter(self):
        assert canonical_resolution("  2160P  ") == "4K"


class TestResolutionKeys:
    def test_movie_keys_by_canonical_resolution(self):
        assert _resolution_keys({"resolution": "2160p"}) == {"4K"}
        assert _resolution_keys({"resolution": "4K"}) == {"4K"}

    def test_4k_facet_matches_both_spellings(self):
        """THE REGRESSION. Selecting the '4K' chip must return the '2160p' row.
        Before the fix the second assertion failed and 61% of 4K movies were
        invisible whenever any resolution filter was active."""
        rset = {"4K"}
        assert _resolution_keys({"resolution": "4K"}) & rset
        assert _resolution_keys({"resolution": "2160p"}) & rset

    def test_1080p_was_never_affected(self):
        """Documents why this survived: parser and chip agree on '1080p', so
        the only widely-used facet that worked kept working."""
        assert _resolution_keys({"resolution": "1080p"}) & {"1080p"}

    def test_1080p_chip_does_not_match_uhd(self):
        """Canonicalisation must not over-merge — the whole point is one key
        per real resolution, not one key for everything."""
        assert not (_resolution_keys({"resolution": "2160p"}) & {"1080p"})

    def test_tv_still_keys_only_as_tv(self):
        """Unchanged by this fix, and deliberately so: TV keys as 'TV' whatever
        its resolution, which is why the 4K/1080p chips are movies-only. This
        is a separate design decision, NOT part of this defect."""
        assert _resolution_keys({"category": "tv", "resolution": "2160p"}) == {"TV"}
        assert _resolution_keys({"season": 1, "resolution": "1080p"}) == {"TV"}

    def test_missing_resolution_yields_no_keys(self):
        assert _resolution_keys({"resolution": None}) == set()
        assert _resolution_keys({}) == set()

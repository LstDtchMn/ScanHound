"""The deployed HDEncode listing path does NOT use the shared grammar.

Written 2026-08-02 after external review found that
``tests/test_discovery_parity.py`` proves parity against the wrong reader.

WHAT HAPPENED. The discovery-parity work fixed six divergences between
``hdencode_feed_parser`` (RSS) and ``SourceBase.extract_*`` (listing) and
asserted the two now agree. They do. But **``SourceBase`` is not the listing
reader the deployed HDEncode scan uses.** The live chain is:

    POST /scan/start  ->  ScannerService.run_scan()
    BackgroundScanner._scan_source()  ->  ScannerService.run_scan()
        ScannerService._crawl_pages()   collects post URLs
        DetailScraper.scrape_details()  produces `is_tv` from the FILENAME
        ScannerService._process_posts() combines it with the source hint

Nothing in that chain constructs ``HDEncodeSource`` or calls
``SourceBase.parse_release()``. So the earlier harness replaced a test-only
transcription of the listing reader with a *different* non-production reader —
the same defect in a new costume, which is why this file asserts the structural
facts directly rather than re-deriving behaviour from copied regexes.

CONSEQUENCE. Divergence (f) — ``Complete Series`` / ``Mini Series`` /
``TV Series`` / ``Season 4`` classified TV by RSS and movie by listing — is
fixed between RSS and ``SourceBase`` and is **not** fixed on the deployed path.
Nor is the ``S104`` ambiguity rule.

Every test here is ``xfail(strict=True)``: each asserts the DESIRED end state,
fails today because that state does not hold, and turns the suite RED the moment
someone wires the active path up — which is the signal to delete the marker
rather than let this decay into folklore.
"""

import inspect

import pytest


def _source_of(module_name: str) -> str:
    module = __import__(module_name, fromlist=["_"])
    return inspect.getsource(module)


# ───────────────────────── CLOSED 2026-08-03 (R-3) ─────────────────────────
# The four DetailScraper-gap tests below were xfail(strict=True) and began
# XPASSing when the live detail-parser seam was unified: DetailScraper now
# delegates season/episode, year, size and resolution to release_grammar
# (divergences (b)(d)(e) died here in their third copy). They are now
# ordinary assertions guarding the delegation — same precedent as the two
# scanner-path tests further down.

def test_detail_scraper_uses_the_shared_grammar():
    assert "release_grammar" in _source_of("backend.detail_scraper")


def test_detail_scraper_sizes_go_through_the_shared_grammar():
    """Terabytes included — divergence (e). The grammar owns the units; the
    scraper keeps only the pick-the-largest selection."""
    source = _source_of("backend.detail_scraper")
    assert "find_all_sizes" in source
    assert "GiB|GB|MiB|MB" not in source, (
        "a local size-unit alternation crept back into DetailScraper")


def test_detail_scraper_resolution_vocabulary_is_the_shared_grammar():
    """720p/4K/UHD come from the shared vocabulary; an explicit WxH converts
    only through the grammar's named dimension bridge — a dimension is never
    itself a resolution."""
    source = _source_of("backend.detail_scraper")
    assert "find_resolution" in source
    assert "resolution_from_dimensions" in source
    assert "2160p|1080p" not in source, (
        "a local resolution alternation crept back into DetailScraper")


def test_detail_scraper_season_width_is_the_shared_grammar():
    """S104 is 'cannot tell', never season 10 — the grammar's ambiguity
    concept replaced the local two-digit cap."""
    source = _source_of("backend.detail_scraper")
    assert "parse_season_episode" in source
    assert r"S(\d{1,2})" not in source, (
        "the two-digit season cap crept back into DetailScraper")


# ───────────────────────── CLOSED 2026-08-02 ───────────────────────────────
# These two were xfail(strict=True) and began XPASSing when the active scanner
# path was wired up, which is exactly what strict xfail is for. They are now
# ordinary assertions guarding against regression.

def test_scanner_service_uses_the_shared_grammar():
    """_process_posts resolves media type through release_grammar's authority
    lattice instead of `details['is_tv'] or post_info['type'] == 'tv'`."""
    source = _source_of("backend.scanner_service")
    assert "release_grammar" in source
    assert "resolve_media_type" in source


def test_the_ordinary_post_record_carries_the_listing_title():
    """The listing title survives _crawl_pages. It used to be read, used for
    the full-disc check, and dropped — so no title-derived rule could run
    downstream."""
    source = _source_of("backend.scanner_service")
    # Merge 2026-08-28: the record is now built as a named `_post` dict (so
    # main's conflict-marking can index it) before being appended; the marker
    # follows the dict literal, the guarantee is unchanged.
    marker = "_post = {'url': post_url"
    start = source.index(marker)
    record = source[start:source.index("}", start)]
    assert "'title': post_title" in record
    assert "all_posts.append(_post)" in source


def test_the_listing_route_is_not_allowed_to_outrank_the_title():
    """The route enters at ROUTE authority, the weakest level. If it were ever
    raised, a movies-category page could overrule a 'Complete Series' title —
    which is divergence (f) restored."""
    source = _source_of("backend.scanner_service")
    assert "Authority.ROUTE, 'listing-route'" in source, (
        "the listing crawl route must enter the resolver at ROUTE authority")


# ─────────────── the fact that makes the parity harness misaimed ────────────

def test_nothing_in_the_scan_chain_constructs_the_SourceBase_reader():
    """NOT an xfail — this is true today and is the finding itself.

    It is asserted so that if someone later wires ``HDEncodeSource`` into the
    scan chain, this test fails and forces a decision about which reader is
    authoritative, rather than leaving two live listing readers.
    """
    for module_name in ("backend.scanner_service", "backend.background_scanner"):
        source = _source_of(module_name)
        assert "HDEncodeSource" not in source, (
            f"{module_name} now references HDEncodeSource; the deployed listing "
            "reader has changed and test_discovery_parity's target must be "
            "reconsidered")


def test_the_parity_harness_target_is_documented_as_not_the_deployed_reader():
    """Guard against the docstring quietly reverting to the old claim.

    The parity harness is still worth having — it covers the RSS reader, which
    IS the promoted path — but its own file must not claim it proves parity for
    the deployed listing scan.
    """
    source = _source_of("tests.test_discovery_parity")
    assert "not the listing reader" in source or "deployed" in source, (
        "test_discovery_parity must state which listing reader it targets and "
        "that it is not the deployed HDEncode scan path")

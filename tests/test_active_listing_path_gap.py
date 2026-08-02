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


# ───────────────── the active media-type producer is independent ────────────

@pytest.mark.xfail(strict=True, reason=(
    "DetailScraper produces `is_tv` for the deployed listing scan from its own "
    "filename regexes and does not reference release_grammar. Until it does, "
    "the shared TV-title rule is absent from the path being qualified."))
def test_detail_scraper_uses_the_shared_grammar():
    assert "release_grammar" in _source_of("backend.detail_scraper")


@pytest.mark.xfail(strict=True, reason=(
    "ScannerService._process_posts derives media type as "
    "`details.get('is_tv') or post_info['type'] == 'tv'` — DetailScraper's "
    "filename verdict OR the source descriptor hint. The listing TITLE is not "
    "consulted, so title_indicates_tv() cannot participate."))
def test_scanner_service_uses_the_shared_grammar():
    assert "release_grammar" in _source_of("backend.scanner_service")


@pytest.mark.xfail(strict=True, reason=(
    "_crawl_pages reads the listing title into post_title, uses it only for the "
    "full-disc check, and appends "
    "{'url', 'type', 'source', 'category'} — no title. A title-derived rule "
    "cannot run downstream because the title is gone by then."))
def test_the_ordinary_post_record_carries_the_listing_title():
    source = _source_of("backend.scanner_service")
    marker = "all_posts.append({'url': post_url"
    start = source.index(marker)
    record = source[start:source.index("}", start)]
    assert "post_title" in record or "'title'" in record


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

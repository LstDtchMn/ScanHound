"""MetadataEnricher — TMDB/OMDb/RT metadata enrichment for scan results.

Extracted from ScannerService._enrich_metadata_async to keep the scan
engine focused on crawling and matching.

Enriches a list of MediaItem objects in-place using:
    1. TMDB — descriptions, posters, canonical titles, year, IMDb ID.
    2. OMDb — IMDb rating, vote count, RT score (when API key configured).
    3. IMDb scraping — rating/votes fallback when OMDb returns N/A.
    4. RT scraping — critics score fallback when OMDb has no RT entry.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from thefuzz import fuzz

from backend.app_service import TMDB_GENRE_MAP, TMDB_LANGUAGE_MAP
from backend.tmdb_client import TmdbClient

logger = logging.getLogger(__name__)

# Minimum token_sort_ratio required to accept a TMDB search result.
_TMDB_MATCH_THRESHOLD = 80


def _best_tmdb_result(query_title: str, results: list) -> dict | None:
    """Pick the TMDB result whose title best matches *query_title*.

    Iterates through all results, computing token_sort_ratio against both
    the English and original title fields.  Returns the result with the
    highest score, provided it meets ``_TMDB_MATCH_THRESHOLD``.  Returns
    ``None`` if no result is a good-enough match.
    """
    if not results:
        return None

    query_lower = query_title.lower().strip()
    best, best_score = None, 0

    for r in results:
        eng = (r.get("title") or r.get("name") or "").lower().strip()
        orig = (r.get("original_title") or r.get("original_name") or "").lower().strip()
        score = max(
            fuzz.token_sort_ratio(query_lower, eng) if eng else 0,
            fuzz.token_sort_ratio(query_lower, orig) if orig else 0,
        )
        if score > best_score:
            best, best_score = r, score

    if best_score >= _TMDB_MATCH_THRESHOLD:
        return best

    logger.debug(
        "No TMDB result matched '%s' well enough (best score: %d)",
        query_title, best_score,
    )
    return None


class MetadataEnricher:
    """Enriches MediaItem objects with TMDB, OMDb, IMDb, and RT metadata.

    Designed to run after the main scan/match phase.  All network I/O
    happens in a thread pool (default 4 workers) so the async event loop
    is not blocked.

    Requires:
        config   – dict-like config object with TMDB/OMDb/RT settings.
        scrapers – WebScrapers instance (for IMDb/RT fallback scraping).
        omdb_cache – LRUCache shared with ScannerService.
    """

    def __init__(self, config, scrapers, omdb_cache):
        """Initialize the enricher.

        Args:
            config:     Application config dict.
            scrapers:   WebScrapers instance.
            omdb_cache: Shared LRUCache for OMDb responses.
        """
        self.config = config
        self.scrapers = scrapers
        self.omdb_cache = omdb_cache

    async def enrich(self, items, stop_flag_fn=None, progress_fn=None, log_fn=None):
        """Enrich a list of MediaItems in-place with external metadata.

        Skips items that already have a description.  Runs workers in a
        ThreadPoolExecutor (4 threads) so multiple API calls proceed
        concurrently.

        Args:
            items:        List of MediaItem objects to enrich.
            stop_flag_fn: Callable returning True when the scan should abort.
            progress_fn:  Optional callable(fraction, label) for progress updates.
            log_fn:       Optional callable(message) for status log messages.
        """
        api_key = self.config.get("tmdb_api_key", "")
        if not api_key:
            return

        if log_fn:
            log_fn("Enriching metadata from TMDB...")

        items_to_enrich = [i for i in items if not i.description or not i.poster_path]
        if not items_to_enrich:
            return

        total = len(items_to_enrich)
        processed = 0
        tmdb = TmdbClient(api_key, timeout=10)

        def fetch_metadata(item):
            try:
                result_data = None

                if item.imdb_id:
                    data = tmdb.find(item.imdb_id)
                    if data:
                        results = data.get("movie_results", []) + data.get("tv_results", [])
                        if results:
                            result_data = results[0]

                if not result_data:
                    search_type = "tv" if item.season is not None else "movie"
                    results = tmdb.search(item.title, media_type=search_type, year=item.year)
                    if results:
                        result_data = _best_tmdb_result(item.title, results)

                # Colon-subtitle fallback: e.g. "A Knight of the Seven Kingdoms: The Hedge Knight"
                # → retry with "A Knight of the Seven Kingdoms" if the full title didn't match.
                if not result_data and ':' in item.title:
                    base_title = item.title.split(':', 1)[0].strip()
                    if base_title:
                        results = tmdb.search(base_title, media_type=search_type, year=item.year)
                        if results:
                            result_data = _best_tmdb_result(base_title, results)

                if result_data:
                    if not item.description:
                        item.description = result_data.get("overview", "")
                    if not item.poster_path:
                        item.poster_path = result_data.get("poster_path")

                    eng_title = result_data.get("title") or result_data.get("name", "")
                    orig_title = result_data.get("original_title") or result_data.get("original_name", "")
                    if eng_title and orig_title and eng_title.lower() != orig_title.lower():
                        item.title = f"{eng_title} ({orig_title})"
                    elif eng_title:
                        item.title = eng_title

                    if not item.year or item.year == 0:
                        date_str = result_data.get("release_date") or result_data.get("first_air_date", "")
                        if date_str and len(date_str) >= 4:
                            try:
                                item.year = int(date_str[:4])
                            except ValueError:
                                pass

                    if not item.imdb_id and result_data.get("id"):
                        tmdb_id = result_data["id"]
                        try:
                            ext_type = "tv" if item.season is not None else "movie"
                            if item.season is not None:
                                ext_data = tmdb.external_ids(tmdb_id, media_type=ext_type)
                            else:
                                ext_data = tmdb.details(tmdb_id, media_type=ext_type)
                            if ext_data:
                                item.imdb_id = ext_data.get("imdb_id")
                        except Exception as e:
                            logger.debug("TMDB external_ids failed for %s: %s", item.title, e)

                    # OMDb ratings
                    omdb_key = self.config.get("omdb_api_key", "")
                    omdb_ok = False
                    if omdb_key and item.imdb_id:
                        try:
                            cached = self.omdb_cache.get(item.imdb_id)
                            if cached:
                                omdb_data = cached
                            else:
                                omdb_resp = requests.get(
                                    "https://www.omdbapi.com/",
                                    params={"apikey": omdb_key, "i": item.imdb_id},
                                    timeout=10,
                                )
                                omdb_data = omdb_resp.json() if omdb_resp.status_code == 200 else None
                                if omdb_data and omdb_data.get("Response") == "True":
                                    self.omdb_cache[item.imdb_id] = omdb_data
                                else:
                                    omdb_data = None
                            if omdb_data:
                                imdb_rating = omdb_data.get("imdbRating", "N/A")
                                if imdb_rating != "N/A":
                                    item.rating = float(imdb_rating)
                                imdb_votes = omdb_data.get("imdbVotes", "N/A")
                                if imdb_votes != "N/A":
                                    item.votes = int(imdb_votes.replace(",", ""))
                                    item.votes_source = "imdb"
                                    omdb_ok = True
                                if item.rt_score is None:
                                    for r in omdb_data.get("Ratings", []):
                                        if r.get("Source") == "Rotten Tomatoes":
                                            try:
                                                item.rt_score = int(r["Value"].replace("%", ""))
                                            except (ValueError, KeyError):
                                                pass
                                            break
                        except Exception as e:
                            logger.debug("OMDb fetch failed for %s: %s", item.title, e)

                    # IMDb direct scraping fallback
                    if not omdb_ok and item.imdb_id:
                        try:
                            imdb_data = self.scrapers.scrape_imdb_data(item.imdb_id)
                            if imdb_data:
                                if imdb_data.get("rating", 0) > 0:
                                    item.rating = imdb_data["rating"]
                                if imdb_data.get("votes", 0) > 0:
                                    item.votes = imdb_data["votes"]
                                    item.votes_source = "imdb"
                        except Exception as e:
                            logger.debug("IMDb scrape failed for %s: %s", item.imdb_id, e)

                    # TMDB vote fallback
                    if item.rating == 0 and result_data.get("vote_average"):
                        item.rating = float(result_data["vote_average"])
                    if item.votes == 0 and result_data.get("vote_count"):
                        item.votes = int(result_data["vote_count"])
                        item.votes_source = "tmdb"

                    if not item.genres and result_data.get("genre_ids"):
                        item.genres = [TMDB_GENRE_MAP.get(gid, "") for gid in result_data["genre_ids"] if gid in TMDB_GENRE_MAP]

                    if not item.language and result_data.get("original_language"):
                        lang_code = result_data["original_language"]
                        item.language = TMDB_LANGUAGE_MAP.get(lang_code, lang_code.upper())

                    # RT score scraping fallback
                    rt_title = eng_title or item.title if result_data else item.title
                    if item.rt_score is None and self.config.get("show_rt", True):
                        try:
                            rt_result = self.scrapers.scrape_rt_score(rt_title, item.year, is_tv=bool(item.season))
                            if rt_result.get("critics") is not None:
                                item.rt_score = rt_result["critics"]
                        except Exception as e:
                            logger.debug("RT scrape failed for %s: %s", item.title, e)

            except Exception as e:
                logger.warning("Metadata enrichment failed for %s: %s", item.title, e)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(fetch_metadata, item) for item in items_to_enrich]
            for future in as_completed(futures):
                if stop_flag_fn and stop_flag_fn():
                    break
                try:
                    future.result()
                except Exception as e:
                    logger.debug("Metadata worker error: %s", e)
                processed += 1
                if progress_fn:
                    progress_fn(processed / total, f"Enriching {processed}/{total}")

        imdb_count = sum(1 for i in items_to_enrich if i.votes_source == "imdb")
        tmdb_count = sum(1 for i in items_to_enrich if i.votes_source == "tmdb")
        if log_fn:
            log_fn(f"Metadata enrichment complete: {processed}/{total} items "
                   f"(ratings: {imdb_count} IMDb, {tmdb_count} TMDB fallback)")

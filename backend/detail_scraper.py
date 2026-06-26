"""DetailScraper — HDEncode post detail scraping.

Provides the DetailScraper class used by WebScrapers to extract structured
media metadata from HDEncode (and compatible) post pages.  All methods are
synchronous (blocking) and designed to run in thread-pool executors.
"""

import logging
import re
import time
from typing import Optional

import cloudscraper
from bs4 import BeautifulSoup

from backend.models import ScrapeResult

logger = logging.getLogger(__name__)


class DetailScraper:
    """Scrapes media metadata from HDEncode post pages."""

    def __init__(self, parent_app):
        """Initialize with reference to the parent app (config, helpers, logging).

        Args:
            parent_app: AppService instance (provides config, parse_size,
                        clean_string, safe_log).
        """
        self.app = parent_app

    def scrape_details(self, url, headers, scraper=None) -> Optional[ScrapeResult]:
        """Scrape movie/TV show details from an HDEncode post page.

        Extracts filename, title, year, resolution, file size, HDR/DV flags,
        IMDb ID, and TV season/episode info from the page content.

        Handles:
            - Movies (Title.Year format)
            - TV season packs (Show.S01 format)
            - Single TV episodes (Show.S01E01 format)
            - Multi-episode packs with mirrors (deduplicates via unique eps)
            - cp437 mojibake repair on Windows-sourced filenames

        Args:
            url: HDEncode detail page URL.
            headers: HTTP headers for the request.
            scraper: Optional pre-created cloudscraper instance (avoids
                     creating a new one per call for batch processing).

        Returns:
            dict with parsed media metadata, or None on failure.
        """
        try:
            if not scraper:
                scraper = cloudscraper.create_scraper()

            # Retry logic for robust connection
            max_retries = 3
            resp = None
            last_error = None

            for attempt in range(max_retries):
                try:
                    resp = scraper.get(url, headers=headers, timeout=20)
                    if resp.status_code == 200:
                        break
                    elif resp.status_code == 429:  # Too Many Requests
                        time.sleep(2 * (attempt + 1))
                        continue
                    else:
                        time.sleep(1 * (attempt + 1))
                        continue
                except Exception as e:
                    last_error = e
                    time.sleep(1 * (attempt + 1))  # Backoff: 1s, 2s, 3s

            if not resp or resp.status_code != 200:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[Scrape Error] Failed after {max_retries} attempts: {last_error or 'Status ' + str(resp.status_code if resp else 'None')}")
                return None

            soup = BeautifulSoup(resp.content, 'html.parser', from_encoding='utf-8')

            # Narrow down text to content area to avoid sidebar/footer matches
            content_div = soup.find('div', class_='entry-content') or \
                          soup.find('div', class_='post-content') or \
                          soup.find('article') or \
                          soup.find('div', id='content') or \
                          soup
            text = content_div.get_text()

            fn_match = re.search(r'Filename\.+:\s*(.+)', text) or re.search(r'Filename\.*:\s*(.+)', text)
            if not fn_match:
                if content_div != soup:
                    text = soup.get_text()
                    fn_match = re.search(r'Filename\.+:\s*(.+)', text) or re.search(r'Filename\.*:\s*(.+)', text)

            if not fn_match:
                return None
            full_fn = fn_match.group(1).strip()

            # Count all episodes (number of Filename entries) for TV packs
            all_filenames = re.findall(r'Filename\.*:\s*.+', text)
            episodes_count = len(all_filenames)

            # Smart Check: Scan ALL filenames for unique episode numbers
            # This distinguishes "Season Pack" (E01, E02...) from "Single Ep with Mirrors" (E01, E01...)
            unique_ep_nums = set()
            for fn_line in all_filenames:
                m = re.search(r'[.\s]S(\d{1,2})E(\d{1,2})(?:[.\s]|$)', fn_line, re.IGNORECASE)
                if m:
                    unique_ep_nums.add(int(m.group(2)))

            # Use unique episode count instead of total filenames (handles mirrors/duplicates)
            if unique_ep_nums:
                episodes_count = len(unique_ep_nums)

            # Check for TV Season pattern first (Show.Name.S01E01 or Show.Name.S01.Complete)
            is_tv = False
            season = None
            episode_number = None

            tv_ep_match = re.search(r'[\s.\-]+S(\d{1,2})E(\d{1,2})(?:[\-E]?\d{1,2})?(?:[\s.\-]|$)', full_fn, re.IGNORECASE)
            tv_season_match = re.search(r'[\s.\-]+S(\d{1,2})(?:[\s.\-]|$)', full_fn, re.IGNORECASE)

            if tv_ep_match:
                is_tv = True
                season = int(tv_ep_match.group(1))
                episode_number = int(tv_ep_match.group(2))

                # OVERRIDE: If we found multiple UNIQUE episodes in the file list,
                # this is a Season Pack, not a single episode
                if len(unique_ep_nums) > 1:
                    if self.app.config.get("debug_mode"):
                        self.app.safe_log(f"[DEBUG] '{full_fn}' has {len(unique_ep_nums)} unique eps -> Treating as Season Pack")
                    episode_number = None

                show_name_match = re.match(r'^(.+?)[\s.\-_]+S\d{1,2}E\d{1,2}', full_fn, re.IGNORECASE)
                if show_name_match:
                    raw_title = show_name_match.group(1)
                    clean_title = raw_title.replace('.', ' ').replace('_', ' ').strip(' -')
                else:
                    clean_title = full_fn
                year = 0
            elif tv_season_match:
                is_tv = True
                season = int(tv_season_match.group(1))
                show_name_match = re.match(r'^(.+?)[\s.\-_]+S\d{1,2}', full_fn, re.IGNORECASE)
                if show_name_match:
                    raw_title = show_name_match.group(1)
                    clean_title = raw_title.replace('.', ' ').replace('_', ' ').strip(' -')
                else:
                    clean_title = full_fn
                year = 0
            else:
                # Movie pattern: Title.Year or Title (Year)
                ty_match = re.search(r'^(.+?)[.\s\(\-]+(19\d{2}|20\d{2})', full_fn)
                if ty_match:
                    raw_title = ty_match.group(1)
                    year = int(ty_match.group(2))
                    clean_title = raw_title.replace('.', ' ').replace('_', ' ').strip()
                else:
                    clean_title = full_fn
                    year = 0

            # Repair cp437 mojibake (e.g. ΓÇÖ → ') common on Windows-sourced filenames
            try:
                clean_title = clean_title.encode('cp437').decode('utf-8')
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass  # Not cp437 mojibake, keep original

            # Normalize smart quotes and dashes to ASCII
            clean_title = clean_title.replace('\u2019', "'").replace('\u2018', "'").replace('\u2014', '-').replace('\u2013', '-')

            rating_match = re.search(r'Rating\s*:\s*(\d+(\.\d+)?)', text, re.IGNORECASE)
            rating = rating_match.group(1) if rating_match else "-"

            # ROBUST SIZE FINDING: Find ALL sizes and pick the largest
            size_matches = re.findall(r'\b(?:Total\s+)?(?:File\s*)?Size\s*(?:\.|:)?\s*(\d+(?:\.\d+)?\s*(?:GiB|GB|MiB|MB|KB))', text, re.IGNORECASE)

            if not size_matches:
                loose_matches = re.findall(r'\b(\d+(?:\.\d+)?\s*(?:GiB|GB|MiB|MB))\b', text, re.IGNORECASE)
                if loose_matches:
                    size_matches = loose_matches
                    if self.app.config.get("debug_mode", False):
                        self.app.safe_log(f"[DEBUG] Using loose size matches for '{clean_title}': {size_matches}")

            size = "?"
            if size_matches:
                max_gb = -1.0
                best_size = "?"
                found_sizes = []

                for s_str in size_matches:
                    gb_val = self.app.parse_size(s_str)
                    found_sizes.append(f"{s_str}({gb_val:.2f}GB)")
                    if gb_val > max_gb:
                        max_gb = gb_val
                        best_size = s_str.upper()

                size = best_size
                if self.app.config.get("debug_mode", False):
                    self.app.safe_log(f"[DEBUG] '{clean_title}' found sizes: {found_sizes} -> Selected: {size}")
            else:
                if self.app.config.get("debug_mode", False):
                    self.app.safe_log(f"[DEBUG] No size found for '{clean_title}' | Text Sample: {text[:200].replace(chr(10), ' ')}")
                    if 'size' in text.lower():
                        idx = text.lower().find('size')
                        snippet = text[max(0, idx-20):min(len(text), idx+50)].replace('\n', ' ')
                        self.app.safe_log(f"[DEBUG] 'Size' keyword found at {idx}: '...{snippet}...'")

            res_match = re.search(r'Resolution\.*:\s*(\d+x\d+|2160p|1080p)', text, re.IGNORECASE)
            res = "?"
            if res_match:
                if "3840" in res_match.group(1) or "2160" in res_match.group(1):
                    res = "4K"
                elif "1080" in res_match.group(1):
                    res = "1080p"
                elif "720" in res_match.group(1):
                    res = "720p"

            # Prefer filename resolution if explicit
            fn_lower = full_fn.lower()
            if "2160" in fn_lower or "4k" in fn_lower or "uhd" in fn_lower:
                res = "4K"
            elif "1080" in fn_lower:
                res = "1080p"
            elif "720" in fn_lower:
                res = "720p"

            hdr = "SDR"
            dovi = False
            if re.search(r'\b(DV|DoVi|Dolby\s?Vision)\b', full_fn, re.IGNORECASE):
                dovi = True
            hdr_match = re.search(r'Color primaries\.*:\s*(.+)', text, re.IGNORECASE)
            if hdr_match:
                ht = hdr_match.group(1).lower()
                if "bt.2020" in ht or "hdr" in ht:
                    hdr = "HDR"
                if "dovi" in ht or "dolby vision" in ht:
                    dovi = True

            full_text = soup.get_text()
            imdb_link = None
            imdb_id = None
            for a in soup.find_all('a', href=True):
                if "imdb.com/title/" in a['href']:
                    imdb_link = a['href']
                    id_match = re.search(r'(tt\d+)', imdb_link)
                    if id_match:
                        imdb_id = id_match.group(1)
                    break
            if not imdb_id:
                pt = re.search(r'(?:imdb\.com/title/|imdb[:\s]+)(tt\d{7,})', full_text, re.IGNORECASE)
                if pt:
                    imdb_id = pt.group(1)
                    imdb_link = f"https://www.imdb.com/title/{imdb_id}/"

            # Extract posted date (e.g. "Posted on March 1, 2026 at 03:15 PM")
            posted_date = None
            date_match = re.search(
                r'Posted\s+on\s+(\w+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*[AP]M)',
                full_text, re.IGNORECASE
            )
            if date_match:
                posted_date = date_match.group(1)

            return {
                'display_title': clean_title,
                'year': year,
                'rating': rating,
                'search_key': self.app.clean_string(clean_title),
                'url': url,
                'imdb_link': imdb_link,
                'imdb_id': imdb_id,
                'size': size,
                'res': res,
                'hdr': hdr,
                'dovi': dovi,
                'tmdb_votes': "-",
                'is_tv': is_tv,
                'season': season,
                'episode_number': episode_number,
                'episodes': episodes_count if is_tv else None,
                'posted_date': posted_date,
            }
        except Exception as e:
            if self.app.config.get("debug_mode", False):
                self.app.safe_log(f"Scrape Details Error ({url}): {e}")
            return None

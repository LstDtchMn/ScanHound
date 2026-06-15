"""LinkScraper — Selenium-based download link resolution.

Provides the LinkScraper class used by WebScrapers to scrape download links
from HDEncode pages and resolve cuty.io/cuttlinks.com shortlinks to their
final 1fichier.com URLs.

Requires a running Selenium WebDriver instance to be passed by the caller.
"""

import logging
import time

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)

logger = logging.getLogger(__name__)

# Shortlink domains supported by resolve_cuty_link
_VALID_SHORTLINK_DOMAINS = ['cuty.io', 'cuttlinks.com', 'cutt.ly', 'cuttus.com', 'fc.lc', 'ouo.io']


class LinkScraper:
    """Scrapes download links using Selenium WebDriver."""

    def __init__(self, parent_app):
        """Initialize with reference to the parent app (config, logging).

        Args:
            parent_app: AppService instance (provides config, safe_log).
        """
        self.app = parent_app

    def scrape_links_with_driver(self, driver, url, service_type):
        """Scrape download links using Selenium WebDriver.

        Navigates to the given URL, clicks the "Access the links" button
        (or equivalent form submit), then collects all hrefs matching the
        requested hosting service.

        Args:
            driver: Selenium WebDriver instance.
            url: URL to scrape.
            service_type: "Rapidgator" or "Nitroflare".

        Returns:
            list: List of download link strings, or empty list if failed.
        """
        try:
            driver.get(url)
            try:
                wait = WebDriverWait(driver, 8)

                access_btn = None
                selectors = [
                    "//input[@value='Access the links']",
                    "//input[contains(@value, 'Access')]",
                    "//input[@type='submit']",
                    "//button[contains(text(), 'Access')]"
                ]

                for xpath in selectors:
                    try:
                        access_btn = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
                        if access_btn:
                            break
                    except (TimeoutException, NoSuchElementException, WebDriverException):
                        continue

                if not access_btn:
                    try:
                        access_btn = driver.find_element(By.CSS_SELECTOR, "form input[type='submit']")
                    except (NoSuchElementException, WebDriverException):
                        pass

                if access_btn:
                    driver.execute_script("arguments[0].scrollIntoView();", access_btn)
                    time.sleep(0.3)
                    driver.execute_script("arguments[0].click();", access_btn)
                else:
                    return []
            except Exception as e:
                self.app.safe_log(f"Scraping Error (Button): {e}")
                return []

            wait = WebDriverWait(driver, 10)
            keyword = "rapidgator" if service_type == "Rapidgator" else "nitroflare"
            xpath_query = f"//a[contains(@href, '{keyword}')]"
            try:
                wait.until(EC.presence_of_element_located((By.XPATH, xpath_query)))
            except (TimeoutException, WebDriverException):
                return []

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            found_links = []
            for a in soup.find_all('a', href=True):
                if keyword in a['href'].lower():
                    found_links.append(a['href'])
            return found_links
        except Exception:
            return []

    def resolve_cuty_link(self, driver, cuty_url, credentials=None):
        """Resolve cuty.io/cuttlinks.com shortened link to final 1fichier.com URL.

        Uses Selenium to navigate through the shortlink redirect and extract
        the final download URL.

        Args:
            driver: Selenium WebDriver instance.
            cuty_url: Shortlink URL (e.g. https://cuty.io/f2Xe6qI).
            credentials: Optional dict with 'email' and 'password' for login.

        Returns:
            str: Final 1fichier.com URL or None if resolution failed.
        """
        if not cuty_url or not any(domain in cuty_url for domain in _VALID_SHORTLINK_DOMAINS):
            return None

        try:
            if credentials and credentials.get('email') and credentials.get('password'):
                try:
                    self._cuty_login(driver, credentials)
                except Exception as e:
                    if self.app.config.get("debug_mode"):
                        self.app.safe_log(f"[Shortlink] Login failed: {e}")

            driver.get(cuty_url)

            wait = WebDriverWait(driver, 15)

            button_selectors = [
                "//button[contains(text(), 'Get Link')]",
                "//button[contains(text(), 'Continue')]",
                "//a[contains(text(), 'Get Link')]",
                "//a[contains(text(), 'Continue')]",
                "//input[@type='submit']",
                "//button[@type='submit']",
                "//*[@id='getlink']",
                "//*[contains(@class, 'get-link')]",
            ]

            for selector in button_selectors:
                try:
                    btn = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                    if btn:
                        driver.execute_script("arguments[0].scrollIntoView();", btn)
                        time.sleep(0.3)
                        driver.execute_script("arguments[0].click();", btn)
                        break
                except (TimeoutException, NoSuchElementException, WebDriverException):
                    continue

            # Wait for countdown timer if present (cuty.io often has a wait timer)
            try:
                time.sleep(2)
                for _ in range(30):
                    try:
                        timer = driver.find_element(By.CSS_SELECTOR, "#timer, .timer, [id*='count'], [class*='count']")
                        timer_text = timer.text.strip()
                        if timer_text and timer_text.isdigit() and int(timer_text) > 0:
                            time.sleep(1)
                            continue
                        else:
                            break
                    except NoSuchElementException:
                        break
            except Exception:
                pass  # Timer handling is optional

            # Look for the final link
            time.sleep(1)
            final_link_selectors = [
                "//a[contains(@href, '1fichier.com')]",
                "//a[contains(text(), 'Download')]",
                "//a[contains(@class, 'download')]",
                "//*[@id='download-link']//a",
                "//a[contains(@href, 'http') and not(contains(@href, 'cuty.io'))]",
            ]

            for selector in final_link_selectors:
                try:
                    link_elem = driver.find_element(By.XPATH, selector)
                    href = link_elem.get_attribute('href')
                    if href and '1fichier.com' in href:
                        if self.app.config.get("debug_mode"):
                            self.app.safe_log(f"[Shortlink] Resolved: {cuty_url} -> {href}")
                        return href
                except (NoSuchElementException, WebDriverException):
                    continue

            # Check current URL in case of redirect
            current_url = driver.current_url
            if '1fichier.com' in current_url:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[Shortlink] Redirected to: {current_url}")
                return current_url

            # Parse page source for 1fichier links
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            for a in soup.find_all('a', href=True):
                if '1fichier.com' in a['href']:
                    if self.app.config.get("debug_mode"):
                        self.app.safe_log(f"[Shortlink] Found in page: {a['href']}")
                    return a['href']

            self.app.safe_log(f"[Shortlink] Could not resolve: {cuty_url}")
            return None

        except Exception as e:
            if self.app.config.get("debug_mode"):
                self.app.safe_log(f"[Shortlink] Error resolving {cuty_url}: {e}")
            return None

    def _cuty_login(self, driver, credentials):
        """Login to cuty.io for faster link resolution.

        Args:
            driver: Selenium WebDriver instance.
            credentials: Dict with 'email' and 'password'.
        """
        login_url = "https://cuty.io/login"
        driver.get(login_url)

        wait = WebDriverWait(driver, 10)

        try:
            email_field = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//input[@type='email' or @name='email' or @id='email']")
            ))
            email_field.clear()
            email_field.send_keys(credentials['email'])

            password_field = driver.find_element(
                By.XPATH, "//input[@type='password' or @name='password' or @id='password']"
            )
            password_field.clear()
            password_field.send_keys(credentials['password'])

            login_btn = driver.find_element(
                By.XPATH, "//button[@type='submit'] | //input[@type='submit']"
            )
            login_btn.click()

            time.sleep(2)

            if self.app.config.get("debug_mode"):
                self.app.safe_log("[Shortlink] Login successful")

        except Exception as e:
            if self.app.config.get("debug_mode"):
                self.app.safe_log(f"[Shortlink] Login error: {e}")
            raise

    def open_cuty_in_browser(self, cuty_url):
        """Fallback: Open cuty.io link in the user's default browser.

        Used when automatic resolution fails.

        Args:
            cuty_url: Shortlink URL to open.

        Returns:
            bool: True if browser was opened successfully.
        """
        import webbrowser

        try:
            webbrowser.open(cuty_url)
            self.app.safe_log(f"[Shortlink] Opened in browser: {cuty_url}")
            return True
        except Exception as e:
            self.app.safe_log(f"[Shortlink] Failed to open browser: {e}")
            return False

    def resolve_cuty_links_batch(self, driver, cuty_urls, credentials=None):
        """Resolve multiple cuty.io links, with browser fallback per link.

        Args:
            driver: Selenium WebDriver instance.
            cuty_urls: List of cuty.io/shortlink URLs.
            credentials: Optional dict with 'email' and 'password'.

        Returns:
            dict: Mapping of cuty_url -> resolved_url (or None if failed).
        """
        results = {}

        for cuty_url in cuty_urls:
            resolved = self.resolve_cuty_link(driver, cuty_url, credentials)
            if resolved:
                results[cuty_url] = resolved
            else:
                self.open_cuty_in_browser(cuty_url)
                results[cuty_url] = None

            # Small delay between resolutions to avoid rate limiting
            time.sleep(1)

        return results

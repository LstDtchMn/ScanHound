"""Network Layer — Async HTTP client with retry, rate-limit handling, and error classification.

Provides AsyncRequestManager for making resilient HTTP requests with
automatic retries, exponential backoff for rate limits, and structured
error types for callers to handle.
"""

import aiohttp
import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ── Exception hierarchy ──────────────────────────────────────────────

class NetworkError(Exception):
    """Base exception for all network-related errors."""
    pass


class RequestTimeoutError(NetworkError):
    """Raised when a request exceeds the configured timeout.

    Named to avoid shadowing the builtin TimeoutError.
    """
    pass


class RateLimitError(NetworkError):
    """Raised when the remote server returns HTTP 429 (Too Many Requests)."""
    pass


# ── Async request manager ───────────────────────────────────────────

class AsyncRequestManager:
    """Manages an aiohttp ClientSession for efficient async HTTP requests.

    Features:
        - Automatic retries with exponential backoff for rate limits (429).
        - Retries on server errors (5xx) and transient network failures.
        - Immediate failure on client errors (4xx, except 429).
        - Session reuse across requests for connection pooling.
    """

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        """Initialize the request manager.

        Args:
            timeout: Request timeout in seconds (applies to entire request).
            max_retries: Maximum number of retry attempts per request.
        """
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._max_retries = max_retries

    async def __aenter__(self):
        """Support async context manager usage."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close session on context exit."""
        await self.close()
        return False

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or lazily create a reusable aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        """Close the underlying aiohttp session and free resources."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("AsyncRequestManager session closed")

    # ── Public fetch methods ─────────────────────────────────────────

    async def fetch_json(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[int] = None,
        **kwargs
    ) -> Optional[Any]:
        """Fetch and parse JSON from a URL with automatic retries.

        Args:
            url: Target URL.
            headers: Optional HTTP headers to include.
            retry: Override the default max retry count.
            **kwargs: Additional arguments forwarded to aiohttp session.get().

        Returns:
            Parsed JSON data (dict/list) on success, None on failure.
        """
        return await self._fetch(url, "json", headers=headers, retry=retry, **kwargs)

    async def fetch_text(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[int] = None,
        **kwargs
    ) -> Optional[str]:
        """Fetch raw text/HTML from a URL with automatic retries.

        Args:
            url: Target URL.
            headers: Optional HTTP headers to include.
            retry: Override the default max retry count.
            **kwargs: Additional arguments forwarded to aiohttp session.get().

        Returns:
            Response text on success, None on failure.
        """
        return await self._fetch(url, "text", headers=headers, retry=retry, **kwargs)

    # ── Internal retry engine ────────────────────────────────────────

    async def _fetch(
        self,
        url: str,
        response_format: str,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[int] = None,
        **kwargs
    ) -> Optional[Any]:
        """Core fetch method with retry logic shared by fetch_json and fetch_text.

        Handles HTTP status codes, rate limiting, server errors, and transient
        network failures with a unified retry strategy:
            - 429 (rate limit): exponential backoff (1s, 2s, 4s, ...)
            - 5xx (server error): 1s pause then retry
            - 4xx (client error): fail immediately (no retry)
            - Network/timeout errors: 0.5s pause then retry

        Args:
            url: Target URL.
            response_format: "json" or "text" — determines response parsing.
            headers: Optional HTTP headers.
            retry: Max retry attempts (defaults to self._max_retries).
            **kwargs: Additional aiohttp request arguments.

        Returns:
            Parsed response data on success, None after all retries exhausted.
        """
        max_attempts = retry if retry is not None else self._max_retries
        session = await self.get_session()
        last_error = None

        for attempt in range(max_attempts):
            try:
                async with session.get(url, headers=headers, **kwargs) as response:
                    # Success — parse response in the requested format
                    if response.status == 200:
                        try:
                            if response_format == "json":
                                return await response.json()
                            return await response.text()
                        except (aiohttp.ContentTypeError, ValueError) as e:
                            logger.error("Invalid %s response from %s: %s",
                                         response_format, url, e)
                            return None

                    # Rate limited — exponential backoff
                    if response.status == 429:
                        wait_time = 2 ** attempt
                        logger.warning(
                            "Rate limited by %s. Waiting %ds (attempt %d/%d)",
                            url, wait_time, attempt + 1, max_attempts)
                        await asyncio.sleep(wait_time)
                        continue

                    # Client errors (4xx, excluding 429) — no point retrying
                    if 400 <= response.status < 500:
                        logger.warning("Client error %d for %s",
                                       response.status, url)
                        return None

                    # Server errors (5xx) — likely transient, retry
                    if 500 <= response.status < 600:
                        logger.warning(
                            "Server error %d for %s (attempt %d/%d)",
                            response.status, url, attempt + 1, max_attempts)
                        await asyncio.sleep(1)
                        continue

                    # Unexpected status codes
                    logger.warning("Unexpected HTTP %d for %s",
                                   response.status, url)
                    return None

            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning("Timeout fetching %s (attempt %d/%d)",
                               url, attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.5)

            except aiohttp.ClientError as e:
                last_error = e
                logger.error("Client error fetching %s: %s: %s (attempt %d/%d)",
                             url, type(e).__name__, e, attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                last_error = e
                logger.error(
                    "Unexpected error fetching %s: %s: %s (attempt %d/%d)",
                    url, type(e).__name__, e, attempt + 1, max_attempts)
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.5)

        # All retries exhausted
        if last_error:
            logger.error("Failed to fetch %s after %d attempts. Last error: %s",
                         url, max_attempts, last_error)
        return None

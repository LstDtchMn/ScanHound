"""Tests for backend/network.py.

Covers:
- NetworkError, RequestTimeoutError, RateLimitError exception hierarchy
- AsyncRequestManager.__init__ defaults and custom values
- AsyncRequestManager async context manager protocol
- AsyncRequestManager.get_session creation and reuse
- AsyncRequestManager.close behaviour
- AsyncRequestManager.fetch_json with various HTTP status codes and error conditions
- AsyncRequestManager.fetch_text with various HTTP status codes and error conditions
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import aiohttp
from backend.network import (
    NetworkError,
    RequestTimeoutError,
    RateLimitError,
    AsyncRequestManager,
)


# ---------------------------------------------------------------------------
# Helpers: mock response objects that support async context manager protocol
# ---------------------------------------------------------------------------

def _make_mock_response(status=200, json_data=None, text_data="", raise_json=None):
    """Create a mock response that works as an async context manager.

    Args:
        status: HTTP status code.
        json_data: Data returned by response.json().
        text_data: Data returned by response.text().
        raise_json: If set, response.json() raises this exception.
    """
    response = AsyncMock()
    response.status = status

    if raise_json:
        response.json = AsyncMock(side_effect=raise_json)
    else:
        response.json = AsyncMock(return_value=json_data)

    response.text = AsyncMock(return_value=text_data)

    # Make response usable as ``async with session.get(...) as resp:``
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, response


def _make_mock_session(context_manager):
    """Create a mock aiohttp.ClientSession whose .get() returns *context_manager*."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(return_value=context_manager)
    session.closed = False
    return session


def _make_mock_session_multi(context_managers):
    """Session whose .get() returns successive context managers (for retries)."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(side_effect=context_managers)
    session.closed = False
    return session


# ===================================================================
# Exception hierarchy
# ===================================================================

class TestExceptionHierarchy:

    def test_network_error_is_exception(self):
        assert issubclass(NetworkError, Exception)

    def test_request_timeout_error_is_network_error(self):
        assert issubclass(RequestTimeoutError, NetworkError)

    def test_rate_limit_error_is_network_error(self):
        assert issubclass(RateLimitError, NetworkError)

    def test_raise_network_error(self):
        with pytest.raises(NetworkError):
            raise NetworkError("boom")

    def test_raise_request_timeout_error(self):
        with pytest.raises(NetworkError):
            raise RequestTimeoutError("timed out")

    def test_raise_rate_limit_error(self):
        with pytest.raises(NetworkError):
            raise RateLimitError("429")

    def test_catch_timeout_as_network_error(self):
        try:
            raise RequestTimeoutError("timed out")
        except NetworkError as exc:
            assert "timed out" in str(exc)

    def test_catch_rate_limit_as_network_error(self):
        try:
            raise RateLimitError("limited")
        except NetworkError as exc:
            assert "limited" in str(exc)

    def test_network_error_not_caught_by_builtin_timeout(self):
        """NetworkError is NOT a subclass of builtin TimeoutError."""
        assert not issubclass(NetworkError, TimeoutError)


# ===================================================================
# AsyncRequestManager.__init__
# ===================================================================

class TestAsyncRequestManagerInit:

    def test_default_timeout_and_retries(self):
        mgr = AsyncRequestManager()
        assert mgr._timeout.total == 30
        assert mgr._max_retries == 3

    def test_custom_timeout_and_retries(self):
        mgr = AsyncRequestManager(timeout=60, max_retries=5)
        assert mgr._timeout.total == 60
        assert mgr._max_retries == 5

    def test_session_initially_none(self):
        mgr = AsyncRequestManager()
        assert mgr._session is None


# ===================================================================
# Async context manager
# ===================================================================

class TestAsyncContextManager:

    @pytest.mark.asyncio
    async def test_aenter_returns_self(self):
        mgr = AsyncRequestManager()
        result = await mgr.__aenter__()
        assert result is mgr

    @pytest.mark.asyncio
    async def test_aexit_closes_session(self):
        mgr = AsyncRequestManager()
        mock_session = AsyncMock()
        mock_session.closed = False
        mgr._session = mock_session

        await mgr.__aexit__(None, None, None)
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_manager_full_cycle(self):
        mock_session = AsyncMock()
        mock_session.closed = False

        with patch("aiohttp.ClientSession", return_value=mock_session):
            async with AsyncRequestManager() as mgr:
                assert mgr is not None
                # Simulate real usage: acquire a session inside the context
                session = await mgr.get_session()
                assert session is mock_session
            # Session should be closed after exiting context
            mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aexit_returns_false(self):
        mgr = AsyncRequestManager()
        result = await mgr.__aexit__(None, None, None)
        assert result is False


# ===================================================================
# get_session
# ===================================================================

class TestGetSession:

    @pytest.mark.asyncio
    async def test_creates_session_when_none(self):
        mgr = AsyncRequestManager()
        mock_session = MagicMock()
        mock_session.closed = False

        with patch("aiohttp.ClientSession", return_value=mock_session) as mock_cls:
            session = await mgr.get_session()
            mock_cls.assert_called_once_with(timeout=mgr._timeout)
            assert session is mock_session

    @pytest.mark.asyncio
    async def test_reuses_existing_session(self):
        mgr = AsyncRequestManager()
        mock_session = MagicMock()
        mock_session.closed = False
        mgr._session = mock_session

        with patch("aiohttp.ClientSession") as mock_cls:
            session = await mgr.get_session()
            mock_cls.assert_not_called()
            assert session is mock_session

    @pytest.mark.asyncio
    async def test_creates_new_session_if_closed(self):
        mgr = AsyncRequestManager()
        old_session = MagicMock()
        old_session.closed = True
        mgr._session = old_session

        new_session = MagicMock()
        new_session.closed = False

        with patch("aiohttp.ClientSession", return_value=new_session) as mock_cls:
            session = await mgr.get_session()
            mock_cls.assert_called_once()
            assert session is new_session


# ===================================================================
# close
# ===================================================================

class TestClose:

    @pytest.mark.asyncio
    async def test_close_closes_open_session(self):
        mgr = AsyncRequestManager()
        mock_session = AsyncMock()
        mock_session.closed = False
        mgr._session = mock_session

        await mgr.close()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_no_error_when_no_session(self):
        mgr = AsyncRequestManager()
        assert mgr._session is None
        # Should not raise
        await mgr.close()

    @pytest.mark.asyncio
    async def test_close_skips_already_closed_session(self):
        mgr = AsyncRequestManager()
        mock_session = AsyncMock()
        mock_session.closed = True
        mgr._session = mock_session

        await mgr.close()
        mock_session.close.assert_not_awaited()


# ===================================================================
# fetch_json
# ===================================================================

class TestFetchJson:

    @pytest.mark.asyncio
    async def test_200_returns_parsed_json(self):
        expected = {"key": "value"}
        ctx, _ = _make_mock_response(status=200, json_data=expected)
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager()
        mgr._session = session
        mgr._session.closed = False

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_json("https://example.com/api")

        assert result == expected

    @pytest.mark.asyncio
    async def test_429_retries_with_backoff(self):
        """429 triggers retry; on second attempt a 200 succeeds."""
        ctx_429, _ = _make_mock_response(status=429)
        ctx_200, _ = _make_mock_response(status=200, json_data={"ok": True})
        session = _make_mock_session_multi([ctx_429, ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await mgr.fetch_json("https://example.com/api")

        assert result == {"ok": True}
        assert session.get.call_count == 2
        # First retry: backoff = 2^0 = 1
        mock_sleep.assert_any_call(1)

    @pytest.mark.asyncio
    async def test_4xx_returns_none_without_retry(self):
        ctx, _ = _make_mock_response(status=404)
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_json("https://example.com/api")

        assert result is None
        assert session.get.call_count == 1  # No retry

    @pytest.mark.asyncio
    async def test_403_returns_none_without_retry(self):
        ctx, _ = _make_mock_response(status=403)
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_json("https://example.com/api")

        assert result is None
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retries(self):
        """500 triggers retries; eventually a 200 succeeds."""
        ctx_500, _ = _make_mock_response(status=500)
        ctx_200, _ = _make_mock_response(status=200, json_data={"recovered": True})
        session = _make_mock_session_multi([ctx_500, ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api")

        assert result == {"recovered": True}
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_retries(self):
        """asyncio.TimeoutError triggers retry."""
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False

        ctx_200, _ = _make_mock_response(status=200, json_data={"ok": True})
        session.get = MagicMock(side_effect=[asyncio.TimeoutError(), ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api")

        assert result == {"ok": True}
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_none(self):
        ctx_500_1, _ = _make_mock_response(status=500)
        ctx_500_2, _ = _make_mock_response(status=500)
        ctx_500_3, _ = _make_mock_response(status=500)
        session = _make_mock_session_multi([ctx_500_1, ctx_500_2, ctx_500_3])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api")

        assert result is None
        assert session.get.call_count == 3

    @pytest.mark.asyncio
    async def test_all_timeout_retries_exhausted_returns_none(self):
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False
        session.get = MagicMock(
            side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()]
        )

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api")

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        """ContentTypeError from response.json() should return None immediately."""
        ctx, resp = _make_mock_response(status=200)
        resp.json = AsyncMock(
            side_effect=aiohttp.ContentTypeError(
                MagicMock(),
                (),
                message="not json",
            )
        )
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_json("https://example.com/api")

        assert result is None
        # Should not retry on ContentTypeError (it's a 200 response)
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_custom_headers_passed(self):
        ctx, _ = _make_mock_response(status=200, json_data={"ok": True})
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager()

        headers = {"Authorization": "Bearer token123"}
        with patch.object(mgr, "get_session", return_value=session):
            await mgr.fetch_json("https://example.com/api", headers=headers)

        session.get.assert_called_once_with(
            "https://example.com/api", headers=headers
        )

    @pytest.mark.asyncio
    async def test_custom_retry_overrides_default(self):
        """The ``retry`` parameter overrides the default max_retries."""
        ctx_500_1, _ = _make_mock_response(status=500)
        ctx_500_2, _ = _make_mock_response(status=500)
        session = _make_mock_session_multi([ctx_500_1, ctx_500_2])

        mgr = AsyncRequestManager(max_retries=5)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api", retry=2)

        assert result is None
        assert session.get.call_count == 2  # Only 2, not 5

    @pytest.mark.asyncio
    async def test_client_error_retries(self):
        """aiohttp.ClientError triggers retry."""
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False

        ctx_200, _ = _make_mock_response(status=200, json_data={"ok": True})
        session.get = MagicMock(
            side_effect=[aiohttp.ClientError("connection reset"), ctx_200]
        )

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_json("https://example.com/api")

        assert result == {"ok": True}
        assert session.get.call_count == 2


# ===================================================================
# fetch_text
# ===================================================================

class TestFetchText:

    @pytest.mark.asyncio
    async def test_200_returns_text(self):
        html = "<html><body>Hello</body></html>"
        ctx, _ = _make_mock_response(status=200, text_data=html)
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager()

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_text("https://example.com")

        assert result == html

    @pytest.mark.asyncio
    async def test_429_retries(self):
        ctx_429, _ = _make_mock_response(status=429)
        ctx_200, _ = _make_mock_response(status=200, text_data="ok")
        session = _make_mock_session_multi([ctx_429, ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await mgr.fetch_text("https://example.com")

        assert result == "ok"
        assert session.get.call_count == 2
        mock_sleep.assert_any_call(1)

    @pytest.mark.asyncio
    async def test_4xx_returns_none(self):
        ctx, _ = _make_mock_response(status=401)
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session):
            result = await mgr.fetch_text("https://example.com")

        assert result is None
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retries(self):
        ctx_502, _ = _make_mock_response(status=502)
        ctx_200, _ = _make_mock_response(status=200, text_data="recovered")
        session = _make_mock_session_multi([ctx_502, ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com")

        assert result == "recovered"
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_retries(self):
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False

        ctx_200, _ = _make_mock_response(status=200, text_data="after timeout")
        session.get = MagicMock(side_effect=[asyncio.TimeoutError(), ctx_200])

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com")

        assert result == "after timeout"
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_returns_none(self):
        ctx1, _ = _make_mock_response(status=503)
        ctx2, _ = _make_mock_response(status=503)
        session = _make_mock_session_multi([ctx1, ctx2])

        mgr = AsyncRequestManager(max_retries=2)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com")

        assert result is None
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_custom_headers_passed(self):
        ctx, _ = _make_mock_response(status=200, text_data="ok")
        session = _make_mock_session(ctx)

        mgr = AsyncRequestManager()
        headers = {"Accept": "text/html"}

        with patch.object(mgr, "get_session", return_value=session):
            await mgr.fetch_text("https://example.com", headers=headers)

        session.get.assert_called_once_with(
            "https://example.com", headers=headers
        )

    @pytest.mark.asyncio
    async def test_custom_retry_overrides_default(self):
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False
        session.get = MagicMock(
            side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError()]
        )

        mgr = AsyncRequestManager(max_retries=5)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com", retry=2)

        assert result is None
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_mixed_429_then_timeout_then_success(self):
        """Exercises multiple retry paths in a single call chain."""
        ctx_429, _ = _make_mock_response(status=429)
        ctx_200, _ = _make_mock_response(status=200, text_data="finally")

        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False
        session.get = MagicMock(
            side_effect=[ctx_429, asyncio.TimeoutError(), ctx_200]
        )

        mgr = AsyncRequestManager(max_retries=4)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com")

        assert result == "finally"
        assert session.get.call_count == 3

    @pytest.mark.asyncio
    async def test_client_error_retries(self):
        session = AsyncMock(spec=aiohttp.ClientSession)
        session.closed = False

        ctx_200, _ = _make_mock_response(status=200, text_data="recovered")
        session.get = MagicMock(
            side_effect=[aiohttp.ClientError("conn dropped"), ctx_200]
        )

        mgr = AsyncRequestManager(max_retries=3)

        with patch.object(mgr, "get_session", return_value=session), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.fetch_text("https://example.com")

        assert result == "recovered"
        assert session.get.call_count == 2

"""WebSocket hub for real-time communication."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

__version__ = "2.0.0-dev"


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: List[WebSocket] = []
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the running loop at app startup so broadcast_sync works even
        before the first WebSocket client connects.

        Without this, _loop is captured only in connect(), so any background
        thread (the results poller, plex auto-connect) that broadcasts during
        startup — before any client has opened /ws — is silently dropped.
        """
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        # Refresh the captured loop for broadcast_sync (set at startup too).
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            self._connections.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self._connections))
        await ws.send_json({"type": "connected", "data": {"version": __version__}})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._connections:
                self._connections.remove(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(self._connections))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        # Snapshot connections under lock, then release before sending
        async with self._lock:
            targets = list(self._connections)

        stale: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)

        if stale:
            async with self._lock:
                for ws in stale:
                    if ws in self._connections:
                        self._connections.remove(ws)

    def broadcast_sync(self, message: Dict[str, Any]) -> None:
        """Thread-safe broadcast from sync code (e.g., scanner callbacks).

        Uses the captured event loop to schedule the broadcast.
        Safe to call from any thread.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        msg_copy = dict(message)
        loop.call_soon_threadsafe(
            lambda m=msg_copy: asyncio.ensure_future(self.broadcast(m))
        )


ws_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    # Validate auth nonce if enabled
    from backend.api.dependencies import registry
    nonce = registry.auth_nonce
    if nonce and token != nonce:
        await ws.close(code=1008, reason="Unauthorized")
        return
    await ws_manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                logger.debug("WS received: %s", msg_type)
                # Client->Server message handling will be added in future tasks
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "data": {"message": "Invalid JSON"}})
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(ws)

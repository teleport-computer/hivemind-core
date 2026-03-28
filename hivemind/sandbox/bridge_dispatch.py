"""Bridge session dispatcher for Phala mode.

When the sandbox backend is "phala", bridge servers cannot run on ephemeral
ports because the agent CVM is remote. Instead, bridge sessions are mounted
on the main FastAPI server under ``/bridge/...`` and dispatched by session
token.

The dispatcher is an ASGI app that:
  1. Extracts the session token from ``Authorization: Bearer <token>``
     or ``x-api-key: <token>`` headers.
  2. Looks up the corresponding per-session bridge ASGI app.
  3. Forwards the request to it.

Mount on the main app with ``app.mount("/bridge", dispatcher)``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Singleton instance — import and use from other modules.
_instance: BridgeDispatcher | None = None


class BridgeDispatcher:
    """Routes ASGI requests to per-session bridge FastAPI apps by token."""

    def __init__(self) -> None:
        self._apps: dict[str, Any] = {}  # token -> ASGI app

    # ── Session management ──

    def register(self, token: str, app: Any) -> None:
        self._apps[token] = app

    def unregister(self, token: str) -> None:
        self._apps.pop(token, None)

    # ── ASGI interface ──

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return

        token = self._extract_token(scope)
        if not token or token not in self._apps:
            await self._send_error(send, 401, "Bridge session not found")
            return

        app = self._apps[token]
        await app(scope, receive, send)

    # ── Helpers ──

    @staticmethod
    def _extract_token(scope: dict) -> str:
        headers = dict(scope.get("headers", []))
        # Authorization: Bearer <token>
        auth = headers.get(b"authorization", b"").decode(errors="ignore")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        # x-api-key: <token>
        api_key = headers.get(b"x-api-key", b"").decode(errors="ignore").strip()
        if api_key:
            return api_key
        return ""

    @staticmethod
    async def _send_error(send, status: int, detail: str) -> None:
        import json

        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def get_dispatcher() -> BridgeDispatcher:
    """Return the global BridgeDispatcher singleton (created on first call)."""
    global _instance
    if _instance is None:
        _instance = BridgeDispatcher()
    return _instance

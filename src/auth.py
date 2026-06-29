"""
TokenManager: handles OAuth2 refresh-token flow for Zoho Analytics.

- On first call to get_access_token() it exchanges the refresh token for an
  access token and caches it.
- When a caller detects token expiry (Zoho error-code 8535, or HTTP 401) it
  calls refresh() to force a new access token.
- Thread-safe via asyncio.Lock so concurrent tool calls share one refresh.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import httpx


class TokenManager:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        accounts_url: str = "https://accounts.zoho.in",
    ) -> None:
        self._client_id     = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._accounts_url  = accounts_url.rstrip("/")

        self._access_token: str | None = None
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_access_token(self) -> str:
        """
        Return the cached access token.
        If none has been fetched yet this raises RuntimeError — call
        await refresh() at startup or handle lazily in the HTTP helper.
        """
        if self._access_token is None:
            raise RuntimeError(
                "No access token available. "
                "Call `await token_manager.refresh()` before making API calls, "
                "or let zoho_client handle it automatically."
            )
        return self._access_token

    async def refresh(self) -> str:
        """
        Exchange the refresh token for a new access token (async, lock-protected).
        Returns the new access token string.
        """
        async with self._lock:
            # Double-check — another coroutine may have refreshed while we waited
            token_url = f"{self._accounts_url}/oauth/v2/token"
            payload = {
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type":    "refresh_token",
            }
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    token_url,
                    content=urllib.parse.urlencode(payload),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Token refresh failed [{resp.status_code}]: {resp.text}"
                )

            data = resp.json()
            if "access_token" not in data:
                raise RuntimeError(
                    f"Token refresh response missing access_token: {data}"
                )

            self._access_token = data["access_token"]
            return self._access_token

    async def ensure(self) -> str:
        """Fetch token if not already cached, then return it."""
        if self._access_token is None:
            await self.refresh()
        return self._access_token

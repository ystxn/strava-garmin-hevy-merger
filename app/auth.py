"""Strava OAuth2 token management.

One athlete, one refresh token (from config). The access token lives ~6 hours;
this manager caches it in memory and refreshes proactively a configurable
buffer before expiry. All refreshes are serialised behind an ``asyncio.Lock``
so a burst of concurrent API calls triggers at most one refresh.

Strava usually returns the *same* refresh token on refresh, but it can rotate.
When it does we update the in-memory copy and log a WARNING (never the token's
neighbours — access tokens are never logged) telling the operator to update
the stored secret, since we cannot persist it back into a Kubernetes Secret
without cluster credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Settings
from .models import TokenResponse

OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"


class TokenManager:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._settings = settings
        self._log = logger
        self._access_token: str | None = None
        self._refresh_token: str = settings.strava_refresh_token
        self._expires_at: int = 0
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def _is_expired(self) -> bool:
        buffer = self._settings.token_refresh_buffer_seconds
        return (
            self._access_token is None
            or time.time() >= (self._expires_at - buffer)
        )

    async def get_access_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing under lock if needed."""
        if self._is_expired():
            async with self._lock:
                # Re-check inside the lock: another coroutine may have just
                # refreshed while we were waiting.
                if self._is_expired():
                    await self._refresh(client)
        assert self._access_token is not None
        return self._access_token

    async def refresh_now(self, client: httpx.AsyncClient) -> None:
        """Force a refresh (used once on startup to fail fast on bad creds)."""
        async with self._lock:
            await self._refresh(client)

    async def _refresh(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self._settings.strava_client_id,
                "client_secret": self._settings.strava_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
        )
        if resp.status_code != 200:
            # Body may contain the error reason but never an access token.
            self._log.error(
                "Token refresh failed",
                extra={
                    "stage": "token_refresh",
                    "http_status": resp.status_code,
                    "response_body": resp.text,
                },
            )
            resp.raise_for_status()

        token = TokenResponse.model_validate(resp.json())
        self._access_token = token.access_token
        self._expires_at = token.expires_at

        if token.refresh_token != self._refresh_token:
            self._refresh_token = token.refresh_token
            self._log.warning(
                "Strava rotated the refresh token. Update STRAVA_REFRESH_TOKEN "
                "in the secret to survive a restart; the new value is held in "
                "memory only.",
                extra={"stage": "token_refresh", "refresh_token_rotated": True},
            )

        self._log.info(
            "Refreshed Strava access token",
            extra={
                "stage": "token_refresh",
                "expires_at": self._expires_at,
                "expires_in_seconds": max(0, self._expires_at - int(time.time())),
            },
        )

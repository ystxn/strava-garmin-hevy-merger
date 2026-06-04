"""Tests for webhook push-subscription setup (creation + resilient retry).

A fresh deploy can have Strava 400 the subscription for the first minute or
two while the ingress and its TLS cert come up (Strava validates the callback
synchronously). The setup must therefore (a) surface Strava's error body so the
failure is diagnosable, and (b) keep retrying instead of giving up forever.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
import respx

from app.auth import TokenManager
from app.config import get_settings
from app.main import AppContext, _ensure_subscription, _subscription_loop
from app.matcher import Matcher
from app.strava import StravaClient

HOST = "www.strava.com"
NOT_VERIFIABLE = (
    '{"message":"Bad Request","errors":[{"resource":"PushSubscription",'
    '"field":"callback url","code":"not verifiable"}]}'
)


def _oauth(router: respx.MockRouter) -> None:
    router.route(method="POST", host=HOST, path="/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-123",
                "refresh_token": "test-refresh-token",
                "expires_at": 9999999999,
                "expires_in": 21600,
                "token_type": "Bearer",
            },
        )
    )


async def _build_ctx() -> AppContext:
    settings = get_settings()
    logger = logging.getLogger("strava_merger")
    http = httpx.AsyncClient()
    tokens = TokenManager(settings, logger)
    strava = StravaClient(settings, tokens, http, logger)
    matcher = Matcher(settings, logger)
    return AppContext(
        settings=settings,
        logger=logger,
        http=http,
        tokens=tokens,
        strava=strava,
        matcher=matcher,
    )


async def test_ensure_subscription_creates_when_absent() -> None:
    with respx.mock(assert_all_called=False) as router:
        _oauth(router)
        router.route(
            method="GET", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(return_value=httpx.Response(200, json=[]))
        create = router.route(
            method="POST", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": 9,
                    "callback_url": "https://strava-merger.example.com/webhook",
                },
            )
        )
        ctx = await _build_ctx()
        try:
            await _ensure_subscription(ctx, announce_present=True)
        finally:
            await ctx.http.aclose()
    assert create.called


async def test_ensure_subscription_skips_create_when_present() -> None:
    with respx.mock(assert_all_called=False) as router:
        _oauth(router)
        router.route(
            method="GET", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "callback_url": "https://strava-merger.example.com/webhook",
                    }
                ],
            )
        )
        create = router.route(
            method="POST", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(return_value=httpx.Response(201, json={"id": 9}))
        ctx = await _build_ctx()
        try:
            await _ensure_subscription(ctx, announce_present=True)
        finally:
            await ctx.http.aclose()
    assert not create.called


async def test_subscription_loop_logs_body_and_retries(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    # Spin fast so the loop's retry/backoff doesn't stall the test.
    monkeypatch.setenv("SUBSCRIPTION_RETRY_MIN_SECONDS", "0")
    monkeypatch.setenv("SUBSCRIPTION_CHECK_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()

    with respx.mock(assert_all_called=False) as router:
        _oauth(router)
        router.route(
            method="GET", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(return_value=httpx.Response(200, json=[]))
        router.route(
            method="POST", host=HOST, path="/api/v3/push_subscriptions"
        ).mock(return_value=httpx.Response(400, text=NOT_VERIFIABLE))

        ctx = await _build_ctx()
        caplog.set_level(logging.WARNING, logger="strava_merger")
        task = asyncio.create_task(_subscription_loop(ctx))
        try:
            # Let the loop fail and retry several times (interval/backoff are 0).
            for _ in range(500):
                await asyncio.sleep(0)
                failures = [
                    r
                    for r in caplog.records
                    if "ensure webhook subscription" in r.getMessage()
                ]
                if len(failures) >= 2:
                    break
            logged = any(
                "not verifiable" in str(getattr(r, "response_body", ""))
                for r in caplog.records
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await ctx.http.aclose()
            get_settings.cache_clear()

    assert logged, "loop should log Strava's 400 response body, not just the status"
    # It retried rather than giving up after the first failure.
    failures = [
        r for r in caplog.records if "ensure webhook subscription" in r.getMessage()
    ]
    assert len(failures) >= 2

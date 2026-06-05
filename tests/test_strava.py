"""Tests for the StravaClient upload transport (multipart JSON strength file)."""

from __future__ import annotations

import logging

import httpx
import respx

from app.auth import TokenManager
from app.config import get_settings
from app.strava import StravaClient

HOST = "www.strava.com"


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


async def test_create_upload_sends_multipart_json_file() -> None:
    with respx.mock(assert_all_called=False) as router:
        _oauth(router)
        up = router.route(method="POST", host=HOST, path="/api/v3/uploads").mock(
            return_value=httpx.Response(201, json={"id": 999, "status": "processing"})
        )
        settings = get_settings()
        logger = logging.getLogger("test_strava")
        http = httpx.AsyncClient()
        tokens = TokenManager(settings, logger)
        strava = StravaClient(settings, tokens, http, logger)
        payload = {
            "version": "1.0",
            "sets": [
                {"exercise_type": "BARBELL_DEADLIFT", "repetitions": 6, "weight": 55.0}
            ],
        }
        try:
            result = await strava.create_upload(
                payload,
                sport_type="WeightTraining",
                external_id="merged-111-222-1780000000",
                name="Strength B",
                description="Logged with hevyapp.com\nDeadlift",
            )
        finally:
            await http.aclose()

    assert result.status.id == 999

    req = up.calls.last.request
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content.decode("utf-8", errors="replace")
    # data_type=json and sport_type are form fields; the JSON payload is the file.
    assert 'name="data_type"' in body and "json" in body
    assert 'name="sport_type"' in body and "WeightTraining" in body
    assert 'name="file"' in body
    assert '"exercise_type": "BARBELL_DEADLIFT"' in body
    # external_id (unique), name (title) and description ride as form fields.
    assert 'name="external_id"' in body and "merged-111-222-1780000000" in body
    assert 'name="name"' in body and "Strength B" in body
    assert 'name="description"' in body and "hevyapp.com" in body

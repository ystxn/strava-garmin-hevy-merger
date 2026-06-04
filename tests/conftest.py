"""Shared fixtures and builders for the test suite."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.models import StravaActivity

# Base environment so `Settings()` validates in any test that needs it.
BASE_ENV = {
    "STRAVA_CLIENT_ID": "test-client-id",
    "STRAVA_CLIENT_SECRET": "test-client-secret",
    "STRAVA_REFRESH_TOKEN": "test-refresh-token",
    "STRAVA_WEBHOOK_VERIFY_TOKEN": "test-verify-token",
    "PUBLIC_BASE_URL": "https://strava-merger.example.com",
}


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide required env vars and reset the settings cache around each test."""
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test")


def make_activity(**overrides: Any) -> StravaActivity:
    """Build a StravaActivity with sensible defaults for tests."""
    data: dict[str, Any] = {
        "id": 1,
        "type": "WeightTraining",
        "sport_type": "WeightTraining",
        "start_date": "2026-01-01T10:00:00Z",
        "elapsed_time": 3600,
        "has_heartrate": False,
        "athlete": {"id": 42},
        "sets": [],
    }
    data.update(overrides)
    return StravaActivity.model_validate(data)


def garmin_activity(**overrides: Any) -> StravaActivity:
    base: dict[str, Any] = {
        "id": 111,
        "external_id": "garmin_push_1234567890.fit",
        "device_name": "Garmin Forerunner 965",
        "has_heartrate": True,
        "sets": [],
    }
    base.update(overrides)
    return make_activity(**base)


def hevy_activity(**overrides: Any) -> StravaActivity:
    base: dict[str, Any] = {
        "id": 222,
        "external_id": "hevy-abc123",
        "has_heartrate": False,
        "sets": [
            {"exercise": {"name": "Bench Press"}, "reps": 5, "weight": 80.0,
             "weight_units": "kilograms"},
            {"exercise": {"name": "Back Squat"}, "reps": 5, "weight": 100.0},
        ],
    }
    base.update(overrides)
    return make_activity(**base)

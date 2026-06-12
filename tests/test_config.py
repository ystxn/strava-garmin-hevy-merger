"""Tests for configuration loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def _settings(**overrides: str) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_defaults(settings: Settings) -> None:
    assert settings.pending_ttl_seconds == 600
    assert settings.match_window_seconds == 300
    assert settings.log_level == "INFO"


def test_log_level_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    get_settings.cache_clear()
    assert get_settings().log_level == "DEBUG"


def test_log_level_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_public_base_url_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")
    get_settings.cache_clear()
    s = get_settings()
    assert s.public_base_url == "https://example.com"
    assert s.webhook_callback_url == "https://example.com/webhook"


def test_public_dict_excludes_secrets(settings: Settings) -> None:
    public = settings.public_dict()
    assert "strava_client_secret" not in public
    assert "strava_refresh_token" not in public
    assert public["match_window_seconds"] == 300

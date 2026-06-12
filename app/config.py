"""Application configuration, loaded from environment variables.

All values documented in the spec's "Configuration" table are surfaced here,
plus a handful of internal tunables (poll cadence, token-refresh buffer, etc.)
that have sensible defaults and rarely need changing.

Secrets and config alike are read from the process environment. In Kubernetes
the secrets arrive via a Secret (envFrom secretRef) and the rest via a
ConfigMap (envFrom configMapRef); locally they can come from a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Resolved configuration for the service.

    Field names map case-insensitively to the upper-cased environment
    variables in the spec (e.g. ``strava_client_id`` <- ``STRAVA_CLIENT_ID``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Secrets (Kubernetes Secret) --------------------------------------
    strava_client_id: str
    strava_client_secret: str
    strava_refresh_token: str
    strava_webhook_verify_token: str

    # --- Public config (Kubernetes ConfigMap) -----------------------------
    public_base_url: str = Field(
        ...,
        description="Publicly reachable base URL, e.g. https://strava-merger.example.com",
    )
    pending_ttl_seconds: int = 600
    match_window_seconds: int = 300
    log_level: str = "INFO"

    # --- Internal tunables (not in the spec env table; safe defaults) ------
    # How often the TTL-eviction loop runs.
    eviction_interval_seconds: int = 120
    # Poll cadence and ceiling while waiting for an upload to be processed.
    upload_poll_interval_seconds: float = 1.0
    upload_poll_max_attempts: int = 60
    # Refresh the access token this many seconds before its stated expiry.
    token_refresh_buffer_seconds: int = 300
    # Whether to verify/create the Strava push subscription on startup. Disable
    # for local development where PUBLIC_BASE_URL is not reachable by Strava.
    manage_webhook_subscription: bool = True
    # Steady-state cadence for re-verifying the push subscription exists, and
    # the floor for the exponential backoff used while it can't be created. A
    # fresh deploy can 400 for a minute or two while the ingress/TLS cert comes
    # up (Strava validates the callback synchronously), so we never give up.
    subscription_check_interval_seconds: int = 300
    subscription_retry_min_seconds: float = 5.0
    # Strava sport_type values treated as "strength" activities.
    strength_sport_types: tuple[str, ...] = ("WeightTraining",)
    # Preferred weight unit emitted in the merged payload.
    weight_units: str = "kilograms"
    # exercise_type used when a Hevy exercise name can't be mapped to a Strava
    # enum (a per-category generic is tried first). Must be a value Strava
    # actually renders: WEIGHT_TRAINING_GENERIC shows as "Unknown" (verified
    # live), whereas TOTAL_BODY_GENERIC renders "Total Body". Configurable so it
    # can be corrected without a code change if Strava changes the enum set.
    fallback_exercise_type: str = "TOTAL_BODY_GENERIC"
    # Bearer token guarding the manual POST /merge endpoint. When unset, the
    # endpoint is disabled (returns 503).
    admin_token: Optional[str] = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        normalised = str(value).strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalised not in allowed:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalised

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return str(value).rstrip("/")

    @property
    def webhook_callback_url(self) -> str:
        """The full URL Strava should POST webhook events to."""
        return f"{self.public_base_url}/webhook"

    def public_dict(self) -> dict[str, object]:
        """Config values safe to log at startup (secrets excluded)."""
        return {
            "public_base_url": self.public_base_url,
            "pending_ttl_seconds": self.pending_ttl_seconds,
            "match_window_seconds": self.match_window_seconds,
            "log_level": self.log_level,
            "eviction_interval_seconds": self.eviction_interval_seconds,
            "upload_poll_interval_seconds": self.upload_poll_interval_seconds,
            "upload_poll_max_attempts": self.upload_poll_max_attempts,
            "token_refresh_buffer_seconds": self.token_refresh_buffer_seconds,
            "manage_webhook_subscription": self.manage_webhook_subscription,
            "subscription_check_interval_seconds": self.subscription_check_interval_seconds,
            "subscription_retry_min_seconds": self.subscription_retry_min_seconds,
            "strength_sport_types": list(self.strength_sport_types),
            "weight_units": self.weight_units,
            "fallback_exercise_type": self.fallback_exercise_type,
            "admin_endpoint_enabled": self.admin_token is not None,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (validated on first access)."""
    return Settings()  # type: ignore[call-arg]

"""Pydantic models for the Strava payloads this service consumes.

Response models use ``extra="allow"`` so that unexpected fields are preserved
in ``model_extra`` rather than dropped; callers can then emit a schema-drift
warning (see :func:`app.logging_setup.log_unexpected_fields`). The models stay
deliberately permissive — Strava's exact strength/set schema is partly
undocumented, and the spec's whole diagnostic-logging strategy assumes the
shape may surprise us on first deploy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .hevy import parse_hevy_sets

# --------------------------------------------------------------------------- #
# Webhook events
# --------------------------------------------------------------------------- #


class WebhookEvent(BaseModel):
    """A Strava push-subscription event delivered to POST /webhook."""

    model_config = ConfigDict(extra="allow")

    # Plain strings (not enums) so an unanticipated value from Strava is parsed
    # and then filtered in process_event, rather than rejected as malformed.
    # Known aspect_type: create|update|delete. Known object_type: activity|athlete.
    aspect_type: str
    object_type: str
    object_id: int
    owner_id: int
    subscription_id: int | None = None
    event_time: int | None = None
    updates: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Activities, sets and streams
# --------------------------------------------------------------------------- #


class Athlete(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | None = None


class StravaSet(BaseModel):
    """A single strength set as returned inside a Strava activity.

    Field names are guesses against an undocumented shape, so everything is
    optional and ``extra="allow"`` keeps anything we didn't anticipate. The
    merger reads from this defensively.
    """

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    reps: int | None = None
    weight: float | None = None
    weight_units: str | None = None
    exercise: dict[str, Any] | None = None
    exercise_name: str | None = None
    name: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    start_date: str | None = None
    elapsed_time: int | None = None


class StravaActivity(BaseModel):
    """Activity detail from GET /api/v3/activities/{id}."""

    model_config = ConfigDict(extra="allow")

    id: int
    external_id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    sport_type: Optional[str] = None
    start_date: Optional[str] = None
    start_date_local: Optional[str] = None
    elapsed_time: Optional[int] = None
    moving_time: Optional[int] = None
    utc_offset: Optional[float] = None
    device_name: Optional[str] = None
    description: Optional[str] = None
    has_heartrate: bool = False
    average_heartrate: Optional[float] = None
    max_heartrate: Optional[float] = None
    athlete: Athlete = Field(default_factory=Athlete)
    sets: list[StravaSet] = Field(default_factory=list)

    # Remaining DetailedActivity fields Strava returns that the merge doesn't
    # use. They were observed verbatim in production activity-detail payloads
    # and are declared here only so the schema-drift canary
    # (log_unexpected_fields) stops firing on every fetch and instead flags
    # genuinely new/undocumented fields. Types are kept permissive (numerics
    # widened to float, containers left loose) so a future type change can't
    # escalate a benign warning into a fetch-breaking ValidationError.
    resource_state: Optional[int] = None
    workout_type: Optional[int] = None
    upload_id: Optional[int] = None
    upload_id_str: Optional[str] = None
    distance: Optional[float] = None
    average_speed: Optional[float] = None
    max_speed: Optional[float] = None
    average_cadence: Optional[float] = None
    average_temp: Optional[float] = None
    average_watts: Optional[float] = None
    max_watts: Optional[float] = None
    weighted_average_watts: Optional[float] = None
    device_watts: Optional[bool] = None
    kilojoules: Optional[float] = None
    calories: Optional[float] = None
    total_elevation_gain: Optional[float] = None
    elev_high: Optional[float] = None
    elev_low: Optional[float] = None
    suffer_score: Optional[float] = None
    perceived_exertion: Optional[float] = None
    prefer_perceived_exertion: Optional[bool] = None
    achievement_count: Optional[int] = None
    kudos_count: Optional[int] = None
    comment_count: Optional[int] = None
    athlete_count: Optional[int] = None
    photo_count: Optional[int] = None
    total_photo_count: Optional[int] = None
    pr_count: Optional[int] = None
    has_kudoed: Optional[bool] = None
    commute: Optional[bool] = None
    manual: Optional[bool] = None
    private: Optional[bool] = None
    flagged: Optional[bool] = None
    trainer: Optional[bool] = None
    from_accepted_tag: Optional[bool] = None
    hide_from_home: Optional[bool] = None
    heartrate_opt_out: Optional[bool] = None
    display_hide_heartrate_option: Optional[bool] = None
    visibility: Optional[str] = None
    sport_image_visibility: Optional[str] = None
    timezone: Optional[str] = None
    embed_token: Optional[str] = None
    gear_id: Optional[str] = None
    location_city: Optional[str] = None
    location_state: Optional[str] = None
    location_country: Optional[str] = None
    map: Optional[dict[str, Any]] = None
    photos: Optional[dict[str, Any]] = None
    similar_activities: Optional[dict[str, Any]] = None
    start_latlng: list[Any] = Field(default_factory=list)
    end_latlng: list[Any] = Field(default_factory=list)
    available_zones: list[Any] = Field(default_factory=list)
    stats_visibility: list[Any] = Field(default_factory=list)
    segment_efforts: list[Any] = Field(default_factory=list)
    best_efforts: list[Any] = Field(default_factory=list)
    laps: list[Any] = Field(default_factory=list)
    splits_metric: list[Any] = Field(default_factory=list)
    splits_standard: list[Any] = Field(default_factory=list)

    @property
    def athlete_id(self) -> int | None:
        return self.athlete.id if self.athlete else None

    @property
    def has_sets(self) -> bool:
        # Strava returns no structured `sets` array for strength workouts; Hevy
        # writes the sets into the free-text description, so fall back to that.
        return bool(self.sets) or bool(parse_hevy_sets(self.description))

    def start_datetime(self) -> datetime | None:
        """Parse ``start_date`` (UTC ISO-8601, e.g. '2026-01-01T10:00:00Z')."""
        if not self.start_date:
            return None
        raw = self.start_date.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


class StreamData(BaseModel):
    """One stream channel from GET /activities/{id}/streams?key_by_type=true."""

    model_config = ConfigDict(extra="allow")

    data: list[Any] = Field(default_factory=list)
    series_type: Optional[str] = None
    original_size: Optional[int] = None
    resolution: Optional[str] = None


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #


class UploadStatus(BaseModel):
    """Status object from POST /uploads and GET /uploads/{id}."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    id_str: Optional[str] = None
    external_id: Optional[str] = None
    error: Optional[str] = None
    status: Optional[str] = None
    activity_id: Optional[int] = None


# --------------------------------------------------------------------------- #
# Push subscriptions
# --------------------------------------------------------------------------- #


class PushSubscription(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    callback_url: Optional[str] = None
    application_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# OAuth token refresh
# --------------------------------------------------------------------------- #


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    access_token: str
    refresh_token: str
    expires_at: int
    expires_in: int | None = None
    token_type: str | None = None

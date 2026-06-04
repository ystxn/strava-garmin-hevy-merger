"""FastAPI application: webhook endpoints + the merge orchestration pipeline.

Webhook POSTs are acknowledged immediately (Strava expects a fast 200) and the
work happens in a background task. Each pipeline stage is wrapped so that any
failure emits the structured, stage-specific ERROR the spec defines — enough
context to diagnose from logs alone, without reproducing the failure.

Pipeline: webhook_ingest -> activity_fetch -> (match) -> source_identification
-> stream_fetch -> payload_build -> upload -> delete.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .auth import TokenManager
from .logging_setup import (
    clip_body,
    configure_logging,
    log_stage_error,
    log_stage_event,
    log_unexpected_fields,
)
from .matcher import Matcher, PendingActivity, _utcnow
from .merger import (
    build_merged_payload,
    identify_pair,
    identify_source,
    is_probably_merged,
    required_payload_problems,
)
from .models import StravaActivity, WebhookEvent
from .strava import StravaApiError, StravaClient

LOGGER_NAME = "strava_merger"

# Fields we expect every activity-detail response to carry; used to compute
# `missing_fields` for the activity_fetch diagnostic.
EXPECTED_ACTIVITY_FIELDS = [
    "id",
    "external_id",
    "type",
    "sport_type",
    "start_date",
    "elapsed_time",
    "has_heartrate",
    "athlete",
    "sets",
]


@dataclass
class AppContext:
    settings: Settings
    logger: logging.Logger
    http: httpx.AsyncClient
    tokens: TokenManager
    strava: StravaClient
    matcher: Matcher
    # Activity IDs we should ignore on future webhooks: inputs we've already
    # consumed into a merge, plus merged outputs we created. Prevents
    # re-merging the same pair and reacting to our own upload's create event.
    # Grows by a handful of IDs per workout — negligible for a single athlete.
    processed_activity_ids: set[int] = field(default_factory=set)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Lifespan: wire dependencies, refresh token, start background loops
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(LOGGER_NAME)

    logger.info(
        "Starting strava-merger",
        extra={"stage": "startup", "config": settings.public_dict()},
    )

    http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    tokens = TokenManager(settings, logger)
    strava = StravaClient(settings, tokens, http, logger)
    matcher = Matcher(settings, logger)
    ctx = AppContext(
        settings=settings,
        logger=logger,
        http=http,
        tokens=tokens,
        strava=strava,
        matcher=matcher,
    )
    app.state.ctx = ctx

    # Refresh the access token up front so misconfigured credentials surface
    # immediately. Don't crash the process on failure — staying up keeps the
    # logs reachable and lets a later refresh recover.
    try:
        await tokens.refresh_now(http)
    except Exception:
        logger.error(
            "Initial token refresh failed; the service cannot call Strava "
            "until credentials are valid",
            extra={"stage": "startup", "traceback": traceback.format_exc()},
        )

    # The eviction loop runs forever; track it apart from the per-event
    # background tasks so callers can drain the latter without awaiting it.
    eviction_task = asyncio.create_task(matcher.eviction_loop())

    if settings.manage_webhook_subscription:
        _schedule(ctx, _setup_subscription(ctx))

    try:
        yield
    finally:
        all_tasks = [eviction_task, *ctx.background_tasks]
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await http.aclose()
        logger.info("Stopped strava-merger", extra={"stage": "shutdown"})


app = FastAPI(title="Strava Strength Activity Merger", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
async def verify_webhook(request: Request) -> JSONResponse:
    """Strava subscription validation handshake (GET with hub.* params)."""
    ctx: AppContext = request.app.state.ctx
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == ctx.settings.strava_webhook_verify_token:
        log_stage_event(
            ctx.logger,
            "Webhook subscription validated",
            stage="webhook_validation",
        )
        return JSONResponse({"hub.challenge": challenge})

    log_stage_event(
        ctx.logger,
        "Webhook validation failed (bad mode or verify token)",
        stage="webhook_validation",
        level=logging.WARNING,
        hub_mode=mode,
    )
    return JSONResponse({"error": "verification failed"}, status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> JSONResponse:
    """Acknowledge the event fast (200), then process it in the background."""
    ctx: AppContext = request.app.state.ctx
    raw = await request.body()
    try:
        event = WebhookEvent.model_validate_json(raw)
    except Exception as exc:
        log_stage_error(
            ctx.logger,
            "Failed to parse webhook event",
            stage="webhook_ingest",
            raw_body=raw.decode("utf-8", errors="replace"),
            headers={
                "content-type": request.headers.get("content-type"),
                "content-length": request.headers.get("content-length"),
                "user-agent": request.headers.get("user-agent"),
            },
            parse_error=f"{type(exc).__name__}: {exc}",
        )
        # Acknowledge anyway: a malformed event is not worth Strava retrying.
        return JSONResponse({"status": "ignored"})

    if event.model_extra:
        log_unexpected_fields(
            ctx.logger, context="webhook_event", unexpected=event.model_extra
        )

    log_stage_event(
        ctx.logger,
        "Webhook event received",
        stage="webhook_ingest",
        athlete_id=event.owner_id,
        activity_id=event.object_id,
        object_type=event.object_type,
        aspect_type=event.aspect_type,
    )

    _schedule(ctx, process_event(ctx, event))
    return JSONResponse({"status": "received"})


# --------------------------------------------------------------------------- #
# Background task plumbing
# --------------------------------------------------------------------------- #


def _schedule(ctx: AppContext, coro: Any) -> None:
    """Fire-and-forget a coroutine, keeping a reference so it isn't GC'd."""
    task = asyncio.create_task(coro)
    ctx.background_tasks.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        ctx.background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            exc = t.exception()
            ctx.logger.error(
                "Background task crashed",
                extra={
                    "stage": "background",
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            )

    task.add_done_callback(_done)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


async def process_event(ctx: AppContext, event: WebhookEvent) -> None:
    """Handle one webhook event: fetch, classify, buffer, and merge on a match."""
    log = ctx.logger

    if event.object_type != "activity" or event.aspect_type != "create":
        log_stage_event(
            log,
            "Ignoring non-activity-create event",
            stage="webhook_ingest",
            level=logging.DEBUG,
            athlete_id=event.owner_id,
            activity_id=event.object_id,
            object_type=event.object_type,
            aspect_type=event.aspect_type,
        )
        return

    activity_id = event.object_id
    athlete_id = event.owner_id

    if activity_id in ctx.processed_activity_ids:
        log_stage_event(
            log,
            "Ignoring webhook for an activity we already processed/created",
            stage="webhook_ingest",
            level=logging.DEBUG,
            athlete_id=athlete_id,
            activity_id=activity_id,
        )
        return

    # --- Stage: activity_fetch -------------------------------------------
    try:
        fetched = await ctx.strava.get_activity(activity_id)
    except StravaApiError as exc:
        log_stage_error(
            log,
            "Failed to fetch activity detail",
            stage="activity_fetch",
            athlete_id=athlete_id,
            activity_id=activity_id,
            http_method=exc.method,
            url=exc.url,
            http_status=exc.status_code,
            expected_fields=EXPECTED_ACTIVITY_FIELDS,
            missing_fields=_missing_fields(exc.body),
            error=str(exc),
            parse_error=exc.parse_error,
            **clip_body(exc.body),
        )
        return
    except Exception as exc:
        log_stage_error(
            log,
            "Unexpected error fetching activity detail",
            stage="activity_fetch",
            athlete_id=athlete_id,
            activity_id=activity_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    activity = fetched.activity
    if activity.model_extra:
        log_unexpected_fields(
            log,
            context="activity_detail",
            unexpected=activity.model_extra,
            activity_id=activity_id,
        )

    # Only strength activities are in scope.
    sport = activity.sport_type or activity.type
    if sport not in ctx.settings.strength_sport_types:
        log_stage_event(
            log,
            f"Ignoring non-strength activity (sport_type={sport!r})",
            stage="classify",
            level=logging.DEBUG,
            athlete_id=athlete_id,
            activity_id=activity_id,
        )
        return

    # Skip anything that already looks merged (both sets and HR).
    if is_probably_merged(activity):
        log_stage_event(
            log,
            "Activity already has both sets and HR; skipping (looks merged)",
            stage="classify",
            athlete_id=athlete_id,
            activity_id=activity_id,
        )
        return

    source = identify_source(activity)
    start_dt = activity.start_datetime()
    if source is None or start_dt is None:
        log_stage_event(
            log,
            "Cannot buffer activity: source or start time undetermined",
            stage="classify",
            level=logging.WARNING,
            athlete_id=athlete_id,
            activity_id=activity_id,
            identified_source=source,
            external_id=activity.external_id,
            device_name=activity.device_name,
            has_sets=activity.has_sets,
            has_heartrate=activity.has_heartrate,
            start_date=activity.start_date,
        )
        return

    pending = PendingActivity(
        activity_id=activity_id,
        source=source,
        start_time=start_dt,
        received_at=_utcnow(),
        activity=activity,
    )

    # Critical section: registering + matching must be atomic per athlete so
    # two near-simultaneous events can't both claim the pair (spec note #3).
    async with ctx.matcher.lock_for(athlete_id):
        partner = ctx.matcher.add_and_match(athlete_id, pending)
        if partner is not None:
            ctx.processed_activity_ids.add(activity_id)
            ctx.processed_activity_ids.add(partner.activity_id)

    if partner is None:
        log_stage_event(
            log,
            f"Buffered {source} activity; waiting for its partner",
            stage="match",
            athlete_id=athlete_id,
            activity_id=activity_id,
            source=source,
            pending_count=ctx.matcher.pending_count(),
        )
        return

    log_stage_event(
        log,
        "Match found; starting merge",
        stage="match",
        athlete_id=athlete_id,
        activity_id=activity_id,
        partner_activity_id=partner.activity_id,
    )
    await run_merge(ctx, athlete_id, partner.activity, activity)


async def run_merge(
    ctx: AppContext,
    athlete_id: int,
    activity_a: StravaActivity,
    activity_b: StravaActivity,
) -> None:
    """Identify the pair, fetch HR, build, upload, and optionally delete."""
    log = ctx.logger
    settings = ctx.settings

    # --- Stage: source_identification ------------------------------------
    pair = identify_pair(activity_a, activity_b)
    if pair is None:
        log_stage_error(
            log,
            "Could not identify Garmin/Hevy roles; leaving both activities "
            "intact (fail-safe no-op)",
            stage="source_identification",
            athlete_id=athlete_id,
            activity_a_id=activity_a.id,
            activity_a_external_id=activity_a.external_id,
            activity_a_device_name=activity_a.device_name,
            activity_a_has_sets=activity_a.has_sets,
            activity_a_has_heartrate=activity_a.has_heartrate,
            activity_b_id=activity_b.id,
            activity_b_external_id=activity_b.external_id,
            activity_b_device_name=activity_b.device_name,
            activity_b_has_sets=activity_b.has_sets,
            activity_b_has_heartrate=activity_b.has_heartrate,
            reason="no assignment satisfies hevy.has_sets and garmin.has_heartrate",
        )
        return

    garmin, hevy = pair
    g_id, h_id = garmin.id, hevy.id
    log_stage_event(
        log,
        "Identified activity sources",
        stage="source_identification",
        athlete_id=athlete_id,
        garmin_activity_id=g_id,
        hevy_activity_id=h_id,
    )

    # --- Stage: stream_fetch (Garmin HR + time) --------------------------
    keys = ["heartrate", "time"]
    try:
        streams = await ctx.strava.get_streams(g_id, keys)
    except StravaApiError as exc:
        log_stage_error(
            log,
            "Failed to fetch heart-rate stream",
            stage="stream_fetch",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            activity_id=g_id,
            url=exc.url,
            http_status=exc.status_code,
            keys_requested=keys,
            keys_returned=[],
            heartrate_sample_count=None,
            time_sample_count=None,
            **clip_body(exc.body),
        )
        return

    hr_stream = streams.streams.get("heartrate")
    time_stream = streams.streams.get("time")
    hr_data = list(hr_stream.data) if hr_stream else []
    time_data = list(time_stream.data) if time_stream else []

    if not hr_data:
        log_stage_error(
            log,
            "Garmin activity returned no heart-rate samples; cannot merge",
            stage="stream_fetch",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            activity_id=g_id,
            url=streams.url,
            http_status=200,
            keys_requested=keys,
            keys_returned=streams.keys_returned,
            heartrate_sample_count=len(hr_data),
            time_sample_count=len(time_data),
            include_traceback=False,
            **clip_body(json.dumps(streams.raw)),
        )
        return

    # --- Stage: payload_build --------------------------------------------
    try:
        payload = build_merged_payload(garmin, hevy, hr_data, time_data, settings)
    except Exception as exc:
        log_stage_error(
            log,
            "Failed to build merged payload",
            stage="payload_build",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            hevy_sets_raw=[s.model_dump() for s in hevy.sets],
            garmin_start_date=garmin.start_date,
            garmin_elapsed_time=garmin.elapsed_time,
            heartrate_stream_length=len(hr_data),
            time_stream_length=len(time_data),
            constructed_payload=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    problems = required_payload_problems(payload)
    if problems:
        log_stage_error(
            log,
            f"Merged payload is incomplete: {'; '.join(problems)}",
            stage="payload_build",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            hevy_sets_raw=[s.model_dump() for s in hevy.sets],
            garmin_start_date=garmin.start_date,
            garmin_elapsed_time=garmin.elapsed_time,
            heartrate_stream_length=len(hr_data),
            time_stream_length=len(time_data),
            constructed_payload=payload,
            error="; ".join(problems),
            include_traceback=False,
        )
        return

    log_stage_event(
        log,
        f"Built merged payload with {len(payload['sets'])} sets and "
        f"{len(hr_data)} HR samples",
        stage="payload_build",
        athlete_id=athlete_id,
        garmin_activity_id=g_id,
        hevy_activity_id=h_id,
    )

    # --- Stage: upload ---------------------------------------------------
    merged_id = await _upload_and_poll(ctx, payload, athlete_id, g_id, h_id)
    if merged_id is None:
        return

    ctx.processed_activity_ids.add(merged_id)
    log_stage_event(
        log,
        "Upload succeeded",
        stage="upload",
        athlete_id=athlete_id,
        garmin_activity_id=g_id,
        hevy_activity_id=h_id,
        merged_activity_id=merged_id,
    )

    # --- Stage: delete ---------------------------------------------------
    if settings.delete_originals:
        await _delete_original(ctx, garmin.id, "garmin", athlete_id, g_id, h_id)
        await _delete_original(ctx, hevy.id, "hevy", athlete_id, g_id, h_id)
        log_stage_event(
            log,
            "Deleted both source activities after successful merge",
            stage="delete",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            merged_activity_id=merged_id,
        )
    else:
        log_stage_event(
            log,
            "DELETE_ORIGINALS=false; leaving originals intact for manual "
            "cleanup",
            stage="delete",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            merged_activity_id=merged_id,
        )


async def _upload_and_poll(
    ctx: AppContext,
    payload: dict[str, Any],
    athlete_id: int,
    g_id: int | None,
    h_id: int | None,
) -> int | None:
    """POST the upload, then poll until it yields an activity id or fails."""
    log = ctx.logger
    settings = ctx.settings

    try:
        result = await ctx.strava.create_upload(payload)
    except StravaApiError as exc:
        log_stage_error(
            log,
            "Upload POST failed",
            stage="upload",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            http_method="POST",
            url=exc.url,
            request_payload=payload,
            http_status=exc.status_code,
            upload_id=None,
            poll_attempts=0,
            final_poll_response=None,
            strava_error_message=None,
            parse_error=exc.parse_error,
            **clip_body(exc.body),
        )
        return None

    status = result.status
    upload_id = status.id

    if status.error:
        log_stage_error(
            log,
            "Strava reported an upload error immediately",
            stage="upload",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            http_method="POST",
            url=result.url,
            request_payload=payload,
            http_status=201,
            upload_id=upload_id,
            poll_attempts=0,
            final_poll_response=result.raw,
            strava_error_message=status.error,
            include_traceback=False,
        )
        return None

    if status.activity_id:
        return status.activity_id

    if upload_id is None:
        log_stage_error(
            log,
            "Upload response contained neither an upload id nor an activity id",
            stage="upload",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            http_method="POST",
            url=result.url,
            request_payload=payload,
            http_status=201,
            upload_id=None,
            poll_attempts=0,
            final_poll_response=result.raw,
            strava_error_message=None,
            include_traceback=False,
        )
        return None

    # Poll GET /uploads/{id} until activity_id is set or error appears.
    attempts = 0
    last = result
    while attempts < settings.upload_poll_max_attempts:
        attempts += 1
        await asyncio.sleep(settings.upload_poll_interval_seconds)
        try:
            last = await ctx.strava.get_upload(upload_id)
        except StravaApiError as exc:
            log_stage_error(
                log,
                "Polling the upload status failed",
                stage="upload",
                athlete_id=athlete_id,
                garmin_activity_id=g_id,
                hevy_activity_id=h_id,
                http_method="GET",
                url=exc.url,
                request_payload=payload,
                http_status=exc.status_code,
                upload_id=upload_id,
                poll_attempts=attempts,
                final_poll_response=exc.body,
                strava_error_message=None,
                **clip_body(exc.body),
            )
            return None

        if last.status.error:
            log_stage_error(
                log,
                "Strava reported an upload error during polling",
                stage="upload",
                athlete_id=athlete_id,
                garmin_activity_id=g_id,
                hevy_activity_id=h_id,
                http_method="GET",
                url=last.url,
                request_payload=payload,
                http_status=200,
                upload_id=upload_id,
                poll_attempts=attempts,
                final_poll_response=last.raw,
                strava_error_message=last.status.error,
                include_traceback=False,
            )
            return None

        if last.status.activity_id:
            return last.status.activity_id

    # Exhausted attempts without a terminal result.
    log_stage_error(
        log,
        f"Upload polling timed out after {attempts} attempts",
        stage="upload",
        athlete_id=athlete_id,
        garmin_activity_id=g_id,
        hevy_activity_id=h_id,
        http_method="GET",
        url=last.url,
        request_payload=payload,
        http_status=200,
        upload_id=upload_id,
        poll_attempts=attempts,
        final_poll_response=last.raw,
        strava_error_message=None,
        include_traceback=False,
    )
    return None


async def _delete_original(
    ctx: AppContext,
    activity_id: int | None,
    source: str,
    athlete_id: int,
    g_id: int | None,
    h_id: int | None,
) -> None:
    log = ctx.logger
    if activity_id is None:
        return
    try:
        await ctx.strava.delete_activity(activity_id)
        log_stage_event(
            log,
            f"Deleted {source} source activity",
            stage="delete",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            activity_id=activity_id,
            source=source,
        )
    except StravaApiError as exc:
        log_stage_error(
            log,
            f"Failed to delete {source} source activity",
            stage="delete",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            activity_id=activity_id,
            source=source,
            http_status=exc.status_code,
            error=str(exc),
            **clip_body(exc.body),
        )
    except Exception as exc:
        log_stage_error(
            log,
            f"Unexpected error deleting {source} source activity",
            stage="delete",
            athlete_id=athlete_id,
            garmin_activity_id=g_id,
            hevy_activity_id=h_id,
            activity_id=activity_id,
            source=source,
            error=f"{type(exc).__name__}: {exc}",
        )


# --------------------------------------------------------------------------- #
# Webhook subscription management
# --------------------------------------------------------------------------- #


async def _setup_subscription(ctx: AppContext) -> None:
    """Verify the push subscription exists, creating it if needed.

    Runs as a background task (not awaited in startup) because creating a
    subscription makes Strava call back to our /webhook for validation — which
    only works once this server is actually accepting requests. We retry a few
    times to cover the brief window before the socket is serving.
    """
    log = ctx.logger
    settings = ctx.settings
    callback = settings.webhook_callback_url

    for attempt in range(1, 6):
        try:
            subs = await ctx.strava.list_subscriptions()
            existing = next(
                (s for s in subs if s.callback_url == callback), None
            )
            if existing:
                log_stage_event(
                    log,
                    "Webhook subscription already present",
                    stage="subscription_setup",
                    subscription_id=existing.id,
                    callback_url=callback,
                )
                return

            # Strava allows one subscription per application; clear any stale
            # one pointing elsewhere before creating ours.
            for stale in subs:
                log_stage_event(
                    log,
                    "Deleting stale subscription with a different callback",
                    stage="subscription_setup",
                    level=logging.WARNING,
                    subscription_id=stale.id,
                    stale_callback_url=stale.callback_url,
                )
                await ctx.strava.delete_subscription(stale.id)

            created = await ctx.strava.create_subscription(
                callback, settings.strava_webhook_verify_token
            )
            log_stage_event(
                log,
                "Created webhook subscription",
                stage="subscription_setup",
                subscription_id=created.id,
                callback_url=callback,
            )
            return
        except Exception as exc:
            log_stage_event(
                log,
                f"Subscription setup attempt {attempt} failed; will retry",
                stage="subscription_setup",
                level=logging.WARNING,
                error=f"{type(exc).__name__}: {exc}",
            )
            await asyncio.sleep(min(2**attempt, 30))

    log_stage_error(
        log,
        "Gave up creating the webhook subscription after retries; events will "
        "not arrive until this succeeds",
        stage="subscription_setup",
        callback_url=callback,
        include_traceback=False,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _missing_fields(body: str | None) -> list[str]:
    """Which EXPECTED_ACTIVITY_FIELDS are absent from a (JSON) response body."""
    if not body:
        return list(EXPECTED_ACTIVITY_FIELDS)
    try:
        parsed = json.loads(body)
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    return [f for f in EXPECTED_ACTIVITY_FIELDS if f not in parsed]

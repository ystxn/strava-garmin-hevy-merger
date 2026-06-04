"""Structured JSON logging configuration and diagnostic helpers.

The spec requires every log line to be JSON with a stable envelope
(``timestamp``, ``level``, ``stage``, ``athlete_id``, ``garmin_activity_id``,
``hevy_activity_id``, ``message``) plus stage-specific fields, full tracebacks
on errors, and a rule never to truncate response bodies except very large ones
(>50 KB -> log the first 10 KB and note the total size).

This module centralises that behaviour so the rest of the app just calls
``log_stage_error(...)`` / ``log_stage_event(...)`` with the relevant fields.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

from pythonjsonlogger import jsonlogger

# Bodies larger than this are clipped in logs; we keep the leading slice and
# annotate the original size so nothing silently disappears.
MAX_BODY_BYTES = 50 * 1024
HEAD_BYTES = 10 * 1024

# LogRecord attributes we must never collide with when injecting extra fields.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


def configure_logging(level: str = "INFO") -> None:
    """Install a single JSON-emitting stdout handler on the root logger."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(levelname)s %(name)s %(message)s",
        rename_fields={"levelname": "level"},
        # Emit an ISO-8601 "timestamp" field for every record.
        timestamp=True,
        # Skip the standard LogRecord attributes (incl. 3.12's taskName) from
        # auto-inclusion; we only want our envelope + explicit extra fields.
        reserved_attrs=_RESERVED,
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn installs its own handlers; route them through ours so access and
    # error logs are JSON too and there are no duplicate lines.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def clip_body(body: Any) -> dict[str, Any]:
    """Render a response body / payload for logging, clipping only if huge.

    Returns a dict so callers can splat it into ``extra``. The value is exposed
    under ``response_body``; when clipped, ``response_body_total_bytes`` and
    ``response_body_truncated`` are added.
    """
    if body is None:
        return {"response_body": None}
    text = body if isinstance(body, str) else str(body)
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= MAX_BODY_BYTES:
        return {"response_body": text}
    head = raw[:HEAD_BYTES].decode("utf-8", errors="replace")
    return {
        "response_body": head,
        "response_body_total_bytes": len(raw),
        "response_body_truncated": True,
    }


def _clean_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Rename any keys that would collide with reserved LogRecord attributes."""
    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        safe_key = f"field_{key}" if key in _RESERVED else key
        cleaned[safe_key] = value
    return cleaned


def _base_fields(
    stage: str,
    athlete_id: int | None,
    garmin_activity_id: int | None,
    hevy_activity_id: int | None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "athlete_id": athlete_id,
        "garmin_activity_id": garmin_activity_id,
        "hevy_activity_id": hevy_activity_id,
    }


def log_stage_event(
    logger: logging.Logger,
    message: str,
    *,
    stage: str,
    level: int = logging.INFO,
    athlete_id: int | None = None,
    garmin_activity_id: int | None = None,
    hevy_activity_id: int | None = None,
    **fields: Any,
) -> None:
    """Emit a structured, non-error log line for a pipeline stage."""
    extra = _base_fields(stage, athlete_id, garmin_activity_id, hevy_activity_id)
    extra.update(fields)
    logger.log(level, message, extra=_clean_extra(extra))


def log_stage_error(
    logger: logging.Logger,
    message: str,
    *,
    stage: str,
    athlete_id: int | None = None,
    garmin_activity_id: int | None = None,
    hevy_activity_id: int | None = None,
    include_traceback: bool = True,
    **fields: Any,
) -> None:
    """Emit a structured ERROR with the full traceback when one is active.

    ``include_traceback`` only attaches a traceback when an exception is
    currently being handled, so non-exceptional failures (e.g. an
    unidentifiable activity pair) don't get a meaningless "NoneType: None".
    """
    extra = _base_fields(stage, athlete_id, garmin_activity_id, hevy_activity_id)
    extra.update(fields)
    if include_traceback and sys.exc_info()[0] is not None:
        extra["traceback"] = traceback.format_exc()
    logger.error(message, extra=_clean_extra(extra))


def log_unexpected_fields(
    logger: logging.Logger,
    *,
    context: str,
    unexpected: dict[str, Any],
    activity_id: int | None = None,
) -> None:
    """Warn when a Strava response carries fields absent from our models.

    A changed/undocumented schema is a likely first-deploy failure mode, so we
    surface the field names and values rather than silently dropping them.
    """
    if not unexpected:
        return
    log_stage_event(
        logger,
        f"Strava response for {context} contained unexpected fields: "
        f"{sorted(unexpected.keys())}",
        stage="schema_drift",
        level=logging.WARNING,
        activity_id=activity_id,
        unexpected_fields=list(unexpected.keys()),
        unexpected_values={k: str(v)[:500] for k, v in unexpected.items()},
    )

"""Tests for structured logging helpers (clipping, reserved keys, tracebacks)."""

from __future__ import annotations

import json
import logging

from app.logging_setup import (
    HEAD_BYTES,
    MAX_BODY_BYTES,
    clip_body,
    configure_logging,
    log_stage_error,
)


def test_clip_body_small_passthrough() -> None:
    out = clip_body("hello")
    assert out == {"response_body": "hello"}


def test_clip_body_none() -> None:
    assert clip_body(None) == {"response_body": None}


def test_clip_body_large_is_clipped() -> None:
    big = "x" * (MAX_BODY_BYTES + 1000)
    out = clip_body(big)
    assert out["response_body_truncated"] is True
    assert out["response_body_total_bytes"] == len(big)
    assert len(out["response_body"].encode("utf-8")) <= HEAD_BYTES


def test_log_stage_error_emits_json_with_envelope(capsys) -> None:
    configure_logging("INFO")
    logger = logging.getLogger("test.logging")
    try:
        raise ValueError("boom")
    except ValueError:
        log_stage_error(
            logger,
            "something failed",
            stage="upload",
            athlete_id=42,
            garmin_activity_id=111,
            hevy_activity_id=222,
            http_status=500,
        )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["level"] == "ERROR"
    assert record["stage"] == "upload"
    assert record["athlete_id"] == 42
    assert record["garmin_activity_id"] == 111
    assert record["hevy_activity_id"] == 222
    assert record["message"] == "something failed"
    assert "timestamp" in record
    assert "traceback" in record
    assert "ValueError: boom" in record["traceback"]


def test_log_stage_error_no_traceback_without_active_exception(capsys) -> None:
    configure_logging("INFO")
    logger = logging.getLogger("test.logging2")
    log_stage_error(
        logger,
        "validation problem",
        stage="payload_build",
        include_traceback=True,
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert "traceback" not in record


def test_health_access_logs_are_suppressed(capsys) -> None:
    # uvicorn.access logs every request; /health probes fire constantly and
    # drown out real events, so they must be filtered while others pass.
    configure_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    fmt = '%s - "%s %s HTTP/%s" %d'
    access.info(fmt, "10.0.0.1:1", "GET", "/health", "1.1", 200)
    access.info(fmt, "10.0.0.1:2", "GET", "/health?probe=1", "1.1", 200)
    access.info(fmt, "10.0.0.1:3", "POST", "/webhook", "1.1", 200)
    out = capsys.readouterr().out
    assert "/health" not in out
    assert "/webhook" in out


def test_client_secret_is_redacted_in_logs(capsys) -> None:
    # httpx logs full request URLs at INFO; Strava's GET/DELETE put
    # client_secret in the query string, which must never reach the logs.
    configure_logging("INFO")
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s"',
        "GET",
        "https://www.strava.com/api/v3/push_subscriptions"
        "?client_id=255222&client_secret=topsecretvalue123",
        "HTTP/1.1 200 OK",
    )
    out = capsys.readouterr().out
    assert "topsecretvalue123" not in out
    assert "client_secret=<redacted>" in out
    assert "client_id=255222" in out  # non-secret identifier is preserved


def test_reserved_key_is_renamed(capsys) -> None:
    # "name" collides with a LogRecord attribute and must be renamed, not crash.
    configure_logging("INFO")
    logger = logging.getLogger("test.logging3")
    log_stage_error(
        logger,
        "reserved key test",
        stage="classify",
        name="should-be-renamed",
    )
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record.get("field_name") == "should-be-renamed"

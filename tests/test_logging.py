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

"""Tests for the pending buffer, matching, and TTL eviction."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.matcher import Matcher, PendingActivity

from .conftest import garmin_activity, hevy_activity

BASE = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _pending(
    activity_id: int,
    source: str,
    start: datetime,
    received: datetime | None = None,
    activity=None,
) -> PendingActivity:
    return PendingActivity(
        activity_id=activity_id,
        source=source,
        start_time=start,
        received_at=received or start,
        activity=activity
        or (garmin_activity() if source == "garmin" else hevy_activity()),
    )


def _matcher(settings: Settings) -> Matcher:
    return Matcher(settings, logging.getLogger("test"))


def test_first_activity_has_no_partner(settings: Settings) -> None:
    m = _matcher(settings)
    partner = m.add_and_match(42, _pending(111, "garmin", BASE))
    assert partner is None
    assert m.pending_count() == 1


def test_complementary_sources_match_within_window(settings: Settings) -> None:
    m = _matcher(settings)
    m.add_and_match(42, _pending(111, "garmin", BASE))
    partner = m.add_and_match(
        42, _pending(222, "hevy", BASE + timedelta(seconds=30))
    )
    assert partner is not None
    assert partner.activity_id == 111
    # Both removed once paired.
    assert m.pending_count() == 0


def test_same_source_does_not_match(settings: Settings) -> None:
    m = _matcher(settings)
    m.add_and_match(42, _pending(111, "garmin", BASE))
    partner = m.add_and_match(
        42, _pending(112, "garmin", BASE + timedelta(seconds=10))
    )
    assert partner is None
    assert m.pending_count() == 2


def test_outside_window_does_not_match(settings: Settings) -> None:
    m = _matcher(settings)  # match_window default 300s
    m.add_and_match(42, _pending(111, "garmin", BASE))
    partner = m.add_and_match(
        42, _pending(222, "hevy", BASE + timedelta(seconds=600))
    )
    assert partner is None
    assert m.pending_count() == 2


def test_different_athletes_do_not_match(settings: Settings) -> None:
    m = _matcher(settings)
    m.add_and_match(1, _pending(111, "garmin", BASE))
    partner = m.add_and_match(2, _pending(222, "hevy", BASE))
    assert partner is None


def test_duplicate_webhook_is_deduped(settings: Settings) -> None:
    m = _matcher(settings)
    m.add_and_match(42, _pending(111, "garmin", BASE))
    # Same activity id arrives again -> replaces, doesn't create a second.
    partner = m.add_and_match(42, _pending(111, "garmin", BASE))
    assert partner is None
    assert m.pending_count() == 1


def test_cross_boundary_match(settings: Settings) -> None:
    # 10:04:00 and 10:06:00 are 2 min apart (within the 5-min window) but fall
    # in different rigid 5-min buckets. A windowed scan still pairs them.
    m = _matcher(settings)
    m.add_and_match(42, _pending(111, "garmin", BASE + timedelta(minutes=4)))
    partner = m.add_and_match(
        42, _pending(222, "hevy", BASE + timedelta(minutes=6))
    )
    assert partner is not None


def test_evict_expired_removes_old_entries(
    settings: Settings, monkeypatch
) -> None:
    m = _matcher(settings)  # ttl default 600s
    now = datetime.now(timezone.utc)
    old = _pending(111, "garmin", BASE, received=now - timedelta(seconds=700))
    fresh = _pending(222, "hevy", BASE, received=now - timedelta(seconds=60))
    m.add_and_match(42, old)
    # add fresh as a same-source-different to avoid an accidental match
    fresh.source = "garmin"
    m.add_and_match(42, fresh)
    assert m.pending_count() == 2

    evicted = m.evict_expired()
    assert evicted == 1
    assert m.pending_count() == 1


def test_remove(settings: Settings) -> None:
    m = _matcher(settings)
    m.add_and_match(42, _pending(111, "garmin", BASE))
    m.remove(42, 111)
    assert m.pending_count() == 0

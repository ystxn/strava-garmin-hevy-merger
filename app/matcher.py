"""In-memory pending buffer, match logic and TTL eviction.

When a strength activity arrives we don't yet know if its partner (the same
workout synced from the *other* source) has arrived. We park it here keyed by
athlete and look for a complementary partner — same athlete, start time within
``MATCH_WINDOW_SECONDS``, a *different* source (one Garmin, one Hevy).

The spec sketches a dict keyed by ``athlete_id:start_time_bucket``. We key by
``athlete_id`` and scan that athlete's small list instead: rigid 5-minute
buckets would miss a pair that straddles a boundary (e.g. 10:04 and 10:06 fall
in different buckets yet are 2 minutes apart), whereas a windowed scan matches
them correctly. Volume is one athlete's workouts, so the scan is trivial.

No persistence: on restart the worst case is both activities remain on Strava
unmerged — exactly the pre-service status quo.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .models import StravaActivity


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PendingActivity:
    activity_id: int
    source: str  # "garmin" | "hevy"
    start_time: datetime
    received_at: datetime
    activity: StravaActivity  # cached detail, so we don't re-fetch at merge time


class Matcher:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._settings = settings
        self._log = logger
        self._pending: dict[int, list[PendingActivity]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, athlete_id: int) -> asyncio.Lock:
        """Per-athlete lock serialising the match/merge critical section."""
        lock = self._locks.get(athlete_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[athlete_id] = lock
        return lock

    def add_and_match(
        self, athlete_id: int, pending: PendingActivity
    ) -> PendingActivity | None:
        """Register ``pending`` and return a complementary partner if present.

        On a match BOTH activities are removed from the buffer and the partner
        is returned — the caller owns the pair and commits to processing it.
        Idempotent on ``activity_id``: a duplicate webhook replaces the prior
        entry rather than creating a second one.

        Must be called while holding ``lock_for(athlete_id)``.
        """
        entries = self._pending.setdefault(athlete_id, [])

        # Replace any existing entry for the same activity (duplicate webhook).
        entries[:] = [e for e in entries if e.activity_id != pending.activity_id]

        window = self._settings.match_window_seconds
        partner: PendingActivity | None = None
        for entry in entries:
            same_source = entry.source == pending.source
            within_window = (
                abs((entry.start_time - pending.start_time).total_seconds())
                <= window
            )
            if not same_source and within_window:
                partner = entry
                break

        if partner is not None:
            entries.remove(partner)
            if not entries:
                self._pending.pop(athlete_id, None)
            return partner

        entries.append(pending)
        return None

    def remove(self, athlete_id: int, activity_id: int) -> None:
        entries = self._pending.get(athlete_id)
        if not entries:
            return
        entries[:] = [e for e in entries if e.activity_id != activity_id]
        if not entries:
            self._pending.pop(athlete_id, None)

    def pending_count(self) -> int:
        return sum(len(v) for v in self._pending.values())

    def evict_expired(self) -> int:
        """Drop entries older than the TTL. Returns the number evicted."""
        ttl = self._settings.pending_ttl_seconds
        now = _utcnow()
        evicted = 0
        for athlete_id in list(self._pending.keys()):
            entries = self._pending[athlete_id]
            kept: list[PendingActivity] = []
            for entry in entries:
                age = (now - entry.received_at).total_seconds()
                if age > ttl:
                    evicted += 1
                    self._log.info(
                        "Evicted unmatched pending activity (no partner synced "
                        "within TTL); leaving it untouched on Strava",
                        extra={
                            "stage": "ttl_eviction",
                            "athlete_id": athlete_id,
                            "activity_id": entry.activity_id,
                            "source": entry.source,
                            "age_seconds": round(age, 1),
                        },
                    )
                else:
                    kept.append(entry)
            if kept:
                self._pending[athlete_id] = kept
            else:
                self._pending.pop(athlete_id, None)
        return evicted

    async def eviction_loop(self) -> None:
        """Background task: evict expired entries on a fixed interval."""
        interval = self._settings.eviction_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                self.evict_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - keep the loop alive
                self._log.exception(
                    "TTL eviction loop iteration failed",
                    extra={"stage": "ttl_eviction"},
                )

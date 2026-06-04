"""Parse Hevy strength sets out of a Strava activity description.

Strava's ``GET /activities/{id}`` response does **not** carry a structured
``sets`` array for strength workouts — that schema is undocumented and, in
practice, absent. Hevy instead writes the workout into the activity's free-text
``description`` field, e.g.::

    Logged with hevyapp.com

    Deadlift (Barbell)
    Set 1: 55 kg x 6
    Set 2: 60 kg x 6

    Shoulder Press (Dumbbell)
    Set 1: 12 kg x 12

This module recovers those sets so the merger can identify Hevy activities and
rebuild a structured ``sets`` payload. It is deliberately defensive: an
unparseable or truncated line is skipped rather than raising.
"""

from __future__ import annotations

import re
from typing import Any

# Marker Hevy stamps on every exported description.
_HEVY_MARKER = "hevyapp.com"

# A set line looks like "Set 1: 55 kg x 6" (the leading label may be "Set N",
# "Warmup N", etc.). We treat anything up to the first colon as the label.
_SET_LINE = re.compile(r"^\s*(?:set|warm\s*up)\b[^:]*:\s*(?P<detail>.+)$", re.IGNORECASE)

# The detail after the colon: optional "<weight> <units>", an optional "x"/"×"
# separator, then the rep count (optionally followed by the word "reps").
_SET_DETAIL = re.compile(
    r"(?:(?P<weight>\d+(?:\.\d+)?)\s*(?P<units>kgs?|kilograms?|lbs?|pounds?)\s*)?"
    r"(?:[x×*]\s*)?(?P<reps>\d+)\s*(?:reps?)?",
    re.IGNORECASE,
)


def _normalize_units(raw: str | None) -> str | None:
    if not raw:
        return None
    u = raw.lower()
    if u.startswith("kg") or u.startswith("kilogram"):
        return "kilograms"
    if u.startswith("lb") or u.startswith("pound"):
        return "pounds"
    return None


def _parse_detail(detail: str) -> dict[str, Any] | None:
    """Parse the part of a set line after the colon into reps/weight/units."""
    m = _SET_DETAIL.search(detail)
    if not m:
        return None
    weight = m.group("weight")
    return {
        "reps": int(m.group("reps")),
        "weight": float(weight) if weight is not None else None,
        "weight_units": _normalize_units(m.group("units")),
    }


def parse_hevy_sets(description: str | None) -> list[dict[str, Any]]:
    """Extract strength sets from a Hevy-written activity description.

    Returns a list of set dicts shaped like the structured sets the rest of the
    code expects (``{"exercise": {"name": ...}, "reps", "weight",
    "weight_units"}``). Returns ``[]`` when the description is empty, is not a
    Hevy export, or contains no recognizable sets.
    """
    if not description or _HEVY_MARKER not in description.lower():
        return []

    sets: list[dict[str, Any]] = []
    current_exercise: str | None = None

    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line or _HEVY_MARKER in line.lower():
            continue

        match = _SET_LINE.match(line)
        if match:
            # A set line only counts once we know which exercise it belongs to.
            if current_exercise is None:
                continue
            detail = _parse_detail(match.group("detail"))
            if detail is None:
                continue
            sets.append({"exercise": {"name": current_exercise}, **detail})
        else:
            # Any other non-blank line is an exercise header.
            current_exercise = line

    return sets

"""Pure transforms: source identification and merged-payload construction.

Everything here is side-effect-free and deterministic so it can be unit-tested
without touching the network. The orchestrator (in ``main.py``) wraps these in
the stage-specific diagnostic logging the spec requires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .exercises import to_exercise_type
from .hevy import parse_hevy_sets
from .models import StravaActivity

# Required top-level keys the Strava JSON strength upload must carry (used to
# validate the built payload before we attempt the upload).
REQUIRED_PAYLOAD_FIELDS = ("version", "start_time", "elapsed_time")

# Pounds -> kilograms (Strava's JSON upload expresses weight in kg).
_LB_TO_KG = 0.45359237

# The sport_type used for merged strength uploads (a multipart form field, not
# a body field).
MERGED_SPORT_TYPE = "WeightTraining"


def identify_source(activity: StravaActivity) -> str | None:
    """Best-effort single-activity source, in the spec's priority order.

    1. ``external_id`` ending in ``.fit`` -> Garmin
    2. ``device_name`` / ``external_id`` / ``name`` naming patterns
    3. fallback: sets present -> Hevy; heart-rate present -> Garmin

    Used at ingest to tag an activity so the matcher can pair *different*
    sources. Returns ``None`` when no signal is conclusive.
    """
    ext = (activity.external_id or "").lower()
    dev = (activity.device_name or "").lower()
    name = (activity.name or "").lower()

    if ext.endswith(".fit"):
        return "garmin"
    if "garmin" in dev or "garmin" in ext:
        return "garmin"
    if "hevy" in ext or "hevy" in name or "hevy" in dev:
        return "hevy"
    if activity.has_sets:
        return "hevy"
    if activity.has_heartrate:
        return "garmin"
    return None


def is_probably_merged(activity: StravaActivity) -> bool:
    """True if the activity already looks merged (has both sets *and* HR).

    Our own merged uploads carry both, so this guards against treating them as
    a fresh Garmin/Hevy half and re-merging them. Defence-in-depth alongside
    the explicit "ignore activity IDs we created" check in the orchestrator.
    """
    return activity.has_sets and activity.has_heartrate


def identify_pair(
    a: StravaActivity, b: StravaActivity
) -> tuple[StravaActivity, StravaActivity] | None:
    """Resolve which activity is Garmin (HR) and which is Hevy (sets).

    The reliable signal is the source tag (``external_id`` / ``device_name``)
    via :func:`identify_source`: Strava does not return a structured ``sets``
    array, so the older "has_sets vs has_heartrate" content check can never
    succeed on real data. We first look for an assignment where one side is
    clearly Garmin and the other clearly Hevy; failing that we fall back to the
    content signals (Hevy=sets, Garmin=HR) for synthetic / edge cases.

    Returns ``(garmin, hevy)``, or ``None`` when no assignment is conclusive —
    the "graceful no-op" case where the caller leaves both activities intact.
    """
    for garmin, hevy in ((a, b), (b, a)):
        if garmin.id == hevy.id:
            continue
        if identify_source(garmin) == "garmin" and identify_source(hevy) == "hevy":
            return garmin, hevy
    # Fallback: content signals, for cases where source tags are inconclusive.
    for garmin, hevy in ((a, b), (b, a)):
        if garmin.id != hevy.id and hevy.has_sets and garmin.has_heartrate:
            return garmin, hevy
    return None


def hevy_set_dicts(hevy: StravaActivity) -> list[dict[str, Any]]:
    """Return the raw Hevy set dicts: a structured array if present, else the
    sets parsed out of the free-text description (the real-world case)."""
    return [s.model_dump() for s in hevy.sets] or parse_hevy_sets(hevy.description)


def _set_name(d: dict[str, Any]) -> str:
    """Read an exercise name from a set dict, across plausible spellings."""
    exercise = d.get("exercise")
    name: str | None = None
    if isinstance(exercise, dict):
        name = exercise.get("name") or exercise.get("title")
    return name or d.get("exercise_name") or d.get("name") or ""


def _to_kg(weight: float | int | None, units: str | None) -> float | None:
    """Convert a weight to kilograms (Strava's upload unit)."""
    if weight is None:
        return None
    if units and units.strip().lower().startswith(("lb", "pound")):
        return round(float(weight) * _LB_TO_KG, 2)
    return float(weight)


def _to_strava_set(d: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Map one set dict onto Strava's JSON strength ``set`` shape.

    ``exercise_type`` is required; ``repetitions``/``weight``/``duration`` are
    only included when present. Weight is normalised to kilograms.
    """
    exercise_type, _matched = to_exercise_type(
        _set_name(d), fallback=settings.fallback_exercise_type
    )
    out: dict[str, Any] = {"exercise_type": exercise_type}

    reps = d.get("reps")
    if reps is None:
        reps = d.get("repetitions")
    if reps is not None:
        out["repetitions"] = reps

    weight = d.get("weight")
    if weight is None:
        weight = d.get("weight_kg")
    weight_kg = _to_kg(weight, d.get("weight_units"))
    if weight_kg is not None:
        out["weight"] = weight_kg

    if d.get("duration") is not None:
        out["duration"] = d["duration"]
    return out


def unmapped_exercise_names(hevy: StravaActivity, settings: Settings) -> list[str]:
    """Distinct Hevy exercise names that fell through to the fallback enum.

    The orchestrator logs these so unmapped exercises are visible (and the
    curated map / config can be extended).
    """
    names: list[str] = []
    for d in hevy_set_dicts(hevy):
        name = _set_name(d)
        _etype, matched = to_exercise_type(
            name, fallback=settings.fallback_exercise_type
        )
        if not matched and name and name not in names:
            names.append(name)
    return names


def _merged_start_time(garmin: StravaActivity, hevy: StravaActivity) -> str | None:
    """Start time for the merged activity: the midpoint of the two source starts.

    Strava dedupes uploads by start time, so reusing either original's start
    (while that original still exists) gets the merged activity rejected as a
    duplicate. The midpoint differs from both — and is maximally far from each —
    while staying an honest "when the workout started". Falls back to Garmin's
    start when only one is parseable (``None`` if Garmin's is absent, so it is
    flagged as missing).
    """
    gd = garmin.start_datetime()
    hd = hevy.start_datetime()
    if gd and hd:
        midpoint = datetime.fromtimestamp(
            round((gd.timestamp() + hd.timestamp()) / 2), tz=timezone.utc
        )
        return midpoint.strftime("%Y-%m-%dT%H:%M:%SZ")
    return garmin.start_date


def _merged_elapsed(garmin: StravaActivity, hevy: StravaActivity) -> int | None:
    """elapsed_time for the merged activity: average end minus midpoint start.

    Each source's end is ``start + elapsed_time``; we average the two ends and
    subtract the midpoint start (the same midpoint used for ``start_time``).
    Falls back to Garmin's ``elapsed_time`` when any component is missing.
    """
    gd, hd = garmin.start_datetime(), hevy.start_datetime()
    if (
        gd is None
        or hd is None
        or garmin.elapsed_time is None
        or hevy.elapsed_time is None
    ):
        return garmin.elapsed_time
    g_end = gd.timestamp() + garmin.elapsed_time
    h_end = hd.timestamp() + hevy.elapsed_time
    midpoint_start = (gd.timestamp() + hd.timestamp()) / 2
    return round((g_end + h_end) / 2 - midpoint_start)


def build_merged_payload(
    garmin: StravaActivity,
    hevy: StravaActivity,
    heartrate_data: list[Any],
    time_data: list[Any],
    settings: Settings,
) -> dict[str, Any]:
    """Construct the Strava JSON strength upload body (best effort, never raises).

    Returns the JSON dict that becomes the uploaded ``file`` (``sport_type`` is
    a separate multipart field, see :data:`MERGED_SPORT_TYPE`). Use
    :func:`required_payload_problems` to validate before uploading.
    """
    sets = [_to_strava_set(d, settings) for d in hevy_set_dicts(hevy)]

    payload: dict[str, Any] = {
        "version": "1.0",
        "start_time": _merged_start_time(garmin, hevy),
        "utc_offset": int(garmin.utc_offset) if garmin.utc_offset is not None else None,
        "elapsed_time": _merged_elapsed(garmin, hevy),
        "active_time": garmin.moving_time,
        "streams": {
            "time": time_data,
            "heartrate": heartrate_data,
        },
        "sets": sets,
    }
    return payload


def required_payload_problems(payload: dict[str, Any]) -> list[str]:
    """Return human-readable problems that would make the upload fail."""
    problems: list[str] = []
    for key in REQUIRED_PAYLOAD_FIELDS:
        if payload.get(key) in (None, ""):
            problems.append(f"missing {key}")
    sets = payload.get("sets") or []
    if not sets:
        problems.append("no sets in payload")
    elif any(not s.get("exercise_type") for s in sets):
        problems.append("set missing exercise_type")
    if not payload.get("streams", {}).get("heartrate"):
        problems.append("empty heartrate stream")
    return problems

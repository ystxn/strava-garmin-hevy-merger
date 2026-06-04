"""Pure transforms: source identification and merged-payload construction.

Everything here is side-effect-free and deterministic so it can be unit-tested
without touching the network. The orchestrator (in ``main.py``) wraps these in
the stage-specific diagnostic logging the spec requires.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .models import StravaActivity, StravaSet

# Required keys the Strava JSON upload must carry (used to validate the built
# payload before we attempt the upload).
REQUIRED_PAYLOAD_FIELDS = ("sport_type", "start_time", "elapsed_time")


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

    Per the spec: the one with a non-empty ``sets`` array is Hevy; the one with
    a heart-rate stream is Garmin. We return the assignment that satisfies both
    conditions, or ``None`` when neither assignment does — that ``None`` is the
    "graceful no-op" case (e.g. no sets anywhere, or the sets-haver's partner
    has no HR), where the caller must leave both activities intact.

    Returns ``(garmin, hevy)``.
    """
    for garmin, hevy in ((a, b), (b, a)):
        if garmin.id != hevy.id and hevy.has_sets and garmin.has_heartrate:
            return garmin, hevy
    return None


def _extract_set(raw_set: StravaSet, index: int, default_units: str) -> dict[str, Any]:
    """Map one Strava/Hevy set onto the merged-upload set shape, defensively.

    The exact field names Strava returns for strength sets are undocumented, so
    we read several plausible spellings and fall back gracefully. Unknown extra
    keys were preserved by the model (``extra="allow"``) and are reachable via
    ``model_dump()``.
    """
    d = raw_set.model_dump()

    exercise = d.get("exercise")
    name: str | None = None
    if isinstance(exercise, dict):
        name = exercise.get("name") or exercise.get("title")
    name = name or d.get("exercise_name") or d.get("name")

    reps = d.get("reps")
    if reps is None:
        reps = d.get("repetitions")

    weight = d.get("weight")
    if weight is None:
        weight = d.get("weight_kg")

    out: dict[str, Any] = {
        "id": d.get("id") if d.get("id") is not None else index,
        "exercise": {"name": name or "Unknown Exercise"},
        "reps": reps,
        "weight": weight,
        "weight_units": d.get("weight_units") or default_units,
    }

    # start_index / end_index are optional positions in the time stream.
    if d.get("start_index") is not None:
        out["start_index"] = d["start_index"]
    if d.get("end_index") is not None:
        out["end_index"] = d["end_index"]
    return out


def _build_description(hevy: StravaActivity, mapped_sets: list[dict[str, Any]]) -> str:
    exercises = []
    for s in mapped_sets:
        ex = s.get("exercise", {}).get("name")
        if ex and ex not in exercises:
            exercises.append(ex)
    parts = [f"{len(mapped_sets)} sets"]
    if exercises:
        parts.append(f"{len(exercises)} exercises")
    parts.append("HR from Garmin, sets from Hevy")
    return " · ".join(parts)


def build_merged_payload(
    garmin: StravaActivity,
    hevy: StravaActivity,
    heartrate_data: list[Any],
    time_data: list[Any],
    settings: Settings,
) -> dict[str, Any]:
    """Construct the merged JSON upload payload (best effort, never raises).

    Returns a dict even when source fields are missing (values may be ``None``)
    so the caller can both validate it and log the partially-constructed payload
    in a ``payload_build`` diagnostic. Use :func:`required_payload_problems` to
    check completeness before uploading.
    """
    mapped_sets = [
        _extract_set(s, i, settings.weight_units) for i, s in enumerate(hevy.sets)
    ]

    payload: dict[str, Any] = {
        "sport_type": "WeightTraining",
        "start_time": garmin.start_date,
        "elapsed_time": garmin.elapsed_time,
        "description": _build_description(hevy, mapped_sets),
        "visibility": settings.merged_activity_visibility,
        "sets": mapped_sets,
        "streams": {
            "time": {"data": time_data},
            "heartrate": {"data": heartrate_data},
        },
    }
    return payload


def required_payload_problems(payload: dict[str, Any]) -> list[str]:
    """Return human-readable problems that would make the upload fail."""
    problems: list[str] = []
    for key in REQUIRED_PAYLOAD_FIELDS:
        if payload.get(key) in (None, ""):
            problems.append(f"missing {key}")
    if not payload.get("sets"):
        problems.append("no sets in payload")
    hr = payload.get("streams", {}).get("heartrate", {}).get("data")
    if not hr:
        problems.append("empty heartrate stream")
    return problems

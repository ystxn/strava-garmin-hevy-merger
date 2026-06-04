"""Pure transforms: source identification and merged-payload construction.

Everything here is side-effect-free and deterministic so it can be unit-tested
without touching the network. The orchestrator (in ``main.py``) wraps these in
the stage-specific diagnostic logging the spec requires.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .hevy import parse_hevy_sets
from .models import StravaActivity

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


def _extract_set(d: dict[str, Any], index: int, default_units: str) -> dict[str, Any]:
    """Map one set dict onto the merged-upload set shape, defensively.

    Accepts either a structured set from Strava (``StravaSet.model_dump()``) or
    a set parsed out of the Hevy description (:func:`app.hevy.parse_hevy_sets`).
    The exact field names are undocumented, so we read several plausible
    spellings and fall back gracefully.
    """
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
    # Prefer a structured `sets` array if Strava ever provides one; otherwise
    # recover the sets from Hevy's free-text description (the real-world case).
    raw_sets = [s.model_dump() for s in hevy.sets] or parse_hevy_sets(hevy.description)
    mapped_sets = [
        _extract_set(d, i, settings.weight_units) for i, d in enumerate(raw_sets)
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

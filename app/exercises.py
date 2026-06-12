"""Map Hevy free-text exercise names to Strava ``exercise_type`` enums.

Strava's JSON strength upload requires an ``exercise_type`` per set, drawn from
a fixed ~400-value enum (``EQUIPMENT_MOVEMENT`` convention, with per-category
``*_GENERIC`` fallbacks). Hevy only gives us free text like
``"Deadlift (Barbell)"``, so we map best-effort:

1. a curated dict of known names -> exact enum;
2. a movement-keyword -> category ``_GENERIC`` heuristic;
3. a configurable fallback enum (caller warns when this is used).

Pure and side-effect-free so it can be unit-tested in isolation.
"""

from __future__ import annotations

# Known Hevy names -> exact Strava enum. Keys are matched case-insensitively.
_CURATED: dict[str, str] = {
    "deadlift (barbell)": "BARBELL_DEADLIFT",
    "shoulder press (dumbbell)": "OVERHEAD_DUMBBELL_PRESS",
    "seated cable row - v grip (cable)": "SEATED_CABLE_ROW",
    "hip thrust (machine)": "HIP_THRUST",
    "sit up (weighted)": "SIT_UP_GENERIC",
    "hip abduction (machine)": "MACHINE_HIP_ABDUCTION",
}

# Movement keyword -> category generic. Checked in order, so put more specific
# multi-word keys before the single-word ones they contain.
_KEYWORD_GENERICS: tuple[tuple[str, str], ...] = (
    # Pull-ups: Strava renders the bare PULL_UP / CHIN_UP enums as "Unknown";
    # only the category generic PULL_UP_GENERIC is accepted (verified live).
    ("pull up", "PULL_UP_GENERIC"),
    ("pull-up", "PULL_UP_GENERIC"),
    ("pullup", "PULL_UP_GENERIC"),
    ("chin up", "PULL_UP_GENERIC"),
    ("chin-up", "PULL_UP_GENERIC"),
    ("chinup", "PULL_UP_GENERIC"),
    # Face pull -> FACE_PULL (specific name is accepted); before "row".
    ("face pull", "FACE_PULL"),
    # Twists -> RUSSIAN_TWIST / CORE_GENERIC (both accepted); specific first.
    ("russian twist", "RUSSIAN_TWIST"),
    ("twist", "CORE_GENERIC"),
    ("hip abduction", "HIP_STABILITY_GENERIC"),
    ("hip adduction", "HIP_STABILITY_GENERIC"),
    ("hip thrust", "HIP_RAISE_GENERIC"),
    ("hip raise", "HIP_RAISE_GENERIC"),
    ("sit up", "SIT_UP_GENERIC"),
    ("situp", "SIT_UP_GENERIC"),
    ("crunch", "SIT_UP_GENERIC"),
    ("shoulder press", "SHOULDER_PRESS_GENERIC"),
    ("overhead press", "SHOULDER_PRESS_GENERIC"),
    ("bench press", "BENCH_PRESS_GENERIC"),
    ("deadlift", "DEADLIFT_GENERIC"),
    ("squat", "SQUAT_GENERIC"),
    ("lunge", "LUNGE_GENERIC"),
    ("curl", "CURL_GENERIC"),
    ("row", "ROW_GENERIC"),
    ("press", "SHOULDER_PRESS_GENERIC"),
)


def to_exercise_type(name: str, *, fallback: str) -> tuple[str, bool]:
    """Resolve a Hevy exercise name to a Strava ``exercise_type``.

    Returns ``(exercise_type, matched)``. ``matched`` is ``False`` only when the
    ``fallback`` was used (no curated/keyword match), so the caller can emit a
    warning naming the unmapped exercise.
    """
    key = (name or "").strip().lower()
    if not key:
        return fallback, False

    if key in _CURATED:
        return _CURATED[key], True

    for keyword, generic in _KEYWORD_GENERICS:
        if keyword in key:
            return generic, True

    return fallback, False

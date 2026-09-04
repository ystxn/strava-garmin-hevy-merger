"""Map Hevy free-text exercise names to Strava ``exercise_type`` enums.

Strava's JSON strength upload requires an ``exercise_type`` per set, drawn from
a fixed ~400-value enum (``EQUIPMENT_MOVEMENT`` convention, with per-category
``*_GENERIC`` fallbacks). Hevy only gives us free text like
``"Romanian Deadlift (Barbell)"``, so we map best-effort:

1. a curated dict of known names -> exact enum;
2. an ordered list of keyword rules -> the most specific enum we can name;
3. a configurable fallback enum (caller warns when this is used).

A rule's keywords must *all* appear in the (lowercased) name for it to match,
and the first matching rule wins. That lets us pick equipment-specific enums
(e.g. ``"Romanian Deadlift (Barbell)"`` -> ``BARBELL_ROMANIAN_DEADLIFT``) while
falling back to a category ``*_GENERIC`` when the equipment is unknown. Rules
are ordered specific -> generic so a precise match is preferred over the
catch-all; cross-category rules must not collide (audited by the tests).

Every enum below is a value from Strava's documented strength ``exercise_type``
list (https://developers.strava.com/docs/uploads/) — invalid values render as
"Unknown" in the Strava UI, so we only emit confirmed ones. Pure and
side-effect-free so it can be unit-tested in isolation.
"""

from __future__ import annotations

# Known Hevy names -> exact Strava enum. Keys are matched case-insensitively and
# take priority over the keyword rules below.
_CURATED: dict[str, str] = {
    "deadlift (barbell)": "BARBELL_DEADLIFT",
    "shoulder press (dumbbell)": "OVERHEAD_DUMBBELL_PRESS",
    "seated cable row - v grip (cable)": "SEATED_CABLE_ROW",
    "hip thrust (machine)": "HIP_THRUST",
    "sit up (weighted)": "SIT_UP_GENERIC",
    "hip abduction (machine)": "MACHINE_HIP_ABDUCTION",
}

# Ordered keyword rules: (required keywords, exercise_type). A rule matches when
# every keyword is a substring of the lowercased name; the first match wins, so
# more specific rules (more/rarer keywords) come before the generics they would
# otherwise be shadowed by. Equipment words (barbell/dumbbell/machine/...) let us
# resolve to a specific enum instead of a category generic.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # ---- Romanian deadlift (RDL): before the plain "deadlift" rules, since
    # "romanian deadlift" contains "deadlift". "rdl" is the common abbreviation.
    # Single-leg variants must precede the two-leg barbell/dumbbell rules below,
    # since "single leg romanian deadlift (dumbbell)" also contains "romanian
    # deadlift" + "dumbbell" and would otherwise match those first.
    (("single leg romanian deadlift", "dumbbell"), "SINGLE_LEG_DUMBBELL_ROMANIAN_DEADLIFTS"),
    (("romanian deadlift", "barbell"), "BARBELL_ROMANIAN_DEADLIFT"),
    (("rdl", "barbell"), "BARBELL_ROMANIAN_DEADLIFT"),
    (("romanian deadlift", "dumbbell"), "DUMBBELL_ROMANIAN_DEADLIFTS"),
    (("rdl", "dumbbell"), "DUMBBELL_ROMANIAN_DEADLIFTS"),
    (("single leg romanian deadlift",), "SINGLE_LEG_ROMANIAN_DEADLIFTS"),
    (("romanian deadlift",), "ROMANIAN_DEADLIFTS"),
    (("rdl",), "ROMANIAN_DEADLIFTS"),
    # ---- Other deadlifts ----
    (("sumo deadlift",), "SUMO_DEADLIFT"),
    (("trap bar deadlift",), "TRAP_BAR_DEADLIFT"),
    (("deadlift", "barbell"), "BARBELL_DEADLIFT"),
    (("deadlift", "dumbbell"), "DUMBBELL_DEADLIFT"),
    (("deadlift",), "DEADLIFT_GENERIC"),
    # ---- Pull-ups / chin-ups: Strava renders the bare PULL_UP / CHIN_UP enums
    # as "Unknown"; only the category generic PULL_UP_GENERIC is accepted.
    (("pull up",), "PULL_UP_GENERIC"),
    (("pull-up",), "PULL_UP_GENERIC"),
    (("pullup",), "PULL_UP_GENERIC"),
    (("chin up",), "PULL_UP_GENERIC"),
    (("chin-up",), "PULL_UP_GENERIC"),
    (("chinup",), "PULL_UP_GENERIC"),
    # ---- Lat pulldown: grip/arm variants before the plain pulldown catch-all.
    (("close grip", "lat pulldown"), "CABLE_LAT_PULLDOWN_CLOSE_GRIP"),
    (("single arm", "lat pulldown"), "SINGLE_ARM_LAT_PULLDOWN"),
    (("underhand", "lat pulldown"), "UNDERHAND_LAT_PULLDOWN"),
    (("neutral grip", "lat pulldown"), "NEUTRAL_GRIP_LAT_PULLDOWN"),
    (("lat pulldown",), "LAT_PULLDOWN"),
    # ---- Rows: chest-supported and other specifics before the "row" catch-all.
    (("iso-lateral high row",), "MACHINE_ISOLATERAL_HIGH_ROW"),
    (("iso lateral high row",), "MACHINE_ISOLATERAL_HIGH_ROW"),
    (("chest supported row", "machine"), "MACHINE_CHEST_SUPPORTED_ROW"),
    (("chest-supported row", "machine"), "MACHINE_CHEST_SUPPORTED_ROW"),
    (("chest supported row",), "CHEST_SUPPORTED_ROW"),
    (("chest-supported row",), "CHEST_SUPPORTED_ROW"),
    (("seal row",), "SEAL_ROW"),
    (("t-bar row",), "T_BAR_ROW"),
    (("t bar row",), "T_BAR_ROW"),
    (("bent over row", "barbell"), "BENT_OVER_BARBELL_ROW"),
    (("bent over row", "dumbbell"), "BENT_OVER_DUMBBELL_ROW"),
    (("bent over row",), "BENT_OVER_ROW"),
    (("seated cable row",), "SEATED_CABLE_ROW"),
    (("dumbbell row",), "DUMBBELL_ROW"),
    # Face pull -> FACE_PULL (specific name is accepted); before "row".
    (("face pull",), "FACE_PULL"),
    (("row",), "ROW_GENERIC"),
    # ---- Twists / core ----
    (("russian twist",), "RUSSIAN_TWIST"),
    (("pallof",), "PALLOF_PRESS"),
    (("twist",), "CORE_GENERIC"),
    # ---- Plank ----
    (("side plank",), "SIDE_PLANK"),
    # ---- Calf raise ----
    (("single leg", "standing calf raise", "dumbbell"), "SINGLE_LEG_DUMBBELL_STANDING_CALF_RAISE"),
    # ---- Hips ----
    (("hip abduction",), "HIP_STABILITY_GENERIC"),
    (("hip adduction",), "HIP_STABILITY_GENERIC"),
    (("hip thrust",), "HIP_RAISE_GENERIC"),
    (("hip raise",), "HIP_RAISE_GENERIC"),
    # ---- Core ----
    (("sit up",), "SIT_UP_GENERIC"),
    (("situp",), "SIT_UP_GENERIC"),
    (("crunch",), "SIT_UP_GENERIC"),
    # ---- Chest press / bench: chest press is a chest (bench-press family)
    # movement, NOT a shoulder press. Must precede the "press" catch-all below.
    (("chest press", "machine"), "MACHINE_CHEST_PRESS"),
    (("chest press",), "BENCH_PRESS_GENERIC"),
    (("bench press",), "BENCH_PRESS_GENERIC"),
    # ---- Shoulder press ----
    (("shoulder press", "dumbbell"), "OVERHEAD_DUMBBELL_PRESS"),
    (("shoulder press",), "SHOULDER_PRESS_GENERIC"),
    (("overhead press",), "SHOULDER_PRESS_GENERIC"),
    # ---- Squat: variant-specific before the barbell-back-squat default. Hevy
    # names a plain barbell back squat just "Squat (Barbell)".
    (("front squat", "barbell"), "BARBELL_FRONT_SQUAT"),
    (("hack squat",), "MACHINE_HACK_SQUAT"),
    (("overhead squat",), "OVERHEAD_SQUAT"),
    (("goblet squat",), "GOBLET_SQUAT"),
    (("box squat", "barbell"), "BARBELL_BOX_SQUAT"),
    # Split/Bulgarian squats: no safe barbell-specific enum, keep them generic
    # so they don't get mis-mapped to a back squat by the rule below. Dumbbell
    # Bulgarian split squats have their own specific enum though.
    (("bulgarian split squat", "dumbbell"), "DUMBBELL_BULGARIAN_SPLIT_SQUATS"),
    (("split squat",), "SQUAT_GENERIC"),
    (("squat", "smith"), "SMITH_MACHINE_SQUAT"),
    (("back squat", "barbell"), "BARBELL_BACK_SQUAT"),
    (("squat", "barbell"), "BARBELL_BACK_SQUAT"),
    (("squat",), "SQUAT_GENERIC"),
    # ---- Misc generics ----
    (("lunge",), "LUNGE_GENERIC"),
    (("curl",), "CURL_GENERIC"),
    (("press",), "SHOULDER_PRESS_GENERIC"),
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

    for keywords, exercise_type in _RULES:
        if all(kw in key for kw in keywords):
            return exercise_type, True

    return fallback, False

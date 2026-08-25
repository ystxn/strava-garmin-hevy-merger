"""Tests for mapping Hevy exercise names to Strava exercise_type enums."""

from __future__ import annotations

from app.exercises import to_exercise_type

FALLBACK = "TOTAL_BODY_GENERIC"


def test_curated_exact_names() -> None:
    cases = {
        "Deadlift (Barbell)": "BARBELL_DEADLIFT",
        "Shoulder Press (Dumbbell)": "OVERHEAD_DUMBBELL_PRESS",
        "Seated Cable Row - V Grip (Cable)": "SEATED_CABLE_ROW",
        "Hip Thrust (Machine)": "HIP_THRUST",
        "Sit Up (Weighted)": "SIT_UP_GENERIC",
        "Hip Abduction (Machine)": "MACHINE_HIP_ABDUCTION",
    }
    for name, expected in cases.items():
        etype, matched = to_exercise_type(name, fallback=FALLBACK)
        assert (etype, matched) == (expected, True), name


def test_curated_is_case_insensitive() -> None:
    etype, matched = to_exercise_type("deadlift (barbell)", fallback=FALLBACK)
    assert (etype, matched) == ("BARBELL_DEADLIFT", True)


def test_keyword_falls_back_to_category_generic() -> None:
    # No equipment to disambiguate, so these resolve to the category generic.
    assert to_exercise_type("Squat", fallback=FALLBACK) == ("SQUAT_GENERIC", True)
    assert to_exercise_type("Deadlift", fallback=FALLBACK) == (
        "DEADLIFT_GENERIC",
        True,
    )
    assert to_exercise_type("Bicep Curl", fallback=FALLBACK) == (
        "CURL_GENERIC",
        True,
    )


def test_pull_face_pull_and_twist_map_to_valid_enums() -> None:
    # These three previously fell through to WEIGHT_TRAINING_GENERIC, which
    # Strava renders as "Unknown". The mapped enums below were verified live to
    # render real names (Pull Up / Face Pull / Russian Twist).
    cases = {
        "Pull Up": "PULL_UP_GENERIC",
        "Pull-Up": "PULL_UP_GENERIC",
        "Chin Up": "PULL_UP_GENERIC",
        "Face Pull": "FACE_PULL",
        "Russian Twist (Weighted)": "RUSSIAN_TWIST",
    }
    for name, expected in cases.items():
        etype, matched = to_exercise_type(name, fallback=FALLBACK)
        assert (etype, matched) == (expected, True), name


def test_chest_press_is_not_a_shoulder_press() -> None:
    # Regression: "chest press" contains "press" and used to fall through to the
    # bare-"press" catch-all -> SHOULDER_PRESS_GENERIC. It is a chest movement,
    # and a machine chest press has its own specific enum.
    assert to_exercise_type("Chest Press (Machine)", fallback=FALLBACK) == (
        "MACHINE_CHEST_PRESS",
        True,
    )
    assert to_exercise_type("Seated Chest Press (Machine)", fallback=FALLBACK) == (
        "MACHINE_CHEST_PRESS",
        True,
    )
    # Non-machine chest press has no specific enum, so the bench-press category
    # is used — anything but SHOULDER_PRESS_GENERIC.
    assert to_exercise_type("Chest Press (Dumbbell)", fallback=FALLBACK) == (
        "BENCH_PRESS_GENERIC",
        True,
    )


def test_rdl_maps_to_specific_romanian_deadlift() -> None:
    # Regression: RDL used to fall to a generic (or the fallback for the bare
    # abbreviation). Equipment now resolves to the specific Romanian deadlift.
    assert to_exercise_type("Romanian Deadlift (Barbell)", fallback=FALLBACK) == (
        "BARBELL_ROMANIAN_DEADLIFT",
        True,
    )
    assert to_exercise_type("RDL (Barbell)", fallback=FALLBACK) == (
        "BARBELL_ROMANIAN_DEADLIFT",
        True,
    )
    assert to_exercise_type("Romanian Deadlift (Dumbbell)", fallback=FALLBACK) == (
        "DUMBBELL_ROMANIAN_DEADLIFTS",
        True,
    )
    # No equipment -> the Romanian-deadlift category enum (still not a plain
    # conventional deadlift).
    for name in ("RDL", "Romanian Deadlift"):
        assert to_exercise_type(name, fallback=FALLBACK) == (
            "ROMANIAN_DEADLIFTS",
            True,
        ), name


def test_session_exercises_map_to_specific_enums() -> None:
    # The exact set the user flagged: only the generics for Pull Up / Sit Up
    # were acceptable; the rest must resolve to specific equipment enums.
    cases = {
        "Squat (Barbell)": "BARBELL_BACK_SQUAT",
        "Chest Press (Machine)": "MACHINE_CHEST_PRESS",
        "Romanian Deadlift (Barbell)": "BARBELL_ROMANIAN_DEADLIFT",
        "Chest Supported Row (Dumbbell)": "CHEST_SUPPORTED_ROW",
        "Chest-Supported Row (Machine)": "MACHINE_CHEST_SUPPORTED_ROW",
        "Pull Up": "PULL_UP_GENERIC",
        "Sit Up (Weighted)": "SIT_UP_GENERIC",
    }
    for name, expected in cases.items():
        etype, matched = to_exercise_type(name, fallback=FALLBACK)
        assert (etype, matched) == (expected, True), name


def test_squat_variants_do_not_collapse_to_back_squat() -> None:
    # The barbell-back-squat default must not swallow other barbell squats.
    assert to_exercise_type("Front Squat (Barbell)", fallback=FALLBACK) == (
        "BARBELL_FRONT_SQUAT",
        True,
    )
    assert to_exercise_type("Bulgarian Split Squat (Barbell)", fallback=FALLBACK) == (
        "SQUAT_GENERIC",
        True,
    )
    assert to_exercise_type("Hack Squat (Machine)", fallback=FALLBACK) == (
        "MACHINE_HACK_SQUAT",
        True,
    )


def test_lat_pulldown_variants_map_to_specific_enums() -> None:
    cases = {
        "Lat Pulldown (Cable)": "LAT_PULLDOWN",
        "Close Grip Lat Pulldown (Cable)": "CABLE_LAT_PULLDOWN_CLOSE_GRIP",
        "Single Arm Lat Pulldown (Cable)": "SINGLE_ARM_LAT_PULLDOWN",
        "Underhand Lat Pulldown (Cable)": "UNDERHAND_LAT_PULLDOWN",
        "Neutral Grip Lat Pulldown (Cable)": "NEUTRAL_GRIP_LAT_PULLDOWN",
    }
    for name, expected in cases.items():
        etype, matched = to_exercise_type(name, fallback=FALLBACK)
        assert (etype, matched) == (expected, True), name


def test_unmappable_uses_fallback_and_flags_unmatched() -> None:
    etype, matched = to_exercise_type("Frobnicator 3000", fallback=FALLBACK)
    assert etype == FALLBACK
    assert matched is False


def test_blank_name_uses_fallback() -> None:
    etype, matched = to_exercise_type("", fallback=FALLBACK)
    assert (etype, matched) == (FALLBACK, False)

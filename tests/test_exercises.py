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
    # Not in the curated dict, but the movement keyword is recognizable.
    assert to_exercise_type("Romanian Deadlift", fallback=FALLBACK) == (
        "DEADLIFT_GENERIC",
        True,
    )
    assert to_exercise_type("Bent Over Row", fallback=FALLBACK) == (
        "ROW_GENERIC",
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


def test_unmappable_uses_fallback_and_flags_unmatched() -> None:
    etype, matched = to_exercise_type("Frobnicator 3000", fallback=FALLBACK)
    assert etype == FALLBACK
    assert matched is False


def test_blank_name_uses_fallback() -> None:
    etype, matched = to_exercise_type("", fallback=FALLBACK)
    assert (etype, matched) == (FALLBACK, False)

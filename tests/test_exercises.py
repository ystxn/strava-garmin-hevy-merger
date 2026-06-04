"""Tests for mapping Hevy exercise names to Strava exercise_type enums."""

from __future__ import annotations

from app.exercises import to_exercise_type

FALLBACK = "WEIGHT_TRAINING_GENERIC"


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


def test_unmappable_uses_fallback_and_flags_unmatched() -> None:
    etype, matched = to_exercise_type("Frobnicator 3000", fallback=FALLBACK)
    assert etype == FALLBACK
    assert matched is False


def test_blank_name_uses_fallback() -> None:
    etype, matched = to_exercise_type("", fallback=FALLBACK)
    assert (etype, matched) == (FALLBACK, False)

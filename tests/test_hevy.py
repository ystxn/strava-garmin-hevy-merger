"""Tests for parsing Hevy strength sets out of the Strava activity description.

Strava's activity-detail API does not return a structured ``sets`` array for
strength workouts. Hevy instead writes the workout into the free-text
``description`` field. These tests pin the parser that recovers the sets.
"""

from __future__ import annotations

from app.hevy import parse_hevy_sets

# The exact description shape Hevy writes (taken from a production webhook).
REAL_DESCRIPTION = (
    "Logged with hevyapp.com\n"
    "\n"
    "Deadlift (Barbell)\n"
    "Set 1: 55 kg x 6 \n"
    "Set 2: 55 kg x 6 \n"
    "Set 3: 60 kg x 6 \n"
    "Set 4: 62.5 kg x 6 \n"
    "\n"
    "Shoulder Press (Dumbbell)\n"
    "Set 1: 12 kg x 12 \n"
    "Set 2: 14 kg x 12 \n"
)


def test_parses_real_hevy_description() -> None:
    sets = parse_hevy_sets(REAL_DESCRIPTION)

    assert len(sets) == 6
    assert sets[0] == {
        "exercise": {"name": "Deadlift (Barbell)"},
        "reps": 6,
        "weight": 55.0,
        "weight_units": "kilograms",
    }
    # Decimal weight is preserved.
    assert sets[3]["weight"] == 62.5
    # Set rolls onto the next exercise once a new exercise header appears.
    assert sets[4]["exercise"]["name"] == "Shoulder Press (Dumbbell)"
    assert sets[4]["weight"] == 12.0
    assert sets[4]["reps"] == 12


def test_returns_empty_for_none() -> None:
    assert parse_hevy_sets(None) == []


def test_returns_empty_for_non_hevy_text() -> None:
    assert parse_hevy_sets("Morning run along the river") == []


def test_parses_pounds_and_normalizes_units() -> None:
    sets = parse_hevy_sets(
        "Logged with hevyapp.com\n\nBench Press (Barbell)\nSet 1: 135 lbs x 5\n"
    )
    assert sets[0]["weight"] == 135.0
    assert sets[0]["weight_units"] == "pounds"


def test_parses_bodyweight_reps_only() -> None:
    sets = parse_hevy_sets(
        "Logged with hevyapp.com\n\nPull Up\nSet 1: 12 reps\nSet 2: 10 reps\n"
    )
    assert len(sets) == 2
    assert sets[0] == {
        "exercise": {"name": "Pull Up"},
        "reps": 12,
        "weight": None,
        "weight_units": None,
    }


def test_ignores_truncated_or_garbage_set_lines() -> None:
    # A trailing, truncated "Set" line (as seen when Strava clips the body)
    # must not blow up or produce a junk set.
    sets = parse_hevy_sets(
        "Logged with hevyapp.com\n\nSquat (Barbell)\nSet 1: 60 kg x 5\nSet"
    )
    assert len(sets) == 1


# --- StravaActivity.has_sets -----------------------------------------------


def test_activity_has_sets_true_from_description() -> None:
    from app.models import StravaActivity

    act = StravaActivity.model_validate(
        {
            "id": 1,
            "sets": [],
            "description": "Logged with hevyapp.com\n\nSquat\nSet 1: 60 kg x 5\n",
        }
    )
    assert act.has_sets is True


def test_activity_has_sets_false_for_plain_description() -> None:
    from app.models import StravaActivity

    act = StravaActivity.model_validate(
        {"id": 1, "sets": [], "description": "Easy recovery ride"}
    )
    assert act.has_sets is False

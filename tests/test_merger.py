"""Tests for source identification and merged-payload construction."""

from __future__ import annotations

from app.config import Settings
from app.merger import (
    build_merged_payload,
    identify_pair,
    identify_source,
    is_probably_merged,
    required_payload_problems,
)

from .conftest import garmin_activity, hevy_activity, make_activity


# --- identify_source -------------------------------------------------------


def test_identify_source_fit_extension_is_garmin() -> None:
    act = make_activity(external_id="12345.fit", has_heartrate=False)
    assert identify_source(act) == "garmin"


def test_identify_source_device_name_is_garmin() -> None:
    act = make_activity(external_id=None, device_name="Garmin Fenix 7")
    assert identify_source(act) == "garmin"


def test_identify_source_sets_means_hevy() -> None:
    assert identify_source(hevy_activity()) == "hevy"


def test_identify_source_heartrate_fallback_is_garmin() -> None:
    act = make_activity(external_id="x", has_heartrate=True, sets=[])
    assert identify_source(act) == "garmin"


def test_identify_source_unknown() -> None:
    act = make_activity(external_id="x", has_heartrate=False, sets=[])
    assert identify_source(act) is None


def test_hevy_external_id_pattern() -> None:
    act = make_activity(external_id="hevy-workout-1", has_heartrate=False, sets=[])
    assert identify_source(act) == "hevy"


# --- is_probably_merged ----------------------------------------------------


def test_is_probably_merged_true_when_both_present() -> None:
    act = hevy_activity(has_heartrate=True)
    assert is_probably_merged(act) is True


def test_is_probably_merged_false_for_plain_garmin() -> None:
    assert is_probably_merged(garmin_activity()) is False


# --- identify_pair ---------------------------------------------------------


def test_identify_pair_resolves_roles_regardless_of_order() -> None:
    g = garmin_activity()
    h = hevy_activity()
    assert identify_pair(g, h) == (g, h)
    assert identify_pair(h, g) == (g, h)


def test_identify_pair_none_when_no_sets_anywhere() -> None:
    g1 = garmin_activity(id=1)
    g2 = garmin_activity(id=2)
    assert identify_pair(g1, g2) is None


def test_identify_pair_none_when_partner_has_no_hr() -> None:
    # Hevy has sets but the other activity also lacks HR -> graceful no-op.
    h = hevy_activity(id=222)
    other = make_activity(id=333, has_heartrate=False, sets=[])
    assert identify_pair(h, other) is None


def test_identify_pair_matches_real_strava_pair_without_sets_array() -> None:
    # Mirrors the production log: neither activity carries a structured `sets`
    # array (Strava doesn't return one). Hevy is identified by external_id /
    # device_name, Garmin by external_id / device_name + heart rate.
    hevy = make_activity(
        id=18780591589,
        external_id="hevy_upload_1780563473040.json",
        device_name="Hevy",
        has_heartrate=False,
        sets=[],
        description="Logged with hevyapp.com\n\nDeadlift (Barbell)\nSet 1: 55 kg x 6",
    )
    garmin = make_activity(
        id=18780601778,
        external_id="garmin_ping_580340013153",
        device_name="Garmin Forerunner 970",
        has_heartrate=True,
        sets=[],
    )
    assert identify_pair(hevy, garmin) == (garmin, hevy)
    assert identify_pair(garmin, hevy) == (garmin, hevy)


# --- build_merged_payload --------------------------------------------------


def test_build_merged_payload_shape(settings: Settings) -> None:
    g = garmin_activity()
    h = hevy_activity()
    payload = build_merged_payload(g, h, [70, 72, 75], [0, 1, 2], settings)

    assert payload["sport_type"] == "WeightTraining"
    assert payload["start_time"] == "2026-01-01T10:00:00Z"
    assert payload["elapsed_time"] == 3600
    assert payload["visibility"] == "only_me"
    assert payload["streams"]["heartrate"]["data"] == [70, 72, 75]
    assert payload["streams"]["time"]["data"] == [0, 1, 2]

    assert len(payload["sets"]) == 2
    first = payload["sets"][0]
    assert first["exercise"]["name"] == "Bench Press"
    assert first["reps"] == 5
    assert first["weight"] == 80.0
    assert first["weight_units"] == "kilograms"
    # Second set had no explicit units -> falls back to the configured default.
    assert payload["sets"][1]["weight_units"] == settings.weight_units


def test_build_merged_payload_handles_alternate_set_fields(
    settings: Settings,
) -> None:
    h = make_activity(
        id=222,
        sets=[{"exercise_name": "Deadlift", "repetitions": 3, "weight_kg": 140.0}],
    )
    g = garmin_activity()
    payload = build_merged_payload(g, h, [60], [0], settings)
    s = payload["sets"][0]
    assert s["exercise"]["name"] == "Deadlift"
    assert s["reps"] == 3
    assert s["weight"] == 140.0


def test_build_merged_payload_missing_name_defaults(settings: Settings) -> None:
    h = make_activity(id=222, sets=[{"reps": 10, "weight": 20.0}])
    g = garmin_activity()
    payload = build_merged_payload(g, h, [60], [0], settings)
    assert payload["sets"][0]["exercise"]["name"] == "Unknown Exercise"


def test_build_merged_payload_parses_sets_from_description(
    settings: Settings,
) -> None:
    # No structured `sets` array (the real-world case) -> parse the Hevy
    # description so the merged upload still carries the sets.
    h = make_activity(
        id=222,
        sets=[],
        description=(
            "Logged with hevyapp.com\n\nDeadlift (Barbell)\n"
            "Set 1: 55 kg x 6\nSet 2: 60 kg x 6\n"
        ),
    )
    g = garmin_activity()
    payload = build_merged_payload(g, h, [70, 72], [0, 1], settings)

    assert len(payload["sets"]) == 2
    assert payload["sets"][0]["exercise"]["name"] == "Deadlift (Barbell)"
    assert payload["sets"][0]["weight"] == 55.0
    assert payload["sets"][0]["reps"] == 6
    assert payload["sets"][0]["weight_units"] == "kilograms"
    assert payload["sets"][1]["weight"] == 60.0
    assert required_payload_problems(payload) == []


# --- required_payload_problems ---------------------------------------------


def test_required_payload_problems_clean(settings: Settings) -> None:
    payload = build_merged_payload(
        garmin_activity(), hevy_activity(), [70, 72], [0, 1], settings
    )
    assert required_payload_problems(payload) == []


def test_required_payload_problems_flags_empty_hr(settings: Settings) -> None:
    payload = build_merged_payload(
        garmin_activity(), hevy_activity(), [], [], settings
    )
    assert "empty heartrate stream" in required_payload_problems(payload)


def test_required_payload_problems_flags_missing_start(settings: Settings) -> None:
    g = garmin_activity(start_date=None)
    payload = build_merged_payload(g, hevy_activity(), [70], [0], settings)
    assert any("start_time" in p for p in required_payload_problems(payload))

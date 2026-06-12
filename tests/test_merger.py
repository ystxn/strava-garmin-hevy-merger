"""Tests for source identification and merged-payload construction."""

from __future__ import annotations

from app.config import Settings
from app.merger import (
    build_merged_payload,
    identify_pair,
    identify_source,
    is_probably_merged,
    required_payload_problems,
    unmapped_exercise_names,
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


# --- build_merged_payload (Strava JSON strength shape) ----------------------


def test_build_merged_payload_shape(settings: Settings) -> None:
    g = garmin_activity()
    h = hevy_activity()  # Bench Press 5x80kg, Back Squat 5x100 (no units)
    payload = build_merged_payload(g, h, [70, 72, 75], [0, 1, 2], settings)

    assert payload["version"] == "1.0"
    assert payload["start_time"] == "2026-01-01T10:01:00Z"  # later start + 1 min
    assert payload["elapsed_time"] == 3600
    # Streams are bare arrays, not {"data": [...]}.
    assert payload["streams"]["heartrate"] == [70, 72, 75]
    assert payload["streams"]["time"] == [0, 1, 2]

    assert len(payload["sets"]) == 2
    # Mapped to Strava exercise_type enums; no name/reps/weight_units keys.
    assert payload["sets"][0] == {
        "exercise_type": "BENCH_PRESS_GENERIC",
        "repetitions": 5,
        "weight": 80.0,
    }
    assert payload["sets"][1]["exercise_type"] == "SQUAT_GENERIC"
    assert payload["sets"][1]["weight"] == 100.0


def test_build_merged_payload_start_time_is_after_later_start(
    settings: Settings,
) -> None:
    # Strava dedupes by start time, so the merged activity must not exactly
    # match either original; one minute after the chronologically later start
    # dodges both, regardless of which source is the later one.
    g = garmin_activity(start_date="2026-06-04T09:29:10Z")
    h = make_activity(
        id=222,
        start_date="2026-06-04T09:28:50Z",
        sets=[{"exercise": {"name": "Deadlift (Barbell)"}, "reps": 6, "weight": 55.0}],
    )
    payload = build_merged_payload(g, h, [70], [0], settings)
    assert payload["start_time"] == "2026-06-04T09:30:10Z"  # garmin is later

    g2 = garmin_activity(start_date="2026-06-04T09:28:50Z")
    h2 = make_activity(
        id=222,
        start_date="2026-06-04T09:29:10Z",
        sets=[{"exercise": {"name": "Deadlift (Barbell)"}, "reps": 6, "weight": 55.0}],
    )
    payload2 = build_merged_payload(g2, h2, [70], [0], settings)
    assert payload2["start_time"] == "2026-06-04T09:30:10Z"  # hevy is later


def test_build_merged_payload_elapsed_is_average_duration(
    settings: Settings,
) -> None:
    # elapsed_time = average of the two durations: (300 + 380) / 2 = 340s.
    g = garmin_activity(start_date="2026-06-04T10:00:00Z", elapsed_time=300)
    h = make_activity(
        id=222,
        start_date="2026-06-04T10:00:40Z",
        elapsed_time=380,
        sets=[{"exercise": {"name": "Deadlift (Barbell)"}, "reps": 6, "weight": 55.0}],
    )
    payload = build_merged_payload(g, h, [70], [0], settings)
    assert payload["start_time"] == "2026-06-04T10:01:40Z"  # later start + 1 min
    assert payload["elapsed_time"] == 340


def test_build_merged_payload_has_no_description_key(settings: Settings) -> None:
    # Description moves to an upload form field (Hevy's text), not the JSON body.
    payload = build_merged_payload(
        garmin_activity(), hevy_activity(), [70], [0], settings
    )
    assert "description" not in payload


def test_build_merged_payload_start_time_falls_back_to_garmin(
    settings: Settings,
) -> None:
    # When only one start is parseable, fall back to Garmin's (None stays None
    # so it's flagged as missing).
    g = garmin_activity(start_date="2026-06-04T09:29:10Z")
    h = make_activity(id=222, start_date=None, sets=[{"reps": 5, "weight": 10.0}])
    payload = build_merged_payload(g, h, [70], [0], settings)
    assert payload["start_time"] == "2026-06-04T09:29:10Z"


def test_build_merged_payload_utc_offset_and_active_time(settings: Settings) -> None:
    g = garmin_activity(utc_offset=28800.0, moving_time=383)
    payload = build_merged_payload(g, hevy_activity(), [70], [0], settings)
    assert payload["utc_offset"] == 28800  # coerced to int
    assert payload["active_time"] == 383


def test_build_merged_payload_converts_pounds_to_kg(settings: Settings) -> None:
    h = make_activity(
        id=222,
        sets=[
            {
                "exercise": {"name": "Bench Press"},
                "reps": 5,
                "weight": 100.0,
                "weight_units": "pounds",
            }
        ],
    )
    payload = build_merged_payload(garmin_activity(), h, [70], [0], settings)
    assert payload["sets"][0]["weight"] == 45.36  # 100 * 0.45359237, 2 dp


def test_build_merged_payload_handles_alternate_set_fields(
    settings: Settings,
) -> None:
    h = make_activity(
        id=222,
        sets=[{"exercise_name": "Deadlift", "repetitions": 3, "weight_kg": 140.0}],
    )
    payload = build_merged_payload(garmin_activity(), h, [60], [0], settings)
    s = payload["sets"][0]
    assert s["exercise_type"] == "DEADLIFT_GENERIC"
    assert s["repetitions"] == 3
    assert s["weight"] == 140.0


def test_build_merged_payload_unmapped_uses_fallback(settings: Settings) -> None:
    h = make_activity(
        id=222, sets=[{"exercise": {"name": "Frobnicator 3000"}, "reps": 5}]
    )
    payload = build_merged_payload(garmin_activity(), h, [60], [0], settings)
    assert payload["sets"][0]["exercise_type"] == settings.fallback_exercise_type


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
    payload = build_merged_payload(garmin_activity(), h, [70, 72], [0, 1], settings)

    assert len(payload["sets"]) == 2
    assert payload["sets"][0] == {
        "exercise_type": "BARBELL_DEADLIFT",
        "repetitions": 6,
        "weight": 55.0,
    }
    assert payload["sets"][1]["weight"] == 60.0
    assert required_payload_problems(payload) == []


def test_unmapped_exercise_names_lists_only_unmatched(settings: Settings) -> None:
    h = make_activity(
        id=222,
        sets=[
            {"exercise": {"name": "Frobnicator 3000"}, "reps": 5},
            {"exercise": {"name": "Deadlift (Barbell)"}, "reps": 5},
        ],
    )
    assert unmapped_exercise_names(h, settings) == ["Frobnicator 3000"]


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


def test_required_payload_problems_flags_no_sets(settings: Settings) -> None:
    h = make_activity(id=222, sets=[], description=None)
    payload = build_merged_payload(garmin_activity(), h, [70], [0], settings)
    assert "no sets in payload" in required_payload_problems(payload)

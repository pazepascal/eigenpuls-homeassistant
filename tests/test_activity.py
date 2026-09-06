"""Activity Summary: the contract from `contract/activity-contract.json`.

The load-bearing property here is not the eight metrics — it is that three
different kinds of nothing stay three different things:

* **no `activity` object** — the client sent no summary for today. Nothing about
  activity changes; whatever is on display stays.
* **object present, field absent** — that value was not delivered. It must not be
  cleared, defaulted or derived from another field.
* **field present and `0`** — a real measurement of zero, which overwrites.

Measured on a real device before any of this was built: across a seven-day window
one day had no summary object at all while another had all three rings at zero.
Those are different facts about a person's day and the wire has to carry the
difference.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.apple_health_sync import registry
from custom_components.apple_health_sync.payload import (
    ACTIVITY_SNAPSHOT_FIELDS,
    PayloadError,
    parse,
)
from custom_components.apple_health_sync.registry import BucketKind
from custom_components.apple_health_sync.state import HealthState

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
TZ = "Europe/Berlin"

ACTIVITY_METRICS = (
    "activity_move_energy", "activity_move_energy_goal",
    "activity_move_time", "activity_move_time_goal",
    "activity_exercise_time", "activity_exercise_goal",
    "activity_stand_hours", "activity_stand_goal",
)


def envelope(**extra):
    body = {
        "version": 4, "type": "sync", "sent_at": NOW.isoformat(),
        "device": {"name": "iPhone", "model": "iPhone18,1", "os_version": "26.5"},
        "sync": {"id": "abc", "final": True},
        "snapshot": {},
    }
    body.update(extra)
    return body


def activity(**fields):
    base = {"date": "2026-05-29", "time_zone": TZ, "move_mode": "active_energy"}
    base.update(fields)
    return base


def snap(**fields):
    return parse(envelope(snapshot={"activity": activity(**fields)}), now=NOW).snapshot


# --- Registry ---------------------------------------------------------------


def test_exactly_eight_activity_metrics_exist():
    found = sorted(m for m in registry.METRICS if m.startswith("activity_"))
    assert found == sorted(ACTIVITY_METRICS)


@pytest.mark.parametrize("metric", ACTIVITY_METRICS)
def test_no_activity_metric_carries_an_individual_snapshot_key(metric):
    """The composite object is the current-value view, as for blood pressure."""
    assert registry.METRICS[metric].snapshot_key == ""


@pytest.mark.parametrize(
    ("metric", "unit", "unit_class", "kind", "has_sum"),
    [
        ("activity_move_energy", "kcal", "energy", BucketKind.DAILY_CUMULATIVE, True),
        ("activity_move_energy_goal", "kcal", "energy", BucketKind.DAILY_DISCRETE, False),
        ("activity_move_time", "min", "duration", BucketKind.DAILY_CUMULATIVE, True),
        ("activity_move_time_goal", "min", "duration", BucketKind.DAILY_DISCRETE, False),
        ("activity_exercise_time", "min", "duration", BucketKind.DAILY_CUMULATIVE, True),
        ("activity_exercise_goal", "min", "duration", BucketKind.DAILY_DISCRETE, False),
        ("activity_stand_hours", "hours", None, BucketKind.DAILY_CUMULATIVE, True),
        ("activity_stand_goal", "hours", None, BucketKind.DAILY_DISCRETE, False),
    ],
)
def test_registry_metadata_matches_the_frozen_contract(metric, unit, unit_class, kind, has_sum):
    spec = registry.METRICS[metric]
    assert (spec.unit, spec.unit_class, spec.kind, spec.has_sum) == (
        unit, unit_class, kind, has_sum
    )


def test_stand_hours_has_no_statistics_unit_converter():
    """Measured, not assumed - and the reason the unit is `hours` and not `h`.

    Home Assistant converts `h` as a duration. It has no converter for `hours`,
    which is what this metric needs: a count of hours that qualified, not time
    elapsed. Nine stand hours must never be rendered as 540 minutes.
    """
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
    )

    assert "hours" not in STATISTIC_UNIT_TO_UNIT_CONVERTER
    assert "h" in STATISTIC_UNIT_TO_UNIT_CONVERTER, "the trap this avoids"
    for metric in ("activity_stand_hours", "activity_stand_goal"):
        assert registry.METRICS[metric].unit == "hours"


@pytest.mark.parametrize("goal", [m for m in ACTIVITY_METRICS if m.endswith("goal")])
def test_a_goal_is_mean_only(goal):
    """A day's goal is one value: a min/max spread would be invented."""
    assert registry.METRICS[goal].required == frozenset({"mean"})
    assert not registry.METRICS[goal].has_sum


def test_active_energy_is_untouched():
    spec = registry.METRICS["active_energy"]
    assert (spec.statistic_id, spec.unit, spec.kind) == (
        "apple_health_sync:active_energy", "kcal", BucketKind.DAILY_CUMULATIVE
    )


def test_the_snapshot_field_table_matches_the_metrics():
    """Neither direction may drift: a field with no metric, or a metric with no field."""
    assert set(ACTIVITY_SNAPSHOT_FIELDS.values()) == set(ACTIVITY_METRICS)


# --- Snapshot parsing -------------------------------------------------------


def test_the_minimal_valid_object_is_the_three_required_fields():
    result = snap()
    assert result.activity is not None
    assert result.activity.day == date(2026, 5, 29)
    assert result.activity.time_zone == TZ
    assert result.activity.move_mode == "active_energy"
    for field_name in ACTIVITY_SNAPSHOT_FIELDS:
        assert getattr(result.activity, field_name) is None


def test_every_optional_field_round_trips():
    result = snap(
        move_energy=388.8, move_energy_goal=600.0,
        move_time=42.0, move_time_goal=30.0,
        exercise_time=47.0, exercise_goal=30.0,
        stand_hours=9.0, stand_goal=12.0,
    )
    assert result.activity.move_energy == 388.8
    assert result.activity.stand_goal == 12.0


@pytest.mark.parametrize("mode", ["active_energy", "move_time"])
def test_both_move_modes_are_accepted(mode):
    assert snap(move_mode=mode).activity.move_mode == mode


def test_an_unknown_move_mode_is_rejected_rather_than_defaulted():
    """Guessing would label whichever series happened to be present as the ring."""
    with pytest.raises(PayloadError) as err:
        snap(move_mode="apple_move_time")
    assert err.value.reason == "bad_activity_move_mode"


def test_a_numeric_move_mode_is_rejected():
    """Apple's raw enum values are not the wire contract."""
    with pytest.raises(PayloadError):
        snap(move_mode=1)


@pytest.mark.parametrize("missing", ["date", "time_zone", "move_mode"])
def test_a_missing_required_field_is_rejected(missing):
    fields = activity()
    del fields[missing]
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"activity": fields}), now=NOW)
    assert err.value.reason == "bad_activity_missing_field"


def test_an_unexpected_field_inside_activity_is_rejected():
    """Strict inside, like blood_pressure and last_workout.

    The tolerance for unknown *top-level* snapshot keys is forward compatibility
    for objects this receiver has never heard of. Once `activity` is known, an
    unexpected field in it means the sender believes something will be stored
    that will not be.
    """
    with pytest.raises(PayloadError) as err:
        snap(move_percent=64)
    assert err.value.reason == "bad_activity_unknown_field"


def test_an_invalid_timezone_is_rejected():
    with pytest.raises(PayloadError):
        snap(time_zone="Mars/Olympus_Mons")


def test_an_invalid_date_is_rejected():
    with pytest.raises(PayloadError):
        snap(date="2026-13-45")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_a_non_finite_or_negative_value_is_rejected(bad):
    with pytest.raises(PayloadError):
        snap(move_energy=bad)


# --- Absence vs zero: the three states --------------------------------------


def test_no_activity_object_changes_nothing():
    state = HealthState()
    state.activity_values["activity_move_energy"] = 300.0
    state.activity_move_mode = "active_energy"

    parsed = parse(envelope(snapshot={}), now=NOW)
    state.apply_snapshot(parsed.snapshot, received_at=NOW)

    assert state.activity_values["activity_move_energy"] == 300.0
    assert state.activity_move_mode == "active_energy"


def test_an_absent_field_does_not_clear_the_previous_value():
    state = HealthState()
    state.activity_values["activity_stand_hours"] = 9.0

    # A summary arrives carrying move energy but no stand hours.
    state.apply_snapshot(snap(move_energy=400.0), received_at=NOW)

    assert state.activity_values["activity_stand_hours"] == 9.0
    assert state.activity_values["activity_move_energy"] == 400.0


def test_an_explicit_zero_is_a_measurement_and_overwrites():
    state = HealthState()
    state.activity_values["activity_stand_hours"] = 9.0

    state.apply_snapshot(snap(stand_hours=0.0), received_at=NOW)

    assert state.activity_values["activity_stand_hours"] == 0.0


def test_the_three_states_are_genuinely_distinguishable():
    """All three in one place, because the whole design turns on it."""
    state = HealthState()
    state.activity_values["activity_move_energy"] = 111.0

    state.apply_snapshot(parse(envelope(snapshot={}), now=NOW).snapshot, received_at=NOW)
    assert state.activity_values["activity_move_energy"] == 111.0     # object absent

    state.apply_snapshot(snap(stand_hours=5.0), received_at=NOW)
    assert state.activity_values["activity_move_energy"] == 111.0     # field absent

    state.apply_snapshot(snap(move_energy=0.0), received_at=NOW)
    assert state.activity_values["activity_move_energy"] == 0.0       # explicit zero


def test_the_move_mode_and_day_follow_the_latest_summary():
    state = HealthState()
    state.apply_snapshot(snap(move_mode="move_time"), received_at=NOW)
    assert state.activity_move_mode == "move_time"
    assert state.activity_day == date(2026, 5, 29)
    assert state.activity_time_zone == TZ


# --- Buckets ----------------------------------------------------------------


def day_bucket(metric, **fields):
    return {"metric": metric, "date": "2026-05-29", "time_zone": TZ, **fields}


@pytest.mark.parametrize("metric", [m for m in ACTIVITY_METRICS if not m.endswith("goal")])
def test_a_value_metric_takes_a_total(metric):
    payload = parse(
        envelope(buckets={"daily": [day_bucket(metric, total=42.0)]},
                 snapshot={"activity": activity()}),
        now=NOW,
    )
    assert [b.metric for b in payload.history.daily] == [metric]


@pytest.mark.parametrize("metric", [m for m in ACTIVITY_METRICS if m.endswith("goal")])
def test_a_goal_metric_takes_a_mean(metric):
    payload = parse(
        envelope(buckets={"daily": [day_bucket(metric, mean=600.0)]},
                 snapshot={"activity": activity()}),
        now=NOW,
    )
    assert [b.metric for b in payload.history.daily] == [metric]


@pytest.mark.parametrize("metric", [m for m in ACTIVITY_METRICS if m.endswith("goal")])
def test_a_goal_with_a_spread_is_rejected(metric):
    with pytest.raises(PayloadError):
        parse(
            envelope(buckets={"daily": [day_bucket(metric, mean=600.0, min=1.0, max=2.0)]},
                     snapshot={"activity": activity()}),
            now=NOW,
        )


def test_a_value_metric_sent_as_a_goal_shape_is_rejected():
    with pytest.raises(PayloadError):
        parse(
            envelope(buckets={"daily": [day_bucket("activity_move_energy", mean=1.0)]},
                     snapshot={"activity": activity()}),
            now=NOW,
        )


def test_an_activity_metric_sent_as_an_hourly_bucket_is_rejected():
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(
                buckets={"hourly": [{
                    "metric": "activity_move_energy",
                    "start": "2026-05-29T11:00:00+00:00",
                    "mean": 1.0, "min": 1.0, "max": 1.0,
                }]},
                snapshot={"activity": activity()},
            ),
            now=NOW,
        )
    assert err.value.reason == "wrong_bucket_kind"


def test_a_zero_bucket_survives():
    """A day of no movement is data, not an absence."""
    payload = parse(
        envelope(buckets={"daily": [day_bucket("activity_stand_hours", total=0.0)]},
                 snapshot={"activity": activity()}),
        now=NOW,
    )
    assert payload.history.daily[0].total == 0.0


# --- Support negotiation ----------------------------------------------------


def test_all_eight_metrics_are_published_as_supported():
    assert set(ACTIVITY_METRICS) <= set(registry.SUPPORTED_METRICS)


def test_the_activity_feature_is_published():
    assert "snapshot.activity" in registry.SUPPORTED_FEATURES
    assert len(registry.SUPPORTED_FEATURES) == 5


def test_the_eight_statistic_ids_are_additive_and_correctly_spelled():
    ids = {registry.METRICS[m].statistic_id for m in ACTIVITY_METRICS}
    assert ids == {f"apple_health_sync:{m}" for m in ACTIVITY_METRICS}

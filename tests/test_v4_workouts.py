"""Phase 3B.2: workout summaries and daily training aggregates.

Deliberately not a second Apple Health workout database. Home Assistant keeps
the latest session in detail and durable daily totals; Apple Health remains
authoritative for individual history.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.apple_health_sync.payload import (
    AggregateHistory,
    MetricDayBucket,
    PayloadError,
    parse,
)
from custom_components.apple_health_sync.registry import METRICS, WORKOUT_ACTIVITIES
from custom_components.apple_health_sync.statistics import async_import_history

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)
TZ = "Europe/Berlin"
BASE_DAY = date(2026, 6, 10)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def envelope(**overrides):
    body = {
        "version": 4, "type": "sync", "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True}, "snapshot": {},
    }
    body.update(overrides)
    return body


def workout(**overrides):
    body = {
        "uuid": "F1A2-0001",
        "activity": "strength_training",
        "start": iso(NOW - timedelta(hours=3)),
        "end": iso(NOW - timedelta(hours=2)),
        "duration_min": 55.0,
        "active_energy_kcal": 410.0,
        "avg_heart_rate_bpm": 126.0,
        "max_heart_rate_bpm": 158.0,
        "source": "Apple Watch",
    }
    body.update(overrides)
    return body


def daily(metric, offset=0, total=1.0):
    return MetricDayBucket(
        metric=metric, day=BASE_DAY + timedelta(days=offset),
        time_zone=TZ, total=total,
    )


async def read(hass, statistic_id, types):
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        functools.partial(
            statistics_during_period, hass, datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 8, 1, tzinfo=UTC), {statistic_id}, "hour", None, types,
        )
    )
    return rows.get(statistic_id, [])


# --- The snapshot ------------------------------------------------------------


def test_a_full_workout_summary_is_accepted():
    parsed = parse(envelope(snapshot={"last_workout": workout()}), now=NOW)
    last = parsed.snapshot.last_workout

    assert last.uuid == "F1A2-0001"
    assert last.activity == "strength_training"
    assert last.duration_min == 55.0
    assert last.active_energy_kcal == 410.0
    assert last.avg_heart_rate_bpm == 126.0
    assert last.max_heart_rate_bpm == 158.0
    assert last.source == "Apple Watch"


def test_only_the_core_fields_are_required():
    """A strength session has no distance; an unworn watch has no heart rate."""
    minimal = {
        "uuid": "F1A2-0002", "activity": "yoga",
        "start": iso(NOW - timedelta(hours=2)), "end": iso(NOW - timedelta(hours=1)),
        "duration_min": 45.0,
    }
    last = parse(envelope(snapshot={"last_workout": minimal}), now=NOW).snapshot.last_workout

    assert last.activity == "yoga"
    # Absent, never zero.
    assert last.active_energy_kcal is None
    assert last.distance_km is None
    assert last.avg_heart_rate_bpm is None
    assert last.max_heart_rate_bpm is None


@pytest.mark.parametrize("missing", ["uuid", "activity", "start", "end", "duration_min"])
def test_a_missing_required_field_is_rejected(missing):
    body = workout()
    del body[missing]
    with pytest.raises(PayloadError):
        parse(envelope(snapshot={"last_workout": body}), now=NOW)


def test_the_uuid_is_required_by_name():
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"last_workout": workout(uuid="")}), now=NOW)
    assert err.value.reason == "workout_missing_uuid"


def test_an_activity_outside_the_vocabulary_is_rejected():
    """The client maps anything it does not know to `other`, so a stray
    identifier means the two halves disagree about the taxonomy."""
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"last_workout": workout(activity="kitesurfing")}), now=NOW)
    assert err.value.reason == "workout_unknown_activity"


def test_other_is_a_valid_activity():
    last = parse(
        envelope(snapshot={"last_workout": workout(activity="other")}), now=NOW
    ).snapshot.last_workout
    assert last.activity == "other"


@pytest.mark.parametrize("activity", WORKOUT_ACTIVITIES)
def test_every_declared_activity_is_accepted(activity):
    last = parse(
        envelope(snapshot={"last_workout": workout(activity=activity)}), now=NOW
    ).snapshot.last_workout
    assert last.activity == activity


def test_a_workout_ending_before_it_starts_is_rejected():
    body = workout(start=iso(NOW - timedelta(hours=1)), end=iso(NOW - timedelta(hours=3)))
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"last_workout": body}), now=NOW)
    assert err.value.reason == "workout_ends_before_it_starts"


def test_a_duration_longer_than_the_span_is_rejected():
    """Pause-aware duration can be shorter than the span, never longer."""
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"last_workout": workout(duration_min=600.0)}), now=NOW)
    assert err.value.reason == "workout_duration_exceeds_span"


def test_a_paused_workout_reports_less_than_its_span():
    """Sixty minutes of wall clock, forty of training."""
    last = parse(
        envelope(snapshot={"last_workout": workout(duration_min=40.0)}), now=NOW
    ).snapshot.last_workout
    span = (last.end - last.start).total_seconds() / 60
    assert last.duration_min == 40.0
    assert span == 60.0
    assert last.duration_min < span


def test_a_negative_optional_value_is_rejected():
    with pytest.raises(PayloadError):
        parse(envelope(snapshot={"last_workout": workout(active_energy_kcal=-5.0)}), now=NOW)


def test_no_workout_leaves_the_sensor_alone():
    assert parse(envelope(snapshot={}), now=NOW).snapshot.last_workout is None


# --- The daily aggregates ----------------------------------------------------


def test_the_three_training_metrics_are_daily_cumulative():
    for metric, unit, unit_class in (
        ("workout_count", "workouts", None),
        ("workout_duration", "min", "duration"),
        ("workout_energy", "kcal", "energy"),
    ):
        spec = METRICS[metric]
        assert spec.has_sum, metric
        assert spec.unit == unit
        assert spec.unit_class == unit_class
        # None of them has an individual snapshot: the current view of training
        # is the composite last_workout.
        assert spec.snapshot_key == ""


def test_there_is_no_daily_distance_aggregate():
    """Summing kilometres across swimming, cycling and running is arithmetic
    without meaning. Distance stays on the individual workout."""
    assert "workout_distance" not in METRICS
    assert not any("workout" in m and "distance" in m for m in METRICS)


def test_there_is_no_event_family_or_event_store():
    """Home Assistant keeps a latest summary and daily totals, not a workout log."""
    from custom_components.apple_health_sync import payload as payload_module

    assert not hasattr(payload_module, "WorkoutEvent")
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"events": [{"kind": "workout"}]}), now=NOW)
    assert err.value.reason == "unknown_bucket_kind"


def test_workout_daily_buckets_are_accepted():
    parsed = parse(
        envelope(buckets={"daily": [
            {"metric": "workout_count", "date": "2026-06-19", "time_zone": TZ, "total": 2},
            {"metric": "workout_duration", "date": "2026-06-19", "time_zone": TZ, "total": 95.0},
            {"metric": "workout_energy", "date": "2026-06-19", "time_zone": TZ, "total": 610.0},
        ]}),
        now=NOW,
    )
    by_metric = {b.metric: b.total for b in parsed.history.daily}
    assert by_metric == {
        "workout_count": 2, "workout_duration": 95.0, "workout_energy": 610.0,
    }


async def test_the_training_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(daily=[
        daily("workout_count", total=2.0),
        daily("workout_duration", total=95.0),
        daily("workout_energy", total=610.0),
    ]))
    await async_wait_recording_done(hass)

    for suffix, unit, unit_class in (
        ("workout_count", "workouts", None),
        ("workout_duration", "min", "duration"),
        ("workout_energy", "kcal", "energy"),
    ):
        found = await get_instance(hass).async_add_executor_job(
            functools.partial(
                get_metadata, hass,
                statistic_ids={f"apple_health_sync:{suffix}"},
            )
        )
        meta = found[f"apple_health_sync:{suffix}"][1]
        assert meta["unit_of_measurement"] == unit, suffix
        assert meta["unit_class"] == unit_class, suffix
        assert meta["has_sum"] is True, suffix


async def test_training_totals_accumulate_as_a_running_sum(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(daily=[
        daily("workout_duration", offset=0, total=60.0),
        daily("workout_duration", offset=1, total=35.0),
    ]))
    rows = await read(hass, "apple_health_sync:workout_duration", {"state", "sum"})

    assert [r["state"] for r in rows] == pytest.approx([60.0, 35.0])
    assert [r["sum"] for r in rows] == pytest.approx([60.0, 95.0])


async def test_an_overlapping_reimport_does_not_double_count(
    recorder_mock, hass: HomeAssistant
):
    """The 90-day window re-sends the same days on every sync."""
    for _ in range(3):
        await async_import_history(hass, AggregateHistory(daily=[
            daily("workout_count", offset=0, total=2.0),
        ]))
    rows = await read(hass, "apple_health_sync:workout_count", {"state", "sum"})

    assert len(rows) == 1
    assert rows[0]["state"] == pytest.approx(2.0), "count must not become 6"
    assert rows[0]["sum"] == pytest.approx(2.0)


async def test_a_corrected_day_replaces_its_previous_value(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(daily=[daily("workout_count", total=1.0)]))
    # A second workout that day arrives later.
    await async_import_history(hass, AggregateHistory(daily=[daily("workout_count", total=2.0)]))
    rows = await read(hass, "apple_health_sync:workout_count", {"state"})

    assert len(rows) == 1
    assert rows[0]["state"] == pytest.approx(2.0)


async def test_a_day_whose_workouts_recorded_no_energy_writes_no_energy_row(
    recorder_mock, hass: HomeAssistant
):
    """The distinction the aggregate must preserve.

    A day with workouts but no energy data is not a day of zero energy. The
    count and duration rows exist; the energy row is simply absent, so
    "trained but energy unknown" stays distinguishable from "burned nothing".
    """
    await async_import_history(hass, AggregateHistory(daily=[
        daily("workout_count", total=1.0),
        daily("workout_duration", total=45.0),
    ]))

    assert len(await read(hass, "apple_health_sync:workout_count", {"state"})) == 1
    assert len(await read(hass, "apple_health_sync:workout_duration", {"state"})) == 1
    assert await read(hass, "apple_health_sync:workout_energy", {"state"}) == []


async def test_a_measured_zero_energy_is_still_recorded(
    recorder_mock, hass: HomeAssistant
):
    """The other half: a genuine zero is data."""
    await async_import_history(hass, AggregateHistory(daily=[daily("workout_energy", total=0.0)]))
    rows = await read(hass, "apple_health_sync:workout_energy", {"state"})

    assert len(rows) == 1
    assert rows[0]["state"] == pytest.approx(0.0)


async def test_a_day_without_training_writes_nothing(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(daily=[daily("workout_count", offset=0, total=1.0)]))
    rows = await read(hass, "apple_health_sync:workout_count", {"state"})
    # Only the day that had a workout exists; the rest are absent, not zero.
    assert len(rows) == 1


# --- Compatibility -----------------------------------------------------------


def test_a_payload_without_any_workout_data_is_unchanged():
    """The installed client sends none of this."""
    parsed = parse(
        envelope(snapshot={"heart_rate": {
            "value": 61.0, "unit": "count/min",
            "measured_at": iso(NOW - timedelta(minutes=5)),
        }}),
        now=NOW,
    )
    assert parsed.rejected == []
    assert parsed.snapshot.heart_rate.value == 61.0
    assert parsed.snapshot.last_workout is None


@pytest.mark.parametrize("version", [1, 2, 3])
def test_earlier_versions_still_parse(version):
    body = envelope(version=version)
    if version == 1:
        body.pop("sync"), body.pop("snapshot")
    else:
        body["snapshot"] = {"heart_rate": None, "steps_today": None}
    assert parse(body, now=NOW).version == version

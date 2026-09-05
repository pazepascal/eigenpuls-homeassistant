"""Recorder-backed tests for the v4 statistic series.

These run against a real Recorder on an in-memory SQLite database, so the
metadata Home Assistant actually stores is asserted rather than the metadata we
believe we passed. That matters most for ``unit_class``: kcal, km, % and minutes
all map to unit converters, unlike the bpm and steps of earlier phases, and a
wrong value there is refused or silently reinterpreted at import time.

The live Home Assistant database is never touched.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
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
    MetricHourBucket,
    NightlySleep,
)
from custom_components.apple_health_sync.registry import (
    BLOOD_PRESSURE_COUNT,
    METRICS,
    SLEEP_SERIES,
    STATISTIC_IDS,
    MeanType,
)
from custom_components.apple_health_sync.statistics import (
    _MEAN_TYPES,
    async_import_history,
    day_start_utc,
    metadata_for,
    sleep_offset_minutes,
)

BASE_DAY = date(2026, 6, 10)
TZ = "Europe/Berlin"


def daily(metric, offset=0, **fields):
    return MetricDayBucket(
        metric=metric, day=BASE_DAY + timedelta(days=offset), time_zone=TZ, **fields
    )


def night(offset=0, **fields):
    wake = BASE_DAY + timedelta(days=offset)
    record = {
        "total_sleep_min": 430.0,
        "sleep_start": datetime(2026, 6, 9, 21, 30, tzinfo=UTC) + timedelta(days=offset),
        "wake_time": datetime(2026, 6, 10, 5, 10, tzinfo=UTC) + timedelta(days=offset),
        "rem_min": 90.0, "core_min": 250.0, "deep_min": 60.0, "awake_min": 25.0,
    }
    record.update(fields)
    return NightlySleep(day=wake, time_zone=TZ, **record)


async def read(hass: HomeAssistant, statistic_id: str, types: set[str]):
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
        {statistic_id},
        "hour",
        None,
        types,
    )
    return rows.get(statistic_id, [])


async def read_metadata(hass: HomeAssistant, statistic_id: str) -> dict:
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        functools.partial(get_metadata, hass, statistic_ids={statistic_id})
    )
    assert statistic_id in found, f"no metadata stored for {statistic_id}"
    return found[statistic_id][1]


# --- Naps are their own daily metric ------------------------------------------


def test_no_statistic_id_has_two_writers():
    """The invariant behind moving naps out of the nightly record.

    Two paths writing one statistic id within a request would be last-write-wins
    and therefore nondeterministic. The nightly fan-out and the metric registry
    must stay disjoint.
    """
    metric_ids = {spec.statistic_id for spec in METRICS.values()}
    nightly_ids = {spec.statistic_id for spec in SLEEP_SERIES.values()}
    # The blood-pressure weight series is derived from a bucket field rather
    # than being a wire metric, so it is a third disjoint writer.
    derived_ids = {BLOOD_PRESSURE_COUNT.statistic_id}

    assert not metric_ids & nightly_ids
    assert not metric_ids & derived_ids
    assert not nightly_ids & derived_ids
    assert len(metric_ids) + len(nightly_ids) + len(derived_ids) == len(STATISTIC_IDS)
    # Naps are written by the metric path only.
    assert "apple_health_sync:nap_total" in metric_ids
    assert "apple_health_sync:nap_count" in metric_ids
    assert not any("nap" in i for i in nightly_ids)


def test_the_nap_statistic_ids_are_unchanged():
    """Existing Home Assistant history must not be orphaned by the move."""
    assert METRICS["nap_total"].statistic_id == "apple_health_sync:nap_total"
    assert METRICS["nap_count"].statistic_id == "apple_health_sync:nap_count"


async def test_nap_total_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("nap_total", mean=45.0)])
    )
    meta = await read_metadata(hass, "apple_health_sync:nap_total")

    assert meta["unit_of_measurement"] == "min"
    assert meta["unit_class"] == "duration"
    assert meta["has_sum"] is False
    assert meta["mean_type"] is StatisticMeanType.ARITHMETIC


async def test_nap_count_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("nap_count", mean=2.0)])
    )
    meta = await read_metadata(hass, "apple_health_sync:nap_count")

    assert meta["unit_of_measurement"] == "naps"
    assert meta["unit_class"] is None


async def test_naps_are_stored_without_any_nightly_record(
    recorder_mock, hass: HomeAssistant
):
    """A day of naps and no main sleep is real data and must be stored."""
    await async_import_history(
        hass,
        AggregateHistory(
            daily=[daily("nap_total", mean=90.0), daily("nap_count", mean=2.0)]
        ),
    )

    total = await read(hass, "apple_health_sync:nap_total", {"mean"})
    count = await read(hass, "apple_health_sync:nap_count", {"mean"})
    assert len(total) == 1 and total[0]["mean"] == pytest.approx(90.0)
    assert len(count) == 1 and count[0]["mean"] == pytest.approx(2.0)
    # And no night was invented to carry them.
    assert await read(hass, "apple_health_sync:sleep_total", {"mean"}) == []


async def test_a_night_writes_no_nap_rows(recorder_mock, hass: HomeAssistant):
    """The nightly fan-out no longer touches the nap series at all."""
    await async_import_history(hass, AggregateHistory(nightly=[night()]))

    assert await read(hass, "apple_health_sync:sleep_total", {"mean"}) != []
    assert await read(hass, "apple_health_sync:nap_total", {"mean"}) == []
    assert await read(hass, "apple_health_sync:nap_count", {"mean"}) == []


async def test_naps_and_a_night_on_one_day_do_not_collide(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass,
        AggregateHistory(
            daily=[daily("nap_total", mean=45.0), daily("nap_count", mean=1.0)],
            nightly=[night()],
        ),
    )

    assert (await read(hass, "apple_health_sync:nap_total", {"mean"}))[0]["mean"] == 45.0
    assert (await read(hass, "apple_health_sync:sleep_total", {"mean"}))[0]["mean"] == 430.0


async def test_reimporting_naps_is_idempotent(recorder_mock, hass: HomeAssistant):
    for _ in range(2):
        await async_import_history(
            hass, AggregateHistory(daily=[daily("nap_total", mean=45.0)])
        )
    rows = await read(hass, "apple_health_sync:nap_total", {"mean"})
    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(45.0)


async def test_a_measured_zero_nap_total_writes_a_row(
    recorder_mock, hass: HomeAssistant
):
    """Zero naps measured is data; no nap bucket at all is absence."""
    await async_import_history(
        hass, AggregateHistory(daily=[daily("nap_total", mean=0.0)])
    )
    rows = await read(hass, "apple_health_sync:nap_total", {"mean"})
    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(0.0)


# --- Phase 3B.1: body composition and blood pressure --------------------------
#
# The metadata is read back out of the recorder rather than compared with what we
# passed in: kg, % and mmHg all map to unit converters, and a wrong unit_class is
# refused at import time rather than at review time.


def hourly_bucket(metric, offset=0, mean=80.0, lo=None, hi=None):
    return MetricHourBucket(
        metric=metric,
        start=datetime(2026, 6, 10, 7, tzinfo=UTC) + timedelta(hours=offset),
        mean=mean, minimum=lo if lo is not None else mean,
        maximum=hi if hi is not None else mean,
    )


async def test_body_mass_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass, AggregateHistory(hourly=[hourly_bucket("body_mass", mean=81.4)])
    )
    meta = await read_metadata(hass, "apple_health_sync:body_mass")

    assert meta["unit_of_measurement"] == "kg"
    assert meta["unit_class"] == "mass"
    assert meta["has_sum"] is False
    assert meta["mean_type"] is StatisticMeanType.ARITHMETIC


async def test_body_fat_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass,
        AggregateHistory(
            hourly=[hourly_bucket("body_fat_percentage", mean=18.2, lo=18.0, hi=18.4)]
        ),
    )
    meta = await read_metadata(hass, "apple_health_sync:body_fat_percentage")

    assert meta["unit_of_measurement"] == "%"
    assert meta["unit_class"] == "unitless"


async def test_blood_pressure_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass,
        AggregateHistory(hourly=[
            hourly_bucket("blood_pressure_systolic", mean=128.0),
            hourly_bucket("blood_pressure_diastolic", mean=82.0),
        ]),
    )
    for suffix in ("blood_pressure_systolic", "blood_pressure_diastolic"):
        meta = await read_metadata(hass, f"apple_health_sync:{suffix}")
        assert meta["unit_of_measurement"] == "mmHg", suffix
        # mmHg maps to the pressure converter, so None here would be wrong.
        assert meta["unit_class"] == "pressure", suffix
        assert meta["has_sum"] is False


async def test_the_two_pressure_series_stay_independent(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass,
        AggregateHistory(hourly=[
            hourly_bucket("blood_pressure_systolic", mean=128.0),
            hourly_bucket("blood_pressure_diastolic", mean=82.0),
        ]),
    )
    systolic = await read(hass, "apple_health_sync:blood_pressure_systolic", {"mean"})
    diastolic = await read(hass, "apple_health_sync:blood_pressure_diastolic", {"mean"})

    assert systolic[0]["mean"] == pytest.approx(128.0)
    assert diastolic[0]["mean"] == pytest.approx(82.0)


async def test_body_metrics_are_discrete_not_cumulative(
    recorder_mock, hass: HomeAssistant
):
    """A running sum of body mass would be meaningless."""
    await async_import_history(
        hass,
        AggregateHistory(hourly=[
            hourly_bucket("body_mass", offset=0, mean=81.4),
            hourly_bucket("body_mass", offset=24, mean=81.1),
        ]),
    )
    rows = await read(hass, "apple_health_sync:body_mass", {"mean", "sum"})
    assert [r["mean"] for r in rows] == pytest.approx([81.4, 81.1])
    assert all(r.get("sum") is None for r in rows)


async def test_reimporting_body_metrics_upserts(recorder_mock, hass: HomeAssistant):
    for value in (81.4, 81.2):
        await async_import_history(
            hass, AggregateHistory(hourly=[hourly_bucket("body_mass", mean=value)])
        )
    rows = await read(hass, "apple_health_sync:body_mass", {"mean"})

    assert len(rows) == 1, "an overlapping window must not duplicate a weigh-in"
    assert rows[0]["mean"] == pytest.approx(81.2), "the corrected value wins"


async def test_an_overlapping_window_does_not_compound_body_mass(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass,
        AggregateHistory(hourly=[
            hourly_bucket("body_mass", offset=0, mean=81.4),
            hourly_bucket("body_mass", offset=24, mean=81.1),
        ]),
    )
    await async_import_history(
        hass,
        AggregateHistory(hourly=[
            hourly_bucket("body_mass", offset=24, mean=81.1),
            hourly_bucket("body_mass", offset=48, mean=80.9),
        ]),
    )
    rows = await read(hass, "apple_health_sync:body_mass", {"mean"})
    assert [r["mean"] for r in rows] == pytest.approx([81.4, 81.1, 80.9])


async def test_an_absent_measurement_writes_no_row(recorder_mock, hass: HomeAssistant):
    """Absence is never a zero row."""
    await async_import_history(
        hass, AggregateHistory(hourly=[hourly_bucket("body_mass", mean=81.4)])
    )
    assert await read(hass, "apple_health_sync:body_fat_percentage", {"mean"}) == []
    assert await read(hass, "apple_health_sync:blood_pressure_systolic", {"mean"}) == []


# --- The enum bridge ---------------------------------------------------------


def test_our_mean_types_map_onto_home_assistants():
    """Our registry keeps its own enum so the parsing layer stays HA-free.

    If Home Assistant ever adds a mean type we care about, this is where the two
    are pinned together.
    """
    assert _MEAN_TYPES[MeanType.NONE] is StatisticMeanType.NONE
    assert _MEAN_TYPES[MeanType.ARITHMETIC] is StatisticMeanType.ARITHMETIC
    assert set(_MEAN_TYPES) == set(MeanType)


# --- 14 / 15 / 16 / 17: stored metadata -------------------------------------


async def test_active_energy_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("active_energy", total=612.0)])
    )
    meta = await read_metadata(hass, "apple_health_sync:active_energy")

    assert meta["unit_of_measurement"] == "kcal"
    # kcal maps to Home Assistant's energy converter; None here would be wrong.
    assert meta["unit_class"] == "energy"
    assert meta["has_sum"] is True
    assert meta["mean_type"] is StatisticMeanType.NONE
    assert meta["source"] == "apple_health_sync"


async def test_distance_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("distance_walking_running", total=7.4)])
    )
    meta = await read_metadata(hass, "apple_health_sync:distance")

    assert meta["unit_of_measurement"] == "km"
    assert meta["unit_class"] == "distance"
    assert meta["has_sum"] is True


async def test_daily_discrete_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass,
        AggregateHistory(
            daily=[daily("hrv_sdnn", mean=44.0, minimum=21.0, maximum=88.0)]
        ),
    )
    meta = await read_metadata(hass, "apple_health_sync:hrv_sdnn")

    assert meta["unit_of_measurement"] == "ms"
    # Milliseconds map to the duration converter.
    assert meta["unit_class"] == "duration"
    assert meta["has_sum"] is False
    assert meta["mean_type"] is StatisticMeanType.ARITHMETIC


async def test_blood_oxygen_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass,
        AggregateHistory(
            daily=[daily("oxygen_saturation", mean=96.5, minimum=92.0, maximum=99.0)]
        ),
    )
    meta = await read_metadata(hass, "apple_health_sync:oxygen_saturation")

    assert meta["unit_of_measurement"] == "%"
    assert meta["unit_class"] == "unitless"


async def test_resting_heart_rate_metadata_and_absent_spread(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("resting_heart_rate", mean=54.0)])
    )
    meta = await read_metadata(hass, "apple_health_sync:resting_heart_rate")
    assert meta["unit_of_measurement"] == "bpm"
    assert meta["unit_class"] is None

    rows = await read(hass, "apple_health_sync:resting_heart_rate", {"mean", "min", "max"})
    assert rows[0]["mean"] == pytest.approx(54.0)
    # No fabricated spread: Apple derives one value per day.
    assert rows[0].get("min") is None
    assert rows[0].get("max") is None


async def test_sleep_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(nightly=[night()]))
    meta = await read_metadata(hass, "apple_health_sync:sleep_total")

    assert meta["unit_of_measurement"] == "min"
    assert meta["unit_class"] == "duration"
    assert meta["has_sum"] is False
    # Mean, not sum: "average sleep per night" is the meaningful rollup, so Home
    # Assistant's own week and month aggregation gives the trend directly.
    assert meta["mean_type"] is StatisticMeanType.ARITHMETIC


async def test_every_registered_series_produces_metadata_home_assistant_accepts(
    recorder_mock, hass: HomeAssistant
):
    """A wrong unit_class is refused at import; this exercises all of them."""
    for spec in list(METRICS.values()) + list(SLEEP_SERIES.values()):
        assert metadata_for(spec)["statistic_id"] == spec.statistic_id


# --- Values round-trip -------------------------------------------------------


async def test_sleep_stages_round_trip(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(nightly=[night()]))

    for suffix, expected in (
        ("sleep_total", 430.0), ("sleep_rem", 90.0),
        ("sleep_core", 250.0), ("sleep_deep", 60.0), ("sleep_awake", 25.0),
    ):
        rows = await read(hass, f"apple_health_sync:{suffix}", {"mean"})
        assert len(rows) == 1, suffix
        assert rows[0]["mean"] == pytest.approx(expected), suffix


async def test_a_missing_stage_writes_no_row_at_all(recorder_mock, hass: HomeAssistant):
    """Null must not become zero anywhere, storage included."""
    await async_import_history(
        hass, AggregateHistory(nightly=[night(rem_min=None, deep_min=None)])
    )

    assert await read(hass, "apple_health_sync:sleep_rem", {"mean"}) == []
    assert await read(hass, "apple_health_sync:sleep_deep", {"mean"}) == []
    # The night still contributes its total and the stages it does have.
    assert len(await read(hass, "apple_health_sync:sleep_total", {"mean"})) == 1
    assert len(await read(hass, "apple_health_sync:sleep_core", {"mean"})) == 1


async def test_a_zero_stage_does_write_a_row(recorder_mock, hass: HomeAssistant):
    """The other half of the contract: measured zero is real data."""
    await async_import_history(hass, AggregateHistory(nightly=[night(rem_min=0.0)]))

    rows = await read(hass, "apple_health_sync:sleep_rem", {"mean"})
    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(0.0)


async def test_bedtime_is_stored_as_an_offset_from_the_anchor(
    recorder_mock, hass: HomeAssistant
):
    """Offsets, because averaging clock times across midnight is wrong."""
    record = night()
    await async_import_history(hass, AggregateHistory(nightly=[record]))

    rows = await read(hass, "apple_health_sync:sleep_start_offset", {"mean"})
    # 21:30 UTC is 23:30 Berlin summer time: five and a half hours after 18:00.
    assert rows[0]["mean"] == pytest.approx(330.0)
    assert sleep_offset_minutes(record.sleep_start, record) == pytest.approx(330.0)

    wake = await read(hass, "apple_health_sync:sleep_wake_offset", {"mean"})
    # 05:10 UTC is 07:10 Berlin: thirteen hours and ten minutes after 18:00.
    assert wake[0]["mean"] == pytest.approx(790.0)


async def test_a_bedtime_after_midnight_does_not_wrap(recorder_mock, hass: HomeAssistant):
    """00:30 must read as later than 23:30, not as the smallest value of the day."""
    late = night(
        sleep_start=datetime(2026, 6, 9, 22, 30, tzinfo=UTC),  # 00:30 Berlin
    )
    early = night(offset=1, sleep_start=datetime(2026, 6, 10, 21, 30, tzinfo=UTC))

    await async_import_history(hass, AggregateHistory(nightly=[late, early]))
    rows = await read(hass, "apple_health_sync:sleep_start_offset", {"mean"})

    assert rows[0]["mean"] == pytest.approx(390.0)   # 00:30 -> 6.5h after 18:00
    assert rows[1]["mean"] == pytest.approx(330.0)   # 23:30 -> 5.5h after 18:00
    assert rows[0]["mean"] > rows[1]["mean"]


# --- 9 / 10: upsert and late corrections ------------------------------------


async def test_resending_a_night_replaces_it_rather_than_duplicating(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(nightly=[night()]))
    await async_import_history(hass, AggregateHistory(nightly=[night()]))

    rows = await read(hass, "apple_health_sync:sleep_total", {"mean"})
    assert len(rows) == 1


async def test_a_late_watch_correction_replaces_the_earlier_values(
    recorder_mock, hass: HomeAssistant
):
    """The Watch often uploads hours after waking; the window must self-heal."""
    await async_import_history(
        hass, AggregateHistory(nightly=[night(total_sleep_min=300.0, rem_min=None)])
    )
    await async_import_history(
        hass, AggregateHistory(nightly=[night(total_sleep_min=430.0, rem_min=90.0)])
    )

    totals = await read(hass, "apple_health_sync:sleep_total", {"mean"})
    assert len(totals) == 1
    assert totals[0]["mean"] == pytest.approx(430.0)

    # The stage that was missing first time round now exists.
    rem = await read(hass, "apple_health_sync:sleep_rem", {"mean"})
    assert len(rem) == 1
    assert rem[0]["mean"] == pytest.approx(90.0)


async def test_energy_and_distance_keep_a_running_sum_like_steps(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass,
        AggregateHistory(
            daily=[
                daily("active_energy", offset=0, total=500.0),
                daily("active_energy", offset=1, total=300.0),
                daily("distance_walking_running", offset=0, total=6.0),
                daily("distance_walking_running", offset=1, total=4.0),
            ]
        ),
    )

    energy = await read(hass, "apple_health_sync:active_energy", {"state", "sum"})
    assert [row["state"] for row in energy] == pytest.approx([500.0, 300.0])
    assert [row["sum"] for row in energy] == pytest.approx([500.0, 800.0])

    distance = await read(hass, "apple_health_sync:distance", {"state", "sum"})
    assert [row["sum"] for row in distance] == pytest.approx([6.0, 10.0])


async def test_an_overlapping_window_does_not_compound_the_running_sum(
    recorder_mock, hass: HomeAssistant
):
    """The rolling window re-sends days it has already sent; that must be safe."""
    await async_import_history(
        hass,
        AggregateHistory(daily=[
            daily("active_energy", offset=0, total=500.0),
            daily("active_energy", offset=1, total=300.0),
        ]),
    )
    await async_import_history(
        hass,
        AggregateHistory(daily=[
            daily("active_energy", offset=1, total=300.0),
            daily("active_energy", offset=2, total=200.0),
        ]),
    )

    rows = await read(hass, "apple_health_sync:active_energy", {"state", "sum"})
    assert [row["sum"] for row in rows] == pytest.approx([500.0, 800.0, 1000.0])


async def test_metrics_absent_from_a_window_are_left_alone(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass, AggregateHistory(daily=[daily("active_energy", total=500.0)])
    )
    await async_import_history(
        hass, AggregateHistory(daily=[daily("steps", total=8000.0)])
    )

    # Importing steps must not have disturbed the energy series.
    energy = await read(hass, "apple_health_sync:active_energy", {"sum"})
    assert len(energy) == 1
    assert energy[0]["sum"] == pytest.approx(500.0)


async def test_a_night_lands_on_the_local_day_it_belongs_to(
    recorder_mock, hass: HomeAssistant
):
    record = night()
    await async_import_history(hass, AggregateHistory(nightly=[record]))
    rows = await read(hass, "apple_health_sync:sleep_total", {"mean"})

    assert datetime.fromtimestamp(float(rows[0]["start"]), UTC) == day_start_utc(record)

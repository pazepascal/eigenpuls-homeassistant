"""Measurement-weighted blood-pressure trends, against a real recorder.

The failure this guards is silent: Home Assistant's arithmetic rollup is an
unweighted mean of hourly means, so an hour holding three readings would count
for no more than an hour holding one. The resulting number looks entirely
plausible and is wrong.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime, timedelta

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
    MetricHourBucket,
)
from custom_components.apple_health_sync.registry import BLOOD_PRESSURE_COUNT
from custom_components.apple_health_sync.statistics import (
    async_blood_pressure_trend,
    async_import_history,
)

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def hour(offset_hours: int) -> datetime:
    """An hour-aligned start, ``offset_hours`` before NOW."""
    return NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset_hours)


def bp(offset_hours: int, systolic: float, diastolic: float, count: int,
       sys_lo: float | None = None, sys_hi: float | None = None):
    """Both halves of one blood-pressure hour, sharing a count."""
    start = hour(offset_hours)
    return [
        MetricHourBucket(
            metric="blood_pressure_systolic", start=start, mean=systolic,
            minimum=sys_lo if sys_lo is not None else systolic,
            maximum=sys_hi if sys_hi is not None else systolic, count=count,
        ),
        MetricHourBucket(
            metric="blood_pressure_diastolic", start=start, mean=diastolic,
            minimum=diastolic, maximum=diastolic, count=count,
        ),
    ]


async def trend(hass, days: int, overlay=None):
    await async_wait_recording_done(hass)
    return await async_blood_pressure_trend(hass, days=days, now=NOW, overlay=overlay)


async def read(hass, statistic_id, types):
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        functools.partial(
            statistics_during_period, hass, NOW - timedelta(days=90),
            NOW + timedelta(days=1), {statistic_id}, "hour", None, types,
        )
    )
    return rows.get(statistic_id, [])


# --- The count series --------------------------------------------------------


async def test_the_count_series_is_stored(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 128, 82, 3)))
    rows = await read(hass, "apple_health_sync:blood_pressure_count", {"mean"})

    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(3.0)


async def test_the_count_series_metadata(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 128, 82, 1)))
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        functools.partial(
            get_metadata, hass,
            statistic_ids={BLOOD_PRESSURE_COUNT.statistic_id},
        )
    )
    meta = found[BLOOD_PRESSURE_COUNT.statistic_id][1]
    assert meta["unit_of_measurement"] == "measurements"
    assert meta["unit_class"] is None
    assert meta["has_sum"] is False


# --- The weighting, which is the point --------------------------------------


async def test_the_exact_example_is_measurement_weighted(
    recorder_mock, hass: HomeAssistant
):
    """One reading in one hour, three in another.

    120/80, then 130/85, 140/90, 150/95 - four real measurements, so the mean is
    135/87.5. The mean of the two hourly means would be 130/85.
    """
    await async_import_history(hass, AggregateHistory(
        hourly=bp(5, 120, 80, 1) + bp(4, 140, 90, 3, sys_lo=130, sys_hi=150)
    ))
    result = await trend(hass, 7)

    assert result is not None
    assert result.systolic == pytest.approx(135.0)
    assert result.diastolic == pytest.approx(87.5)
    assert result.measurements == 4


async def test_the_mean_of_hourly_means_is_explicitly_not_returned(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(
        hourly=bp(5, 120, 80, 1) + bp(4, 140, 90, 3, sys_lo=130, sys_hi=150)
    ))
    result = await trend(hass, 7)

    assert result.systolic != pytest.approx(130.0), "hours were weighted equally"
    assert result.diastolic != pytest.approx(85.0)


async def test_irregular_days_are_not_weighted_by_day(
    recorder_mock, hass: HomeAssistant
):
    """One measurement on one day, three on another, in different hours.

    Day-weighting would give 130/85; every measurement counting once gives
    135/87.5. Pascal measures irregularly, so this is the normal case.
    """
    await async_import_history(hass, AggregateHistory(
        hourly=(
            bp(60, 120, 80, 1)                       # a day with one reading
            + bp(30, 130, 85, 1) + bp(28, 140, 90, 1) + bp(26, 150, 95, 1)
        )
    ))
    result = await trend(hass, 7)

    assert result.systolic == pytest.approx(135.0)
    assert result.diastolic == pytest.approx(87.5)
    assert result.measurements == 4


async def test_a_day_without_measurements_is_ignored_entirely(
    recorder_mock, hass: HomeAssistant
):
    """Not zero-filled, not interpolated, not a divisor."""
    await async_import_history(hass, AggregateHistory(
        hourly=bp(120, 120, 80, 1) + bp(2, 140, 90, 1)
    ))
    result = await trend(hass, 7)

    # Two measurements five days apart: the empty days between contribute nothing.
    assert result.systolic == pytest.approx(130.0)
    assert result.measurements == 2


async def test_one_measurement_returns_that_measurement(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(hourly=bp(3, 128, 82, 1)))
    result = await trend(hass, 7)

    assert result.systolic == pytest.approx(128.0)
    assert result.diastolic == pytest.approx(82.0)
    assert result.measurements == 1


async def test_no_measurements_yields_no_trend(recorder_mock, hass: HomeAssistant):
    assert await trend(hass, 7) is None
    assert await trend(hass, 30) is None


# --- Contributor integrity ---------------------------------------------------


async def test_an_hour_without_a_count_is_skipped_not_guessed(
    recorder_mock, hass: HomeAssistant
):
    """History written before counts existed must not be folded in at weight 1.

    A quietly wrong number is worse than a missing one, so the older hour is
    excluded rather than assumed.
    """
    legacy = [
        MetricHourBucket(metric="blood_pressure_systolic", start=hour(10),
                         mean=200.0, minimum=200.0, maximum=200.0, count=None),
        MetricHourBucket(metric="blood_pressure_diastolic", start=hour(10),
                         mean=120.0, minimum=120.0, maximum=120.0, count=None),
    ]
    await async_import_history(hass, AggregateHistory(hourly=legacy + bp(2, 128, 82, 1)))
    result = await trend(hass, 7)

    # Only the counted hour contributes; the 200/120 hour is absent.
    assert result.systolic == pytest.approx(128.0)
    assert result.measurements == 1


async def test_a_systolic_only_hour_is_not_used(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 128, 82, 1)))
    # A later import writes a count and a systolic for an hour with no diastolic.
    await async_import_history(hass, AggregateHistory(hourly=[
        MetricHourBucket(metric="blood_pressure_systolic", start=hour(3),
                         mean=180.0, minimum=180.0, maximum=180.0, count=5),
    ]))
    result = await trend(hass, 7)

    # The half-populated hour contributes nothing to either half.
    assert result.systolic == pytest.approx(128.0)
    assert result.measurements == 1


async def test_systolic_and_diastolic_share_one_denominator(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(
        hourly=bp(5, 120, 80, 1) + bp(4, 140, 90, 3)
    ))
    result = await trend(hass, 7)

    # Both halves are averaged over the same four contributors.
    total = result.measurements
    assert total == 4
    assert result.systolic == pytest.approx((120 * 1 + 140 * 3) / total)
    assert result.diastolic == pytest.approx((80 * 1 + 90 * 3) / total)


# --- Upsert semantics --------------------------------------------------------


async def test_reimporting_an_hour_does_not_accumulate_the_count(
    recorder_mock, hass: HomeAssistant
):
    for _ in range(3):
        await async_import_history(hass, AggregateHistory(hourly=bp(2, 130, 85, 2)))
    rows = await read(hass, "apple_health_sync:blood_pressure_count", {"mean"})
    result = await trend(hass, 7)

    assert len(rows) == 1
    assert rows[0]["mean"] == pytest.approx(2.0), "count must not become 4 or 6"
    assert result.measurements == 2


async def test_a_corrected_count_replaces_the_old_one(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 130, 85, 2)))
    # HealthKit later reveals a third reading in that hour.
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 140, 90, 3)))
    result = await trend(hass, 7)

    assert result.measurements == 3
    assert result.systolic == pytest.approx(140.0)


async def test_a_corrected_mean_updates_the_derived_average(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 120, 80, 1)))
    assert (await trend(hass, 7)).systolic == pytest.approx(120.0)

    await async_import_history(hass, AggregateHistory(hourly=bp(2, 135, 88, 1)))
    assert (await trend(hass, 7)).systolic == pytest.approx(135.0)


# --- Window boundaries -------------------------------------------------------


async def test_the_seven_day_boundary_is_rolling_and_hour_aligned(
    recorder_mock, hass: HomeAssistant
):
    inside = 7 * 24 - 1
    outside = 7 * 24 + 2
    await async_import_history(hass, AggregateHistory(
        hourly=bp(inside, 120, 80, 1) + bp(outside, 200, 120, 1)
    ))
    result = await trend(hass, 7)

    assert result.measurements == 1
    assert result.systolic == pytest.approx(120.0), "the older hour must be excluded"


async def test_the_thirty_day_window_reaches_further_than_the_seven_day_one(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(hass, AggregateHistory(
        hourly=bp(2, 120, 80, 1) + bp(20 * 24, 140, 90, 1)
    ))

    seven = await trend(hass, 7)
    thirty = await trend(hass, 30)

    assert seven.measurements == 1
    assert seven.systolic == pytest.approx(120.0)
    assert thirty.measurements == 2
    assert thirty.systolic == pytest.approx(130.0)


async def test_beyond_thirty_days_is_excluded(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(
        hourly=bp(2, 120, 80, 1) + bp(31 * 24, 200, 120, 1)
    ))
    result = await trend(hass, 30)

    assert result.measurements == 1
    assert result.systolic == pytest.approx(120.0)


# --- Precision and the freshly imported window -------------------------------


async def test_durable_precision_is_not_rounded(recorder_mock, hass: HomeAssistant):
    await async_import_history(hass, AggregateHistory(hourly=bp(2, 127.842105, 71.631578, 1)))
    rows = await read(hass, "apple_health_sync:blood_pressure_systolic", {"mean"})
    result = await trend(hass, 7)

    assert rows[0]["mean"] == pytest.approx(127.842105)
    # The computation keeps full precision; only the entity rounds for display.
    assert result.systolic == pytest.approx(127.842105)


async def test_the_freshly_imported_window_is_visible_immediately(
    recorder_mock, hass: HomeAssistant
):
    """Statistics writes are queued, so the newest hour would otherwise be
    invisible until a later sync."""
    history = AggregateHistory(hourly=bp(0, 128, 82, 2))
    result = await async_blood_pressure_trend(hass, days=7, now=NOW, overlay=history)

    assert result is not None
    assert result.systolic == pytest.approx(128.0)
    assert result.measurements == 2


async def test_the_overlay_does_not_double_count_a_stored_hour(
    recorder_mock, hass: HomeAssistant
):
    history = AggregateHistory(hourly=bp(2, 130, 85, 2))
    await async_import_history(hass, history)
    result = await trend(hass, 7, overlay=history)

    # The same hour arriving from both sources is one hour, not two.
    assert result.measurements == 2
    assert result.systolic == pytest.approx(130.0)

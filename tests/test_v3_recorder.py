"""Recorder-backed statistics tests.

These use a real Recorder with an in-memory SQLite database (the `recorder_mock`
fixture). Unlike the mocked tests, they prove the actual write -> read ->
correction cycle through Home Assistant's own statistics APIs, including the
upsert behaviour and the running-sum semantics.

The live Home Assistant database is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.apple_health_sync.const import STAT_ID_HEART_RATE, STAT_ID_STEPS
from custom_components.apple_health_sync.payload import (
    AggregateHistory,
    HeartRateHourBucket,
    StepsDayBucket,
)
from custom_components.apple_health_sync.statistics import (
    BASELINE_LOOKBACK,
    _baseline_sum,
    async_import_history,
    day_start_utc,
)

# Well in the past so nothing collides with "now".
BASE_HOUR = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
BASE_DAY = datetime(2026, 6, 10, tzinfo=UTC).date()


def hourly(offset=0, mean=72.4, lo=58.0, hi=141.0):
    return HeartRateHourBucket(
        start=BASE_HOUR + timedelta(hours=offset), mean=mean, minimum=lo, maximum=hi
    )


def daily(offset=0, total=1000.0, tz="Europe/Berlin"):
    return StepsDayBucket(
        day=BASE_DAY + timedelta(days=offset), time_zone=tz, total=total
    )


async def read_back(hass: HomeAssistant, statistic_id: str, types: set[str]):
    """Read statistics through HA's own API, as a consumer would."""
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        BASE_HOUR - timedelta(days=30),
        BASE_HOUR + timedelta(days=30),
        {statistic_id},
        "hour",
        None,
        types,
    )
    return rows.get(statistic_id, [])


async def read_days(hass: HomeAssistant, types: set[str]):
    """Read the stored step rows.

    period="hour" returns rows as stored. period="day" would re-bucket them into
    Home Assistant's own time zone, which is not the device's, and would merge or
    shift rows written at the device's local midnight.
    """
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        BASE_HOUR - timedelta(days=120),
        BASE_HOUR + timedelta(days=120),
        {STAT_ID_STEPS},
        "hour",
        None,
        types,
    )
    return rows.get(STAT_ID_STEPS, [])


# --- Heart rate: write, read back, upsert -----------------------------------


async def test_heart_rate_round_trip(recorder_mock, hass: HomeAssistant):
    """Written values must come back through HA's statistics API unchanged."""
    await async_import_history(
        hass,
        AggregateHistory(
            heart_rate_hourly=[hourly(0, 72.4, 58.0, 141.0), hourly(1, 65.0, 55.0, 90.0)]
        ),
    )
    rows = await read_back(hass, STAT_ID_HEART_RATE, {"mean", "min", "max"})

    assert len(rows) == 2
    assert rows[0]["start"] == BASE_HOUR.timestamp()
    assert rows[0]["mean"] == pytest.approx(72.4)
    assert rows[0]["min"] == pytest.approx(58.0)
    assert rows[0]["max"] == pytest.approx(141.0)
    assert rows[1]["mean"] == pytest.approx(65.0)


async def test_reimporting_the_same_hour_updates_and_does_not_duplicate(
    recorder_mock, hass: HomeAssistant
):
    """The upsert guarantee, proven against the real database."""
    await async_import_history(
        hass, AggregateHistory(heart_rate_hourly=[hourly(0, 72.4, 58.0, 141.0)])
    )
    first = await read_back(hass, STAT_ID_HEART_RATE, {"mean", "min", "max"})
    assert len(first) == 1

    # Same hour, corrected values (a late Watch sync revised the hour).
    await async_import_history(
        hass, AggregateHistory(heart_rate_hourly=[hourly(0, 80.0, 60.0, 150.0)])
    )
    second = await read_back(hass, STAT_ID_HEART_RATE, {"mean", "min", "max"})

    assert len(second) == 1, "re-import must update in place, not append a second row"
    assert second[0]["start"] == first[0]["start"]
    assert second[0]["mean"] == pytest.approx(80.0)
    assert second[0]["min"] == pytest.approx(60.0)
    assert second[0]["max"] == pytest.approx(150.0)


async def test_overlapping_hour_windows_do_not_accumulate_rows(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass, AggregateHistory(heart_rate_hourly=[hourly(0), hourly(1), hourly(2)])
    )
    await async_import_history(
        hass, AggregateHistory(heart_rate_hourly=[hourly(1), hourly(2), hourly(3)])
    )
    rows = await read_back(hass, STAT_ID_HEART_RATE, {"mean"})

    assert len(rows) == 4  # union, not 6


# --- Steps: running sum, correction, no double counting ---------------------


async def test_steps_running_sum_round_trip(recorder_mock, hass: HomeAssistant):
    await async_import_history(
        hass,
        AggregateHistory(
            steps_daily=[daily(0, 1000.0), daily(1, 2000.0), daily(2, 3000.0)]
        ),
    )
    rows = await read_days(hass, {"state", "sum"})

    assert len(rows) == 3
    assert [r["state"] for r in rows] == [
        pytest.approx(1000.0), pytest.approx(2000.0), pytest.approx(3000.0)
    ]
    assert [r["sum"] for r in rows] == [
        pytest.approx(1000.0), pytest.approx(3000.0), pytest.approx(6000.0)
    ]


async def test_correcting_an_earlier_day_shifts_all_downstream_sums(
    recorder_mock, hass: HomeAssistant
):
    """The correctness property HA's day-over-day differencing depends on."""
    await async_import_history(
        hass,
        AggregateHistory(
            steps_daily=[daily(0, 1000.0), daily(1, 2000.0), daily(2, 3000.0)]
        ),
    )
    before = await read_days(hass, {"state", "sum"})
    assert [r["sum"] for r in before] == [
        pytest.approx(1000.0), pytest.approx(3000.0), pytest.approx(6000.0)
    ]

    # Day 0 revised upward by 500; the overlapping window is re-sent.
    await async_import_history(
        hass,
        AggregateHistory(
            steps_daily=[daily(0, 1500.0), daily(1, 2000.0), daily(2, 3000.0)]
        ),
    )
    after = await read_days(hass, {"state", "sum"})

    assert len(after) == 3, "correction must update rows, not duplicate them"
    assert [r["state"] for r in after] == [
        pytest.approx(1500.0), pytest.approx(2000.0), pytest.approx(3000.0)
    ]
    assert [r["sum"] for r in after] == [
        pytest.approx(1500.0), pytest.approx(3500.0), pytest.approx(6500.0)
    ]
    # Every downstream day shifted by exactly the correction.
    for old, new in zip(before, after, strict=True):
        assert new["sum"] - old["sum"] == pytest.approx(500.0)


async def test_overlapping_day_windows_do_not_double_count(
    recorder_mock, hass: HomeAssistant
):
    """The critical property: an overlapping resend must replace, never add."""
    await async_import_history(
        hass, AggregateHistory(steps_daily=[daily(0, 1000.0), daily(1, 2000.0)])
    )
    await async_import_history(  # overlaps day 1, extends to day 2
        hass, AggregateHistory(steps_daily=[daily(1, 2000.0), daily(2, 3000.0)])
    )
    rows = await read_days(hass, {"state", "sum"})

    assert len(rows) == 3
    assert [r["state"] for r in rows] == [
        pytest.approx(1000.0), pytest.approx(2000.0), pytest.approx(3000.0)
    ]
    # Day 1 keeps a single cumulative value across both windows.
    assert [r["sum"] for r in rows] == [
        pytest.approx(1000.0), pytest.approx(3000.0), pytest.approx(6000.0)
    ]


async def test_sums_stay_monotonic_across_many_overlapping_windows(
    recorder_mock, hass: HomeAssistant
):
    for start in range(5):
        await async_import_history(
            hass,
            AggregateHistory(
                steps_daily=[daily(start + i, 1000.0 * (start + i + 1)) for i in range(3)]
            ),
        )
    rows = await read_days(hass, {"sum"})
    sums = [r["sum"] for r in rows]

    assert sums == sorted(sums), "cumulative sums must never decrease"
    assert len(rows) == 7  # days 0..6, no duplicates


# --- _baseline_sum against the real schema ----------------------------------


async def test_baseline_sum_is_zero_on_an_empty_database(
    recorder_mock, hass: HomeAssistant
):
    assert await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(daily(0))) == 0.0


async def test_baseline_sum_reads_the_row_before_the_window(
    recorder_mock, hass: HomeAssistant
):
    await async_import_history(
        hass,
        AggregateHistory(
            steps_daily=[daily(0, 1000.0), daily(1, 2000.0), daily(2, 3000.0)]
        ),
    )
    await async_wait_recording_done(hass)

    # Baseline for a window starting at day 2 is the cumulative sum through day 1.
    assert await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(daily(2))) == pytest.approx(3000.0)
    # ...and for a window starting at day 1, the sum through day 0.
    assert await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(daily(1))) == pytest.approx(1000.0)
    # A window starting at day 0 has nothing before it.
    assert await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(daily(0))) == 0.0


async def test_baseline_sum_ignores_rows_at_or_after_the_window(
    recorder_mock, hass: HomeAssistant
):
    """It must be strictly-before, or a resend would compound its own output."""
    await async_import_history(
        hass, AggregateHistory(steps_daily=[daily(0, 1000.0), daily(1, 2000.0)])
    )
    await async_wait_recording_done(hass)

    baseline = await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(daily(1)))
    assert baseline == pytest.approx(1000.0)  # day 0 only, not day 1's 3000

    # Re-sending the same window must therefore be stable.
    await async_import_history(
        hass, AggregateHistory(steps_daily=[daily(1, 2000.0)])
    )
    rows = await read_days(hass, {"sum"})
    assert [r["sum"] for r in rows] == [pytest.approx(1000.0), pytest.approx(3000.0)]


# --- No entity churn --------------------------------------------------------


async def test_importing_history_creates_no_entity_states(
    recorder_mock, hass: HomeAssistant
):
    """Aggregate history must never surface as entity state changes."""
    before = set(hass.states.async_entity_ids())

    await async_import_history(
        hass,
        AggregateHistory(
            heart_rate_hourly=[hourly(i) for i in range(24)],
            steps_daily=[daily(i, 1000.0 * (i + 1)) for i in range(7)],
        ),
    )
    await async_wait_recording_done(hass)

    assert set(hass.states.async_entity_ids()) == before
    # And the data really did land.
    assert len(await read_back(hass, STAT_ID_HEART_RATE, {"mean"})) == 24


async def test_baseline_survives_an_outage_longer_than_the_near_lookback(
    recorder_mock, hass: HomeAssistant
):
    """A long silence (provisioning expiry, a trip) must not reset the sum.

    The near lookback is deliberately small to keep the common read cheap; this
    proves the fallback scan finds the older row instead of restarting at zero,
    which would make the series non-monotonic and break HA's differencing.
    """
    long_gap_days = BASELINE_LOOKBACK.days + 25

    # Old data, well beyond the near lookback.
    await async_import_history(
        hass, AggregateHistory(steps_daily=[daily(0, 1000.0), daily(1, 2000.0)])
    )
    await async_wait_recording_done(hass)

    # The app returns after a long silence and syncs a fresh window.
    resumed = daily(long_gap_days, 4000.0)
    baseline = await _baseline_sum(hass, STAT_ID_STEPS, day_start_utc(resumed))
    assert baseline == pytest.approx(3000.0), "must find the pre-gap cumulative sum"

    await async_import_history(hass, AggregateHistory(steps_daily=[resumed]))
    rows = await read_days(hass, {"sum"})
    sums = [r["sum"] for r in rows]

    assert sums == sorted(sums), "sums must stay monotonic across the gap"
    assert sums[-1] == pytest.approx(7000.0)

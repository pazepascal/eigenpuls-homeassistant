"""Long-term statistics import: metadata, upserts and the steps running sum."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from homeassistant.components.recorder.models import StatisticMeanType

from custom_components.apple_health_sync.const import (
    DOMAIN,
    STAT_ID_HEART_RATE,
    STAT_ID_STEPS,
)
from custom_components.apple_health_sync.payload import (
    AggregateHistory,
    HeartRateHourBucket,
    StepsDayBucket,
)
from custom_components.apple_health_sync.statistics import (
    async_import_history,
    day_start_utc,
)

HOUR = datetime(2026, 9, 3, 14, 0, 0, tzinfo=UTC)


def hourly(offset=0, mean=72.4, lo=58.0, hi=141.0):
    return HeartRateHourBucket(
        start=HOUR + timedelta(hours=offset), mean=mean, minimum=lo, maximum=hi
    )


def daily(day="2026-09-03", total=8423.0, tz="Europe/Berlin"):
    return StepsDayBucket(
        day=datetime.fromisoformat(day).date(), time_zone=tz, total=total
    )


class Recorded:
    """Captures async_add_external_statistics calls."""

    def __init__(self):
        self.calls: list[tuple[dict, list[dict]]] = []

    def __call__(self, hass, metadata, statistics):
        self.calls.append((metadata, list(statistics)))

    def by_id(self, statistic_id):
        return [c for c in self.calls if c[0]["statistic_id"] == statistic_id]


async def run_import(hass, history, baseline=0.0):
    """Import with the baseline lookup stubbed to a known cumulative value."""
    recorded = Recorded()
    with (
        mock.patch(
            "custom_components.apple_health_sync.statistics"
            ".async_add_external_statistics",
            recorded,
        ),
        mock.patch(
            "custom_components.apple_health_sync.statistics._baseline_sum",
            return_value=baseline,
        ),
    ):
        await async_import_history(hass, history)
    return recorded


# --- Metadata (HA 2026.9) ---------------------------------------------------


async def test_heart_rate_metadata_is_correct(hass):
    recorded = await run_import(hass, AggregateHistory(heart_rate_hourly=[hourly()]))
    metadata, _ = recorded.by_id(STAT_ID_HEART_RATE)[0]

    assert metadata["statistic_id"] == f"{DOMAIN}:heart_rate"
    assert metadata["source"] == DOMAIN  # must equal the id's domain
    assert metadata["mean_type"] is StatisticMeanType.ARITHMETIC
    assert metadata["has_sum"] is False
    assert metadata["unit_of_measurement"] == "bpm"
    assert metadata["unit_class"] is None  # bpm maps to no unit converter
    assert "has_mean" not in metadata  # deprecated, replaced by mean_type


async def test_steps_metadata_is_correct(hass):
    recorded = await run_import(hass, AggregateHistory(steps_daily=[daily()]))
    metadata, _ = recorded.by_id(STAT_ID_STEPS)[0]

    assert metadata["statistic_id"] == f"{DOMAIN}:steps_daily"
    assert metadata["source"] == DOMAIN
    assert metadata["mean_type"] is StatisticMeanType.NONE
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "steps"
    assert metadata["unit_class"] is None


# --- Heart rate rows --------------------------------------------------------


async def test_heart_rate_rows_carry_mean_min_max_and_no_sum(hass):
    recorded = await run_import(hass, AggregateHistory(heart_rate_hourly=[hourly()]))
    _, rows = recorded.by_id(STAT_ID_HEART_RATE)[0]

    assert len(rows) == 1
    assert rows[0]["start"] == HOUR
    assert (rows[0]["mean"], rows[0]["min"], rows[0]["max"]) == (72.4, 58.0, 141.0)
    assert "sum" not in rows[0]


async def test_heart_rate_rows_are_sorted_ascending(hass):
    history = AggregateHistory(heart_rate_hourly=[hourly(2), hourly(0), hourly(1)])
    recorded = await run_import(hass, history)
    _, rows = recorded.by_id(STAT_ID_HEART_RATE)[0]

    assert [r["start"] for r in rows] == [
        HOUR, HOUR + timedelta(hours=1), HOUR + timedelta(hours=2)
    ]


async def test_no_statistics_call_when_nothing_to_import(hass):
    recorded = await run_import(hass, AggregateHistory())
    assert recorded.calls == []


# --- Steps running sum ------------------------------------------------------


async def test_running_sum_accumulates_forward_from_the_baseline(hass):
    history = AggregateHistory(
        steps_daily=[
            daily("2026-09-01", 1000.0),
            daily("2026-09-02", 2000.0),
            daily("2026-09-03", 3000.0),
        ]
    )
    recorded = await run_import(hass, history, baseline=50_000.0)
    _, rows = recorded.by_id(STAT_ID_STEPS)[0]

    assert [r["state"] for r in rows] == [1000.0, 2000.0, 3000.0]
    assert [r["sum"] for r in rows] == [51_000.0, 53_000.0, 56_000.0]


async def test_running_sum_is_monotonic_and_ordered(hass):
    history = AggregateHistory(
        steps_daily=[daily("2026-09-03", 3000.0), daily("2026-09-01", 1000.0)]
    )
    recorded = await run_import(hass, history, baseline=0.0)
    _, rows = recorded.by_id(STAT_ID_STEPS)[0]

    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)
    assert rows[0]["start"] < rows[1]["start"]


async def test_reimporting_the_same_window_reproduces_identical_rows(hass):
    """Idempotency: same input plus same baseline gives byte-identical rows."""
    history = AggregateHistory(
        steps_daily=[daily("2026-09-01", 1000.0), daily("2026-09-02", 2000.0)]
    )
    first = await run_import(hass, history, baseline=500.0)
    second = await run_import(hass, history, baseline=500.0)

    assert first.by_id(STAT_ID_STEPS)[0][1] == second.by_id(STAT_ID_STEPS)[0][1]


async def test_corrected_historical_day_shifts_every_later_sum(hass):
    """The case that would silently corrupt downstream totals if mishandled.

    HA derives per-period values by differencing `sum`, so revising an earlier
    day must move every later day's cumulative sum by the same delta.
    """
    original = AggregateHistory(
        steps_daily=[
            daily("2026-09-01", 1000.0),
            daily("2026-09-02", 2000.0),
            daily("2026-09-03", 3000.0),
        ]
    )
    corrected = AggregateHistory(
        steps_daily=[
            daily("2026-09-01", 1500.0),   # +500 after a late Watch sync
            daily("2026-09-02", 2000.0),
            daily("2026-09-03", 3000.0),
        ]
    )

    before = (await run_import(hass, original, baseline=0.0)).by_id(STAT_ID_STEPS)[0][1]
    after = (await run_import(hass, corrected, baseline=0.0)).by_id(STAT_ID_STEPS)[0][1]

    assert [r["sum"] for r in before] == [1000.0, 3000.0, 6000.0]
    assert [r["sum"] for r in after] == [1500.0, 3500.0, 6500.0]
    # Every later day shifted by exactly the correction, so day-over-day
    # differences (what HA actually displays) stay correct.
    assert [b["sum"] - a["sum"] for a, b in zip(before, after, strict=True)] == [500.0] * 3
    assert [r["state"] for r in after] == [1500.0, 2000.0, 3000.0]


async def test_days_are_not_double_counted_across_overlapping_windows(hass):
    """Overlap must replace, never add. The baseline is what guarantees it."""
    window_one = AggregateHistory(
        steps_daily=[daily("2026-09-01", 1000.0), daily("2026-09-02", 2000.0)]
    )
    window_two = AggregateHistory(  # overlaps 09-02, adds 09-03
        steps_daily=[daily("2026-09-02", 2000.0), daily("2026-09-03", 3000.0)]
    )

    first = (await run_import(hass, window_one, baseline=0.0)).by_id(STAT_ID_STEPS)[0][1]
    # Baseline for window two is the cumulative sum BEFORE 09-02, i.e. 09-01's.
    second = (await run_import(hass, window_two, baseline=1000.0)).by_id(STAT_ID_STEPS)[0][1]

    assert [r["sum"] for r in first] == [1000.0, 3000.0]
    assert [r["sum"] for r in second] == [3000.0, 6000.0]
    # 09-02 lands on the same cumulative value from both windows.
    assert first[1]["sum"] == second[0]["sum"] == 3000.0


# --- Local day to UTC -------------------------------------------------------


def test_day_start_is_local_midnight_in_utc():
    """Berlin is UTC+2 in September, so local midnight is 22:00 UTC the day before."""
    start = day_start_utc(daily("2026-09-03", tz="Europe/Berlin"))
    assert start == datetime(2026, 9, 2, 22, 0, tzinfo=UTC)


def test_day_start_handles_a_dst_transition():
    """Berlin leaves DST on 2026-10-25: the offset changes from +02:00 to +01:00."""
    before = day_start_utc(daily("2026-10-25", tz="Europe/Berlin"))
    after = day_start_utc(daily("2026-10-26", tz="Europe/Berlin"))

    assert before == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # still +02:00
    assert after == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)   # now +01:00
    # A local day spanning the transition is 25 hours, not 24.
    assert after - before == timedelta(hours=25)


def test_day_start_is_hour_aligned():
    """Long-term statistics rows must start on an hour boundary."""
    for day in ("2026-01-15", "2026-06-15", "2026-10-25", "2026-12-31"):
        start = day_start_utc(daily(day, tz="Europe/Berlin"))
        assert (start.minute, start.second, start.microsecond) == (0, 0, 0)

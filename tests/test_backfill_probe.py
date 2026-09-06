"""Phase 5 probes: what the recorder actually does with historical statistics.

HIGH-RIGOR means nothing here is assumed. Every claim the backfill design rests
on is measured against a real Recorder with an in-memory database, using Home
Assistant's own statistics APIs. The live database is never touched and no
personal data is involved - every number below is synthetic.

These are probes rather than regression tests: they exist to establish
behaviour, and what they establish is written into the design.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.apple_health_sync.payload import (
    AggregateHistory,
    MetricDayBucket,
)
from custom_components.apple_health_sync.registry import METRICS
from custom_components.apple_health_sync.statistics import async_import_history

TZ = "Europe/Berlin"
TODAY = datetime(2026, 6, 20, tzinfo=UTC).date()
STEPS = METRICS["steps"].statistic_id


def day(offset: int, total: float) -> MetricDayBucket:
    return MetricDayBucket(
        metric="steps", day=TODAY + timedelta(days=offset), time_zone=TZ, total=total
    )


async def rows(hass: HomeAssistant) -> list[dict]:
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 12, 31, tzinfo=UTC),
        {STEPS},
        "hour",
        None,
        {"state", "sum"},
    )
    return sorted(found.get(STEPS, []), key=lambda r: r["start"])


async def send(hass: HomeAssistant, buckets: list[MetricDayBucket]) -> None:
    await async_import_history(hass, AggregateHistory(daily=buckets))


# --- 1. Can the recorder take points older than what it already holds? -------


async def test_probe_older_points_are_accepted_after_newer_ones(
    recorder_mock, hass: HomeAssistant
):
    """The precondition for backfill existing at all."""
    await send(hass, [day(-1, 1000), day(0, 2000)])
    assert len(await rows(hass)) == 2

    await send(hass, [day(-11, 500), day(-10, 600)])
    after = await rows(hass)

    assert len(after) == 4, "older rows were accepted and did not replace the newer ones"
    assert [r["state"] for r in after] == [500, 600, 1000, 2000]


# --- 2. The running sum, which is where backfill can corrupt a series --------


async def test_probe_a_backfilled_batch_leaves_the_later_sums_stale(
    recorder_mock, hass: HomeAssistant
):
    """**The central Phase 5 finding.**

    `_import_cumulative` builds the running sum forward from the last row
    strictly before its own earliest day, and rewrites only the days in its own
    batch. For the normal sync that is exactly right: its window always ends
    today, so no row ever follows it.

    A backfill batch does have rows after it, and this measures what happens to
    them: nothing. They keep the sums they were given when they were the start
    of the series, so the series stops being monotonic and Home Assistant's
    differencing sees a reset in the middle of the history.
    """
    # The normal window first, as it would already exist on a real instance.
    await send(hass, [day(-1, 1000), day(0, 2000)])
    before = await rows(hass)
    assert [r["sum"] for r in before] == [1000, 3000]

    # Now a backfill batch covering older days.
    await send(hass, [day(-11, 500), day(-10, 600)])
    after = await rows(hass)

    sums = [r["sum"] for r in after]
    assert sums == [500, 1100, 1000, 3000], "measured, not desired"

    monotonic = all(a <= b for a, b in pairwise(sums))
    assert not monotonic, (
        "the series is non-monotonic after a backfill batch: the third row's sum "
        "is lower than the second's, which Home Assistant reads as a meter reset"
    )


async def test_probe_a_forward_pass_over_the_whole_range_is_consistent(
    recorder_mock, hass: HomeAssistant
):
    """And the shape that fixes it, measured rather than argued.

    If every batch is sent oldest-first and each one starts where the previous
    ended, `_baseline_sum` chains them correctly with no receiver change at all:
    each batch's baseline is the previous batch's final sum.
    """
    await send(hass, [day(-11, 500), day(-10, 600)])
    await send(hass, [day(-9, 700), day(-8, 800)])
    await send(hass, [day(-1, 1000), day(0, 2000)])

    sums = [r["sum"] for r in await rows(hass)]
    assert sums == [500, 1100, 1800, 2600, 3600, 5600]
    assert all(a <= b for a, b in pairwise(sums)), "monotonic"


# --- 3. Idempotency ----------------------------------------------------------


async def test_probe_resending_the_same_historical_day_is_idempotent(
    recorder_mock, hass: HomeAssistant
):
    """A resumed backfill will re-send its last batch. That must cost nothing."""
    await send(hass, [day(-11, 500), day(-10, 600)])
    first = await rows(hass)

    await send(hass, [day(-11, 500), day(-10, 600)])
    second = await rows(hass)

    assert len(second) == len(first) == 2, "no duplicate rows"
    assert [r["sum"] for r in second] == [r["sum"] for r in first], (
        "and the running sum did not compound"
    )


async def test_probe_a_corrected_historical_day_updates_in_place(
    recorder_mock, hass: HomeAssistant
):
    await send(hass, [day(-11, 500), day(-10, 600)])
    await send(hass, [day(-11, 900), day(-10, 600)])
    after = await rows(hass)

    assert len(after) == 2
    assert [r["state"] for r in after] == [900, 600]
    assert [r["sum"] for r in after] == [900, 1500], "the later day was re-summed"


# --- 4. Ordering and overlap -------------------------------------------------


async def test_probe_a_shuffled_batch_is_sorted_before_it_is_written(
    recorder_mock, hass: HomeAssistant
):
    await send(hass, [day(-8, 800), day(-11, 500), day(-10, 600), day(-9, 700)])
    result = await rows(hass)
    assert [r["state"] for r in result] == [500, 600, 700, 800]
    assert [r["sum"] for r in result] == [500, 1100, 1800, 2600]


async def test_probe_backfill_and_the_normal_window_may_overlap_one_day(
    recorder_mock, hass: HomeAssistant
):
    """The day where the two meet is written twice, and must survive it."""
    await send(hass, [day(-11, 500), day(-10, 600), day(-9, 700)])
    await send(hass, [day(-9, 700), day(-8, 800)])
    result = await rows(hass)

    assert len(result) == 4, "the shared day is one row, not two"
    assert [r["state"] for r in result] == [500, 600, 700, 800]
    assert [r["sum"] for r in result] == [500, 1100, 1800, 2600]


# --- 5. Discrete metrics, which carry no running sum -------------------------


async def test_probe_a_discrete_series_has_no_ordering_problem_at_all(
    recorder_mock, hass: HomeAssistant
):
    """Mean-only metrics store each row independently, so backfill is trivial."""
    resting = METRICS["resting_heart_rate"]
    assert not resting.has_sum

    await async_import_history(hass, AggregateHistory(daily=[
        MetricDayBucket(metric="resting_heart_rate", day=TODAY, time_zone=TZ, mean=55.0)
    ]))
    await async_import_history(hass, AggregateHistory(daily=[
        MetricDayBucket(
            metric="resting_heart_rate", day=TODAY - timedelta(days=30),
            time_zone=TZ, mean=58.0,
        )
    ]))

    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
        {resting.statistic_id}, "hour", None, {"mean"},
    )
    result = sorted(found.get(resting.statistic_id, []), key=lambda r: r["start"])
    assert len(result) == 2
    assert [pytest.approx(r["mean"]) for r in result] == [58.0, 55.0]


# --- 6. What an abandoned backfill leaves behind -----------------------------


async def test_probe_an_abandoned_backfill_leaves_a_gap_not_a_corruption(
    recorder_mock, hass: HomeAssistant
):
    """The property that decides whether backfill needs any receiver change.

    Someone starts a 90-day backfill, it reaches day -30, and they stop. The
    recent rows still carry the sums they were written with, so at that moment
    the series is non-monotonic. Then the ordinary sync runs.

    Its window ends today and begins a fortnight ago, and `_baseline_sum` takes
    the last row strictly before that window - which is now the newest
    *backfilled* row. So the normal sync rebuilds the recent days forward from
    the correct baseline and the series repairs itself. What remains is a gap
    where the backfill stopped, and a gap is honest: it says "not imported",
    not "you walked backwards".
    """
    # The state of a real instance: a fortnight of ordinary syncing.
    await send(hass, [day(-13, 1000), day(-12, 1100), day(0, 2000)])

    # A backfill that gets as far as day -30 and is then abandoned.
    await send(hass, [day(-40, 500), day(-35, 600), day(-30, 700)])
    mid = [r["sum"] for r in await rows(hass)]
    assert mid == [500, 1100, 1800, 1000, 2100, 4100]
    assert not all(a <= b for a, b in pairwise(mid)), (
        "mid-backfill the series really is non-monotonic"
    )

    # The next ordinary sync, rewriting its own window whole.
    await send(hass, [day(-13, 1000), day(-12, 1100), day(0, 2000)])
    healed = [r["sum"] for r in await rows(hass)]

    assert healed == [500, 1100, 1800, 2800, 3900, 5900]
    assert all(a <= b for a, b in pairwise(healed)), (
        "the ordinary sync repaired the tail without knowing a backfill happened"
    )

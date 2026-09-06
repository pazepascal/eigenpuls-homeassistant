"""The cumulative sum breaks on every batch boundary.

Found in production, not by a test. `async_add_external_statistics` validates
synchronously, puts a task on the recorder's queue and returns; the next
delivery computes its running sum from `_baseline_sum`, which *reads* the
database. Back-to-back deliveries - which is precisely what a backfill is -
therefore read a baseline that predates the delivery before them, the sum falls,
and Home Assistant renders a falling sum as a negative day.

The signature in production was a stale baseline rather than a wrong one: the
lag grew across successive boundaries. Arithmetic is wrong by a constant; a race
is wrong by however far behind the writer happens to be.

Why the earlier recorder probes could not see it, and why this file needs the
`slow_recorder_writes` fixture: the window is only as wide as the time the
recorder thread spends inside one write. Left to itself that is a fraction of a
millisecond, so the bug reproduced in roughly one run in four - too rare to be a
gate, and rare enough that a probe which waits between steps never meets it at
all. The fixture widens the window to something a test can rely on. It does not
create the defect; it makes an existing one observable every time.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder import statistics as recorder_statistics
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
DAY_ONE = datetime(2026, 6, 9, tzinfo=UTC).date()
STEPS = METRICS["steps"].statistic_id

#: Long enough that the recorder thread is unambiguously still inside the write
#: when the next delivery starts, short enough to keep the suite quick.
WRITE_DURATION = 0.05


@pytest.fixture
def slow_recorder_writes(monkeypatch):
    """Hold the recorder thread inside each statistics write.

    The recorder pops a task off its queue and then executes it. For that whole
    stretch the queue is empty while the write has not landed - the window this
    bug lives in. Sleeping inside the write widens it from microseconds to
    something deterministic, without changing what the write does.
    """
    real = recorder_statistics.import_statistics

    def slow(*args, **kwargs):
        time.sleep(WRITE_DURATION)
        return real(*args, **kwargs)

    monkeypatch.setattr(recorder_statistics, "import_statistics", slow)


def batch(offset: int, days: int = 14) -> AggregateHistory:
    """One backfill batch: `days` consecutive days starting at `offset`."""
    return AggregateHistory(daily=[
        MetricDayBucket(
            metric="steps", day=DAY_ONE + timedelta(days=offset + n),
            time_zone=TZ, total=1000.0,
        )
        for n in range(days)
    ])


async def sums(hass: HomeAssistant) -> list[float]:
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
        {STEPS}, "hour", None, {"sum"},
    )
    return [r["sum"] for r in sorted(rows.get(STEPS, []), key=lambda r: r["start"])]


def breaks_in(series: list[float]) -> list[int]:
    """Rows where the cumulative sum falls. With equal days there can be none."""
    return [i for i, (a, b) in enumerate(pairwise(series), start=1) if b < a]


async def test_batches_sent_back_to_back_keep_the_sum_monotonic(
    recorder_mock, hass: HomeAssistant, slow_recorder_writes
):
    """The production case: four batches, no waiting between them.

    Every day carries the same value, so a correct series is a straight ramp and
    any break is unambiguous. A break here is a negative bar on the dashboard.
    """
    for offset in (0, 14, 28, 42):
        await async_import_history(hass, batch(offset))

    series = await sums(hass)
    assert len(series) == 56

    assert not breaks_in(series), (
        f"the cumulative sum falls at rows {breaks_in(series)}; with every day "
        "equal it can only rise. A falling sum is what Home Assistant renders as "
        "a negative day."
    )
    assert series == [1000.0 * n for n in range(1, 57)]


async def test_the_baseline_is_the_previous_batch_and_not_an_older_one(
    recorder_mock, hass: HomeAssistant, slow_recorder_writes
):
    """The same defect stated as the receiver sees it, not as the chart does.

    Worth having separately: a stale baseline is a wrong *number*, and the chart
    only shows it once it makes the sum fall. This checks the number.
    """
    from custom_components.apple_health_sync import statistics as module

    baselines: list[float] = []
    real = module._baseline_sum

    async def record(hass, statistic_id, before):
        value = await real(hass, statistic_id, before)
        baselines.append(value)
        return value

    module._baseline_sum, original = record, module._baseline_sum
    try:
        for offset in (0, 14, 28):
            await async_import_history(hass, batch(offset))
    finally:
        module._baseline_sum = original

    # Each batch adds 14 days at 1000, so each baseline is the running total of
    # everything sent before it.
    assert baselines == [0.0, 14000.0, 28000.0]


async def test_waiting_between_batches_was_always_fine(
    recorder_mock, hass: HomeAssistant, slow_recorder_writes
):
    """Why no earlier probe caught this.

    Waiting for the recorder between deliveries removes the race, so a probe
    written that way passes whether the fix is present or not. Kept as the
    control: it must stay green, and it proves nothing on its own.
    """
    for offset in (0, 14, 28, 42):
        await async_import_history(hass, batch(offset))
        await async_wait_recording_done(hass)

    assert not breaks_in(await sums(hass))


async def test_a_second_pass_heals_a_chain_that_is_already_broken(
    recorder_mock, hass: HomeAssistant, slow_recorder_writes
):
    """What repairing the existing production history would actually take.

    The fix above stops new breaks. It does nothing for the ones already
    written, and those do not heal on their own: the rolling window rewrites
    only its own days, and it rewrites them onto whatever baseline it finds, so
    a shift that happened in June is carried forward for ever.

    This measures the repair rather than assuming it. A broken chain is built
    the way production's was - a downward shift starting at a batch boundary,
    with the daily values themselves left intact, because the daily values were
    never wrong. Then the same batches are sent again over the same days.

    They are not added to the old rows, they replace them, and each batch reads
    a baseline that the batch before it has already corrected. So the correction
    propagates forward and the chain comes out whole. No deletion, no writing to
    the database by hand.
    """
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    from custom_components.apple_health_sync.registry import METRICS
    from custom_components.apple_health_sync.statistics import (
        day_start_utc,
        metadata_for,
    )

    for offset in (0, 14, 28, 42):
        await async_import_history(hass, batch(offset))
    assert not breaks_in(await sums(hass))

    # Reproduce the damage: from day 28 on, every sum is short by one batch.
    # `state` keeps the day's own value - that half was always correct.
    spec = METRICS["steps"]
    shifted = [
        {
            "start": day_start_utc(bucket),
            "state": 1000.0,
            "sum": 1000.0 * (28 + n + 1) - 14000.0,
        }
        for n, bucket in enumerate(batch(28, days=28).daily)
    ]
    async_add_external_statistics(hass, metadata_for(spec), shifted)
    await async_wait_recording_done(hass)

    damaged = await sums(hass)
    assert breaks_in(damaged) == [28], "the damage is the one production had"

    for offset in (0, 14, 28, 42):
        await async_import_history(hass, batch(offset))

    healed = await sums(hass)
    assert not breaks_in(healed)
    assert healed == [1000.0 * n for n in range(1, 57)]
    assert len(healed) == len(damaged), "repaired in place, no rows added"


async def test_a_repair_pass_that_starts_after_the_break_does_not_heal_it(
    recorder_mock, hass: HomeAssistant, slow_recorder_writes
):
    """The one condition the repair has, made executable rather than advisory.

    Each batch builds on the baseline it reads, so a repair inherits whatever
    stands before its first day. Start it after the break and it faithfully
    rebuilds the chain on top of the shift: every later day is still short, and
    the break itself is untouched.

    In operational terms: the range has to reach back to at least the first
    break, not merely to somewhere in the damaged stretch.
    """
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    from custom_components.apple_health_sync.registry import METRICS
    from custom_components.apple_health_sync.statistics import (
        day_start_utc,
        metadata_for,
    )

    for offset in (0, 14, 28, 42):
        await async_import_history(hass, batch(offset))

    async_add_external_statistics(hass, metadata_for(METRICS["steps"]), [
        {
            "start": day_start_utc(bucket),
            "state": 1000.0,
            "sum": 1000.0 * (28 + n + 1) - 14000.0,
        }
        for n, bucket in enumerate(batch(28, days=28).daily)
    ])
    await async_wait_recording_done(hass)
    assert breaks_in(await sums(hass)) == [28]

    # A repair that begins inside the damage instead of before it.
    for offset in (42,):
        await async_import_history(hass, batch(offset))

    assert breaks_in(await sums(hass)) == [28], (
        "the break is still there, and it will stay there until a pass covers it"
    )

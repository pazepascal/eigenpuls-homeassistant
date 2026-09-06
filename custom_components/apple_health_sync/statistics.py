"""Long-term statistics import for the rolling aggregate window.

Home Assistant long-term statistics are the right home for this data and not
ordinary entity history: they are keyed on hour-aligned UTC starts, written at
the *measurement* time rather than the delivery time, are not purged by the
recorder's ``keep_days`` (only short-term statistics are), and produce no entity
state churn. Entity history has none of those properties and cannot be backfilled.

Every series is driven by ``registry.py``: which statistic id, which unit, which
unit class, whether it is a mean or a running sum. Adding a metric therefore adds
no code here.

Durability caveat, stated plainly: ``async_add_external_statistics`` validates
synchronously and then queues the write. It cannot report whether the row was
committed. This module therefore promises "validated and accepted", never
"durably persisted" - a recorder-internal failure afterwards is healed by the
next overlapping 7-14 day window.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.core import HomeAssistant

from . import registry
from .payload import AggregateHistory, MetricDayBucket, NightlySleep
from .registry import (
    BLOOD_PRESSURE_COUNT,
    BLOOD_PRESSURE_METRICS,
    SLEEP_OFFSET_ANCHOR_HOUR,
    SLEEP_SERIES,
    BucketKind,
    MeanType,
    MetricSpec,
)

_LOGGER = logging.getLogger(__name__)

# The usual distance back to the row preceding the window. The widest window is
# 14 days, so 35 covers it with margin and keeps the common read small.
BASELINE_LOOKBACK = timedelta(days=35)

# Floor for the fallback scan, matching the wire format's timestamp floor. Only
# reached when the app has been silent for longer than BASELINE_LOOKBACK.
BASELINE_FLOOR = datetime(2000, 1, 1, tzinfo=UTC)

#: Our registry's mean type -> Home Assistant's. Asserted in the tests so the two
#: enumerations cannot drift apart silently.
_MEAN_TYPES: dict[MeanType, StatisticMeanType] = {
    MeanType.NONE: StatisticMeanType.NONE,
    MeanType.ARITHMETIC: StatisticMeanType.ARITHMETIC,
}


def metadata_for(spec: MetricSpec) -> StatisticMetaData:
    """Statistic metadata for one registry entry."""
    return StatisticMetaData(
        # has_mean is deprecated in favour of mean_type and is removed in a later
        # release; omitting mean_type breaks in 2026.11.
        mean_type=_MEAN_TYPES[spec.mean_type],
        has_sum=spec.has_sum,
        name=spec.name,
        # `source` must equal the statistic_id's domain or the import is refused.
        source=registry.DOMAIN,
        statistic_id=spec.statistic_id,
        unit_class=spec.unit_class,
        unit_of_measurement=spec.unit,
    )


def day_start_utc(bucket: MetricDayBucket | NightlySleep) -> datetime:
    """The UTC instant of local midnight for a local calendar day.

    Long-term statistics rows are hour-aligned, so this assumes a whole-hour UTC
    offset. Zones with a 30/45-minute offset would not align; out of scope.
    """
    zone = ZoneInfo(bucket.time_zone)
    local_midnight = datetime(
        bucket.day.year, bucket.day.month, bucket.day.day, tzinfo=zone
    )
    return local_midnight.astimezone(UTC)


def sleep_offset_minutes(instant: datetime, night: NightlySleep) -> float:
    """Minutes from the anchor evening to ``instant``.

    Bedtimes and wake times are stored as offsets rather than clock times because
    averaging clock times across midnight is wrong: 23:30 and 00:30 average to
    12:00, not to 00:00. Anchoring at 18:00 on the evening before the wake date
    means no plausible bedtime wraps, so a plain arithmetic mean is correct.

    Computed here rather than sent by the client so that the offset and the
    absolute instant can never disagree.
    """
    zone = ZoneInfo(night.time_zone)
    evening = night.day - timedelta(days=1)
    anchor = datetime(
        evening.year, evening.month, evening.day,
        SLEEP_OFFSET_ANCHOR_HOUR, tzinfo=zone,
    )
    return (instant - anchor.astimezone(UTC)).total_seconds() / 60


async def _flush_pending_writes(hass: HomeAssistant) -> None:
    """Wait until everything already handed to the recorder is in the database.

    `async_add_external_statistics` validates synchronously, puts a task on the
    recorder's queue and returns. Every read in this module therefore has to wait
    for that task, or it answers from the state before the last write.

    That is not theoretical. A production backfill sends its batches back to
    back, and each one computes its running sum from a baseline it reads out of
    the database. The baselines came back stale at successive boundaries, the
    cumulative sum fell each time, and Home Assistant renders a falling sum as a
    negative day - which is what was seen on the dashboard.

    The queue is FIFO and the recorder thread runs one task to completion at a
    time, so a task queued now is guaranteed to run after the writes queued
    before it. Queuing `SynchronizeTask` and awaiting its future is therefore an
    exact wait, and `commit_before` makes it a wait for the *commit*.

    `Recorder.async_block_till_done()` is the obvious call here and it does not
    work: it returns without waiting when the queue is empty, and the queue is
    empty for the whole time the recorder thread spends executing the task it
    has already taken off it. That is the window this bug lived in - measured at
    8 failures in 20 runs of `test_backfill_baseline_race.py` with that call in
    place, and 0 in 40 with this one.
    """
    future: asyncio.Future[None] = hass.loop.create_future()
    get_instance(hass).queue_task(SynchronizeTask(future))
    await future


async def _read_sums(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[dict]:
    """Raw stored rows for a cumulative series in a range.

    ``period="hour"`` returns rows exactly as stored. ``period="day"`` must NOT be
    used here: it re-buckets into *Home Assistant's* configured time zone, so rows
    written at the device's local midnight can land in an adjacent bucket and the
    baseline then picks up the wrong value.
    """
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return rows.get(statistic_id) or []


async def _baseline_sum(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float:
    """Cumulative sum of the last row STRICTLY before ``before``.

    "Strictly before" is the whole contract: including the window's own first day
    would compound the running sum every time an overlapping window is re-sent.
    The boundary is filtered explicitly rather than relying on whether the query
    treats ``end_time`` as inclusive.

    The window always ends today and is always rewritten whole, so the row
    preceding it is never one this integration rewrites - that is what keeps the
    baseline stable across overlapping uploads.
    """
    # Before reading anything: this function exists to answer "what did the
    # previous batch end on", and the previous batch may still be in flight.
    await _flush_pending_writes(hass)

    cutoff = before.timestamp()

    def last_sum_before(rows: list[dict]) -> float | None:
        earlier = [row for row in rows if float(row["start"]) < cutoff]
        if not earlier:
            return None
        return float(earlier[-1].get("sum") or 0.0)

    # Common case: the preceding row is days, not years, away.
    near = await _read_sums(hass, statistic_id, before - BASELINE_LOOKBACK, before)
    if (found := last_sum_before(near)) is not None:
        return found

    # Rare case: silent for longer than the near window (provisioning expiry, a
    # long trip). Scan from the floor rather than resetting the sum to zero,
    # which would make the series non-monotonic and break HA's differencing.
    far = await _read_sums(hass, statistic_id, BASELINE_FLOOR, before)
    if (found := last_sum_before(far)) is not None:
        return found

    # Genuinely no earlier data: this is the first import.
    return 0.0


async def _import_cumulative(
    hass: HomeAssistant, spec: MetricSpec, buckets: list[MetricDayBucket]
) -> None:
    # Ascending order matters: the running sum is built forward from the
    # baseline, and a corrected earlier day must shift every later day.
    ordered = sorted(buckets, key=lambda b: b.day)
    running = await _baseline_sum(hass, spec.statistic_id, day_start_utc(ordered[0]))

    rows: list[StatisticData] = []
    for bucket in ordered:
        running += bucket.total or 0.0
        rows.append(
            StatisticData(start=day_start_utc(bucket), state=bucket.total, sum=running)
        )
    async_add_external_statistics(hass, metadata_for(spec), rows)


def _import_discrete(
    hass: HomeAssistant, spec: MetricSpec, history: AggregateHistory
) -> None:
    if spec.kind is BucketKind.HOURLY_DISCRETE:
        rows = [
            StatisticData(
                start=b.start, mean=b.mean, min=b.minimum, max=b.maximum
            )
            for b in sorted(
                (x for x in history.hourly if x.metric == spec.metric),
                key=lambda b: b.start,
            )
        ]
    else:
        rows = []
        for bucket in sorted(
            (x for x in history.daily if x.metric == spec.metric),
            key=lambda b: b.day,
        ):
            row = StatisticData(start=day_start_utc(bucket), mean=bucket.mean)
            # Resting heart rate is mean-only: Apple derives one value per day,
            # so writing a min and max would invent a spread that was never
            # measured.
            if bucket.minimum is not None:
                row["min"] = bucket.minimum
            if bucket.maximum is not None:
                row["max"] = bucket.maximum
            rows.append(row)
    if rows:
        async_add_external_statistics(hass, metadata_for(spec), rows)


def _import_sleep(hass: HomeAssistant, nights: list[NightlySleep]) -> None:
    """Fan one nightly summary out into its durable series.

    A field that is ``None`` writes no row at all, which is what keeps "not
    measured" distinguishable from "measured as zero" in storage rather than only
    on the wire. A night with a total and no staging therefore contributes to the
    total series and to nothing else.
    """
    ordered = sorted(nights, key=lambda n: n.day)
    for field_name, spec in SLEEP_SERIES.items():
        rows: list[StatisticData] = []
        for night in ordered:
            if field_name == "sleep_start_offset_min":
                value = sleep_offset_minutes(night.sleep_start, night)
            elif field_name == "wake_offset_min":
                value = sleep_offset_minutes(night.wake_time, night)
            else:
                value = getattr(night, field_name)
            if value is None:
                continue
            rows.append(StatisticData(start=day_start_utc(night), mean=float(value)))
        if rows:
            async_add_external_statistics(hass, metadata_for(spec), rows)


@dataclass(frozen=True, slots=True)
class BloodPressureTrend:
    """A measurement-weighted blood-pressure average over a rolling window."""

    systolic: float
    diastolic: float
    #: Real complete measurements behind the figures, not hours and not days.
    measurements: int
    period_days: int


def _import_blood_pressure_counts(
    hass: HomeAssistant, history: AggregateHistory
) -> None:
    """Store how many real measurements stand behind each blood-pressure hour.

    Home Assistant's arithmetic rollup is an unweighted mean of hourly means, so
    without this an hour holding three readings would count for exactly as much
    as an hour holding one. The parser has already checked that both halves
    agree on the hours and the counts, so the systolic side alone is authoritative
    here.

    Written as ordinary statistics rows, so a re-imported hour replaces its count
    rather than accumulating: two readings stay two however often the overlapping
    window re-sends them, and become three only if HealthKit says so.
    """
    systolic, _ = BLOOD_PRESSURE_METRICS
    rows = [
        StatisticData(start=bucket.start, mean=float(bucket.count))
        for bucket in sorted(history.hourly, key=lambda b: b.start)
        if bucket.metric == systolic and bucket.count is not None
    ]
    if rows:
        async_add_external_statistics(hass, metadata_for(BLOOD_PRESSURE_COUNT), rows)


async def _read_means(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Stored hourly means for one series, keyed by row start.

    ``period="hour"`` returns rows exactly as stored. Any coarser period would
    make Home Assistant reduce them with its own unweighted mean, which is the
    very thing this module exists to avoid.
    """
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass, start, end, {statistic_id}, "hour", None, {"mean"},
    )
    found: dict[datetime, float] = {}
    for row in rows.get(statistic_id) or []:
        if (mean := row.get("mean")) is not None:
            found[datetime.fromtimestamp(float(row["start"]), UTC)] = float(mean)
    return found


async def async_blood_pressure_trend(
    hass: HomeAssistant,
    *,
    days: int,
    now: datetime,
    overlay: AggregateHistory | None = None,
) -> BloodPressureTrend | None:
    """The measurement-weighted mean over the last ``days`` days.

    Every real measurement counts once:

        systolic  = sum(hourly_systolic_mean * count) / sum(count)
        diastolic = sum(hourly_diastolic_mean * count) / sum(count)

    with one shared denominator, so the two halves can never be averaged over
    different contributors. Neither hours nor calendar days carry weight of their
    own: an hour with three readings contributes three times an hour with one,
    and a day without measurements contributes nothing at all rather than
    dragging the mean toward a fabricated value.

    The window is rolling and snapped to the hour - ``now`` floored to the hour,
    less ``days`` times 24 hours. Hour-based rather than calendar-based because
    the rows themselves are hourly and because calendar days are 23 or 25 hours
    long twice a year, which would quietly change what the window covers.

    An hour is only used when the systolic mean, the diastolic mean and a
    positive count are all present. History written before counts existed is
    therefore skipped rather than folded in at the wrong weight: a number that is
    quietly wrong is worse than one that is missing, so the trend stays absent
    until correctly weighted data arrives.

    ``overlay`` is the window just imported. Statistics writes are queued rather
    than committed synchronously, so the newest hours would otherwise be invisible
    until a later sync; overlaying them applies the same upsert the database will,
    and cannot double count because the hours are keyed.
    """
    systolic_id, diastolic_id = (
        f"{registry.DOMAIN}:{metric}" for metric in BLOOD_PRESSURE_METRICS
    )
    floor_hour = now.replace(minute=0, second=0, microsecond=0)
    start = floor_hour - timedelta(days=days)

    systolic = await _read_means(hass, systolic_id, start, now)
    diastolic = await _read_means(hass, diastolic_id, start, now)
    counts = await _read_means(hass, BLOOD_PRESSURE_COUNT.statistic_id, start, now)

    if overlay is not None:
        systolic_metric, diastolic_metric = BLOOD_PRESSURE_METRICS
        for bucket in overlay.hourly:
            if not start <= bucket.start <= now:
                continue
            if bucket.metric == systolic_metric:
                systolic[bucket.start] = bucket.mean
                if bucket.count is not None:
                    counts[bucket.start] = float(bucket.count)
            elif bucket.metric == diastolic_metric:
                diastolic[bucket.start] = bucket.mean

    weighted_systolic = 0.0
    weighted_diastolic = 0.0
    total = 0.0
    for hour, count in counts.items():
        # Every part of the measurement must be present, or the hour is not a
        # usable contributor at all.
        if count <= 0 or hour not in systolic or hour not in diastolic:
            continue
        weighted_systolic += systolic[hour] * count
        weighted_diastolic += diastolic[hour] * count
        total += count

    if total <= 0:
        return None
    return BloodPressureTrend(
        systolic=weighted_systolic / total,
        diastolic=weighted_diastolic / total,
        measurements=round(total),
        period_days=days,
    )


async def async_import_history(hass: HomeAssistant, history: AggregateHistory) -> None:
    """Import the aggregate window.

    Raises:
        HomeAssistantError: propagated from Home Assistant's own synchronous
            validation. The caller must then apply nothing else.
    """
    for metric, spec in registry.METRICS.items():
        if spec.kind is BucketKind.DAILY_CUMULATIVE:
            buckets = [b for b in history.daily if b.metric == metric]
            if buckets:
                await _import_cumulative(hass, spec, buckets)
        else:
            _import_discrete(hass, spec, history)

    _import_blood_pressure_counts(hass, history)

    if history.nightly:
        _import_sleep(hass, history.nightly)

    # Counts only - never values.
    _LOGGER.debug(
        "Imported aggregate history: %s hourly, %s daily, %s nightly buckets",
        len(history.hourly),
        len(history.daily),
        len(history.nightly),
    )

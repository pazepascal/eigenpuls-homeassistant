"""Wire format parsing and validation for every supported version.

Deliberately free of Home Assistant imports, together with ``registry.py`` and
``const.py``: the parsing layer is a clean boundary that can be reasoned about
and tested without a Home Assistant instance, and it is what
``tools/local_receiver.py`` exercises when the iOS app is tested against a plain
local HTTP server before Home Assistant is involved.

v4 replaces the fixed per-metric bucket keys of v3 with metric-keyed arrays whose
meaning comes from ``registry.py``. The point of that shape is that a later
metric which fits an existing bucket family becomes a registry entry rather than
protocol v5 - see ``protocol/payload-v4.md`` section 9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import registry
from .registry import (
    BLOOD_PRESSURE_METRICS,
    PERCENT_MAX,
    PERCENT_METRICS,
    PERCENT_MIN,
    WORKOUT_ACTIVITIES,
    BucketKind,
)

# v2 introduces snapshot/completion semantics: historical batches no longer
# drive the current-value entities. An old (v1-only) receiver MUST reject a v2
# payload rather than silently ignoring `sync`/`snapshot` and folding history
# into the current Heart Rate - which would succeed with HTTP 200 while leaving
# the sensor semantically wrong.
# v3 adds compact aggregate history (`buckets`) alongside the v2 snapshot.
# A v3 client MUST be rejected by a v2-only receiver: an additive field would
# have been accepted with HTTP 200 while the trend history was silently dropped,
# which for health data is an unacceptable silent loss of a promised feature.
# v4 makes the aggregate window metric-keyed and adds nightly sleep summaries.
# A v3-only receiver MUST reject a v4 payload: v4's buckets live under new keys,
# so a v3 receiver would find nothing it recognises, answer HTTP 200 and store
# none of it - the client would believe a night's sleep had been durably recorded
# when it had been dropped. That is the same silent-loss argument that produced
# v3, applied consistently rather than re-litigated.
WIRE_VERSION = 4
SUPPORTED_VERSIONS = frozenset({1, 2, 3, 4})

MetricKind = Literal["discrete", "cumulative"]

# The v1 raw-sample registry - protocol/payload-v1.md section 6.
#
# Kept separate from ``registry.METRICS`` on purpose: these units are HealthKit's
# canonical sample units, whereas the registry carries the Home-Assistant-facing
# units the statistics are stored in. Conflating the two would silently relabel
# stored data, so the two layers stay distinct.
LEGACY_SAMPLE_METRICS: dict[str, dict[str, Any]] = {
    "heart_rate": {"kind": "discrete", "unit": "count/min"},
    "steps": {"kind": "cumulative", "unit": "count"},
}

# Aggregate-history limits. 14 days x 24h = 336 hourly buckets is the widest
# adaptive recovery window; 400 leaves headroom without allowing an archive.
# Hourly ceilings. Both are enforced by *rejecting* an oversized request, never
# by trimming: silently dropping buckets to fit would lose health history that
# the client believes it stored.
#
# Per metric, because one metric must never be able to squeeze out another. The
# dense heart-rate series needs 336 buckets for a full 14-day recovery window, so
# 400 leaves it headroom; the sparse Phase 3B metrics emit only the hours that
# actually contain a measurement and use a fraction of it.
MAX_HOURLY_BUCKETS_PER_METRIC = 400
# Total envelope across every hourly metric, guarding only against a
# pathological payload: five hourly metrics at a realistic sparse load is well
# under a thousand.
MAX_HOURLY_BUCKETS = 2_000
MAX_DAILY_BUCKETS = 40

# v4 carries a daily bucket per metric per day rather than steps alone: seven
# daily metrics across the widest 14-day window is 98 buckets, so the v3 ceiling
# of 40 would reject a legitimate payload. Kept well above that, and well below
# anything resembling an archive upload.
MAX_V4_DAILY_BUCKETS = 400
MAX_NIGHTLY_BUCKETS = 40

# A night cannot be longer than this, and total sleep cannot exceed the span
# between falling asleep and waking.
MAX_NIGHT_SPAN = timedelta(hours=24)
# Rounding slack when comparing client-computed minute durations against the
# span they were derived from.
SLEEP_TOLERANCE_MIN = 1.0

# Limits - protocol/payload-v1.md section 8.
#
# MAX_SAMPLES is deliberately well inside MAX_BODY_BYTES rather than level with
# it. A heart-rate sample encodes to roughly 207 bytes, so the previous ceiling
# of 5000 came to ~1,035,000 bytes against a 1,048,576 byte body limit - about
# 1% of headroom, which a slightly longer source name would have erased. 2000
# samples is ~414 KB, leaving the body limit as a real backstop.
#
# This is a tightening, not a raise: Home Assistant itself accepts 16 MiB
# (homeassistant.components.http.MAX_CLIENT_SIZE), and 1 MiB stays our own
# abuse guard well below that.
MAX_BODY_BYTES = 1024 * 1024
MAX_SAMPLES = 2_000
MAX_DAILY_TOTALS = 400
MAX_DELETIONS = 5_000
FUTURE_TOLERANCE = timedelta(minutes=5)
TIMESTAMP_FLOOR = datetime(2000, 1, 1, tzinfo=UTC)


class PayloadError(Exception):
    """Envelope-level rejection. Maps to HTTP 400."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Sample:
    """A discrete reading, keyed on its HealthKit sample UUID."""

    metric: str
    uuid: str
    start: datetime
    end: datetime
    value: float
    unit: str
    source: str | None


@dataclass(slots=True)
class DailyTotal:
    """A cumulative daily total, keyed on metric + local date + time zone."""

    metric: str
    day: date
    time_zone: str
    value: float
    unit: str


@dataclass(slots=True)
class HeartRateSnapshot:
    """The newest heart-rate reading, read directly from HealthKit (v2)."""

    value: float
    unit: str
    measured_at: datetime
    source: str | None


@dataclass(slots=True)
class StepsSnapshot:
    """Today's cumulative step total in the device's local day (v2)."""

    value: float
    unit: str
    day: date
    time_zone: str


@dataclass(slots=True)
class MeasurementSnapshot:
    """The newest reading of a discrete metric (v4)."""

    metric: str
    value: float
    unit: str
    measured_at: datetime
    source: str | None = None


@dataclass(slots=True)
class DailyTotalSnapshot:
    """Today's cumulative total of a metric in the device's local day (v4)."""

    metric: str
    value: float
    unit: str
    day: date
    time_zone: str


@dataclass(slots=True)
class BloodPressureSnapshot:
    """One correlated blood-pressure measurement (v4).

    Carried as a single object rather than two independent measurement entries
    because a lone systolic reading is not a blood-pressure measurement, and
    pairing two separate entries by comparing their timestamps would be an
    inference this receiver has no business making. The client reads the pair
    through HealthKit's own blood-pressure correlation, which is the only source
    of truth that both halves belong to one event; this shape carries that fact
    intact instead of asking Home Assistant to reconstruct it.

    Both values are required. A half pair is rejected rather than completed.
    """

    systolic: float
    diastolic: float
    unit: str
    measured_at: datetime
    source: str | None = None


@dataclass(slots=True)
class LastWorkout:
    """The most recent workout, as a compact summary (v4).

    Deliberately a summary and not an event log. Apple Health remains the
    authoritative record of individual workouts; Home Assistant keeps enough to
    answer what the last session was and, through the daily aggregates, how much
    training has happened lately.

    `duration_min` is HealthKit's own pause-aware duration, not the span between
    start and end - a workout paused for ten minutes is not ten minutes of
    training. The optional fields are genuinely optional: a strength session
    records no distance and an unworn watch records no heart rate, and absent
    must stay absent rather than becoming zero.

    `uuid` is HealthKit's own identity, kept so the newest workout is chosen
    deterministically and a re-sent one is recognised rather than re-counted. It
    is not meant for display.
    """

    uuid: str
    activity: str
    start: datetime
    end: datetime
    duration_min: float
    active_energy_kcal: float | None = None
    distance_km: float | None = None
    avg_heart_rate_bpm: float | None = None
    max_heart_rate_bpm: float | None = None
    source: str | None = None


@dataclass(slots=True)
class SleepTrend:
    """The rolling seven-night trend, computed on the device (v4).

    Derived state, not durable truth: the nightly summaries in long-term
    statistics remain authoritative, and this is recomputed and replaced on every
    sync exactly like a current value. Deliberately not stored as a second
    durable history, which could drift from the nights it was derived from.

    ``nights_by_field`` records how many nights actually contributed to each
    average, so a three-night week is never presented as a seven-night one. A
    stage missing from every night yields ``None`` here, never zero.
    """

    nights: int
    avg_total_min: float | None = None
    avg_rem_min: float | None = None
    avg_core_min: float | None = None
    avg_deep_min: float | None = None
    avg_awake_min: float | None = None
    avg_sleep_start_offset_min: float | None = None
    avg_wake_offset_min: float | None = None
    #: Standard deviation of the bedtime offset - the consistency measure.
    sleep_start_stddev_min: float | None = None
    avg_nap_total_min: float | None = None
    nights_by_field: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class Snapshot:
    """Current values at logical sync completion.

    A member that is ``None`` means "leave that sensor unchanged" - never
    "clear it". HealthKit cannot distinguish a denied read from absent data,
    so absence must not be treated as a measurement.
    """

    heart_rate: HeartRateSnapshot | None = None
    steps_today: StepsSnapshot | None = None
    # v4. Keyed by metric so a new metric needs no new field here either.
    measurements: dict[str, MeasurementSnapshot] = field(default_factory=dict)
    daily_totals: dict[str, DailyTotalSnapshot] = field(default_factory=dict)
    sleep_last_night: NightlySleep | None = None
    sleep_7d: SleepTrend | None = None
    blood_pressure: BloodPressureSnapshot | None = None
    last_workout: LastWorkout | None = None


@dataclass(slots=True)
class MetricHourBucket:
    """One hour of a discrete metric, aggregated by HealthKit.

    ``start`` is hour-aligned UTC because Home Assistant long-term statistics are
    keyed on hour-aligned UTC starts. Aligning to UTC rather than local time also
    makes the series immune to DST: a local-midnight anchor would produce 23- and
    25-hour days across a transition.
    """

    metric: str
    start: datetime
    mean: float
    minimum: float
    maximum: float
    count: int | None = None


@dataclass(slots=True)
class MetricDayBucket:
    """One local calendar day of a metric.

    Local days are deliberately used here (unlike hourly buckets): "steps today"
    means the user's day, and HealthKit already accounts for 23- and 25-hour DST
    days. Cumulative metrics fill ``total``; discrete ones fill ``mean`` and,
    unless the registry says mean-only, ``minimum`` and ``maximum``. Which of the
    two applies is decided by the registry, never guessed from what is present.
    """

    metric: str
    day: date
    time_zone: str
    total: float | None = None
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    count: int | None = None


@dataclass(slots=True)
class NightlySleep:
    """One night's already-aggregated sleep summary (v4).

    The receiver never sees raw sleep segments and never aggregates them: the
    client owns that, because HealthKit offers no statistics query for category
    samples and therefore no cross-source de-duplication either.

    ``day`` is the **wake date** - the local date the main sleep ended. Wake-date
    attribution rather than sleep-start attribution because bedtimes of 23:50 and
    00:10 are the same night to a person but fall on different start dates; the
    wake date puts both on the same day.

    A stage that is ``None`` was not measured and must stay ``None`` all the way
    into storage. A night tracked by iPhone alone has a real total with no
    staging, and coercing that to zero would drag every stage average down.

    Describes **main sleep only**. Naps are ordinary daily metrics
    (``nap_total``, ``nap_count``) rather than fields here, because a nap belongs
    to the calendar day and must be storable on a day with no main sleep at all.
    """

    day: date
    time_zone: str
    total_sleep_min: float
    sleep_start: datetime
    wake_time: datetime
    rem_min: float | None = None
    core_min: float | None = None
    deep_min: float | None = None
    awake_min: float | None = None


class AggregateHistory:
    """Compact rolling-window history. Never drives current-value entities.

    Written out rather than generated as a dataclass so that the v3 spelling of
    the two bucket arrays keeps working as constructor keywords. v3 and v4 then
    share one storage path instead of two that could drift apart.
    """

    __slots__ = ("daily", "hourly", "nightly")

    def __init__(
        self,
        hourly: list[MetricHourBucket] | None = None,
        daily: list[MetricDayBucket] | None = None,
        nightly: list[NightlySleep] | None = None,
        *,
        heart_rate_hourly: list[MetricHourBucket] | None = None,
        steps_daily: list[MetricDayBucket] | None = None,
    ) -> None:
        self.hourly = list(hourly or []) + list(heart_rate_hourly or [])
        self.daily = list(daily or []) + list(steps_daily or [])
        self.nightly = list(nightly or [])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AggregateHistory):
            return NotImplemented
        return (self.hourly, self.daily, self.nightly) == (
            other.hourly, other.daily, other.nightly,
        )

    def __repr__(self) -> str:
        return (
            f"AggregateHistory(hourly={self.hourly!r}, daily={self.daily!r}, "
            f"nightly={self.nightly!r})"
        )

    def is_empty(self) -> bool:
        return not self.hourly and not self.daily and not self.nightly

    def for_metric(self, metric: str) -> list[MetricHourBucket] | list[MetricDayBucket]:
        """Buckets belonging to one metric, in the family the registry assigns."""
        spec = registry.spec_for(metric)
        if spec is not None and spec.kind is BucketKind.HOURLY_DISCRETE:
            return [b for b in self.hourly if b.metric == metric]
        return [b for b in self.daily if b.metric == metric]

    @property
    def heart_rate_hourly(self) -> list[MetricHourBucket]:
        """v3 spelling, retained for readers."""
        return [b for b in self.hourly if b.metric == "heart_rate"]

    @property
    def steps_daily(self) -> list[MetricDayBucket]:
        """v3 spelling, retained for readers."""
        return [b for b in self.daily if b.metric == "steps"]


# --- v3 compatibility ---------------------------------------------------------
#
# v3 named its two bucket arrays after the metrics they carried. Internally those
# are now ordinary metric-keyed buckets, so v3 and v4 take exactly the same
# storage path and the older format cannot drift from the newer one. These
# factories keep the v3 spelling working for callers and tests.


def HeartRateHourBucket(  # constructor-shaped by design, hence the name
    start: datetime,
    mean: float,
    minimum: float,
    maximum: float,
    count: int | None = None,
) -> MetricHourBucket:
    return MetricHourBucket(
        metric="heart_rate", start=start, mean=mean, minimum=minimum,
        maximum=maximum, count=count,
    )


def StepsDayBucket(  # constructor-shaped by design, hence the name
    day: date, time_zone: str, total: float
) -> MetricDayBucket:
    return MetricDayBucket(
        metric="steps", day=day, time_zone=time_zone, total=total
    )


@dataclass(slots=True)
class Rejection:
    index: int
    collection: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "collection": self.collection, "reason": self.reason}


@dataclass(slots=True)
class ParsedPayload:
    kind: Literal["sync", "ping"]
    sent_at: datetime
    device: dict[str, Any]
    version: int = WIRE_VERSION
    samples: list[Sample] = field(default_factory=list)
    daily_totals: list[DailyTotal] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    # Diagnostic correlation only. No correctness guarantee depends on it.
    sync_id: str | None = None
    # v1: every delivery is a complete sync. v2: only an explicit final one.
    is_final: bool = True
    snapshot: Snapshot | None = None
    # v3 only. Aggregate history destined for long-term statistics.
    history: AggregateHistory | None = None


def _parse_timestamp(raw: Any, *, now: datetime) -> datetime:
    """Parse an RFC 3339 timestamp, raising ValueError with a reason code."""
    if not isinstance(raw, str):
        raise ValueError("missing_field")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise ValueError("bad_timestamp") from err
    if parsed.tzinfo is None:
        raise ValueError("bad_timestamp")
    parsed = parsed.astimezone(UTC)
    if parsed < TIMESTAMP_FLOOR:
        raise ValueError("bad_timestamp")
    if parsed > now + FUTURE_TOLERANCE:
        raise ValueError("future_timestamp")
    return parsed


def _parse_value(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("bad_value")
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("bad_value")
    return value


def _parse_uuid(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("missing_field")
    try:
        return str(UUID(raw)).upper()
    except (ValueError, AttributeError, TypeError) as err:
        raise ValueError("bad_uuid") from err


def _parse_sample(item: Any, *, now: datetime) -> Sample:
    if not isinstance(item, dict):
        raise ValueError("missing_field")

    metric = item.get("metric")
    if not isinstance(metric, str):
        raise ValueError("missing_field")
    definition = LEGACY_SAMPLE_METRICS.get(metric)
    if definition is None:
        raise ValueError("unknown_metric")
    if definition["kind"] != "discrete":
        # A cumulative metric must never arrive as raw samples - summing
        # overlapping iPhone/Watch samples would double-count.
        raise ValueError("wrong_kind")

    start = _parse_timestamp(item.get("start"), now=now)
    end = _parse_timestamp(item.get("end"), now=now)
    if end < start:
        raise ValueError("bad_timestamp")

    return Sample(
        metric=metric,
        uuid=_parse_uuid(item.get("uuid")),
        start=start,
        end=end,
        value=_parse_value(item.get("value")),
        unit=item.get("unit") if isinstance(item.get("unit"), str) else definition["unit"],
        source=item.get("source") if isinstance(item.get("source"), str) else None,
    )


def _parse_daily_total(item: Any, *, now: datetime) -> DailyTotal:
    if not isinstance(item, dict):
        raise ValueError("missing_field")

    metric = item.get("metric")
    if not isinstance(metric, str):
        raise ValueError("missing_field")
    definition = LEGACY_SAMPLE_METRICS.get(metric)
    if definition is None:
        raise ValueError("unknown_metric")
    if definition["kind"] != "cumulative":
        raise ValueError("wrong_kind")

    raw_date = item.get("date")
    if not isinstance(raw_date, str):
        raise ValueError("missing_field")
    try:
        day = date.fromisoformat(raw_date)
    except ValueError as err:
        raise ValueError("bad_timestamp") from err
    if day < TIMESTAMP_FLOOR.date():
        raise ValueError("bad_timestamp")
    if day > (now + FUTURE_TOLERANCE).date() + timedelta(days=1):
        # One day of slack: the sender's local day can legitimately be ahead of UTC.
        raise ValueError("future_timestamp")

    time_zone = item.get("time_zone")
    if not isinstance(time_zone, str) or not time_zone:
        raise ValueError("missing_field")

    return DailyTotal(
        metric=metric,
        day=day,
        time_zone=time_zone,
        value=_parse_value(item.get("value")),
        unit=item.get("unit") if isinstance(item.get("unit"), str) else definition["unit"],
    )


def _collection(raw: Any, name: str, limit: int) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PayloadError(f"bad_{name}")
    if len(raw) > limit:
        raise PayloadError(f"too_many_{name}")
    return raw


def _parse_heart_rate_snapshot(raw: Any, *, now: datetime) -> HeartRateSnapshot:
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    try:
        return HeartRateSnapshot(
            value=_parse_value(raw.get("value")),
            unit=raw.get("unit") if isinstance(raw.get("unit"), str)
            else LEGACY_SAMPLE_METRICS["heart_rate"]["unit"],
            measured_at=_parse_timestamp(raw.get("measured_at"), now=now),
            source=raw.get("source") if isinstance(raw.get("source"), str) else None,
        )
    except ValueError as err:
        raise PayloadError(f"bad_snapshot_{err}") from err


def _parse_steps_snapshot(raw: Any, *, now: datetime) -> StepsSnapshot:
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    raw_date = raw.get("date")
    time_zone = raw.get("time_zone")
    if not isinstance(raw_date, str) or not isinstance(time_zone, str) or not time_zone:
        raise PayloadError("bad_snapshot_missing_field")
    try:
        day = date.fromisoformat(raw_date)
        value = _parse_value(raw.get("value"))
    except ValueError as err:
        raise PayloadError(f"bad_snapshot_{err}") from err
    return StepsSnapshot(
        value=value,
        unit=raw.get("unit") if isinstance(raw.get("unit"), str)
        else LEGACY_SAMPLE_METRICS["steps"]["unit"],
        day=day,
        time_zone=time_zone,
    )


def _parse_measurement_snapshot(
    raw: Any, *, metric: str, now: datetime
) -> MeasurementSnapshot:
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    spec = registry.spec_for(metric)
    assert spec is not None
    try:
        value = _parse_value(raw.get("value"))
        measured_at = _parse_timestamp(raw.get("measured_at"), now=now)
    except ValueError as err:
        raise PayloadError(f"bad_snapshot_{err}") from err
    _check_percent(metric, value)
    return MeasurementSnapshot(
        metric=metric,
        value=value,
        unit=raw.get("unit") if isinstance(raw.get("unit"), str) else spec.unit,
        measured_at=measured_at,
        source=raw.get("source") if isinstance(raw.get("source"), str) else None,
    )


def _parse_daily_total_snapshot(
    raw: Any, *, metric: str, now: datetime
) -> DailyTotalSnapshot:
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    spec = registry.spec_for(metric)
    assert spec is not None
    raw_date = raw.get("date")
    time_zone = raw.get("time_zone")
    if not isinstance(raw_date, str) or not isinstance(time_zone, str) or not time_zone:
        raise PayloadError("bad_snapshot_missing_field")
    try:
        day = date.fromisoformat(raw_date)
        value = _parse_value(raw.get("value"))
    except ValueError as err:
        raise PayloadError(f"bad_snapshot_{err}") from err
    if value < 0:
        raise PayloadError("bad_snapshot_bad_value")
    return DailyTotalSnapshot(
        metric=metric,
        value=value,
        unit=raw.get("unit") if isinstance(raw.get("unit"), str) else spec.unit,
        day=day,
        time_zone=time_zone,
    )


def _optional_minutes(raw: Any, field_name: str) -> float | None:
    """A duration that may legitimately be absent.

    ``None`` and an absent key both mean "not measured" and stay ``None``. Zero
    is a measurement and stays zero - the distinction the whole sleep model rests
    on, so it is enforced here rather than left to the caller.
    """
    if raw is None:
        return None
    try:
        value = _parse_value(raw)
    except ValueError as err:
        raise PayloadError(f"bad_nightly_{err}") from err
    if value < 0:
        raise PayloadError(f"bad_nightly_negative_{field_name}")
    return value


def _parse_sleep_trend(raw: Any) -> SleepTrend:
    if not isinstance(raw, dict):
        raise PayloadError("bad_sleep_trend")
    nights = raw.get("nights")
    if isinstance(nights, bool) or not isinstance(nights, int) or nights < 0:
        raise PayloadError("bad_sleep_trend_nights")

    fields = (
        "avg_total_min", "avg_rem_min", "avg_core_min", "avg_deep_min",
        "avg_awake_min", "avg_sleep_start_offset_min", "avg_wake_offset_min",
        "sleep_start_stddev_min", "avg_nap_total_min",
    )
    values: dict[str, float | None] = {}
    for name in fields:
        entry = raw.get(name)
        if entry is None:
            values[name] = None
            continue
        try:
            values[name] = _parse_value(entry)
        except ValueError as err:
            raise PayloadError(f"bad_sleep_trend_{err}") from err

    contributing = raw.get("nights_by_field") or {}
    if not isinstance(contributing, dict):
        raise PayloadError("bad_sleep_trend")
    counts: dict[str, int] = {}
    for key, value in contributing.items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > nights
        ):
            raise PayloadError("bad_sleep_trend_nights_by_field")
        counts[key] = value

    return SleepTrend(nights=nights, nights_by_field=counts, **values)


def _parse_blood_pressure(raw: Any, *, now: datetime) -> BloodPressureSnapshot:
    """Parse one correlated pair.

    Both halves are required: an incomplete pair is refused rather than stored
    with a fabricated or missing counterpart.
    """
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    if raw.get("systolic") is None or raw.get("diastolic") is None:
        raise PayloadError("blood_pressure_incomplete_pair")

    spec = registry.METRICS["blood_pressure_systolic"]
    try:
        systolic = _parse_value(raw["systolic"])
        diastolic = _parse_value(raw["diastolic"])
        measured_at = _parse_timestamp(raw.get("measured_at"), now=now)
    except ValueError as err:
        raise PayloadError(f"bad_snapshot_{err}") from err

    if systolic <= 0 or diastolic <= 0:
        raise PayloadError("bad_snapshot_bad_value")
    if diastolic > systolic:
        # Physiologically impossible, and the likeliest cause is the two halves
        # having been swapped or paired from different measurements.
        raise PayloadError("blood_pressure_inverted")

    return BloodPressureSnapshot(
        systolic=systolic,
        diastolic=diastolic,
        unit=raw.get("unit") if isinstance(raw.get("unit"), str) else spec.unit,
        measured_at=measured_at,
        source=raw.get("source") if isinstance(raw.get("source"), str) else None,
    )


def _parse_last_workout(raw: Any, *, now: datetime) -> LastWorkout:
    """Parse the latest workout summary.

    The activity must be one of the agreed vocabulary. An unrecognised
    identifier is rejected rather than stored: the client already maps anything
    it does not know to `other`, so something outside the set means the two
    halves disagree about the taxonomy, not that Apple added a sport.
    """
    if not isinstance(raw, dict):
        raise PayloadError("bad_workout")

    uuid = raw.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        raise PayloadError("workout_missing_uuid")

    activity = raw.get("activity")
    if activity not in WORKOUT_ACTIVITIES:
        raise PayloadError("workout_unknown_activity")

    try:
        start = _parse_timestamp(raw.get("start"), now=now)
        end = _parse_timestamp(raw.get("end"), now=now)
        duration = _parse_value(raw.get("duration_min"))
    except ValueError as err:
        raise PayloadError(f"bad_workout_{err}") from err

    if end < start:
        raise PayloadError("workout_ends_before_it_starts")
    if duration < 0:
        raise PayloadError("bad_workout_bad_value")
    # A pause-aware duration can be shorter than the span but never longer.
    if duration > (end - start).total_seconds() / 60 + 1:
        raise PayloadError("workout_duration_exceeds_span")

    def optional(name: str) -> float | None:
        entry = raw.get(name)
        if entry is None:
            return None
        try:
            value = _parse_value(entry)
        except ValueError as err:
            raise PayloadError(f"bad_workout_{err}") from err
        if value < 0:
            raise PayloadError(f"bad_workout_negative_{name}")
        return value

    return LastWorkout(
        uuid=uuid,
        activity=activity,
        start=start,
        end=end,
        duration_min=duration,
        active_energy_kcal=optional("active_energy_kcal"),
        distance_km=optional("distance_km"),
        avg_heart_rate_bpm=optional("avg_heart_rate_bpm"),
        max_heart_rate_bpm=optional("max_heart_rate_bpm"),
        source=raw.get("source") if isinstance(raw.get("source"), str) else None,
    )


def _parse_snapshot(raw: Any, *, now: datetime, version: int) -> Snapshot:
    """A snapshot member that is absent or null leaves that sensor unchanged."""
    if not isinstance(raw, dict):
        raise PayloadError("bad_snapshot")
    heart_rate = raw.get("heart_rate")
    steps = raw.get("steps_today")
    snapshot = Snapshot(
        heart_rate=None if heart_rate is None
        else _parse_heart_rate_snapshot(heart_rate, now=now),
        steps_today=None if steps is None else _parse_steps_snapshot(steps, now=now),
    )
    if version < 4:
        return snapshot

    for metric, spec in registry.METRICS.items():
        if metric in ("heart_rate", "steps"):
            # Carried under their v2 keys so those semantics are untouched.
            continue
        if not spec.snapshot_key:
            # Opted out of an individual snapshot - blood pressure, whose current
            # value only arrives as a complete correlated pair.
            continue
        entry = raw.get(spec.snapshot_key)
        if entry is None:
            continue
        if spec.kind is BucketKind.DAILY_CUMULATIVE:
            snapshot.daily_totals[metric] = _parse_daily_total_snapshot(
                entry, metric=metric, now=now
            )
        else:
            snapshot.measurements[metric] = _parse_measurement_snapshot(
                entry, metric=metric, now=now
            )

    if (night := raw.get("sleep_last_night")) is not None:
        snapshot.sleep_last_night = _parse_nightly(night, now=now)
    if (trend := raw.get("sleep_7d")) is not None:
        snapshot.sleep_7d = _parse_sleep_trend(trend)
    if (pressure := raw.get("blood_pressure")) is not None:
        snapshot.blood_pressure = _parse_blood_pressure(pressure, now=now)
    if (workout := raw.get("last_workout")) is not None:
        snapshot.last_workout = _parse_last_workout(workout, now=now)
    return snapshot


def _check_percent(metric: str, *values: float) -> None:
    """Guard the percent contract for oxygen saturation.

    HealthKit reports oxygen saturation as a 0.0-1.0 fraction and the client
    converts it to human percent before sending. A value that still looks like a
    fraction is rejected rather than stored, because 0.98 written to a "%" series
    is silently wrong by two orders of magnitude and looks plausible enough to go
    unnoticed for a long time.
    """
    if metric not in PERCENT_METRICS:
        return
    for value in values:
        if not PERCENT_MIN <= value <= PERCENT_MAX:
            raise PayloadError("bucket_percent_out_of_range")


def _check_fields(raw: dict[str, Any], spec: registry.MetricSpec) -> None:
    """Enforce exactly the aggregate fields the registry declares for a metric.

    Both directions matter. A missing required field would store an incomplete
    bucket; an unexpected one means the sender believes it is delivering
    something this receiver will not store - for instance a min/max for resting
    heart rate, where Apple derives a single value per day and any spread would
    be invented rather than measured.
    """
    known = {"mean", "min", "max", "total", "count"}
    present = {name for name in known if raw.get(name) is not None}
    if spec.required - present:
        raise PayloadError("bad_bucket_missing_field")
    if present - spec.allowed:
        raise PayloadError("bad_bucket_unexpected_field")


# Everything a bucket object may carry. The aggregate names are listed in full
# rather than taken from the metric's own spec, so that a field this registry
# knows but does not permit for this metric keeps its specific reason code
# (``bad_bucket_unexpected_field``) instead of being reported as unknown.
_AGGREGATE_FIELDS = frozenset({"mean", "min", "max", "total", "count"})
_HOURLY_KEYS = frozenset({"start"}) | _AGGREGATE_FIELDS
_DAILY_KEYS = frozenset({"date", "time_zone"}) | _AGGREGATE_FIELDS
_NIGHTLY_KEYS = frozenset({
    "date", "time_zone", "total_sleep_min", "sleep_start", "wake_time",
    "rem_min", "core_min", "deep_min", "awake_min",
})


def _bucket_keys(base: frozenset[str], version: int) -> frozenset[str]:
    """The legal keys of a bucket, which gained ``metric`` in v4."""
    return (base | {"metric"}) if version >= 4 else base


def _reject_unknown_keys(
    raw: dict[str, Any], allowed: frozenset[str], reason: str
) -> None:
    """Refuse a bucket carrying a key this receiver does not implement.

    Same reasoning as the metric registry and the bucket-kind check one level
    up: a key nobody here reads is a measurement the sender believes it stored.
    Answering 200 and dropping it is the one failure this protocol was versioned
    to prevent, and a typo in a field name is indistinguishable from it.

    Deliberately not a forward-compatibility hatch. A client with something new
    to say says it in a new protocol version, which an older receiver refuses
    outright - visibly, before any data moves - rather than half-storing.
    """
    if set(raw) - allowed:
        raise PayloadError(reason)


def _parse_count(raw: Any) -> int | None:
    count = raw.get("count")
    if count is not None and (
        isinstance(count, bool) or not isinstance(count, int) or count < 0
    ):
        raise PayloadError("bad_bucket_count")
    return count


def _parse_hour_bucket(
    raw: Any, *, now: datetime, metric: str = "heart_rate", version: int = 4
) -> MetricHourBucket:
    if not isinstance(raw, dict):
        raise PayloadError("bad_bucket")
    spec = registry.spec_for(metric)
    if spec is None:
        raise PayloadError("unknown_metric")
    if spec.kind is not BucketKind.HOURLY_DISCRETE:
        raise PayloadError("wrong_bucket_kind")
    _check_fields(raw, spec)
    # ``metric`` is a v4 key. In a v3 bucket it would be read by nobody, which is
    # the v3-envelope-carrying-v4-keys case the compatibility table rejects.
    _reject_unknown_keys(
        raw, _bucket_keys(_HOURLY_KEYS, version), "bad_bucket_unknown_field"
    )

    try:
        start = _parse_timestamp(raw.get("start"), now=now)
        mean = _parse_value(raw.get("mean"))
        minimum = _parse_value(raw.get("min"))
        maximum = _parse_value(raw.get("max"))
    except ValueError as err:
        raise PayloadError(f"bad_bucket_{err}") from err

    # Home Assistant keys long-term statistics on hour-aligned UTC starts.
    if (start.minute, start.second, start.microsecond) != (0, 0, 0):
        raise PayloadError("bucket_start_not_hour_aligned")
    if not minimum <= mean <= maximum:
        raise PayloadError("bucket_range_inconsistent")
    _check_percent(metric, mean, minimum, maximum)

    return MetricHourBucket(
        metric=metric, start=start, mean=mean, minimum=minimum,
        maximum=maximum, count=_parse_count(raw),
    )


def _parse_local_day(raw: Any, *, now: datetime) -> tuple[date, str]:
    """The (local date, time zone) key shared by every daily bucket."""
    raw_date = raw.get("date")
    if not isinstance(raw_date, str):
        raise PayloadError("bad_bucket_missing_field")
    try:
        day = date.fromisoformat(raw_date)
    except ValueError as err:
        raise PayloadError("bad_bucket_bad_timestamp") from err

    if day < TIMESTAMP_FLOOR.date():
        raise PayloadError("bad_bucket_bad_timestamp")
    # One day of slack: a device ahead of UTC is legitimately on tomorrow's date.
    if day > (now + FUTURE_TOLERANCE).date() + timedelta(days=1):
        raise PayloadError("bad_bucket_future_timestamp")

    time_zone = raw.get("time_zone")
    if not isinstance(time_zone, str) or not time_zone:
        raise PayloadError("bad_bucket_missing_field")
    try:
        # The receiver converts the local day to a UTC instant, so the zone has
        # to be one this machine can actually resolve.
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as err:
        raise PayloadError("bad_bucket_time_zone") from err
    return day, time_zone


def _parse_day_bucket(
    raw: Any, *, now: datetime, metric: str = "steps", version: int = 4
) -> MetricDayBucket:
    if not isinstance(raw, dict):
        raise PayloadError("bad_bucket")
    spec = registry.spec_for(metric)
    if spec is None:
        raise PayloadError("unknown_metric")
    if spec.kind not in (BucketKind.DAILY_CUMULATIVE, BucketKind.DAILY_DISCRETE):
        raise PayloadError("wrong_bucket_kind")
    _check_fields(raw, spec)
    _reject_unknown_keys(
        raw, _bucket_keys(_DAILY_KEYS, version), "bad_bucket_unknown_field"
    )
    day, time_zone = _parse_local_day(raw, now=now)

    if spec.kind is BucketKind.DAILY_CUMULATIVE:
        try:
            total = _parse_value(raw.get("total"))
        except ValueError as err:
            raise PayloadError(f"bad_bucket_{err}") from err
        if total < 0:
            raise PayloadError("bad_bucket_bad_value")
        return MetricDayBucket(
            metric=metric, day=day, time_zone=time_zone, total=total
        )

    try:
        mean = _parse_value(raw.get("mean"))
        # Keyed on what the registry *allows*, not on what it requires. Reading
        # only required fields would silently drop a value a future metric
        # declared optional - after _check_fields had already accepted it, which
        # is precisely the quiet loss this protocol exists to avoid.
        minimum = (
            _parse_value(raw["min"])
            if "min" in spec.allowed and raw.get("min") is not None
            else None
        )
        maximum = (
            _parse_value(raw["max"])
            if "max" in spec.allowed and raw.get("max") is not None
            else None
        )
    except ValueError as err:
        raise PayloadError(f"bad_bucket_{err}") from err
    if minimum is not None and maximum is not None and not minimum <= mean <= maximum:
        raise PayloadError("bucket_range_inconsistent")
    _check_percent(metric, *(v for v in (mean, minimum, maximum) if v is not None))

    return MetricDayBucket(
        metric=metric, day=day, time_zone=time_zone, mean=mean,
        minimum=minimum, maximum=maximum, count=_parse_count(raw),
    )


def _parse_nightly(raw: Any, *, now: datetime) -> NightlySleep:
    """One already-aggregated night.

    Invariants are checked where they are safe to check. They deliberately stop
    short of demanding well-staged sleep: partial Apple Health data is normal and
    legitimate, so a night with a total and no stages must be accepted as it is.
    """
    if not isinstance(raw, dict):
        raise PayloadError("bad_nightly")
    day, time_zone = _parse_local_day(raw, now=now)

    try:
        total = _parse_value(raw.get("total_sleep_min"))
        sleep_start = _parse_timestamp(raw.get("sleep_start"), now=now)
        wake_time = _parse_timestamp(raw.get("wake_time"), now=now)
    except ValueError as err:
        raise PayloadError(f"bad_nightly_{err}") from err

    if total < 0:
        raise PayloadError("bad_nightly_negative_total")
    if wake_time <= sleep_start:
        raise PayloadError("nightly_wake_before_sleep")
    span = wake_time - sleep_start
    if span > MAX_NIGHT_SPAN:
        raise PayloadError("nightly_span_too_long")
    span_minutes = span.total_seconds() / 60
    if total > span_minutes + SLEEP_TOLERANCE_MIN:
        raise PayloadError("nightly_total_exceeds_span")

    stages = {
        name: _optional_minutes(raw.get(name), name)
        for name in ("rem_min", "core_min", "deep_min", "awake_min")
    }
    staged = [v for k, v in stages.items() if k != "awake_min" and v is not None]
    if staged and sum(staged) > total + SLEEP_TOLERANCE_MIN:
        # REM + Core + Deep are subsets of total sleep; unspecified sleep makes
        # up any remainder, so their sum can be lower but never higher.
        raise PayloadError("nightly_stages_exceed_total")
    if (awake := stages["awake_min"]) is not None and awake > span_minutes + SLEEP_TOLERANCE_MIN:
        raise PayloadError("nightly_awake_exceeds_span")

    # The wake date must plausibly match the wake instant. Kept to one day of
    # slack rather than exact, so a night that ends after crossing a time zone is
    # still accepted.
    local_wake = wake_time.astimezone(ZoneInfo(time_zone)).date()
    if abs((local_wake - day).days) > 1:
        raise PayloadError("nightly_date_mismatch")

    # Rejected rather than ignored. A client still putting naps on the nightly
    # record believes they are being stored; accepting the payload and dropping
    # them would answer 200 while losing a day's naps, which is the failure this
    # protocol exists to prevent. Naps belong in their own daily metrics.
    if "nap_total_min" in raw or "nap_count" in raw:
        raise PayloadError("nightly_nap_fields_moved")

    # Checked last so the moved nap fields keep their own reason code: they are
    # unknown here too, but "they moved" is the answer their sender needs.
    _reject_unknown_keys(raw, _NIGHTLY_KEYS, "bad_nightly_unknown_field")

    return NightlySleep(
        day=day, time_zone=time_zone, total_sleep_min=total,
        sleep_start=sleep_start, wake_time=wake_time, **stages,
    )


def _reject_duplicates(keys: list[Any], reason: str) -> None:
    if len(set(keys)) != len(keys):
        # Duplicate keys within one request would make the import order-dependent.
        raise PayloadError(reason)


def _parse_history(raw: Any, *, now: datetime, version: int) -> AggregateHistory:
    """Aggregate history is validated strictly and rejected as a whole.

    Unlike samples, a bad bucket fails the entire request: the caller applies
    nothing unless every bucket is sound (validate-all-then-apply).

    v3 and v4 differ only in how the buckets are keyed on the wire. Both produce
    the same metric-keyed buckets, so the two versions share one storage path and
    the older format cannot drift away from the newer one.
    """
    if not isinstance(raw, dict):
        raise PayloadError("bad_buckets")

    allowed_keys = (
        {"heart_rate_hourly", "steps_daily"} if version < 4
        else {"hourly", "daily", "nightly"}
    )
    if set(raw) - allowed_keys:
        # An unrecognised key means the sender expects storage this receiver will
        # not provide. Rejecting is the whole point of the version contract.
        raise PayloadError("unknown_bucket_kind")

    history = AggregateHistory()

    if version < 4:
        hourly = raw.get("heart_rate_hourly") or []
        daily = raw.get("steps_daily") or []
        if not isinstance(hourly, list) or not isinstance(daily, list):
            raise PayloadError("bad_buckets")
        if len(hourly) > MAX_HOURLY_BUCKETS:
            raise PayloadError("too_many_hourly_buckets")
        if len(daily) > MAX_DAILY_BUCKETS:
            raise PayloadError("too_many_daily_buckets")
        history.hourly = [
            _parse_hour_bucket(item, now=now, version=version) for item in hourly
        ]
        history.daily = [
            _parse_day_bucket(item, now=now, version=version) for item in daily
        ]
    else:
        hourly = raw.get("hourly") or []
        daily = raw.get("daily") or []
        nightly = raw.get("nightly") or []
        if not all(isinstance(x, list) for x in (hourly, daily, nightly)):
            raise PayloadError("bad_buckets")
        if len(hourly) > MAX_HOURLY_BUCKETS:
            raise PayloadError("too_many_hourly_buckets")
        if len(daily) > MAX_V4_DAILY_BUCKETS:
            raise PayloadError("too_many_daily_buckets")
        if len(nightly) > MAX_NIGHTLY_BUCKETS:
            raise PayloadError("too_many_nightly_buckets")

        history.hourly = [
            _parse_hour_bucket(item, now=now, metric=_bucket_metric(item))
            for item in hourly
        ]
        history.daily = [
            _parse_day_bucket(item, now=now, metric=_bucket_metric(item))
            for item in daily
        ]
        history.nightly = [_parse_nightly(item, now=now) for item in nightly]

    # Per-metric ceiling, checked after parsing because the wire arrays are
    # metric-keyed rather than grouped. Rejected, never trimmed: trimming a
    # combined array would drop whole metrics rather than old buckets, which is
    # exactly the silent loss the daily family was fixed for. One metric can
    # never squeeze out another because each is counted on its own.
    per_metric: dict[str, int] = {}
    for bucket in history.hourly:
        per_metric[bucket.metric] = per_metric.get(bucket.metric, 0) + 1
    if any(count > MAX_HOURLY_BUCKETS_PER_METRIC for count in per_metric.values()):
        raise PayloadError("too_many_hourly_buckets_for_metric")

    _reject_duplicates(
        [(b.metric, b.start) for b in history.hourly], "duplicate_hourly_bucket"
    )
    _reject_duplicates(
        [(b.metric, b.day) for b in history.daily], "duplicate_daily_bucket"
    )
    _reject_duplicates([n.day for n in history.nightly], "duplicate_nightly_bucket")
    _check_blood_pressure_pairs(history)
    return history


def _check_blood_pressure_pairs(history: AggregateHistory) -> None:
    """Both halves of blood pressure must describe the same measurements.

    Systolic and diastolic come from one correlation on the device, so for any
    hour they must appear together and agree on how many readings they average.
    A mismatch means the two series were built from different sets, and a
    weighted trend computed from them would silently mix contributors.

    Checked here rather than downstream so it fails loudly at the contract
    boundary instead of skewing a number nobody can eyeball.
    """
    systolic, diastolic = BLOOD_PRESSURE_METRICS
    by_metric: dict[str, dict[datetime, int | None]] = {systolic: {}, diastolic: {}}
    for bucket in history.hourly:
        if bucket.metric in by_metric:
            by_metric[bucket.metric][bucket.start] = bucket.count

    if set(by_metric[systolic]) != set(by_metric[diastolic]):
        raise PayloadError("blood_pressure_hours_mismatched")
    for start, count in by_metric[systolic].items():
        if count != by_metric[diastolic][start]:
            raise PayloadError("blood_pressure_counts_mismatched")
        if count is not None and count <= 0:
            # An hour is only present because it held measurements.
            raise PayloadError("blood_pressure_count_not_positive")


def _bucket_metric(item: Any) -> str:
    """The metric identifier a v4 bucket declares.

    An unknown identifier is rejected here rather than skipped: skipping it would
    return HTTP 200 while dropping whatever it carried.
    """
    if not isinstance(item, dict):
        raise PayloadError("bad_bucket")
    metric = item.get("metric")
    if not isinstance(metric, str) or not metric:
        raise PayloadError("bad_bucket_missing_field")
    if registry.spec_for(metric) is None:
        raise PayloadError("unknown_metric")
    return metric


def parse(body: Any, *, now: datetime | None = None) -> ParsedPayload:
    """Parse a decoded JSON body into a validated payload.

    Envelope problems raise :class:`PayloadError` (HTTP 400). Malformed
    individual items are collected into ``rejected`` so one bad sample cannot
    fail an otherwise good batch.
    """
    now = now or datetime.now(UTC)

    if not isinstance(body, dict):
        raise PayloadError("not_an_object")
    version = body.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise PayloadError("unsupported_version")

    kind = body.get("type")
    if kind not in ("sync", "ping"):
        raise PayloadError("unsupported_type")

    try:
        sent_at = _parse_timestamp(body.get("sent_at"), now=now)
    except ValueError as err:
        raise PayloadError(str(err)) from err

    device = body.get("device")
    if not isinstance(device, dict):
        raise PayloadError("missing_device")

    payload = ParsedPayload(
        kind=kind, sent_at=sent_at, device=device, version=version
    )

    if kind == "ping":
        # A ping must not carry or apply data.
        return payload

    if version >= 2:
        # v2 requires explicit completion semantics on every sync delivery, so a
        # client that omits them fails loudly instead of never completing.
        sync_meta = body.get("sync")
        if not isinstance(sync_meta, dict):
            raise PayloadError("missing_sync")
        is_final = sync_meta.get("final")
        if not isinstance(is_final, bool):
            raise PayloadError("missing_sync")
        payload.is_final = is_final
        sync_id = sync_meta.get("id")
        payload.sync_id = sync_id if isinstance(sync_id, str) else None

        if is_final:
            raw_snapshot = body.get("snapshot")
            if raw_snapshot is None:
                raise PayloadError("missing_snapshot")
            payload.snapshot = _parse_snapshot(
                raw_snapshot, now=now, version=version
            )

    if version >= 3 and (raw_buckets := body.get("buckets")) is not None:
        payload.history = _parse_history(raw_buckets, now=now, version=version)

    for index, item in enumerate(_collection(body.get("samples"), "samples", MAX_SAMPLES)):
        try:
            payload.samples.append(_parse_sample(item, now=now))
        except ValueError as err:
            payload.rejected.append(Rejection(index, "samples", str(err)))

    for index, item in enumerate(
        _collection(body.get("daily_totals"), "daily_totals", MAX_DAILY_TOTALS)
    ):
        try:
            payload.daily_totals.append(_parse_daily_total(item, now=now))
        except ValueError as err:
            payload.rejected.append(Rejection(index, "daily_totals", str(err)))

    for index, item in enumerate(_collection(body.get("deletions"), "deletions", MAX_DELETIONS)):
        try:
            payload.deletions.append(_parse_uuid(item))
        except ValueError as err:
            payload.rejected.append(Rejection(index, "deletions", str(err)))

    return payload

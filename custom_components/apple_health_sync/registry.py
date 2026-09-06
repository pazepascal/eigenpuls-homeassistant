"""The metric registry - the single contract both halves of the bridge share.

Wire format v4 is metric-oriented: a bucket carries a ``metric`` identifier and
this registry says what that identifier means. Adding a Phase 3B metric whose
semantics fit an existing bucket family is therefore a registry entry, not a new
protocol version (``protocol/payload-v4.md`` section 9).

The registry is deliberately *closed*. An unknown metric identifier is rejected
rather than stored: silently accepting a metric this receiver has no statistic
metadata for would mean answering HTTP 200 while dropping health data, which is
the failure mode every version bump in this project has existed to prevent.

Deliberately free of Home Assistant imports, like ``payload.py``, which imports
this module: the parsing layer is a clean boundary that can be reasoned about
and tested without a Home Assistant instance. ``MeanType`` below is therefore
our own enum;
``statistics.py`` translates it to ``StatisticMeanType`` at the single point of
use. That translation is asserted in the tests so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .const import DOMAIN


class BucketKind(StrEnum):
    """The semantic families a metric can belong to.

    Kept distinct rather than collapsed into one maximally generic shape: each
    family has different required fields, and folding them together would lose
    the validation that makes a wrong-shaped payload a clean rejection instead of
    a silent partial write.
    """

    HOURLY_DISCRETE = "hourly_discrete"
    DAILY_DISCRETE = "daily_discrete"
    DAILY_CUMULATIVE = "daily_cumulative"
    NIGHTLY_SLEEP = "nightly_sleep"


class MeanType(StrEnum):
    """Mirrors Home Assistant's StatisticMeanType without importing it."""

    NONE = "none"
    ARITHMETIC = "arithmetic"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Everything the receiver needs in order to validate and store one metric."""

    metric: str
    kind: BucketKind
    statistic_suffix: str
    name: str
    unit: str
    #: Home Assistant unit class. Verified empirically against the installed
    #: version rather than assumed - see the table in section 3 of
    #: protocol/payload-v4.md. A wrong value here is rejected at import time.
    unit_class: str | None
    mean_type: MeanType
    has_sum: bool
    required: frozenset[str]
    #: Key this metric's current value appears under in the v4 snapshot. Held
    #: here so a new metric is one registry entry and not an entry plus a lookup
    #: table somewhere else.
    #:
    #: Empty means the metric has **no individual snapshot entry**. Blood
    #: pressure uses that: a lone systolic reading is not a blood-pressure
    #: measurement, so the only way to set the current value is the composite
    #: `blood_pressure` snapshot carrying both halves of one correlated reading.
    snapshot_key: str = ""
    optional: frozenset[str] = field(default_factory=frozenset)

    @property
    def statistic_id(self) -> str:
        return f"{DOMAIN}:{self.statistic_suffix}"

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


def _discrete(
    metric: str,
    suffix: str,
    name: str,
    unit: str,
    unit_class: str | None,
    kind: BucketKind,
    *,
    mean_only: bool = False,
    snapshot_key: str | None = None,
) -> MetricSpec:
    """A metric whose buckets carry a mean, and usually a min and max too."""
    return MetricSpec(
        metric=metric,
        kind=kind,
        statistic_suffix=suffix,
        name=name,
        unit=unit,
        unit_class=unit_class,
        mean_type=MeanType.ARITHMETIC,
        has_sum=False,
        # None means "default to the metric name"; "" means "no snapshot entry".
        snapshot_key=metric if snapshot_key is None else snapshot_key,
        required=frozenset({"mean"}) if mean_only else frozenset({"mean", "min", "max"}),
        optional=frozenset() if mean_only else frozenset({"count"}),
    )


def _cumulative(
    metric: str,
    suffix: str,
    name: str,
    unit: str,
    unit_class: str | None,
    snapshot_key: str | None = None,
) -> MetricSpec:
    """A metric whose daily bucket carries a total, stored as a running sum."""
    return MetricSpec(
        metric=metric,
        kind=BucketKind.DAILY_CUMULATIVE,
        statistic_suffix=suffix,
        name=name,
        unit=unit,
        unit_class=unit_class,
        mean_type=MeanType.NONE,
        has_sum=True,
        required=frozenset({"total"}),
        # None means "default to <metric>_today"; "" means "no snapshot entry",
        # matching `_discrete`. The `or` idiom would silently turn "" back into
        # the default and give a metric a snapshot key it opted out of.
        snapshot_key=f"{metric}_today" if snapshot_key is None else snapshot_key,
    )


METRICS: Final[dict[str, MetricSpec]] = {
    spec.metric: spec
    for spec in (
        # --- existing metrics; semantics preserved exactly under v4 ---
        _discrete(
            "heart_rate", "heart_rate", "Heart rate", "bpm", None,
            BucketKind.HOURLY_DISCRETE,
        ),
        _cumulative("steps", "steps_daily", "Steps", "steps", None),
        # --- Phase 3A ---
        # Apple derives a single resting value per day, so a min and max would be
        # invented rather than measured: mean only, and anything else rejected.
        _discrete(
            "resting_heart_rate", "resting_heart_rate", "Resting heart rate",
            "bpm", None, BucketKind.DAILY_DISCRETE, mean_only=True,
        ),
        _discrete(
            "hrv_sdnn", "hrv_sdnn", "Heart rate variability (SDNN)",
            "ms", "duration", BucketKind.DAILY_DISCRETE,
        ),
        _discrete(
            "respiratory_rate", "respiratory_rate", "Respiratory rate",
            "breaths/min", None, BucketKind.DAILY_DISCRETE,
        ),
        # HealthKit reports oxygen saturation as a 0.0-1.0 fraction. The client
        # converts to human percent before sending; the receiver stores percent
        # and range-checks it, so a raw fraction cannot pass unnoticed.
        _discrete(
            "oxygen_saturation", "oxygen_saturation", "Blood oxygen",
            "%", "unitless", BucketKind.DAILY_DISCRETE,
        ),
        _cumulative("active_energy", "active_energy", "Active energy", "kcal", "energy"),
        _cumulative(
            "distance_walking_running", "distance", "Walking + running distance",
            "km", "distance", snapshot_key="distance_today",
        ),
        # --- Phase 3A.2 ---
        # Naps are a property of the calendar day, not of the night. They are
        # ordinary daily metrics rather than fields on the nightly record so a
        # nap can be stored on a day with no main sleep at all - a day of naps is
        # real data, and dropping it because no night qualified would be exactly
        # the silent loss this protocol exists to prevent.
        #
        # The statistic ids are unchanged from when these rode on the nightly
        # record, so no history migrates.
        _discrete(
            "nap_total", "nap_total", "Nap duration", "min", "duration",
            BucketKind.DAILY_DISCRETE, mean_only=True, snapshot_key="nap_total_today",
        ),
        _discrete(
            "nap_count", "nap_count", "Nap count", "naps", None,
            BucketKind.DAILY_DISCRETE, mean_only=True, snapshot_key="nap_count_today",
        ),
        # --- Phase 3B.1: body composition and blood pressure ---
        #
        # Hourly rather than daily. A daily bucket would average a morning and an
        # evening weigh-in, which differ by a kilogram or more, and the same
        # applies to blood pressure - that is not a rounding loss but a
        # misleading number. Hourly keeps time-of-day, which is where the signal
        # is, without needing a full event family.
        #
        # These are sparse: the client reads them over a 90-day lookback but
        # emits only the hours that actually contain a measurement, so a
        # realistic upload is a few dozen buckets rather than 90 x 24.
        _discrete(
            "body_mass", "body_mass", "Body mass", "kg", "mass",
            BucketKind.HOURLY_DISCRETE,
        ),
        # HealthKit reports body fat through percentUnit, documented as
        # "% (0.0 - 1.0)" - the same trap as blood oxygen. The client converts to
        # human percent and PERCENT_METRICS below refuses anything that still
        # looks like a fraction.
        _discrete(
            "body_fat_percentage", "body_fat_percentage", "Body fat", "%",
            "unitless", BucketKind.HOURLY_DISCRETE,
        ),
        # Systolic and diastolic keep independent durable series but have no
        # individual snapshot: half a reading is not a blood-pressure
        # measurement. The current value arrives as one correlated pair.
        _discrete(
            "blood_pressure_systolic", "blood_pressure_systolic",
            "Blood pressure (systolic)", "mmHg", "pressure",
            BucketKind.HOURLY_DISCRETE, snapshot_key="",
        ),
        _discrete(
            "blood_pressure_diastolic", "blood_pressure_diastolic",
            "Blood pressure (diastolic)", "mmHg", "pressure",
            BucketKind.HOURLY_DISCRETE, snapshot_key="",
        ),
        # --- Phase 3C: cardio fitness ---
        #
        # Hourly and sparse, for the same reason as body mass: the client reads a
        # 90-day lookback and emits only the hours that contain a measurement.
        # Apple Watch derives VO2 max from outdoor workouts, so a realistic
        # upload is a handful of buckets across three months, not 90 x 24.
        #
        # Hourly rather than daily is also what the client can express: the
        # daily-discrete path shares one window across the Phase 3A vitals and
        # does not consult `lookback_days`, so a 90-day daily metric would mean
        # changing the window of resting heart rate, HRV, respiratory rate and
        # blood oxygen. Hourly needs no shared-code change and loses nothing -
        # Home Assistant rolls hourly statistics up to day, week and month on its
        # own for graphing.
        #
        # Arithmetic mean is Apple's own aggregation for this type: the SDK
        # header documents HKQuantityTypeIdentifierVO2Max as
        # "ml/(kg*min), Discrete (Arithmetic)", and the runtime reports
        # aggregationStyle .discreteArithmetic. No aggregation was chosen here.
        #
        # unit_class is None because no Home Assistant unit converter knows this
        # unit - verified against the installed version, like bpm and
        # breaths/min. HealthKit yields mL/min·kg directly, so nothing is scaled
        # on the way out; there is no percent-style conversion here.
        _discrete(
            "vo2_max", "vo2_max", "VO2 max", "ml/kg/min", None,
            BucketKind.HOURLY_DISCRETE,
        ),
        # --- Phase 3B.2: training ---
        #
        # Daily totals, like steps: "how often and how long did I train this
        # week" is a sum, so Home Assistant's own week and month rollups answer
        # it directly. Deliberately only three - there is no daily distance,
        # because summing kilometres across swimming, cycling and running is
        # arithmetic without meaning. Distance stays on the individual workout.
        #
        # No individual snapshot: the current-value view of training is the
        # composite `last_workout`, not a bare count for today.
        _cumulative("workout_count", "workout_count", "Workouts", "workouts", None,
                    snapshot_key=""),
        _cumulative("workout_duration", "workout_duration", "Training duration",
                    "min", "duration", snapshot_key=""),
        _cumulative("workout_energy", "workout_energy", "Training energy",
                    "kcal", "energy", snapshot_key=""),
        # --- Phase 4A.2: Activity Summary -------------------------------
        #
        # Move, Exercise and Stand, each with its goal. Contract frozen in
        # `contract/activity-contract.json` and specified in
        # protocol/payload-v4.md §9a.
        #
        # No individual snapshot entries: every one carries `snapshot_key=""`
        # and the current-value view is the composite `activity` object, the
        # same choice blood pressure and `last_workout` already made. The
        # composite is what carries `move_mode`, and without the mode a bare
        # move value cannot say whether it is the ring.
        #
        # `activity_move_energy` is deliberately NOT `active_energy` and must
        # never be merged with it. `active_energy` sums Active Energy samples
        # over the local day from a statistics query and works without an Apple
        # Watch; this is Apple's own Move-ring figure with Apple's day boundary
        # and pause handling. They usually agree, they answer different
        # questions - and when the move mode is `move_time`, `active_energy` is
        # not the ring at all.
        _cumulative("activity_move_energy", "activity_move_energy", "Move energy",
                    "kcal", "energy", snapshot_key=""),
        _cumulative("activity_move_time", "activity_move_time", "Move time",
                    "min", "duration", snapshot_key=""),
        _cumulative("activity_exercise_time", "activity_exercise_time",
                    "Exercise time", "min", "duration", snapshot_key=""),
        # `hours` carries no unit class, and that is measured rather than
        # assumed. Home Assistant 2026.9.1 maps `h` to its DurationConverter but
        # has no converter for `hours`, so the latter stays an opaque unit like
        # `steps`, `naps` and `workouts`. That is what this metric needs: it is
        # a count of hours that *qualified*, not time elapsed, and letting Home
        # Assistant turn nine stand hours into 540 minutes would be arithmetic
        # without meaning.
        _cumulative("activity_stand_hours", "activity_stand_hours", "Stand hours",
                    "hours", None, snapshot_key=""),
        # Goals: one value per day. Mean-only, because a min/max spread would be
        # invented, and not cumulative, because a goal accumulates nothing.
        _discrete(
            "activity_move_energy_goal", "activity_move_energy_goal",
            "Move energy goal", "kcal", "energy", BucketKind.DAILY_DISCRETE,
            mean_only=True, snapshot_key="",
        ),
        _discrete(
            "activity_move_time_goal", "activity_move_time_goal",
            "Move time goal", "min", "duration", BucketKind.DAILY_DISCRETE,
            mean_only=True, snapshot_key="",
        ),
        _discrete(
            "activity_exercise_goal", "activity_exercise_goal",
            "Exercise goal", "min", "duration", BucketKind.DAILY_DISCRETE,
            mean_only=True, snapshot_key="",
        ),
        _discrete(
            "activity_stand_goal", "activity_stand_goal", "Stand goal",
            "hours", None, BucketKind.DAILY_DISCRETE,
            mean_only=True, snapshot_key="",
        ),
        # --- Phase 4B: the last four of the target catalogue -------------
        #
        # Four ordinary metrics in three shapes that already exist. None of them
        # needs a new bucket family, a new snapshot composite or a new read
        # path, which is the point: Activity was the hard one.
        #
        # Flights climbed is steps' twin - the SDK header says "count,
        # Cumulative" for both - so it is a daily total with an opaque unit and
        # Home Assistant's own week and month rollups answer "how much climbing
        # this month" directly.
        _cumulative("flights_climbed", "flights_climbed", "Flights climbed",
                    "flights", None),
        # Apple derives one walking average per day, exactly as it does for
        # resting heart rate, so this is that metric's twin: daily, mean only,
        # and a min/max spread would be invented rather than measured. The SDK
        # header declares both as "count/min, Discrete (Temporally Weighted)".
        #
        # Read from Apple's own type rather than computed from walking heart
        # rate samples: Apple's definition of which beats count is not ours to
        # reconstruct, and reconstructing it would produce a number that
        # disagrees with the Health app.
        _discrete(
            "walking_heart_rate_average", "walking_heart_rate_average",
            "Walking heart rate average", "bpm", None,
            BucketKind.DAILY_DISCRETE, mean_only=True,
        ),
        # Body mass index is a body metric and takes the body-metric shape:
        # hourly and sparse, so a morning and an evening reading stay distinct
        # instead of being averaged into a number nobody measured.
        #
        # `kg/m²` rather than no unit at all. HealthKit models this type as
        # `count` because HKUnit has no compound unit for it, not because the
        # quantity is dimensionless - it is mass over height squared and always
        # was. Home Assistant knows no converter for it, so `unit_class` is None
        # like `bpm` and `ml/kg/min`, and nothing is scaled on the way out.
        #
        # Read, never computed. Apple Health already holds a BMI value; deriving
        # a second one from weight and height would produce a number that
        # disagrees with the Health app whenever the stored height is stale.
        _discrete("bmi", "bmi", "Body mass index", "kg/m²", None,
                  BucketKind.HOURLY_DISCRETE),
        # Hourly for the same reason as blood pressure: time of day is the
        # signal, and a daily mean would average a fasting reading with a
        # post-meal one into something that describes neither.
        #
        # mg/dL is HealthKit's own canonical unit for the type. Home Assistant
        # 2026.9.1 has a real converter for it - measured, not assumed:
        # `blood_glucose_concentration` accepts mg/dL and mmol/L - so a person
        # who reads in mmol/L gets that conversion from Home Assistant rather
        # than from a second wire format.
        #
        # The receiver transports what Apple Health already holds and stops
        # there. No reference range, no high/low classification, no device or
        # CGM integration, and nothing that could be read as a clinical
        # statement. A number and its unit.
        _discrete("blood_glucose", "blood_glucose", "Blood glucose", "mg/dL",
                  "blood_glucose_concentration", BucketKind.HOURLY_DISCRETE),
    )
}

#: The two move modes, as stable wire strings.
#:
#: `HKActivitySummary.activityMoveMode` decides which series *is* the Move ring.
#: Sent as a string rather than Apple's numeric enum so the wire does not depend
#: on a platform constant, and closed so an unrecognised value is a rejection
#: rather than a silent fallback to a guessed default.
ACTIVITY_MOVE_MODES: Final = ("active_energy", "move_time")

#: The workout vocabulary, deliberately small and closed.
#:
#: HealthKit has 84 activity types; carrying them all would mean 84 German
#: labels for sports nobody logs. The client maps to these and anything it does
#: not recognise becomes `other`, so a future Apple activity type never costs a
#: workout - and an identifier outside this set is rejected rather than stored,
#: keeping the taxonomy intentional rather than accumulating whatever arrives.
WORKOUT_ACTIVITIES: Final = (
    "walking", "running", "cycling", "strength_training", "functional_strength",
    "hiit", "hiking", "swimming", "rowing", "elliptical", "yoga", "other",
)

#: The measurement weight behind the blood-pressure hourly means.
#:
#: Derived, not a wire metric: the client sends it as the `count` field already
#: allowed on an hourly discrete bucket, and the receiver fans it out into this
#: series. It is deliberately absent from `METRICS` so it can never arrive as a
#: bucket of its own, which would let a count exist without the pair it weights.
#:
#: One shared series for both halves rather than one each. They come from the
#: same correlations by construction, so a single count is the fact; two would
#: be two copies of it that could disagree.
#:
#: It exists because Home Assistant's arithmetic rollup is an unweighted
#: mean-of-hourly-means - proven against a real recorder - so an hour holding
#: three readings would otherwise count for no more than an hour holding one.
BLOOD_PRESSURE_COUNT: Final = MetricSpec(
    metric="blood_pressure_count",
    kind=BucketKind.HOURLY_DISCRETE,
    statistic_suffix="blood_pressure_count",
    name="Blood pressure measurements",
    unit="measurements",
    unit_class=None,
    mean_type=MeanType.ARITHMETIC,
    has_sum=False,
    required=frozenset({"mean"}),
    snapshot_key="",
)

#: The two halves a blood-pressure hour must always carry together.
BLOOD_PRESSURE_METRICS: Final = ("blood_pressure_systolic", "blood_pressure_diastolic")

#: Percent-valued metrics are range-checked so a 0.0-1.0 HealthKit fraction can
#: never be stored as if it were already a percentage.
PERCENT_METRICS: Final = frozenset({"oxygen_saturation", "body_fat_percentage"})
PERCENT_MIN: Final = 1.0
PERCENT_MAX: Final = 100.0


# --- Sleep -------------------------------------------------------------------
#
# Sleep is the one family the client must aggregate itself: HKStatisticsQuery is
# quantity-only and sleepAnalysis is a *category* type, so HealthKit performs
# neither the bucketing nor the cross-source de-duplication it performs for every
# other metric here. The receiver therefore accepts an already-aggregated nightly
# summary and never sees a raw sleep segment - no raw segments are stored.
#
# One nightly record fans out into several durable series, listed below by the
# nightly field that feeds them.

#: Durations that may legitimately be absent. Absence means "not measured" and
#: must never become zero: a night tracked by iPhone alone has a real total with
#: no staging at all, and coercing that to 0 would drag every stage average down.
SLEEP_NULLABLE_FIELDS: Final = frozenset(
    {"rem_min", "core_min", "deep_min", "awake_min"}
)

#: Local hour that the nightly timing offsets are measured from. Clock times are
#: stored as minutes after this hour on the evening before the wake date, because
#: averaging clock times across midnight is otherwise wrong - 23:30 and 00:30
#: average to 12:00, not to 00:00. No plausible bedtime wraps an 18:00 anchor.
SLEEP_OFFSET_ANCHOR_HOUR: Final = 18

#: Gap that splits one night into two sessions, in seconds. A product rule rather
#: than a derived constant: 3 hours keeps a normal wake-up inside one night while
#: still separating genuinely distinct sleeps. Documented here so it stays easy
#: to change - protocol/payload-v4.md section 6.
SLEEP_SESSION_MERGE_GAP_SECONDS: Final = 3 * 3600


def _sleep_series(
    suffix: str, name: str, unit: str = "min", unit_class: str | None = "duration"
) -> MetricSpec:
    """One durable nightly sleep series.

    Mean rather than sum: the meaningful rollup for sleep is "average per night",
    so Home Assistant's own week and month aggregation gives the trend directly.
    Steps are the opposite case, which is why they are cumulative.
    """
    return MetricSpec(
        metric=suffix,
        kind=BucketKind.NIGHTLY_SLEEP,
        statistic_suffix=suffix,
        name=name,
        unit=unit,
        unit_class=unit_class,
        mean_type=MeanType.ARITHMETIC,
        has_sum=False,
        required=frozenset({"mean"}),
    )


#: Nightly field -> durable series. A field whose value is None writes no row at
#: all for that night, which is what keeps null distinguishable from zero in the
#: stored statistics rather than only in the payload.
SLEEP_SERIES: Final[dict[str, MetricSpec]] = {
    "total_sleep_min": _sleep_series("sleep_total", "Sleep duration"),
    "rem_min": _sleep_series("sleep_rem", "REM sleep"),
    "core_min": _sleep_series("sleep_core", "Core sleep"),
    "deep_min": _sleep_series("sleep_deep", "Deep sleep"),
    "awake_min": _sleep_series("sleep_awake", "Awake during sleep"),
    "sleep_start_offset_min": _sleep_series("sleep_start_offset", "Bedtime offset"),
    "wake_offset_min": _sleep_series("sleep_wake_offset", "Wake time offset"),
}

#: Every statistic id this integration writes, and the one place a duplicate
#: writer would show up. Two paths writing one id within a request would be
#: last-write-wins and therefore nondeterministic, so the tests assert that the
#: metric ids and the nightly sleep series stay disjoint.
STATISTIC_IDS: Final = frozenset(
    [spec.statistic_id for spec in METRICS.values()]
    + [spec.statistic_id for spec in SLEEP_SERIES.values()]
    + [BLOOD_PRESSURE_COUNT.statistic_id]
)


def spec_for(metric: str) -> MetricSpec | None:
    """Registry lookup. ``None`` means unknown."""
    return METRICS.get(metric)


#: Every metric id this receiver can store, reported to the client so it can
#: avoid sending anything this version does not know.
#:
#: The client is the one that has to act on this: an iOS update reaches a phone
#: long before a HACS update reaches the instance behind it, so "new client, old
#: receiver" is the normal rollout state rather than an edge case. Without this
#: list the client has no way to find out, and a single unrecognised metric used
#: to cost the whole delivery.
SUPPORTED_METRICS: Final[tuple[str, ...]] = tuple(sorted(METRICS))

#: Additive wire features beyond the metric registry.
#:
#: A metric list cannot describe everything a future client might add. A
#: structured snapshot object - the planned ``activity`` block is the immediate
#: case - is not a metric, and an older receiver ignores unknown snapshot keys
#: *silently*, so the client would believe it had transmitted something that was
#: quietly discarded. Naming these features explicitly is what lets the client
#: tell "understood" from "silently dropped".
#:
#: Everything listed here shipped in v4 and is therefore safe for any receiver
#: that reports the list at all. Later features are appended, never renamed.
SUPPORTED_FEATURES: Final[tuple[str, ...]] = (
    "buckets.nightly",
    "snapshot.activity",
    "snapshot.blood_pressure",
    "snapshot.last_workout",
    "snapshot.sleep_trend",
)

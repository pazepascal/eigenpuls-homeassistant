"""In-memory current-value state behind the sensors.

Home Assistant imports are deliberately absent so this logic is unit-testable
without a running instance.

Phase 1 keeps only current values. The exact-history SQLite store and long-term
statistics arrive with a later phase; until
then a deletion can only be applied to the value currently on display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .payload import (
    ACTIVITY_SNAPSHOT_FIELDS,
    BloodPressureSnapshot,
    DailyTotalSnapshot,
    LastWorkout,
    MeasurementSnapshot,
    NightlySleep,
    ParsedPayload,
    SleepTrend,
    Snapshot,
    WorkoutCategories,
)


@dataclass(slots=True)
class HealthState:
    """Current values surfaced by the three Phase 1 sensors."""

    heart_rate: float | None = None
    heart_rate_uuid: str | None = None
    heart_rate_at: datetime | None = None
    heart_rate_source: str | None = None

    steps: float | None = None
    steps_day: date | None = None
    steps_time_zone: str | None = None

    # v4. Keyed by metric so a new metric needs no new field here: the sensor
    # descriptions read them by key, and the registry decides which is which.
    #: Last displayed value per sensor key, repopulated from Home Assistant's
    #: own restored states after a restart. Read only when live data is absent,
    #: so a real reading always wins over a remembered one.
    restored: dict[str, object] = field(default_factory=dict)

    measurements: dict[str, MeasurementSnapshot] = field(default_factory=dict)
    daily_totals: dict[str, DailyTotalSnapshot] = field(default_factory=dict)
    sleep: NightlySleep | None = None
    sleep_trend: SleepTrend | None = None
    #: One correlated reading, never assembled from two independent halves.
    blood_pressure: BloodPressureSnapshot | None = None

    #: Latest activity ring values, keyed by metric id, **merged** rather than
    #: replaced. A field absent from a snapshot leaves its previous value alone;
    #: an explicit 0.0 overwrites. Those are different facts - one day in a
    #: measured week had no summary at all while another had every ring at zero -
    #: and whole-object replacement would collapse them into the same thing.
    activity_values: dict[str, float] = field(default_factory=dict)
    #: Which series is the Move ring, from the last activity snapshot received.
    activity_move_mode: str | None = None
    activity_day: date | None = None
    activity_time_zone: str | None = None
    last_workout: LastWorkout | None = None
    #: Training by category over the rolling window. Replaced wholesale rather
    #: than merged, unlike the activity values above, and the difference is the
    #: point: this is a complete recomputation over a fixed window every sync, so
    #: a category that drops out of the window has to disappear. Merging would
    #: leave a sport nobody has done since spring on the dashboard for ever.
    workout_categories: WorkoutCategories | None = None
    #: Measurement-weighted rolling averages, derived by Home Assistant from its
    #: own durable history rather than sent by the phone. Keyed by period in days.
    blood_pressure_trends: dict[int, object] = field(default_factory=dict)

    last_sync: datetime | None = None

    def apply_snapshot(self, snapshot: Snapshot, *, received_at: datetime) -> None:
        """Apply a v2 completion snapshot by replacement.

        The snapshot is read directly from HealthKit on the device at completion
        time - it is not reconstructed from the historical sample stream. That is
        what makes current-value correctness independent of how many batches were
        accepted, and therefore independent of a Home Assistant restart partway
        through a backfill.

        Replacement (not accumulation) is what makes a retried completion
        idempotent. A member that is ``None`` leaves that sensor untouched;
        absence is not a measurement.
        """
        if (heart_rate := snapshot.heart_rate) is not None:
            self.heart_rate = heart_rate.value
            self.heart_rate_at = heart_rate.measured_at
            self.heart_rate_source = heart_rate.source
            # v2 does not carry a sample UUID: the snapshot is re-read every
            # sync, so a deleted newest sample simply drops out of the next one.
            self.heart_rate_uuid = None

        if (steps := snapshot.steps_today) is not None:
            self.steps = steps.value
            self.steps_day = steps.day
            self.steps_time_zone = steps.time_zone

        # Merged rather than assigned: a metric absent from this snapshot leaves
        # its sensor untouched, exactly as an absent heart rate does. HealthKit
        # cannot distinguish a denied read from no data, so clearing on absence
        # would turn a permissions state into a false measurement.
        self.measurements.update(snapshot.measurements)
        self.daily_totals.update(snapshot.daily_totals)
        if snapshot.sleep_last_night is not None:
            self.sleep = snapshot.sleep_last_night
        if snapshot.sleep_7d is not None:
            self.sleep_trend = snapshot.sleep_7d
        if snapshot.blood_pressure is not None:
            self.blood_pressure = snapshot.blood_pressure
        if snapshot.last_workout is not None:
            self.last_workout = snapshot.last_workout
        # Absent still means untouched: an older client, or the workouts source
        # switched off, must not clear what is displayed.
        if snapshot.workout_categories is not None:
            self.workout_categories = snapshot.workout_categories

        if (activity := snapshot.activity) is not None:
            self.activity_move_mode = activity.move_mode
            self.activity_day = activity.day
            self.activity_time_zone = activity.time_zone
            # Field by field, not by assignment. A value the snapshot did not
            # carry is not a value of zero, and must not clear what is shown.
            for field_name, metric in ACTIVITY_SNAPSHOT_FIELDS.items():
                value = getattr(activity, field_name)
                if value is not None:
                    self.activity_values[metric] = value

        self.last_sync = received_at

    def apply(self, payload: ParsedPayload, *, received_at: datetime) -> None:
        """Fold a **v1** payload into the current state.

        Retained for wire-format v1 clients, where every delivery is a complete
        sync. v2 clients drive the current values through
        :meth:`apply_snapshot` instead and never reach this path.
        

        No change flag is returned: ``last_sync`` advances on every accepted
        delivery, so a state write is always warranted.
        """

        # Discrete: keep the most recent reading by sample end time. Re-sending an
        # older sample must never move the current value backwards.
        for sample in payload.samples:
            if sample.metric != "heart_rate":
                continue
            if self.heart_rate_at is not None and sample.end <= self.heart_rate_at:
                continue
            self.heart_rate = sample.value
            self.heart_rate_uuid = sample.uuid
            self.heart_rate_at = sample.end
            self.heart_rate_source = sample.source

        # Cumulative: upsert - replace the value for a day, never accumulate.
        # Only advance to a newer day; a late look-back total for an older day
        # must not overwrite today's figure.
        for total in payload.daily_totals:
            if total.metric != "steps":
                continue
            if self.steps_day is not None and total.day < self.steps_day:
                continue
            if (
                total.day == self.steps_day
                and total.value == self.steps
                and total.time_zone == self.steps_time_zone
            ):
                continue
            self.steps = total.value
            self.steps_day = total.day
            self.steps_time_zone = total.time_zone

        # Deletions. With no history store yet, the only thing that can be applied
        # is clearing a value that is currently on display.
        if self.heart_rate_uuid is not None and self.heart_rate_uuid in payload.deletions:
            self.heart_rate = None
            self.heart_rate_uuid = None
            self.heart_rate_at = None
            self.heart_rate_source = None

        self.last_sync = received_at

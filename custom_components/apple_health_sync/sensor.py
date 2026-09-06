"""Sensor platform.

Current values only. The durable history lives in long-term statistics
(``statistics.py``) and is never rebuilt from these entities.

Units and identifiers come from ``registry.py`` rather than being repeated here,
so a metric cannot be stored under one unit and displayed under another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import AppleHealthSyncConfigEntry, registry
from .entity import AppleHealthSyncEntity
from .payload import BloodPressureSnapshot, LastWorkout
from .registry import SLEEP_OFFSET_ANCHOR_HOUR
from .state import HealthState

# Nothing here reaches out to a device or a service; state arrives by push.
PARALLEL_UPDATES = 0


def _restore_heart_rate(state: HealthState, last: State) -> None:
    if state.heart_rate is not None:
        return
    try:
        state.heart_rate = float(last.state)
    except (TypeError, ValueError):
        return
    if measured := last.attributes.get("measured_at"):
        state.heart_rate_at = dt_util.parse_datetime(measured)
    state.heart_rate_source = last.attributes.get("source")


def _restore_steps(state: HealthState, last: State) -> None:
    if state.steps is not None:
        return
    try:
        state.steps = float(last.state)
    except (TypeError, ValueError):
        return
    if day := last.attributes.get("day"):
        try:
            state.steps_day = date.fromisoformat(day)
        except ValueError:
            state.steps_day = None
    state.steps_time_zone = last.attributes.get("time_zone")


def _restore_last_sync(state: HealthState, last: State) -> None:
    if state.last_sync is None:
        state.last_sync = dt_util.parse_datetime(last.state)


# The v4 sensors restore their displayed value into a small cache rather than
# reconstructing a partial domain object. A NightlySleep rebuilt from one entity
# would be missing its date, zone and instants, and a half-built night is worse
# than a remembered number: the cache is only ever read when live data is absent.


def _restore_number(key: str) -> Callable[[HealthState, State], None]:
    def restore(state: HealthState, last: State) -> None:
        try:
            state.restored[key] = float(last.state)
        except (TypeError, ValueError):
            return

    return restore


def _restore_blood_pressure(state: HealthState, last: State) -> None:
    """Restore the pair or nothing at all.

    Both halves come back together or the sensor stays unknown until the next
    sync delivers a complete measurement - half a reading is never displayed.
    Read from the attributes rather than the state, which is now the formatted
    pair rather than a bare number.
    """
    if state.blood_pressure is not None:
        return
    systolic_attr = last.attributes.get("systolic")
    diastolic = last.attributes.get("diastolic")
    measured = last.attributes.get("measured_at")
    if systolic_attr is None or diastolic is None or measured is None:
        return
    parsed = dt_util.parse_datetime(measured)
    if parsed is None:
        return
    try:
        # From the attribute, not the state: the state is now the formatted pair.
        systolic = float(systolic_attr)
        state.blood_pressure = BloodPressureSnapshot(
            systolic=systolic,
            diastolic=float(diastolic),
            unit=registry.METRICS["blood_pressure_systolic"].unit,
            measured_at=parsed,
            source=last.attributes.get("source"),
        )
    except (TypeError, ValueError):
        return


def _restore_timestamp(key: str) -> Callable[[HealthState, State], None]:
    def restore(state: HealthState, last: State) -> None:
        if (parsed := dt_util.parse_datetime(last.state)) is not None:
            state.restored[key] = parsed

    return restore


def _or_restored(
    key: str, live: Callable[[HealthState], StateType | datetime]
) -> Callable[[HealthState], StateType | datetime]:
    def value(state: HealthState) -> StateType | datetime:
        current = live(state)
        return current if current is not None else state.restored.get(key)

    return value


def _measurement(metric: str) -> Callable[[HealthState], StateType]:
    def value(state: HealthState) -> StateType:
        entry = state.measurements.get(metric)
        return entry.value if entry else None

    return value


def _measurement_attrs(metric: str) -> Callable[[HealthState], dict[str, Any]]:
    def attrs(state: HealthState) -> dict[str, Any]:
        entry = state.measurements.get(metric)
        return {
            "measured_at": entry.measured_at.isoformat() if entry else None,
            "source": entry.source if entry else None,
        }

    return attrs


def _daily_total(metric: str) -> Callable[[HealthState], StateType]:
    def value(state: HealthState) -> StateType:
        entry = state.daily_totals.get(metric)
        return entry.value if entry else None

    return value


def _daily_total_attrs(metric: str) -> Callable[[HealthState], dict[str, Any]]:
    def attrs(state: HealthState) -> dict[str, Any]:
        entry = state.daily_totals.get(metric)
        return {
            "day": entry.day.isoformat() if entry else None,
            "time_zone": entry.time_zone if entry else None,
        }

    return attrs


def _night(field_name: str) -> Callable[[HealthState], StateType | datetime]:
    def value(state: HealthState) -> StateType | datetime:
        return getattr(state.sleep, field_name) if state.sleep else None

    return value


def _trend(field_name: str) -> Callable[[HealthState], StateType]:
    def value(state: HealthState) -> StateType:
        return getattr(state.sleep_trend, field_name) if state.sleep_trend else None

    return value


#: The unit the blood-pressure entities render, carried inside the state.
#:
#: It cannot be `native_unit_of_measurement`: Home Assistant's
#: `_numeric_state_expected` returns True whenever a unit, a state class or a
#: display precision is set, and then rejects any value it cannot parse as a
#: number. A formatted pair is not a number, so declaring the unit the ordinary
#: way left all three entities Unknown. The unit therefore lives in the state
#: text and, machine-readably, in a `unit` attribute.
BLOOD_PRESSURE_UNIT: Final = "mmHg"


def _workout_attributes(workout: LastWorkout | None) -> dict[str, Any]:
    """The structured detail behind the last workout.

    The HealthKit uuid is deliberately absent: it exists for identity on the
    device, not for reading, and nothing in Home Assistant needs it.
    """
    return {
        "activity": workout.activity if workout else None,
        "started_at": workout.start.isoformat() if workout else None,
        "ended_at": workout.end.isoformat() if workout else None,
        # HealthKit's pause-aware duration, not the span between the two.
        "duration_min": workout.duration_min if workout else None,
        # Absent stays absent: a strength session records no distance and an
        # unworn watch records no heart rate.
        "active_energy_kcal": workout.active_energy_kcal if workout else None,
        "distance_km": workout.distance_km if workout else None,
        "average_heart_rate_bpm": workout.avg_heart_rate_bpm if workout else None,
        "maximum_heart_rate_bpm": workout.max_heart_rate_bpm if workout else None,
        "source": workout.source if workout else None,
    }


def _restore_move_mode(state: HealthState, last: State) -> None:
    """Restore the move mode, but only a value that is still in the vocabulary."""
    if last.state in registry.ACTIVITY_MOVE_MODES:
        state.activity_move_mode = last.state


def _restore_workout(state: HealthState, last: State) -> None:
    """Bring back the last workout's activity and detail after a restart."""
    if state.last_workout is not None:
        return
    started = last.attributes.get("started_at")
    ended = last.attributes.get("ended_at")
    duration = last.attributes.get("duration_min")
    if (
        last.state not in registry.WORKOUT_ACTIVITIES
        or started is None or ended is None or duration is None
    ):
        return
    start, end = dt_util.parse_datetime(started), dt_util.parse_datetime(ended)
    if start is None or end is None:
        return
    state.last_workout = LastWorkout(
        # The identity is not restored - it is not displayed and a restored
        # summary is only ever replaced wholesale by the next sync.
        uuid="",
        activity=last.state,
        start=start,
        end=end,
        duration_min=float(duration),
        active_energy_kcal=last.attributes.get("active_energy_kcal"),
        distance_km=last.attributes.get("distance_km"),
        avg_heart_rate_bpm=last.attributes.get("average_heart_rate_bpm"),
        max_heart_rate_bpm=last.attributes.get("maximum_heart_rate_bpm"),
        source=last.attributes.get("source"),
    )


def _pressure_pair(systolic: float | None, diastolic: float | None) -> str | None:
    """The conventional way a blood pressure is written and read.

    A blood pressure is one fact with two numbers; showing only the systolic
    reads as half a measurement. Home Assistant gives a sensor a single state, so
    the pair itself is the state - unit included, since it cannot be declared
    separately without demanding a numeric value.

    Whole millimetres, because that is how a cuff reports and how a person reads
    it - the unrounded figures stay available as attributes and the durable
    statistics are untouched.

    Nil unless *both* halves are present: half a pair is never displayed.
    """
    if systolic is None or diastolic is None:
        return None
    return f"{round(systolic)} / {round(diastolic)} {BLOOD_PRESSURE_UNIT}"


def _offset_clock(minutes: float | None) -> str | None:
    """Render an offset from the anchor hour back into a readable clock time."""
    if minutes is None:
        return None
    total = round(SLEEP_OFFSET_ANCHOR_HOUR * 60 + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


#: How many decimals each numeric entity *displays*.
#:
#: Presentation only. `suggested_display_precision` never touches what is
#: stored - the long-term statistics keep full precision, and so do the
#: attributes. Without it Home Assistant renders the raw float, which is how a
#: heart-rate variability of 14.0626384174924 ms reached the dashboard.
#:
#: Deliberately *not* in `registry.py`: that module is the wire contract, and
#: how many decimals a dashboard shows is not something the protocol knows or
#: cares about.
#:
#: The three blood-pressure entities are absent by design, not by omission.
#: Their state is the pair string "128 / 82 mmHg"; setting a precision on one
#: makes `_numeric_state_expected` return True, Home Assistant then demands a
#: number, and all three go Unknown. That regression shipped once already
#: (cd6ea3d) - `test_v4_precision.py` now fails if any of them appears here.
DISPLAY_PRECISION: Final[dict[str, int]] = {
    # Activity. Energies and minutes to whole units - Apple's own rings are
    # whole numbers, and a decimal place would imply a precision the summary
    # does not have. Stand hours likewise: a fraction of a qualifying hour is
    # not a thing.
    "activity_move_energy": 0,
    "activity_move_energy_goal": 0,
    "activity_move_time": 0,
    "activity_move_time_goal": 0,
    "activity_exercise_time": 0,
    "activity_exercise_goal": 0,
    "activity_stand_hours": 0,
    "activity_stand_goal": 0,

    # Whole units: Apple Health itself shows no decimals for these.
    "heart_rate": 0,
    "resting_heart_rate": 0,
    "hrv_sdnn": 0,
    "oxygen_saturation": 0,
    "steps_today": 0,
    "active_energy_today": 0,
    # One decimal: the step that actually carries meaning.
    "respiratory_rate": 1,
    "body_mass": 1,
    "body_fat": 1,
    # One decimal is what Apple Health itself shows for cardio fitness.
    "vo2_max": 1,
    # Two decimals: 10-metre resolution.
    "distance_today": 2,
    # Whole minutes for every duration.
    "sleep_total": 0,
    "sleep_rem": 0,
    "sleep_core": 0,
    "sleep_deep": 0,
    "sleep_awake": 0,
    "nap_total": 0,
    "sleep_7d_total": 0,
    "sleep_7d_rem": 0,
    "sleep_7d_deep": 0,
    "sleep_7d_bedtime": 0,
    "sleep_7d_consistency": 0,
}


def _metric_sensor(
    metric: str,
    *,
    key: str,
    state_class: SensorStateClass,
    device_class: SensorDeviceClass | None = None,
    cumulative: bool = False,
) -> AppleHealthSensorEntityDescription:
    """One current-value sensor, with its unit taken from the registry."""
    spec = registry.METRICS[metric]
    live = _daily_total(metric) if cumulative else _measurement(metric)
    return AppleHealthSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=spec.unit,
        suggested_display_precision=DISPLAY_PRECISION.get(key),
        device_class=device_class,
        state_class=state_class,
        value_fn=_or_restored(key, live),
        attrs_fn=_daily_total_attrs(metric) if cumulative else _measurement_attrs(metric),
        restore_fn=_restore_number(key),
    )


def _activity(metric: str) -> Callable[[HealthState], StateType]:
    """Current value of one activity ring metric.

    Reads the merged dict rather than a snapshot object, which is what makes an
    absent field leave the previous value in place while an explicit zero
    replaces it.
    """
    def value(state: HealthState) -> StateType:
        return state.activity_values.get(metric)

    return value


def _activity_sensor(
    metric: str,
    *,
    device_class: SensorDeviceClass | None,
    state_class: SensorStateClass,
) -> AppleHealthSensorEntityDescription:
    """One activity current-value sensor, unit taken from the registry."""
    spec = registry.METRICS[metric]
    return AppleHealthSensorEntityDescription(
        key=metric,
        translation_key=metric,
        native_unit_of_measurement=spec.unit,
        suggested_display_precision=DISPLAY_PRECISION.get(metric),
        device_class=device_class,
        state_class=state_class,
        value_fn=_or_restored(metric, _activity(metric)),
        attrs_fn=lambda state: {
            "date": state.activity_day.isoformat() if state.activity_day else None,
            "move_mode": state.activity_move_mode,
        },
        restore_fn=_restore_number(metric),
    )


def _blood_pressure_trend(days: int) -> AppleHealthSensorEntityDescription:
    """One rolling measurement-weighted blood-pressure average.

    A pair, like the current reading: the state is systolic and diastolic rides
    as an attribute, because half an average is not a blood pressure any more
    than half a reading is. `measurement_count` says how many real measurements
    stand behind it, which is what separates "128/72 from two readings" from
    "128/72 from twenty-five".

    Whole millimetres are shown while the stored figures keep full precision -
    a mean of 127.84 is displayed as 128 but never rounded in the statistics.
    """

    def trend(state: HealthState):
        return state.blood_pressure_trends.get(days)

    return AppleHealthSensorEntityDescription(
        key=f"blood_pressure_{days}d",
        translation_key=f"blood_pressure_{days}d",
        # No unit, state class, device class or display precision, for the same
        # reason as the current reading: any one of them makes Home Assistant
        # demand a number, and the state is a pair.
        value_fn=lambda state: _pressure_pair(
            getattr(trend(state), "systolic", None),
            getattr(trend(state), "diastolic", None),
        ),
        attrs_fn=lambda state: {
            # Full precision, unrounded: the state rounds for reading, the
            # attributes stay exact for automations and downstream consumers.
            "systolic": getattr(trend(state), "systolic", None),
            "diastolic": getattr(trend(state), "diastolic", None),
            "period_days": days,
            "measurement_count": getattr(trend(state), "measurements", None),
            "unit": BLOOD_PRESSURE_UNIT,
        },
        # Deliberately no restore: the trend is derived from durable statistics
        # and is recomputed on the next sync. Restoring a stale average with a
        # stale measurement count would present it as current.
        restore_fn=lambda state, last: None,
    )


def _sleep_duration(key: str, field_name: str) -> AppleHealthSensorEntityDescription:
    """A nightly duration in minutes.

    Minutes are the canonical representation end to end; Home Assistant renders a
    duration sensor readably on its own, so nothing converts to hours here.
    """
    return AppleHealthSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION.get(key),
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_or_restored(key, _night(field_name)),
        restore_fn=_restore_number(key),
    )


def _trend_duration(key: str, field_name: str) -> AppleHealthSensorEntityDescription:
    return AppleHealthSensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION.get(key),
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_or_restored(key, _trend(field_name)),
        attrs_fn=lambda state: {
            "nights": state.sleep_trend.nights if state.sleep_trend else None,
            "nights_contributing": (
                state.sleep_trend.nights_by_field.get(field_name)
                if state.sleep_trend
                else None
            ),
        },
        restore_fn=_restore_number(key),
    )


@dataclass(frozen=True, kw_only=True)
class AppleHealthSensorEntityDescription(SensorEntityDescription):
    """Describes one Apple Health Sync sensor."""

    value_fn: Callable[[HealthState], StateType | datetime]
    attrs_fn: Callable[[HealthState], dict[str, Any]] | None = None
    restore_fn: Callable[[HealthState, State], None]


SENSORS: tuple[AppleHealthSensorEntityDescription, ...] = (
    AppleHealthSensorEntityDescription(
        key="heart_rate",
        translation_key="heart_rate",
        native_unit_of_measurement="bpm",
        suggested_display_precision=DISPLAY_PRECISION["heart_rate"],
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.heart_rate,
        attrs_fn=lambda state: {
            "measured_at": state.heart_rate_at.isoformat() if state.heart_rate_at else None,
            "source": state.heart_rate_source,
        },
        restore_fn=_restore_heart_rate,
    ),
    AppleHealthSensorEntityDescription(
        key="steps_today",
        translation_key="steps_today",
        native_unit_of_measurement="steps",
        suggested_display_precision=DISPLAY_PRECISION["steps_today"],
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda state: state.steps,
        attrs_fn=lambda state: {
            # The day the total belongs to. With Phase 1's manual-only sync this
            # can lag the actual calendar day until the next Sync Now.
            "day": state.steps_day.isoformat() if state.steps_day else None,
            "time_zone": state.steps_time_zone,
        },
        restore_fn=_restore_steps,
    ),
    AppleHealthSensorEntityDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: state.last_sync,
        restore_fn=_restore_last_sync,
    ),
    # --- Phase 3A current values ---------------------------------------------
    _metric_sensor(
        "resting_heart_rate", key="resting_heart_rate",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # No duration device class: HRV is a time measure but presenting it as a
    # convertible duration would invite a display in seconds, and SDNN is only
    # ever read in milliseconds.
    _metric_sensor(
        "hrv_sdnn", key="hrv_sdnn", state_class=SensorStateClass.MEASUREMENT,
    ),
    _metric_sensor(
        "respiratory_rate", key="respiratory_rate",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # Percent, already converted from HealthKit's 0.0-1.0 fraction by the client
    # and range-checked by the receiver. There is no blood-oxygen device class.
    _metric_sensor(
        "oxygen_saturation", key="oxygen_saturation",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _metric_sensor(
        "active_energy", key="active_energy_today", cumulative=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    _metric_sensor(
        "distance_walking_running", key="distance_today", cumulative=True,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # --- Phase 3C: cardio fitness ---
    #
    # No device class: Home Assistant has none for VO2 max, and the unit is one
    # its converters do not know, so there is nothing to convert to and no
    # conversion menu worth offering.
    _metric_sensor(
        "vo2_max", key="vo2_max", state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- Phase 3B.1: body composition and blood pressure ---------------------
    _metric_sensor(
        "body_mass", key="body_mass",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # No device class: the only ones Home Assistant offers for "%" are battery,
    # humidity, moisture and power factor, none of which is body fat. Percent is
    # already the stored unit, and a wrong class would only add a misleading
    # conversion menu.
    _metric_sensor(
        "body_fat_percentage", key="body_fat",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # One entity for the pair. The state is systolic and diastolic rides as an
    # attribute, because a lone half is not a blood-pressure reading and two
    # separate entities could drift onto different measurements.
    #
    # No device class either: Home Assistant's `pressure` class means atmospheric
    # or gas pressure and would offer conversion to hPa or bar, which is
    # meaningless here. The mmHg unit and the `pressure` unit_class on the
    # statistics are correct on their own.
    AppleHealthSensorEntityDescription(
        key="blood_pressure",
        translation_key="blood_pressure",
        # No unit, state class, device class or display precision: any one of
        # them makes Home Assistant demand a numeric value, and the state is a
        # pair. Nothing is lost - the durable history is the external
        # apple_health_sync:blood_pressure_* series, which stores both halves,
        # where an entity statistic could only ever have recorded the systolic
        # half of each reading.
        value_fn=lambda state: _pressure_pair(
            state.blood_pressure.systolic if state.blood_pressure else None,
            state.blood_pressure.diastolic if state.blood_pressure else None,
        ),
        attrs_fn=lambda state: {
            # Never synthesised: both halves come from one correlated reading or
            # neither is here at all. Kept unrounded and machine-readable
            # alongside the human-readable state.
            "systolic": (
                state.blood_pressure.systolic if state.blood_pressure else None
            ),
            "diastolic": (
                state.blood_pressure.diastolic if state.blood_pressure else None
            ),
            "measured_at": (
                state.blood_pressure.measured_at.isoformat()
                if state.blood_pressure else None
            ),
            "source": state.blood_pressure.source if state.blood_pressure else None,
            "unit": BLOOD_PRESSURE_UNIT,
        },
        restore_fn=_restore_blood_pressure,
    ),
    # Rolling measurement-weighted averages, derived by Home Assistant from its
    # own durable history. Every real measurement counts once - neither hours nor
    # calendar days carry weight of their own.
    _blood_pressure_trend(7),
    _blood_pressure_trend(30),
    # --- Training ------------------------------------------------------------
    #
    # One entity, not one per activity. The state is the English activity
    # identifier and Home Assistant renders the German label from the enum state
    # translations, so the stored value stays stable while the display is
    # localised - the same split the entity ids already use.
    AppleHealthSensorEntityDescription(
        key="last_workout",
        translation_key="last_workout",
        device_class=SensorDeviceClass.ENUM,
        options=list(registry.WORKOUT_ACTIVITIES),
        value_fn=lambda state: (
            state.last_workout.activity if state.last_workout else None
        ),
        attrs_fn=lambda state: _workout_attributes(state.last_workout),
        # Restored so the last session survives a restart rather than reading
        # unknown until the next sync; the value is one of the fixed options.
        restore_fn=_restore_workout,
    ),
    # --- Last night ----------------------------------------------------------
    AppleHealthSensorEntityDescription(
        key="sleep_total",
        translation_key="sleep_total",
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION["sleep_total"],
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_or_restored("sleep_total", _night("total_sleep_min")),
        attrs_fn=lambda state: {
            "date": state.sleep.day.isoformat() if state.sleep else None,
            "time_zone": state.sleep.time_zone if state.sleep else None,
            # Null here means the stage was not measured. It is deliberately not
            # rendered as 0: a night tracked without a Watch has a real total and
            # no staging at all.
            "rem_min": state.sleep.rem_min if state.sleep else None,
            "core_min": state.sleep.core_min if state.sleep else None,
            "deep_min": state.sleep.deep_min if state.sleep else None,
            "awake_min": state.sleep.awake_min if state.sleep else None,
        },
        restore_fn=_restore_number("sleep_total"),
    ),
    _sleep_duration("sleep_rem", "rem_min"),
    _sleep_duration("sleep_core", "core_min"),
    _sleep_duration("sleep_deep", "deep_min"),
    _sleep_duration("sleep_awake", "awake_min"),
    AppleHealthSensorEntityDescription(
        key="sleep_start",
        translation_key="sleep_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_or_restored("sleep_start", _night("sleep_start")),
        restore_fn=_restore_timestamp("sleep_start"),
    ),
    AppleHealthSensorEntityDescription(
        key="sleep_wake",
        translation_key="sleep_wake",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_or_restored("sleep_wake", _night("wake_time")),
        restore_fn=_restore_timestamp("sleep_wake"),
    ),
    # Naps are a daily metric in their own right, not part of the night: the
    # value comes from the generic metric path so it exists on a day with no
    # main sleep at all. The entity key is unchanged, so the entity_id is too.
    AppleHealthSensorEntityDescription(
        key="nap_total",
        translation_key="nap_total",
        native_unit_of_measurement=registry.METRICS["nap_total"].unit,
        suggested_display_precision=DISPLAY_PRECISION["nap_total"],
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_or_restored("nap_total", _measurement("nap_total")),
        attrs_fn=lambda state: {
            "nap_count": (
                state.measurements["nap_count"].value
                if "nap_count" in state.measurements
                else None
            ),
            "date": (
                state.measurements["nap_total"].measured_at.date().isoformat()
                if "nap_total" in state.measurements
                else None
            ),
        },
        restore_fn=_restore_number("nap_total"),
    ),
    # --- Seven-night trend ---------------------------------------------------
    # Derived state recomputed on the device every sync, never a second durable
    # history. Core and awake averages ride as attributes on the total: they are
    # the least actionable of the five and do not each warrant an entity.
    AppleHealthSensorEntityDescription(
        key="sleep_7d_total",
        translation_key="sleep_7d_total",
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION["sleep_7d_total"],
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_or_restored("sleep_7d_total", _trend("avg_total_min")),
        attrs_fn=lambda state: {
            "nights": state.sleep_trend.nights if state.sleep_trend else None,
            "avg_core_min": state.sleep_trend.avg_core_min if state.sleep_trend else None,
            "avg_awake_min": state.sleep_trend.avg_awake_min if state.sleep_trend else None,
            "avg_nap_total_min": (
                state.sleep_trend.avg_nap_total_min if state.sleep_trend else None
            ),
            "avg_wake_time": _offset_clock(
                state.sleep_trend.avg_wake_offset_min if state.sleep_trend else None
            ),
        },
        restore_fn=_restore_number("sleep_7d_total"),
    ),
    _trend_duration("sleep_7d_rem", "avg_rem_min"),
    _trend_duration("sleep_7d_deep", "avg_deep_min"),
    AppleHealthSensorEntityDescription(
        key="sleep_7d_bedtime",
        translation_key="sleep_7d_bedtime",
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION["sleep_7d_bedtime"],
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        # Minutes after the 18:00 anchor rather than a clock time, because a mean
        # bedtime is not an instant and averaging clock times across midnight is
        # wrong. The readable form rides along as an attribute.
        value_fn=_or_restored("sleep_7d_bedtime", _trend("avg_sleep_start_offset_min")),
        attrs_fn=lambda state: {
            "local_time": _offset_clock(
                state.sleep_trend.avg_sleep_start_offset_min if state.sleep_trend else None
            ),
            "nights": state.sleep_trend.nights if state.sleep_trend else None,
        },
        restore_fn=_restore_number("sleep_7d_bedtime"),
    ),
    AppleHealthSensorEntityDescription(
        key="sleep_7d_consistency",
        translation_key="sleep_7d_consistency",
        native_unit_of_measurement="min",
        suggested_display_precision=DISPLAY_PRECISION["sleep_7d_consistency"],
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        # Standard deviation of the bedtime offset: a lower number means a more
        # regular sleep schedule.
        value_fn=_or_restored("sleep_7d_consistency", _trend("sleep_start_stddev_min")),
        restore_fn=_restore_number("sleep_7d_consistency"),
    ),
    # --- Activity rings ------------------------------------------------------
    #
    # Eight values and the mode that says which of them is the Move ring. Every
    # one is driven by the composite `snapshot.activity` rather than by an
    # individual snapshot entry, which is why the registry gives them all an
    # empty snapshot key.
    #
    # `activity_stand_hours` and its goal carry **no** device class. Home
    # Assistant 2026.9.1 has no statistics converter for the unit `hours` - it
    # does for `h` - and that is exactly right here: these count hours that
    # qualified, not time elapsed, and a DURATION class would invite Home
    # Assistant to render nine stand hours as 540 minutes.
    _activity_sensor(
        "activity_move_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    _activity_sensor(
        "activity_move_energy_goal",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _activity_sensor(
        "activity_move_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
    ),
    _activity_sensor(
        "activity_move_time_goal",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _activity_sensor(
        "activity_exercise_time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
    ),
    _activity_sensor(
        "activity_exercise_goal",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _activity_sensor(
        "activity_stand_hours",
        device_class=None,
        state_class=SensorStateClass.TOTAL,
    ),
    _activity_sensor(
        "activity_stand_goal",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # Which series is the Move ring. A closed enum, no long-term statistics -
    # a categorical setting has no meaningful average.
    AppleHealthSensorEntityDescription(
        key="activity_move_mode",
        translation_key="activity_move_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(registry.ACTIVITY_MOVE_MODES),
        value_fn=lambda state: state.activity_move_mode,
        restore_fn=_restore_move_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AppleHealthSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Apple Health Sync sensors."""
    async_add_entities(
        AppleHealthSensor(entry.entry_id, entry.runtime_data.state, description)
        for description in SENSORS
    )


class AppleHealthSensor(AppleHealthSyncEntity, RestoreEntity, SensorEntity):
    """A single current-value sensor backed by the shared health state."""

    entity_description: AppleHealthSensorEntityDescription

    def __init__(
        self,
        entry_id: str,
        state: HealthState,
        description: AppleHealthSensorEntityDescription,
    ) -> None:
        super().__init__(entry_id)
        self._state = state
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_sensor_{description.key}"

    @property
    def suggested_object_id(self) -> str:
        """The stable English basis for this entity's id.

        Home Assistant derives an entity_id from the entity *name*, and German is
        in `NATIVE_ENTITY_IDS` - so on a German instance the translated name would
        become the id, giving `sensor.apple_health_herzfrequenzvariabilitat`. The
        id would then depend on the interface language and change if a
        translation were ever reworded.

        Returning the description key instead pins the id to something technical
        and stable, while `name` continues to come from the translations and can
        be fully German. Home Assistant prefixes this with the device name, so
        the key `hrv_sdnn` yields `sensor.apple_health_hrv_sdnn`.

        The keys of the three original sensors reproduce the ids they already
        have, so this pins existing entities rather than renaming them.
        """
        return self.entity_description.key

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Phase 1 has no history store and no automatic sync, so without this the
        # sensors would read "unknown" after every restart until a manual Sync Now.
        if (last := await self.async_get_last_state()) is not None and last.state not in (
            "unknown",
            "unavailable",
        ):
            self.entity_description.restore_fn(self._state, last)

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self._state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self._state)

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

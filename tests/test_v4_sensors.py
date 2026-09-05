"""The sensor platform is actually built.

Every other test exercises parsing and storage. Without this one, a bad unit, an
invalid device-class pairing or a typo in a value lambda would first surface on
Pascal's own instance, because nothing else imports sensor.py at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor.const import DEVICE_CLASS_UNITS

from custom_components.apple_health_sync import AppleHealthSyncRuntimeData
from custom_components.apple_health_sync.payload import (
    BloodPressureSnapshot,
    DailyTotalSnapshot,
    LastWorkout,
    MeasurementSnapshot,
    NightlySleep,
    SleepTrend,
)
from custom_components.apple_health_sync.registry import METRICS
from custom_components.apple_health_sync.sensor import (
    SENSORS,
    AppleHealthSensor,
    _offset_clock,
)
from custom_components.apple_health_sync.state import HealthState
from custom_components.apple_health_sync.statistics import BloodPressureTrend

TZ = "Europe/Berlin"


def populated_state() -> HealthState:
    state = HealthState()
    state.heart_rate = 61.0
    state.heart_rate_at = datetime(2026, 6, 10, 8, tzinfo=UTC)
    state.steps = 8423.0
    state.steps_day = date(2026, 6, 10)
    state.last_sync = datetime(2026, 6, 10, 9, tzinfo=UTC)

    for metric, value in (
        ("resting_heart_rate", 54.0), ("hrv_sdnn", 44.0),
        ("respiratory_rate", 14.2), ("oxygen_saturation", 96.5),
        # Naps are a daily metric now, not part of the night.
        ("nap_total", 35.0), ("nap_count", 1.0),
        ("body_mass", 81.4), ("body_fat_percentage", 18.2),
        ("vo2_max", 42.7),
    ):
        state.measurements[metric] = MeasurementSnapshot(
            metric=metric, value=value, unit=METRICS[metric].unit,
            measured_at=datetime(2026, 6, 10, 6, tzinfo=UTC), source="Apple Watch",
        )
    for metric, value in (("active_energy", 612.0), ("distance_walking_running", 7.4)):
        state.daily_totals[metric] = DailyTotalSnapshot(
            metric=metric, value=value, unit=METRICS[metric].unit,
            day=date(2026, 6, 10), time_zone=TZ,
        )
    state.last_workout = LastWorkout(
        uuid="F1A2", activity="strength_training",
        start=datetime(2026, 6, 10, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 10, 18, 5, tzinfo=UTC),
        duration_min=58.0, active_energy_kcal=410.0,
        avg_heart_rate_bpm=126.0, max_heart_rate_bpm=158.0, source="Apple Watch",
    )
    state.sleep = NightlySleep(
        day=date(2026, 6, 10), time_zone=TZ, total_sleep_min=431.0,
        sleep_start=datetime(2026, 6, 9, 21, 47, tzinfo=UTC),
        wake_time=datetime(2026, 6, 10, 5, 12, tzinfo=UTC),
        rem_min=92.0, core_min=250.0, deep_min=61.0, awake_min=28.0,
    )
    state.blood_pressure = BloodPressureSnapshot(
        systolic=128.0, diastolic=82.0, unit="mmHg",
        measured_at=datetime(2026, 6, 10, 7, 14, tzinfo=UTC), source="Omron",
    )
    for days, systolic, diastolic, measurements in ((7, 127.84, 71.63, 9), (30, 129.2, 73.1, 31)):
        state.blood_pressure_trends[days] = BloodPressureTrend(
            systolic=systolic, diastolic=diastolic,
            measurements=measurements, period_days=days,
        )
    state.sleep_trend = SleepTrend(
        nights=7, avg_total_min=421.0, avg_rem_min=88.0, avg_deep_min=57.0,
        avg_core_min=240.0, avg_awake_min=26.0,
        avg_sleep_start_offset_min=330.0, avg_wake_offset_min=790.0,
        sleep_start_stddev_min=31.5, avg_nap_total_min=12.0,
        nights_by_field={"avg_total_min": 7, "avg_rem_min": 5, "avg_deep_min": 5},
    )
    return state


def build(state: HealthState) -> list[AppleHealthSensor]:
    return [AppleHealthSensor("entry-1", state, description) for description in SENSORS]


def test_every_sensor_builds_and_reports_a_value():
    sensors = build(populated_state())
    assert len(sensors) == 29

    for sensor in sensors:
        # Both properties are exercised: a typo in either lambda raises here.
        value = sensor.native_value
        attrs = sensor.extra_state_attributes
        assert value is not None, f"{sensor.entity_description.key} produced no value"
        assert attrs is None or isinstance(attrs, dict)


def test_unique_ids_and_translation_keys_are_unique():
    sensors = build(populated_state())
    assert len({s.unique_id for s in sensors}) == len(sensors)
    keys = [s.entity_description.key for s in sensors]
    assert len(set(keys)) == len(keys)


def test_every_declared_device_class_accepts_its_unit():
    """A mismatch here makes Home Assistant refuse the entity at runtime."""
    for description in SENSORS:
        device_class = description.device_class
        unit = description.native_unit_of_measurement
        if device_class in (None, SensorDeviceClass.TIMESTAMP, SensorDeviceClass.ENUM):
            continue
        allowed = DEVICE_CLASS_UNITS[device_class]
        assert unit in allowed, f"{description.key}: {unit} invalid for {device_class}"


def test_sensor_units_match_the_registry():
    """The displayed unit and the stored unit must not diverge."""
    by_key = {d.key: d for d in SENSORS}
    for metric, spec in METRICS.items():
        key = spec.snapshot_key if metric != "heart_rate" else "heart_rate"
        if key not in by_key:
            continue
        assert by_key[key].native_unit_of_measurement == spec.unit, metric


def test_an_empty_state_reports_unknown_rather_than_zero():
    """Absence must never render as a measurement of zero."""
    for sensor in build(HealthState()):
        assert sensor.native_value is None, sensor.entity_description.key


def test_a_missing_sleep_stage_reports_none_not_zero():
    state = populated_state()
    state.sleep.rem_min = None
    by_key = {s.entity_description.key: s for s in build(state)}

    assert by_key["sleep_rem"].native_value is None
    assert by_key["sleep_total"].extra_state_attributes["rem_min"] is None
    # A measured zero still reads as zero.
    state.sleep.deep_min = 0.0
    assert by_key["sleep_deep"].native_value == 0.0


def test_naps_are_reported_separately_from_the_night():
    by_key = {s.entity_description.key: s for s in build(populated_state())}
    assert by_key["nap_total"].native_value == 35.0
    assert by_key["nap_total"].extra_state_attributes["nap_count"] == 1.0
    # The night is untouched by them.
    assert by_key["sleep_total"].native_value == 431.0


def test_the_nap_sensor_keeps_its_entity_id_after_moving_to_a_daily_metric():
    """The value's source changed; the entity must not.

    A renamed entity_id would orphan every dashboard card and automation
    referencing it, and would strand the existing statistics history.
    """
    keys = {d.key for d in SENSORS}
    assert "nap_total" in keys
    # And exactly one entity owns it, so nothing was duplicated by the move.
    assert [d.key for d in SENSORS].count("nap_total") == 1


def test_naps_are_shown_on_a_day_with_no_main_night():
    """The behaviour the move exists to make possible."""
    state = HealthState()
    state.measurements["nap_total"] = MeasurementSnapshot(
        metric="nap_total", value=90.0, unit="min",
        measured_at=datetime(2026, 6, 10, 15, tzinfo=UTC), source="Apple Watch",
    )
    by_key = {s.entity_description.key: s for s in build(state)}

    assert by_key["nap_total"].native_value == 90.0
    # No night at all, and the sleep sensors stay unknown rather than zero.
    assert state.sleep is None
    assert by_key["sleep_total"].native_value is None


def test_the_trend_reports_how_many_nights_contributed():
    by_key = {s.entity_description.key: s for s in build(populated_state())}
    assert by_key["sleep_7d_rem"].extra_state_attributes["nights_contributing"] == 5
    assert by_key["sleep_7d_total"].extra_state_attributes["nights"] == 7


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(330.0, "23:30"), (390.0, "00:30"), (0.0, "18:00"), (790.0, "07:10")],
)
def test_a_bedtime_offset_renders_back_to_a_clock_time(offset, expected):
    assert _offset_clock(offset) == expected


def test_the_bedtime_sensor_carries_both_forms():
    by_key = {s.entity_description.key: s for s in build(populated_state())}
    bedtime = by_key["sleep_7d_bedtime"]
    assert bedtime.native_value == 330.0
    assert bedtime.extra_state_attributes["local_time"] == "23:30"


def test_a_restored_value_is_used_only_until_real_data_arrives():
    state = HealthState()
    state.restored["sleep_total"] = 400.0
    by_key = {s.entity_description.key: s for s in build(state)}
    assert by_key["sleep_total"].native_value == 400.0

    state.sleep = populated_state().sleep
    assert by_key["sleep_total"].native_value == 431.0, "live data must win"


# --- Diagnostics must never carry a measured value --------------------------


async def test_diagnostics_report_presence_but_never_values():
    from types import SimpleNamespace

    from custom_components.apple_health_sync.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = SimpleNamespace(entry_id="entry-1", data={"token": "secret", "webhook_id": "w"})
    entry.runtime_data = AppleHealthSyncRuntimeData(
        token="secret", webhook_id="w", state=populated_state()
    )
    report = await async_get_config_entry_diagnostics(None, entry)
    blob = repr(report)

    assert report["state"]["metrics_present"] == [
        "body_fat_percentage", "body_mass", "hrv_sdnn", "nap_count", "nap_total",
        "oxygen_saturation", "respiratory_rate", "resting_heart_rate", "vo2_max",
    ]
    assert report["state"]["sleep"]["stages_present"] == [
        "awake_min", "core_min", "deep_min", "rem_min",
    ]
    assert report["state"]["sleep_trend_nights"] == 7

    # Not one measured value may appear anywhere in the payload.
    for value in ("431", "92.0", "96.5", "612", "7.4", "54.0", "44.0", "8423",
                  "61.0", "42.7"):
        assert value not in blob, f"diagnostics leaked a health value: {value}"
    assert "secret" not in blob


# --- Phase 3B.1: body composition and blood pressure ------------------------


def test_the_body_metrics_have_stable_english_entity_ids():
    """German names must never reach the entity_id."""
    from homeassistant.util import slugify

    from custom_components.apple_health_sync.const import DEVICE_NAME

    for key, expected in (
        ("body_mass", "sensor.apple_health_body_mass"),
        ("body_fat", "sensor.apple_health_body_fat"),
        ("blood_pressure", "sensor.apple_health_blood_pressure"),
    ):
        description = next(d for d in SENSORS if d.key == key)
        entity = AppleHealthSensor("entry-1", HealthState(), description)
        assert entity.suggested_object_id == key
        assert f"sensor.{slugify(f'{DEVICE_NAME} {key}')}" == expected


def test_blood_pressure_shows_the_pair_together():
    """A blood pressure is one fact with two numbers; the state is the pair."""
    by_key = {s.entity_description.key: s for s in build(populated_state())}
    sensor = by_key["blood_pressure"]

    assert sensor.native_value == "128 / 82 mmHg"
    attrs = sensor.extra_state_attributes
    # Both halves stay machine-readable beside the human-readable state.
    assert attrs["systolic"] == 128.0
    assert attrs["diastolic"] == 82.0
    assert attrs["measured_at"] == "2026-06-10T07:14:00+00:00"
    # The unit rides in the state text and as an attribute; declaring it the
    # ordinary way would make Home Assistant demand a numeric value.
    assert attrs["unit"] == "mmHg"
    assert sensor.entity_description.native_unit_of_measurement is None


def test_no_separate_diastolic_entity_exists():
    """Diastolic keeps its own statistic series but not its own entity."""
    keys = {d.key for d in SENSORS}
    assert "blood_pressure" in keys
    assert "blood_pressure_diastolic" not in keys
    assert "diastolic" not in keys


def test_without_a_reading_blood_pressure_reports_nothing_at_all():
    """Never half a pair, never a fabricated zero."""
    by_key = {s.entity_description.key: s for s in build(HealthState())}
    sensor = by_key["blood_pressure"]

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["diastolic"] is None
    assert sensor.extra_state_attributes["measured_at"] is None


def test_body_metric_units_come_from_the_registry():
    by_key = {d.key: d for d in SENSORS}
    assert by_key["body_mass"].native_unit_of_measurement == "kg"
    assert by_key["body_fat"].native_unit_of_measurement == "%"
    # Blood pressure declares no unit: its state is a pair, and any declared
    # unit would make Home Assistant reject a non-numeric value.
    assert by_key["blood_pressure"].native_unit_of_measurement is None


def test_blood_pressure_and_body_fat_carry_no_device_class():
    """Home Assistant has no class meaning "blood pressure" or "body fat".

    `pressure` means atmospheric or gas pressure and would offer conversion to
    hPa; the percent classes are battery, humidity, moisture and power factor.
    Declaring one for appearance would be actively misleading.
    """
    by_key = {d.key: d for d in SENSORS}
    assert by_key["blood_pressure"].device_class is None
    assert by_key["body_fat"].device_class is None
    # Weight is a real match, so body mass does declare one.
    assert by_key["body_mass"].device_class is SensorDeviceClass.WEIGHT


async def test_diagnostics_never_expose_a_body_value():
    from types import SimpleNamespace

    from custom_components.apple_health_sync.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = SimpleNamespace(entry_id="entry-1", data={"token": "secret", "webhook_id": "w"})
    entry.runtime_data = AppleHealthSyncRuntimeData(
        token="secret", webhook_id="w", state=populated_state()
    )
    report = await async_get_config_entry_diagnostics(None, entry)
    blob = repr(report)

    assert report["state"]["blood_pressure"]["has_pair"] is True
    for value in ("128", "82.0", "81.4", "18.2"):
        assert value not in blob, f"diagnostics leaked a body value: {value}"


def test_blood_pressure_restores_as_a_pair_or_not_at_all():
    """Half a restored reading would be worse than none.

    Restoring the state without its diastolic attribute would put a systolic
    back on display with no partner — the exact half-pair the composite snapshot
    exists to prevent.
    """
    from homeassistant.core import State

    description = next(d for d in SENSORS if d.key == "blood_pressure")

    # A complete restored state brings the pair back.
    complete = HealthState()
    description.restore_fn(complete, State(
        "sensor.apple_health_blood_pressure", "128 / 82",
        {"systolic": 128.0, "diastolic": 82.0,
         "measured_at": "2026-06-10T07:14:00+00:00", "source": "Omron"},
    ))
    assert complete.blood_pressure is not None
    assert complete.blood_pressure.systolic == 128.0
    assert complete.blood_pressure.diastolic == 82.0

    # A state missing the diastolic attribute restores nothing.
    partial = HealthState()
    description.restore_fn(partial, State(
        "sensor.apple_health_blood_pressure", "128 / 82",
        {"systolic": 128.0, "measured_at": "2026-06-10T07:14:00+00:00"},
    ))
    assert partial.blood_pressure is None
    sensor = AppleHealthSensor("entry-1", partial, description)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["diastolic"] is None


def test_a_live_reading_beats_a_restored_one():
    from homeassistant.core import State

    description = next(d for d in SENSORS if d.key == "blood_pressure")
    state = populated_state()
    description.restore_fn(state, State(
        "sensor.apple_health_blood_pressure", "99 / 60",
        {"systolic": 99.0, "diastolic": 60.0,
         "measured_at": "2020-01-01T00:00:00+00:00"},
    ))
    assert state.blood_pressure.systolic == 128.0, "live data must win"


# --- Blood-pressure trend entities ------------------------------------------


def test_the_trend_entities_exist_with_stable_english_keys():
    keys = {d.key for d in SENSORS}
    assert "blood_pressure_7d" in keys
    assert "blood_pressure_30d" in keys
    # The weight series is infrastructure, never an entity.
    assert "blood_pressure_count" not in keys


def test_the_trends_show_a_pair_not_a_scalar():
    by_key = {s.entity_description.key: s for s in build(populated_state())}

    seven = by_key["blood_pressure_7d"]
    assert seven.native_value == "128 / 72 mmHg"
    attrs = seven.extra_state_attributes
    # Rounded for reading, exact for machines.
    assert attrs["systolic"] == pytest.approx(127.84)
    assert attrs["diastolic"] == pytest.approx(71.63)
    assert attrs["period_days"] == 7
    assert attrs["measurement_count"] == 9

    thirty = by_key["blood_pressure_30d"]
    assert thirty.native_value == "129 / 73 mmHg"
    assert thirty.extra_state_attributes["period_days"] == 30
    assert thirty.extra_state_attributes["measurement_count"] == 31


def test_the_trends_display_whole_millimetres_without_rounding_the_source():
    by_key = {d.key: d for d in SENSORS}
    for key in ("blood_pressure_7d", "blood_pressure_30d"):
        # No declared unit: it travels in the state text instead.
        assert by_key[key].native_unit_of_measurement is None

    seven = {s.entity_description.key: s for s in build(populated_state())}["blood_pressure_7d"]
    # Whole millimetres on display...
    assert seven.native_value == "128 / 72 mmHg"
    # ...while the attributes keep every digit the computation produced.
    assert seven.extra_state_attributes["systolic"] == pytest.approx(127.84)
    assert seven.extra_state_attributes["diastolic"] == pytest.approx(71.63)


def test_no_blood_pressure_entity_declares_a_state_class():
    """The state is a pair, which Home Assistant cannot compile statistics from.

    Nothing is lost: the durable history is the external
    apple_health_sync:blood_pressure_* series, which stores both halves, where an
    entity statistic could only ever have recorded the systolic half.
    """
    by_key = {d.key: d for d in SENSORS}
    for key in ("blood_pressure", "blood_pressure_7d", "blood_pressure_30d"):
        assert by_key[key].state_class is None, key
        assert by_key[key].device_class is None, key


def test_without_a_trend_the_entities_report_nothing():
    by_key = {s.entity_description.key: s for s in build(HealthState())}
    for key in ("blood_pressure_7d", "blood_pressure_30d"):
        assert by_key[key].native_value is None
        assert by_key[key].extra_state_attributes["measurement_count"] is None
        # period_days is structural, not a measurement.
        assert by_key[key].extra_state_attributes["period_days"] in (7, 30)


def test_the_latest_reading_is_unaffected_by_the_trends():
    by_key = {s.entity_description.key: s for s in build(populated_state())}
    latest = by_key["blood_pressure"]
    assert latest.native_value == "128 / 82 mmHg"
    assert latest.extra_state_attributes["diastolic"] == 82.0
    # The trend is a different entity with a different value.
    assert by_key["blood_pressure_7d"].native_value != latest.native_value


def test_a_trend_is_never_restored_from_a_stale_state():
    """It is derived from durable statistics and recomputed on the next sync;
    restoring it would present a stale average with a stale count as current."""
    from homeassistant.core import State

    state = HealthState()
    for key in ("blood_pressure_7d", "blood_pressure_30d"):
        description = next(d for d in SENSORS if d.key == key)
        description.restore_fn(state, State(f"sensor.apple_health_{key}", "128.0",
                                            {"diastolic": 82.0}))
        assert AppleHealthSensor("entry-1", state, description).native_value is None


def test_a_half_pair_is_never_displayed():
    """Neither a bare systolic nor a bare diastolic is a blood pressure."""
    from custom_components.apple_health_sync.sensor import _pressure_pair

    assert _pressure_pair(128.0, 82.0) == "128 / 82 mmHg"
    assert _pressure_pair(128.0, None) is None
    assert _pressure_pair(None, 82.0) is None
    assert _pressure_pair(None, None) is None


def test_the_pair_rounds_to_whole_millimetres_for_reading():
    from custom_components.apple_health_sync.sensor import _pressure_pair

    assert _pressure_pair(127.842105, 71.631578) == "128 / 72 mmHg"
    assert _pressure_pair(120.4, 68.5) == "120 / 68 mmHg"


def test_the_blood_pressure_entity_ids_are_unchanged():
    """A renamed entity would orphan dashboards and automations."""
    from homeassistant.util import slugify

    from custom_components.apple_health_sync.const import DEVICE_NAME

    for key, expected in (
        ("blood_pressure", "sensor.apple_health_blood_pressure"),
        ("blood_pressure_7d", "sensor.apple_health_blood_pressure_7d"),
        ("blood_pressure_30d", "sensor.apple_health_blood_pressure_30d"),
    ):
        description = next(d for d in SENSORS if d.key == key)
        entity = AppleHealthSensor("entry-1", HealthState(), description)
        assert entity.suggested_object_id == key
        assert f"sensor.{slugify(f'{DEVICE_NAME} {key}')}" == expected


def test_only_three_blood_pressure_entities_exist():
    """No duplicate display entity was introduced alongside the originals."""
    keys = [d.key for d in SENSORS if d.key.startswith("blood_pressure")]
    assert sorted(keys) == ["blood_pressure", "blood_pressure_30d", "blood_pressure_7d"]


def test_the_other_metrics_still_report_numbers():
    """Only blood pressure became a pair; nothing else changed shape."""
    by_key = {s.entity_description.key: s for s in build(populated_state())}

    assert by_key["body_mass"].native_value == 81.4
    assert by_key["body_fat"].native_value == 18.2
    assert by_key["heart_rate"].native_value == 61.0
    assert by_key["steps_today"].native_value == 8423.0
    assert by_key["sleep_total"].native_value == 431.0
    for key in ("body_mass", "body_fat", "heart_rate", "steps_today"):
        assert by_key[key].entity_description.state_class is not None, key

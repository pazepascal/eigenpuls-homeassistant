"""Blood-pressure entities through a fully set-up integration.

The previous suite only called `native_value` directly. It never went through
`SensorEntity.state`, where Home Assistant validates the value against the unit
and the device/state class, so a representation that satisfied every test still
produced Unknown on the live instance. These tests set the integration up for
real and read the states back out of the state machine.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apple_health_sync.const import (
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    DOMAIN,
    SIGNAL_UPDATE,
)
from custom_components.apple_health_sync.payload import BloodPressureSnapshot
from custom_components.apple_health_sync.sensor import SENSORS
from custom_components.apple_health_sync.statistics import BloodPressureTrend

BP_KEYS = ("blood_pressure", "blood_pressure_7d", "blood_pressure_30d")


@pytest.fixture(autouse=True)
def enable_custom_components(recorder_mock, enable_custom_integrations):
    """The integration lives in custom_components and depends on the recorder.

    `recorder_mock` is requested first because the recorder database fixture
    refuses to initialise once `hass` exists.
    """
    return


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A fully set-up integration, entities and all."""
    config = MockConfigEntry(
        domain=DOMAIN, title="Apple Health Sync",
        data={CONF_TOKEN: "t" * 40, CONF_WEBHOOK_ID: "hook"},
    )
    config.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    return config


async def publish(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Push the current state to the entities, as a completed sync does."""
    async_dispatcher_send(hass, SIGNAL_UPDATE.format(entry.entry_id))
    await hass.async_block_till_done()


def fill(entry: MockConfigEntry) -> None:
    state = entry.runtime_data.state
    state.blood_pressure = BloodPressureSnapshot(
        systolic=123.0, diastolic=70.0, unit="mmHg",
        measured_at=datetime(2026, 6, 10, 7, 14, tzinfo=UTC), source="Omron",
    )
    state.blood_pressure_trends[7] = BloodPressureTrend(
        systolic=120.3, diastolic=68.4, measurements=9, period_days=7)
    state.blood_pressure_trends[30] = BloodPressureTrend(
        systolic=123.2, diastolic=70.8, measurements=31, period_days=30)


# --- The live regression -----------------------------------------------------


async def test_the_entities_exist_after_setup(entry, hass: HomeAssistant):
    for key in BP_KEYS:
        assert hass.states.get(f"sensor.apple_health_{key}") is not None, key


async def test_no_blood_pressure_entity_is_unknown_once_it_has_data(
    hass: HomeAssistant, entry
):
    """The live symptom: all three read Unknown after a successful sync."""
    fill(entry)
    await publish(hass, entry)

    for key in BP_KEYS:
        written = hass.states.get(f"sensor.apple_health_{key}")
        assert written.state not in ("unknown", "unavailable"), (
            f"{key} is {written.state}"
        )


async def test_the_visible_states_show_both_numbers(entry, hass: HomeAssistant):
    fill(entry)
    await publish(hass, entry)

    assert hass.states.get("sensor.apple_health_blood_pressure").state == "123 / 70 mmHg"
    assert hass.states.get("sensor.apple_health_blood_pressure_7d").state == "120 / 68 mmHg"
    assert hass.states.get("sensor.apple_health_blood_pressure_30d").state == "123 / 71 mmHg"


async def test_no_blood_pressure_entity_declares_a_numeric_contract():
    """Any one of these makes Home Assistant demand a number.

    `_numeric_state_expected` returns True if state_class, the unit or the
    display precision is set - a unit alone is enough, which is what produced
    the live Unknown.
    """
    by_key = {d.key: d for d in SENSORS}
    for key in BP_KEYS:
        description = by_key[key]
        assert description.native_unit_of_measurement is None, key
        assert description.state_class is None, key
        assert description.device_class is None, key
        assert description.suggested_display_precision is None, key


# --- Structured data survives the formatting ---------------------------------


async def test_the_latest_reading_keeps_its_structured_values(entry, hass: HomeAssistant):
    fill(entry)
    await publish(hass, entry)
    attrs = hass.states.get("sensor.apple_health_blood_pressure").attributes

    assert attrs["systolic"] == 123.0
    assert attrs["diastolic"] == 70.0
    assert attrs["measured_at"] == "2026-06-10T07:14:00+00:00"
    assert attrs["source"] == "Omron"
    assert attrs["unit"] == "mmHg"


async def test_the_trends_keep_full_precision_and_their_counts(
    hass: HomeAssistant, entry
):
    fill(entry)
    await publish(hass, entry)

    for key, days, systolic, diastolic, count in (
        ("blood_pressure_7d", 7, 120.3, 68.4, 9),
        ("blood_pressure_30d", 30, 123.2, 70.8, 31),
    ):
        attrs = hass.states.get(f"sensor.apple_health_{key}").attributes
        assert attrs["systolic"] == pytest.approx(systolic), key
        assert attrs["diastolic"] == pytest.approx(diastolic), key
        assert attrs["period_days"] == days
        assert attrs["measurement_count"] == count
        assert attrs["unit"] == "mmHg"


async def test_without_data_the_entities_are_unknown_not_a_half_pair(
    hass: HomeAssistant, entry
):
    await publish(hass, entry)
    for key in BP_KEYS:
        written = hass.states.get(f"sensor.apple_health_{key}")
        assert written.state == "unknown", key
        assert "/" not in written.state


# --- Everything else must still behave like a number -------------------------


async def test_the_other_metrics_still_write_numeric_states(entry, hass: HomeAssistant):
    state = entry.runtime_data.state
    state.heart_rate = 61.0
    state.blood_pressure = None
    await publish(hass, entry)

    heart_rate = hass.states.get("sensor.apple_health_heart_rate")
    assert heart_rate.state == "61.0"
    assert heart_rate.attributes["unit_of_measurement"] == "bpm"
    assert heart_rate.attributes["state_class"] == "measurement"


async def test_the_entity_ids_are_the_expected_ones(entry, hass: HomeAssistant):
    """A renamed entity would orphan dashboards and automations."""
    for key in BP_KEYS:
        assert hass.states.get(f"sensor.apple_health_{key}") is not None, key
    # And no duplicate blood-pressure entity appeared beside them.
    bp_entities = [
        state.entity_id for state in hass.states.async_all("sensor")
        if "blood_pressure" in state.entity_id
    ]
    assert sorted(bp_entities) == [
        "sensor.apple_health_blood_pressure",
        "sensor.apple_health_blood_pressure_30d",
        "sensor.apple_health_blood_pressure_7d",
    ]


# --- Last workout, through the same real state machine ----------------------


def fill_workout(entry: MockConfigEntry) -> None:
    from custom_components.apple_health_sync.payload import LastWorkout

    entry.runtime_data.state.last_workout = LastWorkout(
        uuid="F1A2", activity="strength_training",
        start=datetime(2026, 6, 10, 17, 0, tzinfo=UTC),
        end=datetime(2026, 6, 10, 18, 5, tzinfo=UTC),
        duration_min=58.0, active_energy_kcal=410.0,
        avg_heart_rate_bpm=126.0, max_heart_rate_bpm=158.0, source="Apple Watch",
    )


async def test_the_last_workout_state_is_the_activity_identifier(entry, hass):
    """English and stable in the state; German only in the rendered label."""
    fill_workout(entry)
    await publish(hass, entry)

    written = hass.states.get("sensor.apple_health_last_workout")
    assert written.state == "strength_training"
    assert written.attributes["device_class"] == "enum"
    assert "strength_training" in written.attributes["options"]


async def test_the_last_workout_attributes_carry_the_detail(entry, hass):
    fill_workout(entry)
    await publish(hass, entry)
    attrs = hass.states.get("sensor.apple_health_last_workout").attributes

    assert attrs["activity"] == "strength_training"
    assert attrs["started_at"] == "2026-06-10T17:00:00+00:00"
    assert attrs["ended_at"] == "2026-06-10T18:05:00+00:00"
    # HealthKit's pause-aware duration, shorter than the 65-minute span.
    assert attrs["duration_min"] == 58.0
    assert attrs["active_energy_kcal"] == 410.0
    assert attrs["average_heart_rate_bpm"] == 126.0
    assert attrs["maximum_heart_rate_bpm"] == 158.0
    assert attrs["source"] == "Apple Watch"
    # A strength session records no distance, and none is invented.
    assert attrs["distance_km"] is None
    # The HealthKit identity is not for reading.
    assert "uuid" not in attrs


async def test_without_a_workout_the_entity_is_unknown(entry, hass):
    await publish(hass, entry)
    assert hass.states.get("sensor.apple_health_last_workout").state == "unknown"


async def test_no_entity_is_named_after_an_individual_activity(entry, hass):
    """Two composite workout entities, and never one per activity type.

    The rule this protects is 3B.2's: Home Assistant keeps the latest session in
    detail and durable daily totals, not a database of workouts. Phase 4C added
    the category breakdown as a *second composite* - one entity carrying all
    twelve - for the same reason, so the count changed and the rule did not.

    Both halves are asserted, because only the second one is the rule: a future
    "sensor.apple_health_workout_running" would pass a bare count check and be
    exactly what this file exists to prevent.
    """
    from custom_components.apple_health_sync import registry

    workout_entities = sorted(
        state.entity_id for state in hass.states.async_all("sensor")
        if "workout" in state.entity_id or "training" in state.entity_id
    )
    assert workout_entities == [
        "sensor.apple_health_last_workout",
        "sensor.apple_health_workout_categories",
    ]

    for activity in registry.WORKOUT_ACTIVITIES:
        assert not any(
            state.entity_id.endswith(f"_{activity}")
            for state in hass.states.async_all("sensor")
        ), f"an entity was created for the {activity} category"

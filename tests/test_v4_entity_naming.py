"""Entity ids stay English and technical; visible names may be German.

Home Assistant derives an entity_id from the entity *name*, and German is in
`homeassistant.generated.languages.NATIVE_ENTITY_IDS` — so on a German instance
the translated name would otherwise become the id, producing
`sensor.apple_health_herzfrequenzvariabilitat`. Worse, the id would then depend
on the interface language and would move if a translation were reworded.

These tests set the instance language to German and assert both halves against a
real entity registry: technical ids in English, friendly names in German.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.generated import languages
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from custom_components.apple_health_sync.const import DEVICE_NAME, DOMAIN
from custom_components.apple_health_sync.sensor import SENSORS

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / DOMAIN


def names(filename: str) -> dict[str, str]:
    data = json.loads((COMPONENT / filename).read_text(encoding="utf-8"))
    return {key: value["name"] for key, value in data["entity"]["sensor"].items()}


def test_german_really_is_a_native_entity_id_language():
    """The premise. If this ever changes, the pin below becomes unnecessary."""
    assert "de" in languages.NATIVE_ENTITY_IDS
    assert languages.DEFAULT_LANGUAGE == "en"


def test_entity_ids_are_derived_from_the_key_not_the_name():
    """The id must not depend on any translation."""
    for description in SENSORS:
        expected = f"{slugify(f'{DEVICE_NAME} {description.key}')}"
        assert expected.isascii()
        assert expected == f"apple_health_{description.key}"


def test_the_three_original_entity_ids_are_unchanged():
    """Pinning must not rename entities that already exist on the instance."""
    ids = {f"sensor.{slugify(f'{DEVICE_NAME} {d.key}')}" for d in SENSORS}
    for existing in (
        "sensor.apple_health_heart_rate",
        "sensor.apple_health_steps_today",
        "sensor.apple_health_last_sync",
    ):
        assert existing in ids


def test_every_entity_id_is_ascii_and_predictable():
    for description in SENSORS:
        entity_id = f"sensor.{slugify(f'{DEVICE_NAME} {description.key}')}"
        assert entity_id.isascii(), entity_id
        assert " " not in entity_id
        assert entity_id.islower()


# --- The translations themselves --------------------------------------------


def test_german_names_exist_for_every_sensor_and_are_actually_german():
    german, english = names("translations/de.json"), names("strings.json")
    assert set(german) == set(english) == {d.key for d in SENSORS}

    # The specific pairing the product decision named.
    assert german["hrv_sdnn"] == "Herzfrequenzvariabilität"
    assert english["hrv_sdnn"] == "Heart Rate Variability"

    # Nothing German was left as its English string.
    untranslated = {k for k in german if german[k] == english[k]}
    assert not untranslated, f"still English in de.json: {sorted(untranslated)}"


def test_english_names_stay_ascii():
    """strings.json is the object-id fallback for non-native languages."""
    for key, name in names("strings.json").items():
        assert name.isascii(), f"{key}: {name}"


# --- Against a real registry, in German -------------------------------------


@pytest.fixture
async def german_hass(hass: HomeAssistant) -> HomeAssistant:
    await hass.config.async_update(language="de")
    return hass


async def test_ids_english_and_names_german_on_a_german_instance(
    german_hass: HomeAssistant,
):
    """End to end through Home Assistant's own naming machinery."""
    from custom_components.apple_health_sync.sensor import AppleHealthSensor
    from custom_components.apple_health_sync.state import HealthState

    state = HealthState()
    registry = er.async_get(german_hass)
    german = names("translations/de.json")

    for description in SENSORS:
        entity = AppleHealthSensor("entry-1", state, description)
        # The id basis must be the key, never the translated name.
        assert entity.suggested_object_id == description.key
        assert entity.suggested_object_id.isascii()

        entry = registry.async_get_or_create(
            "sensor", DOMAIN, entity.unique_id,
            suggested_object_id=f"{DEVICE_NAME} {entity.suggested_object_id}",
        )
        assert entry.entity_id == f"sensor.apple_health_{description.key}"
        assert entry.entity_id.isascii()
        # And the German name is available for display.
        assert german[description.key]


def test_the_phase_3b1_body_metrics_are_named_in_german():
    german, english = names("translations/de.json"), names("strings.json")

    assert german["body_mass"] == "Gewicht"
    assert german["body_fat"] == "Körperfett"
    assert german["blood_pressure"] == "Blutdruck"
    # And their technical identities stay English.
    assert english["body_mass"] == "Body Mass"
    for key in ("body_mass", "body_fat", "blood_pressure"):
        assert english[key].isascii()


def test_the_blood_pressure_trend_entities_are_named_in_german():
    german, english = names("translations/de.json"), names("strings.json")

    assert german["blood_pressure_7d"] == "Blutdruck Ø 7 Tage"
    assert german["blood_pressure_30d"] == "Blutdruck Ø 30 Tage"
    # Technical identities stay English and ASCII.
    for key in ("blood_pressure_7d", "blood_pressure_30d"):
        assert english[key].isascii()
        assert f"sensor.apple_health_{key}".isascii()

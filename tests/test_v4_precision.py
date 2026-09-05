"""Display precision: presentation only, and never on the blood-pressure pairs.

`suggested_display_precision` changes what a dashboard renders and nothing else -
the long-term statistics and the entity attributes keep full precision. These
tests hold both halves of that: the numeric entities round for reading, and the
three pair entities stay free of every property that would make Home Assistant
demand a number instead of the string "128 / 82 mmHg".
"""

from __future__ import annotations

import pytest

from custom_components.apple_health_sync.sensor import DISPLAY_PRECISION, SENSORS

BLOOD_PRESSURE_KEYS = frozenset(
    {"blood_pressure", "blood_pressure_7d", "blood_pressure_30d"}
)


def _by_key():
    return {description.key: description for description in SENSORS}


class TestBloodPressureStaysAString:
    """The cd6ea3d regression, held down by structure rather than by memory."""

    @pytest.mark.parametrize("key", sorted(BLOOD_PRESSURE_KEYS))
    def test_the_pair_entities_declare_nothing_numeric(self, key: str) -> None:
        description = _by_key()[key]
        # Any one of these makes `_numeric_state_expected` return True, and the
        # entity then goes Unknown because its state is a pair, not a number.
        assert description.native_unit_of_measurement is None
        assert description.state_class is None
        assert description.device_class is None
        assert description.suggested_display_precision is None

    def test_no_blood_pressure_entity_is_in_the_precision_table(self) -> None:
        assert not BLOOD_PRESSURE_KEYS & set(DISPLAY_PRECISION)

    def test_the_pair_state_survives_a_precision_pass(self) -> None:
        """The rendered state is still the pair, unit included."""
        from custom_components.apple_health_sync.sensor import _pressure_pair

        assert _pressure_pair(128.4, 82.6) == "128 / 83 mmHg"
        assert _pressure_pair(None, 82.0) is None
        assert _pressure_pair(120.0, None) is None


class TestPrecisionIsAppliedWhereItIsSafe:
    def test_every_entity_in_the_table_exists(self) -> None:
        keys = {description.key for description in SENSORS}
        assert set(DISPLAY_PRECISION) <= keys

    def test_every_entity_with_precision_also_has_a_unit(self) -> None:
        """Precision without a unit is the shape that broke blood pressure."""
        for description in SENSORS:
            if description.suggested_display_precision is not None:
                assert description.native_unit_of_measurement is not None, description.key

    def test_the_table_reaches_every_numeric_entity(self) -> None:
        """A numeric entity with no precision renders a raw float - the HRV bug."""
        missing = [
            description.key
            for description in SENSORS
            if description.native_unit_of_measurement is not None
            and description.suggested_display_precision is None
        ]
        assert missing == [], f"numeric entities with no display precision: {missing}"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("heart_rate", 0),
            ("resting_heart_rate", 0),
            ("hrv_sdnn", 0),
            ("respiratory_rate", 1),
            ("oxygen_saturation", 0),
            ("body_mass", 1),
            ("body_fat", 1),
            ("active_energy_today", 0),
            ("distance_today", 2),
            ("steps_today", 0),
            ("sleep_total", 0),
            ("sleep_rem", 0),
            ("sleep_core", 0),
            ("sleep_deep", 0),
            ("sleep_awake", 0),
            ("nap_total", 0),
            ("sleep_7d_total", 0),
            ("sleep_7d_rem", 0),
            ("sleep_7d_deep", 0),
            ("sleep_7d_bedtime", 0),
            ("sleep_7d_consistency", 0),
        ],
    )
    def test_the_agreed_precision_reaches_the_entity(self, key: str, expected: int) -> None:
        assert _by_key()[key].suggested_display_precision == expected

    def test_timestamp_entities_get_no_precision(self) -> None:
        for key in ("last_sync", "sleep_start", "sleep_wake"):
            assert _by_key()[key].suggested_display_precision is None

    def test_the_enum_workout_entity_gets_no_precision(self) -> None:
        assert _by_key()["last_workout"].suggested_display_precision is None


class TestDiagnosticsStayValueFree:
    """Workouts report structure and freshness, never what was trained."""

    def test_the_workout_diagnostic_names_no_health_value(self) -> None:
        import inspect

        from custom_components.apple_health_sync import diagnostics

        source = inspect.getsource(diagnostics.async_get_config_entry_diagnostics)
        block = source[source.index('"last_workout"') : source.index('"sleep_trend_nights"')]
        # Presence of a field may be reported; its value may not, and neither
        # may the activity - what sport someone does is a health value too.
        for forbidden in (
            ".duration_min", ".active_energy_kcal", ".distance_km",
            ".avg_heart_rate_bpm", ".max_heart_rate_bpm", ".activity", ".uuid",
        ):
            assert forbidden not in block, f"{forbidden} must not reach diagnostics"

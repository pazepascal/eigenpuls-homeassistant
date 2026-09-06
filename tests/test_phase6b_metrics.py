"""Phase 6B: heart-rate recovery, lean body mass, height, body temperature.

The last four of the catalogue. Like Phase 4B these are deliberately dull -
no new bucket family, no new read path, no new snapshot composite - so what is
worth testing is the handful of decisions that could be quietly wrong.

**The shapes come from Apple's own header.** `HKTypeIdentifiers.h` in the 26.5
SDK annotates every quantity type, and these were read rather than guessed:

    HeartRateRecoveryOneMinute  // count/min, Discrete (Arithmetic)   iOS 16+
    LeanBodyMass                // kg,        Discrete (Arithmetic)
    Height                      // m,         Discrete (Arithmetic)
    BodyTemperature             // degC,      Discrete (Arithmetic)

Note that heart-rate recovery is *arithmetic* where heart rate itself is
temporally weighted. It is one derived number per workout rather than a signal
sampled over time, so occurrences average and duration-weighting them would
mean nothing.

**Body temperature is one of four temperature types, and they are not
interchangeable.** `WaterTemperature` is not a body measurement,
`BasalBodyTemperature` belongs to cycle tracking, and
`AppleSleepingWristTemperature` is an overnight wrist reading that the Health
app shows as a deviation from a personal baseline - its absolute value runs
well below core temperature. Carrying any of those under this name would be
wrong by about two degrees, so this series is
`HKQuantityTypeIdentifierBodyTemperature` alone.

**Nothing here is computed from anything else.** Lean body mass is not body
mass minus fat, and height does not feed BMI. See
`test_phase4b_metrics.py::test_bmi_is_read_and_never_derived_from_the_body_metrics_beside_it`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync import registry
from custom_components.apple_health_sync.registry import METRICS, BucketKind, MeanType

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)

PHASE_6B = ("heart_rate_recovery", "lean_body_mass", "height", "body_temperature")


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def envelope(**overrides):
    body = {
        "version": 4, "type": "sync", "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True}, "snapshot": {},
    }
    body.update(overrides)
    return body


def hour(metric, **fields):
    return {"metric": metric, "start": iso(NOW), **fields}


# --- the shapes, against the SDK header -------------------------------------


@pytest.mark.parametrize(
    ("metric", "unit", "unit_class"),
    [
        # count/min in the header. The wire spells that bpm, as resting heart
        # rate already does. Home Assistant has no bpm converter, so no class.
        ("heart_rate_recovery", "bpm", None),
        ("lean_body_mass", "kg", "mass"),
        # Read in centimetres so HealthKit does the conversion from its
        # canonical metres, and so Home Assistant can offer feet and inches.
        ("height", "cm", "distance"),
        ("body_temperature", "°C", "temperature"),
    ],
)
def test_the_shape_matches_the_sdk_annotation(metric, unit, unit_class):
    spec = METRICS[metric]
    assert spec.kind is BucketKind.HOURLY_DISCRETE
    assert spec.unit == unit
    assert spec.unit_class == unit_class
    # "Discrete (Arithmetic)" for all four: a mean, and no running sum.
    assert spec.mean_type is MeanType.ARITHMETIC
    assert not spec.has_sum


def test_recovery_is_arithmetic_where_heart_rate_is_not():
    """The one place a name could have decided this wrongly.

    Both are heart rates in count/min, so copying heart rate's shape would look
    obviously right. The header says otherwise, and the header is the source.
    """
    assert METRICS["heart_rate_recovery"].mean_type is MeanType.ARITHMETIC
    assert METRICS["heart_rate_recovery"].kind is BucketKind.HOURLY_DISCRETE


def test_every_unit_is_one_home_assistant_can_actually_convert():
    """A unit class naming a converter that does not accept the unit would be
    rejected by Home Assistant at import time, on a real installation only."""
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
    )

    for metric in PHASE_6B:
        spec = METRICS[metric]
        if spec.unit_class is None:
            assert spec.unit not in STATISTIC_UNIT_TO_UNIT_CONVERTER, (
                f"{metric} declares no unit class but {spec.unit} has a converter"
            )
            continue
        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER[spec.unit]
        assert converter.UNIT_CLASS == spec.unit_class


# --- transported, never interpreted ------------------------------------------


def test_a_temperature_is_carried_as_a_number_and_nothing_else():
    """No fever threshold, no classification, no clinical statement.

    The receiver's whole contribution is a unit and a device class, which buys
    a Fahrenheit conversion from Home Assistant rather than a judgement here.
    """
    from custom_components.apple_health_sync.payload import parse

    body = envelope(buckets={"hourly": [
        hour("body_temperature", mean=38.4, min=38.4, max=38.4),
    ]})
    payload = parse(body, now=NOW)

    assert not payload.rejected
    (bucket,) = payload.history.hourly
    assert bucket.metric == "body_temperature"
    assert bucket.mean == 38.4


def test_a_height_arrives_in_centimetres_and_is_not_rescaled():
    """182 must stay 182. A metres/centimetres slip is a factor of a hundred
    and would look entirely plausible in a converted display."""
    from custom_components.apple_health_sync.payload import parse

    body = envelope(buckets={"hourly": [
        hour("height", mean=182.0, min=182.0, max=182.0),
    ]})
    payload = parse(body, now=NOW)

    (bucket,) = payload.history.hourly
    assert bucket.mean == 182.0


def test_lean_body_mass_is_its_own_series_and_not_derived_from_body_fat():
    """A delivery carrying body mass and body fat produces no lean body mass.

    Apple stores lean body mass as its own type, reported by scales that
    measure composition. Computing it as mass x (1 - fat) would produce a
    number that disagrees with the Health app.
    """
    from custom_components.apple_health_sync.payload import parse

    body = envelope(buckets={"hourly": [
        hour("body_mass", mean=78.0, min=78.0, max=78.0),
        hour("body_fat_percentage", mean=18.0, min=18.0, max=18.0),
    ]})
    payload = parse(body, now=NOW)

    assert {b.metric for b in payload.history.hourly} == {
        "body_mass", "body_fat_percentage"
    }
    assert "lean_body_mass" not in payload.snapshot.measurements


# --- identity ----------------------------------------------------------------


def test_the_four_are_additive_and_nothing_moved():
    assert len(METRICS) == 34
    for metric in PHASE_6B:
        assert METRICS[metric].statistic_suffix == metric
        assert METRICS[metric].statistic_id == f"apple_health_sync:{metric}"


def test_the_four_are_published_as_supported():
    """A receiver that has them must say so, or a newer client withholds them."""
    for metric in PHASE_6B:
        assert metric in registry.SUPPORTED_METRICS


def test_each_has_its_own_snapshot_key():
    for metric in PHASE_6B:
        assert METRICS[metric].snapshot_key == metric

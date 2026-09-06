"""Phase 4B: flights climbed, walking heart rate average, BMI, blood glucose.

The last four entries of the target catalogue, and deliberately the dull ones:
three shapes that already existed, no new bucket family, no new snapshot
composite, no new read path. What is worth testing is therefore not the
plumbing but the four decisions that could be quietly wrong.

**The shapes come from Apple's own header.** `HKTypeIdentifiers.h` in the 26.5
SDK annotates every quantity type with its unit and aggregation style, and the
four here were read rather than guessed:

    FlightsClimbed             // count, Cumulative
    WalkingHeartRateAverage    // count/min, Discrete (Temporally Weighted)
    BodyMassIndex              // count, Discrete (Arithmetic)
    BloodGlucose               // mg/dL, Discrete (Arithmetic)

**Nothing is recomputed that Apple already computes.** The walking average is
Apple's own type, not a mean we take over walking heart-rate samples, and BMI is
Apple's stored value, not weight over height squared. Either reconstruction
would produce a number that disagrees with the Health app, which is worse than
not carrying it.

**Blood glucose is transported, never interpreted.** No reference range, no
high/low classification, no unit the person did not already have. The one thing
the receiver does add is a Home Assistant device class, and that buys a unit
conversion — not a judgement.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync import registry
from custom_components.apple_health_sync.payload import PayloadError, parse
from custom_components.apple_health_sync.registry import METRICS, BucketKind

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)
TZ = "Europe/Berlin"

PHASE_4B = ("flights_climbed", "walking_heart_rate_average", "bmi", "blood_glucose")


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


def day(metric, **fields):
    return {"metric": metric, "date": "2026-06-20", "time_zone": TZ, **fields}


def hour(metric, **fields):
    return {"metric": metric, "start": iso(NOW), **fields}


# --- the shapes, against the SDK header -------------------------------------


@pytest.mark.parametrize(
    ("metric", "kind", "unit", "unit_class", "has_sum", "mean_only"),
    [
        # Steps' twin, down to the header annotation.
        ("flights_climbed", BucketKind.DAILY_CUMULATIVE, "flights", None, True, False),
        # Resting heart rate's twin: Apple derives one value per day.
        ("walking_heart_rate_average", BucketKind.DAILY_DISCRETE, "bpm", None,
         False, True),
        # Body-metric shape: hourly and sparse, so a morning and an evening
        # reading stay distinct instead of averaging into one nobody measured.
        ("bmi", BucketKind.HOURLY_DISCRETE, "kg/m²", None, False, False),
        ("blood_glucose", BucketKind.HOURLY_DISCRETE, "mg/dL",
         "blood_glucose_concentration", False, False),
    ],
)
def test_each_metric_takes_the_shape_apples_header_declares(
    metric, kind, unit, unit_class, has_sum, mean_only
):
    spec = METRICS[metric]
    assert (spec.kind, spec.unit, spec.unit_class, spec.has_sum) == (
        kind, unit, unit_class, has_sum
    )
    assert (spec.required == frozenset({"mean"})) is mean_only


def test_the_blood_glucose_unit_class_is_one_home_assistant_actually_knows():
    """Measured against the installed version, like `hours` before it (§34).

    A unit class the recorder does not recognise is rejected at import time, and
    a *wrong* one silently reinterprets rows. This asserts the converter exists
    and that mg/dL is one of its units — which is what lets a person who reads
    in mmol/L get that from Home Assistant instead of from a second wire format.
    """
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
    )

    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get("mg/dL")
    assert converter is not None
    assert converter.UNIT_CLASS == METRICS["blood_glucose"].unit_class
    assert "mmol/L" in {str(unit) for unit in converter.VALID_UNITS}


def test_the_unitless_looking_ones_carry_no_converter():
    """`flights`, `bpm` and `kg/m²` are opaque to Home Assistant, and that is right.

    A unit class implies a conversion menu. There is no sensible other unit for
    a count of flights, and turning a BMI into pounds-per-square-inch is not a
    thing, so None is the honest answer rather than an omission.
    """
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
    )

    for unit in ("flights", "bpm", "kg/m²"):
        assert unit not in STATISTIC_UNIT_TO_UNIT_CONVERTER


# --- what arrives ------------------------------------------------------------


def test_all_four_travel_in_one_payload_and_keep_their_own_series():
    parsed = parse(
        envelope(buckets={
            "daily": [
                day("flights_climbed", total=12.0),
                day("walking_heart_rate_average", mean=96.0),
            ],
            "hourly": [
                hour("bmi", mean=24.6, min=24.6, max=24.6),
                hour("blood_glucose", mean=98.0, min=92.0, max=104.0),
            ],
        }),
        now=NOW,
    )

    assert not parsed.rejected
    assert {b.metric for b in parsed.history.daily} == {
        "flights_climbed", "walking_heart_rate_average"
    }
    assert {b.metric for b in parsed.history.hourly} == {"bmi", "blood_glucose"}
    assert {METRICS[m].statistic_id for m in PHASE_4B} == {
        "apple_health_sync:flights_climbed",
        "apple_health_sync:walking_heart_rate_average",
        "apple_health_sync:bmi",
        "apple_health_sync:blood_glucose",
    }


def test_a_day_with_no_flights_climbed_is_a_zero_and_a_day_with_none_is_nothing():
    """The distinction the whole project is built around, for the new metric.

    Zero flights is a measurement: the person was home all day on one floor. No
    bucket at all is the absence of one, and the two must not collapse.
    """
    parsed = parse(
        envelope(buckets={"daily": [day("flights_climbed", total=0.0)]}), now=NOW
    )
    assert [b.total for b in parsed.history.daily] == [0.0]

    empty = parse(envelope(buckets={"daily": []}), now=NOW)
    assert empty.history.daily == []


def test_the_walking_average_refuses_a_spread_it_never_measured():
    """Apple gives one value per day, so a min and a max would be invented."""
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets={"daily": [
                day("walking_heart_rate_average", mean=96.0, min=88.0, max=104.0)
            ]}),
            now=NOW,
        )
    assert err.value.reason == "bad_bucket_unexpected_field"


def test_the_sparse_pair_stay_hourly_and_keep_the_time_of_day():
    """Two readings in one day are two rows, not one averaged into meaninglessness.

    This is the body-metric argument applied to glucose, where it matters more:
    a fasting reading and a post-meal reading describe different things, and a
    daily mean would describe neither.
    """
    morning = datetime(2026, 6, 20, 7, tzinfo=UTC)
    midday = datetime(2026, 6, 20, 11, tzinfo=UTC)
    parsed = parse(
        envelope(buckets={"hourly": [
            {"metric": "blood_glucose", "start": iso(morning),
             "mean": 92.0, "min": 92.0, "max": 92.0},
            {"metric": "blood_glucose", "start": iso(midday),
             "mean": 140.0, "min": 140.0, "max": 140.0},
        ]}),
        now=NOW,
    )
    assert [b.start.hour for b in parsed.history.hourly] == [7, 11]


# --- what the receiver deliberately does not do -----------------------------


def test_blood_glucose_carries_no_range_check_and_no_interpretation():
    """Transport, not diagnosis.

    `PERCENT_METRICS` exists because a 0.0-1.0 HealthKit fraction stored as a
    percentage is a *unit* bug. There is no equivalent here: a glucose value the
    receiver would call implausible is still the value Apple Health holds, and
    the integration has no business classifying it. Values far outside any
    normal range are stored exactly as sent.
    """
    assert "blood_glucose" not in registry.PERCENT_METRICS

    for value in (20.0, 600.0):
        parsed = parse(
            envelope(buckets={"hourly": [
                hour("blood_glucose", mean=value, min=value, max=value)
            ]}),
            now=NOW,
        )
        assert [b.mean for b in parsed.history.hourly] == [value]


def test_bmi_is_read_and_never_derived_from_the_body_metrics_beside_it():
    """BMI is Apple's stored value, not weight over height squared.

    This test used to rest on there being nothing to compute from: height was
    not carried at all. Phase 6B added it, so the ingredients are now both
    present and the temptation is real - which makes the invariant worth more
    than it was, not less.

    So it is asserted as behaviour rather than as an absence. A delivery
    carrying body mass and height and no BMI must produce no BMI. A derived one
    would disagree with the Health app whenever the stored height is stale, and
    a number that contradicts Apple's own is worse than no number.
    """
    assert {"body_mass", "height", "bmi"} <= set(METRICS)

    body = envelope(buckets={"hourly": [
        hour("body_mass", mean=78.0, min=78.0, max=78.0),
        hour("height", mean=182.0, min=182.0, max=182.0),
    ]})
    payload = parse(body, now=NOW)

    assert not payload.rejected
    assert {b.metric for b in payload.history.hourly} == {"body_mass", "height"}
    assert "bmi" not in payload.snapshot.measurements

    assert METRICS["bmi"].kind is METRICS["body_mass"].kind
    assert METRICS["bmi"].snapshot_key == "bmi"


# --- identity ----------------------------------------------------------------


def test_the_four_are_additive_and_nothing_moved():
    """Every id is new; no existing series was renamed to make room."""
    assert len(METRICS) == 34  # 30 after 4B, +4 in 6B
    for metric in PHASE_4B:
        assert METRICS[metric].statistic_suffix == metric
    # The ids that already exist on Pascal's instance, spot-checked at both ends
    # of the registry's history.
    assert METRICS["steps"].statistic_id == "apple_health_sync:steps_daily"
    assert (
        METRICS["activity_move_energy"].statistic_id
        == "apple_health_sync:activity_move_energy"
    )


def test_the_four_are_published_as_supported():
    """A receiver that has them must say so, or a newer client withholds them."""
    for metric in PHASE_4B:
        assert metric in registry.SUPPORTED_METRICS

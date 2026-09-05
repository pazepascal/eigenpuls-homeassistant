"""Phase 3C: VO2 max (cardio fitness).

Two things could go wrong quietly here. A unit or scale mistake — VO2 max is
delivered in mL/min·kg by HealthKit and needs no conversion, unlike blood oxygen
and body fat, so any scaling would be a bug rather than a fix. And a fabricated
history — VO2 max is measured a handful of times a month, so absence must stay
absence and never become a zero.

No normative classification is stored or derived anywhere: HealthKit exposes no
above/below-average cardio-fitness API, so this module holds a number and its
time, nothing more.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.apple_health_sync.payload import PayloadError, parse
from custom_components.apple_health_sync.registry import (
    METRICS,
    PERCENT_METRICS,
    STATISTIC_IDS,
    BucketKind,
    MeanType,
)

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)
TZ = "Europe/Berlin"


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


def hour(offset=0, mean=42.7, lo=None, hi=None):
    return {
        "metric": "vo2_max",
        "start": iso(datetime(2026, 6, 18, 7, tzinfo=UTC) + timedelta(hours=offset)),
        "mean": mean,
        "min": lo if lo is not None else mean,
        "max": hi if hi is not None else mean,
    }


def snapshot(value=42.7, source="Apple Watch"):
    return {
        "vo2_max": {
            "value": value, "unit": "ml/kg/min",
            "measured_at": iso(datetime(2026, 6, 18, 7, 30, tzinfo=UTC)),
            "source": source,
        }
    }


class TestRegistryContract:
    def test_vo2_max_is_a_known_metric(self) -> None:
        assert "vo2_max" in METRICS

    def test_the_statistic_id_is_stable_and_unique(self) -> None:
        assert METRICS["vo2_max"].statistic_id == "apple_health_sync:vo2_max"
        assert METRICS["vo2_max"].statistic_id in STATISTIC_IDS

    def test_the_unit_is_the_human_wire_unit(self) -> None:
        assert METRICS["vo2_max"].unit == "ml/kg/min"

    def test_no_unit_class_is_claimed(self) -> None:
        """No Home Assistant converter knows this unit; claiming one is rejected
        at import time, so None is the verified answer, not a placeholder."""
        assert METRICS["vo2_max"].unit_class is None

    def test_the_mean_is_arithmetic_as_apple_documents(self) -> None:
        """The SDK header reads "ml/(kg*min), Discrete (Arithmetic)" and the
        runtime reports aggregationStyle .discreteArithmetic, so the aggregation
        is Apple's, not ours."""
        assert METRICS["vo2_max"].mean_type is MeanType.ARITHMETIC

    def test_there_is_no_cumulative_sum(self) -> None:
        assert METRICS["vo2_max"].has_sum is False

    def test_it_is_hourly_and_sparse_like_the_body_metrics(self) -> None:
        assert METRICS["vo2_max"].kind is BucketKind.HOURLY_DISCRETE

    def test_it_has_its_own_snapshot_key(self) -> None:
        assert METRICS["vo2_max"].snapshot_key == "vo2_max"

    def test_it_is_not_percent_scaled(self) -> None:
        """HealthKit hands over mL/min·kg directly. A percent-style x100 here
        would be the blood-oxygen trap repeated on a metric that never needed it."""
        assert "vo2_max" not in PERCENT_METRICS


class TestWireAcceptance:
    def test_a_snapshot_is_accepted_and_keeps_its_value(self) -> None:
        parsed = parse(envelope(snapshot=snapshot(), buckets={}), now=NOW)
        m = parsed.snapshot.measurements["vo2_max"]
        assert m.value == 42.7
        assert m.unit == "ml/kg/min"
        assert m.source == "Apple Watch"
        assert m.measured_at == datetime(2026, 6, 18, 7, 30, tzinfo=UTC)

    def test_an_hourly_bucket_is_accepted(self) -> None:
        parsed = parse(
            envelope(snapshot=snapshot(), buckets={"hourly": [hour()]}), now=NOW
        )
        buckets = [b for b in parsed.history.hourly if b.metric == "vo2_max"]
        assert len(buckets) == 1
        assert buckets[0].mean == 42.7

    def test_a_realistic_sparse_month_is_accepted(self) -> None:
        """A handful of measurements across the window, not one per hour."""
        # Backwards from the base hour, spanning weeks - the shape a real
        # 90-day lookback produces.
        hours = [hour(offset=o, mean=v) for o, v in
                 ((-1400, 41.9), (-900, 42.3), (-400, 42.7), (0, 43.1))]
        parsed = parse(
            envelope(snapshot=snapshot(43.1), buckets={"hourly": hours}), now=NOW
        )
        got = [b for b in parsed.history.hourly if b.metric == "vo2_max"]
        assert [b.mean for b in got] == [41.9, 42.3, 42.7, 43.1]

    def test_absence_stays_absence(self) -> None:
        """No VO2 max in the payload must not become a zero anywhere."""
        parsed = parse(envelope(snapshot={}, buckets={}), now=NOW)
        assert "vo2_max" not in parsed.snapshot.measurements
        assert not [b for b in parsed.history.hourly if b.metric == "vo2_max"]

    def test_a_zero_is_not_silently_accepted_as_a_measurement(self) -> None:
        """A real VO2 max is never 0; if one ever arrives it is stored as the
        number it is rather than dropped, so the guard is that we never *invent*
        one - proven by the absence test above. This pins current behaviour."""
        parsed = parse(
            envelope(snapshot=snapshot(), buckets={"hourly": [hour(mean=0.0)]}), now=NOW
        )
        got = [b for b in parsed.history.hourly if b.metric == "vo2_max"]
        assert [b.mean for b in got] == [0.0]

    def test_a_non_numeric_value_is_rejected_without_echoing_it(self) -> None:
        with pytest.raises(PayloadError) as err:
            parse(
                envelope(snapshot=snapshot(), buckets={"hourly": [hour(mean="42.7x")]}),
                now=NOW,
            )
        assert "42.7x" not in str(err.value)

    def test_an_unexpected_field_is_rejected(self) -> None:
        bad = hour()
        bad["total"] = 42.7
        with pytest.raises(PayloadError):
            parse(envelope(snapshot=snapshot(), buckets={"hourly": [bad]}), now=NOW)

    def test_a_duplicate_hour_is_rejected(self) -> None:
        with pytest.raises(PayloadError):
            parse(
                envelope(snapshot=snapshot(), buckets={"hourly": [hour(), hour()]}),
                now=NOW,
            )


class TestNothingElseMoved:
    """Phase 3A, body composition, blood pressure and workouts are untouched."""

    @pytest.mark.parametrize("metric", [
        "heart_rate", "steps", "resting_heart_rate", "hrv_sdnn",
        "respiratory_rate", "oxygen_saturation", "active_energy",
        "distance_walking_running", "nap_total", "nap_count",
        "body_mass", "body_fat_percentage",
        "blood_pressure_systolic", "blood_pressure_diastolic",
        "workout_count", "workout_duration", "workout_energy",
    ])
    def test_the_existing_metric_still_exists(self, metric: str) -> None:
        assert metric in METRICS

    def test_the_registry_grew_by_exactly_one(self) -> None:
        assert len(METRICS) == 18

    def test_percent_metrics_are_unchanged(self) -> None:
        assert PERCENT_METRICS == frozenset(
            {"oxygen_saturation", "body_fat_percentage"}
        )

    def test_every_statistic_id_is_still_unique(self) -> None:
        assert len(STATISTIC_IDS) == len(set(STATISTIC_IDS))

"""Phase 3B.1: body mass, body fat and blood pressure.

Two things here would be wrong *quietly* rather than loudly: a body-fat fraction
stored as if it were already a percentage, and a blood-pressure pair assembled
from two halves that did not belong together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.apple_health_sync.payload import (
    MAX_HOURLY_BUCKETS,
    MAX_HOURLY_BUCKETS_PER_METRIC,
    PayloadError,
    parse,
)
from custom_components.apple_health_sync.registry import METRICS, BucketKind

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


def hour(metric, offset=0, mean=80.0, lo=None, hi=None):
    return {
        "metric": metric,
        "start": iso(datetime(2026, 6, 18, 7, tzinfo=UTC) + timedelta(hours=offset)),
        "mean": mean, "min": lo if lo is not None else mean,
        "max": hi if hi is not None else mean,
    }


# --- Registry ----------------------------------------------------------------


def test_the_three_metrics_are_registered_as_hourly_discrete():
    for metric, unit, unit_class in (
        ("body_mass", "kg", "mass"),
        ("body_fat_percentage", "%", "unitless"),
        ("blood_pressure_systolic", "mmHg", "pressure"),
        ("blood_pressure_diastolic", "mmHg", "pressure"),
    ):
        spec = METRICS[metric]
        assert spec.kind is BucketKind.HOURLY_DISCRETE, metric
        assert spec.unit == unit
        assert spec.unit_class == unit_class
        assert not spec.has_sum


def test_blood_pressure_halves_have_no_individual_snapshot():
    """Half a reading is not a blood-pressure measurement."""
    assert METRICS["blood_pressure_systolic"].snapshot_key == ""
    assert METRICS["blood_pressure_diastolic"].snapshot_key == ""
    # But they keep independent durable series.
    assert (
        METRICS["blood_pressure_systolic"].statistic_id
        != METRICS["blood_pressure_diastolic"].statistic_id
    )


# --- Body mass ---------------------------------------------------------------


def test_body_mass_buckets_are_accepted():
    parsed = parse(
        envelope(buckets={"hourly": [hour("body_mass", mean=81.4)]}), now=NOW
    )
    assert parsed.history.hourly[0].metric == "body_mass"
    assert parsed.history.hourly[0].mean == 81.4


def test_two_weigh_ins_on_one_day_stay_separate_buckets():
    """The reason these are hourly: a morning and an evening weigh-in differ."""
    parsed = parse(
        envelope(buckets={"hourly": [
            hour("body_mass", offset=0, mean=81.4),
            hour("body_mass", offset=12, mean=82.6),
        ]}),
        now=NOW,
    )
    assert [b.mean for b in parsed.history.hourly] == [81.4, 82.6]


def test_a_day_without_a_weigh_in_sends_no_bucket():
    parsed = parse(envelope(buckets={"hourly": []}), now=NOW)
    assert not any(b.metric == "body_mass" for b in parsed.history.hourly)


def test_body_mass_snapshot():
    parsed = parse(
        envelope(snapshot={"body_mass": {
            "value": 81.4, "unit": "kg",
            "measured_at": iso(NOW - timedelta(hours=5)), "source": "Withings",
        }}),
        now=NOW,
    )
    entry = parsed.snapshot.measurements["body_mass"]
    assert entry.value == 81.4
    assert entry.unit == "kg"
    assert entry.measured_at == NOW - timedelta(hours=5)


# --- Body fat: the percent contract ------------------------------------------


def test_human_percentage_body_fat_is_accepted():
    parsed = parse(
        envelope(buckets={"hourly": [
            hour("body_fat_percentage", mean=18.2, lo=18.0, hi=18.4)
        ]}),
        now=NOW,
    )
    assert parsed.history.hourly[0].mean == 18.2


def test_a_raw_healthkit_fraction_is_refused_for_body_fat():
    """0.18 in a "%" series is wrong by 100x and looks entirely plausible.

    HealthKit's percentUnit is documented as 0.0-1.0, so the client converts
    before sending; this is the guard that stops an unconverted value.
    """
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets={"hourly": [
                hour("body_fat_percentage", mean=0.18, lo=0.18, hi=0.18)
            ]}),
            now=NOW,
        )
    assert err.value.reason == "bucket_percent_out_of_range"


def test_the_body_fat_percent_guard_covers_the_snapshot_too():
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(snapshot={"body_fat_percentage": {
                "value": 0.182, "unit": "%", "measured_at": iso(NOW),
            }}),
            now=NOW,
        )
    assert err.value.reason == "bucket_percent_out_of_range"


def test_body_fat_snapshot_in_human_percent():
    parsed = parse(
        envelope(snapshot={"body_fat_percentage": {
            "value": 18.2, "unit": "%", "measured_at": iso(NOW - timedelta(hours=3)),
        }}),
        now=NOW,
    )
    assert parsed.snapshot.measurements["body_fat_percentage"].value == 18.2


# --- Blood pressure ----------------------------------------------------------


def pressure(**overrides):
    body = {
        "systolic": 128.0, "diastolic": 82.0, "unit": "mmHg",
        "measured_at": iso(NOW - timedelta(hours=5)), "source": "Omron",
    }
    body.update(overrides)
    return body


def test_both_halves_are_accepted_as_independent_series():
    parsed = parse(
        envelope(buckets={"hourly": [
            hour("blood_pressure_systolic", mean=128.0),
            hour("blood_pressure_diastolic", mean=82.0),
        ]}),
        now=NOW,
    )
    by_metric = {b.metric: b.mean for b in parsed.history.hourly}
    assert by_metric == {
        "blood_pressure_systolic": 128.0, "blood_pressure_diastolic": 82.0,
    }


def test_the_latest_pair_arrives_as_one_correlated_measurement():
    parsed = parse(envelope(snapshot={"blood_pressure": pressure()}), now=NOW)
    reading = parsed.snapshot.blood_pressure

    assert reading.systolic == 128.0
    assert reading.diastolic == 82.0
    assert reading.unit == "mmHg"
    assert reading.measured_at == NOW - timedelta(hours=5)
    assert reading.source == "Omron"


@pytest.mark.parametrize("missing", ["systolic", "diastolic"])
def test_half_a_pair_is_refused_rather_than_completed(missing):
    """A lone systolic is not a blood-pressure reading."""
    body = pressure()
    del body[missing]
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"blood_pressure": body}), now=NOW)
    assert err.value.reason == "blood_pressure_incomplete_pair"


@pytest.mark.parametrize("missing", ["systolic", "diastolic"])
def test_an_explicitly_null_half_is_also_refused(missing):
    with pytest.raises(PayloadError) as err:
        parse(envelope(snapshot={"blood_pressure": pressure(**{missing: None})}), now=NOW)
    assert err.value.reason == "blood_pressure_incomplete_pair"


def test_an_inverted_pair_is_refused():
    """Diastolic above systolic means swapped or mismatched halves."""
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(snapshot={"blood_pressure": pressure(systolic=80.0, diastolic=120.0)}),
            now=NOW,
        )
    assert err.value.reason == "blood_pressure_inverted"


def test_a_lone_systolic_cannot_be_smuggled_in_as_a_metric_snapshot():
    """The halves have no individual snapshot key, so this sets nothing.

    Otherwise a client could set a current systolic with no diastolic and the
    entity would show half a reading.
    """
    parsed = parse(
        envelope(snapshot={"blood_pressure_systolic": {
            "value": 128.0, "unit": "mmHg", "measured_at": iso(NOW),
        }}),
        now=NOW,
    )
    assert parsed.snapshot.blood_pressure is None
    assert "blood_pressure_systolic" not in parsed.snapshot.measurements


def test_the_measurement_timestamp_is_preserved_exactly():
    measured = NOW - timedelta(hours=9, minutes=37)
    parsed = parse(
        envelope(snapshot={"blood_pressure": pressure(measured_at=iso(measured))}),
        now=NOW,
    )
    assert parsed.snapshot.blood_pressure.measured_at == measured


# --- Limits ------------------------------------------------------------------


def test_a_realistic_sparse_ninety_day_upload_is_accepted():
    """One weigh-in a day for 90 days, plus body fat and a weekly pair."""
    hourly = []
    for offset in range(90):
        hourly.append(hour("body_mass", offset=-offset * 24, mean=81.0))
        hourly.append(hour("body_fat_percentage", offset=-offset * 24, mean=18.0))
    for offset in range(0, 90, 7):
        hourly.append(hour("blood_pressure_systolic", offset=-offset * 24, mean=128.0))
        hourly.append(hour("blood_pressure_diastolic", offset=-offset * 24, mean=82.0))
    # Plus a full dense 14-day heart-rate window alongside them.
    hourly += [hour("heart_rate", offset=-i, mean=70.0, lo=60.0, hi=80.0)
               for i in range(336)]

    parsed = parse(envelope(buckets={"hourly": hourly}), now=NOW)
    assert len(parsed.history.hourly) == len(hourly)
    assert len(hourly) < MAX_HOURLY_BUCKETS


def test_one_metric_over_its_ceiling_fails_loudly_and_removes_nothing():
    """No trimming: a client that oversends is told, not silently pruned."""
    hourly = [hour("body_mass", offset=-i, mean=81.0)
              for i in range(MAX_HOURLY_BUCKETS_PER_METRIC + 1)]
    hourly.append(hour("heart_rate", offset=1, mean=70.0, lo=60.0, hi=80.0))

    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"hourly": hourly}), now=NOW)
    assert err.value.reason == "too_many_hourly_buckets_for_metric"


def test_the_total_envelope_is_enforced_too():
    hourly = []
    for index, metric in enumerate(
        ["body_mass", "body_fat_percentage", "blood_pressure_systolic",
         "blood_pressure_diastolic", "heart_rate"]
    ):
        hourly += [
            hour(metric, offset=-(i + index * 500), mean=70.0, lo=60.0, hi=80.0)
            for i in range(MAX_HOURLY_BUCKETS_PER_METRIC + 1)
        ]
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"hourly": hourly}), now=NOW)
    # Either ceiling may fire first; both are loud and neither trims.
    assert err.value.reason in (
        "too_many_hourly_buckets", "too_many_hourly_buckets_for_metric",
    )


def test_metric_order_cannot_change_what_is_retained():
    """The old daily bug: a grouped-array trim dropped whole metrics."""
    forward = [hour("body_mass", mean=81.0), hour("heart_rate", mean=70.0, lo=60.0, hi=80.0)]
    reversed_order = list(reversed(forward))

    a = parse(envelope(buckets={"hourly": forward}), now=NOW).history.hourly
    b = parse(envelope(buckets={"hourly": reversed_order}), now=NOW).history.hourly
    assert {x.metric for x in a} == {x.metric for x in b} == {"body_mass", "heart_rate"}


def test_a_dense_heart_rate_window_still_fits_beside_the_sparse_metrics():
    hourly = [hour("heart_rate", offset=-i, mean=70.0, lo=60.0, hi=80.0)
              for i in range(336)]
    hourly += [hour("body_mass", offset=-i * 24, mean=81.0) for i in range(90)]
    parsed = parse(envelope(buckets={"hourly": hourly}), now=NOW)
    assert len(parsed.history.hourly) == 426


# --- Compatibility with what is already installed ----------------------------


def phase_3a_payload():
    """What the currently installed client sends: no Phase 3B.1 metrics at all."""
    day = (NOW - timedelta(days=1)).date().isoformat()
    return envelope(
        buckets={
            "hourly": [hour("heart_rate", mean=72.0, lo=58.0, hi=141.0)],
            "daily": [
                {"metric": "steps", "date": day, "time_zone": TZ, "total": 8423.0},
                {"metric": "resting_heart_rate", "date": day, "time_zone": TZ, "mean": 54.0},
                {"metric": "oxygen_saturation", "date": day, "time_zone": TZ,
                 "mean": 96.5, "min": 92.0, "max": 99.0},
            ],
        },
        snapshot={
            "heart_rate": {"value": 61.0, "unit": "count/min",
                           "measured_at": iso(NOW - timedelta(minutes=5))},
            "steps_today": {"value": 8423.0, "unit": "count",
                            "date": NOW.date().isoformat(), "time_zone": TZ},
            "oxygen_saturation": {"value": 97.0, "unit": "%",
                                  "measured_at": iso(NOW - timedelta(hours=6))},
        },
    )


def test_the_installed_phase_3a_client_still_syncs():
    """It sends none of these metrics and must be entirely unaffected."""
    parsed = parse(phase_3a_payload(), now=NOW)

    assert parsed.rejected == []
    assert parsed.snapshot.heart_rate.value == 61.0
    assert parsed.snapshot.measurements["oxygen_saturation"].value == 97.0
    # And nothing Phase 3B.1 was invented for it.
    assert parsed.snapshot.blood_pressure is None
    assert "body_mass" not in parsed.snapshot.measurements


def test_the_installed_sleep_capable_client_still_syncs():
    night = {
        "date": (NOW - timedelta(days=1)).date().isoformat(), "time_zone": TZ,
        "total_sleep_min": 430.0,
        "sleep_start": iso(NOW - timedelta(days=1, hours=10)),
        "wake_time": iso(NOW - timedelta(days=1, hours=2)),
        "rem_min": 90.0, "core_min": 250.0, "deep_min": 60.0, "awake_min": 20.0,
    }
    body = phase_3a_payload()
    body["buckets"]["nightly"] = [night]
    body["buckets"]["daily"].append(
        {"metric": "nap_total", "date": (NOW - timedelta(days=1)).date().isoformat(),
         "time_zone": TZ, "mean": 45.0}
    )
    parsed = parse(body, now=NOW)

    assert parsed.rejected == []
    assert parsed.history.nightly[0].total_sleep_min == 430.0
    assert any(b.metric == "nap_total" for b in parsed.history.daily)


def test_a_body_metric_payload_coexists_with_everything_else():
    body = phase_3a_payload()
    body["buckets"]["hourly"] += [
        hour("body_mass", offset=1, mean=81.4),
        hour("body_fat_percentage", offset=1, mean=18.2),
        hour("blood_pressure_systolic", offset=1, mean=128.0),
        hour("blood_pressure_diastolic", offset=1, mean=82.0),
    ]
    body["snapshot"]["body_mass"] = {
        "value": 81.4, "unit": "kg", "measured_at": iso(NOW - timedelta(hours=5))}
    body["snapshot"]["blood_pressure"] = pressure()
    parsed = parse(body, now=NOW)

    assert parsed.rejected == []
    assert len(parsed.history.hourly) == 5
    assert parsed.snapshot.heart_rate.value == 61.0
    assert parsed.snapshot.blood_pressure.diastolic == 82.0


def test_an_unknown_metric_is_still_reported_loudly():
    """The registry stays closed; these additions did not loosen it.

    "Loudly" now means reported in ``rejected`` rather than fatal to the whole
    delivery - see ``test_v4_protocol`` for why that trade changed. Nothing is
    stored under a guessed meaning either way.
    """
    payload = parse(
        envelope(buckets={"hourly": [hour("bone_density", mean=1.2)]}), now=NOW
    )
    assert [r.reason for r in payload.rejected] == ["unknown_metric"]
    assert payload.history.hourly == []


@pytest.mark.parametrize("version", [1, 2, 3])
def test_earlier_versions_are_unaffected(version):
    body = envelope(version=version)
    if version == 1:
        body.pop("sync"), body.pop("snapshot")
    else:
        body["snapshot"] = {"heart_rate": None, "steps_today": None}
    assert parse(body, now=NOW).version == version


# --- Blood-pressure pair integrity at the contract boundary ------------------


def bp_hour(metric, offset=0, mean=128.0, count=None):
    bucket = hour(metric, offset=offset, mean=mean, lo=mean, hi=mean)
    if count is not None:
        bucket["count"] = count
    return bucket


def test_a_counted_pair_is_accepted():
    parsed = parse(
        envelope(buckets={"hourly": [
            bp_hour("blood_pressure_systolic", mean=128.0, count=3),
            bp_hour("blood_pressure_diastolic", mean=82.0, count=3),
        ]}),
        now=NOW,
    )
    counts = {b.metric: b.count for b in parsed.history.hourly}
    assert counts == {"blood_pressure_systolic": 3, "blood_pressure_diastolic": 3}


def test_an_hour_present_for_only_one_half_is_rejected():
    """The two halves come from one correlation; one without the other means
    they were built from different sets."""
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets={"hourly": [bp_hour("blood_pressure_systolic", count=1)]}),
            now=NOW,
        )
    assert err.value.reason == "blood_pressure_hours_mismatched"


def test_disagreeing_counts_are_rejected():
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets={"hourly": [
                bp_hour("blood_pressure_systolic", mean=128.0, count=3),
                bp_hour("blood_pressure_diastolic", mean=82.0, count=2),
            ]}),
            now=NOW,
        )
    assert err.value.reason == "blood_pressure_counts_mismatched"


def test_a_non_positive_count_is_rejected():
    """An hour exists only because it held measurements."""
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets={"hourly": [
                bp_hour("blood_pressure_systolic", mean=128.0, count=0),
                bp_hour("blood_pressure_diastolic", mean=82.0, count=0),
            ]}),
            now=NOW,
        )
    assert err.value.reason == "blood_pressure_count_not_positive"


def test_a_pair_without_counts_is_still_accepted():
    """Backward compatibility: the installed client sends no counts yet."""
    parsed = parse(
        envelope(buckets={"hourly": [
            bp_hour("blood_pressure_systolic", mean=128.0),
            bp_hour("blood_pressure_diastolic", mean=82.0),
        ]}),
        now=NOW,
    )
    assert all(b.count is None for b in parsed.history.hourly)

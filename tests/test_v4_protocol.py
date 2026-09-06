"""Wire format v4: the metric registry, bucket families and sleep semantics.

Two properties are load-bearing here and are asserted rather than assumed:

* an older receiver must never answer 200 while dropping health data, so a
  v3-only receiver has to reject a v4 payload outright; and
* a sleep stage that was not measured must stay null all the way through, since
  coercing it to zero would quietly bias every average built on top of it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.apple_health_sync import payload as payload_module
from custom_components.apple_health_sync.payload import (
    SUPPORTED_VERSIONS,
    WIRE_VERSION,
    PayloadError,
    parse,
)
from custom_components.apple_health_sync.registry import METRICS, BucketKind

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
TZ = "Europe/Berlin"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def envelope(**overrides):
    body = {
        "version": 4,
        "type": "sync",
        "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True},
        "snapshot": {},
    }
    body.update(overrides)
    return body


def hour(metric="heart_rate", offset=0, mean=72.0, lo=58.0, hi=141.0):
    return {
        "metric": metric,
        "start": iso(datetime(2026, 6, 18, 3, tzinfo=UTC) + timedelta(hours=offset)),
        "mean": mean, "min": lo, "max": hi,
    }


def day(metric="steps", offset=0, **fields):
    bucket = {
        "metric": metric,
        "date": (date(2026, 6, 18) + timedelta(days=offset)).isoformat(),
        "time_zone": TZ,
    }
    bucket.update(fields)
    return bucket


def night(offset=0, **fields):
    wake_date = date(2026, 6, 18) + timedelta(days=offset)
    record = {
        "date": wake_date.isoformat(),
        "time_zone": TZ,
        "total_sleep_min": 430.0,
        "sleep_start": iso(datetime(2026, 6, 17, 21, 30, tzinfo=UTC)
                           + timedelta(days=offset)),
        "wake_time": iso(datetime(2026, 6, 18, 5, 10, tzinfo=UTC)
                         + timedelta(days=offset)),
        "rem_min": 90.0, "core_min": 250.0, "deep_min": 60.0, "awake_min": 25.0,
    }
    record.update(fields)
    return record


def buckets(hourly=None, daily=None, nightly=None):
    out = {}
    if hourly is not None:
        out["hourly"] = hourly
    if daily is not None:
        out["daily"] = daily
    if nightly is not None:
        out["nightly"] = nightly
    return out


# --- 1 / 2: version acceptance ----------------------------------------------


def test_v4_is_current_and_every_earlier_version_stays_supported():
    assert WIRE_VERSION == 4
    assert SUPPORTED_VERSIONS == frozenset({1, 2, 3, 4})


def test_a_full_v4_payload_is_accepted():
    body = envelope(
        buckets=buckets(
            hourly=[hour()],
            daily=[
                day("steps", total=8423.0),
                day("resting_heart_rate", mean=54.0),
                day("hrv_sdnn", mean=44.0, min=21.0, max=88.0),
                day("respiratory_rate", mean=14.2, min=12.0, max=17.0),
                day("oxygen_saturation", mean=96.5, min=92.0, max=99.0),
                day("active_energy", total=612.0),
                day("distance_walking_running", total=7.4),
            ],
            nightly=[night()],
        )
    )
    parsed = parse(body, now=NOW)

    assert parsed.version == 4
    assert len(parsed.history.daily) == 7
    assert len(parsed.history.hourly) == 1
    assert len(parsed.history.nightly) == 1


@pytest.mark.parametrize("version", [1, 2, 3])
def test_earlier_versions_are_still_accepted(version):
    body = envelope(version=version)
    if version == 1:
        body.pop("sync"), body.pop("snapshot")
    else:
        body["snapshot"] = {"heart_rate": None, "steps_today": None}
    assert parse(body, now=NOW).version == version


# --- 3: a v3-only receiver must refuse v4 -----------------------------------


def test_a_v3_only_receiver_rejects_a_v4_payload(monkeypatch):
    """The contract that makes the version bump worth making.

    Modelled by restoring the version set a v3 receiver actually shipped with. A
    v3 receiver offered no storage for nightly sleep or for any metric beyond
    heart rate and steps, so accepting the payload would mean answering 200 while
    discarding it - the silent loss the bump exists to prevent.
    """
    monkeypatch.setattr(payload_module, "SUPPORTED_VERSIONS", frozenset({1, 2, 3}))
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[night()])), now=NOW)
    assert err.value.reason == "unsupported_version"


def test_a_v3_payload_cannot_smuggle_v4_bucket_keys():
    """Otherwise a v3 envelope could carry sleep that a v3 reader never stores."""
    body = envelope(version=3, snapshot={"heart_rate": None, "steps_today": None},
                    buckets=buckets(nightly=[night()]))
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "unknown_bucket_kind"


def test_a_v4_payload_cannot_use_the_v3_bucket_keys():
    body = envelope(buckets={"heart_rate_hourly": [hour()]})
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "unknown_bucket_kind"


# --- 4 / 5 / 6: the registry is a closed contract ---------------------------


def test_an_unknown_metric_is_reported_not_ignored():
    """Still never silent - but no longer fatal.

    The original rule was "reject rather than ignore", and the point of it was
    that a metric this receiver does not understand must never be quietly
    dropped. That point is preserved: the bucket comes back in ``rejected`` and
    the client surfaces the count.

    What changed is that it no longer takes the rest of the delivery with it. A
    newer client naming a metric this version has not learned yet is version
    skew, not a protocol violation, and an App Store update reaches a phone long
    before a HACS update reaches the instance behind it.
    """
    body = envelope(buckets=buckets(daily=[day("not_a_metric", total=5.4)]))
    payload = parse(body, now=NOW)

    assert [r.reason for r in payload.rejected] == ["unknown_metric"]
    assert payload.rejected[0].collection == "daily"
    assert payload.history.daily == []


def test_an_unknown_metric_in_the_hourly_family_is_also_reported():
    body = envelope(buckets=buckets(hourly=[hour(metric="body_temperature")]))
    payload = parse(body, now=NOW)

    assert [r.reason for r in payload.rejected] == ["unknown_metric"]
    assert payload.rejected[0].collection == "hourly"
    assert payload.history.hourly == []


def test_a_known_metric_survives_an_unknown_one_beside_it():
    """The whole reason the rule changed.

    Before this, one unrecognised name cost every other metric in the delivery.
    """
    body = envelope(
        buckets=buckets(
            daily=[day("steps", total=1000), day("not_a_metric", total=5.4)]
        )
    )
    payload = parse(body, now=NOW)

    assert [b.metric for b in payload.history.daily] == ["steps"]
    assert [r.reason for r in payload.rejected] == ["unknown_metric"]


def test_a_malformed_bucket_is_still_fatal():
    """Only an unknown *metric* is tolerated. Everything else is unchanged.

    A missing metric key is a malformed request, not version skew, and the
    strictness that catches it must not be loosened by association.
    """
    body = envelope(buckets=buckets(daily=[{"date": "2026-05-29", "total": 1}]))
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bad_bucket_missing_field"


def test_a_daily_metric_sent_as_an_hourly_bucket_is_rejected():
    body = envelope(buckets=buckets(hourly=[hour(metric="hrv_sdnn")]))
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "wrong_bucket_kind"


def test_an_hourly_metric_sent_as_a_daily_bucket_is_rejected():
    body = envelope(
        buckets=buckets(daily=[day("heart_rate", mean=70.0, min=60.0, max=80.0)])
    )
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "wrong_bucket_kind"


@pytest.mark.parametrize(
    ("metric", "fields"),
    [
        ("steps", {}),                                   # no total
        ("active_energy", {"mean": 400.0}),              # cumulative sent as discrete
        ("hrv_sdnn", {"mean": 44.0}),                    # no min/max
        ("resting_heart_rate", {}),                      # no mean
        ("oxygen_saturation", {"mean": 96.0, "min": 92.0}),  # no max
    ],
)
def test_missing_required_fields_are_rejected(metric, fields):
    body = envelope(buckets=buckets(daily=[day(metric, **fields)]))
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason in ("bad_bucket_missing_field", "bad_bucket_unexpected_field")


def test_resting_heart_rate_refuses_an_invented_spread():
    """Apple derives one resting value per day; a min/max would be fabricated."""
    body = envelope(
        buckets=buckets(
            daily=[day("resting_heart_rate", mean=54.0, min=48.0, max=61.0)]
        )
    )
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bad_bucket_unexpected_field"


def test_every_registered_metric_has_coherent_metadata():
    for metric, spec in METRICS.items():
        assert spec.statistic_id.startswith("apple_health_sync:"), metric
        assert spec.unit, metric
        assert spec.required, metric
        if spec.kind is BucketKind.DAILY_CUMULATIVE:
            assert spec.has_sum and spec.required == frozenset({"total"}), metric
        else:
            assert not spec.has_sum and "mean" in spec.required, metric


def test_statistic_ids_are_unique():
    ids = [spec.statistic_id for spec in METRICS.values()]
    assert len(set(ids)) == len(ids)


# --- 7 / 8: null is not zero -------------------------------------------------


def test_absent_sleep_stages_stay_null():
    """A night tracked without a Watch has a real total and no staging at all."""
    record = night()
    for stage in ("rem_min", "core_min", "deep_min", "awake_min"):
        record.pop(stage)
    parsed = parse(envelope(buckets=buckets(nightly=[record])), now=NOW)
    stored = parsed.history.nightly[0]

    assert stored.total_sleep_min == 430.0
    assert stored.rem_min is None
    assert stored.core_min is None
    assert stored.deep_min is None
    assert stored.awake_min is None


def test_explicit_null_sleep_stages_stay_null():
    record = night(rem_min=None, deep_min=None)
    stored = parse(envelope(buckets=buckets(nightly=[record])), now=NOW).history.nightly[0]
    assert stored.rem_min is None
    assert stored.deep_min is None
    assert stored.core_min == 250.0


def test_a_zero_stage_is_kept_distinct_from_a_missing_one():
    """Zero REM is a measurement; missing REM is not. They must not collapse."""
    measured = parse(
        envelope(buckets=buckets(nightly=[night(rem_min=0.0)])), now=NOW
    ).history.nightly[0]
    absent = parse(
        envelope(buckets=buckets(nightly=[night(rem_min=None)])), now=NOW
    ).history.nightly[0]

    assert measured.rem_min == 0.0
    assert absent.rem_min is None
    assert measured.rem_min is not None
    assert measured.rem_min != absent.rem_min


# --- Nightly invariants ------------------------------------------------------


def test_waking_before_falling_asleep_is_rejected():
    record = night(
        sleep_start=iso(datetime(2026, 6, 18, 6, tzinfo=UTC)),
        wake_time=iso(datetime(2026, 6, 18, 5, tzinfo=UTC)),
    )
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[record])), now=NOW)
    assert err.value.reason == "nightly_wake_before_sleep"


def test_total_sleep_cannot_exceed_the_time_in_bed():
    record = night(total_sleep_min=900.0)
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[record])), now=NOW)
    assert err.value.reason == "nightly_total_exceeds_span"


def test_stages_cannot_exceed_the_total():
    record = night(rem_min=300.0, core_min=300.0, deep_min=100.0)
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[record])), now=NOW)
    assert err.value.reason == "nightly_stages_exceed_total"


def test_partial_staging_below_the_total_is_accepted():
    """Unspecified sleep makes up the remainder; this is normal, not an error."""
    record = night(rem_min=40.0, core_min=50.0, deep_min=10.0)
    stored = parse(envelope(buckets=buckets(nightly=[record])), now=NOW).history.nightly[0]
    assert stored.total_sleep_min == 430.0
    assert stored.rem_min == 40.0


def test_a_negative_stage_is_rejected():
    with pytest.raises(PayloadError):
        parse(envelope(buckets=buckets(nightly=[night(deep_min=-5.0)])), now=NOW)


def test_two_summaries_for_one_night_are_rejected():
    body = envelope(buckets=buckets(nightly=[night(), night()]))
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "duplicate_nightly_bucket"


def test_the_same_metric_and_day_cannot_appear_twice():
    body = envelope(
        buckets=buckets(daily=[day("steps", total=1.0), day("steps", total=2.0)])
    )
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "duplicate_daily_bucket"


def test_the_same_day_for_different_metrics_is_fine():
    body = envelope(
        buckets=buckets(daily=[day("steps", total=1.0), day("active_energy", total=2.0)])
    )
    assert len(parse(body, now=NOW).history.daily) == 2


def test_an_unresolvable_time_zone_is_rejected():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[night(time_zone="Mars/Olympus")])), now=NOW)
    assert err.value.reason == "bad_bucket_time_zone"


def test_a_wake_date_far_from_the_wake_instant_is_rejected():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[night(date="2026-06-01")])), now=NOW)
    assert err.value.reason == "nightly_date_mismatch"


# --- Naps --------------------------------------------------------------------


def test_naps_travel_as_daily_metrics_not_nightly_fields():
    """Naps belong to the calendar day, so they are ordinary daily metrics."""
    parsed = parse(
        envelope(
            buckets=buckets(
                daily=[day("nap_total", mean=35.0), day("nap_count", mean=2)],
                nightly=[night()],
            )
        ),
        now=NOW,
    )
    naps = {b.metric: b.mean for b in parsed.history.daily}
    assert naps == {"nap_total": 35.0, "nap_count": 2}

    # The night is main sleep only and carries no nap data at all.
    stored = parsed.history.nightly[0]
    assert not hasattr(stored, "nap_total_min")
    assert stored.total_sleep_min == 430.0
    assert (stored.rem_min, stored.core_min, stored.deep_min) == (90.0, 250.0, 60.0)


def test_naps_are_accepted_on_a_date_with_no_nightly_record():
    """The whole point of the move: a day of naps and no main sleep is real."""
    parsed = parse(
        envelope(buckets=buckets(daily=[day("nap_total", mean=90.0), day("nap_count", mean=2)])),
        now=NOW,
    )
    assert len(parsed.history.daily) == 2
    assert parsed.history.nightly == []


def test_a_main_night_is_accepted_without_any_nap_fields():
    stored = parse(envelope(buckets=buckets(nightly=[night()])), now=NOW).history.nightly[0]
    assert stored.total_sleep_min == 430.0


@pytest.mark.parametrize("field", ["nap_total_min", "nap_count"])
def test_nightly_nap_fields_are_rejected_not_ignored(field):
    """A client still sending naps on the night must fail loudly.

    Accepting the payload and dropping the field would answer 200 while losing a
    day's naps - exactly the silent loss the version contract exists to prevent.
    """
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=[night(**{field: 30.0})])), now=NOW)
    assert err.value.reason == "nightly_nap_fields_moved"


def test_a_day_without_naps_sends_no_nap_bucket():
    """Absence, not a fabricated zero: no nap measured is not "zero naps"."""
    parsed = parse(envelope(buckets=buckets(nightly=[night()])), now=NOW)
    assert not any(b.metric.startswith("nap") for b in parsed.history.daily)


def test_a_measured_zero_nap_total_is_still_a_measurement():
    parsed = parse(
        envelope(buckets=buckets(daily=[day("nap_total", mean=0.0)])), now=NOW
    )
    assert parsed.history.daily[0].mean == 0.0


# --- 11 / 12: existing semantics are untouched ------------------------------


def test_heart_rate_hourly_semantics_are_identical_under_v3_and_v4():
    v3_body = envelope(
        version=3, snapshot={"heart_rate": None, "steps_today": None},
        buckets={"heart_rate_hourly": [{k: v for k, v in hour().items() if k != "metric"}]},
    )
    v4_body = envelope(buckets=buckets(hourly=[hour()]))

    assert parse(v3_body, now=NOW).history.hourly == parse(v4_body, now=NOW).history.hourly


def test_steps_cumulative_semantics_are_identical_under_v3_and_v4():
    raw = day("steps", total=8423.0)
    v3_body = envelope(
        version=3, snapshot={"heart_rate": None, "steps_today": None},
        buckets={"steps_daily": [{k: v for k, v in raw.items() if k != "metric"}]},
    )
    v4_body = envelope(buckets=buckets(daily=[raw]))

    v3_daily = parse(v3_body, now=NOW).history.daily
    assert v3_daily == parse(v4_body, now=NOW).history.daily
    assert v3_daily[0].metric == "steps"
    assert v3_daily[0].total == 8423.0
    # Discrete fields stay unset for a cumulative metric.
    assert v3_daily[0].mean is None


def test_hour_alignment_is_still_enforced_under_v4():
    bucket = hour()
    bucket["start"] = iso(datetime(2026, 6, 18, 3, 30, tzinfo=UTC))
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(hourly=[bucket])), now=NOW)
    assert err.value.reason == "bucket_start_not_hour_aligned"


def test_an_inconsistent_range_is_still_rejected_under_v4():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(hourly=[hour(mean=200.0)])), now=NOW)
    assert err.value.reason == "bucket_range_inconsistent"


# --- 13: the SpO2 percentage contract ---------------------------------------


def test_a_healthkit_fraction_is_refused_for_blood_oxygen():
    """0.98 stored in a "%" series is wrong by 100x and looks plausible.

    HealthKit's percentUnit is documented as 0.0-1.0, so the client converts
    before sending. This is the guard that keeps an unconverted value from being
    stored silently.
    """
    body = envelope(
        buckets=buckets(
            daily=[day("oxygen_saturation", mean=0.98, min=0.92, max=0.99)]
        )
    )
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bucket_percent_out_of_range"


def test_human_percent_blood_oxygen_is_accepted():
    body = envelope(
        buckets=buckets(
            daily=[day("oxygen_saturation", mean=96.5, min=92.0, max=99.0)]
        )
    )
    stored = parse(body, now=NOW).history.daily[0]
    assert stored.mean == 96.5


def test_an_impossible_percentage_is_refused():
    body = envelope(
        buckets=buckets(
            daily=[day("oxygen_saturation", mean=140.0, min=92.0, max=150.0)]
        )
    )
    with pytest.raises(PayloadError):
        parse(body, now=NOW)


def test_the_percent_guard_applies_to_the_snapshot_too():
    body = envelope(snapshot={"oxygen_saturation": {
        "value": 0.97, "unit": "%", "measured_at": iso(NOW - timedelta(hours=2))
    }})
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bucket_percent_out_of_range"


# --- Snapshot ----------------------------------------------------------------


def test_the_v4_snapshot_carries_the_new_metrics():
    body = envelope(snapshot={
        "heart_rate": {"value": 61.0, "unit": "bpm",
                       "measured_at": iso(NOW - timedelta(minutes=5))},
        "resting_heart_rate": {"value": 54.0, "unit": "bpm",
                               "measured_at": iso(NOW - timedelta(hours=6))},
        "oxygen_saturation": {"value": 97.0, "unit": "%",
                              "measured_at": iso(NOW - timedelta(hours=7))},
        "active_energy_today": {"value": 612.0, "unit": "kcal",
                                "date": "2026-06-20", "time_zone": TZ},
        "distance_today": {"value": 7.4, "unit": "km",
                           "date": "2026-06-20", "time_zone": TZ},
        "sleep_last_night": night(offset=2),
        "sleep_7d": {"nights": 7, "avg_total_min": 421.0, "avg_rem_min": 88.0,
                     "avg_deep_min": None, "sleep_start_stddev_min": 31.5,
                     "nights_by_field": {"avg_total_min": 7, "avg_rem_min": 5}},
    })
    snapshot = parse(body, now=NOW).snapshot

    assert snapshot.heart_rate.value == 61.0
    assert snapshot.measurements["resting_heart_rate"].value == 54.0
    assert snapshot.measurements["oxygen_saturation"].value == 97.0
    assert snapshot.daily_totals["active_energy"].value == 612.0
    assert snapshot.daily_totals["distance_walking_running"].value == 7.4
    assert snapshot.sleep_last_night.total_sleep_min == 430.0
    assert snapshot.sleep_7d.nights == 7
    # A stage with no contributing night is None, never 0.
    assert snapshot.sleep_7d.avg_deep_min is None
    assert snapshot.sleep_7d.nights_by_field["avg_rem_min"] == 5


def test_an_absent_snapshot_metric_leaves_that_sensor_alone():
    snapshot = parse(envelope(snapshot={}), now=NOW).snapshot
    assert snapshot.measurements == {}
    assert snapshot.daily_totals == {}
    assert snapshot.sleep_last_night is None
    assert snapshot.sleep_7d is None


def test_a_trend_claiming_more_contributing_nights_than_it_has_is_rejected():
    body = envelope(snapshot={
        "sleep_7d": {"nights": 3, "avg_total_min": 400.0,
                     "nights_by_field": {"avg_total_min": 7}},
    })
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bad_sleep_trend_nights_by_field"


# --- Limits ------------------------------------------------------------------


def test_the_daily_ceiling_accommodates_every_metric_across_the_widest_window():
    """Seven daily metrics over a 14-day recovery window is a legitimate payload."""
    def fields(metric):
        spec = METRICS[metric]
        if spec.has_sum:
            return {"total": 1.0}
        if spec.required == frozenset({"mean"}):
            return {"mean": 50.0}
        return {"mean": 50.0, "min": 40.0, "max": 60.0}

    daily = [
        day(metric, offset=offset, **fields(metric))
        for metric in METRICS
        if METRICS[metric].kind is not BucketKind.HOURLY_DISCRETE
        for offset in range(-13, 1)
    ]
    # Twelve daily metrics: naps and the three training aggregates included.
    # Twenty daily metrics now, the eight Activity series included. 280 of a
    # 400 ceiling - still headroom, and worth watching as Phase 4 continues.
    assert len(daily) == 308
    assert len(parse(envelope(buckets=buckets(daily=daily)), now=NOW).history.daily) == 308


def test_too_many_nights_are_refused():
    nights = [night(offset=-i) for i in range(60)]
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(nightly=nights)), now=NOW)
    assert err.value.reason == "too_many_nightly_buckets"


# --- Registry-driven field reading ------------------------------------------


def test_optional_min_max_would_be_read_not_silently_dropped(monkeypatch):
    """Guards the "a new metric is just a registry entry" promise.

    Reading only the *required* fields would let a metric that declares min/max
    as optional pass validation and then lose those values on the way to
    storage — accepted with a 200, stored incomplete.
    """
    import dataclasses

    from custom_components.apple_health_sync import registry as registry_module

    relaxed = dataclasses.replace(
        METRICS["hrv_sdnn"],
        required=frozenset({"mean"}),
        optional=frozenset({"min", "max", "count"}),
    )
    monkeypatch.setitem(registry_module.METRICS, "hrv_sdnn", relaxed)

    # Omitted: accepted, and absent rather than zero.
    without = parse(
        envelope(buckets=buckets(daily=[day("hrv_sdnn", mean=44.0)])), now=NOW
    ).history.daily[0]
    assert without.mean == 44.0
    assert without.minimum is None

    # Supplied: actually carried through.
    with_spread = parse(
        envelope(buckets=buckets(daily=[day("hrv_sdnn", mean=44.0, min=21.0, max=88.0)])),
        now=NOW,
    ).history.daily[0]
    assert (with_spread.minimum, with_spread.maximum) == (21.0, 88.0)

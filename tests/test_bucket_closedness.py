"""Buckets carry only keys this receiver implements.

The gap this closes was real and shipped: a field the registry knows but does not
permit for a metric was rejected, while an entirely unknown key was accepted and
dropped. Both are the same failure from the sender's side - it believes a
measurement was stored - and the protocol has answered that failure the same way
since v2: refuse, visibly, rather than answer 200 and lose data.

The two reason codes stay distinct on purpose. ``bad_bucket_unexpected_field``
means "this receiver stores that field, but not for this metric";
``bad_bucket_unknown_field`` means "nothing here reads that at all". A client
author needs to tell those apart.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.apple_health_sync.payload import PayloadError, parse

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


def hour(metric="heart_rate", **fields):
    bucket = {
        "metric": metric,
        "start": iso(datetime(2026, 6, 18, 3, tzinfo=UTC)),
        "mean": 72.0, "min": 58.0, "max": 141.0,
    }
    bucket.update(fields)
    return bucket


def day(metric="steps", **fields):
    """A daily bucket. `total` belongs to the cumulative family only."""
    bucket = {"metric": metric, "date": "2026-06-18", "time_zone": TZ}
    if metric == "steps":
        bucket["total"] = 8000.0
    bucket.update(fields)
    return bucket


def night(**fields):
    record = {
        "date": "2026-06-18",
        "time_zone": TZ,
        "total_sleep_min": 430.0,
        "sleep_start": iso(datetime(2026, 6, 17, 21, 30, tzinfo=UTC)),
        "wake_time": iso(datetime(2026, 6, 18, 5, 10, tzinfo=UTC)),
        "rem_min": 90.0, "core_min": 250.0, "deep_min": 60.0, "awake_min": 25.0,
    }
    record.update(fields)
    return record


# --- an unknown key is refused, in every family ------------------------------


@pytest.mark.parametrize(
    ("buckets", "reason"),
    [
        ({"hourly": [hour(surprise=1)]}, "bad_bucket_unknown_field"),
        ({"hourly": [hour("blood_pressure_systolic", mean=118.0, min=110.0,
                          max=126.0, count=2, surprise=1)]},
         "bad_bucket_unknown_field"),
        ({"daily": [day(surprise=1)]}, "bad_bucket_unknown_field"),
        ({"daily": [day("hrv_sdnn", mean=44.0, min=21.0, max=88.0, surprise=1)]},
         "bad_bucket_unknown_field"),
        ({"daily": [day("resting_heart_rate", mean=54.0, surprise=1)]},
         "bad_bucket_unknown_field"),
        ({"nightly": [night(surprise=1)]}, "bad_nightly_unknown_field"),
    ],
    ids=["hourly", "hourly-bp", "daily-cumulative", "daily-discrete",
         "daily-mean-only", "nightly"],
)
def test_an_unknown_key_is_rejected_in_every_bucket_family(buckets, reason):
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets), now=NOW)
    assert err.value.reason == reason


def test_a_typo_in_a_field_name_is_rejected_rather_than_dropped():
    """The realistic case: `maen` looks like a value that was stored. It wasn't."""
    bucket = hour()
    bucket["maen"] = bucket.pop("mean")
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"hourly": [bucket]}), now=NOW)
    assert err.value.reason in (
        "bad_bucket_missing_field", "bad_bucket_unknown_field"
    )


# --- what must keep working --------------------------------------------------


def test_every_permitted_field_is_still_accepted():
    body = envelope(
        buckets={
            "hourly": [hour(count=61)],
            "daily": [
                day(),
                day("hrv_sdnn", mean=44.0, min=21.0, max=88.0),
                day("resting_heart_rate", mean=54.0),
            ],
            "nightly": [night()],
        }
    )
    parsed = parse(body, now=NOW)
    assert len(parsed.history.hourly) == 1
    assert len(parsed.history.daily) == 3
    assert len(parsed.history.nightly) == 1


def test_a_known_field_the_metric_does_not_permit_keeps_its_own_reason():
    """Distinct from unknown: the receiver does store min, just not for this one."""
    bucket = day("resting_heart_rate", mean=54.0, min=48.0)
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"daily": [bucket]}), now=NOW)
    assert err.value.reason == "bad_bucket_unexpected_field"


def test_the_moved_nap_fields_keep_their_own_reason():
    """"Unknown" would be true but useless; the sender needs to know they moved."""
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets={"nightly": [night(nap_total_min=45.0)]}), now=NOW)
    assert err.value.reason == "nightly_nap_fields_moved"


# --- older versions ----------------------------------------------------------


def v3_envelope(**overrides):
    body = {
        "version": 3,
        "type": "sync",
        "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True},
        "snapshot": {},
    }
    body.update(overrides)
    return body


def test_a_valid_v3_payload_is_still_accepted():
    body = v3_envelope(
        buckets={
            "heart_rate_hourly": [
                {"start": iso(datetime(2026, 6, 18, 3, tzinfo=UTC)),
                 "mean": 72.0, "min": 58.0, "max": 141.0, "count": 61}
            ],
            "steps_daily": [
                {"date": (date(2026, 6, 18)).isoformat(), "time_zone": TZ,
                 "total": 8000.0}
            ],
        }
    )
    parsed = parse(body, now=NOW)
    assert len(parsed.history.hourly) == 1
    assert len(parsed.history.daily) == 1


def test_a_v3_bucket_carrying_the_v4_metric_key_is_rejected():
    """The same rule the compatibility table already states for bucket kinds."""
    body = v3_envelope(
        buckets={
            "heart_rate_hourly": [
                {"metric": "heart_rate",
                 "start": iso(datetime(2026, 6, 18, 3, tzinfo=UTC)),
                 "mean": 72.0, "min": 58.0, "max": 141.0}
            ]
        }
    )
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "bad_bucket_unknown_field"


def test_a_v1_payload_carrying_only_samples_is_untouched():
    body = {
        "version": 1,
        "type": "sync",
        "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "samples": [
            {
                "metric": "heart_rate",
                "uuid": "11111111-1111-1111-1111-111111111111",
                "start": iso(NOW - timedelta(minutes=5)),
                "end": iso(NOW - timedelta(minutes=5)),
                "value": 71.0,
                "unit": "count/min",
                "source": "Apple Watch",
            }
        ],
    }
    assert parse(body, now=NOW).samples

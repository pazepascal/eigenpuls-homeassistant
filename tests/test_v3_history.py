"""Wire format v3: aggregate history parsing and validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.apple_health_sync.payload import (
    MAX_DAILY_BUCKETS,
    MAX_HOURLY_BUCKETS,
    MAX_HOURLY_BUCKETS_PER_METRIC,
    SUPPORTED_VERSIONS,
    WIRE_VERSION,
    PayloadError,
    parse,
)

NOW = datetime(2026, 9, 3, 21, 0, 0, tzinfo=UTC)
HOUR = datetime(2026, 9, 3, 14, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def hourly(start=HOUR, mean=72.4, lo=58.0, hi=141.0, count=37):
    item = {"start": iso(start), "mean": mean, "min": lo, "max": hi}
    if count is not None:
        item["count"] = count
    return item


def daily(day="2026-09-03", tz="Europe/Berlin", total=8423):
    return {"date": day, "time_zone": tz, "total": total}


def envelope(**overrides):
    body = {
        "version": 3,
        "type": "sync",
        "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True},
        "snapshot": {"heart_rate": None, "steps_today": None},
    }
    body.update(overrides)
    return body


def buckets(h=None, d=None):
    return {"heart_rate_hourly": h if h is not None else [], "steps_daily": d or []}


# --- Version gate -----------------------------------------------------------


def test_v4_is_the_current_version_and_all_four_are_supported():
    assert WIRE_VERSION == 4
    assert SUPPORTED_VERSIONS == frozenset({1, 2, 3, 4})


# 4 is a supported version now; use ones that are genuinely unknown.
@pytest.mark.parametrize("version", [0, 5, 99, "3", None])
def test_unknown_versions_still_refused(version):
    with pytest.raises(PayloadError) as err:
        parse(envelope(version=version), now=NOW)
    assert err.value.reason == "unsupported_version"


def test_v2_payload_must_not_carry_buckets_semantics():
    """A v2 envelope is parsed as v2: buckets are not interpreted."""
    body = envelope(version=2, buckets=buckets(h=[hourly()]))
    result = parse(body, now=NOW)
    assert result.version == 2
    assert result.history is None


# --- Accepted v3 ------------------------------------------------------------


def test_v3_with_both_bucket_kinds_is_accepted():
    result = parse(envelope(buckets=buckets(h=[hourly()], d=[daily()])), now=NOW)
    assert result.version == 3
    assert len(result.history.heart_rate_hourly) == 1
    assert len(result.history.steps_daily) == 1

    bucket = result.history.heart_rate_hourly[0]
    assert (bucket.mean, bucket.minimum, bucket.maximum, bucket.count) == (
        72.4, 58.0, 141.0, 37,
    )
    day = result.history.steps_daily[0]
    assert (day.day.isoformat(), day.time_zone, day.total) == (
        "2026-09-03", "Europe/Berlin", 8423.0,
    )


def test_v3_without_buckets_is_valid():
    assert parse(envelope(), now=NOW).history is None


def test_v3_with_empty_bucket_arrays_is_valid():
    result = parse(envelope(buckets=buckets()), now=NOW)
    assert result.history.is_empty()


def test_count_is_optional():
    result = parse(envelope(buckets=buckets(h=[hourly(count=None)])), now=NOW)
    assert result.history.heart_rate_hourly[0].count is None


# --- Hourly bucket validation ----------------------------------------------


@pytest.mark.parametrize("minutes", [1, 30, 59])
def test_non_hour_aligned_start_is_refused(minutes):
    """HA long-term statistics are keyed on hour-aligned UTC starts."""
    with pytest.raises(PayloadError) as err:
        parse(
            envelope(buckets=buckets(h=[hourly(start=HOUR + timedelta(minutes=minutes))])),
            now=NOW,
        )
    assert err.value.reason == "bucket_start_not_hour_aligned"


@pytest.mark.parametrize(
    ("lo", "mean", "hi"),
    [(80.0, 72.4, 141.0), (58.0, 200.0, 141.0), (141.0, 72.4, 58.0)],
)
def test_inconsistent_min_mean_max_is_refused(lo, mean, hi):
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(h=[hourly(mean=mean, lo=lo, hi=hi)])), now=NOW)
    assert err.value.reason == "bucket_range_inconsistent"


@pytest.mark.parametrize("count", [-1, 1.5, "37", True])
def test_bad_count_is_refused(count):
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(h=[hourly(count=count)])), now=NOW)
    assert err.value.reason == "bad_bucket_count"


def test_future_hourly_bucket_is_refused():
    with pytest.raises(PayloadError):
        parse(
            envelope(buckets=buckets(h=[hourly(start=NOW + timedelta(days=2))])), now=NOW
        )


def test_duplicate_hour_in_one_request_is_refused():
    """Two rows for the same hour would make the import order-dependent."""
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(h=[hourly(), hourly(mean=90.0)])), now=NOW)
    assert err.value.reason == "duplicate_hourly_bucket"


# --- Daily bucket validation ------------------------------------------------


def test_unknown_time_zone_is_refused():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(d=[daily(tz="Mars/Olympus")])), now=NOW)
    assert err.value.reason == "bad_bucket_time_zone"


@pytest.mark.parametrize("total", [-1, "8423", None])
def test_bad_step_total_is_refused(total):
    with pytest.raises(PayloadError):
        parse(envelope(buckets=buckets(d=[daily(total=total)])), now=NOW)


def test_future_day_is_refused():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(d=[daily(day="2099-01-01")])), now=NOW)
    assert err.value.reason == "bad_bucket_future_timestamp"


def test_local_day_may_lead_utc():
    """A device ahead of UTC is legitimately already on tomorrow's date."""
    result = parse(envelope(buckets=buckets(d=[daily(day="2026-09-04")])), now=NOW)
    assert len(result.history.steps_daily) == 1


def test_duplicate_day_in_one_request_is_refused():
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(d=[daily(), daily(total=99)])), now=NOW)
    assert err.value.reason == "duplicate_daily_bucket"


# --- Limits -----------------------------------------------------------------


def test_hourly_bucket_ceiling():
    """v3 carries one hourly metric, so its ceiling is the per-metric one.

    That value is unchanged at 400, so a v3 client behaves exactly as before;
    only the rejection reason is now more specific. The larger
    MAX_HOURLY_BUCKETS is a total envelope across every hourly metric, which v3
    cannot reach with a single series.
    """
    limit = MAX_HOURLY_BUCKETS_PER_METRIC
    ok = [hourly(start=HOUR - timedelta(hours=i)) for i in range(limit)]
    assert len(parse(envelope(buckets=buckets(h=ok)), now=NOW).history.heart_rate_hourly) \
        == limit

    too_many = [hourly(start=HOUR - timedelta(hours=i)) for i in range(limit + 1)]
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(h=too_many)), now=NOW)
    assert err.value.reason == "too_many_hourly_buckets_for_metric"
    assert limit < MAX_HOURLY_BUCKETS, "the envelope must be the looser of the two"


def test_daily_bucket_ceiling():
    days = [
        daily(day=(datetime(2026, 9, 3, tzinfo=UTC) - timedelta(days=i)).date().isoformat())
        for i in range(MAX_DAILY_BUCKETS + 1)
    ]
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=buckets(d=days)), now=NOW)
    assert err.value.reason == "too_many_daily_buckets"


def test_14_day_recovery_window_fits_inside_the_ceiling():
    """336 hourly + 14 daily is the widest window the client can produce."""
    assert 14 * 24 <= MAX_HOURLY_BUCKETS
    assert 14 <= MAX_DAILY_BUCKETS

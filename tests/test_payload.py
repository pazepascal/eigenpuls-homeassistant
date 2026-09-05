"""Wire format v1 conformance - protocol/payload-v1.md."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync.payload import PayloadError, parse

NOW = datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC)
UUID_A = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
UUID_B = "5A1B2C3D-4E5F-4A6B-8C9D-0E1F2A3B4C5D"


def envelope(**overrides):
    body = {
        "version": 1,
        "type": "sync",
        "sent_at": "2026-09-02T21:00:00Z",
        "device": {"name": "iPhone", "model": "iPhone17,1"},
    }
    body.update(overrides)
    return body


def hr(**overrides):
    item = {
        "metric": "heart_rate",
        "uuid": UUID_A,
        "start": "2026-09-02T20:14:31Z",
        "end": "2026-09-02T20:14:31Z",
        "value": 62,
        "unit": "count/min",
        "source": "Apple Watch",
    }
    item.update(overrides)
    return item


def steps(**overrides):
    item = {
        "metric": "steps",
        "date": "2026-09-02",
        "time_zone": "Europe/Berlin",
        "value": 8423,
        "unit": "count",
    }
    item.update(overrides)
    return item


# --- Envelope ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        # v2 is now a supported version; use one that is genuinely unknown.
        ({"version": 99, "type": "sync"}, "unsupported_version"),
        (envelope(type="nonsense"), "unsupported_type"),
        ({"version": 1, "type": "sync", "device": {}}, "missing_field"),
    ],
)
def test_envelope_rejections(body, reason):
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == reason


def test_non_object_body_rejected():
    with pytest.raises(PayloadError):
        parse([], now=NOW)


def test_ping_carries_no_data_and_applies_nothing():
    result = parse(
        envelope(type="ping", samples=[hr()], daily_totals=[steps()]), now=NOW
    )
    assert result.kind == "ping"
    assert result.samples == []
    assert result.daily_totals == []


# --- Samples ----------------------------------------------------------------


def test_heart_rate_sample_accepted():
    result = parse(envelope(samples=[hr()]), now=NOW)
    assert len(result.samples) == 1
    sample = result.samples[0]
    assert sample.metric == "heart_rate"
    assert sample.value == 62
    assert sample.uuid == UUID_A
    assert sample.source == "Apple Watch"


def test_uuid_is_normalised_to_uppercase():
    result = parse(envelope(samples=[hr(uuid=UUID_A.lower())]), now=NOW)
    assert result.samples[0].uuid == UUID_A


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"metric": "vo2max"}, "unknown_metric"),
        ({"metric": "steps"}, "wrong_kind"),          # cumulative must not arrive as samples
        ({"uuid": "not-a-uuid"}, "bad_uuid"),
        ({"value": "62"}, "bad_value"),
        ({"value": True}, "bad_value"),
        ({"start": "yesterday"}, "bad_timestamp"),
        ({"start": "2026-09-02T20:14:31"}, "bad_timestamp"),   # naive
        ({"end": "2099-01-01T00:00:00Z"}, "future_timestamp"),
        ({"start": "1970-01-01T00:00:00Z"}, "bad_timestamp"),  # below floor
    ],
)
def test_bad_sample_is_rejected_individually(override, reason):
    result = parse(envelope(samples=[hr(**override)]), now=NOW)
    assert result.samples == []
    assert [r.reason for r in result.rejected] == [reason]


def test_one_bad_sample_does_not_fail_the_batch():
    result = parse(
        envelope(samples=[hr(), hr(uuid="broken", value=70), hr(uuid=UUID_B, value=71)]),
        now=NOW,
    )
    assert len(result.samples) == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].index == 1
    assert result.rejected[0].collection == "samples"


def test_end_before_start_rejected():
    result = parse(
        envelope(samples=[hr(start="2026-09-02T20:15:00Z", end="2026-09-02T20:14:00Z")]),
        now=NOW,
    )
    assert result.rejected[0].reason == "bad_timestamp"


# --- Daily totals -----------------------------------------------------------


def test_daily_total_accepted():
    result = parse(envelope(daily_totals=[steps()]), now=NOW)
    total = result.daily_totals[0]
    assert total.value == 8423
    assert total.day.isoformat() == "2026-09-02"
    assert total.time_zone == "Europe/Berlin"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"metric": "heart_rate"}, "wrong_kind"),   # discrete must not arrive as a total
        ({"metric": "flights"}, "unknown_metric"),
        ({"date": "02-09-2026"}, "bad_timestamp"),
        ({"date": "2099-01-01"}, "future_timestamp"),
        ({"time_zone": ""}, "missing_field"),
        ({"value": None}, "bad_value"),
    ],
)
def test_bad_daily_total_rejected(override, reason):
    result = parse(envelope(daily_totals=[steps(**override)]), now=NOW)
    assert result.daily_totals == []
    assert [r.reason for r in result.rejected] == [reason]


def test_local_day_may_lead_utc():
    """A device in Europe/Berlin is legitimately a day ahead of a UTC 'now'."""
    result = parse(envelope(daily_totals=[steps(date="2026-09-03")]), now=NOW)
    assert len(result.daily_totals) == 1


# --- Deletions and limits ---------------------------------------------------


def test_deletions_parsed_and_normalised():
    result = parse(envelope(deletions=[UUID_A.lower()]), now=NOW)
    assert result.deletions == [UUID_A]


def test_bad_deletion_rejected_individually():
    result = parse(envelope(deletions=[UUID_A, "nope"]), now=NOW)
    assert result.deletions == [UUID_A]
    assert result.rejected[0].collection == "deletions"


def test_collection_limit_fails_the_request():
    with pytest.raises(PayloadError) as err:
        parse(envelope(samples=[hr()] * 5001), now=NOW)
    assert err.value.reason == "too_many_samples"


def test_absent_collections_are_valid():
    result = parse(envelope(), now=NOW)
    assert (result.samples, result.daily_totals, result.deletions) == ([], [], [])

"""The receiver tells the client what it understands, and survives being told
about something it does not.

The problem this file exists for: an iOS update reaches a phone long before a
HACS update reaches the Home Assistant instance behind it, so "newer client,
older receiver" is the ordinary state during a rollout rather than an edge case.
Before this, one metric the receiver had never heard of cost the entire
delivery - every other health value in it included.

Two halves, and both are needed:

* the receiver publishes ``supported_metrics`` and ``supported_features`` so a
  client can withhold anything newer, which is the primary defence;
* the receiver degrades gracefully if a client sends something new anyway,
  which is the backstop for a stale client cache.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync import registry
from custom_components.apple_health_sync.payload import PayloadError, parse

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
TZ = "Europe/Berlin"


def envelope(**extra):
    body = {
        "version": 4,
        "type": "sync",
        "sent_at": NOW.isoformat(),
        "device": {"name": "iPhone", "model": "iPhone18,1", "os_version": "26.5"},
        "sync": {"id": "abc", "final": True},
        "snapshot": {
            "heart_rate": {
                "value": 60.0,
                "unit": "count/min",
                "measured_at": NOW.isoformat(),
            }
        },
    }
    body.update(extra)
    return body


def daily(metric: str, total: float = 1.0):
    return {"metric": metric, "date": "2026-05-29", "time_zone": TZ, "total": total}


def hourly(metric: str):
    return {
        "metric": metric,
        "start": "2026-05-29T11:00:00+00:00",
        "mean": 1.0,
        "min": 1.0,
        "max": 1.0,
    }


# --- What the receiver publishes -------------------------------------------


def test_the_support_lists_cover_the_whole_registry():
    assert set(registry.SUPPORTED_METRICS) == set(registry.METRICS)
    assert len(registry.SUPPORTED_METRICS) == 18


def test_the_support_lists_are_sorted_and_free_of_duplicates():
    # Stable output so a client can compare them cheaply and a diff is readable.
    assert list(registry.SUPPORTED_METRICS) == sorted(set(registry.SUPPORTED_METRICS))
    assert len(set(registry.SUPPORTED_FEATURES)) == len(registry.SUPPORTED_FEATURES)


def test_every_published_feature_is_something_this_receiver_actually_has():
    # A feature list that promises more than the code does would be worse than
    # no list at all: the client would send something and lose it silently.
    assert set(registry.SUPPORTED_FEATURES) == {
        "buckets.nightly",
        "snapshot.blood_pressure",
        "snapshot.last_workout",
        "snapshot.sleep_trend",
    }


# --- Graceful degradation, the backstop -------------------------------------


def test_a_known_metric_survives_an_unknown_one():
    payload = parse(
        envelope(buckets={"daily": [daily("steps", 8000), daily("future_metric")]}),
        now=NOW,
    )
    assert [b.metric for b in payload.history.daily] == ["steps"]
    assert [r.reason for r in payload.rejected] == ["unknown_metric"]


def test_an_unknown_metric_is_never_stored_under_a_guessed_meaning():
    payload = parse(envelope(buckets={"daily": [daily("future_metric")]}), now=NOW)
    assert payload.history.daily == []
    assert payload.rejected


def test_the_rejection_names_the_family_it_came_from():
    payload = parse(
        envelope(
            buckets={
                "hourly": [hourly("future_hourly")],
                "daily": [daily("future_daily")],
            }
        ),
        now=NOW,
    )
    assert {r.collection for r in payload.rejected} == {"hourly", "daily"}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"daily": [{"date": "2026-05-29", "total": 1}]}, "bad_bucket_missing_field"),
        ({"future_family": []}, "unknown_bucket_kind"),
        ({"daily": [{**daily("steps"), "future_field": 1}]}, "bad_bucket_unknown_field"),
    ],
)
def test_everything_that_is_not_version_skew_is_still_fatal(body, reason):
    """Only an unknown metric id is tolerated.

    A malformed bucket, an unknown bucket family and an unexpected field inside a
    known bucket are protocol violations, not version skew. Loosening those by
    association is exactly how a graceful-degradation change turns into silent
    data loss.
    """
    with pytest.raises(PayloadError) as err:
        parse(envelope(buckets=body), now=NOW)
    assert err.value.reason == reason


def test_an_old_client_is_completely_unaffected():
    """No v3 or earlier payload changes shape or outcome."""
    for version in (1, 2, 3):
        body = envelope(version=version)
        if version == 1:
            body.pop("sync")
            body.pop("snapshot")
        else:
            body["snapshot"] = {"heart_rate": None, "steps_today": None}
        assert parse(body, now=NOW).version == version


# --- Snapshot: the opposite failure mode ------------------------------------


def test_an_unknown_snapshot_object_is_accepted_and_ignored():
    """Measured, and the reason a metric list alone is not enough.

    Buckets reject what they do not know; the snapshot silently ignores it. A
    client that sent a new structured snapshot object to an older receiver would
    get HTTP 200 and believe it had transmitted something that was discarded.
    That is why the receiver publishes ``supported_features`` as well, and why
    the client must not send a feature it has not been told about.
    """
    payload = parse(
        envelope(
            snapshot={
                "heart_rate": {
                    "value": 60.0,
                    "unit": "count/min",
                    "measured_at": NOW.isoformat(),
                },
                "activity": {"move_energy": 300, "move_energy_goal": 600},
            }
        ),
        now=NOW,
    )
    assert payload.snapshot is not None
    assert payload.snapshot.heart_rate is not None
    # Accepted, and gone. Nothing in the response says so - hence the feature list.
    assert not hasattr(payload.snapshot, "activity")

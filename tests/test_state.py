"""Current-value folding rules."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.apple_health_sync.payload import parse
from custom_components.apple_health_sync.state import HealthState

NOW = datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC)
UUID_A = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
UUID_B = "5A1B2C3D-4E5F-4A6B-8C9D-0E1F2A3B4C5D"


def build(samples=(), daily_totals=(), deletions=()):
    return parse(
        {
            "version": 1,
            "type": "sync",
            "sent_at": "2026-09-02T21:00:00Z",
            "device": {"name": "iPhone"},
            "samples": list(samples),
            "daily_totals": list(daily_totals),
            "deletions": list(deletions),
        },
        now=NOW,
    )


def hr(uuid, end, value):
    return {
        "metric": "heart_rate",
        "uuid": uuid,
        "start": end,
        "end": end,
        "value": value,
        "unit": "count/min",
    }


def steps(day, value):
    return {
        "metric": "steps",
        "date": day,
        "time_zone": "Europe/Berlin",
        "value": value,
        "unit": "count",
    }


def test_latest_sample_wins():
    state = HealthState()
    state.apply(
        build(samples=[hr(UUID_A, "2026-09-02T20:00:00Z", 60),
                       hr(UUID_B, "2026-09-02T20:30:00Z", 72)]),
        received_at=NOW,
    )
    assert state.heart_rate == 72


def test_older_sample_never_moves_the_value_backwards():
    state = HealthState()
    state.apply(build(samples=[hr(UUID_B, "2026-09-02T20:30:00Z", 72)]), received_at=NOW)
    state.apply(build(samples=[hr(UUID_A, "2026-09-02T20:00:00Z", 60)]), received_at=NOW)
    assert state.heart_rate == 72


def test_replaying_a_payload_is_idempotent():
    state = HealthState()
    payload = build(samples=[hr(UUID_A, "2026-09-02T20:00:00Z", 60)],
                    daily_totals=[steps("2026-09-02", 8423)])
    state.apply(payload, received_at=NOW)
    state.apply(payload, received_at=NOW)
    assert state.heart_rate == 60
    assert state.steps == 8423  # replaced, never accumulated


def test_daily_total_is_replaced_not_added():
    state = HealthState()
    state.apply(build(daily_totals=[steps("2026-09-02", 8000)]), received_at=NOW)
    state.apply(build(daily_totals=[steps("2026-09-02", 8423)]), received_at=NOW)
    assert state.steps == 8423


def test_look_back_total_for_an_older_day_does_not_overwrite_today():
    state = HealthState()
    state.apply(build(daily_totals=[steps("2026-09-02", 8423)]), received_at=NOW)
    state.apply(build(daily_totals=[steps("2026-09-01", 12000)]), received_at=NOW)
    assert state.steps == 8423
    assert state.steps_day.isoformat() == "2026-09-02"


def test_new_day_advances_the_total():
    state = HealthState()
    state.apply(build(daily_totals=[steps("2026-09-02", 8423)]), received_at=NOW)
    state.apply(build(daily_totals=[steps("2026-09-03", 120)]), received_at=NOW)
    assert state.steps == 120


def test_deleting_the_displayed_sample_clears_it():
    state = HealthState()
    state.apply(build(samples=[hr(UUID_A, "2026-09-02T20:00:00Z", 60)]), received_at=NOW)
    state.apply(build(deletions=[UUID_A]), received_at=NOW)
    assert state.heart_rate is None
    assert state.heart_rate_uuid is None


def test_deleting_an_unrelated_sample_is_a_no_op():
    state = HealthState()
    state.apply(build(samples=[hr(UUID_A, "2026-09-02T20:00:00Z", 60)]), received_at=NOW)
    state.apply(build(deletions=[UUID_B]), received_at=NOW)
    assert state.heart_rate == 60


def test_last_sync_advances_on_every_delivery():
    state = HealthState()
    later = datetime(2026, 9, 2, 22, 0, 0, tzinfo=UTC)
    state.apply(build(), received_at=NOW)
    state.apply(build(), received_at=later)
    assert state.last_sync == later

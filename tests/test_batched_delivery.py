"""Receiver behaviour across a multi-batch upload.

The client now splits a history into several deliveries. These pin the guarantees
that must survive that split.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.apple_health_sync.payload import parse
from custom_components.apple_health_sync.state import HealthState

NOW = datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC)


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


def hr(index: int, minute: int, value: float) -> dict:
    return {
        "metric": "heart_rate",
        "uuid": f"3F2504E0-4F89-11D3-9A0C-{index:012X}",
        "start": f"2026-09-02T20:{minute:02d}:00Z",
        "end": f"2026-09-02T20:{minute:02d}:00Z",
        "value": value,
        "unit": "count/min",
    }


def steps(day: str, value: float) -> dict:
    return {
        "metric": "steps",
        "date": day,
        "time_zone": "Europe/Berlin",
        "value": value,
        "unit": "count",
    }


def test_batched_upload_matches_a_single_large_upload():
    """Splitting a history across batches must not change the result."""
    items = [hr(i, i % 60, 60 + i % 30) for i in range(30)]

    one_shot = HealthState()
    one_shot.apply(build(samples=items), received_at=NOW)

    batched = HealthState()
    for start in range(0, 30, 10):
        batched.apply(build(samples=items[start : start + 10]), received_at=NOW)

    assert batched.heart_rate == one_shot.heart_rate
    assert batched.heart_rate_uuid == one_shot.heart_rate_uuid
    assert batched.heart_rate_at == one_shot.heart_rate_at


def test_batches_arriving_out_of_order_still_keep_the_newest_reading():
    """A retry can re-send an earlier batch after a later one already landed."""
    early = [hr(0, 10, 61)]
    late = [hr(1, 50, 77)]

    state = HealthState()
    state.apply(build(samples=late), received_at=NOW)
    state.apply(build(samples=early), received_at=NOW)

    assert state.heart_rate == 77


def test_replaying_a_whole_multi_batch_upload_changes_nothing():
    items = [hr(i, i % 60, 60 + i % 30) for i in range(30)]
    totals = [steps("2026-09-02", 8423)]

    state = HealthState()

    def deliver_all():
        for start in range(0, 30, 10):
            batch_totals = totals if start == 0 else []
            state.apply(
                build(samples=items[start : start + 10], daily_totals=batch_totals),
                received_at=NOW,
            )

    deliver_all()
    first = (state.heart_rate, state.heart_rate_uuid, state.steps, state.steps_day)
    deliver_all()  # full retry of every batch
    second = (state.heart_rate, state.heart_rate_uuid, state.steps, state.steps_day)

    assert first == second
    # Crucially the total was replaced, never accumulated.
    assert state.steps == 8423


def test_totals_repeated_across_batches_are_replaced_not_summed():
    state = HealthState()
    for _ in range(5):
        state.apply(build(daily_totals=[steps("2026-09-02", 8423)]), received_at=NOW)
    assert state.steps == 8423


def test_deletion_in_a_later_batch_clears_a_sample_from_an_earlier_one():
    state = HealthState()
    state.apply(build(samples=[hr(0, 10, 61)]), received_at=NOW)
    assert state.heart_rate == 61

    state.apply(
        build(deletions=["3F2504E0-4F89-11D3-9A0C-000000000000"]), received_at=NOW
    )
    assert state.heart_rate is None

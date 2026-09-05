"""Wire format v2: snapshot/completion semantics.

v2 exists because v1 let historical batches drive the current-value entities.
Anchored queries return samples in *anchor* order, not measurement-date order,
so the last batch of a backfill is not necessarily the newest reading — and a
Home Assistant restart partway through could leave the sensors wrong while every
HTTP request returned 200.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync.payload import (
    SUPPORTED_VERSIONS,
    WIRE_VERSION,
    PayloadError,
    parse,
)
from custom_components.apple_health_sync.state import HealthState

NOW = datetime(2026, 9, 3, 21, 0, 0, tzinfo=UTC)


def envelope(**overrides):
    body = {
        "version": 2,
        "type": "sync",
        "sent_at": "2026-09-03T21:00:00Z",
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": False},
    }
    body.update(overrides)
    return body


def snapshot(hr=64.0, steps=359.0, measured="2026-09-03T20:41:12Z"):
    return {
        "heart_rate": {
            "value": hr,
            "unit": "count/min",
            "measured_at": measured,
            "source": "Apple Watch",
        },
        "steps_today": {
            "value": steps,
            "unit": "count",
            "date": "2026-09-03",
            "time_zone": "Europe/Berlin",
        },
    }


def hr_sample(index: int, minute: int, value: float) -> dict:
    return {
        "metric": "heart_rate",
        "uuid": f"3F2504E0-4F89-11D3-9A0C-{index:012X}",
        "start": f"2026-09-03T20:{minute:02d}:00Z",
        "end": f"2026-09-03T20:{minute:02d}:00Z",
        "value": value,
        "unit": "count/min",
    }


# --- Version negotiation ----------------------------------------------------


def test_v2_remains_supported_alongside_newer_versions():
    """This file's concern is that v2 keeps working, not what the newest is."""
    assert 2 in SUPPORTED_VERSIONS
    assert 1 in SUPPORTED_VERSIONS
    assert WIRE_VERSION >= 2


# 4 is a supported version now; use ones that are genuinely unknown.
@pytest.mark.parametrize("version", [0, 5, 99, "2", None])
def test_unknown_versions_are_refused(version):
    with pytest.raises(PayloadError) as err:
        parse(envelope(version=version), now=NOW)
    assert err.value.reason == "unsupported_version"


# --- v2 envelope requirements ----------------------------------------------


def test_v2_sync_without_sync_metadata_is_refused():
    body = envelope()
    del body["sync"]
    with pytest.raises(PayloadError) as err:
        parse(body, now=NOW)
    assert err.value.reason == "missing_sync"


@pytest.mark.parametrize("sync", [{}, {"id": "x"}, {"final": "yes"}, {"final": None}])
def test_v2_sync_metadata_must_carry_a_boolean_final(sync):
    with pytest.raises(PayloadError) as err:
        parse(envelope(sync=sync), now=NOW)
    assert err.value.reason == "missing_sync"


def test_v2_final_without_snapshot_is_refused():
    with pytest.raises(PayloadError) as err:
        parse(envelope(sync={"id": "s", "final": True}), now=NOW)
    assert err.value.reason == "missing_snapshot"


def test_v2_ping_needs_no_sync_metadata():
    result = parse(
        {
            "version": 2,
            "type": "ping",
            "sent_at": "2026-09-03T21:00:00Z",
            "device": {"name": "iPhone"},
        },
        now=NOW,
    )
    assert result.kind == "ping"


def test_sync_id_is_carried_for_correlation_only():
    result = parse(envelope(sync={"id": "abc-123", "final": False}), now=NOW)
    assert result.sync_id == "abc-123"
    assert result.is_final is False


def test_missing_sync_id_is_tolerated():
    """id is diagnostic; nothing correctness-bearing may depend on it."""
    result = parse(envelope(sync={"final": False}), now=NOW)
    assert result.sync_id is None
    assert result.is_final is False


# --- Snapshot application ---------------------------------------------------


def test_completion_snapshot_sets_all_three_values():
    result = parse(
        envelope(sync={"id": "s", "final": True}, snapshot=snapshot()), now=NOW
    )
    state = HealthState()
    state.apply_snapshot(result.snapshot, received_at=NOW)

    assert state.heart_rate == 64.0
    assert state.steps == 359.0
    assert state.steps_day.isoformat() == "2026-09-03"
    assert state.steps_time_zone == "Europe/Berlin"
    assert state.last_sync == NOW


def test_null_snapshot_member_leaves_that_sensor_unchanged():
    """Absence is not a measurement: HealthKit cannot distinguish denied reads."""
    state = HealthState()
    state.apply_snapshot(
        parse(
            envelope(sync={"id": "s", "final": True}, snapshot=snapshot()), now=NOW
        ).snapshot,
        received_at=NOW,
    )

    partial = parse(
        envelope(
            sync={"id": "s2", "final": True},
            snapshot={"heart_rate": None, "steps_today": None},
        ),
        now=NOW,
    )
    state.apply_snapshot(partial.snapshot, received_at=NOW)

    assert state.heart_rate == 64.0  # not cleared
    assert state.steps == 359.0


def test_repeated_identical_completion_is_idempotent():
    """A retried completion must not manufacture different state."""
    parsed = parse(
        envelope(sync={"id": "s", "final": True}, snapshot=snapshot()), now=NOW
    )
    state = HealthState()

    state.apply_snapshot(parsed.snapshot, received_at=NOW)
    first = (state.heart_rate, state.steps, state.steps_day)
    state.apply_snapshot(parsed.snapshot, received_at=NOW)

    assert (state.heart_rate, state.steps, state.steps_day) == first


def test_newer_completion_replaces_rather_than_accumulates():
    state = HealthState()
    for value in (100.0, 200.0, 359.0):
        parsed = parse(
            envelope(sync={"id": "s", "final": True}, snapshot=snapshot(steps=value)),
            now=NOW,
        )
        state.apply_snapshot(parsed.snapshot, received_at=NOW)

    assert state.steps == 359.0  # replaced, never summed


# --- The restart hole this version exists to close --------------------------


def test_restart_midway_through_backfill_cannot_affect_current_values():
    """Hundreds of accepted pages, all receiver state lost, then completion.

    Under v2 the data batches never touch the current values, so the snapshot
    alone determines them — regardless of how many batches were accepted before
    the restart, and regardless of the order HealthKit delivered them in.
    """
    state = HealthState()

    # 300 historical batches accepted, deliberately in a misleading order:
    # the *last* batch carries the OLDEST reading.
    for index in range(300):
        parsed = parse(
            envelope(samples=[hr_sample(index, index % 60, 200.0 - index * 0.5)]),
            now=NOW,
        )
        assert parsed.is_final is False
        # v2 batches are transport only — nothing is applied.

    # Home Assistant restarts: every scrap of accumulated state is gone.
    state = HealthState()
    assert state.heart_rate is None

    # The client resumes and completes with a freshly read snapshot.
    completion = parse(
        envelope(sync={"id": "s2", "final": True}, snapshot=snapshot()), now=NOW
    )
    state.apply_snapshot(completion.snapshot, received_at=NOW)

    assert state.heart_rate == 64.0
    assert state.steps == 359.0
    assert state.last_sync == NOW


def test_historical_batch_order_cannot_determine_the_current_value():
    """Anchor order is not date order; the snapshot must win regardless."""
    completion = parse(
        envelope(sync={"id": "s", "final": True}, snapshot=snapshot(hr=64.0)), now=NOW
    )
    state = HealthState()
    state.apply_snapshot(completion.snapshot, received_at=NOW)
    assert state.heart_rate == 64.0

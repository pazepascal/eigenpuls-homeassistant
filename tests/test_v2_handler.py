"""Handler behaviour under v2: who is allowed to move the entities."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import streams

from custom_components.apple_health_sync import AppleHealthSyncRuntimeData
from custom_components.apple_health_sync.state import HealthState
from custom_components.apple_health_sync.webhook import _make_handler

TOKEN = "correct-horse-battery-staple"
_FEEDERS: list[asyncio.Task] = []


# These tests drive the real handler, which validates against the real clock.
# Timestamps are therefore derived from now rather than hard-coded, so the suite
# cannot start failing on a future date.
def ago(minutes: int) -> str:
    stamp = datetime.now(UTC) - timedelta(minutes=minutes)
    return stamp.isoformat().replace("+00:00", "Z")


def local_today() -> str:
    return datetime.now(UTC).date().isoformat()


@pytest.fixture(autouse=True)
async def _cancel_feeders():
    yield
    for task in _FEEDERS:
        task.cancel()
    for task in _FEEDERS:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _FEEDERS.clear()


def make_request(body: dict):
    raw = json.dumps(body).encode()
    protocol = mock.Mock(_reading_paused=False)
    reader = streams.StreamReader(protocol, limit=2**16, loop=asyncio.get_event_loop())
    reader.feed_data(raw)
    reader.feed_eof()
    return SimpleNamespace(
        headers={"Authorization": f"Bearer {TOKEN}"},
        content_length=len(raw),
        content=reader,
    )


def make_entry():
    entry = SimpleNamespace(entry_id="entry-1")
    entry.runtime_data = AppleHealthSyncRuntimeData(
        token=TOKEN, webhook_id="hook-1", state=HealthState()
    )
    return entry


async def post(body: dict, entry, dispatch: mock.Mock):
    handler = _make_handler(entry)
    with mock.patch(
        "custom_components.apple_health_sync.webhook.async_dispatcher_send", dispatch
    ):
        response = await handler(mock.Mock(), "hook-1", make_request(body))
    return response, json.loads(response.body)


def data_batch(index: int, final: bool = False):
    return {
        "version": 2,
        "type": "sync",
        "sent_at": ago(1),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": final},
        "samples": [
            {
                "metric": "heart_rate",
                "uuid": f"3F2504E0-4F89-11D3-9A0C-{index:012X}",
                "start": ago(30),
                "end": ago(30),
                "value": 200.0 - index,
                "unit": "count/min",
            }
        ],
    }


def completion(hr=64.0, steps=359.0):
    return {
        "version": 2,
        "type": "sync",
        "sent_at": ago(1),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True},
        "snapshot": {
            "heart_rate": {
                "value": hr,
                "unit": "count/min",
                "measured_at": ago(10),
                "source": "Apple Watch",
            },
            "steps_today": {
                "value": steps,
                "unit": "count",
                "date": local_today(),
                "time_zone": "Europe/Berlin",
            },
        },
    }


# --- Data batches must not move anything ------------------------------------


async def test_v2_data_batch_does_not_touch_state_or_dispatch():
    entry, dispatch = make_entry(), mock.Mock()
    response, payload = await post(data_batch(0), entry, dispatch)

    assert response.status == 200
    assert payload["completed"] is False
    assert payload["accepted"]["samples"] == 1  # accepted and counted
    assert entry.runtime_data.state.heart_rate is None
    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


async def test_many_data_batches_then_completion_writes_exactly_once():
    """The direct regression test for 916 Last Sync state changes."""
    entry, dispatch = make_entry(), mock.Mock()

    for index in range(50):
        await post(data_batch(index), entry, dispatch)

    assert dispatch.call_count == 0
    assert entry.runtime_data.state.last_sync is None

    response, payload = await post(completion(), entry, dispatch)

    assert response.status == 200
    assert payload["completed"] is True
    assert dispatch.call_count == 1  # exactly one entity write for the whole sync
    assert entry.runtime_data.state.heart_rate == 64.0
    assert entry.runtime_data.state.steps == 359.0
    assert entry.runtime_data.state.last_sync is not None


async def test_sync_that_never_completes_leaves_last_sync_untouched():
    """A failure at batch 900/916 must not look fresh."""
    entry, dispatch = make_entry(), mock.Mock()
    for index in range(900):
        await post(data_batch(index), entry, dispatch)

    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


async def test_completion_after_restart_still_correct():
    """Data batches to one runtime, completion to a freshly restarted one."""
    before, dispatch = make_entry(), mock.Mock()
    for index in range(300):
        await post(data_batch(index), before, dispatch)

    after = make_entry()  # simulates the restart: all accumulated state gone
    response, payload = await post(completion(), after, dispatch)

    assert response.status == 200
    assert payload["completed"] is True
    assert after.runtime_data.state.heart_rate == 64.0
    assert after.runtime_data.state.steps == 359.0


async def test_retried_completion_is_idempotent_at_the_handler():
    entry, dispatch = make_entry(), mock.Mock()
    await post(completion(), entry, dispatch)
    first = (entry.runtime_data.state.heart_rate, entry.runtime_data.state.steps)
    await post(completion(), entry, dispatch)

    assert (entry.runtime_data.state.heart_rate, entry.runtime_data.state.steps) == first
    assert dispatch.call_count == 2  # two genuine completions occurred


async def test_v2_final_without_snapshot_is_rejected_by_the_handler():
    entry, dispatch = make_entry(), mock.Mock()
    body = completion()
    del body["snapshot"]
    response, payload = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == "missing_snapshot"
    dispatch.assert_not_called()


# --- v1 legacy path unchanged ----------------------------------------------


async def test_v1_payload_still_applies_and_dispatches():
    """Retained v1 behaviour: every delivery is a complete sync."""
    entry, dispatch = make_entry(), mock.Mock()
    body = {
        "version": 1,
        "type": "sync",
        "sent_at": ago(1),
        "device": {"name": "iPhone"},
        "samples": [
            {
                "metric": "heart_rate",
                "uuid": "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
                "start": ago(30),
                "end": ago(30),
                "value": 61.0,
                "unit": "count/min",
            }
        ],
    }
    response, payload = await post(body, entry, dispatch)

    assert response.status == 200
    assert payload["version"] == 1
    assert payload["completed"] is True
    assert entry.runtime_data.state.heart_rate == 61.0
    assert entry.runtime_data.state.last_sync is not None
    dispatch.assert_called_once()


async def test_unsupported_version_is_refused():
    entry, dispatch = make_entry(), mock.Mock()
    body = completion()
    body["version"] = 5  # 4 is supported now
    response, payload = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == "unsupported_version"
    dispatch.assert_not_called()

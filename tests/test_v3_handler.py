"""v3 handler: validate-all-then-apply ordering and failure semantics."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import streams
from homeassistant.exceptions import HomeAssistantError

from custom_components.apple_health_sync import AppleHealthSyncRuntimeData
from custom_components.apple_health_sync.state import HealthState
from custom_components.apple_health_sync.webhook import _make_handler

TOKEN = "correct-horse-battery-staple"
_FEEDERS: list[asyncio.Task] = []


@pytest.fixture(autouse=True)
async def _cancel_feeders():
    yield
    for task in _FEEDERS:
        task.cancel()
    for task in _FEEDERS:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _FEEDERS.clear()


def ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace(
        "+00:00", "Z"
    )


def last_hour() -> str:
    stamp = (datetime.now(UTC) - timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    return stamp.isoformat().replace("+00:00", "Z")


def today() -> str:
    return datetime.now(UTC).date().isoformat()


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


async def post(body, entry, dispatch, importer=None):
    handler = _make_handler(entry)
    importer = importer or mock.AsyncMock()
    with (
        mock.patch(
            "custom_components.apple_health_sync.webhook.async_dispatcher_send", dispatch
        ),
        mock.patch(
            "custom_components.apple_health_sync.webhook.async_import_history", importer
        ),
        # The derived blood-pressure trend reads the recorder; these tests
        # exercise the handler with the statistics layer stubbed out.
        mock.patch(
            "custom_components.apple_health_sync.webhook.async_blood_pressure_trend",
            mock.AsyncMock(return_value=None),
        ),
    ):
        response = await handler(mock.Mock(), "hook-1", make_request(body))
    return response, json.loads(response.body), importer


def v3(final=True, with_buckets=True, **overrides):
    body = {
        "version": 3,
        "type": "sync",
        "sent_at": ago(1),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": final},
    }
    if final:
        body["snapshot"] = {
            "heart_rate": {
                "value": 64.0,
                "unit": "count/min",
                "measured_at": ago(10),
                "source": "Apple Watch",
            },
            "steps_today": {
                "value": 359.0,
                "unit": "count",
                "date": today(),
                "time_zone": "Europe/Berlin",
            },
        }
    if with_buckets:
        body["buckets"] = {
            "heart_rate_hourly": [
                {"start": last_hour(), "mean": 72.4, "min": 58, "max": 141, "count": 37}
            ],
            "steps_daily": [
                {"date": today(), "time_zone": "Europe/Berlin", "total": 8423}
            ],
        }
    body.update(overrides)
    return body


# --- Happy path -------------------------------------------------------------


async def test_v3_completion_imports_history_and_applies_snapshot_once():
    entry, dispatch = make_entry(), mock.Mock()
    response, payload, importer = await post(v3(), entry, dispatch)

    assert response.status == 200
    assert payload["version"] == 3
    assert payload["completed"] is True
    importer.assert_awaited_once()

    assert entry.runtime_data.state.heart_rate == 64.0
    assert entry.runtime_data.state.steps == 359.0
    assert entry.runtime_data.state.last_sync is not None
    assert dispatch.call_count == 1


async def test_v3_without_buckets_is_accepted_and_imports_nothing():
    entry, dispatch = make_entry(), mock.Mock()
    response, _, importer = await post(v3(with_buckets=False), entry, dispatch)

    assert response.status == 200
    importer.assert_not_awaited()
    assert entry.runtime_data.state.heart_rate == 64.0


# --- Buckets never drive current sensors ------------------------------------

async def test_buckets_alone_never_move_current_sensors():
    """A non-final v3 delivery imports history but must not touch entities."""
    entry, dispatch = make_entry(), mock.Mock()
    response, payload, importer = await post(
        v3(final=False, with_buckets=True), entry, dispatch
    )

    assert response.status == 200
    assert payload["completed"] is False
    importer.assert_awaited_once()          # history still imported
    assert entry.runtime_data.state.heart_rate is None   # but nothing displayed moved
    assert entry.runtime_data.state.steps is None
    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


async def test_bucket_values_do_not_leak_into_the_snapshot_path():
    """Bucket means differ wildly from the snapshot; the snapshot must win."""
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    body["buckets"]["heart_rate_hourly"][0].update({"mean": 130.0, "min": 120, "max": 190})
    body["buckets"]["steps_daily"][0]["total"] = 99_999

    await post(body, entry, dispatch)

    assert entry.runtime_data.state.heart_rate == 64.0      # snapshot, not 130
    assert entry.runtime_data.state.steps == 359.0          # snapshot, not 99999


# --- Failure semantics ------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b["buckets"]["heart_rate_hourly"][0].update({"start": ago(90)}),
         "bucket_start_not_hour_aligned"),
        (lambda b: b["buckets"]["heart_rate_hourly"][0].update({"min": 200}),
         "bucket_range_inconsistent"),
        (lambda b: b["buckets"]["steps_daily"][0].update({"time_zone": "Mars/Olympus"}),
         "bad_bucket_time_zone"),
        (lambda b: b["buckets"].update({"steps_daily": "nonsense"}), "bad_buckets"),
    ],
)
async def test_invalid_bucket_rejects_request_and_mutates_nothing(mutate, reason):
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    mutate(body)
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == reason
    importer.assert_not_awaited()
    assert entry.runtime_data.state.heart_rate is None
    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


async def test_statistics_failure_rejects_request_and_mutates_nothing():
    """Synchronous HA validation failure must not yield a false success."""
    entry, dispatch = make_entry(), mock.Mock()
    failing = mock.AsyncMock(side_effect=HomeAssistantError("Invalid statistic_id"))

    response, payload, _ = await post(v3(), entry, dispatch, importer=failing)

    assert response.status == 500
    assert payload["error"] == "statistics_rejected"
    assert entry.runtime_data.state.heart_rate is None   # snapshot NOT applied
    assert entry.runtime_data.state.last_sync is None    # Last Sync NOT advanced
    dispatch.assert_not_called()


async def test_statistics_failure_leaves_existing_sensor_values_intact():
    """A later failure must not corrupt values a previous sync established."""
    entry, dispatch = make_entry(), mock.Mock()
    await post(v3(), entry, dispatch)
    established = (
        entry.runtime_data.state.heart_rate,
        entry.runtime_data.state.steps,
        entry.runtime_data.state.last_sync,
    )

    failing = mock.AsyncMock(side_effect=HomeAssistantError("recorder unavailable"))
    body = v3()
    body["snapshot"]["heart_rate"]["value"] = 999.0
    response, _, _ = await post(body, entry, dispatch, importer=failing)

    assert response.status == 500
    assert (
        entry.runtime_data.state.heart_rate,
        entry.runtime_data.state.steps,
        entry.runtime_data.state.last_sync,
    ) == established


async def test_v3_final_without_snapshot_still_refused():
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    del body["snapshot"]
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == "missing_snapshot"
    importer.assert_not_awaited()


# --- Legacy compatibility ---------------------------------------------------


async def test_v2_completion_still_works_and_imports_no_history():
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    body["version"] = 2
    body.pop("buckets")
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 200
    assert payload["version"] == 2
    assert entry.runtime_data.state.heart_rate == 64.0
    importer.assert_not_awaited()
    dispatch.assert_called_once()


async def test_v2_payload_carrying_buckets_ignores_them_entirely():
    """Buckets are a v3 concept; a v2 envelope must not activate them."""
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    body["version"] = 2
    response, _, importer = await post(body, entry, dispatch)

    assert response.status == 200
    importer.assert_not_awaited()


async def test_v1_legacy_path_unchanged():
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
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 200
    assert payload["version"] == 1
    assert entry.runtime_data.state.heart_rate == 61.0
    importer.assert_not_awaited()
    dispatch.assert_called_once()


async def test_unsupported_version_rejected_before_any_work():
    entry, dispatch = make_entry(), mock.Mock()
    body = v3()
    body["version"] = 5  # 4 is supported now
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == "unsupported_version"
    importer.assert_not_awaited()
    dispatch.assert_not_called()

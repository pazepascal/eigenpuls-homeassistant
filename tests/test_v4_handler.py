"""v4 handler: nothing user-visible moves until the durable writes are accepted.

The ordering is the point. If the aggregate window is refused, the request must
fail with no snapshot applied, no Last Sync advanced and no entity update
dispatched - otherwise a failed sleep import could still be reported to the
person as a completed sync.
"""

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
TZ = "Europe/Berlin"
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


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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
        response = await handler(SimpleNamespace(), None, make_request(body))
    return response, json.loads(response.body), importer


def night(**fields):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    record = {
        "date": datetime.now(UTC).date().isoformat(),
        "time_zone": TZ,
        "total_sleep_min": 430.0,
        "sleep_start": iso(yesterday.replace(hour=21, minute=30, second=0, microsecond=0)),
        "wake_time": iso(
            datetime.now(UTC).replace(hour=5, minute=10, second=0, microsecond=0)
        ),
        "rem_min": 90.0, "core_min": 250.0, "deep_min": 60.0, "awake_min": 25.0,
    }
    record.update(fields)
    return record


def v4(**overrides):
    body = {
        "version": 4,
        "type": "sync",
        "sent_at": iso(datetime.now(UTC) - timedelta(minutes=1)),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True},
        "snapshot": {
            "heart_rate": {
                "value": 61.0, "unit": "bpm",
                "measured_at": iso(datetime.now(UTC) - timedelta(minutes=5)),
            },
            "sleep_last_night": night(),
            "sleep_7d": {"nights": 7, "avg_total_min": 421.0,
                         "nights_by_field": {"avg_total_min": 7}},
        },
        "buckets": {"nightly": [night()]},
    }
    body.update(overrides)
    return body


# --- The happy path ----------------------------------------------------------


async def test_a_v4_delivery_is_accepted_and_applied():
    entry, dispatch = make_entry(), mock.Mock()
    response, payload, importer = await post(v4(), entry, dispatch)

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["version"] == 4
    assert payload["completed"] is True
    importer.assert_awaited_once()

    state = entry.runtime_data.state
    assert state.heart_rate == 61.0
    assert state.sleep.total_sleep_min == 430.0
    assert state.sleep_trend.nights == 7
    assert state.last_sync is not None
    dispatch.assert_called_once()


# --- 18: durable first, entities second -------------------------------------


async def test_a_refused_import_leaves_every_entity_untouched():
    entry, dispatch = make_entry(), mock.Mock()
    failing = mock.AsyncMock(side_effect=HomeAssistantError("unit_class mismatch"))

    response, payload, _ = await post(v4(), entry, dispatch, importer=failing)

    assert response.status == 500
    assert payload["error"] == "statistics_rejected"

    state = entry.runtime_data.state
    # Nothing user-visible may move when the durable write did not land.
    assert state.heart_rate is None
    assert state.sleep is None
    assert state.sleep_trend is None
    assert state.last_sync is None, "a failed import must not look like a fresh sync"
    dispatch.assert_not_called()


async def test_a_refused_import_does_not_partially_apply_the_snapshot():
    """Some sensors updated and others not would be worse than none at all."""
    entry, dispatch = make_entry(), mock.Mock()
    entry.runtime_data.state.heart_rate = 55.0
    entry.runtime_data.state.last_sync = datetime(2026, 1, 1, tzinfo=UTC)
    failing = mock.AsyncMock(side_effect=HomeAssistantError("refused"))

    await post(v4(), entry, dispatch, importer=failing)

    # The previous values survive unchanged rather than being half-replaced.
    assert entry.runtime_data.state.heart_rate == 55.0
    assert entry.runtime_data.state.last_sync == datetime(2026, 1, 1, tzinfo=UTC)


# --- 19: a malformed night cannot mutate anything ---------------------------


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (night(total_sleep_min=5000.0), "nightly_total_exceeds_span"),
        (night(rem_min=400.0, core_min=400.0, deep_min=100.0), "nightly_stages_exceed_total"),
        (night(time_zone="Nowhere/Land"), "bad_bucket_time_zone"),
        (night(date="2020-01-01"), "nightly_date_mismatch"),
    ],
)
async def test_a_malformed_night_is_rejected_before_anything_is_written(record, reason):
    entry, dispatch = make_entry(), mock.Mock()
    body = v4(buckets={"nightly": [record]})

    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == reason
    # Rejected during parsing: the importer is never even reached.
    importer.assert_not_awaited()
    assert entry.runtime_data.state.heart_rate is None
    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


async def test_an_unknown_metric_no_longer_fails_the_whole_request():
    entry, dispatch = make_entry(), mock.Mock()
    body = v4(buckets={"daily": [
        {"metric": "steps", "date": datetime.now(UTC).date().isoformat(),
         "time_zone": TZ, "total": 8000.0},
        {"metric": "not_a_metric", "date": datetime.now(UTC).date().isoformat(),
         "time_zone": TZ, "total": 5.4},
    ]})

    response, payload, importer = await post(body, entry, dispatch)

    # The known bucket is stored; the unrecognised one is reported back.
    #
    # This assertion used to read `status == 400` and `importer.assert_not_awaited()`
    # - validate-all-then-apply, one unknown name costing the whole delivery.
    # That is the wrong trade once a newer client can reach an older receiver,
    # which is the normal rollout state rather than an edge case.
    assert response.status == 200
    assert [r["reason"] for r in payload["rejected"]] == ["unknown_metric"]
    importer.assert_awaited()
    dispatch.assert_called()


async def test_a_ping_still_changes_nothing_under_v4():
    entry, dispatch = make_entry(), mock.Mock()
    body = {
        "version": 4, "type": "ping",
        "sent_at": iso(datetime.now(UTC)), "device": {"name": "iPhone"},
    }
    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 200
    assert payload["pong"] is True
    assert entry.runtime_data.state.last_sync is None
    importer.assert_not_awaited()
    dispatch.assert_not_called()


async def test_a_non_final_v4_delivery_does_not_advance_last_sync():
    entry, dispatch = make_entry(), mock.Mock()
    body = v4()
    body["sync"] = {"id": "sync-1", "final": False}
    body.pop("snapshot")

    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 200
    assert payload["completed"] is False
    # The window is still stored; only completion is withheld.
    importer.assert_awaited_once()
    assert entry.runtime_data.state.last_sync is None
    dispatch.assert_not_called()


# --- Null survives the whole chain -------------------------------------------


async def test_a_null_stage_survives_parse_state_and_entity_exposure():
    """The requirement is end to end, not per layer.

    A stage that was never measured has to still be absent by the time it
    reaches a sensor's state, and a measured zero has to still be zero.
    """
    from custom_components.apple_health_sync.sensor import SENSORS, AppleHealthSensor

    entry, dispatch = make_entry(), mock.Mock()
    body = v4()
    body["snapshot"]["sleep_last_night"] = night(rem_min=None, deep_min=0.0)

    response, _, _ = await post(body, entry, dispatch)
    assert response.status == 200

    state = entry.runtime_data.state
    assert state.sleep.rem_min is None, "null must not become zero in state"
    assert state.sleep.deep_min == 0.0, "a measured zero must survive as zero"

    by_key = {
        d.key: AppleHealthSensor("entry-1", state, d) for d in SENSORS
    }
    assert by_key["sleep_rem"].native_value is None
    assert by_key["sleep_deep"].native_value == 0.0
    assert by_key["sleep_total"].extra_state_attributes["rem_min"] is None
    assert by_key["sleep_total"].extra_state_attributes["deep_min"] == 0.0


async def test_naps_arrive_as_daily_metrics_and_leave_the_night_alone():
    entry, dispatch = make_entry(), mock.Mock()
    body = v4()
    today = datetime.now(UTC).date().isoformat()
    body["buckets"]["daily"] = [
        {"metric": "nap_total", "date": today, "time_zone": TZ, "mean": 95.0},
        {"metric": "nap_count", "date": today, "time_zone": TZ, "mean": 3},
    ]
    body["snapshot"]["nap_total_today"] = {
        "value": 95.0, "unit": "min", "measured_at": iso(datetime.now(UTC)),
    }

    response, _, _ = await post(body, entry, dispatch)
    assert response.status == 200

    state = entry.runtime_data.state
    assert state.measurements["nap_total"].value == 95.0
    # A 95-minute nap appears nowhere in the night itself.
    assert state.sleep.total_sleep_min == 430.0
    assert (state.sleep.rem_min, state.sleep.core_min, state.sleep.deep_min) == (
        90.0, 250.0, 60.0,
    )


async def test_a_nightly_record_carrying_naps_is_refused():
    entry, dispatch = make_entry(), mock.Mock()
    body = v4(buckets={"nightly": [night(nap_total_min=30.0)]})

    response, payload, importer = await post(body, entry, dispatch)

    assert response.status == 400
    assert payload["error"] == "nightly_nap_fields_moved"
    importer.assert_not_awaited()
    dispatch.assert_not_called()

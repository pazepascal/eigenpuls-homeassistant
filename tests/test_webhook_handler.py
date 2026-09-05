"""Status codes the handler returns — the contract the iOS app reacts to."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import streams

from custom_components.apple_health_sync import AppleHealthSyncRuntimeData
from custom_components.apple_health_sync.payload import MAX_BODY_BYTES
from custom_components.apple_health_sync.state import HealthState
from custom_components.apple_health_sync.webhook import _make_handler

TOKEN = "correct-horse-battery-staple"
CHUNK = 16 * 1024
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


def make_request(body: bytes, *, token: str | None = TOKEN, content_length=...):
    protocol = mock.Mock(_reading_paused=False)
    reader = streams.StreamReader(protocol, limit=2**16, loop=asyncio.get_event_loop())
    chunks = [body[i : i + CHUNK] for i in range(0, len(body), CHUNK)] or [b""]
    reader.feed_data(chunks[0])

    async def deliver_rest() -> None:
        for chunk in chunks[1:]:
            await asyncio.sleep(0)
            reader.feed_data(chunk)
        reader.feed_eof()

    _FEEDERS.append(asyncio.get_event_loop().create_task(deliver_rest()))

    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return SimpleNamespace(
        headers=headers,
        content_length=len(body) if content_length is ... else content_length,
        content=reader,
    )


def make_entry():
    entry = SimpleNamespace(entry_id="entry-1")
    entry.runtime_data = AppleHealthSyncRuntimeData(
        token=TOKEN, webhook_id="hook-1", state=HealthState()
    )
    return entry


async def call(request, entry=None):
    entry = entry or make_entry()
    handler = _make_handler(entry)
    with mock.patch(
        "custom_components.apple_health_sync.webhook.async_dispatcher_send"
    ):
        response = await handler(mock.Mock(), "hook-1", request)
    return response, json.loads(response.body)


def sync_body(sample_count: int) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "type": "sync",
            "sent_at": "2026-09-02T21:00:00Z",
            "device": {"name": "iPhone"},
            "samples": [
                {
                    "metric": "heart_rate",
                    "uuid": f"3F2504E0-4F89-11D3-9A0C-{i:012X}",
                    "start": "2026-09-02T20:14:31Z",
                    "end": "2026-09-02T20:14:31Z",
                    "value": 62.5,
                    "unit": "count/min",
                    "source": "Pascal's Apple Watch Ultra 2",
                }
                for i in range(sample_count)
            ],
        }
    ).encode()


# --- The reported bug -------------------------------------------------------


async def test_multi_chunk_batch_is_accepted_not_reported_as_invalid_json():
    """The exact failure: a ~100 KB batch answered 400 invalid_json."""
    body = sync_body(500)
    assert len(body) > CHUNK

    response, payload = await call(make_request(body))

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["accepted"]["samples"] == 500
    assert payload["rejected"] == []


async def test_small_ping_still_works():
    """The case that always worked — it fits in one chunk."""
    body = json.dumps(
        {
            "version": 1,
            "type": "ping",
            "sent_at": "2026-09-02T21:00:00Z",
            "device": {"name": "iPhone"},
        }
    ).encode()

    response, payload = await call(make_request(body))

    assert response.status == 200
    assert payload["pong"] is True


async def test_multi_chunk_batch_updates_state():
    entry = make_entry()
    response, _ = await call(make_request(sync_body(500)), entry)

    assert response.status == 200
    assert entry.runtime_data.state.heart_rate is not None
    assert entry.runtime_data.state.last_sync is not None


# --- Limits and malformation still behave -----------------------------------


async def test_declared_oversize_is_refused_before_reading():
    response, payload = await call(
        make_request(b"{}", content_length=MAX_BODY_BYTES + 1)
    )
    assert response.status == 413
    assert payload["error"] == "payload_too_large"


async def test_undeclared_oversize_is_refused_while_reading():
    """Content-Length absent (chunked): the streaming limit must still fire."""
    body = b"x" * (MAX_BODY_BYTES + CHUNK)
    response, payload = await call(make_request(body, content_length=None))

    assert response.status == 413
    assert payload["error"] == "payload_too_large"


async def test_genuinely_malformed_json_returns_invalid_json():
    response, payload = await call(make_request(b'{"version": 1,,,}'))
    assert response.status == 400
    assert payload["error"] == "invalid_json"


async def test_unsupported_version_is_not_reported_as_invalid_json():
    body = json.dumps(
        {
            "version": 99,
            "type": "sync",
            "sent_at": "2026-09-02T21:00:00Z",
            "device": {},
        }
    ).encode()
    response, payload = await call(make_request(body))

    assert response.status == 400
    assert payload["error"] == "unsupported_version"


async def test_wrong_token_is_rejected_without_reading_the_body():
    response, payload = await call(make_request(sync_body(500), token="wrong"))
    assert response.status == 401
    assert payload["error"] == "unauthorized"
    # No token material may appear in the response body.
    assert TOKEN not in json.dumps(payload)


async def test_missing_authorization_header_is_rejected():
    response, _ = await call(make_request(b"{}", token=None))
    assert response.status == 401

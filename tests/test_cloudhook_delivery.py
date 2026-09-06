"""Deliveries that arrive through a Home Assistant Cloud cloudhook.

These do not arrive as an aiohttp request. Cloud relays the body and builds a
`homeassistant.util.aiohttp.MockRequest`, which is a different shape in two ways
that both crashed the handler in production:

* it has no `content_length` attribute at all, and
* its `content` is a `MockStreamReader`, which offers `read()` but not
  `iter_chunked()`.

Neither was caught before because every existing webhook test constructs a
stand-in with a real `StreamReader` and a `content_length` — a fake shaped like
the path that already worked. This file uses the real class instead, so the
thing under test is the object Cloud actually passes.

Found by a first real cloudhook pairing, not by review: the productive
installation had always reached Home Assistant over Remote UI, which is an
ordinary HTTP request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from homeassistant.util.aiohttp import MockRequest

from custom_components.apple_health_sync import AppleHealthSyncRuntimeData
from custom_components.apple_health_sync.payload import MAX_BODY_BYTES
from custom_components.apple_health_sync.state import HealthState
from custom_components.apple_health_sync.webhook import _make_handler

TOKEN = "correct-horse-battery-staple"


def cloudhook_request(body: bytes, *, token: str | None = TOKEN) -> MockRequest:
    """Exactly how Home Assistant Cloud hands a relayed webhook to a handler."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return MockRequest(
        content=body, mock_source="cloud", method="POST", headers=headers
    )


def make_entry():
    entry = SimpleNamespace(entry_id="entry-1")
    entry.runtime_data = AppleHealthSyncRuntimeData(
        token=TOKEN, webhook_id="hook-1", state=HealthState()
    )
    return entry


async def call(request):
    handler = _make_handler(make_entry())
    with mock.patch(
        "custom_components.apple_health_sync.webhook.async_dispatcher_send"
    ):
        response = await handler(mock.Mock(), "hook-1", request)
    return response, json.loads(response.body)


def ping_body() -> bytes:
    return json.dumps(
        {
            "version": 4,
            "type": "ping",
            "sent_at": "2026-09-06T08:00:00Z",
            "device": {"name": "iPhone"},
        }
    ).encode()


def sync_body() -> bytes:
    return json.dumps(
        {
            "version": 4,
            "type": "sync",
            "sent_at": "2026-09-06T08:00:00Z",
            "device": {"name": "iPhone"},
            "sync": {"id": "cloudhook-1", "final": False},
            "snapshot": {},
        }
    ).encode()


async def test_a_ping_over_a_cloudhook_is_answered():
    """The exact call the app makes to test a connection after pairing."""
    response, body = await call(cloudhook_request(ping_body()))
    assert response.status == 200
    assert body["ok"] is True


async def test_a_sync_over_a_cloudhook_is_accepted():
    response, body = await call(cloudhook_request(sync_body()))
    assert response.status == 200
    assert body["ok"] is True


async def test_a_body_split_across_reads_arrives_whole():
    """`MockStreamReader.read(n)` returns n bytes at a time, so a body larger
    than one chunk is only complete if the handler loops to EOF."""
    body = json.dumps(
        {
            "version": 4,
            "type": "sync",
            "sent_at": "2026-09-06T08:00:00Z",
            "device": {"name": "iPhone", "note": "x" * 200_000},
            "sync": {"id": "big", "final": False},
            "snapshot": {},
        }
    ).encode()
    assert len(body) > 64 * 1024  # more than one read
    response, payload = await call(cloudhook_request(body))
    assert response.status == 200
    assert payload["ok"] is True


async def test_an_oversized_cloudhook_body_is_still_refused():
    """No `content_length` to check up front, so the limit has to hold while
    reading - otherwise losing the cheap check would also lose the real one."""
    body = b'{"version": 4, "type": "ping", "pad": "' + b"x" * (MAX_BODY_BYTES + 1024)
    response, payload = await call(cloudhook_request(body))
    assert response.status == 413
    assert payload["error"] == "payload_too_large"


async def test_a_bad_token_over_a_cloudhook_is_still_rejected():
    response, payload = await call(cloudhook_request(ping_body(), token="wrong"))
    assert response.status == 401
    assert payload["error"] == "unauthorized"

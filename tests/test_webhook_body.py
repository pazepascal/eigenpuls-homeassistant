"""Request-body reading — regression cover for the `400 invalid_json` bug.

The receiver used `StreamReader.read(n)`, which waits only until *some* data is
buffered and then returns what is available. A body split across TCP segments
came back truncated and then failed JSON parsing, so a well-formed ~100 KB batch
was reported to the client as malformed. A small ping never reproduced it,
because it arrives in a single segment.

These tests drive a real `aiohttp.StreamReader` with data still in flight, so
they exercise the genuine semantics rather than a stand-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest import mock

import pytest
from aiohttp import streams

from custom_components.apple_health_sync.payload import MAX_BODY_BYTES, parse
from custom_components.apple_health_sync.webhook import BodyTooLarge, read_body

CHUNK = 16 * 1024  # a typical TCP-segment-sized delivery

# Feeder tasks created by stream_arriving_in_chunks. Tests that stop reading
# early (an abort, or the truncation demo) legitimately leave a feeder pending,
# and the Home Assistant harness fails a test that leaks a task.
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


def make_stream() -> streams.StreamReader:
    protocol = mock.Mock(_reading_paused=False)
    return streams.StreamReader(protocol, limit=2**16, loop=asyncio.get_event_loop())


def stream_arriving_in_chunks(body: bytes, size: int = CHUNK) -> streams.StreamReader:
    """A stream whose first chunk is present and whose remainder arrives later.

    This is the shape that broke the old reader: feeding everything up front
    hides the bug, because the buffer already holds the whole body.
    """
    chunks = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    reader = make_stream()
    reader.feed_data(chunks[0])

    async def deliver_rest() -> None:
        for chunk in chunks[1:]:
            await asyncio.sleep(0)  # yield control: data arrives later
            reader.feed_data(chunk)
        reader.feed_eof()

    _FEEDERS.append(asyncio.get_event_loop().create_task(deliver_rest()))
    return reader


def payload_bytes(sample_count: int) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "type": "sync",
            "sent_at": "2026-09-02T21:00:00Z",
            "device": {"name": "iPhone", "model": "iPhone17,1"},
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


# --- The regression itself --------------------------------------------------


async def test_the_old_reader_really_did_truncate():
    """Pins the root cause, so nobody reintroduces `read(n)` believing it safe."""
    body = payload_bytes(500)
    assert len(body) > CHUNK, "body must span more than one chunk to be meaningful"

    truncated = await stream_arriving_in_chunks(body).read(MAX_BODY_BYTES + 1)

    assert len(truncated) < len(body)
    with pytest.raises(ValueError):
        json.loads(truncated)


async def test_body_split_across_chunks_is_read_in_full():
    body = payload_bytes(500)
    assert len(body) > CHUNK

    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)

    assert raw == body
    assert len(parse(json.loads(raw)).samples) == 500


@pytest.mark.parametrize("size", [1, 7, 1024, CHUNK, 64 * 1024])
async def test_read_is_complete_at_any_chunk_granularity(size):
    body = payload_bytes(120)
    raw = await read_body(stream_arriving_in_chunks(body, size=size), MAX_BODY_BYTES)
    assert raw == body


# --- Limits -----------------------------------------------------------------


async def test_payload_just_below_the_limit_is_read():
    body = b'{"a":"' + b"x" * (MAX_BODY_BYTES - 10) + b'"}'
    assert len(body) <= MAX_BODY_BYTES

    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)
    assert len(raw) == len(body)


async def test_body_exactly_at_the_limit_is_accepted():
    body = b"x" * MAX_BODY_BYTES
    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)
    assert len(raw) == MAX_BODY_BYTES


async def test_one_byte_over_the_limit_is_refused():
    body = b"x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(BodyTooLarge):
        await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)


async def test_oversized_body_is_not_buffered_in_full():
    """The reader must abort mid-stream, not accumulate then check."""
    limit = 100 * 1024
    body = b"x" * (10 * 1024 * 1024)  # 100x the limit

    reader = stream_arriving_in_chunks(body, size=CHUNK)
    with pytest.raises(BodyTooLarge):
        await read_body(reader, limit)


# --- No Content-Length / chunked transfer -----------------------------------


async def test_works_without_content_length():
    """read_body never consults Content-Length; EOF terminates it.

    This is what makes chunked transfer encoding work, where the header is absent.
    """
    body = payload_bytes(300)
    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)
    assert raw == body


async def test_empty_body_reads_as_empty():
    reader = make_stream()
    reader.feed_eof()
    assert await read_body(reader, MAX_BODY_BYTES) == b""


# --- Genuine malformation still reported as malformed -----------------------


async def test_genuinely_malformed_json_is_still_malformed():
    body = b'{"version": 1, "type": "sync",,,}'
    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)

    assert raw == body
    with pytest.raises(ValueError):
        json.loads(raw)


async def test_client_truncated_body_is_reported_as_malformed():
    """A client that disconnects mid-body genuinely sent incomplete JSON.

    The reader returns exactly what arrived; the handler then answers 400.
    """
    body = payload_bytes(500)[: 20 * 1024]
    raw = await read_body(stream_arriving_in_chunks(body), MAX_BODY_BYTES)

    assert raw == body  # complete relative to what the client actually sent
    with pytest.raises(ValueError):
        json.loads(raw)

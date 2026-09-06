"""Webhook receiver: authentication, body limits, payload handling."""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any, Final

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_UPDATE
from .payload import MAX_BODY_BYTES, WIRE_VERSION, PayloadError, parse
from .registry import SUPPORTED_FEATURES, SUPPORTED_METRICS
from .statistics import async_blood_pressure_trend, async_import_history

_LOGGER = logging.getLogger(__name__)

#: Rolling windows the blood-pressure trend entities cover, in days.
TREND_PERIOD_DAYS: Final = (7, 30)

# Read granularity. Bounded work per iteration; unrelated to the body limit.
_READ_CHUNK_BYTES = 64 * 1024


class BodyTooLarge(Exception):
    """The request body exceeded MAX_BODY_BYTES while being read."""


async def read_body(content: Any, limit: int) -> bytes:
    """Read a request body in full, refusing to buffer more than `limit`.

    `StreamReader.read(n)` must not be used for this. It waits only until *some*
    data is buffered and then returns whatever is available, up to n - so a body
    split across several TCP segments comes back truncated, and the truncated
    bytes then fail JSON parsing as if the client had sent malformed data. A
    ~100 KB batch reproduces this reliably; a small ping does not, because it
    arrives in a single segment.

    Iterating to EOF is also what makes this work without a Content-Length
    header, as with chunked transfer encoding.

    Raises:
        BodyTooLarge: as soon as the accumulated body passes `limit`, so an
            oversized request is never buffered in full.
    """
    chunks: list[bytes] = []
    total = 0

    if not hasattr(content, "iter_chunked"):
        # Home Assistant Cloud relays a cloudhook by constructing a MockRequest,
        # whose stream is a MockStreamReader: it offers read() and not
        # iter_chunked(). Reading in a loop until EOF applies the same limit to
        # the same bytes; the real path below is left exactly as it was rather
        # than unified, because that one has a year of production behind it and
        # its warning about partial reads is specific to real sockets.
        while True:
            chunk = await content.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise BodyTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    async for chunk in content.iter_chunked(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _server_time() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _authorized(request: web.Request, expected_token: str) -> bool:
    """Constant-time bearer check. Never logs either token."""
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip(), expected_token)


async def async_register(hass: HomeAssistant, entry: ConfigEntry, webhook_id: str) -> None:
    """Register the webhook for a config entry."""
    webhook.async_register(
        hass,
        DOMAIN,
        "Eigenpuls",
        webhook_id,
        _make_handler(entry),
        allowed_methods=["POST"],
        # Payloads arrive over Nabu Casa Remote UI, not only from the LAN.
        local_only=False,
    )


def async_unregister(hass: HomeAssistant, webhook_id: str) -> None:
    webhook.async_unregister(hass, webhook_id)


def _make_handler(entry: ConfigEntry):
    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        runtime = entry.runtime_data

        if not _authorized(request, runtime.token):
            # Actionable but silent: no token material in the log, no detail in
            # the body that would help an attacker distinguish failure modes.
            _LOGGER.warning("Rejected Eigenpuls delivery: authentication failed")
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        # Cheap rejection when the client declares an oversized body up front.
        # Not sufficient on its own: Content-Length is absent under chunked
        # transfer encoding, and a client may understate it - and absent
        # entirely on a cloudhook, where Home Assistant Cloud hands us a
        # MockRequest that has no such attribute at all. The real limit is
        # enforced while reading, so missing it here costs nothing.
        length = getattr(request, "content_length", None)
        if length is not None and length > MAX_BODY_BYTES:
            return _json_response({"ok": False, "error": "payload_too_large"}, status=413)

        try:
            raw = await read_body(request.content, MAX_BODY_BYTES)
        except BodyTooLarge:
            return _json_response({"ok": False, "error": "payload_too_large"}, status=413)

        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return _json_response(
                {"ok": False, "error": "invalid_json", "version": WIRE_VERSION}, status=400
            )

        try:
            payload = parse(body)
        except PayloadError as err:
            return _json_response(
                {"ok": False, "error": err.reason, "version": WIRE_VERSION}, status=400
            )

        if payload.kind == "ping":
            # Must not modify state.
            return _json_response(
                {
                    "ok": True,
                    "pong": True,
                    "version": WIRE_VERSION,
                    # What this receiver understands. A client that reads these
                    # can withhold anything newer instead of losing a delivery
                    # to it. Absent in every release before this one, which the
                    # client must read as "assume only the v4 baseline".
                    "supported_metrics": list(SUPPORTED_METRICS),
                    "supported_features": list(SUPPORTED_FEATURES),
                    "accepted": {"samples": 0, "daily_totals": 0, "deletions": 0},
                    "rejected": [],
                    "server_time": _server_time(),
                }
            )

        # v1: every delivery is a complete sync (legacy behaviour, unchanged).
        # v2: historical batches are transport only. Exactly one completion
        #     delivery carries the snapshot that drives the entities, so a
        #     restart partway through a backfill cannot affect current values,
        #     and a sync that never completes never looks fresh.
        # v3: additionally carries aggregate history for long-term statistics.
        # v4: the same, keyed by metric, plus nightly sleep summaries.
        received_at = datetime.now(UTC)
        completed = payload.version == 1 or payload.is_final

        # Validate-all-then-apply: the aggregate history must be accepted before
        # anything touches the current values. If it is refused, the request
        # fails and no snapshot is applied, no Last Sync advances and no entity
        # update is dispatched - so an aggregate failure can never be reported as
        # a successful sync, and can never corrupt the current sensors either.
        if payload.history is not None and not payload.history.is_empty():
            try:
                await async_import_history(hass, payload.history)
            except HomeAssistantError as err:
                _LOGGER.error("Rejected Eigenpuls delivery: %s", err)
                return _json_response(
                    {
                        "ok": False,
                        "error": "statistics_rejected",
                        "version": payload.version,
                    },
                    status=500,
                )

        # Derived from the durable history that was just updated, and only
        # after it was accepted - a trend must never describe data the receiver
        # refused. The freshly imported window is overlaid because statistics
        # writes are queued, so its newest hours are not yet readable.
        if payload.history is not None:
            for period in TREND_PERIOD_DAYS:
                trend = await async_blood_pressure_trend(
                    hass, days=period, now=received_at, overlay=payload.history
                )
                if trend is not None:
                    runtime.state.blood_pressure_trends[period] = trend

        if payload.version == 1:
            runtime.state.apply(payload, received_at=received_at)
        elif payload.is_final and payload.snapshot is not None:
            runtime.state.apply_snapshot(payload.snapshot, received_at=received_at)

        if completed:
            async_dispatcher_send(hass, SIGNAL_UPDATE.format(entry.entry_id))

        # Counts only - never the values themselves.
        _LOGGER.debug(
            "Accepted Eigenpuls delivery (v%s, sync=%s, final=%s):"
            " %s samples, %s totals, %s deletions,"
            " %s hourly, %s daily, %s nightly, %s rejected",
            payload.version,
            payload.sync_id or "-",
            completed,
            len(payload.samples),
            len(payload.daily_totals),
            len(payload.deletions),
            # Counted across every metric: under v4 these are no longer heart
            # rate and steps alone.
            len(payload.history.hourly) if payload.history else 0,
            len(payload.history.daily) if payload.history else 0,
            len(payload.history.nightly) if payload.history else 0,
            len(payload.rejected),
        )

        return _json_response(
            {
                "ok": True,
                "version": payload.version,
                # Also on the sync response, so a client that never pings still
                # learns what this receiver takes.
                "supported_metrics": list(SUPPORTED_METRICS),
                "supported_features": list(SUPPORTED_FEATURES),
                "completed": completed,
                "accepted": {
                    "samples": len(payload.samples),
                    "daily_totals": len(payload.daily_totals),
                    "deletions": len(payload.deletions),
                },
                "rejected": [rejection.as_dict() for rejection in payload.rejected],
                "server_time": _server_time(),
            }
        )

    return _handle

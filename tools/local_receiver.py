#!/usr/bin/env python3
"""Standalone test receiver for the Apple Health Sync wire format.

Mimics the Home Assistant webhook using the integration's own payload parser, so
the iOS app can be exercised end to end before Home Assistant is involved.
Standard library only - no Home Assistant, no third-party packages.

    python3 tools/local_receiver.py --token dev-token --port 8099

Then point the iOS app at http://<mac-ip>:8099/api/webhook/dev with that token
and the local-development override enabled.

Never run this on an untrusted network: it speaks plain HTTP by design.
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "homeassistant" / "custom_components"))

from apple_health_sync.payload import (  # noqa: E402
    MAX_BODY_BYTES,
    WIRE_VERSION,
    PayloadError,
    parse,
)
from apple_health_sync.state import HealthState  # noqa: E402

STATE = HealthState()
TOKEN = ""
SHOW_VALUES = False


class Handler(BaseHTTPRequestHandler):
    server_version = "AppleHealthSyncTestReceiver/1"

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), TOKEN):
            print("  -> 401 authentication failed")
            self._send({"ok": False, "error": "unauthorized"}, 401)
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send({"ok": False, "error": "payload_too_large"}, 413)
            return

        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self._send({"ok": False, "error": "invalid_json"}, 400)
            return

        try:
            payload = parse(body)
        except PayloadError as err:
            print(f"  -> 400 {err.reason}")
            self._send({"ok": False, "error": err.reason, "version": WIRE_VERSION}, 400)
            return

        if payload.kind == "ping":
            print("  -> 200 pong")
            self._send({
                "ok": True, "pong": True, "version": WIRE_VERSION,
                "accepted": {"samples": 0, "daily_totals": 0, "deletions": 0},
                "rejected": [], "server_time": datetime.now(UTC).isoformat(),
            })
            return

        STATE.apply(payload, received_at=datetime.now(UTC))
        print(
            f"  -> 200 samples={len(payload.samples)} "
            f"totals={len(payload.daily_totals)} "
            f"deletions={len(payload.deletions)} rejected={len(payload.rejected)}"
        )
        for rejection in payload.rejected:
            print(f"     rejected {rejection.collection}[{rejection.index}]: {rejection.reason}")
        if SHOW_VALUES:
            # Opt-in only: these are real health values.
            print(f"     heart_rate={STATE.heart_rate} steps={STATE.steps} day={STATE.steps_day}")

        self._send({
            "ok": True, "version": WIRE_VERSION,
            "accepted": {
                "samples": len(payload.samples),
                "daily_totals": len(payload.daily_totals),
                "deletions": len(payload.deletions),
            },
            "rejected": [r.as_dict() for r in payload.rejected],
            "server_time": datetime.now(UTC).isoformat(),
        })

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")


def main() -> int:
    global TOKEN, SHOW_VALUES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="bearer token the app must present")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="print received health values (off by default)",
    )
    args = parser.parse_args()

    TOKEN = args.token
    SHOW_VALUES = args.show_values

    print(f"Apple Health Sync test receiver on http://0.0.0.0:{args.port}/")
    print("Any path is accepted. Ctrl-C to stop.")
    try:
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Wire format v2

Status: **Superseded by `payload-v3.md`**, but still accepted by the receiver.
Sections 2-5 (envelope, `sync`, snapshot, completion) remain normative for v3.

Part of the versioned wire contract; see `protocol/README.md`.

## 1. Why v2 exists

v1 let **historical sample batches drive the current-value entities**. Two defects
followed, both found by real-device validation:

- A multi-batch import wrote `last_sync` once per batch (916 times for 457,741
  samples), so an import that failed at batch 900 would still have looked fresh —
  defeating the stalled-app signal the client depends on.
- The receiver accumulated current values across requests in memory. A Home
  Assistant restart mid-import could lose that accumulation while the device
  anchor had already advanced past those pages.

`HKAnchoredObjectQueryDescriptor` exposes **no `sortDescriptors`** — anchored
queries return samples in *anchor* order (store insertion/modification sequence),
which is **not** measurement-date order. So "the last batch holds the newest
reading" is not true, and no recovery scheme may assume it.

**v2 separates the two concerns.** Historical batches are transport only. Exactly
one *completion* delivery carries a snapshot of current values, read directly from
HealthKit. Current-value correctness therefore depends on nothing the receiver
accumulates, which is what makes a restart mid-backfill irrelevant.

### Why a version bump rather than optional fields

A v1-only receiver would ignore unknown `sync`/`snapshot` keys, still fold history
into the current Heart Rate, and answer **200**. Silent semantic corruption of
health data is unacceptable, so the version gate exists to make the mismatch
**fail loudly** as `unsupported_version`.

## 2. Envelope

```json
{
  "version": 2,
  "type": "sync",
  "sent_at": "2026-09-03T21:15:00Z",
  "device": { "name": "iPhone", "model": "iPhone17,1", "os_version": "26.5" },
  "sync": { "id": "3F2504E0-…", "final": false },
  "samples": [], "daily_totals": [], "deletions": []
}
```

`samples`, `daily_totals`, `deletions` are unchanged from v1 (`payload-v1.md` §3–§5),
as are the limits (§8) and per-item rejection (§7).

### `sync` — required on every `type: "sync"` delivery

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | no | Diagnostic correlation only (§4). |
| `final` | bool | **yes** | `true` marks the single completion delivery. |

A `type: "sync"` payload without a `sync` object, or without a boolean `final`, is
rejected `missing_sync` (400) — a client that omits it would otherwise never
complete, and silently never update the entities. `type: "ping"` needs no `sync`.

## 3. Completion and the snapshot

`final: true` **must** carry `snapshot`, else `missing_snapshot` (400).

```json
{
  "version": 2, "type": "sync", "sent_at": "…", "device": { … },
  "sync": { "id": "3F2504E0-…", "final": true },
  "snapshot": {
    "heart_rate": { "value": 64, "unit": "count/min",
                    "measured_at": "2026-09-03T20:41:12Z", "source": "Apple Watch" },
    "steps_today": { "value": 359, "unit": "count",
                     "date": "2026-09-03", "time_zone": "Europe/Berlin" }
  }
}
```

- `heart_rate` comes from an **independent latest-sample query**, never from the
  batch stream.
- `steps_today` is the current cumulative total for the device's local day.
- A member that is **absent or `null` leaves that sensor unchanged** — never
  clears it. HealthKit cannot distinguish a denied read from absent data, so
  absence is not a measurement.
- The snapshot is applied by **replacement**, which is what makes a retried
  completion idempotent.

## 4. `sync.id` lifecycle

Diagnostic correlation only. **No correctness guarantee may depend on it.**

- A fresh UUID per `SyncEngine.sync()` invocation.
- Constant across every delivery of that invocation.
- Never persisted; **not reused by a later resume**, because a resume covers a
  different page range and produces a different snapshot — it is a new logical
  sync, not a continuation.
- The receiver never keys state off it, so no dedupe or expiry is needed.

## 5. Receiver behaviour

| Delivery | Data | `last_sync` | Entity write |
|---|---|---|---|
| v2, `final: false` | validated, counted, **not** applied to current values | no | no |
| v2, `final: true` | snapshot applied by replacement | yes | yes, exactly one |
| v1 (legacy) | applied as in v1 | yes | yes |

The response echoes the request's `version` and adds `"completed": <bool>`.

## 6. Client obligations

1. Page the read (`HKAnchoredObjectQueryDescriptor.limit`); send each page as
   bounded deliveries with `final: false`.
2. Advance the anchor only after every delivery of a page is accepted.
3. After the final page, read the snapshot **fresh from HealthKit** and send one
   `final: true` delivery.
4. If the paging ceiling is reached with data possibly remaining, **send no
   completion** and report the sync as incomplete.

## 7. Failure and retry

| Situation | Result |
|---|---|
| Delivery fails mid-page | Anchor unmoved for that page; no completion; retry re-reads it. |
| Ceiling reached | Accepted pages durable; no completion; reported incomplete; resumable. |
| Completion fails | All data accepted but no completion. Next sync reads 0 samples and sends only a completion — cheap recovery. |
| Completion accepted, response lost | Receiver already correct. Next sync re-sends a fresh completion; replacement makes it harmless. |
| Restart mid-backfill | Accumulated receiver state is irrelevant under v2; the next completion sets the values. |

## 8. Compatibility

| | v1 receiver | v2 receiver |
|---|---|---|
| **v1 client** | works | works (legacy path retained) |
| **v2 client** | **rejected `unsupported_version` (400)** | works |

The bottom-left cell is the point of the version bump: loud failure instead of a
silently wrong sensor. **Deployment order is therefore receiver first, then
Test Connection, then the client** — a v2 `ping` against a v1 receiver fails
immediately and visibly.

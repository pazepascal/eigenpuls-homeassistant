# Wire format v3

Status: **Superseded by `payload-v4.md`**, but still accepted by the receiver and
not deprecated. Supersedes `payload-v2.md`, which the receiver still accepts (§7),
as it still accepts v1.

Part of the versioned wire contract; see `protocol/README.md`.

## 1. Why v3 exists

v3 adds **compact aggregate history**: a rolling window of pre-aggregated buckets
that Home Assistant stores as long-term statistics, so it can show short-term
trends without ever holding a raw HealthKit archive.

### Why a version bump rather than an additive field

An additive `buckets` field in v2 would have been technically backward
compatible. It was rejected: a **v3 client against a v2 receiver would have
received HTTP 200 while the receiver silently discarded the trend history**. For
health data, silently losing a promised feature is not an acceptable failure
mode. The version gate turns that into an explicit `unsupported_version`.

This is a weaker justification than the v1→v2 bump (which prevented *wrong*
current values, not merely absent history) — and it was taken deliberately, to
keep one consistent rule: **the wire version changes whenever the receiver's
obligations change.**

## 2. Envelope

Everything from v2 is unchanged — `sync`, `snapshot`, and the v2 completion
semantics are carried over verbatim. v3 adds one optional key:

```json
{
  "version": 3,
  "type": "sync",
  "sent_at": "2026-09-03T21:15:00Z",
  "device": { "name": "iPhone", "model": "iPhone17,1", "os_version": "26.5" },
  "sync": { "id": "3F2504E0-…", "final": true },
  "snapshot": {
    "heart_rate":  { "value": 64, "unit": "count/min",
                     "measured_at": "2026-09-03T20:41:12Z", "source": "Apple Watch" },
    "steps_today": { "value": 359, "unit": "count",
                     "date": "2026-09-03", "time_zone": "Europe/Berlin" }
  },
  "buckets": {
    "heart_rate_hourly": [
      { "start": "2026-09-03T14:00:00Z",
        "mean": 72.4, "min": 58, "max": 141, "count": 37 }
    ],
    "steps_daily": [
      { "date": "2026-09-03", "time_zone": "Europe/Berlin", "total": 8423 }
    ]
  }
}
```

`buckets` is optional; a sync with no new history is valid.

## 3. `buckets.heart_rate_hourly`

| Field | Type | Required | Notes |
|---|---|---|---|
| `start` | RFC 3339 | yes | **Hour-aligned UTC.** |
| `mean` / `min` / `max` | number | yes | Must satisfy `min <= mean <= max`. |
| `count` | int ≥ 0 | no | Samples behind the aggregate; diagnostic. |

**Why UTC hours.** Home Assistant keys long-term statistics on hour-aligned UTC
starts, and UTC hours are unaffected by DST. Anchoring hourly buckets to *local*
midnight would produce 23- and 25-hour days across a transition and misalign
every bucket in them.

## 4. `buckets.steps_daily`

| Field | Type | Required | Notes |
|---|---|---|---|
| `date` | `YYYY-MM-DD` | yes | **Local** calendar day in `time_zone`. |
| `time_zone` | IANA id | yes | Must resolve on the receiver. |
| `total` | number ≥ 0 | yes | The day's cumulative total. |

**Why local days** — the opposite choice from heart rate, deliberately. "Steps
today" means the user's day, and HealthKit already accounts for 23/25-hour DST
days. The receiver converts local midnight to a UTC instant for storage.

Known limitation: this assumes a whole-hour UTC offset, since statistics rows are
hour-aligned. A 30- or 45-minute offset zone would not align. Out of scope.

## 5. Validation — all-or-nothing

Unlike `samples` (rejected per item), a malformed bucket **rejects the entire
request with 400**. Nothing is applied: no statistics, no snapshot, no Last Sync,
no entity update. That is what makes "validate-all-then-apply" meaningful.

Reason codes: `bad_buckets`, `bad_bucket`, `bad_bucket_missing_field`,
`bad_bucket_bad_value`, `bad_bucket_bad_timestamp`, `bad_bucket_future_timestamp`,
`bad_bucket_time_zone`, `bad_bucket_count`, `bucket_start_not_hour_aligned`,
`bucket_range_inconsistent`, `duplicate_hourly_bucket`, `duplicate_daily_bucket`,
`too_many_hourly_buckets`, `too_many_daily_buckets`.

Duplicate keys within one request are refused because they would make the import
order-dependent.

## 6. Limits

| Limit | Value | Rationale |
|---|---|---|
| `heart_rate_hourly` | 400 | 14-day recovery window is 336; leaves headroom, forbids an archive. |
| `steps_daily` | 40 | 14 days plus margin. |

The whole v3 payload for a full 7-day window is ~22 KB — about 2% of the 1 MiB
body limit, and one request.

## 7. Compatibility

| | v1 receiver | v2 receiver | v3 receiver |
|---|---|---|---|
| v1 client | works | works | works (legacy path) |
| v2 client | rejected | works | works |
| **v3 client** | **rejected** | **rejected** | works |

Both rejections are `unsupported_version` (400). **Deployment order is receiver
first → Test Connection → client**: a v3 `ping` against an older receiver fails
immediately and visibly, before any data moves.

A v2 envelope carrying a `buckets` key does **not** activate v3 behaviour — the
key is ignored, because buckets are a v3 obligation.

## 8. Durability — what the receiver does and does not promise

`async_add_external_statistics` validates synchronously and then **queues** the
write. It cannot report whether the row was committed.

The receiver therefore promises **"validated and accepted"**, never "durably
persisted". A recorder-internal failure after acceptance is not reported — and is
healed by the next overlapping 7–14 day window, which recomputes the same hours
and days. This is stated plainly rather than papered over with a transaction that
the API cannot provide.

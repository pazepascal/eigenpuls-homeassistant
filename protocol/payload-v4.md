# Wire format v4

Status: **current and live.** Implemented on both sides and deployed; the iOS
client sends v4.

v1, v2 and v3 remain supported. v3 is **not** deprecated and stays a legitimate
client protocol.

## 1. Why v4 exists

v4 carries the Phase 3A metrics — resting heart rate, heart rate variability,
respiratory rate, blood oxygen, active energy, walking and running distance —
and nightly sleep summaries.

### Why a version bump rather than additive fields

The rule this project has applied since v2: **an older receiver must never
answer HTTP 200 while silently discarding health data the client believes it has
durably stored.**

Added as new keys inside v3, the new buckets would reach a v3 receiver that
recognises none of them. It would validate the envelope, store heart rate and
steps, return 200, and drop a week of sleep without a word. The client would
have no way to tell. So a v3-only receiver must refuse a v4 payload outright,
and that requires the version number to change.

### Why there should not need to be a v5

The version bump would be worth little if every future metric forced another
one. v4 therefore changes the *shape* rather than adding fields: buckets are
keyed by a `metric` identifier, and what that identifier means comes from a
registry both sides share (`custom_components/apple_health_sync/registry.py`).

A future metric whose semantics fit one of the existing bucket families is then
a registry entry on each side — no new version — because the receiver's
obligation does not change: *store every bucket you are given, keyed by metric*.
The Phase 3B candidates (weight, body fat, blood pressure) fit the daily
families and need no protocol change.

A new version is still required for a metric that needs a genuinely new *family*
— one whose storage semantics no existing family expresses.

## 2. Envelope

Unchanged from v2 apart from the version number.

```json
{
  "version": 4,
  "type": "sync",
  "sent_at": "2026-09-03T14:22:05Z",
  "device": { "name": "iPhone", "model": "iPhone17,1", "os_version": "26.5" },
  "sync": { "id": "5B1E…", "final": true },
  "snapshot": { … },
  "buckets": { "hourly": [ … ], "daily": [ … ], "nightly": [ … ] }
}
```

`type: "ping"` still carries and applies nothing.

## 3. The metric registry

One table, `registry.py`, is the contract. It is **closed**: an unknown metric
identifier is rejected, never stored and never ignored.

| metric | family | statistic_id | unit | unit_class | mean type | sum |
|---|---|---|---|---|---|---|
| `heart_rate` | hourly discrete | `apple_health_sync:heart_rate` | bpm | — | arithmetic | no |
| `steps` | daily cumulative | `apple_health_sync:steps_daily` | steps | — | none | yes |
| `resting_heart_rate` | daily discrete | `apple_health_sync:resting_heart_rate` | bpm | — | arithmetic | no |
| `hrv_sdnn` | daily discrete | `apple_health_sync:hrv_sdnn` | ms | duration | arithmetic | no |
| `respiratory_rate` | daily discrete | `apple_health_sync:respiratory_rate` | breaths/min | — | arithmetic | no |
| `oxygen_saturation` | daily discrete | `apple_health_sync:oxygen_saturation` | % | unitless | arithmetic | no |
| `active_energy` | daily cumulative | `apple_health_sync:active_energy` | kcal | energy | none | yes |
| `distance_walking_running` | daily cumulative | `apple_health_sync:distance` | km | distance | none | yes |
| `nap_total` | daily discrete | `apple_health_sync:nap_total` | min | duration | arithmetic | no |
| `nap_count` | daily discrete | `apple_health_sync:nap_count` | naps | — | arithmetic | no |
| `body_mass` | hourly discrete | `apple_health_sync:body_mass` | kg | mass | arithmetic | no |
| `body_fat_percentage` | hourly discrete | `apple_health_sync:body_fat_percentage` | % | unitless | arithmetic | no |
| `blood_pressure_systolic` | hourly discrete | `apple_health_sync:blood_pressure_systolic` | mmHg | pressure | arithmetic | no |
| `blood_pressure_diastolic` | hourly discrete | `apple_health_sync:blood_pressure_diastolic` | mmHg | pressure | arithmetic | no |
| `vo2_max` | hourly discrete | `apple_health_sync:vo2_max` | ml/kg/min | — | arithmetic | no |
| `workout_count` | daily cumulative | `apple_health_sync:workout_count` | workouts | — | none | yes |
| `workout_duration` | daily cumulative | `apple_health_sync:workout_duration` | min | duration | none | yes |
| `workout_energy` | daily cumulative | `apple_health_sync:workout_energy` | kcal | energy | none | yes |

Blood-pressure hourly buckets additionally carry the optional `count` field —
the number of complete correlations averaged into that hour. Both halves of an
hour must be present and must agree on the count, or the payload is rejected
(`blood_pressure_hours_mismatched`, `blood_pressure_counts_mismatched`,
`blood_pressure_count_not_positive`): they come from one correlation on the
device, so a disagreement means the two series were built from different sets.

The receiver fans that count into a derived series,
`apple_health_sync:blood_pressure_count`, which is **not** a wire metric and
cannot arrive as a bucket of its own. It is the measurement weight behind the
hourly means, and it exists because Home Assistant's arithmetic rollup is an
unweighted mean of hourly means — proven against a real recorder — so an hour
holding three readings would otherwise count for no more than an hour holding
one. One shared series serves both halves, because they average the same
correlations by construction.

Rolling trends are then derived by Home Assistant, never sent by the client:

    systolic  = Σ(hourly_systolic_mean × count) / Σ count
    diastolic = Σ(hourly_diastolic_mean × count) / Σ count

with one shared denominator, over `now` floored to the hour less 7 or 30 × 24
hours. Hour-based rather than calendar-based because the rows are hourly and
calendar days are 23 or 25 hours long twice a year. An hour contributes only
when both means and a positive count are present, so history written before
counts existed is skipped rather than folded in at the wrong weight.

`unit_class` was **verified against the installed Home Assistant**, not assumed.
The results are not guessable: `bpm`, `steps`, `breaths/min` and `naps` map to no
converter, while `kcal` → `energy`, `km` → `distance`, `%` → `unitless`,
`ms`/`min` → `duration`, `kg` → `mass` and `mmHg` → `pressure`. A wrong value
here is refused at import time, and the recorder-backed tests read the stored
metadata back rather than trusting what was passed in.

## 4. Bucket families

Three arrays on the wire; four semantic families, because `daily` splits by what
the registry says the metric is. Which one applies is decided by the registry,
never inferred from which fields happen to be present.

**A bucket object is closed.** It carries the keys listed for its family and
nothing else; anything further is rejected, whole request, no partial apply:

| | rejected as |
|---|---|
| a field the registry knows but does not permit for this metric — `min` on `resting_heart_rate` | `bad_bucket_unexpected_field` |
| a key nothing here reads at all — a typo, or a field from a newer client | `bad_bucket_unknown_field`, and `bad_nightly_unknown_field` for a nightly record |

Two codes rather than one because a client author needs to tell "this receiver
stores that, but not for this metric" from "nothing here reads that".

This is not a forward-compatibility hatch that was closed; it is the same rule
the metric registry and the bucket-kind check already apply, finally applied to
the field level too. A client with something new to say says it in a new
protocol version — which an older receiver refuses outright, before any data
moves, instead of answering 200 and storing part of it.

### `buckets.hourly` — hourly discrete

```json
{ "metric": "heart_rate", "start": "2026-09-03T13:00:00Z",
  "mean": 72.4, "min": 58.0, "max": 141.0, "count": 61 }
```

`start` must be hour-aligned UTC. `count` is optional. Requires `mean`, `min`
and `max`.

### `buckets.daily` — daily discrete

```json
{ "metric": "hrv_sdnn", "date": "2026-09-03", "time_zone": "Europe/Berlin",
  "mean": 44.0, "min": 21.0, "max": 88.0 }
```

`resting_heart_rate` is the exception: it requires `mean` **only**, and a `min`
or `max` is *rejected*. Apple derives a single resting value per day, so a spread
would be invented rather than measured.

### `buckets.daily` — daily cumulative

```json
{ "metric": "active_energy", "date": "2026-09-03",
  "time_zone": "Europe/Berlin", "total": 612.0 }
```

Stored as a running sum, exactly like steps.

### `buckets.nightly` — sleep

See section 6.

### Percent

`oxygen_saturation` and `body_fat_percentage` values must be human percent
(0.98 → **98**, 0.18 → **18**). HealthKit's `percentUnit` is documented as a
0.0–1.0 fraction, so the client converts before sending, and the receiver
range-checks 1–100. A fraction stored in a `%` series would be wrong by two
orders of magnitude and look entirely plausible.

### Body composition and blood pressure

These are **hourly** rather than daily. A daily bucket would average a morning
and an evening weigh-in, which differ by a kilogram or more, and the same applies
to blood pressure — that is not a rounding loss but a misleading number. Hourly
keeps the time of day, which is where the signal is, without needing a full event
family.

They are **sparse**: the client reads them over a 90-day lookback but emits only
the hours that actually contain a measurement, so a realistic upload is a few
dozen buckets rather than 90 × 24. The dense metrics keep their 7-day normal and
14-day recovery window unchanged.

**Blood pressure travels as two independent series with one shared current
value.** `blood_pressure_systolic` and `blood_pressure_diastolic` each keep their
own durable statistic, but the current reading arrives as a single composite:

```json
"snapshot": {
  "blood_pressure": {
    "systolic": 128.0, "diastolic": 82.0, "unit": "mmHg",
    "measured_at": "2026-09-03T07:14:00Z", "source": "Omron"
  }
}
```

Neither half has an individual snapshot key, so a lone systolic cannot set the
current value. **The client must read the pair through
`HKCorrelationTypeIdentifierBloodPressure`** and must not query the two
quantities separately and pair them by nearby timestamps — the correlation is the
only source of truth that both halves belong to one measurement, and Home
Assistant does no timestamp pairing of its own. An incomplete pair is rejected
(`blood_pressure_incomplete_pair`) rather than completed, and a pair whose
diastolic exceeds its systolic is rejected as swapped or mismatched
(`blood_pressure_inverted`).

Known limitation: if two readings fall in the same statistics hour, the durable
hourly row is an aggregate rather than one exact pair. The snapshot still carries
the latest real correlated measurement. Solving that in storage would need a full
event family, which Phase 3B.1 deliberately does not introduce.

## 5. Absence is not zero

A day with no measurements produces **no bucket** — never a zero. Zero is a
measurement; absence is not. This holds for every metric, and matters most for
blood oxygen and respiratory rate, which on most days are sampled only during
sleep.

The same rule governs the snapshot: a member that is absent or `null` leaves that
sensor unchanged and never clears it. HealthKit cannot distinguish a denied read
from absent data, so absence must not be recorded as a measurement.

## 6. Sleep

### What the receiver does and does not do

The receiver **never sees a raw sleep segment and never aggregates one**. It
accepts an already-aggregated nightly summary.

That is forced by HealthKit, not chosen for convenience: `HKStatisticsQuery`
operates on quantity samples only, and `sleepAnalysis` is a *category* type. So
for sleep alone, HealthKit performs neither the bucketing nor the cross-source
de-duplication it performs for every other metric here. The client owns both.

### The nightly record

```json
{
  "date": "2026-09-03",
  "time_zone": "Europe/Berlin",
  "total_sleep_min": 431.0,
  "sleep_start": "2026-09-02T21:47:00Z",
  "wake_time": "2026-09-03T06:12:00Z",
  "rem_min": 92.0, "core_min": 250.0, "deep_min": 61.0, "awake_min": 28.0
}
```

A nightly record describes **main sleep only**. `nap_total_min` and `nap_count`
are no longer accepted here and are *rejected* (`nightly_nap_fields_moved`)
rather than ignored — accepting the payload and dropping them would answer 200
while losing a day's naps.

### What a night is

`date` is the **wake date** — the local date the main sleep ended.

Wake-date attribution rather than sleep-start attribution because bedtimes of
23:50 and 00:10 are the same night to a person but fall on different *start*
dates. The wake date puts both on the same day, which is exactly where bedtimes
actually cluster.

Sessions separated by less than **3 hours** are one night
(`SLEEP_SESSION_MERGE_GAP_SECONDS` in `registry.py`). That is a product rule, not
a derived constant: it keeps a normal night-time waking inside one night while
still separating genuinely distinct sleeps. It is named and documented so it
stays easy to change.

### Null is not zero

`rem_min`, `core_min`, `deep_min` and `awake_min` are nullable. Null means **not
measured**, and stays null all the way into storage: a null stage writes no
statistics row for that night at all.

A night tracked by iPhone alone has a real total and no staging whatsoever.
Coercing that to zero would drag every stage average toward zero and misreport
sleep quality in a way nobody would notice. A stage genuinely measured as zero
*does* write a row with the value zero, and the two remain distinguishable.

`inBed` is not sleep and is never counted as such.

### Naps

**Naps are independent daily metrics, not part of the night.** They travel as
ordinary `buckets.daily` entries under `nap_total` and `nap_count`.

A nap belongs to the calendar day, not to the night. Carried as nightly fields
they could only exist on a date that also had a main sleep session, so a day of
naps and no proper night — a shift, an illness, a bad night — had nowhere to be
stored and was silently dropped. As daily metrics they are storable whether or
not that date has a nightly record.

They still never affect total sleep, the stage durations, or the bedtime and
wake-time trends: the seven-night trend remains specifically a **night-sleep**
trend.

The statistic ids are unchanged from when these rode on the nightly record, so
no Home Assistant history migrates and the existing nap entity keeps its
entity_id.

A day with no naps sends no nap bucket at all. Absence is not a measurement; a
nap total genuinely measured as zero is, and the two stay distinguishable.

### Bedtime and wake time

Both are sent as absolute RFC 3339 instants. The receiver derives the stored
statistic as **minutes after 18:00 local on the evening before the wake date**.

An offset rather than a clock time because averaging clock times across midnight
is wrong: 23:30 and 00:30 average to 12:00, not 00:00. Nothing plausible wraps an
18:00 anchor, so a plain arithmetic mean and standard deviation are correct. The
offset is computed by the receiver from the instant and the zone, so the two can
never disagree.

### Validated invariants

Checked, because they indicate a real defect: wake after sleep start; span at
most 24 hours; total sleep no greater than the span; REM + Core + Deep no greater
than the total (the remainder being unspecified sleep); no negative durations;
the wake date within a day of the wake instant's local date; a resolvable zone.

Deliberately **not** checked: that staging is present or complete. Partial Apple
Health data is normal and must be accepted as it is.

### Durable series

| field | statistic_id |
|---|---|
| `total_sleep_min` | `apple_health_sync:sleep_total` |
| `rem_min` | `apple_health_sync:sleep_rem` |
| `core_min` | `apple_health_sync:sleep_core` |
| `deep_min` | `apple_health_sync:sleep_deep` |
| `awake_min` | `apple_health_sync:sleep_awake` |
| derived bedtime offset | `apple_health_sync:sleep_start_offset` |
| derived wake offset | `apple_health_sync:sleep_wake_offset` |
| `nap_total_min` | `apple_health_sync:nap_total` |
| `nap_count` | `apple_health_sync:nap_count` |

All in minutes (`nap_count` in naps), all arithmetic-mean series. Mean rather
than sum because the meaningful rollup for sleep is *average per night*, so Home
Assistant's own week and month aggregation gives the trend directly. Steps are
the opposite case, which is why they are cumulative.

### Training

Home Assistant keeps the latest session in detail and durable daily totals. It is
deliberately **not** a second workout database: Apple Health remains authoritative
for individual history, so there is no event family, no event store, and no
per-workout history in Home Assistant.

```json
"snapshot": {
  "last_workout": {
    "uuid": "F1A2…", "activity": "strength_training",
    "start": "2026-06-10T17:00:00Z", "end": "2026-06-10T18:05:00Z",
    "duration_min": 58.0,
    "active_energy_kcal": 410.0, "distance_km": 7.8,
    "avg_heart_rate_bpm": 126.0, "max_heart_rate_bpm": 158.0,
    "source": "Apple Watch"
  }
}
```

`uuid`, `activity`, `start`, `end` and `duration_min` are required; the rest are
genuinely optional — a strength session records no distance and an unworn watch
records no heart rate, and absent stays absent rather than becoming zero.

`duration_min` is **HealthKit's own pause-aware duration**, not the span between
start and end: a session paused for ten minutes is not ten minutes of training.
It may therefore be shorter than the span, and a value longer than the span is
rejected.

`activity` is one of a small closed vocabulary — `walking`, `running`, `cycling`,
`strength_training`, `functional_strength`, `hiit`, `hiking`, `swimming`,
`rowing`, `elliptical`, `yoga`, `other`. HealthKit has 84 activity types; the
client maps to these and anything it does not recognise becomes `other`, so a
future Apple type never costs a workout. An identifier outside the set is
rejected, keeping the taxonomy intentional. The raw enum value is not sent —
Apple Health holds the original.

`uuid` is HealthKit's identity, used to pick the newest workout deterministically
and to avoid counting a re-sent one twice. It is not displayed.

**Daily aggregates** are three ordinary cumulative metrics, attributed to the
local calendar day containing each workout's **start**, so a session never splits
across two days. There is deliberately no daily distance: summing kilometres
across swimming, cycling and running is arithmetic without meaning, and distance
stays on the individual workout.

A day whose workouts recorded no energy sends **no** `workout_energy` bucket, so
"trained but energy unknown" stays distinguishable from "burned nothing" — the
count and duration rows still exist. A genuine zero is still sent as zero.

## 7. The seven-night trend

Sent in the snapshot as `sleep_7d`. It is **derived state, not durable truth**:
recomputed and replaced on every sync, exactly like a current value.

The nightly rows above remain authoritative. There is deliberately no second
durable seven-day history, because two stored representations of the same nights
can drift apart and there would then be no way to say which was right.

`nights_by_field` records how many nights actually contributed to each average,
so a three-night week is never presented as a seven-night one. A stage no night
measured yields `null`, never zero.

## 8. Compatibility

| client → receiver | result |
|---|---|
| v1, v2, v3 → v4 receiver | accepted, unchanged semantics |
| v4 → v4 receiver | accepted |
| v4 → v3 receiver | **rejected**, `unsupported_version` |
| v3 envelope carrying v4 bucket keys | rejected, `unknown_bucket_kind` |
| v4 envelope carrying v3 bucket keys | rejected, `unknown_bucket_kind` |

Heart rate and steps keep their v2/v3 semantics exactly. Internally a v3 payload
is converted into the same metric-keyed buckets a v4 payload produces, so both
versions share one storage path and the older format cannot drift from the newer
one. This is asserted directly: the parsed result of a v3 and a v4 payload
carrying the same data must be equal.

Deployment order still matters for a *new wire version*: **receiver first**,
then Test Connection, then the client.

### 8.1 Forward compatibility — the receiver says what it understands

Ordering the deployment is advice Pascal can follow on his own instance. It is
not something a shipped app can rely on: an App Store update reaches a phone long
before a HACS update reaches the Home Assistant behind it, so **a newer client
talking to an older receiver is the ordinary state during a rollout.**

Every response — both `ping` and `sync` — therefore carries what this receiver
can take:

```json
"supported_metrics": ["active_energy", "blood_pressure_diastolic", "…"],
"supported_features": ["buckets.nightly", "snapshot.blood_pressure",
                       "snapshot.last_workout", "snapshot.sleep_trend"]
```

`supported_metrics` lists every metric id the registry accepts.
`supported_features` names additive wire features that are **not** metrics — a
structured snapshot object is the case that forced it to exist.

**Both lists are needed, because the two surfaces fail in opposite ways.** An
unknown bucket metric is reported; an unknown snapshot key is *silently ignored*
and returns HTTP 200. A client that only knew the metric list could therefore
send a new snapshot object, be told everything was fine, and have it discarded.

**A receiver that sends neither field is a legitimate older receiver, not an
error.** The client must then fall back to the frozen v4 baseline set rather than
assuming support.

Features are appended, never renamed or removed. A feature appears in the list
only when the receiver genuinely implements it: a list that over-promises would
be worse than no list, because the client would send and lose.

## 9. Validation and failure model

Unchanged from v3, and now covering the new families.

Aggregate history is **all-or-nothing for anything malformed**. One malformed
bucket fails the whole request; nothing is applied.

**One deliberate exception, added with §8.1.** A v4 bucket that is perfectly well
formed but names a metric this receiver does not know is recorded as a per-item
`rejected` entry instead of failing the delivery. That is version skew, not a
protocol violation, and losing every other metric to one unrecognised name was
the wrong trade.

This is *not* the silent drop the strictness was protecting against. The bucket
is reported back in `rejected`, the client surfaces the count, and nothing is
stored under a guessed meaning. Everything else is unchanged and still fatal:

| case | outcome |
|---|---|
| unknown metric id, valid bucket | `rejected` entry, rest of the delivery stored |
| malformed bucket / missing field | **fatal**, `bad_bucket_missing_field` |
| unknown bucket family | **fatal**, `unknown_bucket_kind` |
| unexpected field in a known bucket | **fatal**, `bad_bucket_unknown_field` |
| unexpected field in a nightly bucket | **fatal**, `bad_nightly_unknown_field` |
| unsupported envelope version | **fatal**, `unsupported_version` |

Reason codes added in earlier cycles: `bad_bucket_unknown_field`,
`bad_nightly_unknown_field` (§4).

Ordering is load-bearing: the durable window is imported **before** anything
touches the current values. If the import is refused, the request fails, no
snapshot is applied, `last_sync` does not advance and no entity update is
dispatched — so a failed sleep import can never be reported to the person as a
completed sync.

## 9a. Activity Summary

Reserved in Phase 4A.1, implemented on this side in Phase 4A.2, released in
1.4.0. The eight metrics are in the registry, the parser accepts
`snapshot.activity`, the sensors exist, and both `supported_metrics` and
`supported_features` publish it.

Every installation still running 1.3.x or earlier knows none of that, which is
the case the forward-compatibility layer in §8.1 exists for: it publishes no
feature list at all, the client falls back to the frozen v4 baseline and
withholds the whole source. Measured against the code at the `v1.3.1` tag, not
asserted: unfiltered, a payload carrying Activity makes that version raise
`unknown_metric` and reject the entire delivery — so withholding is not a
nicety, it is what keeps the other fourteen sources working until the receiver
is upgraded.

Measured before the unit was chosen: Home Assistant 2026.9.1 has a statistics
converter for `h` but **none for `hours`**. That is why stand hours use the
latter — see the table note below.

### Eight daily metrics

| metric | family | unit | unit_class | mean | sum |
|---|---|---|---|---|---|
| `activity_move_energy` | daily cumulative | kcal | energy | none | yes |
| `activity_move_energy_goal` | daily discrete, mean only | kcal | energy | arithmetic | no |
| `activity_move_time` | daily cumulative | min | duration | none | yes |
| `activity_move_time_goal` | daily discrete, mean only | min | duration | arithmetic | no |
| `activity_exercise_time` | daily cumulative | min | duration | none | yes |
| `activity_exercise_goal` | daily discrete, mean only | min | duration | arithmetic | no |
| `activity_stand_hours` | daily cumulative | hours | — | none | yes |
| `activity_stand_goal` | daily discrete, mean only | hours | — | arithmetic | no |

`activity_stand_hours` has **no unit class**, and that is measured rather than
argued. Home Assistant 2026.9.1 maps `h` to its `DurationConverter` and has no
converter at all for `hours`, so `hours` stays opaque exactly like `steps`,
`naps` and `workouts`. That is what this metric needs: it counts hours that
*qualified*, not time elapsed, and `h` would have let Home Assistant render nine
stand hours as 540 minutes.

Goals are mean-only daily discrete: a day's goal is one value, so a min/max
spread would be invented, and it accumulates nothing.

All eight carry `snapshot_key: ""`. The current-value view is the composite
object below, exactly as blood pressure and `last_workout` already do it.

### `snapshot.activity`

```json
"activity": {
  "date": "2026-09-06", "time_zone": "Europe/Berlin",
  "move_mode": "active_energy",
  "move_energy": 388.8, "move_energy_goal": 600,
  "move_time": 42, "move_time_goal": 30,
  "exercise_time": 42, "exercise_goal": 30,
  "stand_hours": 9, "stand_goal": 12
}
```

`date`, `time_zone` and `move_mode` are required when the object is present.
Every value is optional.

### Move mode decides which series is the ring

`HKActivitySummary.activityMoveMode` is `active_energy` or `move_time`, sent as a
stable string and never as Apple's numeric enum. It names which series **is** the
Move ring. Both series travel whenever the summary carries them — withholding one
would make the payload depend on a setting that can change between days — but
only the named one is the ring.

**`activity_move_energy` is not `active_energy` and the two must never merge.**
`active_energy` is the sum of Active Energy samples over the local day from a
statistics query, and works without an Apple Watch. `activity_move_energy` is
Apple's own Move-ring figure, with Apple's day boundary and pause handling. They
usually agree, they answer different questions, and in `move_time` mode
`active_energy` is not the ring at all.

### Absence, zero and missing goals

Measured on a real device: across a seven-day window one day had **no summary
object at all** while another had **all three rings at zero**. Those are different
facts and the difference is load-bearing.

- no summary for a day → no bucket for that day, and no snapshot
- a summary carrying zero → a bucket carrying `0`
- a nullable goal absent (`exerciseTimeGoal`, `standHoursGoal`, iOS 16+) → no goal
  bucket and no snapshot field. Never `0`, never a default, never interpolated
  from another day. The deprecated `appleExerciseTimeGoal` / `appleStandHoursGoal`
  are not used.

### Day boundary

Apple's own assignment via `dateComponentsForCalendar:`, never reconstructed from
UTC timestamps. Carried as the ordinary v4 daily bucket — local date plus
`time_zone` — so 23- and 25-hour days remain HealthKit's arithmetic, and Phase 5
backfill needs no new shape.

### No percentages

No `move_percent`, `exercise_percent` or `stand_percent`. Home Assistant derives
value over goal from two statistics, and a stored percentage becomes historically
wrong the moment a goal changes.

### Errors

| reason | when |
|---|---|
| `bad_activity_missing_field` | `date`, `time_zone` or `move_mode` absent |
| `bad_activity_move_mode` | a mode outside the closed set — never defaulted |
| `bad_activity_unknown_field` | an unexpected key inside `activity` |
| `bad_activity_bad_value` | a negative ring or goal |
| `bad_activity_*` | a non-finite number, or an unusable date or zone |

### Integration note

Apple's guidance applies to anyone building on this: the data may be used, but a
dashboard must not imitate the Activity ring graphic.

## 9b. Phase 4B: the last four catalogue metrics

Four ordinary metrics in three shapes that already existed. No new bucket
family, no new snapshot composite, no new read path — deliberately, because
Activity was the hard one and these should not have been.

| metric | family | unit | unit_class | mean | sum | snapshot key |
|---|---|---|---|---|---|---|
| `flights_climbed` | daily cumulative | flights | — | none | yes | `flights_climbed_today` |
| `walking_heart_rate_average` | daily discrete, mean only | bpm | — | arithmetic | no | `walking_heart_rate_average` |
| `bmi` | hourly discrete | kg/m² | — | arithmetic | no | `bmi` |
| `blood_glucose` | hourly discrete | mg/dL | `blood_glucose_concentration` | arithmetic | no | `blood_glucose` |

Every shape is Apple's, read from `HKTypeIdentifiers.h` in the 26.5 SDK rather
than chosen:

```
FlightsClimbed            // count, Cumulative
WalkingHeartRateAverage   // count/min, Discrete (Temporally Weighted)
BodyMassIndex             // count, Discrete (Arithmetic)
BloodGlucose              // mg/dL, Discrete (Arithmetic)
```

`flights_climbed` is steps' twin and gets steps' treatment. The walking average
is one value per day, exactly as resting heart rate is, so it is mean-only and a
min/max spread is rejected rather than invented.

BMI and blood glucose are **hourly**, which looks odd for a value read once a
week until you notice it is the same argument the body metrics already won: a
daily mean of a morning and an evening reading is a number nobody measured. For
glucose that argument is stronger, not weaker — a fasting reading and a
post-meal one describe different things.

### Two things read rather than computed

`walking_heart_rate_average` is Apple's own quantity type, not a mean this
bridge takes over walking heart-rate samples. `bmi` is Apple's stored value, not
weight over height squared — height is not carried at all. Either
reconstruction would produce a number that disagrees with the Health app, which
is worse than not carrying the metric.

### `kg/m²`, and why the unit is not empty

HealthKit models body mass index as `count` because `HKUnit` has no compound
unit for it, not because the quantity is dimensionless: it is mass over height
squared. Home Assistant knows no converter for `kg/m²`, so the unit class is
None — the same answer `bpm` and `ml/kg/min` get, and for the same reason.

### Blood glucose is transported, never interpreted

`mg/dL` is HealthKit's canonical unit for the type. Home Assistant 2026.9.1 has
a real converter for it — measured, not assumed: the `blood_glucose_concentration`
class accepts mg/dL and mmol/L — so a person who reads in mmol/L gets that
conversion from Home Assistant rather than from a second wire format.

That device class is the only thing the receiver adds, and it buys a unit
conversion, not a judgement. There is no reference range, no high/low
classification, no `PERCENT_METRICS`-style plausibility check, and no device or
CGM integration. A value far outside any normal range is still the value Apple
Health holds and is stored exactly as sent.

## 10. Limits

| limit | value |
|---|---|
| body | 1 MiB |
| hourly buckets, per metric | 400 |
| hourly buckets, total | 2000 |
| daily buckets (v4) | 400 |
| daily buckets (v3) | 40 |
| nightly records | 40 |

The v4 daily ceiling is higher because v4 carries a bucket per metric per day:
seven daily metrics across the widest 14-day recovery window is 98 buckets, which
the v3 ceiling of 40 would have refused.

As of Phase 4B there are **22 daily metrics**, so a full 14-day window is 308 of
the 400 available. The figure that runs out first is not this one but the
client's per-metric allowance, which divides the ceiling by the daily metric
count: at 28 daily metrics a 14-day window no longer fits, and the window
silently shortens instead. Any design that *multiplies* metrics — a
per-category workout aggregate, say — has to be measured against that number
before it is built, not after.

The hourly ceilings are two-tier and both are enforced by **rejecting** an
oversized request, never by trimming. Per metric first, so one series can never
squeeze out another — trimming a combined array would drop whole metrics rather
than old buckets, which is a silent loss of exactly the kind this protocol
exists to prevent. A dense heart-rate series needs 336 buckets for a full 14-day
recovery window, and the sparse body metrics use a fraction of their allowance,
so a realistic combined upload sits far inside the total envelope.

A full v4 window is roughly 38 KB — about 27× inside the body limit, so it still
travels as a single request.

## 11. Durability

Unchanged from v3, and stated plainly: `async_add_external_statistics` validates
synchronously and then queues the write. It cannot report whether the row was
committed. The receiver therefore promises *validated and accepted*, never
*durably persisted*. A recorder-internal failure afterwards is healed by the next
overlapping 7–14 day window — which is also what heals a night the Apple Watch
uploads hours after waking.

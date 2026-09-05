"""Body and collection limits — protocol/payload-v1.md §8.

Regression cover for the Phase 1 "payload was too large" bug: the first sync sent
an entire HealthKit history in one request.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync.payload import (
    MAX_BODY_BYTES,
    MAX_DAILY_TOTALS,
    MAX_DELETIONS,
    MAX_SAMPLES,
    PayloadError,
    parse,
)

NOW = datetime(2026, 9, 2, 21, 0, 0, tzinfo=UTC)


def envelope(**overrides):
    body = {
        "version": 1,
        "type": "sync",
        "sent_at": "2026-09-02T21:00:00Z",
        "device": {"name": "iPhone", "model": "iPhone17,1"},
    }
    body.update(overrides)
    return body


def sample(index: int) -> dict:
    return {
        "metric": "heart_rate",
        # Deterministic, distinct, correctly shaped UUIDs.
        "uuid": f"3F2504E0-4F89-11D3-9A0C-{index:012X}",
        "start": "2026-09-02T20:14:31Z",
        "end": "2026-09-02T20:14:31Z",
        "value": 62.5,
        "unit": "count/min",
        "source": "Pascal's Apple Watch Ultra 2",
    }


# --- The limits must be mutually consistent --------------------------------


def test_max_samples_cannot_overflow_the_body_limit():
    """A batch at MAX_SAMPLES must still fit in MAX_BODY_BYTES with real margin.

    The original limits were 5000 samples against a 1 MiB body — about 1% of
    headroom, which a longer source name would have erased.
    """
    body = envelope(samples=[sample(i) for i in range(MAX_SAMPLES)])
    encoded = len(json.dumps(body).encode())

    assert encoded < MAX_BODY_BYTES
    # Demand real headroom, not a coincidence.
    assert encoded < MAX_BODY_BYTES * 0.6, (
        f"{MAX_SAMPLES} samples encode to {encoded} bytes, "
        f"{100 * encoded / MAX_BODY_BYTES:.0f}% of the {MAX_BODY_BYTES}-byte limit"
    )


# --- Just below / just above ------------------------------------------------


def test_batch_exactly_at_the_sample_limit_is_accepted():
    result = parse(envelope(samples=[sample(i) for i in range(MAX_SAMPLES)]), now=NOW)
    assert len(result.samples) == MAX_SAMPLES
    assert result.rejected == []


def test_one_sample_over_the_limit_is_refused():
    with pytest.raises(PayloadError) as err:
        parse(envelope(samples=[sample(i) for i in range(MAX_SAMPLES + 1)]), now=NOW)
    assert err.value.reason == "too_many_samples"


@pytest.mark.parametrize(
    ("collection", "limit", "item", "reason"),
    [
        ("daily_totals", MAX_DAILY_TOTALS,
         {"metric": "steps", "date": "2026-09-02", "time_zone": "UTC",
          "value": 1, "unit": "count"}, "too_many_daily_totals"),
        ("deletions", MAX_DELETIONS,
         "3F2504E0-4F89-11D3-9A0C-0305E82C3301", "too_many_deletions"),
    ],
)
def test_each_collection_has_its_own_ceiling(collection, limit, item, reason):
    ok = parse(envelope(**{collection: [item] * limit}), now=NOW)
    assert ok.rejected == [] or all(r.collection == collection for r in ok.rejected)

    with pytest.raises(PayloadError) as err:
        parse(envelope(**{collection: [item] * (limit + 1)}), now=NOW)
    assert err.value.reason == reason


# --- A refused oversize batch must not be partially applied -----------------


def test_oversize_batch_is_refused_whole_not_truncated():
    """An over-limit request fails outright; it must never silently drop the tail."""
    with pytest.raises(PayloadError):
        parse(envelope(samples=[sample(i) for i in range(MAX_SAMPLES + 50)]), now=NOW)


# --- Batching preserves per-item validation ---------------------------------


def test_per_item_validation_still_applies_inside_a_full_batch():
    items = [sample(i) for i in range(MAX_SAMPLES)]
    items[7] = {**items[7], "value": "not-a-number"}
    items[9] = {**items[9], "metric": "vo2max"}

    result = parse(envelope(samples=items), now=NOW)

    assert len(result.samples) == MAX_SAMPLES - 2
    assert {r.index for r in result.rejected} == {7, 9}
    assert {r.reason for r in result.rejected} == {"bad_value", "unknown_metric"}

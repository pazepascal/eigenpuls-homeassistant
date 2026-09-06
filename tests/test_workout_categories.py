"""Phase 4C: training broken down by category.

The shape here was chosen by arithmetic, and the arithmetic belongs in the test
file so nobody re-litigates it from taste.

Twelve categories times count, duration and energy is 36 daily metrics. The
client divides its 400-bucket ceiling by the daily series count to get a
per-metric allowance, so 22 + 36 = 58 series gives an allowance of 6: the 14-day
recovery window silently becomes **6 days**. Dropping energy does not save it
either - 46 series allow 8 days.

The ceiling itself is not what breaks; 348 buckets still fit. What breaks is the
reason the 14-day window exists at all, which is a phone that was offline for a
fortnight catching up. Halving it turns an outage into permanent data loss, and
it would do so silently.

So there is no per-category long-term history. The existing ``workout_count``,
``workout_duration`` and ``workout_energy`` series keep the durable totals and
are untouched; this composite answers the different question of *what* the
training was, for the whole window at once, at zero bucket cost.

The other decision worth pinning: a category with no workouts in the window is
**absent**, never a zero. The same rule the daily energy series already follows.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.apple_health_sync import registry
from custom_components.apple_health_sync.payload import (
    PayloadError,
    WorkoutCategories,
    WorkoutCategoryTotals,
    parse,
)
from custom_components.apple_health_sync.state import HealthState

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def envelope(snapshot):
    return {
        "version": 4, "type": "sync", "sent_at": iso(NOW),
        "device": {"name": "iPhone"},
        "sync": {"id": "sync-1", "final": True}, "snapshot": snapshot,
    }


def breakdown(**categories):
    return {"window_days": 90, "categories": categories}


# --- the arithmetic that chose this shape ------------------------------------


def test_a_statistic_per_category_would_have_halved_the_recovery_window():
    """The measurement, kept executable so it cannot rot into folklore.

    The registry's daily metrics are the same series the client counts, naps
    included, so this is the client's own divisor computed from the receiver's
    own registry - not a transcription of it.
    """
    ceiling, window, per_metric_cap = 400, 14, 40
    series_now = sum(
        1 for spec in registry.METRICS.values()
        if spec.kind is not registry.BucketKind.HOURLY_DISCRETE
    )

    def sent(series):
        allowance = min(per_metric_cap, max(ceiling // max(series, 1), 1))
        days = min(allowance, window)
        return days, series * days

    days, buckets = sent(series_now)
    assert days == window and buckets <= ceiling, "today sends a full window"

    for extra, expected_days in ((12 * 3, 6), (12 * 2, 8)):
        days, buckets = sent(series_now + extra)
        assert days == expected_days, f"{extra} extra series would send {days} days"
        # Worth being precise about: the ceiling is not what fails here.
        assert buckets <= ceiling, "the ceiling holds; the window is what collapses"


def test_no_category_became_a_statistic():
    """Twelve categories, zero new registry entries and zero new statistic ids."""
    for activity in registry.WORKOUT_ACTIVITIES:
        assert activity not in registry.METRICS
        assert f"workout_{activity}" not in registry.METRICS
    # The durable training series are the three that already existed.
    training = {m for m in registry.METRICS if m.startswith("workout_")}
    assert training == {"workout_count", "workout_duration", "workout_energy"}


# --- parsing -----------------------------------------------------------------


def test_every_category_in_the_vocabulary_is_accepted():
    payload = parse(
        envelope({"workout_categories": breakdown(**{
            activity: {"count": 1, "duration_min": 30.0}
            for activity in registry.WORKOUT_ACTIVITIES
        })}),
        now=NOW,
    )
    assert set(payload.snapshot.workout_categories.categories) == set(
        registry.WORKOUT_ACTIVITIES
    )


def test_an_activity_outside_the_vocabulary_is_refused_rather_than_folded_into_other():
    """The client already maps unknown HealthKit types to `other`.

    So a name arriving here that this receiver does not know does not mean a new
    sport - it means the two halves disagree about the taxonomy. Folding it into
    `other` would hide that disagreement behind plausible-looking data.
    """
    with pytest.raises(PayloadError) as err:
        parse(
            envelope({"workout_categories": breakdown(
                padel={"count": 2, "duration_min": 90.0}
            )}),
            now=NOW,
        )
    assert err.value.reason == "bad_workout_categories_unknown_activity"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"window_days": 0, "categories": {}}, "bad_workout_categories_window"),
        ({"window_days": 90}, "bad_workout_categories"),
        ({"window_days": 90, "categories": {}, "extra": 1},
         "bad_workout_categories_unknown_field"),
        ({"window_days": 90, "categories": {"walking": {"count": 0, "duration_min": 1.0}}},
         "bad_workout_categories_count"),
        ({"window_days": 90, "categories": {"walking": {"count": 1, "duration_min": -1.0}}},
         "bad_workout_categories_negative"),
        ({"window_days": 90,
          "categories": {"walking": {"count": 1, "duration_min": 1.0, "pace": 2}}},
         "bad_workout_categories_unknown_field"),
    ],
)
def test_amalformed_breakdown_is_rejected_with_its_own_reason(raw, reason):
    with pytest.raises(PayloadError) as err:
        parse(envelope({"workout_categories": raw}), now=NOW)
    assert err.value.reason == reason


def test_a_category_that_recorded_no_energy_carries_none_rather_than_zero():
    payload = parse(
        envelope({"workout_categories": breakdown(
            walking={"count": 4, "duration_min": 120.0, "energy_kcal": 300.0},
            yoga={"count": 3, "duration_min": 90.0},
        )}),
        now=NOW,
    )
    categories = payload.snapshot.workout_categories.categories
    assert categories["walking"].energy_kcal == 300.0
    assert categories["yoga"].energy_kcal is None


def test_an_untrained_category_is_absent_and_never_a_zero():
    payload = parse(
        envelope({"workout_categories": breakdown(
            running={"count": 2, "duration_min": 60.0}
        )}),
        now=NOW,
    )
    assert set(payload.snapshot.workout_categories.categories) == {"running"}


# --- the headline value ------------------------------------------------------


def test_the_most_trained_category_is_decided_by_minutes_not_by_count():
    """Four short walks are not more training than one long ride."""
    categories = WorkoutCategories(window_days=90, categories={
        "walking": WorkoutCategoryTotals(count=9, duration_min=60.0),
        "cycling": WorkoutCategoryTotals(count=1, duration_min=180.0),
    })
    assert categories.most_trained == "cycling"


def test_atie_breaks_on_the_name_so_the_answer_does_not_flicker():
    categories = WorkoutCategories(window_days=90, categories={
        "yoga": WorkoutCategoryTotals(count=1, duration_min=60.0),
        "rowing": WorkoutCategoryTotals(count=1, duration_min=60.0),
    })
    assert categories.most_trained == "rowing"
    assert categories.most_trained == "rowing", "and it is the same on the next read"


def test_an_empty_window_has_no_most_trained_category():
    assert WorkoutCategories(window_days=90, categories={}).most_trained is None


# --- state -------------------------------------------------------------------


def test_the_breakdown_is_replaced_rather_than_merged():
    """A category that leaves the window has to disappear.

    This is the one place where the merge rule the activity rings use would be
    wrong: those are per-field values that can be individually absent from a
    snapshot, while this is a complete recomputation of a fixed window. Merging
    would leave a sport nobody has done since spring on the dashboard for ever.
    """
    state = HealthState()
    first = parse(
        envelope({"workout_categories": breakdown(
            swimming={"count": 5, "duration_min": 200.0},
            running={"count": 2, "duration_min": 60.0},
        )}),
        now=NOW,
    )
    state.apply_snapshot(first.snapshot, received_at=NOW)
    assert set(state.workout_categories.categories) == {"swimming", "running"}

    later = parse(
        envelope({"workout_categories": breakdown(
            running={"count": 3, "duration_min": 95.0}
        )}),
        now=NOW,
    )
    state.apply_snapshot(later.snapshot, received_at=NOW)
    assert set(state.workout_categories.categories) == {"running"}


def test_an_absent_breakdown_leaves_the_previous_one_alone():
    """An older client, or the workouts source switched off, is not a measurement."""
    state = HealthState()
    state.apply_snapshot(
        parse(envelope({"workout_categories": breakdown(
            hiking={"count": 1, "duration_min": 240.0}
        )}), now=NOW).snapshot,
        received_at=NOW,
    )
    state.apply_snapshot(parse(envelope({}), now=NOW).snapshot, received_at=NOW)
    assert set(state.workout_categories.categories) == {"hiking"}


def test_an_older_receiver_would_have_ignored_it_rather_than_failing():
    """Why this needs no capability gate, asserted rather than assumed.

    An unknown *bucket metric* is fatal to a delivery; an unknown *snapshot key*
    is not. That asymmetry is what lets this ride along to a receiver that has
    never heard of it, and it is why gating the whole workouts source behind a
    `snapshot.workout_categories` feature would have been a regression rather
    than a safeguard - a v1.4.0 receiver would have lost its workouts entirely.
    """
    payload = parse(
        envelope({
            "heart_rate": {"value": 60, "unit": "bpm", "measured_at": iso(NOW)},
            "a_key_from_a_later_phase": {"anything": 1},
        }),
        now=NOW,
    )
    assert payload.snapshot.heart_rate is not None
    assert not payload.rejected

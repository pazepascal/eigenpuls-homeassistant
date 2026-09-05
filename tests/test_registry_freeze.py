"""The registry is a compatibility contract, not just a table.

Every entry here already exists as a Home Assistant long-term statistic on some
installation, and Home Assistant statistics have no rollback: renaming a series
orphans its history, and changing a unit or mean type silently reinterprets years
of stored rows.

So this test is deliberately one-directional. Adding a metric passes. Removing one,
or altering the meaning of one that already shipped, fails and has to be a conscious
decision — which, for a metric already in the field, means a new series rather than
an edited one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.apple_health_sync import registry

SNAPSHOT = json.loads((Path(__file__).parent / "registry_snapshot.json").read_text())

FIELDS = ("statistic_id", "kind", "unit", "unit_class", "mean_type", "has_sum")


def _actual(spec) -> dict:
    return {
        "statistic_id": spec.statistic_id,
        "kind": str(spec.kind),
        "unit": spec.unit,
        "unit_class": spec.unit_class,
        "mean_type": str(spec.mean_type),
        "has_sum": bool(spec.has_sum),
    }


def _cases(section: str, live: dict):
    return [(name, frozen, live.get(name)) for name, frozen in SNAPSHOT[section].items()]


@pytest.mark.parametrize(
    ("name", "frozen", "spec"),
    _cases("wire_metrics", registry.METRICS)
    + _cases("sleep_series", registry.SLEEP_SERIES)
    + _cases("derived_series", {"blood_pressure_count": registry.BLOOD_PRESSURE_COUNT}),
)
def test_frozen_series_is_unchanged(name, frozen, spec):
    assert spec is not None, (
        f"{name} was removed from the registry. Installations already hold "
        f"{frozen['statistic_id']}; removing it orphans that history."
    )
    actual = _actual(spec)
    for field in FIELDS:
        assert actual[field] == frozen[field], (
            f"{name}.{field} changed from {frozen[field]!r} to {actual[field]!r}. "
            "That reinterprets statistics already stored on real installations. "
            "Add a new series instead of editing this one."
        )


def test_workout_vocabulary_is_append_only():
    frozen = SNAPSHOT["workout_activities"]
    live = list(registry.WORKOUT_ACTIVITIES)
    missing = [a for a in frozen if a not in live]
    assert not missing, (
        f"Workout activities removed: {missing}. Stored workout states would stop "
        "resolving to a translation."
    )


def test_additions_are_allowed_and_visible():
    """Not a constraint — a reminder that the snapshot needs extending when a
    metric is added deliberately, so an addition never passes unnoticed."""
    added = sorted(set(registry.METRICS) - set(SNAPSHOT["wire_metrics"]))
    assert not added, (
        f"New metrics not in the snapshot: {added}. Adding a metric is fine; "
        "regenerate tests/registry_snapshot.json in the same commit so the new "
        "series is frozen from the day it ships."
    )

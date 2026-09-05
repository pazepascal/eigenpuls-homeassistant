"""Diagnostics: never expose the token, the webhook id, or health values."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AppleHealthSyncConfigEntry
from .const import CONF_TOKEN, CONF_WEBHOOK_ID

TO_REDACT = {CONF_TOKEN, CONF_WEBHOOK_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AppleHealthSyncConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    state = entry.runtime_data.state
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        # Presence and freshness only - no measured values.
        "state": {
            "has_heart_rate": state.heart_rate is not None,
            "heart_rate_at": state.heart_rate_at.isoformat() if state.heart_rate_at else None,
            "has_steps": state.steps is not None,
            "steps_day": state.steps_day.isoformat() if state.steps_day else None,
            "steps_time_zone": state.steps_time_zone,
            # Which metrics have arrived, never what they measured. Sleep stage
            # durations are health values like any other and stay out; only the
            # night's date, its zone and which stages are present are reported,
            # which is what makes a "why is REM missing" question answerable
            # without putting the person's sleep in a diagnostics download.
            "metrics_present": sorted(state.measurements),
            "daily_totals_present": sorted(state.daily_totals),
            "sleep": None if state.sleep is None else {
                "date": state.sleep.day.isoformat(),
                "time_zone": state.sleep.time_zone,
                "stages_present": sorted(
                    name
                    for name in ("rem_min", "core_min", "deep_min", "awake_min")
                    if getattr(state.sleep, name) is not None
                ),
            },
            # Naps are a daily metric now, so their presence is reported with
            # the other metrics above rather than as part of the night.
            # Presence and freshness only: no systolic, no diastolic, no
            # weight, no body-fat figure.
            "blood_pressure": None if state.blood_pressure is None else {
                "measured_at": state.blood_pressure.measured_at.isoformat(),
                "has_pair": True,
                "source": state.blood_pressure.source,
            },
            # Structure only: which fields the last workout carried and when it
            # was, never how long it lasted, how far it went or what the heart
            # did. The activity is deliberately absent too - what sport someone
            # does is a health value like any other.
            "last_workout": None if state.last_workout is None else {
                "ended_at": state.last_workout.end.isoformat(),
                "fields_present": sorted(
                    name
                    for name in (
                        "active_energy_kcal", "distance_km",
                        "avg_heart_rate_bpm", "max_heart_rate_bpm",
                    )
                    if getattr(state.last_workout, name) is not None
                ),
                "source": state.last_workout.source,
            },
            "sleep_trend_nights": (
                state.sleep_trend.nights if state.sleep_trend else None
            ),
            "last_sync": state.last_sync.isoformat() if state.last_sync else None,
        },
    }

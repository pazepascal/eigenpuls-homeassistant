"""The Apple Health Sync integration.

Push-only: the iOS app delivers to a webhook. There is no backend to poll, so
there is deliberately no DataUpdateCoordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import webhook as webhook_receiver
from .const import CONF_TOKEN, CONF_WEBHOOK_ID
from .state import HealthState

# Defined here rather than in const.py, which must stay free of Home
# Assistant imports so the parsing layer can import it.
PLATFORMS: Final[list[Platform]] = [Platform.SENSOR]


@dataclass(slots=True)
class AppleHealthSyncRuntimeData:
    """Typed setup artefacts, held on the config entry - never hass.data."""

    token: str
    webhook_id: str
    state: HealthState = field(default_factory=HealthState)


type AppleHealthSyncConfigEntry = ConfigEntry[AppleHealthSyncRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: AppleHealthSyncConfigEntry
) -> bool:
    """Set up Apple Health Sync from a config entry."""
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    entry.runtime_data = AppleHealthSyncRuntimeData(
        token=entry.data[CONF_TOKEN],
        webhook_id=webhook_id,
    )

    await webhook_receiver.async_register(hass, entry, webhook_id)
    entry.async_on_unload(
        lambda: webhook_receiver.async_unregister(hass, webhook_id)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AppleHealthSyncConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

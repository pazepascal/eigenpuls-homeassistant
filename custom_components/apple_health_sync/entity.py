"""Shared entity base for Apple Health Sync.

Deviation from ha/entity-architecture, recorded deliberately: that spec requires
the base class to subclass CoordinatorEntity. This integration is push-only and
has no coordinator, so the base subclasses Entity and
receives updates over the dispatcher instead.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DEVICE_NAME, DOMAIN, MANUFACTURER, SIGNAL_UPDATE


def build_device_info(entry_id: str) -> DeviceInfo:
    """One device per config entry.

    The device name decides the generated entity_id: HA slugifies
    "<device name> <entity name>", so "Apple Health" + "Heart Rate" yields
    sensor.apple_health_heart_rate.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=DEVICE_NAME,
        manufacturer=MANUFACTURER,
        model="Apple Health Sync",
    )


class AppleHealthSyncEntity(Entity):
    """Base entity: device identity plus dispatcher-driven updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._attr_device_info = build_device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self._entry_id),
                self._handle_update,
            )
        )

    def _handle_update(self) -> None:
        self.async_write_ha_state()

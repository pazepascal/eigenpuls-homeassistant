"""Constants for the Apple Health Sync integration.

Deliberately free of Home Assistant imports: ``registry.py`` reads ``DOMAIN``
from here to build the statistic ids, and ``payload.py`` imports the registry,
so an import here would pull Home Assistant into the pure parsing layer.
``PLATFORMS`` therefore lives in ``__init__.py``, its only consumer."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "apple_health_sync"

# Config entry keys
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_TOKEN: Final = "token"
# Present only on entries paired through Home Assistant Cloud. Absent on every
# entry created before pairing existed, and on any local-only setup - so every
# read of it must tolerate its absence.
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"

# Device identity. The device name determines the generated entity_id
# (HA composes it as slugify("<device name> <entity name>")), so "Apple Health"
# is what produces sensor.apple_health_heart_rate. See docs/DECISIONS note in
# See the note on the device name above.
DEVICE_NAME: Final = "Apple Health"
MANUFACTURER: Final = "Apple"

# Long-term statistics. The id's domain must equal the metadata `source`.
STAT_ID_HEART_RATE: Final = f"{DOMAIN}:heart_rate"
STAT_ID_STEPS: Final = f"{DOMAIN}:steps_daily"
UNIT_HEART_RATE: Final = "bpm"
UNIT_STEPS: Final = "steps"

# Dispatcher signal, formatted with the config entry id.
SIGNAL_UPDATE: Final = DOMAIN + "_update_{}"

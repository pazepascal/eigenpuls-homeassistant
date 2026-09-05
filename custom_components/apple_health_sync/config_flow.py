"""Config flow: generates the webhook id and the bearer token."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import CONF_TOKEN, CONF_WEBHOOK_ID, DOMAIN

TOKEN_BYTES = 32  # 256-bit.


class AppleHealthSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-initiated setup."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the credentials, then show them once for the iOS app."""
        # A single instance is enough: one iPhone, one feed.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=vol.Schema({})
            )

        webhook_id = webhook.async_generate_id()
        token = secrets.token_urlsafe(TOKEN_BYTES)

        try:
            base_url = get_url(self.hass, prefer_external=True)
        except NoURLAvailableError:
            base_url = ""

        return self.async_create_entry(
            title="Apple Health",
            data={CONF_WEBHOOK_ID: webhook_id, CONF_TOKEN: token},
            description_placeholders={
                "url": f"{base_url}/api/webhook/{webhook_id}" if base_url else "",
                "token": token,
            },
        )

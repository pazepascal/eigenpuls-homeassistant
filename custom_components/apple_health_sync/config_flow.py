"""Config flow: create the credentials, then show them once as a pairing code.

The v1 flow put the URL and the token into `description_placeholders` on
`async_create_entry`, which works but is the wrong shape for two reasons. The
person had to retype a 43-character token into a phone, and Home Assistant
already knows things about reachability - whether there is a cloud subscription,
whether an external URL exists - that v1 made the person work out themselves.

So the credentials are still created once and shown once, but as a scannable
code, on a form that appears **before** the entry is created. That ordering is
forced: after `async_create_entry` a flow cannot show another form, so the
pairing screen has to come first and the entry is written only once the person
confirms they have scanned it.
"""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import CONF_CLOUDHOOK_URL, CONF_TOKEN, CONF_WEBHOOK_ID, DOMAIN
from .pairing import PairingError, Reach, encode, is_usable_target

TOKEN_BYTES = 32  # 256-bit.


async def async_resolve_target(
    hass: HomeAssistant, webhook_id: str
) -> tuple[str, Reach, str | None]:
    """Work out where the phone should deliver, best option first.

    Returns the URL, whether it works away from home, and the cloudhook URL if
    one was created - the last so the entry can clean it up on removal.

    Ordered by what the person gets, not by what is easiest to implement:

    1. **A cloudhook**, when Home Assistant Cloud is active. It works from
       anywhere, needs no port forwarding and no certificate, and the person
       never has to hear the word "cloudhook". `webhook.async_generate_url` does
       *not* return one, which is why v1 could not do this automatically.
    2. **An external URL**, when one is configured and usable. Someone with a
       reverse proxy has already solved remote access.
    3. **An internal URL**, accepted only when it satisfies the cleartext policy,
       and honestly labelled as working at home only.

    Raises:
        PairingError: with `no_usable_url`, when none of the three works. That
            is deliberately an error rather than an empty string: v1 stored an
            empty endpoint and the failure only surfaced later, on the phone.
    """
    # Imported here rather than at module scope. `cloud` is optional - the
    # manifest lists it under after_dependencies, not dependencies - and
    # importing it eagerly drags in alexa and camera, which a custom
    # integration's slim test environment has no reason to install.
    from homeassistant.components import cloud

    if cloud.async_active_subscription(hass):
        try:
            cloudhook_url = await cloud.async_get_or_create_cloudhook(hass, webhook_id)
        except cloud.CloudNotAvailable:
            # Logged in but the cloud connection is not up right now. Not fatal:
            # the local paths below may still produce something usable, and a
            # reconfigure once the cloud is back will pick this branch up.
            pass
        else:
            return cloudhook_url, Reach.REMOTE, cloudhook_url

    path = f"/api/webhook/{webhook_id}"

    try:
        external = get_url(hass, allow_internal=False, prefer_external=True)
    except NoURLAvailableError:
        external = ""
    if external and is_usable_target(external + path):
        return external + path, Reach.REMOTE, None

    try:
        internal = get_url(hass, allow_external=False)
    except NoURLAvailableError:
        internal = ""
    if internal and is_usable_target(internal + path):
        return internal + path, Reach.LOCAL, None

    raise PairingError("no_usable_url")


def _pair_step_id(base: str, reach: Reach) -> str:
    """Pick the step whose wording matches what this connection can actually do.

    Two step ids rather than one step with a substituted sentence, because
    placeholders are filled *after* translation - a pre-rendered "works away
    from home" would stay English in a German UI. Home Assistant's answer to
    conditional prose is a separate translated step, so that is what this is.
    """
    return base if reach is Reach.REMOTE else f"{base}_local"


def _pair_schema(pairing_uri: str) -> vol.Schema:
    """The pairing form: a QR code, and the same code as selectable text.

    The text field is not a leftover - it is the path most people will actually
    use. A QR is useless to anyone setting this up on the same machine or using
    a screen reader, and until the ordering problem below is fixed it is the
    only path that works at all.

    A multiline `TextSelector` rather than a fenced code block in the
    description: the code block scrolls sideways, so copying it means dragging
    through text that is mostly off-screen, which was slow and error-prone in
    practice. A text field wraps, selects with one tap-and-hold, and offers the
    system's own Select All.

    The field is editable and its value is ignored. That is the cost of there
    being no read-only text selector; an editable field someone can copy from
    beats a read-only block they cannot.
    """
    return vol.Schema(
        {
            vol.Optional("qr"): QrCodeSelector(
                QrCodeSelectorConfig(data=pairing_uri, scale=6)
            ),
            vol.Optional("code", default=pairing_uri): TextSelector(
                TextSelectorConfig(multiline=True)
            ),
        }
    )


class AppleHealthSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-initiated setup and later re-pairing."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Hold the pending credentials between the two steps."""
        self._webhook_id: str | None = None
        self._token: str | None = None
        self._pairing_uri: str | None = None
        self._reach: Reach | None = None
        self._cloudhook_url: str | None = None

    async def _async_prepare_pairing(self, webhook_id: str) -> str | None:
        """Mint a token and build the pairing code. Returns an error reason."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            url, reach, cloudhook_url = await async_resolve_target(
                self.hass, webhook_id
            )
            pairing_uri = encode(url, token, reach)
        except PairingError as err:
            return err.reason

        self._webhook_id = webhook_id
        self._token = token
        self._pairing_uri = pairing_uri
        self._reach = reach
        self._cloudhook_url = cloudhook_url
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then mint credentials and move to the pairing screen."""
        # A single instance is enough: one iPhone, one feed.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            reason = await self._async_prepare_pairing(webhook.async_generate_id())
            if reason is None:
                return await self.async_step_pair()
            # Shown on the same form rather than aborted: "no usable URL" is
            # something the person can fix in Home Assistant's network settings
            # and then retry, without starting the flow again.
            errors["base"] = reason

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the pairing code, and create the entry once it is confirmed."""
        assert self._pairing_uri is not None
        assert self._reach is not None

        if user_input is None:
            return self.async_show_form(
                step_id=_pair_step_id("pair", self._reach),
                data_schema=_pair_schema(self._pairing_uri),
                description_placeholders={"code": self._pairing_uri},
            )

        data = {
            CONF_WEBHOOK_ID: self._webhook_id,
            CONF_TOKEN: self._token,
        }
        if self._cloudhook_url:
            data[CONF_CLOUDHOOK_URL] = self._cloudhook_url
        return self.async_create_entry(title="Apple Health", data=data)

    async def async_step_pair_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Same step, different wording. Home Assistant dispatches on step id."""
        return await self.async_step_pair(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-pair: issue a new token for the entry that already exists.

        The webhook id deliberately stays: it is the address, not the secret,
        and the receiver refuses anything without a valid bearer token, so
        rotating the token is what actually invalidates an old pairing code.
        Keeping the id also means the cloudhook, the entities and every
        `apple_health_sync:*` statistic survive untouched, which is the whole
        point of offering this instead of "delete and set up again".
        """
        entry = self._get_reconfigure_entry()

        errors: dict[str, str] = {}
        if user_input is not None:
            reason = await self._async_prepare_pairing(entry.data[CONF_WEBHOOK_ID])
            if reason is None:
                return await self.async_step_reconfigure_pair()
            errors["base"] = reason

        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_reconfigure_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the new pairing code, then swap the token in place."""
        assert self._pairing_uri is not None
        assert self._reach is not None

        if user_input is None:
            return self.async_show_form(
                step_id=_pair_step_id("reconfigure_pair", self._reach),
                data_schema=_pair_schema(self._pairing_uri),
                description_placeholders={"code": self._pairing_uri},
            )

        updates: dict[str, Any] = {CONF_TOKEN: self._token}
        if self._cloudhook_url:
            updates[CONF_CLOUDHOOK_URL] = self._cloudhook_url
        return self.async_update_reload_and_abort(
            self._get_reconfigure_entry(), data_updates=updates
        )

    async def async_step_reconfigure_pair_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Same step, different wording. Home Assistant dispatches on step id."""
        return await self.async_step_reconfigure_pair(user_input)

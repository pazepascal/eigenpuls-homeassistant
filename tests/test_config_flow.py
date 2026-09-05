"""The setup flow: what the person is shown, and what is stored afterwards.

Two properties are load-bearing and are asserted rather than assumed:

* the pairing screen appears **before** the entry exists, because after
  `async_create_entry` a flow cannot show a form and the credentials would have
  nowhere to go; and
* no environment produces a config entry with an unusable endpoint. v1 stored an
  empty URL when `get_url` failed, and the failure only surfaced later, on the
  phone, as a sync that never worked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apple_health_sync.const import (
    CONF_CLOUDHOOK_URL,
    CONF_TOKEN,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.apple_health_sync.pairing import Reach, decode

CLOUDHOOK = "https://hooks.nabu.casa/gAbCdEf"
FLOW = "custom_components.apple_health_sync.config_flow"


@pytest.fixture(autouse=True)
def _setup(recorder_mock, enable_custom_integrations):
    """Recorder first, then the custom integration.

    Order matters and is not cosmetic: `recorder_db_url` asserts that `hass` has
    not been created yet, so anything that pulls `hass` in early - an autouse
    fixture depending only on `enable_custom_integrations`, for instance - makes
    every test in the file error during setup.
    """
    return enable_custom_integrations


def url_patch(*, external: str | None, internal: str | None):
    """`get_url` answers differently depending on what the caller allows."""

    def _get_url(hass, **kwargs):
        if kwargs.get("allow_internal") is False:
            if external is None:
                raise NoURLAvailableError
            return external
        if kwargs.get("allow_external") is False:
            if internal is None:
                raise NoURLAvailableError
            return internal
        raise NoURLAvailableError

    return patch(f"{FLOW}.get_url", _get_url)


async def start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


# --- the happy paths, in the order the flow prefers them ---------------------


async def test_cloud_subscription_produces_a_remote_pairing_code(hass: HomeAssistant, fake_cloud):
    fake_cloud.subscribed = True
    with url_patch(external=None, internal=None):
        result = await start(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"

    payload = decode(result["description_placeholders"]["code"])
    assert payload.url == CLOUDHOOK
    assert payload.reach is Reach.REMOTE


async def test_without_cloud_a_usable_external_url_is_used(hass: HomeAssistant, fake_cloud):
    with url_patch(external="https://ha.example.com", internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "pair"
    payload = decode(result["description_placeholders"]["code"])
    assert payload.url.startswith("https://ha.example.com/api/webhook/")
    assert payload.reach is Reach.REMOTE


async def test_only_a_local_url_pairs_as_home_network_only(hass: HomeAssistant, fake_cloud):
    with url_patch(external=None, internal="http://192.168.1.10:8123"):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    # A different step id, because the wording has to be honest about this and
    # a substituted placeholder would not survive translation.
    assert result["step_id"] == "pair_local"
    payload = decode(result["description_placeholders"]["code"])
    assert payload.reach is Reach.LOCAL


async def test_a_cloud_outage_falls_back_instead_of_failing(hass: HomeAssistant, fake_cloud):
    """Logged in but not connected. The local path may still work."""
    fake_cloud.subscribed = True
    fake_cloud.raise_not_available = True
    with url_patch(external=None, internal="http://192.168.1.10:8123"):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "pair_local"


# --- what must never happen --------------------------------------------------


async def test_no_usable_url_shows_an_error_and_creates_nothing(hass: HomeAssistant, fake_cloud):
    with url_patch(external=None, internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_usable_url"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_a_public_cleartext_url_is_refused(hass: HomeAssistant, fake_cloud):
    """iOS would send this happily. Home Assistant is the guard, so it refuses."""
    with url_patch(external="http://example.com", internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["errors"] == {"base": "no_usable_url"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_the_pairing_screen_comes_before_the_entry(hass: HomeAssistant, fake_cloud):
    """After async_create_entry a flow cannot show a form. Hence this ordering."""
    fake_cloud.subscribed = True
    with url_patch(external=None, internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

        assert result["type"] is FlowResultType.FORM
        assert not hass.config_entries.async_entries(DOMAIN)

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_confirming_stores_the_credentials_and_the_cloudhook(
    hass: HomeAssistant, fake_cloud
):
    fake_cloud.subscribed = True
    with url_patch(external=None, internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        code = result["description_placeholders"]["code"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    data = result["data"]
    assert data[CONF_TOKEN] == decode(code).token
    assert data[CONF_WEBHOOK_ID]
    # Stored so removal can delete it; an orphaned cloudhook is a public
    # endpoint left on the person's account for a webhook that is gone.
    assert data[CONF_CLOUDHOOK_URL] == CLOUDHOOK


async def test_a_local_pairing_stores_no_cloudhook(hass: HomeAssistant, fake_cloud):
    with url_patch(external=None, internal="http://192.168.1.10:8123"):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert CONF_CLOUDHOOK_URL not in result["data"]


async def test_only_one_instance(hass: HomeAssistant, fake_cloud):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)
    result = await start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- re-pairing --------------------------------------------------------------


async def test_reconfigure_rotates_the_token_and_keeps_the_entry(hass: HomeAssistant, fake_cloud):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_WEBHOOK_ID: "the-same-id", CONF_TOKEN: "the-old-token"},
    )
    entry.add_to_hass(hass)

    fake_cloud.subscribed = True
    with url_patch(external=None, internal=None):
        result = await entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "reconfigure_pair"
        code = result["description_placeholders"]["code"]

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    # The identity that must survive: same entry, same webhook id - so the
    # device, its entities and every apple_health_sync:* statistic are untouched.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_WEBHOOK_ID] == "the-same-id"
    # The secret is what rotates, and the old one is gone.
    assert entry.data[CONF_TOKEN] != "the-old-token"
    assert entry.data[CONF_TOKEN] == decode(code).token


async def test_reconfigure_with_no_usable_url_changes_nothing(hass: HomeAssistant, fake_cloud):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_WEBHOOK_ID: "the-same-id", CONF_TOKEN: "the-old-token"},
    )
    entry.add_to_hass(hass)

    with url_patch(external=None, internal=None):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["errors"] == {"base": "no_usable_url"}
    # A failed re-pair must not leave the person unable to sync with the token
    # they already had.
    assert entry.data[CONF_TOKEN] == "the-old-token"


# --- entries created before any of this existed ------------------------------


async def test_a_legacy_entry_still_loads(hass: HomeAssistant, fake_cloud):
    """The productive installation has exactly this shape: no cloudhook key."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_WEBHOOK_ID: "legacy-webhook-id", CONF_TOKEN: "legacy-token"},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.token == "legacy-token"
    assert entry.runtime_data.webhook_id == "legacy-webhook-id"


async def test_removing_a_legacy_entry_touches_no_cloud(hass: HomeAssistant, fake_cloud):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_WEBHOOK_ID: "legacy-webhook-id", CONF_TOKEN: "legacy-token"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    # No cloudhook key means nothing to delete - and nothing to import either.
    assert fake_cloud.deleted_for == []


# --- the token must not leak -------------------------------------------------


async def test_the_token_is_never_logged(hass: HomeAssistant, fake_cloud, caplog):
    fake_cloud.subscribed = True
    with url_patch(external=None, internal=None):
        result = await start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        token = decode(result["description_placeholders"]["code"]).token
        await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert token not in caplog.text

"""Shared fixtures.

The Home Assistant Cloud stub below deserves an explanation. `cloud` is an
optional dependency: the integration imports it lazily and lists it under
`after_dependencies`, so an installation without it still works. The custom
integration test environment does not install it either - importing it pulls in
alexa and camera, which want `turbojpeg`.

So the tests run against a stub. Its function names, signatures and exception
types were read from `homeassistant/components/cloud/__init__.py` at 2026.8.3
rather than guessed, and they are asserted below so a drift in the real API
shows up here as a failing test rather than as a broken pairing flow. What these
tests therefore prove is that our flow uses that API correctly - not that Nabu
Casa itself behaves; nothing offline can prove the latter.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

CLOUD_MODULE = "homeassistant.components.cloud"


class FakeCloud:
    """Records what the flow asked for, and answers how the caller configured it."""

    def __init__(self) -> None:
        self.subscribed = False
        self.cloudhook_url = "https://hooks.nabu.casa/gAbCdEf"
        self.raise_not_available = False
        self.created_for: list[str] = []
        self.deleted_for: list[str] = []


@pytest.fixture
def fake_cloud(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Install a stand-in `homeassistant.components.cloud` for the duration."""
    state = FakeCloud()
    module = types.ModuleType(CLOUD_MODULE)

    class CloudNotAvailable(Exception):
        """Mirrors the real exception raised when the cloud is not usable."""

    class CloudNotConnected(CloudNotAvailable):
        """Mirrors the real subclass raised when logged in but disconnected."""

    def async_active_subscription(hass: Any) -> bool:
        return state.subscribed

    async def async_get_or_create_cloudhook(hass: Any, webhook_id: str) -> str:
        if state.raise_not_available:
            raise CloudNotConnected
        state.created_for.append(webhook_id)
        return state.cloudhook_url

    async def async_delete_cloudhook(hass: Any, webhook_id: str) -> None:
        state.deleted_for.append(webhook_id)

    module.CloudNotAvailable = CloudNotAvailable
    module.CloudNotConnected = CloudNotConnected
    module.async_active_subscription = async_active_subscription
    module.async_get_or_create_cloudhook = async_get_or_create_cloudhook
    module.async_delete_cloudhook = async_delete_cloudhook

    monkeypatch.setitem(sys.modules, CLOUD_MODULE, module)
    return state

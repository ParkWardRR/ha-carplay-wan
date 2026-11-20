"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.carplay_wan.const import DOMAIN

from .const import HOST, PAYLOAD, PORT, URL

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: Any,
) -> Generator[None, None, None]:
    """Without this the custom component is not loaded at all."""
    yield


@pytest.fixture
def mock_agent(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """A healthy agent."""
    aioclient_mock.get(URL, json=PAYLOAD)
    return aioclient_mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="CarPlay WAN",
        data={CONF_HOST: HOST, CONF_PORT: PORT},
        unique_id=f"{HOST}:{PORT}",
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_agent: AiohttpClientMocker
) -> MockConfigEntry:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry

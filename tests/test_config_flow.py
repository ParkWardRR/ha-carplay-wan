"""Config flow: every branch, including the ones that fail.

This is the Bronze rule the integration was failing. `_probe()` sorts a reply
into three different errors and a success, and until now none of the four was
covered -- so a typo in any of them would have shipped silently and shown a
user the wrong advice about their own network.
"""

from __future__ import annotations

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.carplay_wan.const import DOMAIN

from .const import HOST, PAYLOAD, PORT, URL

USER_INPUT = {CONF_HOST: HOST, CONF_PORT: PORT}


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]
    return result["flow_id"]


async def test_user_flow_creates_the_entry(
    hass: HomeAssistant, mock_agent: AiohttpClientMocker
) -> None:
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == f"{HOST}:{PORT}"


async def test_default_port_is_offered(hass: HomeAssistant) -> None:
    """Nobody should have to know 8099 by heart."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    schema = result["data_schema"].schema
    port = next(k for k in schema if k == CONF_PORT)
    assert port.default() == PORT


async def test_nothing_listening(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(URL, exc=ClientError)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_agent_too_slow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(URL, exc=TimeoutError)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "timeout"}


@pytest.mark.parametrize(
    "body",
    [
        {"nothing": "useful"},
        {"cars": "not a mapping"},
        ["a list"],
    ],
    ids=["no cars key", "cars is not a mapping", "not even an object"],
)
async def test_something_answered_but_it_is_not_the_agent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: object
) -> None:
    """Pointing this at carplayd's metrics port is the obvious wrong turn.

    It has its own error because "cannot connect" would send someone hunting a
    network fault that does not exist.
    """
    aioclient_mock.get(URL, json=body)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_the_agent"}


async def test_an_http_error_is_a_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(URL, status=500)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_recovery_after_an_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The form must stay usable after a failure.

    A flow that fails and then cannot succeed without being restarted is the
    specific thing this rule exists to catch.
    """
    aioclient_mock.get(URL, exc=ClientError)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=PAYLOAD)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_same_agent_cannot_be_added_twice(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_agent: AiohttpClientMocker,
) -> None:
    config_entry.add_to_hass(hass)
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

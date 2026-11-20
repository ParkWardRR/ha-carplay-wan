"""Setup, teardown, and what happens when the agent goes away."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.carplay_wan.const import DOMAIN

from .const import PAYLOAD, URL


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_fails_cleanly_when_the_agent_is_down(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A box that is off must give a retry, not a crash."""
    aioclient_mock.get(URL, exc=ClientError)
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_fails_when_the_reply_has_no_cars(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.get(URL, json={"agent": {"version": "0.1.0"}})
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_one_device_per_car(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), setup_integration.entry_id
    )
    assert {d.name for d in devices} == {"F32", "F22"}
    assert all(d.manufacturer == "carplay-wan" for d in devices)


async def test_entities_go_unavailable_when_the_agent_stops_answering(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Unavailable means the *agent* is unreachable.

    A car being away is not unavailable -- that is `not_home`, a real value.
    Conflating the two would make every automation fire on every trip, so the
    distinction gets a test of its own.
    """
    assert hass.states.get("device_tracker.f32").state == "home"

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, exc=ClientError)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    assert hass.states.get("device_tracker.f32").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.f32_signal").state == STATE_UNAVAILABLE


async def test_it_comes_back(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, exc=ClientError)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.f32").state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=PAYLOAD)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    assert hass.states.get("device_tracker.f32").state == "home"


async def test_a_car_vanishing_from_the_agent_does_not_break_the_others(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Removing a car from the agent's config should strand that car's
    entities, not take the whole integration down with them."""
    one_car = {**PAYLOAD, "cars": {"A": PAYLOAD["cars"]["A"]}}
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=one_car)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    assert hass.states.get("device_tracker.f32").state == "home"
    assert hass.states.get("device_tracker.f22").state == STATE_UNAVAILABLE


async def test_the_domain_is_set_up_from_the_entry_only(hass: HomeAssistant) -> None:
    """No YAML path exists, which is a Bronze expectation in its own right."""
    assert DOMAIN not in hass.config.components

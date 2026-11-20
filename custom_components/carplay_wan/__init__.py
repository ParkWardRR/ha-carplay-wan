"""CarPlay WAN: a car's own Wi-Fi link, as Home Assistant entities."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant

from .coordinator import CarPlayWanConfigEntry, CarPlayWanCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: CarPlayWanConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = CarPlayWanCoordinator(
        hass, entry, entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CarPlayWanConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

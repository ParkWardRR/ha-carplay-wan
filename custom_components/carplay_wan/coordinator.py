"""Poll the box agent and hand its state to the entities.

The integration never talks to the car, and never talks to carplayd either. It
polls one JSON endpoint on the box. That boundary is deliberate: anything that
touches the car link has to reason about airtime, and Home Assistant is the
wrong place to do that.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type CarPlayWanConfigEntry = ConfigEntry["CarPlayWanCoordinator"]


class CarPlayWanCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches /state from the agent."""

    config_entry: CarPlayWanConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CarPlayWanConfigEntry,
                 host: str, port: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self.url = f"http://{host}:{port}/state"
        self._session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self._session.get(
                self.url, timeout=ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json(content_type=None)
        except (ClientError, TimeoutError) as err:
            raise UpdateFailed(f"cannot reach the agent at {self.url}: {err}") from err

        if "cars" not in data:
            raise UpdateFailed("the agent replied, but with no cars in it")
        return data

    def car(self, car: str) -> dict[str, Any]:
        """One car's slice, or an empty dict if the agent stopped reporting it.

        Returning {} rather than raising matters: a car that disappears from
        the agent's config should make its entities unavailable, not break
        every other entity in the coordinator.
        """
        if not self.data:
            return {}
        cars: dict[str, Any] = self.data.get("cars", {})
        return cars.get(car, {})

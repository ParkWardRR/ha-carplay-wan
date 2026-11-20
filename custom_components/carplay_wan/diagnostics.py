"""Diagnostics, redacted.

A diagnostics download is the most common way a credential escapes a Home
Assistant install, because people paste them into public issues. The agent
already hashes identifiers unless it was started with --reveal; this redacts
again rather than trusting that, because the cost of being wrong is somebody's
car network key on the internet, and the cost of being right twice is nothing.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import TO_REDACT
from .coordinator import CarPlayWanConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CarPlayWanConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data: dict[str, Any] = coordinator.data or {}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "agent": data.get("agent", {}),
        "upstream": data.get("upstream"),
        "cars": {
            car: async_redact_data(car_data, TO_REDACT)
            for car, car_data in data.get("cars", {}).items()
        },
    }

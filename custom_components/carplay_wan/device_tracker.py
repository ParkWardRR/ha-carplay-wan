"""Device tracker: is the car home?

This is the entity that justifies the project. A phone-based tracker says the
car is home because the driver is; a GPS tracker says nothing at all two floors
underground. This says the car is home because its own radio is audible from
the garage, which is the thing actually being asked.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CarPlayWanConfigEntry, CarPlayWanCoordinator
from .entity import CarPlayWanEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CarPlayWanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        CarPlayWanTracker(coordinator, car)
        for car in (coordinator.data or {}).get("cars", {})
    )


class CarPlayWanTracker(CarPlayWanEntity, TrackerEntity):
    _attr_name = None  # the device's own name is the right one here

    def __init__(self, coordinator: CarPlayWanCoordinator, car: str) -> None:
        super().__init__(coordinator, car, "tracker")

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        return bool(self.car_data.get("present"))

    @property
    def icon(self) -> str:
        return "mdi:car" if self.is_connected else "mdi:car-off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.car_data
        # Deliberately not here: the SSID and any raw MAC. Attributes end up in
        # the recorder, in templates and in screenshots people post.
        return {
            "awake": d.get("present"),
            "route_up": d.get("serving"),
            "internet_validated": d.get("validated"),
            "signal_dbm": d.get("signal_dbm"),
            "phones": d.get("phones"),
            "last_arrival": d.get("last_arrival"),
            "last_departure": d.get("last_departure"),
            "last_departure_reason": d.get("last_departure_reason"),
        }

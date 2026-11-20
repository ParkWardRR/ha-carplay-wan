"""Device tracker: is the car home?

This is the entity that justifies the project. A phone-based tracker says the
car is home because the driver is; a GPS tracker says nothing at all two floors
underground. This says the car is home because its own radio is audible from
the garage, which is the thing actually being asked.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import STATE_HOME, STATE_NOT_HOME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CarPlayWanConfigEntry, CarPlayWanCoordinator
from .entity import CarPlayWanEntity

# Everything here is read-only and served from one coordinator, so there is
# no device to overwhelm and no reason to serialise updates.
PARALLEL_UPDATES = 0


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
    """Home or away, from the car's own beacon.

    Choosing between Home Assistant's two tracker bases is not obvious and
    getting it wrong is silent, so the reasoning lives here:

    * `ScannerEntity` is the router-style base and takes its state straight
      from `is_connected`, which is exactly the shape of this data. But its
      `device_info` is `@final` and returns `None` -- scanner entities are
      forbidden from creating device registry entries -- and its `unique_id`
      is hardwired to `mac_address`. Using it would scatter the tracker away
      from its car's device and key it on a MAC this integration deliberately
      does not publish.
    * `TrackerEntity` keeps the device grouping. Its state is `location_name`
      when one is set, falling back to zone maths on coordinates. We have no
      coordinates and never will -- the whole point is that this works where
      GPS does not -- so `location_name` is the correct and only lever.

    The first draft inherited `TrackerEntity` but implemented `is_connected`,
    which that base ignores entirely: every tracker reported `unknown` and
    nothing warned. The tests caught it.
    """

    _attr_name = None  # the device's own name is the right one here

    def __init__(self, coordinator: CarPlayWanCoordinator, car: str) -> None:
        super().__init__(coordinator, car, "tracker")

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def location_name(self) -> str:
        """home / not_home, which TrackerEntity reports as the state."""
        return STATE_HOME if self.car_data.get("present") else STATE_NOT_HOME

    @property
    def icon(self) -> str:
        return "mdi:car" if self.car_data.get("present") else "mdi:car-off"

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

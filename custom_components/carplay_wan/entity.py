"""Shared base: one Home Assistant device per car."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CarPlayWanCoordinator


class CarPlayWanEntity(CoordinatorEntity[CarPlayWanCoordinator]):
    """Base entity bound to one car."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CarPlayWanCoordinator, car: str, key: str) -> None:
        super().__init__(coordinator)
        self._car = car
        self._key = key
        name = self.car_data.get("name") or f"Car {car}"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{car}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_{car}")},
            name=name,
            manufacturer="carplay-wan",
            model="CarPlay Wi-Fi link",
            sw_version=(coordinator.data or {}).get("agent", {}).get("version"),
        )

    @property
    def car_data(self) -> dict[str, Any]:
        return self.coordinator.car(self._car)

    @property
    def available(self) -> bool:
        # The agent being reachable is what availability means here. A car
        # that is away is *present: false*, which is a value, not an outage --
        # conflating the two would make every automation fire on every trip.
        return super().available and bool(self.car_data)

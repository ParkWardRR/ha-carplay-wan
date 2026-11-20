"""Binary sensors: awake, route up, internet validated, upstream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CarPlayWanConfigEntry, CarPlayWanCoordinator
from .entity import CarPlayWanEntity


@dataclass(frozen=True, kw_only=True)
class CarPlayWanBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[CarPlayWanBinaryDescription, ...] = (
    CarPlayWanBinaryDescription(
        key="present",
        translation_key="awake",
        device_class=BinarySensorDeviceClass.PRESENCE,
        # The AP exists only while the car is on or in accessory mode, so
        # "beaconing" and "awake" are the same fact. This is the entity most
        # automations should hang off: it goes true the moment someone starts
        # the car, two floors underground, where nothing else knows.
        value=lambda d: d.get("present"),
    ),
    CarPlayWanBinaryDescription(
        key="serving",
        translation_key="route_up",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value=lambda d: d.get("serving"),
    ),
    CarPlayWanBinaryDescription(
        key="validated",
        translation_key="internet_validated",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        # The one that matters. iOS ranks a CarPlay network as low as it goes,
        # so the box can be serving perfectly while every app ignores the
        # route. False here, with the car home and the route up, is the state
        # that means "go tap Use For Internet".
        value=lambda d: d.get("validated"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CarPlayWanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        CarPlayWanBinarySensor(coordinator, car, desc)
        for car in (coordinator.data or {}).get("cars", {})
        for desc in BINARY_SENSORS
    ]
    entities.extend(
        CarPlayWanUpstream(coordinator, car)
        for car in (coordinator.data or {}).get("cars", {})
    )
    async_add_entities(entities)


class CarPlayWanBinarySensor(CarPlayWanEntity, BinarySensorEntity):
    entity_description: CarPlayWanBinaryDescription

    def __init__(
        self,
        coordinator: CarPlayWanCoordinator,
        car: str,
        description: CarPlayWanBinaryDescription,
    ) -> None:
        super().__init__(coordinator, car, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.car_data)


class CarPlayWanUpstream(CarPlayWanEntity, BinarySensorEntity):
    """The box's own internet. Global, but attached to each car's device so a
    dashboard for one car explains its own failure without a second lookup."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "upstream"

    def __init__(self, coordinator: CarPlayWanCoordinator, car: str) -> None:
        super().__init__(coordinator, car, "upstream")

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return bool(self.coordinator.data.get("upstream"))

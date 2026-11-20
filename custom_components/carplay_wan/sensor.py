"""Sensors: signal, phones, bytes, session length, link rate, head unit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CarPlayWanConfigEntry, CarPlayWanCoordinator
from .entity import CarPlayWanEntity


@dataclass(frozen=True, kw_only=True)
class CarPlayWanSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]


SENSORS: tuple[CarPlayWanSensorDescription, ...] = (
    CarPlayWanSensorDescription(
        key="signal_dbm",
        translation_key="signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.get("signal_dbm"),
    ),
    CarPlayWanSensorDescription(
        key="phones",
        translation_key="phones",
        icon="mdi:cellphone",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.get("phones"),
    ),
    CarPlayWanSensorDescription(
        key="forwarded_bytes",
        translation_key="forwarded",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        # The counter resets when carplayd restarts and when a new session
        # starts, which is exactly what TOTAL_INCREASING is for -- TOTAL would
        # make every reset look like a negative spike in the statistics.
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda d: d.get("forwarded_bytes"),
    ),
    CarPlayWanSensorDescription(
        key="session_duration",
        translation_key="awake_for",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value=lambda d: d.get("session_duration"),
    ),
    CarPlayWanSensorDescription(
        key="link_rate_mbps",
        translation_key="link_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.get("link_rate_mbps"),
    ),
    CarPlayWanSensorDescription(
        key="head_unit",
        translation_key="head_unit",
        icon="mdi:car-info",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.get("head_unit"),
    ),
    CarPlayWanSensorDescription(
        key="last_departure_reason",
        translation_key="last_departure",
        icon="mdi:car-arrow-right",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.get("last_departure_reason"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CarPlayWanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        CarPlayWanSensor(coordinator, car, desc)
        for car in (coordinator.data or {}).get("cars", {})
        for desc in SENSORS
    )


class CarPlayWanSensor(CarPlayWanEntity, SensorEntity):
    entity_description: CarPlayWanSensorDescription

    def __init__(
        self,
        coordinator: CarPlayWanCoordinator,
        car: str,
        description: CarPlayWanSensorDescription,
    ) -> None:
        super().__init__(coordinator, car, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.car_data)

"""The entities themselves: both cars, both states, and the awkward nulls."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        # Car A: home, awake, serving, validated.
        ("device_tracker.f32", "home"),
        ("binary_sensor.f32_awake", STATE_ON),
        ("binary_sensor.f32_route_up", STATE_ON),
        ("binary_sensor.f32_internet_validated", STATE_ON),
        ("sensor.f32_signal", "-58"),
        ("sensor.f32_phones", "1"),
        ("sensor.f32_awake_for", "100"),
        ("sensor.f32_link_rate", "65.0"),
        ("sensor.f32_head_unit", "bssid-9f21c0ab4e"),
        # Car B: away. The nulls are the interesting half -- a sensor with no
        # value must be unknown, not 0, or statistics and automations will both
        # read an absent car as a car reporting zero.
        ("device_tracker.f22", "not_home"),
        ("binary_sensor.f22_awake", STATE_OFF),
        ("binary_sensor.f22_route_up", STATE_OFF),
        ("binary_sensor.f22_internet_validated", STATE_OFF),
        ("sensor.f22_signal", STATE_UNKNOWN),
        ("sensor.f22_awake_for", STATE_UNKNOWN),
        ("sensor.f22_link_rate", STATE_UNKNOWN),
        ("sensor.f22_last_departure", "car switched off"),
    ],
)
async def test_states(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    entity_id: str,
    expected: str,
) -> None:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} was never created"
    assert state.state == expected


async def test_forwarded_bytes_is_reported_in_megabytes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The agent counts bytes; nobody wants to read 1255890 on a dashboard."""
    state = hass.states.get("sensor.f32_forwarded")
    assert state is not None
    assert float(state.state) == pytest.approx(1.25589, rel=1e-4)
    assert state.attributes["unit_of_measurement"] == "MB"


async def test_upstream_is_attached_to_each_car(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The box's internet is one fact, but a dashboard for one car should
    explain that car's failure without a second lookup."""
    for entity_id in ("binary_sensor.f32_box_upstream", "binary_sensor.f22_box_upstream"):
        assert hass.states.get(entity_id).state == STATE_ON


async def test_tracker_attributes_carry_the_useful_context(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    attrs = hass.states.get("device_tracker.f32").attributes
    assert attrs["awake"] is True
    assert attrs["internet_validated"] is True
    assert attrs["signal_dbm"] == -58
    assert attrs["last_arrival"] == "2026-11-19T17:41:04.053-08:00"


async def test_no_identifier_leaks_into_a_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Attributes end up in the recorder, in templates and in screenshots.

    The agent hashes BSSIDs before they leave the box; this asserts the
    integration does not undo that by putting an SSID or a raw MAC back into
    an attribute.
    """
    import re

    mac = re.compile(r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
    for state in hass.states.async_all():
        if not state.entity_id.endswith(("f32", "f22")) and "f32" not in state.entity_id:
            continue
        blob = f"{state.state} {state.attributes}"
        assert not mac.search(blob), f"a MAC leaked into {state.entity_id}"
        assert "CarPlay" not in blob, f"an SSID leaked into {state.entity_id}"


async def test_diagnostic_entities_are_categorised(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Signal, link rate and head unit are diagnostics. The two entities people
    actually automate on are not -- putting those behind the diagnostics fold
    would be a usability bug, so both halves are asserted."""
    registry = er.async_get(hass)

    for entity_id in (
        "sensor.f32_signal",
        "sensor.f32_link_rate",
        "sensor.f32_head_unit",
        "binary_sensor.f32_box_upstream",
        # Not our choice: BaseTrackerEntity sets entity_category = DIAGNOSTIC
        # for every tracker in Home Assistant. Asserted so that if upstream
        # ever changes it, this tells us instead of a dashboard quietly moving.
        "device_tracker.f32",
    ):
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC

    for entity_id in ("binary_sensor.f32_awake", "binary_sensor.f32_internet_validated"):
        assert registry.async_get(entity_id).entity_category is None


async def test_every_entity_lands_on_its_own_car(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One device per car, with everything under it -- including the tracker.

    This is worth pinning down: BaseTrackerEntity declares
    `_attr_device_info: None = None`, so a tracker only keeps its device
    because the shared base sets the attribute on the instance. A refactor
    that moved that assignment to the class would silently orphan the
    headline entity of the whole integration.
    """
    from homeassistant.helpers import device_registry as dr

    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    for entity_id, car_name in (
        ("device_tracker.f32", "F32"),
        ("sensor.f32_signal", "F32"),
        ("binary_sensor.f32_awake", "F32"),
        ("device_tracker.f22", "F22"),
        ("sensor.f22_signal", "F22"),
    ):
        entry = entities.async_get(entity_id)
        assert entry is not None and entry.device_id is not None, entity_id
        assert devices.async_get(entry.device_id).name == car_name


async def test_unique_ids_are_unique_and_stable(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_integration.entry_id)
    unique_ids = [e.unique_id for e in entries]

    assert len(unique_ids) == len(set(unique_ids))
    assert all(uid.startswith(setup_integration.entry_id) for uid in unique_ids)
    # Two cars must not collide on a shared key.
    assert len([u for u in unique_ids if u.endswith("_signal_dbm")]) == 2

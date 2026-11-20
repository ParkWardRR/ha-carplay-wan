"""Diagnostics: the thing people paste into public issues.

Upstream published secrets twice. A diagnostics download is the most likely
third time, so the redaction is asserted rather than assumed.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator


async def test_diagnostics_redacts_the_host_and_the_head_unit(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)

    assert diag["entry"]["host"] == "**REDACTED**"
    assert diag["cars"]["A"]["head_unit"] == "**REDACTED**"
    # Still useful afterwards: the fields that explain a fault survive.
    assert diag["cars"]["A"]["present"] is True
    assert diag["cars"]["A"]["validated"] is True
    assert diag["agent"]["version"] == "0.1.0"


async def test_no_identifier_survives_anywhere_in_the_dump(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """Belt and braces: scan the serialised blob, not just the fields we
    remembered to check. A key added later is exactly how this regresses."""
    import re

    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    blob = json.dumps(diag)

    assert not re.search(r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b", blob, re.I)
    assert "CarPlay" not in blob
    assert "garage-box.example" not in blob


async def test_the_agent_reports_whether_it_was_redacting(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """If somebody ran the agent with --reveal, the dump should say so, so a
    maintainer reading an issue knows the identifiers in it are real."""
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    assert diag["agent"]["redacted"] is True

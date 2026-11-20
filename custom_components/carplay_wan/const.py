"""Constants for the CarPlay WAN integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "carplay_wan"

CONF_CAR: Final = "car"

DEFAULT_PORT: Final = 8099
DEFAULT_NAME: Final = "CarPlay WAN"

# The agent polls carplayd every 5 s and a car's state does not change faster
# than a person can get into it. Polling harder would add nothing but recorder
# rows, and the agent is on the same LAN, not the car link -- none of this
# traffic touches the channel CarPlay is using.
SCAN_INTERVAL: Final = timedelta(seconds=10)

# Anything that identifies the car or the people in it. The agent already
# hashes these unless it was started with --reveal, but a diagnostics download
# is the single most common way a credential escapes a Home Assistant install,
# so the integration redacts again rather than trusting the upstream default.
TO_REDACT: Final = {
    "head_unit",
    "bssid",
    "ssid",
    "mac",
    "psk",
    "password",
    "host",
    "unique_id",
}

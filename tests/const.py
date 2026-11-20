"""One payload, copied from the live agent, used by every test.

Taken from `carplay_wan_agent.py --once` on the real box rather than invented,
so the keys here are the keys the integration will actually meet. A test that
agrees with a fixture I made up would prove nothing: the first bug this kind
of integration gets is a key that was renamed on one side only.
"""

HOST = "garage-box.example"
PORT = 8099
URL = f"http://{HOST}:{PORT}/state"

# Car A is home, awake, serving and validated. Car B is away. Both states have
# to be exercised: "away" is the state the entities spend most of their life in.
PAYLOAD = {
    "agent": {
        "version": "0.1.0",
        "uptime": 4213,
        "events_seen": 91422,
        "metrics_ok": True,
        "redacted": True,
    },
    "upstream": True,
    "cars": {
        "A": {
            "car": "A",
            "name": "F32",
            "present": True,
            "serving": True,
            "validated": True,
            "signal_dbm": -58,
            "phones": 1,
            "forwarded_bytes": 1255890,
            "probe_bytes": 4304,
            "link_rate_mbps": 65.0,
            "beacon_loss": 19,
            "head_unit": "bssid-9f21c0ab4e",
            "key_mgmt": "WPA2-PSK",
            "wifi_generation": "4",
            "session_start": 1763600000.0,
            "last_seen": 1763600100.0,
            "last_arrival": "2026-11-19T17:41:04.053-08:00",
            "last_departure": None,
            "last_departure_reason": None,
            "detect_seconds": 0.221,
            "session_duration": 100,
            "state": "home",
        },
        "B": {
            "car": "B",
            "name": "F22",
            "present": False,
            "serving": False,
            "validated": False,
            "signal_dbm": None,
            "phones": 0,
            "forwarded_bytes": 372414,
            "probe_bytes": 11638,
            "link_rate_mbps": None,
            "beacon_loss": None,
            "head_unit": None,
            "key_mgmt": None,
            "wifi_generation": None,
            "session_start": None,
            "last_seen": None,
            "last_arrival": None,
            "last_departure": "2026-11-19T14:19:07-0800",
            "last_departure_reason": "car switched off",
            "detect_seconds": 0.198,
            "session_duration": None,
            "state": "not_home",
        },
    },
}

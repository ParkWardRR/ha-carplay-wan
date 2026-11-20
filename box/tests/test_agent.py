"""Unit tests for the agent. Standard library only: these have to run on the
box, which has Python 3.11 and nothing installed.

    python3 -m unittest discover -s box/tests -v

Every event line below is copied verbatim from a real carplayd log, including
the evening of 2026-11-18 when the box left a car that was parked six feet
away. Synthetic fixtures would have agreed with whatever the parser did.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carplay_wan_agent import (  # noqa: E402
    PRESENCE_TIMEOUT,
    Redactor,
    Tracker,
    parse_cars,
    parse_event,
    parse_metrics,
)

REAL_LOG = [
    "2026-11-18T19:55:27.799-08:00 EV 393004.886 carplayd_seen A 2462 -50 bc:30:7e:8a:6b:0e",
    "2026-11-18T19:55:27.799-08:00 EV 393004.887 carplayd_select A -50 bc:30:7e:8a:6b:0e",
    "2026-11-18T19:55:28.075-08:00 EV 393005.163 carplayd_connected A bc:30:7e:8a:6b:0e freq=2462 key_mgmt=WPA2-PSK pairwise=CCMP group=TKIP wifi_generation=4",
    "2026-11-18T19:55:28.130-08:00 EV 393005.218 carplayd_serving A fd5c:ba1e:f32:a::/64 route",
    "2026-11-18T19:55:49.695-08:00 EV 393026.783 carplayd_leave A -60 trend base=-51",
    "2026-11-18T19:55:50.797-08:00 EV 393027.884 carplayd_goodbye A",
    "2026-11-18T19:58:13.796-08:00 EV 393170.883 carplayd_gone A 166s",
]

METRICS = """\
# HELP carplayd_serving 1 while serving a car.
# TYPE carplayd_serving gauge
carplayd_serving{car="A"} 1
carplayd_serving{car="B"} 0
# TYPE carplayd_signal_dbm gauge
carplayd_signal_dbm{car="A"} -58
# TYPE carplayd_phones gauge
carplayd_phones{car="A"} 1
# HELP carplayd_upstream 1 while the internet is reachable through the uplink.
# TYPE carplayd_upstream gauge
carplayd_upstream 1
# TYPE carplayd_phone_probe_bytes counter
carplayd_phone_probe_bytes{car="A"} 4304
# TYPE carplayd_phone_forward_bytes counter
carplayd_phone_forward_bytes{car="A"} 1255890
# TYPE carplayd_events_total counter
carplayd_events_total{event="serving"} 12
"""


def tracker(reveal: bool = False) -> Tracker:
    return Tracker({"A": "F32", "B": "F22"}, Redactor("test-salt", reveal=reveal))


class TestParseEvent(unittest.TestCase):
    def test_event_with_car_and_kv(self) -> None:
        ev = parse_event(REAL_LOG[2])
        assert ev is not None
        self.assertEqual(ev.event, "connected")
        self.assertEqual(ev.car, "A")
        self.assertEqual(ev.kv["key_mgmt"], "WPA2-PSK")
        self.assertEqual(ev.kv["wifi_generation"], "4")
        self.assertEqual(ev.args[0], "bc:30:7e:8a:6b:0e")

    def test_signal_is_found_among_bare_args(self) -> None:
        self.assertEqual(parse_event(REAL_LOG[0]).signal, -50)
        self.assertEqual(parse_event(REAL_LOG[4]).signal, -60)

    def test_event_without_a_car_letter(self) -> None:
        # The supplicant chatter is keyed by interface, not by car letter, and
        # must not be mistaken for car "c".
        ev = parse_event("2026-11-18T20:01:05.690-08:00 EV 393342.778 carplayd_wpa cara WPS-AP-AVAILABLE")
        assert ev is not None
        self.assertIsNone(ev.car)
        self.assertEqual(ev.event, "wpa")

    def test_goodbye_has_no_arguments(self) -> None:
        ev = parse_event(REAL_LOG[5])
        assert ev is not None
        self.assertEqual((ev.event, ev.car, ev.args), ("goodbye", "A", []))

    def test_junk_is_ignored(self) -> None:
        for line in ("", "\n", "not an event", "2026-11-18 something else"):
            self.assertIsNone(parse_event(line))


class TestParseMetrics(unittest.TestCase):
    def test_labelled_and_global(self) -> None:
        m = parse_metrics(METRICS)
        self.assertEqual(m["carplayd_serving"]["A"], 1.0)
        self.assertEqual(m["carplayd_serving"]["B"], 0.0)
        self.assertEqual(m["carplayd_upstream"][""], 1.0)
        self.assertEqual(m["carplayd_events_total"]["serving"], 12.0)

    def test_comments_do_not_become_samples(self) -> None:
        self.assertNotIn("HELP", parse_metrics(METRICS))


class TestSession(unittest.TestCase):
    def test_arrival_then_departure(self) -> None:
        t = tracker()
        for line in REAL_LOG:
            ev = parse_event(line)
            assert ev is not None
            t.apply_event(ev)
        a = t.snapshot()["cars"]["A"]
        self.assertFalse(a["present"])
        self.assertEqual(a["state"], "not_home")
        self.assertEqual(a["last_departure_reason"], "left on trend")
        self.assertIsNone(a["session_start"])

    def test_mid_session_state(self) -> None:
        t = tracker()
        for line in REAL_LOG[:4]:
            t.apply_event(parse_event(line))
        a = t.snapshot()["cars"]["A"]
        self.assertTrue(a["present"])
        self.assertTrue(a["serving"])
        self.assertEqual(a["state"], "home")
        self.assertEqual(a["signal_dbm"], -50)
        self.assertEqual(a["key_mgmt"], "WPA2-PSK")
        self.assertIsNotNone(a["session_duration"])

    def test_the_other_car_is_untouched(self) -> None:
        t = tracker()
        for line in REAL_LOG:
            t.apply_event(parse_event(line))
        b = t.snapshot()["cars"]["B"]
        self.assertFalse(b["present"])
        self.assertIsNone(b["head_unit"])

    def test_departure_reason_when_the_car_is_switched_off(self) -> None:
        t = tracker()
        t.apply_event(parse_event(REAL_LOG[0]))
        t.apply_event(parse_event(
            "2026-11-18T09:08:34.300-08:00 EV 354191.388 carplayd_link_lost A -58 "
            "reason=3 why=sta_leaving locally_generated=0 bssid=bc:30:7e:8a:6b:0e"))
        self.assertEqual(t.snapshot()["cars"]["A"]["last_departure_reason"],
                         "car switched off")

    def test_validation_from_the_probe_counter(self) -> None:
        # The probe counter is the honest answer to "will apps use this link?"
        # and has to work even if the agent started mid-session and never saw
        # the validated event.
        t = tracker()
        t.apply_event(parse_event(REAL_LOG[0]))
        t.apply_metrics(parse_metrics(METRICS))
        self.assertTrue(t.snapshot()["cars"]["A"]["validated"])

    def test_unvalidated_when_no_probe_bytes(self) -> None:
        t = tracker()
        t.apply_event(parse_event(REAL_LOG[0]))
        t.apply_metrics(parse_metrics(METRICS.replace(
            'carplayd_phone_probe_bytes{car="A"} 4304',
            'carplayd_phone_probe_bytes{car="A"} 0')))
        self.assertFalse(t.snapshot()["cars"]["A"]["validated"])

    def test_presence_expires_without_a_gone_event(self) -> None:
        # The agent must not depend on seeing `gone`: a restart or a missed
        # line would otherwise strand a car at home forever.
        t = tracker()
        t.apply_event(parse_event(REAL_LOG[0]))
        self.assertTrue(t.snapshot()["cars"]["A"]["present"])
        t.cars["A"].last_seen = time.time() - PRESENCE_TIMEOUT - 1
        t.expire()
        a = t.snapshot()["cars"]["A"]
        self.assertFalse(a["present"])
        self.assertEqual(a["last_departure_reason"], "stopped beaconing")

    def test_a_new_session_resets_the_last_one(self) -> None:
        t = tracker()
        for line in REAL_LOG:
            t.apply_event(parse_event(line))
        t.apply_metrics(parse_metrics(METRICS))
        t.apply_event(parse_event(REAL_LOG[0]))  # arrives again
        a = t.snapshot()["cars"]["A"]
        self.assertTrue(a["present"])
        self.assertFalse(a["validated"], "stale validation must not carry over")
        self.assertEqual(a["forwarded_bytes"], 0)

    def test_arrival_fires_the_probe_once(self) -> None:
        fired: list[str] = []
        t = Tracker({"A": "F32"}, Redactor("s"), on_arrival=fired.append)
        for line in REAL_LOG[:4]:
            t.apply_event(parse_event(line))
        for _ in range(50):
            if fired:
                break
            time.sleep(0.01)
        self.assertEqual(fired, ["A"], "one arrival is one probe, not four")


class TestWatchMode(unittest.TestCase):
    def test_a_beacon_alone_is_enough_for_presence(self) -> None:
        t = tracker()
        t.sighting("A", -61, "bc:30:7e:8a:6b:0e")
        a = t.snapshot()["cars"]["A"]
        self.assertTrue(a["present"])
        self.assertEqual(a["signal_dbm"], -61)
        self.assertFalse(a["serving"], "watching is not serving")


class TestRedaction(unittest.TestCase):
    def test_bssid_never_leaves_in_the_clear(self) -> None:
        t = tracker()
        for line in REAL_LOG[:4]:
            t.apply_event(parse_event(line))
        blob = json.dumps(t.snapshot())
        self.assertNotIn("bc:30:7e:8a:6b:0e", blob)
        self.assertIn("bssid-", blob)
        self.assertTrue(t.snapshot()["agent"]["redacted"])

    def test_reveal_is_opt_in_and_says_so(self) -> None:
        t = tracker(reveal=True)
        for line in REAL_LOG[:4]:
            t.apply_event(parse_event(line))
        snap = t.snapshot()
        self.assertIn("bc:30:7e:8a:6b:0e", json.dumps(snap))
        self.assertFalse(snap["agent"]["redacted"])

    def test_the_salt_changes_the_hash(self) -> None:
        mac = "bc:30:7e:8a:6b:0e"
        self.assertNotEqual(Redactor("salt-one")(mac), Redactor("salt-two")(mac))

    def test_same_salt_is_stable(self) -> None:
        mac = "bc:30:7e:8a:6b:0e"
        self.assertEqual(Redactor("s")(mac), Redactor("s")(mac))


class TestCarSpec(unittest.TestCase):
    def test_with_and_without_ssid(self) -> None:
        names, ssids = parse_cars(["A=F32", "B=F22=BMW00000 CarPlay"])
        self.assertEqual(names, {"A": "F32", "B": "F22"})
        self.assertEqual(ssids, {"B": "BMW00000 CarPlay"})

    def test_rejects_nonsense(self) -> None:
        for bad in (["F32"], ["AA=F32"], ["=x"]):
            with self.assertRaises(SystemExit):
                parse_cars(bad)


if __name__ == "__main__":
    unittest.main()

"""Tests for the head-unit probe's parsers.

The probe cannot be run on demand -- the car's AP only exists while the car is
awake -- so the parsers have to be right the first time they ever see a head
unit. They are tested two ways: against a real beacon captured off the air,
and against a synthetic one carrying the full WPS attribute set that only ever
appears in a probe response.

    python3 -m unittest discover -s box/tests -v
"""

from __future__ import annotations

import struct
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "probe"))

from headunit_probe import (  # noqa: E402
    Budget,
    decode_interworking,
    decode_wps,
    parse_ies,
    summarise_ies,
)

REAL_BEACON = bytes.fromhex((HERE / "fixtures" / "beacon_with_wps.hex").read_text().strip())


def ie(eid: int, body: bytes) -> bytes:
    return bytes([eid, len(body)]) + body


def wps_attr(attr: int, val: bytes) -> bytes:
    return struct.pack(">HH", attr, len(val)) + val


def synthetic_probe_response() -> bytes:
    """What a head unit should answer a WPS probe request with.

    Values are invented; the encoding is not. This is the shape the real
    answer will arrive in, and the only chance to get the parser right before
    a car is in front of it.
    """
    wps = b"".join([
        wps_attr(0x104A, b"\x10"),
        wps_attr(0x1044, b"\x02"),
        wps_attr(0x1011, "NBT EVO HU".encode()),
        wps_attr(0x1021, "Harman/Becker".encode()),
        wps_attr(0x1023, "NBT EVO ID6".encode()),
        wps_attr(0x1024, "6NBT-2016".encode()),
        wps_attr(0x1042, "FIXTURE-SERIAL-0001".encode()),
        wps_attr(0x1047, bytes(range(16))),
        wps_attr(0x1054, struct.pack(">H4sH", 7, bytes.fromhex("0050f204"), 1)),
        wps_attr(0x103C, b"\x01"),
    ])
    return b"".join([
        ie(0, b"BMW00000 CarPlay"),
        ie(3, b"\x0b"),
        ie(7, b"US \x01\x0b\x1e"),
        # 802.11u with the Internet bit CLEAR, which is what these cars do.
        ie(107, b"\x00"),
        ie(221, bytes.fromhex("0050f204") + wps),
        ie(221, bytes.fromhex("0017f2") + b"\x0a\x00\x01"),
    ])


class TestIEWalking(unittest.TestCase):
    def test_a_real_beacon_parses(self) -> None:
        elements = parse_ies(REAL_BEACON)
        self.assertGreater(len(elements), 10)
        self.assertEqual(elements[0][0], 0, "first element should be the SSID")

    def test_truncated_trailer_is_dropped_not_raised(self) -> None:
        """Beacons come off the air; a short last element is a damaged frame."""
        blob = ie(0, b"x") + bytes([221, 40]) + b"\x00\x05"
        self.assertEqual(parse_ies(blob), [(0, b"x")])

    def test_empty_and_garbage(self) -> None:
        self.assertEqual(parse_ies(b""), [])
        self.assertEqual(parse_ies(b"\x00"), [])


class TestRealBeacon(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = summarise_ies(REAL_BEACON)

    def test_basics_are_recovered(self) -> None:
        self.assertEqual(self.summary["ssid"], "CarPlay-Fixture")
        self.assertEqual(self.summary["channel"], 11)
        self.assertEqual(self.summary["country"], "US")

    def test_vendor_elements_are_enumerated(self) -> None:
        ouis = {v["oui"] for v in self.summary["vendor"]}
        self.assertIn("0050f2", ouis, "WPS/WMM vendor element expected")
        self.assertTrue(all("vendor" in v and "len" in v for v in self.summary["vendor"]))

    def test_a_real_beacon_carries_almost_no_wps(self) -> None:
        """The finding that justifies probe_wps.

        A real beacon advertises version, state and RF bands and nothing that
        names the device. If this ever starts returning a manufacturer, the
        active probe can be dropped.
        """
        wps = self.summary.get("wps", {})
        self.assertIn("wps_state", wps)
        for rich in ("manufacturer", "model_name", "serial_number"):
            self.assertNotIn(rich, wps)


class TestProbeResponse(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = summarise_ies(synthetic_probe_response())

    def test_the_device_names_itself(self) -> None:
        wps = self.summary["wps"]
        self.assertEqual(wps["manufacturer"], "Harman/Becker")
        self.assertEqual(wps["model_name"], "NBT EVO ID6")
        self.assertEqual(wps["model_number"], "6NBT-2016")
        self.assertEqual(wps["serial_number"], "FIXTURE-SERIAL-0001")
        self.assertEqual(wps["device_name"], "NBT EVO HU")

    def test_structured_attributes(self) -> None:
        wps = self.summary["wps"]
        self.assertEqual(wps["wps_state"], "configured")
        self.assertEqual(wps["uuid_e"], bytes(range(16)).hex())
        self.assertEqual(wps["primary_device_type"]["category"], 7)

    def test_apple_elements_are_collected(self) -> None:
        apple = self.summary.get("apple_elements", [])
        self.assertEqual(len(apple), 1)
        self.assertEqual(apple[0]["oui"], "0017f2")

    def test_the_internet_bit_is_read_correctly(self) -> None:
        """The single most load-bearing bit in this whole project: these head
        units advertise no internet, which is why the box exists."""
        self.assertFalse(self.summary["interworking"]["internet"])


class TestInterworking(unittest.TestCase):
    def test_internet_bit_set_and_clear(self) -> None:
        self.assertTrue(decode_interworking(b"\x10")["internet"])
        self.assertFalse(decode_interworking(b"\x00")["internet"])

    def test_access_network_type_is_the_low_nibble(self) -> None:
        self.assertEqual(decode_interworking(b"\x13")["access_network_type"], 3)

    def test_empty(self) -> None:
        self.assertEqual(decode_interworking(b""), {})


class TestWpsDecoder(unittest.TestCase):
    def test_truncated_attribute_stops_cleanly(self) -> None:
        blob = wps_attr(0x1021, b"ACME") + struct.pack(">HH", 0x1023, 99) + b"short"
        out = decode_wps(blob)
        self.assertEqual(out["manufacturer"], "ACME")
        self.assertNotIn("model_name", out)

    def test_unknown_attributes_are_kept_as_hex(self) -> None:
        out = decode_wps(wps_attr(0xBEEF, b"\x01\x02"))
        self.assertEqual(out["unknown"]["0xbeef"], "0102")

    def test_nulls_and_padding_are_stripped(self) -> None:
        out = decode_wps(wps_attr(0x1021, b"ACME\x00\x00"))
        self.assertEqual(out["manufacturer"], "ACME")


class TestBudget(unittest.TestCase):
    """The safety rail. CarPlay shares this channel, so a probe that ignores
    its budget is a probe that can drop someone's audio."""

    def test_refuses_work_it_cannot_finish(self) -> None:
        b = Budget(0.5)
        self.assertFalse(b.ok(3.0))

    def test_paces_packets(self) -> None:
        b = Budget(30)
        t0 = time.monotonic()
        for _ in range(4):
            b.spend(1)
        self.assertGreater(time.monotonic() - t0, 0.2,
                           "four packets must not leave back to back")
        self.assertEqual(b.packets, 4)

    def test_deadline_counts_down(self) -> None:
        b = Budget(2.0)
        self.assertLessEqual(b.left(), 2.0)
        self.assertGreater(b.left(), 1.0)


if __name__ == "__main__":
    unittest.main()


class TestHeadUnitAccess(unittest.TestCase):
    """The temporary access mode.

    The interesting failure is not "it did not work" -- that is visible and
    harmless. It is "it worked and then did not clean up", which silently
    leaves this box reachable from a car network. So the teardown path is what
    gets the tests.
    """

    def setUp(self) -> None:
        import headunit_probe as hp

        self.hp = hp
        self.calls: list[list[str]] = []
        self.state = {"addr": False, "rule": False}
        self._real_run = hp.run

        def fake_run(cmd: list[str], timeout: float) -> tuple[int, str]:
            self.calls.append(cmd)
            joined = " ".join(cmd)
            if cmd[:3] == ["ip", "-4", "-br"]:
                return 0, "cara UP 172.16.224.231/24" if self.state["addr"] else "cara UP"
            if joined.startswith("ip addr add"):
                self.state["addr"] = True
                return 0, ""
            if joined.startswith("ip addr del"):
                self.state["addr"] = False
                return 0, ""
            if joined.startswith("nft add rule"):
                self.state["rule"] = True
                return 0, ""
            if joined.startswith("nft -a list chain"):
                return 0, ('  iifname "cara" ct state established,related accept '
                           'comment "carplay-wan-probe-temp" # handle 42'
                           if self.state["rule"] else "")
            if joined.startswith("nft delete rule"):
                self.state["rule"] = False
                return 0, ""
            if joined.startswith("nft list chain"):
                return 0, "carplay-wan-probe-temp" if self.state["rule"] else ""
            return 0, ""

        hp.run = fake_run

    def tearDown(self) -> None:
        self.hp.run = self._real_run

    def test_sets_up_and_tears_down(self) -> None:
        with self.hp.HeadUnitAccess("cara", "172.16.224.231/24") as a:
            self.assertTrue(a.usable)
            self.assertEqual(a.rule_handle, "42")
            self.assertTrue(self.state["addr"] and self.state["rule"])
        self.assertFalse(self.state["addr"], "address must be removed")
        self.assertFalse(self.state["rule"], "firewall rule must be removed")
        self.assertEqual(a.problems, [])

    def test_the_address_installs_no_route(self) -> None:
        """noprefixroute is load-bearing: the house uses this same /24, so an
        ordinary address would install a route that hijacks real traffic."""
        with self.hp.HeadUnitAccess("cara", "172.16.224.231/24"):
            pass
        add = next(c for c in self.calls if " ".join(c).startswith("ip addr add"))
        self.assertIn("noprefixroute", add)

    def test_the_rule_is_stateful_only(self) -> None:
        """Replies to what the box started -- never a way in for the car."""
        with self.hp.HeadUnitAccess("cara", "172.16.224.231/24"):
            pass
        rule = next(c for c in self.calls if " ".join(c).startswith("nft add rule"))
        self.assertIn("established,related", rule)
        self.assertIn("accept", rule)

    def test_teardown_happens_even_when_the_probe_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.hp.HeadUnitAccess("cara", "172.16.224.231/24"):
                raise RuntimeError("a probe blew up")
        self.assertFalse(self.state["addr"])
        self.assertFalse(self.state["rule"])

    def test_refuses_an_address_it_did_not_add(self) -> None:
        self.state["addr"] = True
        with self.hp.HeadUnitAccess("cara", "172.16.224.231/24") as a:
            self.assertFalse(a.usable)
        self.assertTrue(any("refusing to manage" in p for p in a.problems))
        self.assertTrue(self.state["addr"], "someone else's address is left alone")

    def test_a_failed_teardown_is_reported_loudly(self) -> None:
        a = self.hp.HeadUnitAccess("cara", "172.16.224.231/24")
        a.__enter__()
        # Simulate the delete silently failing.
        self.hp.run = lambda cmd, timeout: (
            (0, "carplay-wan-probe-temp") if cmd[:3] == ["nft", "list", "chain"]
            else (0, "cara UP 172.16.224.231/24") if cmd[:3] == ["ip", "-4", "-br"]
            else (1, "nope")
        )
        a.__exit__()
        self.assertTrue(any("TEARDOWN FAILED" in p for p in a.problems))
        self.assertEqual(len([p for p in a.problems if "TEARDOWN FAILED" in p]), 2,
                         "both the rule and the address must be reported")

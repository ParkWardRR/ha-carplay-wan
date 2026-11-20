#!/usr/bin/env python3
"""headunit_probe: ask a car's head unit everything it will answer, once.

This is the Q2 experiment from the README, as a suite rather than one script.
It runs while a car is awake and CarPlay is up, because that is the only time
the AP exists -- there is no park-it-and-scan option.

Everything here obeys four rules, and they are not negotiable:

  * Read-only. It asks. It never writes, pairs, or authenticates.
  * Your own car.
  * Cheap. The head unit relays every byte of ours twice over the air and
    shares that channel with CarPlay's own audio and screen. Upstream measured
    that saturating it drops CarPlay entirely and needs a hard reset to
    re-pair, so every active probe is paced, budgeted, and preceded by a
    latency check that aborts the rest if the link is already struggling.
  * Bounded. The car may be switched off mid-run. Nothing may hang.

Output is one JSON report per run. It contains identifiers -- BSSIDs, possibly
the SSID and a serial number -- so it is written 0600 and the summary says so.

    headunit_probe.py A --json /var/lib/carplay-wan/probe
    headunit_probe.py A --dry-run          # what it would do, no packets
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

VERSION = "0.2.0"

# Rates chosen against the measured ceiling, not guessed: the head unit's relay
# collapses around 3.8 Mbit/s and CarPlay already takes ~26% of the channel.
# None of these probes should ever be a measurable fraction of that.
MAX_PACKETS_PER_SEC = 8.0
DEFAULT_BUDGET = 75.0

# If the round trip to the head unit is worse than this, something is already
# wrong and piling probes on top is the one thing guaranteed to make it worse.
RTT_ABORT_MS = 400.0

# The WPS attributes worth having. A beacon carries almost none of these -- the
# rich ones only come back in a probe response to a probe request that itself
# carries a WPS IE, which is why probe_wps exists.
WPS_ATTRS = {
    0x1011: "device_name",
    0x1021: "manufacturer",
    0x1023: "model_name",
    0x1024: "model_number",
    0x1042: "serial_number",
    0x1044: "wps_state",
    0x1047: "uuid_e",
    0x1049: "vendor_extension",
    0x104A: "version",
    0x103C: "rf_bands",
    0x1008: "config_methods",
    0x1054: "primary_device_type",
    0x1057: "ap_setup_locked",
}
WPS_STATE = {1: "not configured", 2: "configured"}

# A WPS IE for our own probe request, marking us an enrollee asking for device
# info. This is the same thing any WPS-capable phone emits while scanning.
# The length byte must equal the payload that follows it. The first draft
# declared 0x1d over a 20-byte payload and `iw` rejected the whole scan with
# EINVAL -- which is the correct behaviour, and cost a live session to learn.
WPS_PROBE_IE = (
    "dd14"  # vendor IE, 20 bytes follow
    "0050f204"  # WPS OUI + type
    "104a000110"  # Version 1.0
    "103a000100"  # Request Type: enrollee, info only
    "10080002318c"  # Config Methods
)
assert len(WPS_PROBE_IE) // 2 - 2 == int(WPS_PROBE_IE[2:4], 16), "WPS IE length byte"

OUI_NAMES = {
    "0050f2": "Microsoft/WPS-WMM",
    "00904c": "Broadcom",
    "0017f2": "Apple",
    "00a040": "Apple (device IE)",
    "506f9a": "Wi-Fi Alliance",
    "8cfdf0": "Qualcomm",
    "0010e0": "Harman",
}


# --------------------------------------------------------------------------
# 802.11 information elements
# --------------------------------------------------------------------------


def parse_ies(blob: bytes) -> list[tuple[int, bytes]]:
    """Walk a concatenated IE blob into (element id, body) pairs.

    Truncated trailers are dropped rather than raising: these bytes come off
    the air and a short last element is a damaged frame, not a bug here.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    while i + 2 <= len(blob):
        eid, ln = blob[i], blob[i + 1]
        body = blob[i + 2 : i + 2 + ln]
        if len(body) < ln:
            break
        out.append((eid, body))
        i += 2 + ln
    return out


def decode_wps(body: bytes) -> dict[str, Any]:
    """WPS attributes are big-endian type/length/value, not 802.11 IEs."""
    out: dict[str, Any] = {}
    i = 0
    while i + 4 <= len(body):
        attr, ln = struct.unpack_from(">HH", body, i)
        val = body[i + 4 : i + 4 + ln]
        if len(val) < ln:
            break
        name = WPS_ATTRS.get(attr)
        if name is None:
            out.setdefault("unknown", {})[f"0x{attr:04x}"] = val.hex()
        elif name in ("device_name", "manufacturer", "model_name",
                      "model_number", "serial_number"):
            out[name] = val.decode("utf-8", "replace").strip("\x00").strip()
        elif name == "wps_state" and ln == 1:
            out[name] = WPS_STATE.get(val[0], val[0])
        elif name == "uuid_e":
            out[name] = val.hex()
        elif name == "primary_device_type" and ln == 8:
            cat, oui, sub = struct.unpack(">H4sH", val)
            out[name] = {"category": cat, "oui": oui.hex(), "subcategory": sub}
        elif ln <= 8:
            out[name] = int.from_bytes(val, "big")
        else:
            out[name] = val.hex()
        i += 4 + ln
    return out


def decode_interworking(body: bytes) -> dict[str, Any]:
    """802.11u. The Internet bit is the one that matters to this project: it
    is how an AP says "there is internet behind me", and these head units
    leave it clear, which is the whole reason the box exists."""
    if not body:
        return {}
    ant = body[0] & 0x0F
    return {
        "access_network_type": ant,
        "internet": bool(body[0] & 0x10),
        "asra": bool(body[0] & 0x20),
        "esr": bool(body[0] & 0x40),
        "uesa": bool(body[0] & 0x80),
    }


def summarise_ies(blob: bytes) -> dict[str, Any]:
    """Everything an IE blob is willing to say, in one dict."""
    out: dict[str, Any] = {"vendor": [], "elements": []}
    for eid, body in parse_ies(blob):
        out["elements"].append(eid)
        if eid == 0:
            out["ssid"] = body.decode("utf-8", "replace")
        elif eid == 3 and body:
            out["channel"] = body[0]
        elif eid == 7 and len(body) >= 3:
            out["country"] = body[:2].decode("ascii", "replace")
        elif eid == 107:
            out["interworking"] = decode_interworking(body)
        elif eid == 48:
            out["rsn"] = body.hex()
        elif eid == 221 and len(body) >= 4:
            oui, otype = body[:3].hex(), body[3]
            entry: dict[str, Any] = {
                "oui": oui,
                "type": otype,
                "vendor": OUI_NAMES.get(oui, "unknown"),
                "len": len(body) - 4,
            }
            if oui == "0050f2" and otype == 4:
                entry["wps"] = decode_wps(body[4:])
                out["wps"] = entry["wps"]
            elif oui in ("0017f2", "00a040"):
                # Apple elements. Upstream found neither on these head units;
                # recorded anyway, because "still absent" is a result.
                entry["data"] = body[4:].hex()
                out.setdefault("apple_elements", []).append(entry)
            out["vendor"].append(entry)
    return out


# --------------------------------------------------------------------------
# running things, carefully
# --------------------------------------------------------------------------


class Budget:
    """A deadline and a packet pacer in one object.

    Every active probe asks `ok()` before it does anything and `spend()` after.
    A probe that would overrun simply does not start, and says so in the
    report -- a half-finished probe is worse than an absent one, because it
    looks like an answer.
    """

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.packets = 0
        self._last = 0.0

    def left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def ok(self, need: float = 1.0) -> bool:
        return self.left() > need

    def spend(self, packets: int = 1) -> None:
        self.packets += packets
        gap = packets / MAX_PACKETS_PER_SEC
        now = time.monotonic()
        wait = self._last + gap - now
        if wait > 0:
            time.sleep(wait)
        self._last = max(now, self._last + gap)


def run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(0.5, timeout), check=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"{type(exc).__name__}: {exc}"


@dataclass
class Ctx:
    car: str
    iface: str
    sibling: str | None
    ctrl: str
    gw4: str
    budget: Budget
    dry_run: bool = False
    # Set when there is no association to learn the BSSID from -- the
    # post-shutdown window, where the AP is still beaconing into the
    # supplicant's scan cache but nothing is connected to it.
    bssid_hint: str | None = None
    # Set only in --head-unit mode: the temporary source address that makes
    # the head unit reachable. None means the IPv4 probes stay skipped.
    v4_source: str | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

PROBES: list[tuple[str, str, Callable[[Ctx], dict[str, Any]]]] = []


def probe(name: str, kind: str) -> Callable[..., Any]:
    def wrap(fn: Callable[[Ctx], dict[str, Any]]) -> Callable[[Ctx], dict[str, Any]]:
        PROBES.append((name, kind, fn))
        return fn

    return wrap


@probe("link", "passive")
def p_link(c: Ctx) -> dict[str, Any]:
    """What the driver already knows about the association. Free."""
    rc, out = run(["/usr/sbin/iw", "dev", c.iface, "link"], 5)
    if rc != 0:
        return {"error": out.strip()[:200]}
    d: dict[str, Any] = {}
    for key, pat in (
        ("bssid", r"Connected to ([0-9a-f:]{17})"),
        ("ssid", r"SSID: (.+)"),
        ("freq", r"freq: (\d+)"),
        ("signal_dbm", r"signal: (-?\d+)"),
        ("tx_bitrate", r"tx bitrate: ([\d.]+)"),
        ("rx_bitrate", r"rx bitrate: ([\d.]+)"),
    ):
        m = re.search(pat, out)
        if m:
            d[key] = m.group(1)
    return d


@probe("beacon_ies", "passive")
def p_beacon_ies(c: Ctx) -> dict[str, Any]:
    """The full raw information elements, via the supplicant's own cache.

    `iw scan dump` renders a handful of WPS fields and hides the rest; the
    supplicant hands over the whole blob, so every vendor element can be
    decoded here instead of trusted to someone else's pretty-printer.
    """
    bssid = p_link(c).get("bssid") or c.bssid_hint
    if not bssid:
        return {"skipped": "not associated and no BSSID known"}
    rc, out = run(["/usr/sbin/wpa_cli", "-p", c.ctrl, "-i", c.iface, "bss", bssid], 8)
    if rc != 0:
        return {"error": out.strip()[:200]}
    fields = dict(re.findall(r"^(\w+)=(.*)$", out, re.M))
    res: dict[str, Any] = {k: fields.get(k) for k in
                           ("bssid", "freq", "beacon_int", "capabilities", "level")}
    for which in ("ie", "beacon_ie"):
        hexed = fields.get(which)
        if hexed:
            try:
                res[which] = summarise_ies(bytes.fromhex(hexed))
            except ValueError:
                res[which] = {"error": "unparsable hex"}
    return res


@probe("probe_wps", "active")
def p_probe_wps(c: Ctx) -> dict[str, Any]:
    """Ask the AP directly, with a WPS-bearing probe request.

    A beacon's WPS element is nearly empty. The manufacturer, model name,
    model number, serial number and device name only come back in a probe
    response to a request that advertises WPS -- which is what every
    WPS-capable phone does while scanning, so this is ordinary discovery
    rather than anything exotic.

    It runs on the *sibling* radio when there is one. That radio is already
    hunting this channel, so the probe costs airtime that was being spent
    anyway, and the serving radio's association is never disturbed.
    """
    dev = c.sibling or c.iface
    on_serving = dev == c.iface
    ssid = p_link(c).get("ssid")
    if not ssid:
        return {"skipped": "not associated, no SSID to ask for"}
    if on_serving:
        # Scanning on the serving radio means leaving the link briefly. Not
        # worth a model string.
        return {"skipped": "no idle sibling radio; refusing to scan on the "
                           "radio that is serving the car"}
    if c.dry_run:
        return {"would_run": f"iw dev {dev} scan ssid {ssid!r} ies {WPS_PROBE_IE}"}
    c.budget.spend(2)
    rc, out = run(["/usr/sbin/iw", "dev", dev, "scan", "freq", "2462",
                   "ssid", ssid, "ies", WPS_PROBE_IE], min(20.0, c.budget.left()))
    if rc != 0:
        return {"error": out.strip()[:200]}
    # Pull our car's BSS back out of the results and re-read its IEs, this
    # time from a probe response rather than a beacon.
    block, keep = [], False
    for line in out.splitlines():
        if line.startswith("BSS "):
            keep, block = False, []
        block.append(line)
        if line.strip() == f"SSID: {ssid}":
            keep = True
    found: dict[str, Any] = {"scanned_on": dev}
    if keep:
        found["raw_excerpt"] = "\n".join(block[:40])
    # The decoded answer comes from the supplicant's cache, which the scan has
    # just refreshed with a probe response rather than a beacon.
    found["ies"] = p_beacon_ies(c)
    return found


@probe("station", "passive")
def p_station(c: Ctx) -> dict[str, Any]:
    rc, out = run(["/usr/sbin/iw", "dev", c.iface, "station", "dump"], 6)
    if rc != 0:
        return {"error": out.strip()[:200]}
    d = dict(re.findall(r"^\s*([a-z ]+):\s+(.+)$", out, re.M))
    return {k.strip().replace(" ", "_"): v.strip() for k, v in d.items()}


@probe("neighbours", "passive")
def p_neighbours(c: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fam in ("-6", "-4"):
        rc, text = run(["ip", fam, "neigh", "show", "dev", c.iface], 5)
        out["v6" if fam == "-6" else "v4"] = text.strip().splitlines()[:20] if rc == 0 else []
    return out


@probe("reachability", "active")
def p_reachability(c: Ctx) -> dict[str, Any]:
    """Who on this link will answer, and how fast.

    The first version pinged the IPv4 gateway and declared the link dead when
    nothing came back. It could never have worked: carplayd runs the car link
    IPv6-only on purpose, so the box has no IPv4 address and no route, and the
    probe was measuring its own absence. Worse, it treated that as "the link is
    struggling" and cancelled every probe after it.

    Now it asks three different questions and only the *answers* can fail it.
    """
    if c.dry_run:
        return {"would_run": "ping6 head unit link-local, ping6 phone"}
    out: dict[str, Any] = {"targets": {}}
    link = p_link(c)
    targets: list[tuple[str, str]] = []
    hu_ll = eui64_ll(link.get("bssid", ""))
    if hu_ll:
        targets.append(("head_unit_v6", f"{hu_ll}%{c.iface}"))
    for addr in neighbour_v6(c.iface)[:2]:
        targets.append(("phone_v6", f"{addr}%{c.iface}" if addr.startswith("fe80") else addr))

    for label, addr in targets:
        if not c.budget.ok(4):
            break
        c.budget.spend(2)
        rc, text = run(["ping", "-6", "-c", "2", "-i", "0.3", "-W", "1", addr], 6)
        m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", text)
        entry: dict[str, Any] = {"address": addr.split("%")[0], "answered": rc == 0}
        if m:
            entry["rtt_avg_ms"] = float(m.group(2))
        out["targets"][label] = entry

    if c.v4_source:
        c.budget.spend(2)
        rc, text = run(["ping", "-c", "2", "-i", "0.3", "-W", "1",
                        "-I", c.iface, c.gw4], 6)
        m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", text)
        entry: dict[str, Any] = {"address": c.gw4, "answered": rc == 0}
        if m:
            entry["rtt_avg_ms"] = float(m.group(2))
        out["targets"]["head_unit_v4"] = entry

    answered = [t for t in out["targets"].values() if t.get("answered")]
    rtts = [t["rtt_avg_ms"] for t in answered if "rtt_avg_ms" in t]
    out["any_answer"] = bool(answered)
    if rtts:
        out["rtt_avg_ms"] = max(rtts)
    # Nothing answering is not evidence of a struggling link -- plenty of
    # devices simply do not reply to ICMP. Only a measured, bad round trip
    # stops the rest of the suite.
    out["verdict"] = "degraded" if rtts and max(rtts) > RTT_ABORT_MS else "ok"
    return out


@probe("dhcp", "active")
def p_dhcp(c: Ctx) -> dict[str, Any]:
    """One DHCPDISCOVER, read the OFFER's options, never REQUEST.

    Upstream already knows the subnet has no router and no DNS. What has never
    been read is the rest of the option set -- vendor class (60), vendor
    specific (43), hostname (12), domain (15) -- any of which may name the
    head unit.
    """
    if c.dry_run:
        return {"would_run": "DHCPDISCOVER on " + c.iface}
    if not c.budget.ok(5):
        return {"skipped": "out of budget"}
    try:
        mac = bytes.fromhex(
            open(f"/sys/class/net/{c.iface}/address").read().strip().replace(":", "")
        )
    except OSError as exc:
        return {"error": str(exc)}
    xid = os.urandom(4)
    pkt = b"\x01\x01\x06\x00" + xid + b"\x00\x00\x00\x00" + b"\x00" * 16
    pkt += mac + b"\x00" * 10 + b"\x00" * 192
    pkt += b"\x63\x82\x53\x63"  # magic cookie
    pkt += b"\x35\x01\x01"  # DHCPDISCOVER
    pkt += b"\x37\x08\x01\x03\x06\x0c\x0f\x1c\x2b\x3c"  # parameter request list
    pkt += b"\xff"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, 25, c.iface.encode())  # SO_BINDTODEVICE
        s.bind(("0.0.0.0", 68))
        s.settimeout(min(4.0, c.budget.left()))
        c.budget.spend(1)
        s.sendto(pkt, ("255.255.255.255", 67))
        data, _ = s.recvfrom(2048)
    except (OSError, socket.timeout) as exc:
        return {"offer": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        s.close()
    if len(data) < 240 or data[4:8] != xid:
        return {"offer": False, "note": "reply did not match our transaction"}
    opts: dict[str, Any] = {}
    i = 240
    while i + 2 <= len(data) and data[i] != 0xFF:
        code, ln = data[i], data[i + 1]
        val = data[i + 2 : i + 2 + ln]
        if code in (1, 3, 6, 28, 54):
            opts[str(code)] = ".".join(str(b) for b in val[:4]) if ln >= 4 else val.hex()
        elif code in (12, 15, 60):
            opts[str(code)] = val.decode("utf-8", "replace")
        elif code == 43:
            opts["43"] = val.hex()
        i += 2 + ln
    return {
        "offer": True,
        "yiaddr": ".".join(str(b) for b in data[16:20]),
        "options": opts,
        "named": {"hostname": opts.get("12"), "domain": opts.get("15"),
                  "vendor_class": opts.get("60"), "router": opts.get("3"),
                  "dns": opts.get("6")},
    }


@probe("mdns", "passive")
def p_mdns(c: Ctx) -> dict[str, Any]:
    """Listen for Bonjour on the car link. Sends nothing.

    Two things forced this shape. avahi-daemon on the box only watches the
    wired interface, so `avahi-browse` never saw this link at all -- it was
    reporting the house. And the head unit is IPv4-only here while the box is
    IPv6-only, so no ordinary socket can join the IPv4 multicast group.

    A raw capture sidesteps both: it reads the frames the radio is receiving
    anyway, from either address family, with nothing transmitted. CarPlay's
    own service discovery is Bonjour, so there is traffic here to hear.
    """
    seconds = min(12.0, max(4.0, c.budget.left() / 4))
    if c.dry_run:
        return {"would_run": f"passive 5353 capture on {c.iface} for {seconds:.0f}s"}
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        s.bind((c.iface, 0))
        s.settimeout(1.0)
    except (OSError, AttributeError) as exc:
        return {"error": f"raw capture unavailable: {exc}"}
    records: list[dict[str, Any]] = []
    seen_src: set[str] = set()
    frames = 0
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            try:
                frame = s.recv(4096)
            except socket.timeout:
                continue
            frames += 1
            # Ethernet II; walk to UDP and look for port 5353 either side.
            if len(frame) < 42:
                continue
            etype = struct.unpack_from(">H", frame, 12)[0]
            if etype == 0x0800 and frame[23] == 17:
                ihl = (frame[14] & 0x0F) * 4
                udp = 14 + ihl
                src = ".".join(str(b) for b in frame[26:30])
            elif etype == 0x86DD and frame[20] == 17:
                udp = 54
                src = socket.inet_ntop(socket.AF_INET6, frame[22:38])
            else:
                continue
            if udp + 8 > len(frame):
                continue
            sport, dport = struct.unpack_from(">HH", frame, udp)
            if 5353 not in (sport, dport):
                continue
            seen_src.add(src)
            for rec in parse_mdns(frame[udp + 8 :]):
                if rec not in records:
                    records.append(rec)
    finally:
        s.close()
    txt_all: dict[str, str] = {}
    for r in records:
        if r["type"] == "TXT":
            txt_all.update(r["txt"])
    services = sorted({r["target"] for r in records if r["type"] == "PTR"})
    return {
        "listened_s": round(seconds, 1),
        "frames_seen": frames,
        "speakers": sorted(seen_src),
        "count": len(records),
        "services": services[:40],
        "records": records[:40],
        "interesting": {k: v for k, v in txt_all.items() if k.lower() in
                        ("model", "manufacturer", "srcvers", "deviceid", "fv",
                         "osvers", "vv", "features", "protovers", "rsf", "pi")},
    }


@probe("tcp_ports", "active")
def p_tcp(c: Ctx) -> dict[str, Any]:
    """One connect per port, paced, short list.

    Deliberately not a sweep. The list is what a CarPlay/AirPlay head unit
    plausibly runs plus the only comparable published scan of an aftermarket
    unit; widening it buys little and spends the one resource CarPlay needs.
    """
    ports = [22, 80, 443, 554, 3689, 5000, 7000, 7100, 8008, 8080, 49152]
    if c.dry_run:
        return {"would_run": f"connect {c.gw4}:{ports}"}
    if not c.v4_source:
        # Not a failure, a property of the design: carplayd runs the car link
        # IPv6-only, so there is no source address to reach the head unit's
        # IPv4 from, and carplayd drops the replies anyway. --head-unit opens
        # both, briefly. Without it this stays skipped on purpose.
        return {"skipped": "needs --head-unit (temporary address + stateful "
                           "return path); the link is IPv6-only by design",
                "head_unit_v4": c.gw4}
    open_ports, checked = [], []
    for port in ports:
        if not c.budget.ok(4):
            break
        c.budget.spend(1)
        checked.append(port)
        s = socket.socket()
        s.settimeout(1.2)
        try:
            s.setsockopt(socket.SOL_SOCKET, 25, c.iface.encode())
            s.bind((c.v4_source, 0))
            if s.connect_ex((c.gw4, port)) == 0:
                open_ports.append(port)
        except OSError:
            pass
        finally:
            s.close()
    return {"checked": checked, "open": open_ports, "source": c.v4_source}


@probe("http_info", "active")
def p_http(c: Ctx) -> dict[str, Any]:
    """AirPlay receivers answer GET /info with a plist naming the device.

    Only runs against ports tcp_ports actually found open, so a head unit that
    exposes nothing costs nothing.
    """
    if c.dry_run:
        return {"would_run": "GET /info on any open AirPlay-ish port"}
    if not c.v4_source:
        return {"skipped": "needs --head-unit; see tcp_ports"}
    found: dict[str, Any] = {}
    for port in (7000, 7100, 5000, 8080, 80):
        if not c.budget.ok(5):
            break
        c.budget.spend(1)
        try:
            with socket.create_connection((c.gw4, port), timeout=1.5,
                                          source_address=(c.v4_source, 0)) as s:
                s.sendall(
                    f"GET /info HTTP/1.1\r\nHost: {c.gw4}\r\n"
                    "User-Agent: AirPlay/420.45\r\nConnection: close\r\n\r\n".encode()
                )
                s.settimeout(2.0)
                data = s.recv(4096)
        except OSError:
            continue
        if data:
            head = data[:400].decode("utf-8", "replace")
            found[str(port)] = {
                "status": head.splitlines()[0] if head else "",
                "excerpt": head[:300],
            }
    return found or {"note": "nothing answered /info"}


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def _dns_name(buf: bytes, off: int, depth: int = 0) -> tuple[str, int]:
    """Decode a DNS name, following compression pointers."""
    out: list[str] = []
    while off < len(buf) and depth < 12:
        ln = buf[off]
        if ln == 0:
            return ".".join(out), off + 1
        if ln & 0xC0 == 0xC0:
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            tail, _ = _dns_name(buf, ptr, depth + 1)
            out.append(tail)
            return ".".join(out), off + 2
        out.append(buf[off + 1 : off + 1 + ln].decode("utf-8", "replace"))
        off += 1 + ln
    return ".".join(out), off


def parse_mdns(payload: bytes) -> list[dict[str, Any]]:
    """Pull the answer records out of an mDNS message.

    Only the record types that say something about a device: PTR names the
    service, SRV gives the host and port, TXT is where AirPlay responders put
    model and manufacturer.
    """
    recs: list[dict[str, Any]] = []
    if len(payload) < 12:
        return recs
    qd, an, ns, ar = struct.unpack_from(">HHHH", payload, 4)
    off = 12
    for _ in range(qd):
        _, off = _dns_name(payload, off)
        off += 4
    for _ in range(an + ns + ar):
        if off >= len(payload):
            break
        name, off = _dns_name(payload, off)
        if off + 10 > len(payload):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack_from(">HHIH", payload, off)
        off += 10
        rdata = payload[off : off + rdlen]
        off += rdlen
        if rtype == 12:  # PTR
            target, _ = _dns_name(payload, off - rdlen)
            recs.append({"type": "PTR", "name": name, "target": target})
        elif rtype == 33 and rdlen >= 6:  # SRV
            port = struct.unpack_from(">H", rdata, 4)[0]
            host, _ = _dns_name(payload, off - rdlen + 6)
            recs.append({"type": "SRV", "name": name, "port": port, "host": host})
        elif rtype == 16:  # TXT
            txt: dict[str, str] = {}
            i = 0
            while i < len(rdata):
                ln = rdata[i]
                item = rdata[i + 1 : i + 1 + ln].decode("utf-8", "replace")
                k, _, v = item.partition("=")
                if k:
                    txt[k] = v
                i += 1 + ln
            recs.append({"type": "TXT", "name": name, "txt": txt})
    return recs


def has_v4(iface: str) -> bool:
    rc, out = run(["ip", "-4", "-br", "addr", "show", iface], 4)
    return rc == 0 and bool(re.search(r"\d+\.\d+\.\d+\.\d+", out))


def eui64_ll(mac: str) -> str | None:
    """The link-local address a MAC would have under EUI-64.

    The head unit never sends a router advertisement and never appears in the
    neighbour cache with an IPv6 address, so if it has IPv6 at all this is
    where it would be. Worth one packet to find out: an answer would be a new
    fact about these head units, and silence is also a fact.
    """
    try:
        b = [int(x, 16) for x in mac.split(":")]
    except (ValueError, AttributeError):
        return None
    if len(b) != 6:
        return None
    b[0] ^= 0x02
    return "fe80::%02x%02x:%02xff:fe%02x:%02x%02x" % (b[0], b[1], b[2], b[3], b[4], b[5])


def neighbour_v6(iface: str) -> list[str]:
    """IPv6 neighbours we have actually heard from -- in practice, the phone."""
    rc, out = run(["ip", "-6", "neigh", "show", "dev", iface], 5)
    if rc != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if parts and ("REACHABLE" in line or "STALE" in line):
            found.append(parts[0])
    return found


class HeadUnitAccess:
    """Make the head unit reachable from the box, briefly and reversibly.

    Three things stand between this box and the head unit, and all three are
    ours rather than the car's:

      * the car link is IPv6-only by design, so there is no IPv4 source
        address to speak from;
      * the house uses the same 172.16.224.0/24, so the main routing table
        sends anything bound for the head unit out of the wired interface
        instead -- which is why `noprefixroute` is not optional here. Adding
        the address the ordinary way would install a /24 route that hijacks
        every packet the box sends to that range, including real traffic to
        the house;
      * carplayd drops everything arriving from the car link, which is the
        isolation that keeps a car from reaching the box. The head unit was
        answering pings all along; the replies were being discarded here.

    So this opens the narrowest thing that works: one address that installs no
    route, and a stateful rule that admits replies to conversations the box
    itself started. The car link still cannot initiate anything inbound.

    Teardown is the part that matters. Leaving either change behind would
    weaken the isolation silently, so removal runs in a finally, is verified
    afterwards, and complains loudly if verification fails.
    """

    COMMENT = "carplay-wan-probe-temp"

    def __init__(self, iface: str, addr: str, dry_run: bool = False) -> None:
        self.iface = iface
        self.addr = addr
        self.dry_run = dry_run
        self.added_addr = False
        self.rule_handle: str | None = None
        self.problems: list[str] = []

    def _addr_present(self) -> bool:
        rc, out = run(["ip", "-4", "-br", "addr", "show", self.iface], 4)
        return rc == 0 and self.addr.split("/")[0] in out

    def __enter__(self) -> HeadUnitAccess:
        if self.dry_run:
            return self
        if self._addr_present():
            # Someone else's address, or a previous run that did not clean up.
            # Either way, not ours to remove later.
            self.problems.append(f"{self.addr} already on {self.iface}; "
                                 "refusing to manage an address we did not add")
            return self
        rc, out = run(["ip", "addr", "add", self.addr, "dev", self.iface,
                       "noprefixroute"], 6)
        if rc != 0:
            self.problems.append(f"could not add address: {out.strip()[:120]}")
            return self
        self.added_addr = True

        rc, out = run(["nft", "add", "rule", "inet", "carplayd", "input",
                       "iifname", self.iface, "ct", "state",
                       "established,related", "accept",
                       "comment", f'"{self.COMMENT}"'], 6)
        if rc != 0:
            self.problems.append(f"could not add return-path rule: {out.strip()[:120]}")
            return self
        rc, out = run(["nft", "-a", "list", "chain", "inet", "carplayd", "input"], 6)
        for line in out.splitlines():
            if self.COMMENT in line and "handle" in line:
                self.rule_handle = line.rsplit("handle", 1)[1].strip()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.dry_run:
            return
        if self.rule_handle:
            run(["nft", "delete", "rule", "inet", "carplayd", "input",
                 "handle", self.rule_handle], 6)
        if self.added_addr:
            run(["ip", "addr", "del", self.addr, "dev", self.iface], 6)

        # Verify, do not assume. carplayd rebuilds this table on a radio
        # recovery, so the rule may already be gone -- that is fine. What is
        # not fine is it still being there.
        rc, out = run(["nft", "list", "chain", "inet", "carplayd", "input"], 6)
        if rc == 0 and self.COMMENT in out:
            self.problems.append("TEARDOWN FAILED: the temporary firewall rule "
                                 "is still present. Remove it by hand.")
        if self._addr_present():
            self.problems.append(f"TEARDOWN FAILED: {self.addr} is still on "
                                 f"{self.iface}. Remove it by hand.")

    @property
    def usable(self) -> bool:
        return self.added_addr and self.rule_handle is not None


def iface_up(iface: str) -> bool:
    try:
        return open(f"/sys/class/net/{iface}/operstate").read().strip() == "up"
    except OSError:
        return False


def bssid_from_scan(c: Ctx, ssid: str | None) -> str | None:
    """Find the car's BSSID in the supplicant's scan cache.

    Used after a car is switched off: the association is gone, but the head
    unit keeps beaconing for a while and those beacons are still in the cache.
    Reads only; no scan is triggered.
    """
    rc, out = run(["/usr/sbin/wpa_cli", "-p", c.ctrl, "-i", c.iface,
                   "scan_results"], 8)
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0].count(":") == 5:
            if ssid is None or parts[4].strip() == ssid:
                if "CarPlay" in parts[4] or ssid:
                    return parts[0]
    return None


def wait_for_link(c: Ctx, limit: float) -> float | None:
    """Block until the radio is associated, or give up.

    Purely passive: it reads `iw link` once a second and sends nothing. The
    cost of waiting is a few seconds of a session; the cost of not waiting is
    the entire run, because every interesting probe needs the link.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if iface_up(c.iface) and p_link(c).get("bssid"):
            return round(time.monotonic() - t0, 1)
        time.sleep(1.0)
    return None


def self_test(ctrl: str, iface: str) -> int:
    """Decode whatever beacons the supplicant has already heard.

    Purely passive: these frames were received anyway. It exists because the
    real target is available for seconds at a time, months apart, and a
    parser that has never been run against a real beacon is a parser that
    will fail on the one that matters.
    """
    rc, listing = run(["/usr/sbin/wpa_cli", "-p", ctrl, "-i", iface,
                       "scan_results"], 8)
    if rc != 0:
        print(json.dumps({"error": listing.strip()[:200]}, indent=1))
        return 1
    seen = 0
    for idx in range(20):
        rc, out = run(["/usr/sbin/wpa_cli", "-p", ctrl, "-i", iface,
                       "bss", str(idx)], 6)
        if rc != 0 or "bssid=" not in out:
            continue
        fields = dict(re.findall(r"^(\w+)=(.*)$", out, re.M))
        blob = fields.get("beacon_ie") or fields.get("ie")
        if not blob:
            continue
        try:
            summary = summarise_ies(bytes.fromhex(blob))
        except ValueError:
            continue
        seen += 1
        vendors = sorted({v["vendor"] for v in summary.get("vendor", [])})
        print(f"  bss {idx:<2} ch {summary.get('channel', '?'):<3} "
              f"country={summary.get('country', '--')} "
              f"elements={len(summary['elements']):<3} "
              f"vendor={','.join(vendors) or 'none'}"
              f"{'  WPS' if 'wps' in summary else ''}"
              f"{'  INTERWORKING' if 'interworking' in summary else ''}"
              f"{'  APPLE' if 'apple_elements' in summary else ''}")
    print(f"decoded {seen} cached beacons, sent nothing")
    return 0 if seen else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe a car's head unit, once.")
    ap.add_argument("car", help="carplayd car letter, e.g. A")
    ap.add_argument("--iface")
    ap.add_argument("--sibling", help="idle radio to send the WPS probe from")
    ap.add_argument("--ctrl", help="wpa_supplicant control dir")
    ap.add_argument("--gw", default="172.16.224.1")
    ap.add_argument("--ssid", help="SSID to look for in unassociated mode")
    ap.add_argument("--json", dest="outdir", default="/var/lib/carplay-wan/probe")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    ap.add_argument("--unassociated", action="store_true",
                    help="do not wait for a link. For the window after a car "
                         "is switched off, when the AP is still beaconing but "
                         "the association is gone")
    ap.add_argument("--tag", default="", help="label this run in the report")
    ap.add_argument("--head-unit", action="store_true",
                    help="briefly make the head unit reachable: a temporary "
                         "IPv4 address that installs no route, plus a rule "
                         "admitting replies to connections this box starts. "
                         "Both are removed on exit and the removal is "
                         "verified. Off by default: it relaxes the isolation "
                         "that keeps the car link away from the box")
    ap.add_argument("--head-unit-addr", default="172.16.224.231/24",
                    help="the temporary address to use")
    ap.add_argument("--assoc-wait", type=float, default=25.0,
                    help="seconds to wait for the radio to associate before "
                         "giving up (the probe is fired on first sighting, "
                         "which is earlier than that)")
    ap.add_argument("--only", help="comma-separated probe names")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would run, send nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="decode a beacon already in the supplicant's cache. "
                         "Sends nothing, needs no car, and proves the whole "
                         "wpa_cli -> hex -> decode path works before a car "
                         "ever turns up -- which matters, because the probe "
                         "only ever gets one shot at a live head unit")
    args = ap.parse_args(argv)

    car = args.car.upper()
    low = car.lower()
    iface = args.iface or f"car{low}"
    sibling = args.sibling or ("carb" if low == "a" else "cara")
    ctrl = args.ctrl or f"/var/run/carplayd/car-{low}"

    # The sibling is only useful if it actually exists and is not the radio
    # serving this car. Both branches of the obvious ternary are the same
    # value, which is how the first draft of this line shipped a no-op.
    have_sibling = sibling != iface and os.path.exists(f"/sys/class/net/{sibling}")
    ctx = Ctx(car=car, iface=iface, sibling=sibling if have_sibling else None,
              ctrl=ctrl, gw4=args.gw, budget=Budget(args.budget), dry_run=args.dry_run)

    if args.self_test:
        return self_test(ctrl, iface)

    report_tag = args.tag
    if args.unassociated and not args.dry_run:
        ctx.bssid_hint = bssid_from_scan(ctx, args.ssid)

    report: dict[str, Any] = {
        "version": VERSION,
        "tag": report_tag,
        "unassociated": args.unassociated,
        "car": car,
        "iface": iface,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": args.dry_run,
        "probes": {},
    }

    # The agent fires this on first sighting, which is before the radio has
    # associated -- the AP is seen in a scan seconds before there is a link.
    # Without this wait every probe would skip with "not associated" and the
    # one chance at a live head unit would be spent on nothing.
    if not args.dry_run and not args.unassociated:
        waited = wait_for_link(ctx, args.assoc_wait)
        report["waited_for_link_s"] = waited
        if waited is None:
            report["aborted"] = (
                f"{iface} never associated within {args.assoc_wait}s; "
                "the car was seen but no link came up"
            )
            print(json.dumps(report, indent=1))
            return 0

    wanted = set(args.only.split(",")) if args.only else None
    guard_failed = False
    access = HeadUnitAccess(iface, args.head_unit_addr, dry_run=args.dry_run) \
        if args.head_unit else None

    if access is not None:
        access.__enter__()
        if access.usable:
            ctx.v4_source = args.head_unit_addr.split("/")[0]
            report["head_unit_access"] = {"source": ctx.v4_source,
                                          "rule_handle": access.rule_handle}
        else:
            report["head_unit_access"] = {"failed": access.problems}

    for name, kind, fn in PROBES:
        if wanted and name not in wanted:
            continue
        if kind == "active" and guard_failed:
            report["probes"][name] = {"skipped": "link already struggling"}
            continue
        if kind == "active" and not ctx.budget.ok(3):
            report["probes"][name] = {"skipped": "out of time budget"}
            continue
        t0 = time.monotonic()
        try:
            result = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a probe must never kill the run
            result = {"error": f"{type(exc).__name__}: {exc}"}
        result["_ms"] = round((time.monotonic() - t0) * 1000)
        report["probes"][name] = result

        # The reachability probe is also the gate for everything after it.
        if name == "reachability" and not args.dry_run:
            if result.get("verdict") == "degraded":
                guard_failed = True
                ctx.notes.append(
                    f"stopped active probing: measured round trip "
                    f"{result.get('rtt_avg_ms')} ms is over {RTT_ABORT_MS} ms. "
                    "CarPlay shares this channel and comes first."
                )
            elif not result.get("any_answer"):
                ctx.notes.append(
                    "nothing answered ICMP on this link. Not treated as a "
                    "fault: devices are entitled to ignore pings, and the "
                    "remaining probes do not depend on it."
                )

    if access is not None:
        access.__exit__()
        report.setdefault("head_unit_access", {})["teardown_problems"] = access.problems
        for problem in access.problems:
            ctx.notes.append(problem)
            print(f"headunit_probe: {problem}", file=sys.stderr)

    report["packets_sent"] = ctx.budget.packets
    report["seconds_left"] = round(ctx.budget.left(), 1)
    report["notes"] = ctx.notes

    # What a human should read first.
    wps = (report["probes"].get("beacon_ies", {}).get("ie", {}) or {}).get("wps", {})
    report["summary"] = {
        "manufacturer": wps.get("manufacturer"),
        "model_name": wps.get("model_name"),
        "model_number": wps.get("model_number"),
        "serial_number": wps.get("serial_number"),
        "device_name": wps.get("device_name"),
        "mdns_services": report["probes"].get("mdns", {}).get("count"),
        "open_tcp": report["probes"].get("tcp_ports", {}).get("open"),
        "dhcp_vendor_class": (report["probes"].get("dhcp", {})
                              .get("named", {}) or {}).get("vendor_class"),
    }

    text = json.dumps(report, indent=1, default=str)
    if args.outdir and not args.dry_run:
        os.makedirs(args.outdir, mode=0o700, exist_ok=True)
        path = os.path.join(args.outdir, f"probe-{car}-{time.strftime('%Y%m%dT%H%M%S')}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        link = os.path.join(args.outdir, f"latest-{car}.json")
        try:
            os.remove(link)
        except FileNotFoundError:
            pass
        os.symlink(os.path.basename(path), link)
        print(f"headunit_probe: wrote {path}", file=sys.stderr)
        print("headunit_probe: contains identifiers - scrub before sharing",
              file=sys.stderr)
        print(json.dumps(report["summary"], indent=1))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

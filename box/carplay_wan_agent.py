#!/usr/bin/env python3
"""carplay-wan-agent: turn a car's CarPlay Wi-Fi link into Home Assistant state.

The agent runs on the box, not in Home Assistant. It reads what carplayd
already knows -- its Prometheus metrics and its event log -- keeps a state
machine per car, and publishes that two ways:

  * an HTTP JSON endpoint, which the carplay_wan custom integration polls;
  * optional MQTT discovery, for people who would rather not install one.

It also runs in `watch` mode with no carplayd at all: beacons only, no PSK, no
association, nothing that can disturb CarPlay. That mode is the whole product
for anyone who only wants to know whether the car is home.

Standard library only. The box runs Python 3.11 with nothing installed, and an
agent that needs a pip install before it can tell you the car is home is an
agent that will not get installed. paho-mqtt is imported lazily and only when
MQTT is actually switched on.

Privacy is a correctness requirement here, not a nicety. The SSID carries the
last five digits of the VIN and the MACs identify people, so both are hashed
with a per-install salt before they leave this process. See redact().
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Iterator
from urllib.error import URLError
from urllib.request import urlopen

LOG = logging.getLogger("carplay-wan-agent")

# A car that has not been heard from for this long is gone. carplayd emits its
# own `gone` event, but the agent must not depend on seeing it: if the agent
# starts while a car is absent, or misses a line during a restart, a car would
# otherwise stay "home" forever. Belt and braces, with the braces being time.
PRESENCE_TIMEOUT = 90.0

# How stale metrics may be before the agent stops claiming they are true. A
# scrape failure is not evidence that the car left.
METRICS_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------


def _salt_path(state_dir: str) -> str:
    return os.path.join(state_dir, "redaction-salt")


def load_or_make_salt(state_dir: str) -> str:
    """A per-install salt, so a hashed MAC cannot be looked up in a rainbow
    table of every MAC that exists -- there are only 2^48 of them, which is
    nothing. The salt never leaves the box."""
    path = _salt_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            salt = fh.read().strip()
            if salt:
                return salt
    except FileNotFoundError:
        pass
    salt = secrets.token_hex(16)
    os.makedirs(state_dir, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(salt + "\n")
    return salt


class Redactor:
    """Hashes identifiers on the way out.

    `reveal` exists because a human debugging their own garage is a legitimate
    use, but it is off by default and the README says so. Everything that
    leaves the process goes through here: HTTP, MQTT and logs alike.
    """

    def __init__(self, salt: str, reveal: bool = False) -> None:
        self.salt = salt
        self.reveal = reveal

    def __call__(self, value: str | None, kind: str = "id") -> str | None:
        if value is None:
            return None
        if self.reveal:
            return value
        digest = hashlib.sha256((self.salt + ":" + value).encode()).hexdigest()
        return f"{kind}-{digest[:10]}"


# --------------------------------------------------------------------------
# carplayd event log
# --------------------------------------------------------------------------

# 2026-11-18T19:55:28.130-08:00 EV 393005.218 carplayd_serving A fd5c:.../64 route
EVENT_RE = re.compile(
    r"^(?P<ts>\S+)\s+EV\s+(?P<uptime>[\d.]+)\s+carplayd_(?P<event>\w+)(?:\s+(?P<rest>.*))?$"
)
CAR_RE = re.compile(r"^[A-Z]$")


@dataclass
class Event:
    ts: str
    event: str
    car: str | None
    args: list[str]
    kv: dict[str, str]

    @property
    def signal(self) -> int | None:
        """Most car events carry the signal as the first bare argument."""
        for a in self.args:
            if re.fullmatch(r"-\d{1,3}", a):
                return int(a)
        return None


def parse_event(line: str) -> Event | None:
    m = EVENT_RE.match(line.strip())
    if not m:
        return None
    rest = (m.group("rest") or "").split()
    car = None
    if rest and CAR_RE.match(rest[0]):
        car, rest = rest[0], rest[1:]
    kv, args = {}, []
    for tok in rest:
        if "=" in tok:
            k, _, v = tok.partition("=")
            kv[k] = v
        else:
            args.append(tok)
    return Event(ts=m.group("ts"), event=m.group("event"), car=car, args=args, kv=kv)


def tail(path: str, stop: threading.Event, from_start: bool = False) -> Iterator[str]:
    """Follow a file across rotation.

    carplayd rotates daily, so the agent must notice that the inode under its
    handle is no longer the inode at the path. Polling the file's identity once
    a second is cheaper and far more reliable than inotify on a vendor kernel.
    """
    fh = None
    ino = None
    try:
        while not stop.is_set():
            try:
                st = os.stat(path)
            except FileNotFoundError:
                stop.wait(1.0)
                continue
            if fh is None or st.st_ino != ino:
                if fh is not None:
                    fh.close()
                fh = open(path, "r", encoding="utf-8", errors="replace")
                ino = st.st_ino
                if not from_start:
                    fh.seek(0, os.SEEK_END)
                from_start = True  # after a rotation, read the new file whole
            line = fh.readline()
            if line:
                yield line
            else:
                stop.wait(0.25)
    finally:
        if fh is not None:
            fh.close()


# --------------------------------------------------------------------------
# carplayd metrics
# --------------------------------------------------------------------------

SAMPLE_RE = re.compile(r"^(?P<name>\w+)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-\d.e+]+)$")


def parse_metrics(text: str) -> dict[str, dict[str, float]]:
    """Prometheus text format, only as much as carplayd actually emits.

    Returns {metric: {label_value_or_empty: value}}. carplayd labels everything
    car-shaped with car="A"; the few global metrics get the empty-string key.
    """
    out: dict[str, dict[str, float]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = SAMPLE_RE.match(line)
        if not m:
            continue
        key = ""
        if m.group("labels"):
            pairs = dict(
                (k.strip(), v.strip().strip('"'))
                for k, _, v in (p.partition("=") for p in m.group("labels").split(","))
            )
            key = pairs.get("car") or pairs.get("event") or ""
        try:
            out.setdefault(m.group("name"), {})[key] = float(m.group("value"))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass
class CarState:
    """Everything the agent is willing to claim about one car.

    `present` and `awake` are the same physical fact -- the AP is beaconing,
    which it only does while the car is on -- but they are published as two
    entities because they answer different questions and people automate on
    them differently.
    """

    car: str
    name: str
    present: bool = False
    serving: bool = False
    validated: bool = False
    signal_dbm: int | None = None
    phones: int = 0
    forwarded_bytes: int = 0
    probe_bytes: int = 0
    link_rate_mbps: float | None = None
    beacon_loss: int | None = None
    head_unit: str | None = None
    key_mgmt: str | None = None
    wifi_generation: str | None = None
    session_start: float | None = None
    last_seen: float | None = None
    last_arrival: str | None = None
    last_departure: str | None = None
    last_departure_reason: str | None = None
    detect_seconds: float | None = None
    post_off_probed: bool = False

    @property
    def session_duration(self) -> int | None:
        if self.session_start is None:
            return None
        return int(time.time() - self.session_start)


class Tracker:
    """The state machine. One lock, because HTTP, MQTT and the tailer all read
    it from different threads."""

    def __init__(self, cars: dict[str, str], redact: Redactor,
                 on_arrival: Callable[[str], None] | None = None,
                 on_departure: Callable[[str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self.redact = redact
        self.on_arrival = on_arrival
        self.on_departure = on_departure
        self.cars = {c: CarState(car=c, name=n) for c, n in cars.items()}
        self.upstream = True
        self.metrics_ok = False
        self.metrics_at = 0.0
        self.started = time.time()
        self.events_seen = 0

    # -- ingest -----------------------------------------------------------

    def apply_event(self, ev: Event) -> None:
        with self._lock:
            self.events_seen += 1
            if ev.car is None or ev.car not in self.cars:
                return
            st = self.cars[ev.car]
            now = time.time()
            name = ev.event

            if name in ("seen", "sighting", "select", "connected", "serving",
                        "signal", "validated", "phone_unvalidated"):
                st.last_seen = now
                if not st.present:
                    self._arrive(st, ev, now)

            if name == "connected":
                st.head_unit = self.redact(ev.args[0] if ev.args else None, "bssid")
                st.key_mgmt = ev.kv.get("key_mgmt")
                st.wifi_generation = ev.kv.get("wifi_generation")
            elif name == "serving":
                st.serving = True
            elif name == "validated":
                st.validated = True
            elif name == "phone_unvalidated":
                st.validated = False
            elif name == "signal":
                st.phones = _int(ev.kv.get("phones"), st.phones)
                st.link_rate_mbps = _float(ev.kv.get("tx_bitrate"), st.link_rate_mbps)
                st.beacon_loss = _int(ev.kv.get("beacon_loss"), st.beacon_loss)
            elif name in ("leave", "link_lost"):
                st.serving = False
                st.last_departure_reason = self._departure_reason(ev)
                # The head unit keeps beaconing for a while after the car is
                # switched off. That window is the only chance to see what the
                # AP advertises with nothing associated to it, so the probe
                # gets a second run -- once per session, not once per event.
                if not st.post_off_probed and self.on_departure is not None:
                    st.post_off_probed = True
                    threading.Thread(target=self.on_departure, args=(st.car,),
                                     daemon=True).start()
            elif name == "gone":
                self._depart(st, now)

            if ev.signal is not None and name != "gone":
                st.signal_dbm = ev.signal

    @staticmethod
    def _departure_reason(ev: Event) -> str:
        if ev.event == "leave":
            # "leave A -60 trend base=-51" -> trend; "... threshold"; "... failing"
            for a in ev.args:
                if a in ("threshold", "trend", "failing"):
                    return f"left on {a}"
            return "left"
        why = ev.kv.get("why")
        if why == "sta_leaving":
            return "car switched off"
        if why == "inactivity":
            return "link dropped (inactivity)"
        return f"link lost ({why})" if why else "link lost"

    def _arrive(self, st: CarState, ev: Event, now: float) -> None:
        st.present = True
        st.session_start = now
        st.last_arrival = ev.ts
        st.validated = False
        st.forwarded_bytes = 0
        st.probe_bytes = 0
        st.post_off_probed = False
        LOG.info("%s arrived (%s)", st.name, ev.event)
        if self.on_arrival is not None:
            # Never inline: a probe that blocks the tailer would make the agent
            # miss the rest of the arrival it was triggered by.
            threading.Thread(target=self.on_arrival, args=(st.car,), daemon=True).start()

    def _depart(self, st: CarState, now: float) -> None:
        if not st.present:
            return
        st.present = False
        st.serving = False
        st.validated = False
        st.phones = 0
        st.session_start = None
        st.signal_dbm = None
        st.link_rate_mbps = None
        st.last_departure = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))
        LOG.info("%s departed: %s", st.name, st.last_departure_reason or "unknown")

    def apply_metrics(self, metrics: dict[str, dict[str, float]]) -> None:
        with self._lock:
            self.metrics_ok = True
            self.metrics_at = time.time()
            self.upstream = bool(metrics.get("carplayd_upstream", {}).get("", 1.0))
            for car, st in self.cars.items():
                g = lambda n, d=0.0: metrics.get(n, {}).get(car, d)  # noqa: E731
                st.serving = bool(g("carplayd_serving"))
                st.phones = int(g("carplayd_phones"))
                st.forwarded_bytes = int(g("carplayd_phone_forward_bytes"))
                st.probe_bytes = int(g("carplayd_phone_probe_bytes"))
                st.detect_seconds = g("carplayd_detect_seconds", None) or None
                sig = metrics.get("carplayd_signal_dbm", {}).get(car)
                if sig is not None and st.present:
                    st.signal_dbm = int(sig)
                # The probe counter is the only honest answer to "will apps use
                # this link?". carplayd's validated event says the same thing;
                # the counter also survives an agent restart mid-session.
                if st.probe_bytes > 0 and st.serving:
                    st.validated = True

    def sighting(self, car: str, signal: int | None, bssid: str | None) -> None:
        """Watch mode: a beacon was seen for this car."""
        with self._lock:
            st = self.cars.get(car)
            if st is None:
                return
            now = time.time()
            st.last_seen = now
            if signal is not None:
                st.signal_dbm = signal
            if bssid:
                st.head_unit = self.redact(bssid, "bssid")
            if not st.present:
                self._arrive(st, Event(ts=_now_iso(), event="beacon", car=car,
                                       args=[], kv={}), now)

    def expire(self) -> None:
        with self._lock:
            now = time.time()
            for st in self.cars.values():
                if st.present and st.last_seen and now - st.last_seen > PRESENCE_TIMEOUT:
                    st.last_departure_reason = st.last_departure_reason or "stopped beaconing"
                    self._depart(st, now)
            if self.metrics_ok and time.time() - self.metrics_at > METRICS_TIMEOUT:
                self.metrics_ok = False

    # -- publish ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cars = {}
            for car, st in self.cars.items():
                d = asdict(st)
                d["session_duration"] = st.session_duration
                d["state"] = "home" if st.present else "not_home"
                cars[car] = d
            return {
                "agent": {
                    "version": __version__,
                    "uptime": int(time.time() - self.started),
                    "events_seen": self.events_seen,
                    "metrics_ok": self.metrics_ok,
                    "redacted": not self.redact.reveal,
                },
                "upstream": self.upstream,
                "cars": cars,
            }


__version__ = "0.1.0"


def _int(v: str | None, default: int | None) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(v: str | None, default: float | None) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------
# watch mode: beacons only
# --------------------------------------------------------------------------


def scan_once(dev: str, ssids: dict[str, str]) -> list[tuple[str, int | None, str | None]]:
    """One `iw scan` restricted to the CarPlay channel.

    Watch mode never associates and never needs the PSK, so it cannot disturb
    CarPlay -- but a scan is not free either: it is 131-156 ms of the channel
    on this hardware, and the channel is carrying someone's CarPlay audio.
    That is why the caller's default interval is seconds, not milliseconds.
    """
    iw = shutil.which("iw") or "/usr/sbin/iw"
    try:
        out = subprocess.run(
            [iw, "dev", dev, "scan", "freq", "2462", "-u"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("scan failed on %s: %s", dev, exc)
        return []
    found: list[tuple[str, int | None, str | None]] = []
    bssid: str | None = None
    signal: int | None = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("BSS "):
            bssid = line.split()[1].split("(")[0]
            signal = None
        elif line.startswith("signal:"):
            try:
                signal = int(float(line.split()[1]))
            except (IndexError, ValueError):
                signal = None
        elif line.startswith("SSID:"):
            ssid = line[5:].strip()
            for car, want in ssids.items():
                if ssid == want:
                    found.append((car, signal, bssid))
    return found


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    tracker: Tracker

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/state", ""):
            body = json.dumps(self.tracker.snapshot(), indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.rstrip("/") == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("http %s", fmt % args)


# --------------------------------------------------------------------------
# MQTT (optional)
# --------------------------------------------------------------------------

DISCOVERY = [
    # (component, key, name, device_class, unit, state_class, icon, category)
    ("device_tracker", "state", None, None, None, None, "mdi:car", None),
    ("binary_sensor", "present", "Awake", "presence", None, None, None, None),
    ("binary_sensor", "serving", "Route up", "connectivity", None, None, None, None),
    ("binary_sensor", "validated", "Internet validated", "connectivity", None, None, None, None),
    ("sensor", "signal_dbm", "Signal", "signal_strength", "dBm", "measurement", None, "diagnostic"),
    ("sensor", "phones", "Phones", None, None, "measurement", "mdi:cellphone", None),
    ("sensor", "forwarded_bytes", "Forwarded", "data_size", "B", "total_increasing", None, None),
    ("sensor", "session_duration", "Awake for", "duration", "s", "measurement", None, None),
    ("sensor", "link_rate_mbps", "Link rate", "data_rate", "Mbit/s", "measurement", None, "diagnostic"),
    ("sensor", "head_unit", "Head unit", None, None, None, "mdi:car-info", "diagnostic"),
    ("sensor", "last_departure_reason", "Last departure", None, None, None, "mdi:car-arrow-right", "diagnostic"),
]


class MqttPublisher:
    def __init__(self, tracker: Tracker, host: str, port: int, user: str | None,
                 password: str | None, prefix: str) -> None:
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SystemExit(
                "MQTT was requested but paho-mqtt is not installed.\n"
                "  pip install paho-mqtt   (or drop --mqtt-host and use the HTTP "
                "endpoint with the carplay_wan integration, which needs nothing)"
            ) from exc
        self.tracker = tracker
        self.prefix = prefix
        self.client = mqtt.Client(client_id="carplay-wan-agent", protocol=mqtt.MQTTv311)
        if user:
            self.client.username_pw_set(user, password or "")
        self.client.will_set(f"{prefix}/availability", "offline", retain=True)
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()
        self.client.publish(f"{prefix}/availability", "online", retain=True)
        self._announce()

    def _announce(self) -> None:
        for car, st in self.tracker.cars.items():
            slug = re.sub(r"\W+", "_", st.name.lower()).strip("_")
            device = {
                "identifiers": [f"carplay_wan_{slug}"],
                "name": st.name,
                "manufacturer": "carplay-wan",
                "model": "CarPlay Wi-Fi link",
                "sw_version": __version__,
            }
            for comp, key, label, dclass, unit, sclass, icon, cat in DISCOVERY:
                uid = f"carplay_wan_{slug}_{key}"
                cfg: dict[str, Any] = {
                    "unique_id": uid,
                    "object_id": uid,
                    "state_topic": f"{self.prefix}/{car}/state",
                    "value_template": "{{ value_json.%s }}" % key,
                    "availability_topic": f"{self.prefix}/availability",
                    "device": device,
                }
                cfg["name"] = label if label else None
                if comp == "binary_sensor":
                    cfg["payload_on"], cfg["payload_off"] = "True", "False"
                for k, v in (("device_class", dclass), ("unit_of_measurement", unit),
                             ("state_class", sclass), ("icon", icon),
                             ("entity_category", cat)):
                    if v:
                        cfg[k] = v
                self.client.publish(
                    f"homeassistant/{comp}/{uid}/config",
                    json.dumps({k: v for k, v in cfg.items() if v is not None}),
                    retain=True,
                )

    def publish(self) -> None:
        snap = self.tracker.snapshot()
        for car, data in snap["cars"].items():
            self.client.publish(f"{self.prefix}/{car}/state", json.dumps(data), retain=True)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def fetch_metrics(url: str) -> dict[str, dict[str, float]] | None:
    try:
        with urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed localhost URL
            return parse_metrics(resp.read().decode("utf-8", "replace"))
    except (URLError, OSError, TimeoutError) as exc:
        LOG.debug("metrics scrape failed: %s", exc)
        return None


def parse_cars(spec: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """--car A=F32[=SSID] -> ({A: F32}, {A: SSID}). The SSID is only needed in
    watch mode, and is deliberately not required otherwise: it contains VIN
    digits, so the fewer places it has to be written down, the better."""
    names, ssids = {}, {}
    for item in spec:
        parts = item.split("=", 2)
        if len(parts) < 2 or not CAR_RE.match(parts[0]):
            raise SystemExit(f"--car wants LETTER=Name[=SSID], got {item!r}")
        names[parts[0]] = parts[1]
        if len(parts) == 3:
            ssids[parts[0]] = parts[2]
    return names, ssids


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="carplay-wan-agent",
        description="Publish a car's CarPlay Wi-Fi link to Home Assistant.",
    )
    p.add_argument("--car", action="append", default=[], metavar="A=Name[=SSID]",
                   help="repeatable; the letter is carplayd's car id")
    p.add_argument("--events", default="/var/lib/carplayd/events.log",
                   help="carplayd event log to follow")
    p.add_argument("--metrics", default="http://127.0.0.1:9111/metrics",
                   help="carplayd Prometheus endpoint")
    p.add_argument("--watch-dev", metavar="DEV",
                   help="watch mode: scan for beacons on DEV instead of reading "
                        "carplayd. No PSK, no association, cannot disturb CarPlay")
    p.add_argument("--watch-interval", type=float, default=10.0,
                   help="seconds between scans in watch mode (default 10)")
    p.add_argument("--listen", default="0.0.0.0:8099", metavar="HOST:PORT")
    p.add_argument("--state-dir", default="/var/lib/carplay-wan")
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between metric scrapes and publishes")
    p.add_argument("--probe", metavar="SCRIPT",
                   help="run SCRIPT <car> when a car arrives (see probe/)")
    p.add_argument("--mqtt-host"), p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--mqtt-user"), p.add_argument("--mqtt-password")
    p.add_argument("--mqtt-prefix", default="carplay_wan")
    p.add_argument("--reveal", action="store_true",
                   help="publish real BSSIDs instead of hashes. Off by default: "
                        "these identify your car and the people in it")
    p.add_argument("--once", action="store_true", help="print one snapshot and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.car:
        raise SystemExit("at least one --car A=Name is required")
    names, ssids = parse_cars(args.car)
    if args.watch_dev and not ssids:
        raise SystemExit("watch mode needs the SSID: --car A=Name=SSID")

    redact = Redactor(load_or_make_salt(args.state_dir), reveal=args.reveal)
    if args.reveal:
        LOG.warning("--reveal: real identifiers will be published. "
                    "Do not paste this output into an issue.")

    probe_cb = None
    if args.probe:
        def probe_cb(car: str) -> None:  # noqa: F811
            LOG.info("probe: running %s for car %s", args.probe, car)
            try:
                r = subprocess.run([args.probe, car], capture_output=True,
                                   text=True, timeout=120, check=False)
                LOG.info("probe finished rc=%d %s", r.returncode,
                         (r.stdout or r.stderr).strip().splitlines()[-1:] or "")
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.warning("probe failed: %s", exc)

    depart_cb = None
    if args.probe:
        def depart_cb(car: str) -> None:  # noqa: F811
            LOG.info("probe: post-shutdown run for car %s", car)
            try:
                subprocess.run([args.probe, car, "--unassociated",
                                "--tag", "post-off"],
                               capture_output=True, text=True, timeout=120,
                               check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                LOG.warning("post-shutdown probe failed: %s", exc)

    tracker = Tracker(names, redact, on_arrival=probe_cb, on_departure=depart_cb)

    if args.once:
        if not args.watch_dev:
            m = fetch_metrics(args.metrics)
            if m:
                tracker.apply_metrics(m)
            try:
                with open(args.events, encoding="utf-8", errors="replace") as fh:
                    for line in fh.readlines()[-5000:]:
                        ev = parse_event(line)
                        if ev:
                            tracker.apply_event(ev)
            except OSError as exc:
                LOG.warning("event log: %s", exc)
        tracker.expire()
        json.dump(tracker.snapshot(), sys.stdout, indent=2)
        print()
        return 0

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    host, _, port = args.listen.rpartition(":")
    Handler.tracker = tracker
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("agent %s listening on %s (cars: %s)", __version__, args.listen,
             ", ".join(f"{k}={v}" for k, v in names.items()))

    mqtt_pub = None
    if args.mqtt_host:
        mqtt_pub = MqttPublisher(tracker, args.mqtt_host, args.mqtt_port,
                                 args.mqtt_user, args.mqtt_password, args.mqtt_prefix)
        LOG.info("mqtt: publishing discovery to %s:%d", args.mqtt_host, args.mqtt_port)

    if args.watch_dev:
        LOG.info("watch mode on %s: beacons only, no association", args.watch_dev)
        def watch() -> None:
            while not stop.is_set():
                for car, sig_dbm, bssid in scan_once(args.watch_dev, ssids):
                    tracker.sighting(car, sig_dbm, bssid)
                stop.wait(args.watch_interval)
        threading.Thread(target=watch, daemon=True).start()
    else:
        def follow() -> None:
            for line in tail(args.events, stop):
                ev = parse_event(line)
                if ev:
                    tracker.apply_event(ev)
        threading.Thread(target=follow, daemon=True).start()

    while not stop.is_set():
        if not args.watch_dev:
            m = fetch_metrics(args.metrics)
            if m:
                tracker.apply_metrics(m)
        tracker.expire()
        if mqtt_pub is not None:
            mqtt_pub.publish()
        stop.wait(args.interval)

    LOG.info("stopping")
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

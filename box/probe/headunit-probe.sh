#!/bin/sh
# headunit-probe.sh — ask the head unit what it exposes, once per arrival.
#
#   headunit-probe.sh <car-letter>
#
# This is the experiment behind Q2 in the README. CarPlay's service discovery
# runs over Bonjour, so there is near-certainly an mDNS responder on the car
# link, and what an NBT EVO advertises in its TXT records has never been
# published. Nobody can run this on demand: the car's AP only exists while the
# car is awake, so the agent fires it on arrival and it has to finish fast.
#
# Rules this script follows, and you should keep:
#
#   * Read-only. It asks questions. It never writes, pairs, or authenticates.
#   * Your own car only.
#   * Polite. A short fixed port list, one connect attempt each, a hard
#     timeout. CarPlay is sharing this channel and the head unit is an
#     infotainment computer, not a server -- upstream measured that saturating
#     it drops CarPlay entirely and needs a hard reset to re-pair.
#   * Bounded. The whole run is capped, because the car may leave mid-probe.
#
# Output: one JSON file per run under $OUT, and a `latest-<car>.json` symlink.
# Results are the input to the README's tier-2 section. If this comes back
# empty on every car, that is a real finding and the README should say so.
set -eu

CAR=${1:?usage: headunit-probe.sh <car-letter>}
IFACE=${IFACE:-$([ "$CAR" = A ] && echo cara || echo carb)}
GW=${GW:-172.16.224.1}
OUT=${OUT:-/var/lib/carplay-wan/probe}
BUDGET=${BUDGET:-45}
# Ports worth one connect each. Chosen from what a CarPlay/AirPlay head unit
# plausibly runs plus the only comparable published scan (a Kenwood unit:
# 22/7000/8086/8888/5355). Keep this list short on purpose.
PORTS=${PORTS:-"22 80 443 554 3689 5000 5353 7000 7100 8008 8080 8086 8888 49152"}

mkdir -p "$OUT"
stamp=$(date +%Y%m%dT%H%M%S)
f="$OUT/probe-$CAR-$stamp.json"
start=$(date +%s)

log() { echo "headunit-probe: $*" >&2; }
left() { echo $(( BUDGET - ($(date +%s) - start) )); }

# Nothing to probe if the link is not up. This is the normal case for a car
# that woke up and was gone again before the agent got here.
if ! ip link show "$IFACE" >/dev/null 2>&1; then
	log "no interface $IFACE, nothing to do"
	exit 0
fi
state=$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null || echo unknown)
if [ "$state" != "up" ]; then
	log "$IFACE is $state, nothing to do"
	exit 0
fi

json_escape() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/ /g' | tr -d '\000-\037'; }

# --- mDNS ------------------------------------------------------------------
# The headline question. -a all services, -r resolve, -t terminate on cache
# exhaustion, -p parseable. Restricted to the car interface by filtering the
# first field, because avahi-browse has no reliable per-interface flag.
mdns=""
if command -v avahi-browse >/dev/null 2>&1; then
	mdns=$(timeout "$(left)" avahi-browse -artp 2>/dev/null \
		| awk -F';' -v i="$IFACE" '$2==i' | head -200 || true)
	log "mdns: $(printf '%s' "$mdns" | grep -c . || true) records on $IFACE"
else
	log "avahi-browse not installed; skipping the most interesting half"
fi

# --- neighbours ------------------------------------------------------------
# Who else is on this link, by IPv6 neighbour cache and the known IPv4 gateway.
neigh=$(ip -6 neigh show dev "$IFACE" 2>/dev/null | head -20 || true)
v4=$(ip -4 neigh show dev "$IFACE" 2>/dev/null | head -20 || true)

# --- ports -----------------------------------------------------------------
# One TCP connect per port, 1 s each, stop if the budget runs out. No nmap
# dependency: /dev/tcp is in every shell that matters here, and a connect is
# all that is wanted -- no banners pulled, no payloads sent.
open=""
for p in $PORTS; do
	[ "$(left)" -gt 5 ] || { log "budget spent, stopping port probe"; break; }
	if timeout 1 sh -c "echo > /dev/tcp/$GW/$p" 2>/dev/null; then
		open="$open $p"
	fi
done
log "open tcp:${open:- none}"

# --- beacon elements -------------------------------------------------------
# Upstream's ieaudit decodes the Interworking and Apple Device elements; this
# just records the raw scan so the decoder has something to chew on later.
ies=""
if command -v iw >/dev/null 2>&1 && [ "$(left)" -gt 10 ]; then
	ies=$(timeout 10 iw dev "$IFACE" scan dump 2>/dev/null | head -120 || true)
fi

{
	printf '{\n'
	printf '  "car": "%s",\n' "$CAR"
	printf '  "iface": "%s",\n' "$IFACE"
	printf '  "started": "%s",\n' "$stamp"
	printf '  "elapsed_s": %s,\n' "$(( $(date +%s) - start ))"
	printf '  "open_tcp": [%s],\n' "$(echo "$open" | tr ' ' '\n' | grep . | paste -sd, - 2>/dev/null || true)"
	printf '  "mdns": "%s",\n' "$(printf '%s' "$mdns" | json_escape | paste -sd'\\n' - 2>/dev/null || true)"
	printf '  "neigh6": "%s",\n' "$(printf '%s' "$neigh" | json_escape | paste -sd'\\n' - 2>/dev/null || true)"
	printf '  "neigh4": "%s",\n' "$(printf '%s' "$v4" | json_escape | paste -sd'\\n' - 2>/dev/null || true)"
	printf '  "scan_dump": "%s"\n' "$(printf '%s' "$ies" | json_escape | paste -sd'\\n' - 2>/dev/null || true)"
	printf '}\n'
} > "$f"

ln -sf "$(basename "$f")" "$OUT/latest-$CAR.json"
log "wrote $f ($(wc -c < "$f") bytes)"

# A reminder, because this output is exactly the kind of thing that gets pasted
# into an issue: it contains BSSIDs and may contain the SSID.
log "contains identifiers — scrub before sharing"

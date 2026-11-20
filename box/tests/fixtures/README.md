# Fixtures

`beacon_with_wps.hex` is a real 802.11 information-element blob, captured from
the garage box's own radio with `wpa_cli bss`, from a **neighbour's** access
point on channel 11 — not from either car.

**The SSID element has been replaced** with `CarPlay-Fixture`. Everything else
is the capability advertisement the AP broadcasts to the whole street: rates,
country, HT/VHT/HE, RSN, and a minimal WPS element. No BSSID appears in an IE
blob, so nothing here identifies the network it came from.

It is here because a parser tested only against bytes its author invented
agrees with its author. Real beacons carry unknown extension IDs, vendor
elements from three different chipset vendors, and a WPS element with four
attributes in it rather than the twelve the specification allows — which is
the whole reason `probe_wps` has to ask the AP directly.

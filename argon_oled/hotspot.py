"""Discover the local Wi-Fi hotspot config via nmcli (no display deps).

Reads via the active console user's nmcli, which on Trixie's default polkit
policy can read AP-mode connection PSKs without sudo. If that's not the case
in your environment, override `connection_name` with a name whose secrets the
user can read, or pre-stage a config file (future work).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

NMCLI = "nmcli"
NMCLI_TIMEOUT = 2.0


@dataclass(frozen=True)
class HotspotConfig:
    connection: str
    ssid: str
    device: str        # wlan0, etc.
    band: str          # "a" (5 GHz), "bg" (2.4 GHz), "" (auto/unknown)
    channel: int
    psk: str
    key_mgmt: str       # "wpa-psk", "sae", "none", etc.
    proto: str          # "wpa", "rsn", "wpa rsn", or "" (NM default)
    hidden: bool

    @property
    def display_auth(self) -> str:
        """Human-friendly auth label for on-screen display.

        The Wi-Fi QR spec uses just `WPA` for the whole WPA family — phones
        negotiate the actual flavor. But on the OLED we want to show the
        real protocol. Mapping:

        - sae                 → WPA3
        - wpa-psk + proto wpa → WPA  (rare; deprecated WPA1)
        - wpa-psk + proto rsn → WPA2
        - wpa-psk + proto ""  → WPA2 (modern NM default is RSN-only)
        - none                → open
        """
        km = self.key_mgmt.lower()
        if km == "sae":
            return "WPA3"
        if km == "none":
            return "open"
        if km == "wpa-psk":
            tokens = self.proto.lower().split()
            if "rsn" in tokens and "wpa" not in tokens:
                return "WPA2"
            if "wpa" in tokens and "rsn" not in tokens:
                return "WPA"
            if not tokens:
                return "WPA2"  # modern NM default
            return "WPA/2"      # both allowed
        return km.upper()


def _nmcli(*args: str) -> str:
    return subprocess.check_output(
        [NMCLI, *args], text=True, timeout=NMCLI_TIMEOUT,
    )


def find_active_hotspot() -> str | None:
    """Return the name of an active 802-11-wireless connection in AP mode."""
    try:
        out = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show", "--active")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        log.debug("nmcli list active failed: %s", e)
        return None
    for line in out.splitlines():
        if not line:
            continue
        # NAME may contain ':'; type is the last colon-separated field.
        idx = line.rfind(":")
        if idx < 0:
            continue
        name, conn_type = line[:idx], line[idx + 1:]
        if conn_type != "802-11-wireless":
            continue
        try:
            mode = _nmcli("-t", "-g", "802-11-wireless.mode",
                          "connection", "show", name).strip()
        except subprocess.CalledProcessError:
            continue
        if mode == "ap":
            return name
    return None


def read_hotspot_config(connection_name: str) -> HotspotConfig | None:
    try:
        out = _nmcli("--show-secrets", "-t",
                     "connection", "show", connection_name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        log.warning("Cannot read hotspot config %r: %s", connection_name, e)
        return None
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k] = v
    try:
        return HotspotConfig(
            connection=connection_name,
            ssid=fields["802-11-wireless.ssid"],
            device=fields.get("GENERAL.DEVICES", ""),
            band=fields.get("802-11-wireless.band", ""),
            channel=int(fields.get("802-11-wireless.channel", "0") or 0),
            psk=fields.get("802-11-wireless-security.psk", ""),
            key_mgmt=fields.get("802-11-wireless-security.key-mgmt", "none"),
            proto=fields.get("802-11-wireless-security.proto", ""),
            hidden=fields.get("802-11-wireless.hidden", "no") == "yes",
        )
    except KeyError as e:
        log.warning("Missing field in nmcli output for %s: %s",
                    connection_name, e)
        return None


def _qr_escape(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace(";", "\\;")
             .replace(",", "\\,")
             .replace(":", "\\:")
             .replace('"', '\\"'))


def wifi_qr_payload(cfg: HotspotConfig) -> str:
    """Build the standard ``WIFI:`` URI consumed by phone QR scanners."""
    auth_map = {"wpa-psk": "WPA", "sae": "WPA", "none": "nopass"}
    auth = auth_map.get(cfg.key_mgmt, "WPA")
    parts = [
        "WIFI:",
        f"T:{auth};",
        f"S:{_qr_escape(cfg.ssid)};",
    ]
    if auth != "nopass":
        parts.append(f"P:{_qr_escape(cfg.psk)};")
    if cfg.hidden:
        parts.append("H:true;")
    parts.append(";")
    return "".join(parts)


def count_connected_stations(iface: str) -> int | None:
    """Count clients associated to the AP on `iface`. Uses `iw`, no root."""
    if not iface:
        return None
    try:
        out = subprocess.check_output(
            ["iw", "dev", iface, "station", "dump"],
            text=True, timeout=NMCLI_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        log.debug("iw station dump on %s failed: %s", iface, e)
        return None
    return sum(1 for line in out.splitlines() if line.startswith("Station "))


def band_label(band: str) -> str:
    if band == "a":
        return "5GHz"
    if band == "bg":
        return "2.4G"
    return "?"

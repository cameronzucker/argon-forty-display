"""Render each carousel screen to a PNG for the README.

Generates ``docs/screenshots/<NN>_<name>.png`` at 4x scale (512 x 256), with
the carousel hint (``n/N``) overlaid so the image matches what a user sees on
the OLED while navigating. Re-run after any layout change to refresh the
README previews.

Uses live system data where it works without contending with the running
service (no GPIO claims, no I2C-0x36 access); uses fixture data for screens
that would otherwise render unrepresentatively (e.g. battery is stubbed
because the systemd service already holds GPIO6).
"""

from __future__ import annotations

import datetime as dt
import math
import sys
import time
from pathlib import Path

# Allow `uv run python scripts/render_screens.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from argon_oled import metrics  # noqa: E402
from argon_oled.battery import BatteryStatus  # noqa: E402
from argon_oled.screens import (  # noqa: E402
    BatteryScreen,
    DiskScreen,
    GPSScreen,
    HelpScreen,
    HotspotScreen,
    NetworkScreen,
    StatusScreen,
)

OUT_DIR = Path("docs/screenshots")
SCALE = 4
CAROUSEL = ["status", "battery", "network", "wifi", "disk", "gps", "help"]


class _StubWatcher:
    """Provides the only attribute BatteryScreen reads (`status`)."""
    def __init__(self, status: BatteryStatus):
        self.status = status


class _StubGPSClient:
    """Mimics the surface of GPSDClient that GPSScreen renders against."""
    def __init__(self):
        self.tpv = {
            "mode": 3,
            "lat": 37.7749, "lon": -122.4194,
            "altMSL": 12.0, "eph": 1.5, "epv": 2.0,
        }
        self.sky = {
            "satellites": [
                {"PRN": 1, "az": 45, "el": 60, "used": True},
                {"PRN": 2, "az": 120, "el": 30, "used": True},
                {"PRN": 3, "az": 200, "el": 75, "used": True},
                {"PRN": 4, "az": 290, "el": 15, "used": False},
                {"PRN": 5, "az": 350, "el": 45, "used": True},
                {"PRN": 6, "az": 80, "el": 5, "used": False},
            ],
        }
        self.connected = True
        self.error = None
        self.last_pps_ns = time.monotonic_ns() - 100_000_000


def _add_carousel_hint(img: Image.Image, font: ImageFont.ImageFont,
                       idx: int, total: int) -> None:
    draw = ImageDraw.Draw(img)
    w, _ = img.size
    hint = f"{idx}/{total}"
    bbox = font.getbbox(hint)
    hint_w = bbox[2] - bbox[0]
    draw.rectangle((w - hint_w - 2, 0, w - 1, 9), fill=0)
    draw.text((w - hint_w - 1, 0), hint, fill=1, font=font)


def _render_status(font, snap):
    s = StatusScreen(font)
    n_cores = max(len(snap.cpu_per_core), 4)
    for tick in range(60):
        t = tick / 60.0
        fake_cores = tuple(
            max(2.0, min(98.0,
                30 + 25 * math.sin(2 * math.pi * (t + i * 0.2))
                + 15 * math.sin(8 * math.pi * t)))
            for i in range(n_cores)
        )
        synth = metrics.SystemSnapshot(
            timestamp=snap.timestamp + dt.timedelta(seconds=tick),
            hostname=snap.hostname, primary_ip=snap.primary_ip,
            cpu_percent=sum(fake_cores) / len(fake_cores),
            cpu_per_core=fake_cores,
            cpu_freq_mhz=snap.cpu_freq_mhz, cpu_temp_c=snap.cpu_temp_c,
            mem_used_pct=snap.mem_used_pct, mem_used_mb=snap.mem_used_mb,
            mem_total_mb=snap.mem_total_mb, load_1m=snap.load_1m,
            uptime_s=snap.uptime_s + tick,
        )
        s._ingest(synth)
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_battery(font, snap):
    status = BatteryStatus(
        detected=True, stale=False,
        voltage_v=4.10, soc_pct=89.0, soc_pct_raw=89.0,
        direction="discharging", source_hint="battery",
        eta_seconds=15120, slope_pct_per_min=-0.4,
        soc_history=tuple(
            85 + 10 * math.sin(2 * math.pi * i / 40) for i in range(128)
        ),
    )
    s = BatteryScreen(font, _StubWatcher(status))
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_network(font, snap):
    s = NetworkScreen(font)
    # Push a future _next_sample so render() doesn't overwrite our
    # synthesized history with a zero-rate first sample.
    s._next_sample = time.monotonic() + 3600
    s._last_t = time.monotonic()
    s._last_tx = 0
    s._last_rx = 0
    for i in range(128):
        s._tx_history.append(1024 * (50 + 30 * math.sin(2 * math.pi * i / 40)))
        s._rx_history.append(1024 * (200 + 100 * math.sin(2 * math.pi * i / 30 + 1)))
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_wifi(font, snap):
    s = HotspotScreen(font)
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_disk(font, snap):
    s = DiskScreen(font)
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_gps(font, snap):
    s = GPSScreen(font)
    s._client = _StubGPSClient()
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


def _render_help(font, snap):
    s = HelpScreen(font)
    img = Image.new("1", (128, 64), 0)
    s.render(img, snap)
    return img


_RENDERERS = {
    "status": _render_status,
    "battery": _render_battery,
    "network": _render_network,
    "wifi": _render_wifi,
    "disk": _render_disk,
    "gps": _render_gps,
    "help": _render_help,
}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default()
    snap = metrics.gather()
    total = len(CAROUSEL)
    for idx, name in enumerate(CAROUSEL, start=1):
        img = _RENDERERS[name](font, snap)
        _add_carousel_hint(img, font, idx, total)
        scaled = img.convert("L").resize(
            (img.width * SCALE, img.height * SCALE),
            Image.NEAREST,
        )
        out = OUT_DIR / f"{idx:02d}_{name}.png"
        scaled.save(out)
        print(f"wrote {out} ({scaled.width}x{scaled.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

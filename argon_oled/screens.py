"""Display screens. Each screen renders a SystemSnapshot onto a Pillow Image.

A `ScreenCarousel` owns a list of screens, tracks the active index, and adds
a small "n/N" hint in the top-right corner. Button input rotates the index.
"""

from __future__ import annotations

import logging
import math
import socket as _socket
import time
from collections import deque
from typing import Protocol

import psutil
import segno
from PIL import Image, ImageDraw, ImageFont

from .gps import GPSDClient
from .hotspot import (
    HotspotConfig,
    band_label,
    count_connected_stations,
    find_active_hotspot,
    read_hotspot_config,
    wifi_qr_payload,
)
from .metrics import SystemSnapshot, format_uptime

_log = logging.getLogger(__name__)

LINE_H = 10


class Screen(Protocol):
    name: str

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None: ...


class StatusScreen:
    """btop-style host overview: hostname, time/uptime, CPU summary, then
    per-core scrolling sparklines (one column per second of history),
    and a memory bar. Uses the full 64px.
    """

    name = "status"

    def __init__(self, font: ImageFont.ImageFont, history_len: int = 60):
        self._font = font
        self._history_len = history_len
        self._core_history: list[deque[float]] = []
        self._last_snap_ts = None

    def _ingest(self, snap: SystemSnapshot) -> None:
        if snap.timestamp == self._last_snap_ts:
            return
        cores = snap.cpu_per_core
        if len(self._core_history) != len(cores):
            self._core_history = [
                deque(maxlen=self._history_len) for _ in cores
            ]
        for i, pct in enumerate(cores):
            self._core_history[i].append(pct)
        self._last_snap_ts = snap.timestamp

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        self._ingest(snap)

        draw = ImageDraw.Draw(image)
        w, _ = image.size
        time_str = snap.timestamp.strftime("%H:%M:%S")
        up = format_uptime(snap.uptime_s)
        temp = (
            f"{snap.cpu_temp_c:.0f}C"
            if snap.cpu_temp_c is not None else "--C"
        )
        freq = _format_freq(snap.cpu_freq_mhz)

        draw.text((0, 0), snap.hostname[:17], fill=1, font=self._font)
        draw.text((0, 10), f"{time_str}  up {up}", fill=1, font=self._font)
        draw.text((0, 20),
                  f"CPU {snap.cpu_percent:4.1f}%  {temp}  {freq}",
                  fill=1, font=self._font)

        cores = snap.cpu_per_core
        if cores:
            n = len(cores)
            gap = 2
            bar_w = max(4, (w - (n - 1) * gap) // n)
            bar_y = 31
            bar_h = 13
            for i in range(n):
                bx = i * (bar_w + gap)
                history = (
                    self._core_history[i]
                    if i < len(self._core_history) else ()
                )
                _draw_sparkline(draw, bx, bar_y, bar_w, bar_h, history)

        draw.text((0, 46),
                  f"MEM {snap.mem_used_pct:4.1f}%  l{snap.load_1m:.2f}",
                  fill=1, font=self._font)
        _draw_bar(draw, 0, 57, w, 6, snap.mem_used_pct / 100.0)


class NetworkScreen:
    """Per-iface IPv4 list, current TX/RX rates, and a dual sparkline
    (TX above midline, RX below) covering the bottom portion of the screen.
    Aggregates across all non-loopback interfaces.
    """

    name = "network"

    def __init__(self, font: ImageFont.ImageFont, history_len: int = 128,
                 sample_period: float = 1.0):
        self._font = font
        self._history_len = history_len
        self._tx_history: deque[float] = deque(maxlen=history_len)
        self._rx_history: deque[float] = deque(maxlen=history_len)
        self._sample_period = sample_period
        self._last_tx = 0
        self._last_rx = 0
        self._last_t = 0.0
        self._next_sample = 0.0

    def _aggregate_counters(self) -> tuple[int, int]:
        tx = rx = 0
        for nic, c in psutil.net_io_counters(pernic=True).items():
            if nic == "lo":
                continue
            tx += c.bytes_sent
            rx += c.bytes_recv
        return tx, rx

    def _sample(self) -> tuple[float, float]:
        now = time.monotonic()
        tx, rx = self._aggregate_counters()
        if self._last_t == 0.0:
            self._last_tx, self._last_rx, self._last_t = tx, rx, now
            return 0.0, 0.0
        dt = max(now - self._last_t, 0.001)
        tx_rate = max(0.0, (tx - self._last_tx) / dt)
        rx_rate = max(0.0, (rx - self._last_rx) / dt)
        self._last_tx, self._last_rx, self._last_t = tx, rx, now
        return tx_rate, rx_rate

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        now = time.monotonic()
        if now >= self._next_sample:
            tx_rate, rx_rate = self._sample()
            self._tx_history.append(tx_rate)
            self._rx_history.append(rx_rate)
            self._next_sample = now + self._sample_period

        draw = ImageDraw.Draw(image)
        w, h = image.size

        draw.text((0, 0), "network", fill=1, font=self._font)

        ifaces: list[tuple[str, str]] = []
        for name, addrs in psutil.net_if_addrs().items():
            if name == "lo":
                continue
            for a in addrs:
                if a.family == _socket.AF_INET:
                    ifaces.append((name, a.address))
                    break
        for i, (name, ip) in enumerate(ifaces[:2]):
            draw.text((0, 10 + i * LINE_H),
                      f"{name[:5]:<5} {ip}", fill=1, font=self._font)

        tx_rate = self._tx_history[-1] if self._tx_history else 0.0
        rx_rate = self._rx_history[-1] if self._rx_history else 0.0
        draw.text((0, 30),
                  f"TX {_humanize_rate(tx_rate)}/s  "
                  f"RX {_humanize_rate(rx_rate)}/s",
                  fill=1, font=self._font)

        spark_top = 40
        spark_bottom = h - 1
        spark_h = spark_bottom - spark_top + 1  # 24
        mid_y = spark_top + spark_h // 2
        half_h = (spark_h // 2) - 1

        peak = max(
            (max(self._tx_history) if self._tx_history else 0.0),
            (max(self._rx_history) if self._rx_history else 0.0),
            1.0,
        )

        n = len(self._tx_history)
        if n > 0:
            for i, val in enumerate(self._tx_history):
                x = w - n + i
                if 0 <= x < w:
                    bh = int(val / peak * half_h)
                    if bh > 0:
                        draw.line((x, mid_y - bh, x, mid_y - 1), fill=1)
            for i, val in enumerate(self._rx_history):
                x = w - n + i
                if 0 <= x < w:
                    bh = int(val / peak * half_h)
                    if bh > 0:
                        draw.line((x, mid_y + 1, x, mid_y + bh), fill=1)


def _humanize_bytes(n: int) -> str:
    if n >= 10 * 1024 ** 3:
        return f"{n / 1024 ** 3:.0f}G"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 10 * 1024 ** 2:
        return f"{n / 1024 ** 2:.0f}M"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f}M"
    return f"{n / 1024:.0f}K"


def _short_mount(m: str) -> str:
    if m == "/":
        return "/"
    return m.split("/")[-1] or m


def _device_for_mount(mount: str) -> str | None:
    for p in psutil.disk_partitions(all=False):
        if p.mountpoint == mount:
            return p.device
    return None


def _parent_block(device_path: str) -> str:
    import re
    name = device_path.removeprefix("/dev/")
    if name.startswith("nvme"):
        m = re.match(r"(nvme\d+n\d+)", name)
    elif name.startswith("mmcblk"):
        m = re.match(r"(mmcblk\d+)", name)
    else:
        m = re.match(r"([a-z]+)", name)
    return m.group(1) if m else name


def _block_model(device_path: str) -> str:
    parent = _parent_block(device_path)
    for sysfs_path in (
        f"/sys/block/{parent}/device/model",
        f"/sys/block/{parent}/device/name",
    ):
        try:
            with open(sysfs_path) as f:
                return f.read().strip().replace("_", " ")
        except FileNotFoundError:
            continue
    return parent


def _draw_bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
              fraction: float) -> None:
    fraction = max(0.0, min(1.0, fraction))
    draw.rectangle((x, y, x + w - 1, y + h - 1), outline=1, fill=0)
    inner_w = w - 4
    filled = int(inner_w * fraction)
    if filled > 0:
        draw.rectangle((x + 2, y + 2, x + 2 + filled, y + h - 3), fill=1)


def _draw_vbar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
               fraction: float) -> None:
    """Vertical bar. Outline rectangle, fills upward from the bottom."""
    fraction = max(0.0, min(1.0, fraction))
    draw.rectangle((x, y, x + w - 1, y + h - 1), outline=1, fill=0)
    inner_h = h - 4
    filled = int(inner_h * fraction)
    if filled > 0:
        bottom = y + h - 3
        top = bottom - filled + 1
        draw.rectangle((x + 2, top, x + w - 3, bottom), fill=1)


def _format_freq(mhz: float | None) -> str:
    if mhz is None:
        return "?"
    if mhz >= 1000:
        return f"{mhz / 1000:.1f}G"
    return f"{mhz:.0f}M"


def _draw_sparkline(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                    history, peak: float = 100.0) -> None:
    """Outline rectangle with a right-aligned bar-graph sparkline inside.
    Each sample becomes one 1-pixel column whose height is `value / peak`
    of the inner area. Newer samples on the right.
    """
    draw.rectangle((x, y, x + w - 1, y + h - 1), outline=1, fill=0)
    inner_x = x + 1
    inner_y = y + 1
    inner_w = w - 2
    inner_h = h - 2
    if inner_w < 1 or inner_h < 1:
        return
    samples = list(history)
    if not samples:
        return
    samples = samples[-inner_w:]
    n_show = len(samples)
    baseline = inner_y + inner_h - 1
    base_x = inner_x + inner_w - n_show
    for j, v in enumerate(samples):
        col_x = base_x + j
        bar_h = int(inner_h * max(0.0, min(1.0, v / peak)))
        if bar_h > 0:
            top = baseline - bar_h + 1
            draw.line((col_x, top, col_x, baseline), fill=1)


def _draw_dir_arrow(draw: ImageDraw.ImageDraw, x: int, y: int,
                    direction: str, size: int = 8) -> None:
    """8 x 8 (by default) direction glyph drawn as primitives.

    Avoids font glyphs because the default Pillow bitmap doesn't render
    Unicode arrows. ``unknown`` draws nothing.
    """
    if direction == "charging":
        # Filled up-triangle: apex top, base bottom.
        draw.polygon(
            [(x, y + size - 1),
             (x + size - 1, y + size - 1),
             (x + (size - 1) // 2, y)],
            fill=1,
        )
    elif direction == "discharging":
        # Filled down-triangle: base top, apex bottom.
        draw.polygon(
            [(x, y),
             (x + size - 1, y),
             (x + (size - 1) // 2, y + size - 1)],
            fill=1,
        )
    elif direction == "full":
        # Filled horizontal bar mid-height.
        mid = y + (size - 1) // 2
        draw.rectangle((x + 1, mid - 1, x + size - 2, mid + 1), fill=1)
    elif direction == "idle":
        # Thin line through the middle.
        mid = y + (size - 1) // 2
        draw.line((x, mid, x + size - 1, mid), fill=1)
    # "unknown" draws nothing.


def _humanize_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / 1024 ** 2:.1f}M"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f}K"
    return f"{bytes_per_sec:.0f}B"


class DiskScreen:
    """Disk view: shows the primary block device's model, then per-mount
    usage with a progress bar. Optimized for the common Pi case of one or
    two mounts on a single physical device.
    """

    name = "disk"

    def __init__(self, font: ImageFont.ImageFont, mounts: list[str] | None = None):
        self._font = font
        self._mounts = mounts or ["/", "/boot/firmware"]
        self._model_cache: dict[str, str] = {}

    def _model_for(self, mount: str) -> str:
        if mount in self._model_cache:
            return self._model_cache[mount]
        device = _device_for_mount(mount)
        model = _block_model(device) if device else "(unknown)"
        self._model_cache[mount] = model
        return model

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        draw = ImageDraw.Draw(image)
        w, _ = image.size
        draw.text((0, 0), "disk", fill=1, font=self._font)

        primary = self._mounts[0] if self._mounts else "/"
        model = self._model_for(primary)
        draw.text((0, 11), model[:21], fill=1, font=self._font)

        rows = []
        for m in self._mounts:
            try:
                u = psutil.disk_usage(m)
            except OSError:
                continue
            rows.append((_short_mount(m), u))

        if not rows:
            draw.text((0, 22), "(no mounts)", fill=1, font=self._font)
            return

        for i, (label, u) in enumerate(rows[:2]):
            base_y = 22 + i * 21
            line = (
                f"{label[:5]:<5} "
                f"{_humanize_bytes(u.used)}/{_humanize_bytes(u.total)} "
                f"{u.percent:5.1f}%"
            )
            draw.text((0, base_y), line, fill=1, font=self._font)
            _draw_bar(draw, 0, base_y + 11, w, 7, u.percent / 100.0)


class HelpScreen:
    name = "help"

    def __init__(self, font: ImageFont.ImageFont):
        self._font = font

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        draw = ImageDraw.Draw(image)
        for i, text in enumerate([
            "argon industria",
            "tap  = next",
            "hold = prev",
            "GPIO4 active-low",
            "(both buttons)",
        ]):
            draw.text((0, i * LINE_H), text, fill=1, font=self._font)


def _qr_to_image(payload: str, scale: int = 2, border: int = 1) -> Image.Image:
    """Render a QR for `payload` as a 1-bit Pillow image with white quiet
    zone and black modules — i.e. a normal-polarity QR. Caller decides where
    to paste it.
    """
    qr = segno.make(payload, error="m")
    matrix = list(qr.matrix)
    n = len(matrix)
    side = (n + 2 * border) * scale
    img = Image.new("1", (side, side), 1)  # white background
    d = ImageDraw.Draw(img)
    for y, row in enumerate(matrix):
        for x, val in enumerate(row):
            if val:
                px = (x + border) * scale
                py = (y + border) * scale
                d.rectangle((px, py, px + scale - 1, py + scale - 1), fill=0)
    return img


class HotspotScreen:
    """Render a Wi-Fi join QR for the local AP-mode connection.

    Discovers the active AP via nmcli, builds a WIFI: URI, and renders the
    QR on the left half with SSID/band/channel info on the right. Config is
    cached and re-checked every `refresh_seconds` (cheap; SSID/PSK rarely
    change at runtime).
    """

    name = "wifi"

    def __init__(
        self,
        font: ImageFont.ImageFont,
        connection_name: str | None = None,
        refresh_seconds: float = 10.0,
        station_refresh_seconds: float = 2.0,
    ):
        self._font = font
        self._connection_name = connection_name
        self._refresh_seconds = refresh_seconds
        self._station_refresh_seconds = station_refresh_seconds
        self._next_load = 0.0
        self._next_stations = 0.0
        self._cfg: HotspotConfig | None = None
        self._qr_image: Image.Image | None = None
        self._error: str | None = None
        self._stations: int | None = None

    def _refresh(self) -> None:
        name = self._connection_name or find_active_hotspot()
        if not name:
            self._error = "no AP active"
            self._cfg = None
            self._qr_image = None
            return
        cfg = read_hotspot_config(name)
        if not cfg:
            self._error = "config unread"
            self._cfg = None
            self._qr_image = None
            return
        if cfg == self._cfg and self._qr_image is not None:
            self._error = None
            return
        self._cfg = cfg
        self._error = None
        try:
            self._qr_image = _qr_to_image(wifi_qr_payload(cfg))
        except Exception as e:
            _log.warning("QR build failed: %s", e)
            self._qr_image = None
            self._error = "qr error"

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        now = time.monotonic()
        if now >= self._next_load:
            self._refresh()
            self._next_load = now + self._refresh_seconds
        if self._cfg and now >= self._next_stations:
            self._stations = count_connected_stations(self._cfg.device or "wlan0")
            self._next_stations = now + self._station_refresh_seconds

        draw = ImageDraw.Draw(image)
        w, h = image.size

        if self._error or not self._cfg or not self._qr_image:
            draw.text((0, 0), "wifi", fill=1, font=self._font)
            draw.text((0, LINE_H + 2),
                      self._error or "loading...",
                      fill=1, font=self._font)
            return

        qr = self._qr_image
        qy = (h - qr.height) // 2
        image.paste(qr, (0, qy))

        cfg = self._cfg
        text_x = qr.width + 4
        clients = (
            "Clients: ?" if self._stations is None
            else f"Clients: {self._stations}"
        )
        info_lines = [
            cfg.ssid[:10],
            band_label(cfg.band),
            f"Ch{cfg.channel}",
            cfg.display_auth,
            clients,
        ]
        for i, text in enumerate(info_lines):
            draw.text((text_x, 2 + i * LINE_H + i),
                      text, fill=1, font=self._font)


_FIX_MODE_LABELS = {0: "?", 1: "no", 2: "2D", 3: "3D"}


class GPSScreen:
    """xgps-style GPS view: lock status, lat/lon/alt, uncertainty, PPS,
    and a polar sky map of visible satellites on the right side.

    Spins up a background gpsd client on first render. If gpsd isn't
    running or there's no fix yet, the screen shows a status message.
    """

    name = "gps"

    def __init__(
        self,
        font: ImageFont.ImageFont,
        host: str = "127.0.0.1",
        port: int = 2947,
    ):
        self._font = font
        self._host = host
        self._port = port
        self._client: GPSDClient | None = None

    def _ensure_client(self) -> GPSDClient:
        if self._client is None:
            self._client = GPSDClient(host=self._host, port=self._port)
            self._client.start()
        return self._client

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        client = self._ensure_client()
        draw = ImageDraw.Draw(image)
        w, h = image.size

        tpv = client.tpv
        sky = client.sky
        connected = client.connected
        err = client.error

        # Lock + sat count
        mode = tpv.get("mode", 0) if tpv else 0
        mode_str = _FIX_MODE_LABELS.get(mode, "?")
        sats = (sky or {}).get("satellites") or []
        sats_used = sum(1 for s in sats if s.get("used"))
        sats_total = len(sats)
        draw.text((0, 0),
                  f"GPS {mode_str} {sats_used}/{sats_total}",
                  fill=1, font=self._font)

        if not connected:
            draw.text((0, 11), "no gpsd", fill=1, font=self._font)
            if err:
                draw.text((0, 22), f"({err})", fill=1, font=self._font)
            return

        # Coordinates / alt / accuracy in left column (x=0..63)
        if tpv and tpv.get("lat") is not None and tpv.get("lon") is not None:
            lat = tpv["lat"]; lon = tpv["lon"]
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            draw.text((0, 11), f"{ns}{abs(lat):8.4f}", fill=1, font=self._font)
            draw.text((0, 22), f"{ew}{abs(lon):8.4f}", fill=1, font=self._font)
        else:
            draw.text((0, 11), "no fix", fill=1, font=self._font)

        if tpv:
            alt = tpv.get("altMSL", tpv.get("alt"))
            if alt is not None:
                draw.text((0, 33), f"alt {alt:.0f}m", fill=1, font=self._font)
            eph = tpv.get("eph")
            epv = tpv.get("epv")
            if eph is not None:
                acc = f"h:{eph:.1f}"
                if epv is not None:
                    acc += f" v:{epv:.0f}m"
                else:
                    acc += "m"
                draw.text((0, 44), acc, fill=1, font=self._font)

        # PPS pulse blink — shown lit for 0.5s after each PPS event.
        pps_ns = client.last_pps_ns
        if pps_ns:
            age_s = (time.monotonic_ns() - pps_ns) / 1e9
            label = "PPS *" if age_s < 0.5 else "PPS ."
        else:
            label = "PPS -"
        draw.text((0, 53), label, fill=1, font=self._font)

        # Sky map on right half. Center at (96, 32), outer radius 30 so
        # it just fits the 64-pixel column with a 1-px margin.
        cx, cy, r = 96, 32, 30
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=1, fill=0)
        # Inner ring at 45° elevation
        ir = r // 2
        draw.ellipse((cx - ir, cy - ir, cx + ir, cy + ir), outline=1, fill=0)
        # Tiny tick at North so map orientation is unambiguous
        draw.line((cx, cy - r - 1, cx, cy - r + 2), fill=1)

        for s in sats:
            az = s.get("az")
            el = s.get("el")
            if az is None or el is None or el < 0:
                continue
            sat_r = (1.0 - el / 90.0) * r
            sx = int(cx + sat_r * math.sin(math.radians(az)))
            sy = int(cy - sat_r * math.cos(math.radians(az)))
            if s.get("used"):
                draw.ellipse((sx - 1, sy - 1, sx + 1, sy + 1), fill=1)
            else:
                draw.point((sx, sy), fill=1)


class ScreenCarousel:
    """Owns a list of screens and renders the active one, with an "n/N"
    index hint in the top-right corner.
    """

    def __init__(self, screens: list[Screen], font: ImageFont.ImageFont):
        if not screens:
            raise ValueError("at least one screen required")
        self._screens = screens
        self._font = font
        self._index = 0

    @property
    def current(self):
        return self._screens[self._index]

    def __len__(self) -> int:
        return len(self._screens)

    def next(self) -> None:
        self._index = (self._index + 1) % len(self._screens)

    def prev(self) -> None:
        self._index = (self._index - 1) % len(self._screens)

    def render(self, image: Image.Image, snap: SystemSnapshot) -> None:
        screen = self._screens[self._index]
        screen.render(image, snap)

        draw = ImageDraw.Draw(image)
        w, _ = image.size

        hint = f"{self._index + 1}/{len(self._screens)}"
        bbox = self._font.getbbox(hint)
        hint_w = bbox[2] - bbox[0]
        draw.rectangle((w - hint_w - 2, 0, w - 1, 9), fill=0)
        draw.text((w - hint_w - 1, 0), hint, fill=1, font=self._font)

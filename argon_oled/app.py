"""Main runtime loop. Renders the active screen, drains button events.

Two cadences:
- metrics refresh   (~1s)    — pulls fresh psutil data
- frame render      (~100ms) — paints the OLED
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import subprocess
import sys
import time

from PIL import Image, ImageFont
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306

from . import metrics
from .buttons import ButtonEvent, ButtonWatcher
from .screens import (
    DiskScreen,
    GPSScreen,
    HelpScreen,
    HotspotScreen,
    NetworkScreen,
    ScreenCarousel,
    StatusScreen,
)

log = logging.getLogger("argon_oled")


class App:
    def __init__(
        self,
        device,
        carousel: ScreenCarousel,
        button_events: queue.Queue[ButtonEvent] | None,
        frame_period: float = 0.10,
        metrics_period: float = 1.0,
    ):
        self.device = device
        self.carousel = carousel
        self.button_events = button_events
        self.frame_period = frame_period
        self.metrics_period = metrics_period
        self._stop = False

    def stop(self, *_args) -> None:
        self._stop = True

    def _drain_buttons(self) -> None:
        if self.button_events is None:
            return
        while True:
            try:
                ev = self.button_events.get_nowait()
            except queue.Empty:
                return
            if ev is ButtonEvent.SHORT:
                self.carousel.next()
                log.info("-> screen %s", self.carousel.current.name)
            elif ev is ButtonEvent.LONG:
                self.carousel.prev()
                log.info("-> screen %s (prev)", self.carousel.current.name)

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        snap = metrics.gather()
        last_metrics = time.monotonic()
        log.info("Render loop @ %.0fms; %d screen(s)",
                 self.frame_period * 1000, len(self.carousel))

        try:
            while not self._stop:
                now = time.monotonic()
                if now - last_metrics >= self.metrics_period:
                    snap = metrics.gather()
                    last_metrics = now

                self._drain_buttons()

                image = Image.new("1", self.device.size, 0)
                self.carousel.render(image, snap)
                self.device.display(image)

                time.sleep(self.frame_period)
        finally:
            log.info("Stopping; clearing display.")
            try:
                self.device.clear()
            except Exception as e:
                log.warning("Clear on shutdown failed: %s", e)


def _i2cdetect_dump(port: int) -> str:
    try:
        out = subprocess.run(
            ["i2cdetect", "-y", str(port)],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout or out.stderr
    except FileNotFoundError:
        return "(i2cdetect not installed)"


def _open_device(port: int, address: int):
    try:
        serial = i2c(port=port, address=address)
        return ssd1306(serial, width=128, height=64)
    except Exception as e:
        log.error("SSD1306 init failed on i2c-%d @ 0x%02X: %s", port, address, e)
        log.error("i2cdetect -y %d:\n%s", port, _i2cdetect_dump(port))
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="argon-oled-status")
    p.add_argument("--i2c-port", type=int, default=1)
    p.add_argument("--i2c-address", type=lambda x: int(x, 0), default=0x3C)
    p.add_argument("--gpiochip", default="/dev/gpiochip0")
    p.add_argument("--button-line", type=int, default=4)
    p.add_argument("--long-press-ms", type=int, default=700)
    p.add_argument("--debounce-ms", type=int, default=50)
    p.add_argument("--no-buttons", action="store_true",
                   help="disable button watcher (useful if line is contested)")
    p.add_argument("--frame-ms", type=int, default=100)
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--hotspot-connection",
        default=None,
        help="NetworkManager connection name for the AP (auto-detect if omitted)",
    )
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    device = _open_device(args.i2c_port, args.i2c_address)

    font = ImageFont.load_default()
    carousel = ScreenCarousel(
        screens=[
            StatusScreen(font),
            NetworkScreen(font),
            HotspotScreen(font, connection_name=args.hotspot_connection),
            DiskScreen(font),
            GPSScreen(font),
            HelpScreen(font),
        ],
        font=font,
    )

    events: queue.Queue[ButtonEvent] | None = None
    watcher: ButtonWatcher | None = None
    if not args.no_buttons:
        events = queue.Queue()
        watcher = ButtonWatcher(
            events=events,
            chip_path=args.gpiochip,
            line=args.button_line,
            long_press_ms=args.long_press_ms,
            debounce_ms=args.debounce_ms,
        )
        watcher.start()

    app = App(
        device, carousel, events,
        frame_period=args.frame_ms / 1000.0,
    )
    try:
        app.run()
    finally:
        if watcher is not None:
            watcher.stop()
            watcher.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""Phase 3 button discovery probe.

Watches every unclaimed GPIO header line on gpiochip0 (GPIO4..GPIO27, skipping
SDA/SCL on GPIO2/3) for edge events with PULL_UP bias and BOTH-edge detection.
Concurrently polls a read_byte from the SSD1306 at 0x3C to see if button state
surfaces over I2C.

Run interactively, press buttons in a known pattern, observe output, Ctrl+C to
get a per-line summary including pulse-width distribution.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import gpiod
from gpiod.line import Bias, Direction, Edge
from smbus2 import SMBus

GPIOCHIP = "/dev/gpiochip0"
CANDIDATE_LINES = list(range(4, 28))  # GPIO4..GPIO27; SDA(2)/SCL(3) excluded
I2C_PORT = 1
I2C_ADDRESS = 0x3C
CONSUMER = "argon-button-probe"
I2C_POLL_PERIOD = 0.05

log = logging.getLogger("probe")


@dataclass
class LineStats:
    edges: int = 0
    last_value: int = -1
    last_change_ns: int = 0
    low_pulses_ns: list[int] = field(default_factory=list)
    high_pulses_ns: list[int] = field(default_factory=list)


def filter_claimable(chip_path: str, lines: list[int]) -> list[int]:
    """Return the subset of `lines` we can request as inputs without conflict."""
    settings = gpiod.LineSettings(
        direction=Direction.INPUT,
        bias=Bias.PULL_UP,
        edge_detection=Edge.BOTH,
    )
    ok: list[int] = []
    for line in lines:
        try:
            r = gpiod.request_lines(
                chip_path,
                consumer=f"{CONSUMER}-probe",
                config={line: settings},
            )
            r.release()
            ok.append(line)
        except OSError as e:
            log.warning("skip GPIO%d (cannot claim): %s", line, e)
    return ok


class I2CWatcher(threading.Thread):
    """Polls read_byte from 0x3C and logs any value changes."""

    def __init__(self, port: int, addr: int, stop: threading.Event,
                 period: float = I2C_POLL_PERIOD):
        super().__init__(daemon=True, name="i2c-watcher")
        self.port = port
        self.addr = addr
        self.stop = stop
        self.period = period
        self.changes = 0
        self.read_errors = 0
        self.last: int | None = None

    def run(self) -> None:
        try:
            bus = SMBus(self.port)
        except OSError as e:
            log.error("I2C open failed: %s", e)
            return
        try:
            while not self.stop.is_set():
                try:
                    b = bus.read_byte(self.addr)
                except OSError:
                    b = None
                    self.read_errors += 1
                if self.last is not None and b != self.last:
                    log.info("[i2c 0x%02X] read_byte: %s -> %s",
                             self.addr,
                             "ERR" if self.last is None else f"0x{self.last:02X}",
                             "ERR" if b is None else f"0x{b:02X}")
                    self.changes += 1
                self.last = b
                self.stop.wait(self.period)
        finally:
            bus.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Filtering claimable lines from GPIO%d..GPIO%d on %s",
             CANDIDATE_LINES[0], CANDIDATE_LINES[-1], GPIOCHIP)
    claimable = filter_claimable(GPIOCHIP, CANDIDATE_LINES)
    if not claimable:
        log.error("No GPIO lines could be claimed; aborting.")
        return 1
    log.info("Watching: %s", ", ".join(f"GPIO{l}" for l in claimable))

    settings = gpiod.LineSettings(
        direction=Direction.INPUT,
        bias=Bias.PULL_UP,
        edge_detection=Edge.BOTH,
    )
    request = gpiod.request_lines(
        GPIOCHIP,
        consumer=CONSUMER,
        config={line: settings for line in claimable},
    )

    initial = request.get_values(claimable)
    log.info("baseline: %s", ", ".join(
        f"GPIO{l}={v.value}" for l, v in zip(claimable, initial)
    ))

    stats: dict[int, LineStats] = defaultdict(LineStats)
    for line, val in zip(claimable, initial):
        stats[line].last_value = val.value

    stop = threading.Event()

    def handle_sig(*_):
        stop.set()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    i2c_watcher = I2CWatcher(I2C_PORT, I2C_ADDRESS, stop)
    i2c_watcher.start()

    log.info("=== Press buttons. Ctrl+C to stop. ===")
    try:
        while not stop.is_set():
            if request.wait_edge_events(timedelta(milliseconds=500)):
                for ev in request.read_edge_events():
                    line = ev.line_offset
                    rising = (ev.event_type == ev.Type.RISING_EDGE)
                    et = "RISING " if rising else "FALLING"
                    ts = ev.timestamp_ns
                    s = stats[line]
                    pulse_str = ""
                    if s.last_change_ns:
                        delta_ns = ts - s.last_change_ns
                        if rising and s.last_value == 0:
                            s.low_pulses_ns.append(delta_ns)
                            pulse_str = f"  low_pulse={delta_ns / 1e6:.1f}ms"
                        elif not rising and s.last_value == 1:
                            s.high_pulses_ns.append(delta_ns)
                            pulse_str = f"  high_pulse={delta_ns / 1e6:.1f}ms"
                    log.info("GPIO%-2d %s%s", line, et, pulse_str)
                    s.edges += 1
                    s.last_change_ns = ts
                    s.last_value = 1 if rising else 0
    finally:
        stop.set()
        try:
            request.release()
        except Exception:
            pass
        i2c_watcher.join(timeout=1.0)

    # Summary
    log.info("=== Summary ===")
    active = [l for l in stats if stats[l].edges > 0]
    if not active:
        log.info("No edges detected on any GPIO line.")
    for line in sorted(active):
        s = stats[line]
        lows = sorted(round(p / 1e6, 1) for p in s.low_pulses_ns)
        highs = sorted(round(p / 1e6, 1) for p in s.high_pulses_ns)
        log.info("GPIO%-2d edges=%d  low_pulses_ms=%s  high_pulses_ms=%s",
                 line, s.edges, lows, highs)
    log.info("I2C 0x%02X read_byte changes=%d  read_errors=%d",
             I2C_ADDRESS, i2c_watcher.changes, i2c_watcher.read_errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())

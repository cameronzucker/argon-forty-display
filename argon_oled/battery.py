"""X1207 UPS HAT monitor.

Background thread that samples the MAX17040 fuel gauge over I2C-1 and watches
GPIO6 (debounced) for power-source change events. Publishes an atomic
``BatteryStatus`` snapshot the render loop can read lock-free.

The MAX17040 silicon on the X1207 (VERSION = 0x0002) has no CRATE register, so
charge direction is derived from a rolling SOC slope rather than a single
register read. GPIO6 is bouncy and can latch silent under rapid input cycling
(documented in README's Hardware reference); it only feeds the source-hint
label, never the direction.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

# MAX17040 register addresses (datasheet § Register Summary).
VCELL_REG = 0x02
SOC_REG = 0x04
VERSION_REG = 0x08

# Voltage scaling: VCELL is the 12 MSBs of the 16-bit word, each LSB is
# 1.25 mV. Treating the whole word as a 16-bit value scaled by 78.125 µV
# yields the same number (the lower 4 bits are always zero).
VCELL_LSB_UV = 78.125

Direction = Literal["charging", "discharging", "idle", "full", "unknown"]
SourceHint = Literal["external", "battery", "?"]


@dataclass(frozen=True)
class BatteryStatus:
    """Atomic snapshot of the UPS state. Reference-swapped on each sample.

    Readers can fetch ``watcher.status`` without locking — Python name binding
    is atomic, and this dataclass is frozen.
    """
    detected: bool
    stale: bool
    voltage_v: float | None
    soc_pct: float | None        # clamped 0-100 for display
    soc_pct_raw: float | None    # unclamped (>100 is normal at full charge)
    direction: Direction
    source_hint: SourceHint
    eta_seconds: int | None
    slope_pct_per_min: float | None
    soc_history: tuple[float, ...]


# Initial status used before the watcher has read anything.
EMPTY_STATUS = BatteryStatus(
    detected=False, stale=False,
    voltage_v=None, soc_pct=None, soc_pct_raw=None,
    direction="unknown", source_hint="?",
    eta_seconds=None, slope_pct_per_min=None,
    soc_history=(),
)


def swap16(x: int) -> int:
    """Swap the two bytes of a 16-bit word.

    The MAX17040 transmits MSB first; SMBus's ``read_word_data`` returns the
    little-endian interpretation, so each word must be byte-swapped to get
    the chip's intended value.
    """
    return ((x & 0xFF) << 8) | ((x >> 8) & 0xFF)


def parse_voltage(swapped_word: int) -> float:
    """Convert a byte-swapped MAX17040 VCELL word to volts."""
    return swapped_word * VCELL_LSB_UV / 1_000_000.0


def parse_soc(swapped_word: int) -> float:
    """Convert a byte-swapped MAX17040 SOC word to percent.

    Upper byte = whole percent, lower byte = fractional /256.
    """
    return swapped_word / 256.0


def compute_slope(samples: list[tuple[float, float]],
                  window_s: float) -> float | None:
    """Linear least-squares slope of SOC over time, in %/minute.

    ``samples`` is ``[(monotonic_seconds, soc_pct), ...]`` in any order.
    Only samples within ``window_s`` of the most recent timestamp are used.
    Returns None if fewer than 3 samples fall in the window or all samples
    share the same timestamp.

    Time values are shifted to start at zero before the least-squares math
    so the formula stays numerically robust — raw monotonic timestamps run
    into the millions and would lose low-order bits to cancellation.
    """
    if not samples:
        return None
    t_latest = max(t for t, _ in samples)
    in_window = [(t, v) for t, v in samples if t_latest - t <= window_s]
    n = len(in_window)
    if n < 3:
        return None
    t0 = min(t for t, _ in in_window)
    ts = [t - t0 for t, _ in in_window]
    ys = [y for _, y in in_window]
    sum_t = sum(ts)
    sum_y = sum(ys)
    sum_ty = sum(t * y for t, y in zip(ts, ys))
    sum_tt = sum(t * t for t in ts)
    denom = n * sum_tt - sum_t * sum_t
    if denom <= 0:
        return None
    slope_per_s = (n * sum_ty - sum_t * sum_y) / denom
    return slope_per_s * 60.0


def classify_direction(slope_pct_per_min: float | None,
                       soc_pct: float) -> Direction:
    """Direction-inference table, per spec § 3.2."""
    if slope_pct_per_min is None:
        return "unknown"
    s = slope_pct_per_min
    if s > 0.1 and soc_pct < 99:
        return "charging"
    if s < -0.1:
        return "discharging"
    if abs(s) <= 0.1 and soc_pct >= 98:
        return "full"
    return "idle"


def compute_eta_seconds(direction: Direction,
                        slope_pct_per_min: float | None,
                        soc_pct: float | None) -> int | None:
    """Seconds until full (charging) or empty (discharging).

    Returns None if direction is full/idle/unknown, slope is too small to be
    meaningful (|s| < 0.2 %/min), or any input is None.
    """
    if (direction in ("full", "idle", "unknown")
            or slope_pct_per_min is None
            or soc_pct is None
            or abs(slope_pct_per_min) < 0.2):
        return None
    if direction == "charging":
        return max(0, round((100.0 - soc_pct) / slope_pct_per_min * 60.0))
    if direction == "discharging":
        return max(0, round(soc_pct / -slope_pct_per_min * 60.0))
    return None


import threading
from datetime import timedelta

import gpiod
from gpiod.line import Bias, Direction as LineDirection, Edge
from smbus2 import SMBus


DEFAULT_I2C_BUS = 1
DEFAULT_I2C_ADDRESS = 0x36
DEFAULT_GPIOCHIP = "/dev/gpiochip0"
DEFAULT_GPIO_LINE = 6
DEFAULT_SAMPLE_PERIOD_S = 5.0
DEFAULT_DEBOUNCE_MS = 750
DEFAULT_SLOPE_WINDOW_S = 60.0
DEFAULT_HISTORY_PERIOD_S = 10.0
DEFAULT_HISTORY_LEN = 128
STALE_AFTER_S = 15.0
CONSUMER = "argon-oled-battery"


class BatteryWatcher(threading.Thread):
    """Background sampler for the X1207 UPS HAT.

    Run loop: every ``sample_period_s``, read VCELL+SOC over I2C; between
    those ticks, block on GPIO6 edge events with a short timeout so the
    loop stays responsive. Each pass rebuilds and atomically publishes a
    new ``BatteryStatus``.
    """

    def __init__(
        self,
        i2c_bus: int = DEFAULT_I2C_BUS,
        i2c_address: int = DEFAULT_I2C_ADDRESS,
        gpio_chip: str = DEFAULT_GPIOCHIP,
        gpio_line: int = DEFAULT_GPIO_LINE,
        sample_period_s: float = DEFAULT_SAMPLE_PERIOD_S,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        slope_window_s: float = DEFAULT_SLOPE_WINDOW_S,
        history_period_s: float = DEFAULT_HISTORY_PERIOD_S,
        history_len: int = DEFAULT_HISTORY_LEN,
    ):
        super().__init__(daemon=True, name="argon-oled-battery")
        self.i2c_bus = i2c_bus
        self.i2c_address = i2c_address
        self.gpio_chip = gpio_chip
        self.gpio_line = gpio_line
        self.sample_period_s = sample_period_s
        self.debounce_ns = debounce_ms * 1_000_000
        self.slope_window_s = slope_window_s
        self.history_period_s = history_period_s
        self.history_len = history_len

        self._stop = threading.Event()
        self._slope_samples: deque[tuple[float, float]] = deque(maxlen=200)
        self._history: deque[float] = deque(maxlen=history_len)
        self._last_history_ts: float = 0.0
        self._last_ok_ts: float = 0.0
        self._fail_streak: int = 0
        # GPIO debounce state.
        self._gpio_request: gpiod.LineRequest | None = None
        self._gpio_level: int | None = None  # last debounced stable level
        self._pending_level: int | None = None
        self._pending_since_ns: int = 0
        self._gpio_unavailable: bool = False
        self._last_logged_contradiction: tuple[str, str] | None = None

        self.status: BatteryStatus = EMPTY_STATUS
        self._version_logged = False

    def stop(self) -> None:
        self._stop.set()

    # ---- I2C ---------------------------------------------------------------

    def _open_bus(self) -> SMBus | None:
        try:
            return SMBus(self.i2c_bus)
        except OSError as e:
            log.error("Cannot open I2C bus %d: %s", self.i2c_bus, e)
            return None

    def _read_word(self, bus: SMBus, reg: int) -> int | None:
        try:
            return swap16(bus.read_word_data(self.i2c_address, reg))
        except OSError as e:
            log.debug("I2C read of reg 0x%02X failed: %s", reg, e)
            return None

    def _sample_i2c(self, bus: SMBus) -> tuple[float | None, float | None]:
        if not self._version_logged:
            ver = self._read_word(bus, VERSION_REG)
            if ver is not None:
                self._version_logged = True
                family = "MAX17048-style" if ver >= 0x0010 else "MAX17040-style"
                log.info("Fuel gauge VERSION=0x%04X (%s)", ver, family)
        vcell_raw = self._read_word(bus, VCELL_REG)
        soc_raw = self._read_word(bus, SOC_REG)
        if vcell_raw is None or soc_raw is None:
            return None, None
        return parse_voltage(vcell_raw), parse_soc(soc_raw)

    # ---- GPIO --------------------------------------------------------------

    def _claim_gpio(self) -> None:
        settings = gpiod.LineSettings(
            direction=LineDirection.INPUT,
            bias=Bias.PULL_UP,
            edge_detection=Edge.BOTH,
        )
        try:
            self._gpio_request = gpiod.request_lines(
                self.gpio_chip,
                consumer=CONSUMER,
                config={self.gpio_line: settings},
            )
            initial = self._gpio_request.get_values([self.gpio_line])
            self._gpio_level = initial[0].value
            log.info("Watching GPIO%d (initial=%d)",
                     self.gpio_line, self._gpio_level)
        except OSError as e:
            log.warning(
                "Cannot claim GPIO%d on %s (%s); source_hint will be '?'",
                self.gpio_line, self.gpio_chip, e,
            )
            self._gpio_request = None
            self._gpio_unavailable = True

    def _drain_gpio_edges(self) -> None:
        if self._gpio_request is None:
            return
        if not self._gpio_request.wait_edge_events(timedelta(milliseconds=200)):
            return
        for ev in self._gpio_request.read_edge_events():
            level = 1 if ev.event_type == ev.Type.RISING_EDGE else 0
            self._pending_level = level
            self._pending_since_ns = ev.timestamp_ns

    def _resolve_gpio_level(self) -> None:
        """Promote pending edge to stable level if debounce window has passed."""
        if self._pending_level is None:
            return
        # We compare against monotonic_ns; libgpiod timestamps are also
        # CLOCK_MONOTONIC on Linux, so the units match.
        if time.monotonic_ns() - self._pending_since_ns >= self.debounce_ns:
            self._gpio_level = self._pending_level
            self._pending_level = None

    def _source_hint(self) -> SourceHint:
        if self._gpio_unavailable or self._gpio_level is None:
            return "?"
        if self._pending_level is not None:
            return "?"
        return "external" if self._gpio_level == 1 else "battery"

    # ---- run loop ----------------------------------------------------------

    def _build_status(self, voltage: float | None, soc_raw_pct: float | None,
                      detected: bool) -> BatteryStatus:
        soc_clamped = (
            max(0.0, min(100.0, soc_raw_pct)) if soc_raw_pct is not None else None
        )
        slope = compute_slope(list(self._slope_samples), self.slope_window_s)
        direction: Direction = (
            classify_direction(slope, soc_clamped) if soc_clamped is not None
            else "unknown"
        )
        eta = compute_eta_seconds(direction, slope, soc_clamped)
        source_hint = self._source_hint()
        # Log a one-shot INFO when source_hint and direction contradict, so the
        # X1207 latch state is visible in journalctl without spamming the log.
        contradiction: tuple[str, str] | None = None
        if (source_hint == "external" and direction == "discharging"
                and slope is not None and slope < -0.5):
            contradiction = ("external", "discharging")
        elif source_hint == "battery" and direction == "charging":
            contradiction = ("battery", "charging")
        if contradiction != self._last_logged_contradiction:
            if contradiction is not None:
                log.info("source_hint=%s but direction=%s — possible X1207 latch "
                         "state or rate transient", *contradiction)
            self._last_logged_contradiction = contradiction
        stale = (
            self._last_ok_ts > 0
            and time.monotonic() - self._last_ok_ts > STALE_AFTER_S
        )
        return BatteryStatus(
            detected=detected,
            stale=stale,
            voltage_v=voltage,
            soc_pct=soc_clamped,
            soc_pct_raw=soc_raw_pct,
            direction=direction,
            source_hint=source_hint,
            eta_seconds=eta,
            slope_pct_per_min=slope,
            soc_history=tuple(self._history),
        )

    def _sample_once(self, bus: SMBus) -> None:
        voltage, soc_raw_pct = self._sample_i2c(bus)
        now_mono = time.monotonic()
        if voltage is None or soc_raw_pct is None:
            self._fail_streak += 1
            self.status = self._build_status(
                self.status.voltage_v, self.status.soc_pct_raw,
                detected=self.status.detected,
            )
            return
        soc_clamped = max(0.0, min(100.0, soc_raw_pct))
        self._slope_samples.append((now_mono, soc_clamped))
        if (self._last_history_ts == 0.0
                or now_mono - self._last_history_ts >= self.history_period_s):
            self._history.append(soc_clamped)
            self._last_history_ts = now_mono
        self._fail_streak = 0
        self._last_ok_ts = now_mono
        self.status = self._build_status(voltage, soc_raw_pct, detected=True)

    def run(self) -> None:
        self._claim_gpio()
        bus = self._open_bus()
        if bus is None:
            log.error("Battery watcher cannot start without I2C bus")
            return
        # First sample synchronously so the screen has truth on first frame.
        self._sample_once(bus)
        next_sample_at = time.monotonic() + self.sample_period_s
        try:
            while not self._stop.is_set():
                self._drain_gpio_edges()
                self._resolve_gpio_level()
                now = time.monotonic()
                if now >= next_sample_at:
                    self._sample_once(bus)
                    next_sample_at = now + self.sample_period_s
                else:
                    # If GPIO line wasn't claimed, wait_edge_events isn't sleeping
                    # for us; do it manually so we don't spin.
                    if self._gpio_request is None:
                        self._stop.wait(0.2)
                # If the I2C side has been failing, rebuild the status to
                # propagate the stale flag without waiting for the next sample.
                if self._fail_streak >= 3:
                    self.status = self._build_status(
                        self.status.voltage_v, self.status.soc_pct_raw,
                        detected=self.status.detected,
                    )
        finally:
            try:
                bus.close()
            except Exception:
                pass
            if self._gpio_request is not None:
                try:
                    self._gpio_request.release()
                except Exception:
                    pass
            log.info("Battery watcher stopped")

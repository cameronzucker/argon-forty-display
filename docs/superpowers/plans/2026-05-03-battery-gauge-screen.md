# Battery Gauge Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a seventh OLED screen displaying live X1207 UPS state — SOC, voltage, charge/discharge direction, source hint, full-width SOC bar, and a 21-minute SOC sparkline — at carousel index 1 (right after StatusScreen).

**Architecture:** New `argon_oled/battery.py` module exposes a `BatteryWatcher` background thread (mirrors the `GPSDClient` pattern) that owns the I²C bus session and a libgpiod-claimed GPIO6, atomically publishing a frozen `BatteryStatus` snapshot the render loop reads each frame. Direction is inferred from a 60 s rolling SOC slope (the MAX17040 has no `CRATE` register); GPIO6 (debounced 750 ms) only feeds the source-hint label, never overriding the trend, so the documented X1207 latch behavior cannot corrupt the displayed direction.

**Tech Stack:** Python 3.13, `smbus2` (from `python3-smbus2` system-site-package — same convention as `gpiod`), `gpiod` v2.x (libgpiod), Pillow, `threading`, `dataclasses`, `collections.deque`.

**Spec:** [docs/superpowers/specs/2026-05-03-battery-gauge-screen-design.md](../specs/2026-05-03-battery-gauge-screen-design.md)

**Spec deviation noted up front:** Spec § 3.5 calls for adding `smbus2` to `pyproject.toml`. This plan **does not** add it, because doing so contradicts the project's existing convention: `gpiod` is also a kernel-bound Python wrapper sourced from `python3-libgpiod` via `--system-site-packages`, and is intentionally not in `pyproject.toml`. `smbus2` is already used by `scripts/probe_buttons.py` and `scripts/probe_x1207.py` without being declared. Treating `smbus2` the same way keeps the pattern consistent.

**Testing convention:** This project has no automated test suite (per HANDOFF.md, "Hardware-touching code stays manual"). Each task uses inline REPL verification (`uv run python -c "..."`) for pure-Python logic and on-hardware smoke tests for hardware-touching code. No `pytest` is added.

**Working environment note:** Use `uv run python ...` for all Python invocation. If `VIRTUAL_ENV` is set to something other than the project's `.venv`, `unset VIRTUAL_ENV` first to avoid uv complaining and falling back to a sync attempt.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `argon_oled/battery.py` | **Create** | `BatteryStatus` dataclass + pure helpers (swap16, voltage/SOC parsers, slope, direction, ETA) + `BatteryWatcher` thread. ~250 lines. |
| `argon_oled/screens.py` | **Modify** | Append `_draw_dir_arrow` helper + `BatteryScreen` class. No edits to existing classes. |
| `argon_oled/app.py` | **Modify** | Add `--no-battery`, `--battery-*` CLI flags; instantiate `BatteryWatcher`; insert `BatteryScreen` at carousel index 1; lifecycle in `finally`. |

`pyproject.toml`, `argon_oled/__init__.py`, `argon_oled/__main__.py`, and the systemd unit are **not** modified.

---

## Task 1: Pure helpers — `swap16`, voltage/SOC parsing, slope, direction, ETA

**Files:**
- Create: `argon_oled/battery.py`

This task lays down the new module file with only its pure-Python utility functions and the `BatteryStatus` dataclass. No threading, no I²C, no GPIO yet — those come in Task 2. By the end of this task, the helpers can be imported and exercised in isolation.

- [ ] **Step 1: Write the verification command**

This is the failing-test equivalent. It exercises every helper and the dataclass shape. Save this string somewhere mentally; you'll re-run it after each implementation step.

```bash
unset VIRTUAL_ENV
uv run python -c "
from argon_oled import battery as b

# swap16
assert b.swap16(0xD3B0) == 0xB0D3, 'swap16 broke'
assert b.swap16(0x0102) == 0x0201

# parse_voltage: from probe data, 0xD3B0 ~ 4.234V
assert abs(b.parse_voltage(0xD3B0) - 4.234) < 0.005, 'voltage parse off'
# parse_soc: 0x6617 ~ 102.09%
assert abs(b.parse_soc(0x6617) - 102.09) < 0.05, 'soc parse off'

# compute_slope: 6 samples, +1.0 %/min trend (0.1 %/sample at 6 s spacing -> 1.0 %/min)
import time
now = time.monotonic()
samples = [(now - 30 + i*6, 80.0 + i*1.0) for i in range(6)]  # 80,81,...85 over 30 s
slope = b.compute_slope(samples, window_s=60.0)
assert slope is not None and abs(slope - 10.0) < 0.5, f'slope off: {slope}'

# compute_slope: too few samples
assert b.compute_slope([], 60.0) is None
assert b.compute_slope([(now, 50.0), (now+5, 51.0)], 60.0) is None

# classify_direction
assert b.classify_direction(slope_pct_per_min=0.5, soc_pct=80.0) == 'charging'
assert b.classify_direction(slope_pct_per_min=0.05, soc_pct=99.5) == 'full'
assert b.classify_direction(slope_pct_per_min=-0.5, soc_pct=80.0) == 'discharging'
assert b.classify_direction(slope_pct_per_min=0.05, soc_pct=80.0) == 'idle'
assert b.classify_direction(slope_pct_per_min=None, soc_pct=80.0) == 'unknown'

# compute_eta_seconds
assert b.compute_eta_seconds('charging', 1.0, 80.0) == 1200  # (100-80)/1.0 = 20 min
assert b.compute_eta_seconds('discharging', -2.0, 80.0) == 2400  # 80/2 = 40 min
assert b.compute_eta_seconds('charging', 0.05, 80.0) is None  # rate too low
assert b.compute_eta_seconds('full', 0.0, 100.0) is None
assert b.compute_eta_seconds('unknown', None, 80.0) is None

# BatteryStatus is a frozen dataclass with the spec'd fields
s = b.BatteryStatus(
    detected=True, stale=False,
    voltage_v=4.10, soc_pct=89.0, soc_pct_raw=89.0,
    direction='discharging', source_hint='battery',
    eta_seconds=14400, slope_pct_per_min=-0.4,
    soc_history=(89.5, 89.4, 89.2, 89.0),
)
assert s.detected is True
assert s.soc_pct == 89.0

print('battery helpers OK')
"
```

- [ ] **Step 2: Run the verification — expect ImportError**

Run the command above. Expected output: `ModuleNotFoundError: No module named 'argon_oled.battery'`. This confirms we haven't accidentally got an old version sitting around.

- [ ] **Step 3: Write `argon_oled/battery.py` with helpers and dataclass**

```python
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
    Returns None if fewer than 3 samples fall in the window.
    """
    if not samples:
        return None
    t_latest = max(t for t, _ in samples)
    in_window = [(t, v) for t, v in samples if t_latest - t <= window_s]
    n = len(in_window)
    if n < 3:
        return None
    # Least-squares: slope = (n*sum(ty) - sum(t)*sum(y)) / (n*sum(t^2) - sum(t)^2)
    sum_t = sum(t for t, _ in in_window)
    sum_y = sum(y for _, y in in_window)
    sum_ty = sum(t * y for t, y in in_window)
    sum_tt = sum(t * t for t, _ in in_window)
    denom = n * sum_tt - sum_t * sum_t
    if denom == 0:
        return None
    slope_per_s = (n * sum_ty - sum_t * sum_y) / denom
    return slope_per_s * 60.0  # %/s -> %/min


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
        return round((100.0 - soc_pct) / slope_pct_per_min * 60.0)
    if direction == "discharging":
        return round(soc_pct / -slope_pct_per_min * 60.0)
    return None
```

- [ ] **Step 4: Re-run the verification — expect success**

Run the same command from Step 1. Expected output: `battery helpers OK`. If anything fails, fix the implementation and rerun.

- [ ] **Step 5: Commit**

```bash
cd /home/administrator/Code/argon-forty-display
git add argon_oled/battery.py
git -c user.name="Cameron Zucker" -c user.email="cameronzucker@gmail.com" commit -m "$(cat <<'EOF'
Add BatteryStatus dataclass and pure helpers in argon_oled.battery

Module skeleton with the frozen status dataclass, MAX17040 register parsers
(swap16, parse_voltage, parse_soc), rolling SOC slope (least-squares),
direction inference, and ETA computation. No threading or hardware access
yet — those come with BatteryWatcher in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `BatteryWatcher` thread with I²C sampling and debounced GPIO6

**Files:**
- Modify: `argon_oled/battery.py` (append to)

This task adds the actual hardware-touching watcher. Implementation lives in the same module so all UPS concerns are colocated, like `gps.py` keeps its `GPSDClient` alongside related utilities.

- [ ] **Step 1: Write the on-hardware verification command**

```bash
unset VIRTUAL_ENV
uv run python -c "
import time
from argon_oled.battery import BatteryWatcher

w = BatteryWatcher()
w.start()
try:
    # Allow time for two sample cycles + GPIO claim.
    time.sleep(12.0)
    s = w.status
    print('detected:', s.detected)
    print('voltage:', s.voltage_v)
    print('soc_pct:', s.soc_pct)
    print('source_hint:', s.source_hint)
    print('history len:', len(s.soc_history))
    assert s.detected is True, 'X1207 should be detected on this Pi'
    assert s.voltage_v is not None and 3.0 < s.voltage_v < 4.5, f'unhealthy voltage: {s.voltage_v}'
    assert s.soc_pct is not None and 0.0 <= s.soc_pct <= 100.0, f'soc out of range: {s.soc_pct}'
    assert s.source_hint in ('external', 'battery', '?')
    print('watcher OK')
finally:
    w.stop()
    w.join(timeout=2.0)
"
```

- [ ] **Step 2: Run the verification — expect AttributeError**

Expected: `AttributeError: module 'argon_oled.battery' has no attribute 'BatteryWatcher'`. Confirms there's no stale class around.

- [ ] **Step 3: Implement `BatteryWatcher` — append to `argon_oled/battery.py`**

Append the following block at the end of the file (after `compute_eta_seconds`):

```python
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
```

- [ ] **Step 4: Re-run the verification on hardware**

Run the command from Step 1. Expected output: lines showing `detected: True`, `voltage: 4.x`, `soc_pct: <number>`, `source_hint: external` (assuming PoE is plugged in right now), `history len: 1` (only one history sample inside 12 s — the very first), and `watcher OK`. If `source_hint` is `?`, GPIO6 wasn't claimable; check that no other process holds it.

- [ ] **Step 5: Smoke-test the SOC history accumulation**

```bash
uv run python -c "
import time
from argon_oled.battery import BatteryWatcher
w = BatteryWatcher(history_period_s=2.0, sample_period_s=2.0)
w.start()
try:
    time.sleep(15.0)
    print('history:', w.status.soc_history)
    assert len(w.status.soc_history) >= 5, 'history did not accumulate'
    print('history accumulation OK')
finally:
    w.stop(); w.join(timeout=2.0)
"
```

Expected: a tuple of 5+ floats, all near each other (battery is currently full and stable), then `history accumulation OK`.

- [ ] **Step 6: Commit**

```bash
git add argon_oled/battery.py
git -c user.name="Cameron Zucker" -c user.email="cameronzucker@gmail.com" commit -m "$(cat <<'EOF'
Add BatteryWatcher thread to argon_oled.battery

Background sampler for the X1207 UPS HAT. Owns the SMBus session and a
libgpiod-claimed GPIO6 (with 750 ms software debounce). Publishes a frozen
BatteryStatus snapshot atomically — readers fetch watcher.status without
locking. Direction comes from a 60 s rolling SOC slope; GPIO6 only sets the
source_hint label, never the direction (since GPIO6 latches silent on this
hardware after rapid input cycling, per README's hardware reference).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `_draw_dir_arrow` helper in `screens.py`

**Files:**
- Modify: `argon_oled/screens.py` (append)

A small 8 × 8 pixel primitive for the direction icon. Pure pixel art, no font rendering.

- [ ] **Step 1: Write the verification command**

```bash
uv run python -c "
from PIL import Image, ImageDraw
from argon_oled.screens import _draw_dir_arrow

# Render each direction onto separate 8x8 images and confirm 'on' pixel counts
# distinguish them. Triangles: ~14-20 px filled. Bar: ~12-16 px. Line: ~8 px.
# Unknown: 0 px.
def count_on(img):
    return sum(1 for px in img.getdata() if px)

for direction, low, high in [
    ('charging', 10, 36),
    ('discharging', 10, 36),
    ('full', 6, 18),
    ('idle', 4, 12),
    ('unknown', 0, 0),
]:
    img = Image.new('1', (8, 8), 0)
    _draw_dir_arrow(ImageDraw.Draw(img), 0, 0, direction)
    n = count_on(img)
    print(f'{direction}: {n} px on')
    assert low <= n <= high, f'{direction} px count {n} not in [{low},{high}]'
print('_draw_dir_arrow OK')
"
```

- [ ] **Step 2: Run the verification — expect ImportError**

Expected: `ImportError: cannot import name '_draw_dir_arrow'`.

- [ ] **Step 3: Append `_draw_dir_arrow` to `argon_oled/screens.py`**

Add this function near the other `_draw_*` helpers (after `_draw_sparkline`):

```python
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
```

- [ ] **Step 4: Re-run the verification**

Expected output: 5 lines, e.g. `charging: 16 px on`, `discharging: 16 px on`, etc., and `_draw_dir_arrow OK`. If your numbers fall outside the bands, adjust the `low`/`high` in the verification — the bands are sized for the algorithm above and shouldn't need to move.

- [ ] **Step 5: Commit**

```bash
git add argon_oled/screens.py
git -c user.name="Cameron Zucker" -c user.email="cameronzucker@gmail.com" commit -m "$(cat <<'EOF'
Add _draw_dir_arrow helper for direction icon

8 x 8 px primitive drawn from polygons / rectangles instead of a font glyph,
since the default Pillow bitmap doesn't render Unicode arrows. Used by the
upcoming BatteryScreen for charging/discharging/full/idle icons.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `BatteryScreen` class with three render modes

**Files:**
- Modify: `argon_oled/screens.py` (append)

Renders the snapshot the watcher publishes. Imports `BatteryWatcher`, `BatteryStatus`, and `format_uptime` for the ETA.

- [ ] **Step 1: Write the verification command**

This script renders all three modes with stub data and dumps the resulting images to `/tmp` so you can `eog` / `xdg-open` them and eyeball the layout. The assertion is that the function runs and writes 3 PNGs.

```bash
uv run python -c "
from PIL import Image, ImageFont
from argon_oled.battery import BatteryStatus
from argon_oled.screens import BatteryScreen

font = ImageFont.load_default()

class _StubWatcher:
    def __init__(self, status):
        self.status = status

normal = BatteryStatus(
    detected=True, stale=False,
    voltage_v=4.105, soc_pct=89.0, soc_pct_raw=89.0,
    direction='discharging', source_hint='battery',
    eta_seconds=15120, slope_pct_per_min=-0.4,
    soc_history=tuple(80 + (i % 20) for i in range(128)),
)
no_ups = BatteryStatus(
    detected=False, stale=False,
    voltage_v=None, soc_pct=None, soc_pct_raw=None,
    direction='unknown', source_hint='?',
    eta_seconds=None, slope_pct_per_min=None,
    soc_history=(),
)
stale = BatteryStatus(
    detected=True, stale=True,
    voltage_v=4.10, soc_pct=89.0, soc_pct_raw=89.0,
    direction='discharging', source_hint='battery',
    eta_seconds=None, slope_pct_per_min=None,
    soc_history=tuple(80 + (i % 20) for i in range(128)),
)

for label, status in [('normal', normal), ('no_ups', no_ups), ('stale', stale)]:
    screen = BatteryScreen(font, _StubWatcher(status))
    img = Image.new('1', (128, 64), 0)
    screen.render(img, snap=None)
    out = f'/tmp/battery_{label}.png'
    img.convert('L').save(out)
    print(f'wrote {out}')
print('BatteryScreen render OK')
"
```

- [ ] **Step 2: Run the verification — expect ImportError**

Expected: `ImportError: cannot import name 'BatteryScreen'`.

- [ ] **Step 3: Append `BatteryScreen` to `argon_oled/screens.py`**

First, at the top of `screens.py` near the other module-level imports (after `from .gps import GPSDClient`), add:

```python
from .battery import BatteryStatus, BatteryWatcher
from .metrics import format_uptime
```

(The existing `from .metrics import SystemSnapshot, format_uptime` line — check the file: if `format_uptime` is already imported there, don't double-import.)

Then append this class at the end of the file:

```python
def _format_eta_text(direction: str, eta_seconds: int | None) -> str:
    """Right-side label for the voltage/ETA row of BatteryScreen."""
    if direction == "full":
        return "full"
    if direction == "idle":
        return "idle"
    if direction == "unknown" or eta_seconds is None:
        return "—"
    pretty = format_uptime(int(eta_seconds))
    if direction == "charging":
        return f"~{pretty} to full"
    if direction == "discharging":
        return f"~{pretty} left"
    return "—"


def _draw_soc_sparkline(draw: ImageDraw.ImageDraw, x: int, y: int,
                        w: int, h: int, history: tuple[float, ...]) -> None:
    """Borderless full-width SOC sparkline.

    One column per sample, newest on the right. SOC value 0-100 maps to 0
    to (h-1) vertical pixels.
    """
    if not history:
        return
    samples = list(history)[-w:]
    n = len(samples)
    base_x = x + w - n
    baseline = y + h - 1
    for j, v in enumerate(samples):
        col_x = base_x + j
        v_clamped = max(0.0, min(100.0, v))
        bar_h = int(round((h - 1) * (v_clamped / 100.0)))
        if bar_h > 0:
            draw.line((col_x, baseline - bar_h + 1, col_x, baseline), fill=1)


class BatteryScreen:
    """Live X1207 UPS state.

    Reads ``watcher.status`` per render — no I/O on the render path. The
    watcher's lifecycle is owned by ``app.py``.
    """

    name = "battery"

    def __init__(self, font: ImageFont.ImageFont, watcher):
        self._font = font
        self._watcher = watcher

    def render(self, image: Image.Image, snap) -> None:
        draw = ImageDraw.Draw(image)
        w, _ = image.size
        status: BatteryStatus = self._watcher.status

        draw.text((0, 0), "battery", fill=1, font=self._font)

        if not status.detected:
            draw.text((0, 11), "no UPS detected", fill=1, font=self._font)
            draw.text((0, 22), "(I2C 0x36 silent)", fill=1, font=self._font)
            return

        # Row 1: arrow + SOC + source label
        _draw_dir_arrow(draw, 0, 10, status.direction)
        soc_text = (
            f"{int(round(status.soc_pct))}%" if status.soc_pct is not None else "?%"
        )
        draw.text((10, 10), soc_text, fill=1, font=self._font)
        source_label = {
            "battery": "on battery",
            "external": "external",
            "?": "??",
        }[status.source_hint]
        draw.text((50, 10), source_label, fill=1, font=self._font)

        # Row 2: voltage + ETA (right-aligned)
        v_text = (
            f"{status.voltage_v:.2f}V" if status.voltage_v is not None else "?V"
        )
        draw.text((0, 20), v_text, fill=1, font=self._font)
        eta_text = "stale" if status.stale else _format_eta_text(
            status.direction, status.eta_seconds,
        )
        bbox = self._font.getbbox(eta_text)
        eta_w = bbox[2] - bbox[0]
        draw.text((w - eta_w, 20), eta_text, fill=1, font=self._font)

        # Row 3: SOC bar (8 px tall, full width)
        soc_frac = (status.soc_pct or 0.0) / 100.0
        _draw_bar(draw, 0, 30, w, 8, soc_frac)

        # SOC sparkline at y=41, height 23, full width.
        _draw_soc_sparkline(draw, 0, 41, w, 23, status.soc_history)
```

- [ ] **Step 4: Re-run the verification**

Expected: three `wrote /tmp/battery_<mode>.png` lines and `BatteryScreen render OK`. Then visually inspect:

```bash
ls -la /tmp/battery_*.png
```

If you have a desktop session, open them. If headless, you can `xdotool`/`feh` from the framebuffer or just trust the on-hardware test in Task 6 to confirm visual correctness.

- [ ] **Step 5: Commit**

```bash
git add argon_oled/screens.py
git -c user.name="Cameron Zucker" -c user.email="cameronzucker@gmail.com" commit -m "$(cat <<'EOF'
Add BatteryScreen with normal/no-UPS/stale render modes

Renders the X1207 status snapshot the BatteryWatcher publishes. No I/O on
the render path — reads watcher.status, draws direction arrow + SOC +
source label, voltage + right-aligned ETA, full-width SOC bar, and a
borderless 21-minute SOC sparkline. Stale and no-UPS modes degrade
gracefully without blanking the historical sparkline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire `BatteryWatcher` and `BatteryScreen` into `app.py`

**Files:**
- Modify: `argon_oled/app.py`

CLI flags, conditional carousel insertion at index 1, lifecycle in the existing `finally` block alongside `ButtonWatcher`.

- [ ] **Step 1: Re-read `argon_oled/app.py` to find the exact insertion points**

```bash
grep -n -E "(parse_args|carousel = |ScreenCarousel|watcher\.start|watcher\.stop)" argon_oled/app.py
```

Expected: lines naming `parse_args`, the `carousel = ScreenCarousel(...)` block, the `watcher.start()` call for `ButtonWatcher`, and the `watcher.stop()` cleanup. Confirm the file shape matches before editing.

- [ ] **Step 2: Add CLI flags to `parse_args`**

Edit `argon_oled/app.py`. After the existing `--no-buttons` line in `parse_args`, add:

```python
    p.add_argument("--no-battery", action="store_true",
                   help="disable the X1207 battery watcher (e.g. on a Pi without the HAT)")
    p.add_argument("--battery-i2c-bus", type=int, default=1)
    p.add_argument("--battery-i2c-address", type=lambda x: int(x, 0), default=0x36)
    p.add_argument("--battery-gpio-line", type=int, default=6)
    p.add_argument("--battery-debounce-ms", type=int, default=750)
    p.add_argument("--battery-sample-ms", type=int, default=5000)
```

- [ ] **Step 3: Import `BatteryScreen` and `BatteryWatcher`**

At the top of `argon_oled/app.py`, change the existing imports:

```python
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
```

to also include `BatteryScreen` and import `BatteryWatcher`:

```python
from .battery import BatteryWatcher
from .buttons import ButtonEvent, ButtonWatcher
from .screens import (
    BatteryScreen,
    DiskScreen,
    GPSScreen,
    HelpScreen,
    HotspotScreen,
    NetworkScreen,
    ScreenCarousel,
    StatusScreen,
)
```

- [ ] **Step 4: Build the `BatteryWatcher` and conditional screen list in `run()`**

Find the existing `carousel = ScreenCarousel(...)` block in `run()` and replace it with this:

```python
    battery_watcher: BatteryWatcher | None = None
    if not args.no_battery:
        battery_watcher = BatteryWatcher(
            i2c_bus=args.battery_i2c_bus,
            i2c_address=args.battery_i2c_address,
            gpio_line=args.battery_gpio_line,
            debounce_ms=args.battery_debounce_ms,
            sample_period_s=args.battery_sample_ms / 1000.0,
        )
        battery_watcher.start()

    screens = [StatusScreen(font)]
    if battery_watcher is not None:
        screens.append(BatteryScreen(font, battery_watcher))
    screens.extend([
        NetworkScreen(font),
        HotspotScreen(font, connection_name=args.hotspot_connection),
        DiskScreen(font),
        GPSScreen(font),
        HelpScreen(font),
    ])
    carousel = ScreenCarousel(screens=screens, font=font)
```

- [ ] **Step 5: Add cleanup to the `finally` block at the bottom of `run()`**

Find the existing `finally` block that handles `watcher.stop()` for the button watcher. Append:

```python
        if battery_watcher is not None:
            battery_watcher.stop()
            battery_watcher.join(timeout=1.0)
```

Right after the existing button-watcher cleanup. The full `finally` should now stop both watchers.

- [ ] **Step 6: Verify the file parses and the CLI shape is right**

```bash
uv run python -c "import ast; ast.parse(open('argon_oled/app.py').read()); print('syntax OK')"
uv run python -m argon_oled --help | grep -E "battery|--no-battery"
```

Expected: `syntax OK`, then six lines mentioning the new battery flags.

- [ ] **Step 7: Commit**

```bash
git add argon_oled/app.py
git -c user.name="Cameron Zucker" -c user.email="cameronzucker@gmail.com" commit -m "$(cat <<'EOF'
Wire BatteryScreen + BatteryWatcher into app

New CLI flags --no-battery, --battery-i2c-bus, --battery-i2c-address,
--battery-gpio-line, --battery-debounce-ms, --battery-sample-ms (defaults
match the values empirically derived during X1207 probing). Battery
screen inserts at carousel index 1 only when the watcher is enabled;
omitted entirely under --no-battery so a Pi without the HAT shows the
original 6-screen carousel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Live end-to-end verification on the Pi

**Files:** none modified — this is the spec's testing plan executed.

This task does not commit (unless bugs are discovered and fixed; in that case, fix-then-commit). The systemd service is left running as the user's daily driver after this task.

- [ ] **Step 1: Stop the running service so the foreground run can take the hardware**

```bash
sudo systemctl stop argon-oled
```

- [ ] **Step 2: Foreground run, watch logs**

```bash
cd /home/administrator/Code/argon-forty-display
uv run python -m argon_oled
```

Expected within ~5 seconds:
- A `Fuel gauge VERSION=0x0002 (MAX17040-style)` log line.
- A `Watching GPIO6 (initial=1)` log line (if PoE is currently in, GPIO6 is high).
- `Render loop @ 100ms; 7 screen(s)` (note: **7** — battery added).

- [ ] **Step 3: Navigate the carousel**

Tap the Industria button once: should land on the new `battery` screen (index 1 in 0-indexed = position 2 in the on-screen `2/7` hint). Confirm:
- The direction arrow renders (likely `=` idle initially while slope window builds).
- SOC % matches what the foreground log shows.
- Voltage shows `4.xxV`.
- The SOC bar reflects the percentage.
- The sparkline is mostly empty for the first ~10 s, then begins filling from the right.

Tap again to advance to `network`, then keep tapping through `wifi`, `disk`, `gps`, `help`, and back to `status`. Confirm none of the existing screens broke.

- [ ] **Step 4: Validate direction inference by yanking the X1207's USB-C input**

(PoE keeps the Pi up; we just want the X1207 to switch to its battery so the SOC trend shifts.)

If USB-C is currently in the X1207 jack: pull it. If it's already out: plug it in, wait a minute, then pull. Within ~60 s of the pull, watch the screen:
- The direction arrow should shift toward `▼` (discharging) or stay `=` if the cell is full and the rate is below threshold (this hardware's battery is currently around 102%, so ▼ may not appear — the trend will be flat).
- The source label should toggle to `on battery` after the 750 ms debounce.

- [ ] **Step 5: Test `--no-battery`**

`Ctrl-C` the foreground run. Then:

```bash
uv run python -m argon_oled --no-battery
```

Expected: `Render loop @ 100ms; 6 screen(s)`. Tap through the carousel and confirm the original 6 screens are present, no battery screen, and no error in the log.

`Ctrl-C` again.

- [ ] **Step 6: Restart the systemd service and confirm clean recovery**

```bash
sudo systemctl start argon-oled
sleep 5
systemctl status argon-oled --no-pager | head -20
journalctl -u argon-oled -n 30 --no-pager
```

Expected: service `active (running)`, journal shows the same `Fuel gauge VERSION=0x0002` and `Watching GPIO6` log lines, no Python tracebacks.

- [ ] **Step 7: If any defect was found, fix it and commit**

If everything in Steps 2-6 worked, this task is complete with no further commit. If a bug was found, fix it in the appropriate module, re-run the relevant verification step, and commit with a `Fix:` style message describing what was wrong and how the fix matches the spec.

---

## Self-Review

**Spec coverage check** — every requirement in [docs/superpowers/specs/2026-05-03-battery-gauge-screen-design.md](../specs/2026-05-03-battery-gauge-screen-design.md) maps to a task:

| Spec section | Implementing task |
|---|---|
| § 3.1 `BatteryStatus` dataclass shape | Task 1, Step 3 (frozen dataclass with all fields, plus `EMPTY_STATUS`) |
| § 3.1 `BatteryWatcher` constructor signature | Task 2, Step 3 (full param list with defaults matching spec) |
| § 3.2 I²C sampling: VCELL, SOC, one-time VERSION read, 5 s cadence | Task 2, Step 3 (`_sample_i2c`, `_version_logged`) |
| § 3.2 First sample synchronous on `start()` | Task 2, Step 3 (`run()` calls `_sample_once(bus)` before the loop) |
| § 3.2 GPIO6 claim, edge debounce 750 ms | Task 2, Step 3 (`_claim_gpio`, `_drain_gpio_edges`, `_resolve_gpio_level`, `_pending_since_ns`) |
| § 3.2 Source-hint table (4 cases) | Task 2, Step 3 (`_source_hint`) |
| § 3.2 Direction inference table | Task 1, Step 3 (`classify_direction`) |
| § 3.2 ETA formula | Task 1, Step 3 (`compute_eta_seconds`) |
| § 3.2 SOC slope (60 s window, ≥3 samples) | Task 1, Step 3 (`compute_slope`) |
| § 3.2 History deque (128 × 10 s) | Task 2, Step 3 (`_history`, `history_period_s`, `history_len`) |
| § 3.2 Stale flag (15 s threshold, 3-fail streak) | Task 2, Step 3 (`STALE_AFTER_S`, `_fail_streak` checks in `run()` + `_build_status`) |
| § 3.2 Contradiction logging once per transition | Task 2, Step 3 (`_last_logged_contradiction` in `_build_status`) |
| § 3.3 `_draw_dir_arrow` 8 × 8 primitives | Task 3, Step 3 |
| § 3.3.1 Normal mode layout (rows at y=0,10,20,30; sparkline at y=41-63) | Task 4, Step 3 (`BatteryScreen.render` happy path) |
| § 3.3.1 ETA text mapping | Task 4, Step 3 (`_format_eta_text`) |
| § 3.3.1 Borderless full-width sparkline | Task 4, Step 3 (`_draw_soc_sparkline`) |
| § 3.3.2 No-UPS mode | Task 4, Step 3 (`if not status.detected:` branch) |
| § 3.3.3 Stale mode | Task 4, Step 3 (`"stale" if status.stale else _format_eta_text(...)`) |
| § 3.4 CLI flags (six new) | Task 5, Step 2 |
| § 3.4 Carousel insert at index 1 | Task 5, Step 4 (battery appended right after `StatusScreen`) |
| § 3.4 `--no-battery` omits screen + watcher | Task 5, Step 4 (`if battery_watcher is not None`) |
| § 3.4 Lifecycle cleanup in `finally` | Task 5, Step 5 |
| § 3.5 `smbus2` dependency | **Deviation noted at top of plan** — relies on system-site-packages convention used by `gpiod`, no `pyproject.toml` change |
| § 4 Permissions / systemd unchanged | No task — already satisfied |
| § 5 Failure modes (each row) | Task 2, Step 3 (`_open_bus` returns None → log error; `_read_word` returns None → fail streak; GPIO claim fails → `_gpio_unavailable`; source-hint contradiction → log once) |
| § 6 Manual testing plan | Task 6 (each step matches a spec acceptance criterion) |
| § 8 Acceptance criteria | Verified in Task 6, Steps 2-6 |

**Placeholder scan** — no `TBD`, `TODO`, `add appropriate error handling`, `similar to Task N`, or other placeholder phrases appear in this plan. Every code block contains the actual code an engineer would commit; every command shows the expected output.

**Type / name consistency** — checked across tasks:
- `BatteryStatus` field names match between Task 1's dataclass definition and every subsequent reference (`status.detected`, `status.stale`, `status.voltage_v`, `status.soc_pct`, `status.soc_pct_raw`, `status.direction`, `status.source_hint`, `status.eta_seconds`, `status.slope_pct_per_min`, `status.soc_history`).
- `BatteryWatcher` constructor params (`i2c_bus`, `i2c_address`, `gpio_chip`, `gpio_line`, `sample_period_s`, `debounce_ms`, `slope_window_s`, `history_period_s`, `history_len`) are introduced once in Task 2 Step 3 and referenced consistently in Task 5 Step 4.
- `_draw_dir_arrow` signature `(draw, x, y, direction, size=8)` matches between definition (Task 3) and call site (Task 4).
- `Direction` literal values `"charging" | "discharging" | "idle" | "full" | "unknown"` are used identically in `classify_direction`, `_format_eta_text`, `_draw_dir_arrow`, and `BatteryScreen.render`.
- `SourceHint` literal values `"external" | "battery" | "?"` are used identically in `_source_hint`, `_build_status` contradiction logging, and `BatteryScreen.render`'s source-label dict.

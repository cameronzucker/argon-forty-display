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

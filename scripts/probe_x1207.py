"""Geekworm X1207 UPS HAT power-event discovery probe.

Empirically maps the X1207's signaling during PoE / USB-C connect & disconnect
events. Designed to run autonomously (operator follows on-screen prompts on a
fixed clock; the script does not require interactive input or surviving stdout).

What it captures:

- Every claimable GPIO line on gpiochip0 (excluding I2C, UART, EEPROM, and
  lines already in use by the running services), with PULL_UP bias and BOTH-
  edge detection. Each edge is logged with its phase label and timestamp.
- Periodic reads of the MAX17040-compatible fuel gauge at I2C 0x36: VCELL,
  SOC, MODE, VERSION, CONFIG. Sampled every 2s, register changes are logged.
- A configurable sequence of named phases. The default sequence walks the full
  spectrum of PoE / USB-C combinations across two repetitions to confirm any
  observed edges aren't one-offs.

Output goes to /tmp/x1207_probe_<timestamp>.log and is mirrored to stdout. A
human-readable summary (per-line edge counts grouped by phase, IC version,
register drifts per phase) is appended at the end so it can be pasted into a
hardware-reference doc.

Recommended invocation (detaches from controlling terminal so the probe
survives SSH disconnects mid-run):

    nohup uv run python scripts/probe_x1207.py \\
        > /tmp/x1207_probe_stdout.log 2>&1 < /dev/null &
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import gpiod
from gpiod.line import Bias, Direction, Edge
from smbus2 import SMBus

GPIOCHIP = "/dev/gpiochip0"
# BCM 0/1 = ID EEPROM, 2/3 = I2C-1, 14/15 = UART. Skip those a priori; the
# claimable-filter handles other in-use lines (button watcher, gpsd PPS).
CANDIDATE_LINES = [n for n in range(0, 28) if n not in (0, 1, 2, 3, 14, 15)]

I2C_BUS = 1
I2C_ADDR = 0x36
VCELL_REG = 0x02
SOC_REG = 0x04
MODE_REG = 0x06
VERSION_REG = 0x08
CONFIG_REG = 0x0C

CONSUMER = "x1207-probe"
I2C_POLL_S = 2.0

# (phase_label, on-screen prompt, duration_seconds)
DEFAULT_PHASES: list[tuple[str, str, int]] = [
    ("STARTUP",            "Capturing baseline. Hold all power as-is.",         8),
    ("S1_PoE_OUT",         "STEP 1/8: DISCONNECT PoE NOW.",                    30),
    ("S2_USB_IN",          "STEP 2/8: CONNECT USB-C to X1207 NOW.",            30),
    ("S3_USB_OUT",         "STEP 3/8: DISCONNECT USB-C from X1207 NOW.",       30),
    ("S4_PoE_IN",          "STEP 4/8: RECONNECT PoE NOW.",                     30),
    ("S5_PoE_OUT_2",       "STEP 5/8: DISCONNECT PoE NOW (round 2).",          30),
    ("S6_USB_IN_2",        "STEP 6/8: CONNECT USB-C to X1207 NOW (round 2).",  30),
    ("S7_USB_OUT_2",       "STEP 7/8: DISCONNECT USB-C from X1207 NOW (rd 2).",30),
    ("S8_PoE_IN_FINAL",    "STEP 8/8: FINAL PoE reconnect.",                   30),
]

log = logging.getLogger("probe")


@dataclass
class LineStats:
    line: int
    initial: int = -1
    edges: int = 0
    last_value: int = -1
    last_change_ns: int = 0
    events: list[tuple[int, str, str]] = field(default_factory=list)


@dataclass
class I2CSample:
    ts: float
    phase: str
    vcell_raw: int | None
    soc_raw: int | None
    mode_raw: int | None
    version_raw: int | None
    config_raw: int | None

    @property
    def voltage_v(self) -> float | None:
        return self.vcell_raw * 78.125 / 1_000_000.0 if self.vcell_raw is not None else None

    @property
    def soc_pct(self) -> float | None:
        return self.soc_raw / 256.0 if self.soc_raw is not None else None


def swap16(x: int) -> int:
    return ((x & 0xFF) << 8) | ((x >> 8) & 0xFF)


def filter_claimable(chip_path: str, lines: list[int]) -> list[int]:
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
                consumer=f"{CONSUMER}-test",
                config={line: settings},
            )
            r.release()
            ok.append(line)
        except OSError as e:
            log.warning("skip GPIO%d (cannot claim: %s)", line, e)
    return ok


class FuelGaugeWatcher(threading.Thread):
    def __init__(self, bus: int, addr: int, stop: threading.Event,
                 phase_ref: dict, samples: list[I2CSample],
                 period: float = I2C_POLL_S):
        super().__init__(daemon=True, name="fuel-gauge-watcher")
        self.bus_no = bus
        self.addr = addr
        self.stop = stop
        self.phase_ref = phase_ref
        self.samples = samples
        self.period = period
        self.read_errors = 0
        self.last_logged: dict[str, int] = {}

    def _read_word(self, bus: SMBus, reg: int) -> int | None:
        try:
            raw = bus.read_word_data(self.addr, reg)
            return swap16(raw)
        except OSError:
            self.read_errors += 1
            return None

    def run(self) -> None:
        try:
            bus = SMBus(self.bus_no)
        except OSError as e:
            log.error("I2C open failed on bus %d: %s", self.bus_no, e)
            return
        try:
            while not self.stop.is_set():
                phase = self.phase_ref.get("name", "?")
                vcell = self._read_word(bus, VCELL_REG)
                soc = self._read_word(bus, SOC_REG)
                mode = self._read_word(bus, MODE_REG)
                ver = self._read_word(bus, VERSION_REG)
                cfg = self._read_word(bus, CONFIG_REG)
                self.samples.append(I2CSample(
                    ts=time.time(), phase=phase,
                    vcell_raw=vcell, soc_raw=soc, mode_raw=mode,
                    version_raw=ver, config_raw=cfg,
                ))
                for name, val in (
                    ("VCELL", vcell), ("SOC", soc), ("MODE", mode),
                    ("VERSION", ver), ("CONFIG", cfg),
                ):
                    last = self.last_logged.get(name)
                    if last is None and val is not None:
                        log.info("[i2c %s] %s = 0x%04X (initial)", phase, name, val)
                        self.last_logged[name] = val
                    elif val is not None and val != last:
                        log.info("[i2c %s] %s: 0x%04X -> 0x%04X", phase, name, last, val)
                        self.last_logged[name] = val
                self.stop.wait(self.period)
        finally:
            bus.close()


def announce(msg: str) -> None:
    bar = "=" * max(20, len(msg) + 4)
    log.info("")
    log.info(bar)
    log.info(">> %s", msg)
    log.info(bar)


def run_phase(label: str, prompt: str, seconds: int, request,
              stats: dict[int, LineStats], phase_ref: dict,
              stop: threading.Event) -> None:
    phase_ref["name"] = label
    announce(f"{label} ({seconds}s): {prompt}")
    deadline = time.monotonic() + seconds
    last_tick = -1
    while not stop.is_set() and time.monotonic() < deadline:
        if request.wait_edge_events(timedelta(milliseconds=500)):
            for ev in request.read_edge_events():
                line = ev.line_offset
                rising = (ev.event_type == ev.Type.RISING_EDGE)
                etype = "RISING" if rising else "FALLING"
                ts = ev.timestamp_ns
                s = stats[line]
                pulse = ""
                if s.last_change_ns:
                    pulse = f"  dt_prev={(ts - s.last_change_ns) / 1e6:.1f}ms"
                log.info("[gpio %s] GPIO%-2d %s%s", label, line, etype, pulse)
                s.edges += 1
                s.last_change_ns = ts
                s.last_value = 1 if rising else 0
                s.events.append((ts, label, etype))
        remaining = int(deadline - time.monotonic())
        if remaining != last_tick and remaining % 5 == 0 and remaining >= 0:
            last_tick = remaining
            log.info("[%s] %ds remaining", label, remaining)


def fmt_voltage(v: float | None) -> str:
    return f"{v:.3f}V" if v is not None else "?"


def fmt_soc(s: float | None) -> str:
    return f"{s:.2f}%" if s is not None else "?"


def summarize(claimable: list[int], stats: dict[int, LineStats],
              samples: list[I2CSample], phases: list[tuple[str, str, int]]) -> str:
    out: list[str] = []
    out.append("")
    out.append("=" * 72)
    out.append("X1207 Probe Summary")
    out.append("=" * 72)

    version = next((s.version_raw for s in samples if s.version_raw is not None), None)
    if version is not None:
        family = "MAX17048-style (has CRATE)" if version >= 0x0010 else "MAX17040-style (no CRATE)"
        out.append(f"IC VERSION register: 0x{version:04X}  ({family})")
    else:
        out.append("IC VERSION register: unreadable")

    out.append("")
    out.append("Fuel-gauge readings per phase:")
    by_phase: dict[str, list[I2CSample]] = defaultdict(list)
    for s in samples:
        by_phase[s.phase].append(s)
    for label, _, _ in phases:
        ph = by_phase.get(label, [])
        if not ph:
            out.append(f"  {label:24s}: (no samples)")
            continue
        out.append(
            f"  {label:24s}: V {fmt_voltage(ph[0].voltage_v)} -> {fmt_voltage(ph[-1].voltage_v)}  "
            f"SOC {fmt_soc(ph[0].soc_pct)} -> {fmt_soc(ph[-1].soc_pct)}  "
            f"({len(ph)} samples)"
        )

    out.append("")
    out.append("GPIO line behavior (PULL_UP bias):")
    active = sorted(line for line in claimable if stats[line].edges > 0)
    quiet = sorted(line for line in claimable if stats[line].edges == 0)
    if active:
        out.append("  Active lines (edges detected):")
        for line in active:
            s = stats[line]
            per_phase: dict[str, dict[str, int]] = defaultdict(lambda: {"R": 0, "F": 0})
            for _ts, ph, etype in s.events:
                per_phase[ph]["R" if etype == "RISING" else "F"] += 1
            phase_str = "  ".join(
                f"{p}:R{per_phase[p]['R']}/F{per_phase[p]['F']}"
                for p, _, _ in phases if p in per_phase
            )
            out.append(
                f"    GPIO{line:<2d}  initial={s.initial}  total_edges={s.edges}  {phase_str}"
            )
    else:
        out.append("  (no GPIO lines changed during the probe)")
    if quiet:
        out.append(f"  Quiet lines (no edges): {', '.join(f'GPIO{l}' for l in quiet)}")
    return "\n".join(out)


def main() -> int:
    ts_label = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(f"/tmp/x1207_probe_{ts_label}.log")

    file_handler = logging.FileHandler(log_path, mode="w")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])

    # Survive shells closing under us during PoE/USB-C cycling.
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass

    log.info("X1207 probe starting; full log -> %s", log_path)
    log.info("Filtering claimable GPIO lines on %s", GPIOCHIP)
    claimable = filter_claimable(GPIOCHIP, CANDIDATE_LINES)
    if not claimable:
        log.error("No GPIO lines could be claimed.")
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
    stats: dict[int, LineStats] = {
        line: LineStats(line=line, initial=val.value, last_value=val.value)
        for line, val in zip(claimable, initial)
    }
    log.info("Initial state: %s", ", ".join(
        f"GPIO{l}={s.initial}" for l, s in sorted(stats.items())
    ))

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    samples: list[I2CSample] = []
    phase_ref: dict[str, str] = {"name": "INIT"}
    fg = FuelGaugeWatcher(I2C_BUS, I2C_ADDR, stop, phase_ref, samples)
    fg.start()

    time.sleep(I2C_POLL_S * 1.2)  # Ensure at least one fuel-gauge sample.

    total_s = sum(s for _, _, s in DEFAULT_PHASES)
    log.info("Running %d phases, total ~%ds", len(DEFAULT_PHASES), total_s)

    try:
        for label, prompt, seconds in DEFAULT_PHASES:
            run_phase(label, prompt, seconds, request, stats, phase_ref, stop)
            if stop.is_set():
                break
        announce("Probe complete")
    finally:
        stop.set()
        try:
            request.release()
        except Exception:
            pass
        fg.join(timeout=2.0)

    summary = summarize(claimable, stats, samples, DEFAULT_PHASES)
    log.info("%s", summary)
    log.info("Log file: %s", log_path)
    print(f"\nLog file: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

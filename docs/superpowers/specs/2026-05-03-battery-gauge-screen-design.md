# Battery Gauge Screen — Design

**Date:** 2026-05-03
**Status:** Approved, ready for implementation planning
**Hardware target:** Raspberry Pi 5 + Argon Industria OLED + Geekworm X1207 UPS / PoE HAT

## 1. Goal

Add a seventh screen to the existing OLED carousel that displays the live state of the X1207 UPS: state of charge, voltage, charge/discharge direction, source (external vs battery), and a 21-minute SOC trend sparkline. The screen sits one tap from the boot-default `StatusScreen` so "is the Pi about to die?" is the cheapest possible gesture.

## 2. Constraints established by empirical probe

A multi-phase autonomous probe (`scripts/probe_x1207.py`, run 2026-05-03) walked PoE and USB-C inputs through eight transitions across two repetitions while watching every claimable BCM line on `gpiochip0` and the MAX17040 fuel gauge over I²C-1. Findings, also captured in [README.md § Hardware reference § X1207 UPS HAT](../../../README.md):

- **Fuel gauge IC** is MAX17040-family — `VERSION` register `0x08` returns `0x0002`. **No `CRATE` register**, so charge direction is not directly readable from a single I²C register; it must be inferred from a SOC time series and/or GPIO state.
- **GPIO6** fires edges around USB-C / PoE plug events but is **bouncy** (1-4 edges per transition over ~5-10 s) and **can latch silent** under rapid input cycling. Useful as a *hint*; not as ground truth.
- **GPIO16** read constant low through every transition under pull-up bias. It is *not* a Pi-driven output as prior community scripts assume on this unit. Don't use it.
- **No other BCM lines** on the header carry X1207 signal.
- **Battery rail transients**: during source switches, VCELL briefly sags ~0.5 V before recovering. Any direction-inference window must be long enough to ignore these transients.

## 3. Architecture

Pattern after [argon_oled/gps.py](../../../argon_oled/gps.py) and `GPSDClient`: a background thread owns the hardware interaction; the render loop reads an atomic snapshot.

### 3.1 New module — `argon_oled/battery.py`

Two public types:

- `BatteryStatus` — frozen dataclass, one snapshot of state. Fields:
  - `detected: bool` — false when the MAX17040 doesn't ack on `0x36`.
  - `stale: bool` — true if last successful I²C read was more than 15 s ago.
  - `voltage_v: float | None`
  - `soc_pct: float | None` — clamped to 0-100 for display.
  - `soc_pct_raw: float | None` — unclamped (above 100 is normal at full charge).
  - `direction: Literal["charging", "discharging", "idle", "full", "unknown"]`
  - `source_hint: Literal["external", "battery", "?"]`
  - `eta_seconds: int | None` — seconds to full or to empty; `None` if direction doesn't have a meaningful ETA.
  - `slope_pct_per_min: float | None`
  - `soc_history: tuple[float, ...]` — most recent up to 128 SOC samples, newest last, for the sparkline.

- `BatteryWatcher` — `threading.Thread` subclass. Fields and methods:
  - Constructor params: `i2c_bus: int = 1`, `i2c_address: int = 0x36`, `gpio_chip: str = "/dev/gpiochip0"`, `gpio_line: int = 6`, `sample_period_s: float = 5.0`, `debounce_ms: int = 750`, `slope_window_s: float = 60.0`, `history_period_s: float = 10.0`, `history_len: int = 128`.
  - `start()` / `stop()` — standard thread lifecycle. `stop()` releases the gpiod request and closes the SMBus.
  - `status: BatteryStatus` — public attribute holding the latest snapshot. Replaced atomically (whole-dataclass reference swap) on each sample tick. Readers do not need a lock; Python name binding is atomic.

### 3.2 Sampling and inference

- **I²C** — every `sample_period_s` (default 5 s), the thread reads `VCELL` (`0x02`), `SOC` (`0x04`), and on the first successful sample also `VERSION` (`0x08`) for one-time IC identification logging. The very first sample is taken synchronously in `start()` before the worker loop begins, so `status.detected` reflects truth on the first render frame instead of briefly showing the no-UPS mode for ~5 s after boot. Each MAX17040 word is byte-swapped after `read_word_data` (the chip transmits MSB-first; SMBus returns little-endian). Voltage = raw × 78.125 µV; SOC = raw / 256 in percent. A single `OSError` is logged at DEBUG and skipped; three consecutive failures set `stale = True`. The next success clears it.

- **GPIO6** — claimed once at `start()` with `Direction.INPUT`, `Bias.PULL_UP`, `Edge.BOTH`. The watcher reads the initial level synchronously immediately after the claim succeeds and seeds `source_hint` from it; this means the screen renders a meaningful source label on the very first frame, not after the first plug event. The thread then blocks on `request.wait_edge_events(timeout)` between I²C samples (timeout chosen so the loop wakes for I²C polling on schedule). When an edge fires, the new level is recorded but not promoted to `source_hint` until it has been stable for `debounce_ms` (default 750 ms). If the GPIO6 line cannot be claimed (e.g., another process holds it), the thread continues without it — `source_hint` becomes permanently `"?"` and SOC trend alone drives `direction`.

- **History deque** — every `history_period_s` (default 10 s), the latest SOC value is appended to a `collections.deque(maxlen=history_len)`. With defaults, this is 128 samples × 10 s = 21:20 of history.

- **Slope** — computed on each `BatteryStatus` rebuild from the samples within the trailing `slope_window_s` (default 60 s). Linear fit (least-squares against time deltas in minutes) → `slope_pct_per_min`. If fewer than 3 samples are in the window, `slope_pct_per_min` is `None`.

- **Direction inference** — using the slope and current SOC:

| Condition | Direction |
|---|---|
| `slope_pct_per_min` is `None` (boot, <3 samples) | `unknown` |
| `slope > +0.1` and `soc_pct < 99` | `charging` |
| `slope < -0.1` | `discharging` |
| `|slope| ≤ 0.1` and `soc_pct ≥ 98` | `full` |
| otherwise | `idle` |

  GPIO6 does *not* override the direction. It only sets `source_hint`:

| Condition | `source_hint` |
|---|---|
| Line claimed; current level HIGH and (no edge ever, or last edge ≥ `debounce_ms` ago) | `external` |
| Line claimed; current level LOW and (no edge ever, or last edge ≥ `debounce_ms` ago) | `battery` |
| Line claimed; last edge less than `debounce_ms` ago (debounce window in progress) | `?` |
| Line was never claimed (claim failed at startup) | `?` |

  If `source_hint` and `direction` contradict (e.g. `external` but `discharging` faster than -0.5 %/min, suggesting the documented X1207 latch state), the watcher logs an INFO line once per transition but does not alter what the screen displays — the user sees the trend-derived direction and the raw GPIO-derived hint, and can read between the lines.

- **ETA** — derived from slope and current SOC:

| Direction | `eta_seconds` |
|---|---|
| `charging`, slope ≥ 0.2 %/min | `(100 - soc_pct) / slope_pct_per_min × 60`, rounded |
| `discharging`, |slope| ≥ 0.2 %/min | `soc_pct / -slope_pct_per_min × 60`, rounded |
| any other case (slope below 0.2, or `full`/`idle`/`unknown`) | `None` |

### 3.3 New screen — `BatteryScreen` in `argon_oled/screens.py`

Constructor takes `font` and `watcher: BatteryWatcher`. The screen does not own the watcher — it only reads `watcher.status` per render. This mirrors how `GPSScreen` lazily owns its client; `BatteryWatcher` is owned by `app.py` so its lifecycle is explicit and visible alongside `ButtonWatcher`.

Rendering modes:

#### 3.3.1 Normal mode (`status.detected and not status.stale`)

```
y=0   battery               [n/N]   <- carousel hint added by ScreenCarousel
y=10  [arrow] 89%   on battery
y=20  4.10V        ~4h12m left
y=30  ████████░░░░░░░░░░░░░░░░       <- 8 px tall, full width SOC bar
y=41-63                              <- SOC sparkline, 23 px tall, full width
```

- `[arrow]` is a custom 8 × 8 polygon at `(0, 10)` drawn by a new `_draw_dir_arrow(draw, x, y, direction)` helper:

| `direction` | Glyph |
|---|---|
| `charging` | filled up-triangle |
| `discharging` | filled down-triangle |
| `full` | filled horizontal bar (`y + 3` to `y + 5`, `x + 1` to `x + 6`) |
| `idle` | thin horizontal line through middle |
| `unknown` | nothing drawn (8 × 8 area left blank) |

- SOC text starts at `(10, 10)` and shows `f"{soc:.0f}%"`.
- Source label at `(50, 10)` uses the values: `"on battery"`, `"external"`, `"??"`, mapping from `source_hint` (`battery → "on battery"`, `external → "external"`, `? → "??"`).
- Voltage at `(0, 20)`: `f"{voltage_v:.2f}V"`.
- ETA text right-aligned at y=20:

| Condition | Right-side text |
|---|---|
| `direction == "full"` | `full` |
| `direction == "idle"` | `idle` |
| `direction == "unknown"` | `—` |
| `eta_seconds is None` (rate too small) | `—` |
| `direction == "charging"` and `eta_seconds` set | `~{format_eta(eta_seconds)} to full` |
| `direction == "discharging"` and `eta_seconds` set | `~{format_eta(eta_seconds)} left` |

  Where `format_eta` reuses `format_uptime` from `metrics.py` (already produces compact `12s / 4m / 3h12m / 2d4h` formats — perfect for ETA).

- SOC bar at `y=30`, height 8 px, uses the existing `_draw_bar` helper from screens.py.
- SOC sparkline at `y=41`, height 23 px, full 128 px width:
  - One column per sample, newest on the right.
  - Sample value 0-100 mapped to 0-22 vertical pixels.
  - No outline — matches `NetworkScreen`'s borderless aesthetic for full-width sparklines.
  - Newer samples drawn even if there are fewer than 128 — the line just doesn't fill the full width yet.

#### 3.3.2 No-UPS mode (`status.detected is False`)

```
y=0   battery               [n/N]
y=11  no UPS detected
y=22  (I²C 0x36 silent)
```

#### 3.3.3 Stale mode (`status.detected and status.stale`)

Render normal mode but:
- Replace the right-side ETA with the literal text `stale`.
- The SOC bar shows the last known SOC (not zeroed).
- Sparkline continues showing what was captured before staleness — the most recent samples are simply old.

This is a deliberate UX choice: when an I²C read intermittently fails, the user wants to see what the trajectory was just before the failure, not a blank screen.

### 3.4 `app.py` changes

CLI flags added (defaults match probe-derived values):

| Flag | Default | Purpose |
|---|---|---|
| `--no-battery` | off | disable the battery watcher entirely |
| `--battery-i2c-bus` | `1` | I²C bus number for the X1207 fuel gauge |
| `--battery-i2c-address` | `0x36` | parsed via `int(x, 0)` so `0x36`, `54`, `0o66` all work |
| `--battery-gpio-line` | `6` | BCM line number for the X1207 power-source signal |
| `--battery-debounce-ms` | `750` | GPIO6 debounce window |
| `--battery-sample-ms` | `5000` | I²C poll cadence |

Lifecycle in `run()`, parallel to `ButtonWatcher`:

```
watcher: BatteryWatcher | None = None
if not args.no_battery:
    watcher = BatteryWatcher(
        i2c_bus=args.battery_i2c_bus,
        i2c_address=args.battery_i2c_address,
        gpio_line=args.battery_gpio_line,
        debounce_ms=args.battery_debounce_ms,
        sample_period_s=args.battery_sample_ms / 1000.0,
    )
    watcher.start()
```

The carousel is built conditionally — `BatteryScreen(font, watcher)` is added at index 1 (right after `StatusScreen`) iff `watcher is not None`. New full carousel order:

```
1. status     2. battery     3. network     4. wifi
5. disk       6. gps         7. help
```

Cleanup in the `finally` of `run()`:

```
if watcher is not None:
    watcher.stop()
    watcher.join(timeout=1.0)
```

### 3.5 Dependency

`smbus2` is added to `pyproject.toml` under `dependencies`. It's already part of the `python3-smbus2` Debian package on Trixie and works in the system-site-packages venv used by this project.

## 4. Permissions and systemd

No changes. The runtime user must already be in the `i2c` group (for the OLED) and the `gpio` group (for the button). The systemd unit and install instructions are unchanged.

## 5. Failure modes summary

| Condition | Behavior |
|---|---|
| MAX17040 absent at startup | `status.detected = False`. Screen renders no-UPS mode. Watcher continues retrying; supports hot-plug recovery. |
| Single I²C `OSError` | Skipped silently. |
| Three consecutive I²C failures | `stale = True`, screen renders stale mode keeping last known values + history. |
| GPIO6 line cannot be claimed | Thread continues without it. `source_hint` is `"?"` permanently. Direction still inferred from SOC trend. INFO log once. |
| GPIO6 latches silent (X1207 fault state) | `source_hint` reflects last debounced level. Direction comes from SOC trend, which keeps working. INFO log once on the contradiction (high source_hint but discharging trend). |
| Pi reboots / battery dies | Out of scope. |

## 6. Testing plan

Manual; matches the project convention of leaving hardware-touching code uncovered by automated tests.

1. Foreground: `uv run python -m argon_oled`. Battery screen appears at index 2. After ~30 s of running, the SOC bar matches the displayed percentage and the sparkline has begun populating from the right edge.
2. Use buttons to navigate to/from battery screen — no glitches in either direction.
3. Pull USB-C input. Within ~60 s, direction transitions `idle → discharging` and the ETA appears. Source hint transitions to `battery` once GPIO6 stabilizes.
4. Plug USB-C back in. Direction transitions back; if the cell is full (current state), it reads `full` rather than `charging`. Source hint returns to `external`.
5. `uv run python -m argon_oled --no-battery` — battery screen is absent from the carousel; no thread is spawned; existing screens unaffected.
6. `sudo systemctl restart argon-oled` — battery screen renders correctly after restart.
7. Existing screens (status, network, wifi, disk, gps, help) all still render. The button screen still navigates. The OLED frame rate is not visibly degraded.

## 7. Out of scope

- Charging-enable control. The old `x1207_ups.py` set GPIO16 to enable/disable charging; we determined GPIO16 is not behaving the way that script assumes on this unit, and a battery *gauge* screen has no business toggling charging anyway.
- HTTP / JSON API. The OLED screen is the entire delivery surface for v1.
- Time-to-empty / time-to-full as a stored long-term log. Only the in-memory rolling deque is maintained.
- Per-cell voltage or temperature. The MAX17040 doesn't expose those.
- Updating the OLED brightness or putting the screen to sleep based on battery state. (Item already in HANDOFF.md "things that aren't done.")
- Adding the battery findings to `HANDOFF.md`'s "things that aren't done" table — that table will be updated to *remove* the battery item once implementation lands, not in this design phase.

## 8. Acceptance criteria

- A new screen labeled `battery` appears at index 2 of the carousel when run on the live X1207-equipped Pi.
- SOC, voltage, direction, and source labels render and update without crashing.
- The SOC sparkline accumulates new samples once every ~10 s and visibly tracks the live SOC.
- Pulling and replugging USB-C results in a direction change within ~60 s.
- `--no-battery` cleanly omits the screen and the watcher thread.
- Existing screens, button navigation, GPS PPS, and Wi-Fi QR continue to work.
- README's Hardware reference section is updated (already done in this branch).

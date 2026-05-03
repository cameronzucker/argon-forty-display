# Handoff

Snapshot of project state for a future agent or contributor picking this up. Updated 2026-05-03.

## What this is

A status-display application for the Argon Forty Industria OLED on Raspberry Pi 5, plus reverse-engineered hardware notes for the module's undocumented connector and button protocol — and now for the Geekworm X1207 UPS / PoE HAT it sits above. See [README.md](README.md) for the public overview.

## Current state

- **Application**: 7 screens (status, battery, network, wifi-QR, disk, gps, help). All live-tested on the same Pi.
- **Hardware findings**: documented in [README.md § Hardware reference](README.md#hardware-reference). Empirically verified via `scripts/probe_buttons.py` (button protocol) and `scripts/probe_x1207.py` (UPS signaling) on the test Pi. Argon Forty's own product page (`argon40.com/products/argon-industria-oled-display-module`) returns 404; Geekworm's X1207 docs are sparse and partially inaccurate against real hardware behavior — between the two, this README is the de facto reference.
- **Persistence**: installed as a system-level systemd service (`/etc/systemd/system/argon-oled.service`). Running, enabled at boot. Verify with `systemctl status argon-oled` and `journalctl -u argon-oled -f`.
- **Tested on**: Raspberry Pi 5, Debian 13 (Trixie), kernel 6.12.x, libgpiod 2.2.x, gpsd 3.25, NetworkManager, Geekworm X1207 UPS / PoE HAT (MAX17040 fuel gauge at I²C 0x36).
- **GPS receiver tested**: Waveshare LC29H over USB (CP2102N → `/dev/ttyUSB0`, NMEA at 115200, PPS via modem-control line).

## Architecture (one-paragraph version)

Render loop in [argon_oled/app.py](argon_oled/app.py) drives the OLED at ~10 fps. Metrics are sampled at 1 Hz from [argon_oled/metrics.py](argon_oled/metrics.py) and passed to whichever screen is active. Screens implement a simple `Screen` protocol in [argon_oled/screens.py](argon_oled/screens.py); the `ScreenCarousel` rotates between them on button events. Buttons are watched in a daemon thread by [argon_oled/buttons.py](argon_oled/buttons.py) (libgpiod v2, debounced, classified as SHORT vs LONG by hold duration). The wifi screen reads NetworkManager via `nmcli` ([argon_oled/hotspot.py](argon_oled/hotspot.py)). The GPS screen connects to `gpsd`'s JSON socket from a background thread ([argon_oled/gps.py](argon_oled/gps.py)) and renders an xgps-style polar sky map. The battery screen reads from a `BatteryWatcher` ([argon_oled/battery.py](argon_oled/battery.py)) — another background thread that owns the I²C session for the MAX17040 fuel gauge and a libgpiod-claimed GPIO6, publishing a frozen `BatteryStatus` snapshot the render loop can read lock-free. Direction is inferred from a 60 s rolling SOC slope (the MAX17040 has no `CRATE` register); GPIO6 only feeds the source-hint label, never the direction, because the line latches silent under rapid input cycling on this hardware.

## Operational notes

- **Two physical buttons share GPIO 4** (parallel-wired). There is no electrical way to distinguish them. All UI uses press patterns (short tap = next screen, long hold = previous). This is THE key undocumented finding.
- **Pi 5 + Trixie GPIO chip is `/dev/gpiochip0`**, not `gpiochip4` as some older docs claim. Always verify with `gpioinfo` if the kernel changes.
- **gpsd device is not auto-attached** for generic CP210x USB-UART bridges — Argon's stock udev rules don't match. Run `sudo gpsdctl add /dev/ttyUSB0` (or whatever ttyUSB ends up being) once after plugging in the GPS. To make this permanent across reboots, add `DEVICES="/dev/ttyUSB0"` to `/etc/default/gpsd`.
- **`nmcli --show-secrets` on AP-mode connections works without sudo** under default Trixie polkit policy — that's why the wifi screen can build the QR payload as the runtime user.
- **`uv` lives in `~/.local/bin`** which isn't on systemd's default PATH. The unit explicitly sets `Environment=PATH=` and uses an absolute `ExecStart`.
- **X1207 GPIO6 is bouncy AND can latch silent.** Each USB-C / PoE plug or unplug typically produces 1-4 edges over ~5-10 s; after ~3 rapid input transitions in succession the line stops emitting entirely until the UPS is rebooted. The screen treats GPIO6 as a *hint* on top of the SOC trend rather than ground truth. Don't trust it as a single-shot status read.
- **X1207 GPIO16 is not a Pi-driven output on this unit**, despite what older community scripts (e.g. `geekworm-com/x120x` and derivatives) assume. With pull-up bias it reads constant low through every input transition. Reading it does not yield meaningful charging-state information.
- **The X1207's USB-C input cannot be combined with the Pi's own USB-C** (they fight at the rail). Use the X1207's USB-C jack only.

## Things that aren't done (but could be)

| Item | Notes |
|---|---|
| Confirm exact pin order on the 5-pin JST-SH connector | We know which signals are present, not which physical pin carries which. Would need a multimeter probe of each contact. |
| Portable systemd unit | Currently uses `<USER>` / `<HOME>` placeholders that the README sed's into `/etc/systemd/system/`. Could template via `systemd-run` or `systemd-tmpfiles` substitutions if it gets uglier. |
| Tests | None. Reasonable targets: `metrics.py` (mockable), `marquee` removed, the QR payload escape function, the parent-block-device parser. Hardware-touching code stays manual. |
| Packaging | Not on PyPI. Distributed by clone-and-`uv-sync`. Fine for now. |
| Wider hardware testing | Only verified on Pi 5 + Trixie. The Industria OLED also ships in the V5 case bundle for Pi 4; behavior should be identical but is unverified. |
| Per-CPU frequency reporting | We show aggregate `cpu_freq().current`. Per-core frequency exists via `psutil.cpu_freq(percpu=True)` but currently unused. |
| Display dimming / sleep | The OLED runs at full brightness 24/7. Would extend panel life to fade or blank after inactivity. Argon's stock firmware does this. |

## Persistent memory

This project's working directory is `/home/administrator/argon-forty-display/`. Earlier session memories were written to `/home/administrator/.claude/projects/-home-administrator-Code-aredn-pi-setup/memory/` (the parent repo's path) — a future agent running directly from `argon-forty-display/` will start with a fresh memory store. The most useful entries from the older path, if you want to copy them forward:

- `project_argon_buttons.md` — the GPIO 4 / parallel-wiring finding with prior-art notes
- `project_argon_oled.md` — overall hardware setup and unknowns
- `project_pi5_gpiochip.md` — `gpiochip0` vs `gpiochip4` on Pi 5 + Trixie
- `feedback_coding_style.md` — the user's stated preferences for hardware-Python work
- `user_profile.md` — context on the user including the dev-Pi vs test-Pi distinction

## Quick verification on a fresh agent session

```bash
# On the test Pi:
systemctl status argon-oled        # should be active (running) and enabled
journalctl -u argon-oled -n 5      # recent log lines
git -C ~/argon-forty-display log --oneline -5
```

If the display is dark, the most common cause is the service crashed at boot before NetworkManager came up (the unit declares `After=NetworkManager.service` but races still happen). Restart with `sudo systemctl restart argon-oled` and check logs.

## Two physical Pis

The user rotates between a "dev" Pi and a "test" Pi. The Pi this repo is checked out on is the **test Pi** — it's the one with the Argon hardware. If a future user shares disk/CPU/uptime numbers that don't match, ask which box they were on before assuming a bug.

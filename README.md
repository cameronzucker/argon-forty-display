# argon-forty-display

A Python status-display application for the **Argon Forty Industria OLED module** on Raspberry Pi 5, *and* reverse-engineered notes on the module's otherwise-undocumented hardware interface.

The Industria OLED ships as part of the Argon V5 case bundle and connects to the Argon PoE+ / M.2 NVMe HAT via a proprietary 5-pin JST-SH connector. As of this writing, Argon Forty publishes essentially **no** documentation on the connector pinout or the button protocol — their official product page returns 404. Existing community projects work around the gap empirically; this repo names the constraint they're conforming to.

---

## Hardware reference

Empirically verified on a Raspberry Pi 5 running Debian 13 (Trixie) with `libgpiod` 2.2.x. Anyone with the same module can reproduce these findings using `scripts/probe_buttons.py`.

### OLED panel

| Property | Value |
|---|---|
| Controller | SSD1306 (compatible with `luma.oled`) |
| Resolution | 128 × 64, 1-bit |
| Bus | `/dev/i2c-1` |
| I2C address | `0x3C` |
| Initialization | Standard SSD1306 init; no proprietary handshake |

### Buttons — *the part nobody documents*

The Industria has **two physical buttons** (microswitches actuated by pressing the case housing — not capacitive touch on the screen face, despite some product copy suggesting otherwise). They are:

- Both wired **in parallel onto a single signal line: BCM GPIO 4** (`/dev/gpiochip0`, line 4).
- **Electrically indistinguishable.** There is no way to tell which of the two buttons was pressed; the connector geometry simply does not have room for a second signal line.
- Active-low. Internal `PULL_UP` bias is sufficient — no external pull-up needed.
- Bounce can be severe on at least one of the two buttons (mechanical force on the housing produces inconsistent contact). A software debounce window of ~50 ms is comfortable.

This is the constraint that explains why every existing Argon OLED community add-on uses *press patterns* (short / long / double / hold-N-seconds) rather than per-button actions: with one wire for two buttons, press patterns are the only encoding available.

### Connector

The OLED module connects to the HAT via a 5-pin JST-SH socket. Empirically the connector carries:

- `3.3 V` (presumed; not metered)
- `GND`
- `SDA` (BCM 2 / I2C-1 data)
- `SCL` (BCM 3 / I2C-1 clock)
- Button signal (BCM 4)

The exact pin order on the connector body has not been confirmed by probing each contact individually; only the *set of signals* is verified.

### Pi 5 + Trixie GPIO chip note

On current Raspberry Pi 5 Trixie kernels (`6.12.x`, `libgpiod 2.2.x`), the 40-pin header is exposed on **`/dev/gpiochip0`**, not `/dev/gpiochip4`. Older Pi 5 documentation that names `gpiochip4` was correct under earlier kernels and is no longer accurate. Always verify with `gpioinfo` before targeting a chip number.

### What is *not* on the I2C bus

Polling I2C address `0x3C` (the SSD1306) for register reads while pressing buttons yields zero changes. The buttons are pure GPIO; do not look for them on I2C. No other I2C addresses appear when the module is connected.

---

## Application

A multi-screen system status display that uses the Industria OLED for headless Pi monitoring. Six screens, navigated with the buttons (any button → tap = next, hold = previous):

| # | Screen | Shows |
|---|---|---|
| 1 | **system** | hostname, time/uptime, CPU summary (% / temp / freq), per-core scrolling sparklines, MEM bar with load average |
| 2 | **network** | IPv4 address per interface, current TX/RX rates, dual-channel sparkline (TX above midline, RX below) |
| 3 | **wifi** | Wi-Fi join QR code (decoded from active NetworkManager AP-mode connection), SSID / band / channel / auth / connected-client count |
| 4 | **disk** | Primary block-device model name (read from `/sys/block/<dev>/device/model`), per-mount usage with progress bars |
| 5 | **gps** | Lock status (`no` / `2D` / `3D`), satellites used / visible, lat/lon/alt, horizontal/vertical accuracy, PPS pulse indicator, polar sky map of visible satellites — graceful "no gpsd" / "no fix" handling when the receiver isn't ready |
| 6 | **help** | Button cheat-sheet |

The wifi screen auto-discovers the active AP-mode connection from NetworkManager and reads the PSK without requiring root, on default polkit policies.

The GPS screen connects to a local `gpsd` (port 2947) over its JSON socket — no extra Python deps. Works with any gpsd-supported receiver; tested with the Waveshare LC29H multi-GNSS module via USB-UART (CP2102N).

---

## Requirements

- Raspberry Pi 5 (other Pis likely work but untested)
- Debian 13 (Trixie) or compatible
- `libgpiod` v2 (`python3-libgpiod 2.x`)
- I2C-1 enabled (`raspi-config`)
- The current user in `i2c` and `gpio` groups
- `uv` (or any other PEP 517 / 621 runner)

For the optional features:
- **GPS screen**: `gpsd` with a configured device (e.g. `sudo gpsdctl add /dev/ttyUSB0`)
- **Wifi QR screen**: NetworkManager with an AP-mode connection on the wireless interface

---

## Install

```bash
git clone https://github.com/cameronzucker/argon-forty-display.git
cd argon-forty-display
uv venv --system-site-packages
uv sync
```

The `--system-site-packages` flag matters: it lets the venv use the Debian-packaged `python3-libgpiod` (built against the system `libgpiod3` C library) instead of pulling a separate `gpiod` from PyPI. On ARM that's the path of least friction.

## Run

```bash
uv run python -m argon_oled
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--i2c-port` | `1` | I2C bus number |
| `--i2c-address` | `0x3C` | OLED I2C address |
| `--gpiochip` | `/dev/gpiochip0` | GPIO chip path |
| `--button-line` | `4` | BCM GPIO line for the button signal |
| `--long-press-ms` | `700` | Threshold for short vs long press |
| `--debounce-ms` | `50` | Software debounce window |
| `--no-buttons` | off | Disable the button watcher (e.g. for testing in a contested env) |
| `--hotspot-connection` | autodetect | Override the NM connection name for the wifi screen |
| `--frame-ms` | `100` | Render frame period |
| `--log-level` | `INFO` | Python logging level |

## Run at boot (systemd)

Once you've confirmed the app works in the foreground, install it as a system service so it starts automatically on every boot:

```bash
sudo install -m 644 systemd/argon-oled.service /etc/systemd/system/
sudo sed -i "s|<USER>|$USER|g; s|<HOME>|$HOME|g" /etc/systemd/system/argon-oled.service
sudo systemctl daemon-reload
sudo systemctl enable --now argon-oled
```

The unit runs as your normal login user (no root) — the user just needs to be in `i2c` and `gpio` (which is the case if the foreground command worked). Logs go to the journal:

```bash
journalctl -u argon-oled -f
```

To stop or remove later:

```bash
sudo systemctl disable --now argon-oled
sudo rm /etc/systemd/system/argon-oled.service
sudo systemctl daemon-reload
```

## Diagnostic scripts

- `scripts/hello.py` — minimum SSD1306 smoke test. Draws a four-line test pattern. Use this first if the display isn't responding.
- `scripts/probe_buttons.py` — the tool used to reverse-engineer the button protocol. Watches every unclaimed GPIO header line on `gpiochip0` for edge events with `PULL_UP` bias and concurrently polls I2C `0x3C` for changing reads. Press buttons in known sequences and read the per-line summary on Ctrl-C.

---

## Architecture

```
argon_oled/
├── app.py        # render loop, button-event drain, CLI
├── buttons.py    # debounced GPIO watcher → ButtonEvent (SHORT / LONG)
├── metrics.py    # SystemSnapshot dataclass (psutil, /proc, /sys)
├── hotspot.py    # nmcli wrapper, Wi-Fi QR payload builder
├── gps.py        # background gpsd JSON-socket client
├── screens.py    # Screen protocol + StatusScreen, NetworkScreen,
│                 # HotspotScreen, DiskScreen, GPSScreen, HelpScreen,
│                 # ScreenCarousel
├── __main__.py   # `python -m argon_oled` entry point
└── __init__.py
```

Each screen renders into its own Pillow `Image` per frame; the carousel composites the active screen and adds a top-right index hint. Metrics and rendering run at separate cadences so animations (sparklines, PPS blink) don't gate on slow data sources.

---

## Acknowledgments / prior art

These projects independently arrived at "BCM GPIO 4 + press patterns" without naming the underlying parallel-wiring constraint. They were the starting point that confirmed we were on the right pin:

- [BenWolstencroft/home-assistant-addons — argon-oled-addon](https://github.com/BenWolstencroft/home-assistant-addons/tree/main/argon-oled-addon)
- [g8keeper22/Pi-Hole-Data-on-Argon-ONE-V5-OLED-Display](https://github.com/g8keeper22/Pi-Hole-Data-on-Argon-ONE-V5-OLED-Display)
- [forum-raspberrypi.de thread #64770](https://forum-raspberrypi.de/forum/thread/64770-argon-one-v5-oled-display/) — useful for the libgpiod v1 → v2 transition on Trixie

If you're building software for this module and you find evidence that something in the *Hardware reference* section is wrong on your specific board revision, please open an issue.

## License

MIT — see [LICENSE](LICENSE).

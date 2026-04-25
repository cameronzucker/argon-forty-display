"""Phase 1 smoke test: prove the SSD1306 at i2c-1 / 0x3C draws pixels.

Failure-mode diagnostics: if device init throws, dump i2cdetect output so we
can tell at a glance whether the bus saw the panel.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

I2C_PORT = 1
I2C_ADDRESS = 0x3C

log = logging.getLogger("argon_oled.hello")


def i2cdetect_dump(port: int) -> str:
    try:
        out = subprocess.run(
            ["i2cdetect", "-y", str(port)],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout or out.stderr
    except FileNotFoundError:
        return "(i2cdetect not installed)"
    except subprocess.TimeoutExpired:
        return "(i2cdetect timed out — bus stuck?)"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info("Opening SSD1306 on i2c-%d @ 0x%02X", I2C_PORT, I2C_ADDRESS)
    try:
        serial = i2c(port=I2C_PORT, address=I2C_ADDRESS)
        device = ssd1306(serial, width=128, height=64)
    except Exception as e:
        log.error("Device init failed: %s", e)
        log.error("i2cdetect -y %d output:\n%s", I2C_PORT, i2cdetect_dump(I2C_PORT))
        return 1

    log.info("Init OK. Drawing test pattern.")
    with canvas(device) as draw:
        draw.rectangle(device.bounding_box, outline="white", fill="black")
        draw.text((4, 4), "Argon Industria", fill="white")
        draw.text((4, 18), "OLED — Phase 1", fill="white")
        draw.text((4, 36), "i2c-1 @ 0x3C", fill="white")
        draw.text((4, 50), "luma.oled OK", fill="white")

    log.info("Holding display for 10s...")
    time.sleep(10)
    device.clear()
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

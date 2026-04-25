"""GPIO 4 button handler for the Argon Industria OLED.

Both buttons on the module are wired in parallel on GPIO 4 — there is no
electrical way to distinguish them. We classify by press duration:

- SHORT: low-pulse < `long_press_ms` (default 700ms)
- LONG:  low-pulse >= `long_press_ms`

Bounce is severe on at least one of the two buttons; we apply a 50ms ignore
window after each accepted edge.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
from datetime import timedelta

import gpiod
from gpiod.line import Bias, Direction, Edge

log = logging.getLogger(__name__)

DEFAULT_GPIOCHIP = "/dev/gpiochip0"
DEFAULT_LINE = 4


class ButtonEvent(enum.Enum):
    SHORT = "short"
    LONG = "long"


class ButtonWatcher(threading.Thread):
    """Watches a single GPIO line and emits classified press events.

    Events are pushed onto `events` (a queue.Queue). The main thread should
    drain it non-blockingly each frame. Internal state machine:

        idle      --FALLING--> pressed (record t_press)
        pressed   --RISING-->  classify by (now - t_press), emit event,
                                start ignore-window timer
        pressed   --FALLING--> ignored (within ignore window)
    """

    def __init__(
        self,
        events: queue.Queue[ButtonEvent],
        chip_path: str = DEFAULT_GPIOCHIP,
        line: int = DEFAULT_LINE,
        long_press_ms: int = 700,
        debounce_ms: int = 50,
        consumer: str = "argon-oled-buttons",
    ):
        super().__init__(daemon=True, name="argon-oled-buttons")
        self.events = events
        self.chip_path = chip_path
        self.line = line
        self.long_press_ns = long_press_ms * 1_000_000
        self.debounce_ns = debounce_ms * 1_000_000
        self.consumer = consumer
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            bias=Bias.PULL_UP,
            edge_detection=Edge.BOTH,
        )
        try:
            request = gpiod.request_lines(
                self.chip_path,
                consumer=self.consumer,
                config={self.line: settings},
            )
        except OSError as e:
            log.error("Cannot claim %s line %d: %s", self.chip_path, self.line, e)
            return

        log.info("Watching GPIO%d on %s for button events", self.line, self.chip_path)
        try:
            t_press_ns = 0
            last_accepted_ns = 0
            in_press = False

            while not self._stop.is_set():
                if not request.wait_edge_events(timedelta(milliseconds=200)):
                    continue
                for ev in request.read_edge_events():
                    rising = (ev.event_type == ev.Type.RISING_EDGE)
                    ts = ev.timestamp_ns

                    # Debounce: ignore edges within `debounce_ns` of the last
                    # accepted state change.
                    if ts - last_accepted_ns < self.debounce_ns:
                        continue

                    if not rising and not in_press:
                        in_press = True
                        t_press_ns = ts
                        last_accepted_ns = ts
                    elif rising and in_press:
                        duration_ns = ts - t_press_ns
                        kind = (
                            ButtonEvent.LONG
                            if duration_ns >= self.long_press_ns
                            else ButtonEvent.SHORT
                        )
                        log.info("Button %s press (%.0fms)",
                                 kind.value, duration_ns / 1e6)
                        self.events.put(kind)
                        in_press = False
                        last_accepted_ns = ts
                    # Other transitions (rising-while-idle, falling-while-pressed)
                    # are spurious bounces or duplicate kernel events — ignore.
        finally:
            try:
                request.release()
            except Exception:
                pass
            log.info("Button watcher stopped")

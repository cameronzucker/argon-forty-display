"""Minimal gpsd client. Talks JSON over a TCP socket so we don't pull in
the full ``gps`` Python package as a dependency.

Spawns a daemon thread that maintains a connection to gpsd, parses TPV
(time/position/velocity), SKY (satellites visible/used), and PPS messages,
and exposes the latest snapshot via thread-safe properties. Reconnects with
backoff if gpsd is unreachable.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2947


class GPSDClient(threading.Thread):
    """Background thread reader of gpsd messages. Cheap to instantiate even
    when gpsd isn't running — it just retries indefinitely.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 reconnect_delay: float = 2.0):
        super().__init__(daemon=True, name="gpsd-client")
        self._host = host
        self._port = port
        self._reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tpv: dict[str, Any] | None = None
        self._sky: dict[str, Any] | None = None
        self._error: str | None = "starting"
        self._last_pps_ns: int = 0
        self._connected = False

    def stop(self) -> None:
        self._stop.set()

    @property
    def tpv(self) -> dict[str, Any] | None:
        with self._lock:
            return self._tpv

    @property
    def sky(self) -> dict[str, Any] | None:
        with self._lock:
            return self._sky

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_pps_ns(self) -> int:
        with self._lock:
            return self._last_pps_ns

    def _set_error(self, msg: str | None) -> None:
        with self._lock:
            self._error = msg
            if msg is not None:
                self._connected = False

    def _set_connected(self) -> None:
        with self._lock:
            self._error = None
            self._connected = True

    def _consume(self, msg: dict[str, Any]) -> None:
        cls = msg.get("class")
        with self._lock:
            if cls == "TPV":
                self._tpv = msg
            elif cls == "SKY":
                self._sky = msg
            elif cls in ("PPS", "TOFF"):
                self._last_pps_ns = time.monotonic_ns()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection(
                    (self._host, self._port), timeout=2.0
                ) as s:
                    s.settimeout(1.0)
                    s.sendall(
                        b'?WATCH={"enable":true,"json":true,"pps":true}\n'
                    )
                    self._set_connected()
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                self._consume(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                self._set_error(type(e).__name__)
                log.debug("gpsd connect failed: %s", e)
            self._stop.wait(self._reconnect_delay)

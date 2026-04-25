"""System metrics gathering. No display dependencies — pure data."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime

import psutil

log = logging.getLogger(__name__)

_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"


@dataclass(frozen=True)
class SystemSnapshot:
    timestamp: datetime
    hostname: str
    primary_ip: str
    cpu_percent: float
    cpu_per_core: tuple[float, ...]
    cpu_freq_mhz: float | None
    cpu_temp_c: float | None
    mem_used_pct: float
    mem_used_mb: int
    mem_total_mb: int
    load_1m: float
    uptime_s: int


def _primary_ip() -> str:
    """Return the local IP that would be used to reach the public internet.

    Uses a connectionless UDP socket so no packets are actually sent. Falls
    back to "no-route" if the kernel has no default route.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("1.1.1.1", 80))
            return s.getsockname()[0]
    except OSError:
        return "no-route"


def _cpu_temp_c() -> float | None:
    try:
        with open(_THERMAL_ZONE, "r") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError) as e:
        log.debug("CPU temp unavailable: %s", e)
        return None


def _uptime_s() -> int:
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError):
        return 0


def gather() -> SystemSnapshot:
    """Snapshot the current system state. Cheap; safe to call once per second."""
    vm = psutil.virtual_memory()
    per_core = tuple(psutil.cpu_percent(percpu=True, interval=None))
    cpu_total = sum(per_core) / len(per_core) if per_core else 0.0
    freq = psutil.cpu_freq()
    return SystemSnapshot(
        timestamp=datetime.now(),
        hostname=socket.gethostname(),
        primary_ip=_primary_ip(),
        cpu_percent=cpu_total,
        cpu_per_core=per_core,
        cpu_freq_mhz=freq.current if freq else None,
        cpu_temp_c=_cpu_temp_c(),
        mem_used_pct=vm.percent,
        mem_used_mb=int(vm.used / (1024 * 1024)),
        mem_total_mb=int(vm.total / (1024 * 1024)),
        load_1m=os.getloadavg()[0],
        uptime_s=_uptime_s(),
    )


def format_uptime(seconds: int) -> str:
    """Compact uptime: 12s, 4m, 3h12m, 2d4h."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"

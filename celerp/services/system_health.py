# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import psutil

_GB = 1024 ** 3

_RAM_WARN = 80.0
_RAM_CRIT = 90.0
_CPU_WARN = 90.0
_DISK_WARN = 80.0
_DISK_CRIT = 90.0

_SEVERITY_ORDER = {"ok": 0, "warning": 1, "critical": 2}

_MESSAGES = {
    "ram_warning": (
        "Your computer is running low on memory. Performance may be affected."
        " Consider closing other applications."
    ),
    "ram_critical": (
        "Your computer is critically low on memory. Celerp may slow down or become"
        " unresponsive. Upgrade your RAM or close other applications."
    ),
    "cpu_warning": "Your computer's processor is under heavy load. Response times may be slow.",
    "disk_warning": "Your disk is getting full. Free up space to keep Celerp running smoothly.",
    "disk_critical": (
        "Your disk is almost full. Celerp may stop working if disk space runs out."
        " Free up space immediately."
    ),
}


def _threshold(value: float, warn: float, crit: float | None) -> tuple[str, str | None]:
    """Return (status, message_key_suffix | None) for a metric."""
    if crit is not None and value > crit:
        return "critical", "critical"
    if value > warn:
        return "warning", "warning"
    return "ok", None


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda s: _SEVERITY_ORDER[s])


def get_system_health() -> dict:
    mem = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=1)
    disk = psutil.disk_usage("/")

    ram_status, ram_suffix = _threshold(mem.percent, _RAM_WARN, _RAM_CRIT)
    cpu_status, cpu_suffix = _threshold(cpu_pct, _CPU_WARN, None)
    disk_status, disk_suffix = _threshold(disk.percent, _DISK_WARN, _DISK_CRIT)

    return {
        "ram": {
            "used_percent": mem.percent,
            "used_gb": round(mem.used / _GB, 2),
            "total_gb": round(mem.total / _GB, 2),
            "status": ram_status,
            "message": _MESSAGES.get(f"ram_{ram_suffix}") if ram_suffix else None,
        },
        "cpu": {
            "used_percent": cpu_pct,
            "status": cpu_status,
            "message": _MESSAGES.get(f"cpu_{cpu_suffix}") if cpu_suffix else None,
        },
        "disk": {
            "used_percent": disk.percent,
            "free_gb": round(disk.free / _GB, 2),
            "total_gb": round(disk.total / _GB, 2),
            "status": disk_status,
            "message": _MESSAGES.get(f"disk_{disk_suffix}") if disk_suffix else None,
        },
        "overall": _worst(ram_status, cpu_status, disk_status),
    }

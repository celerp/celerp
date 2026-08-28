# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

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
}


def _disk_message(suffix: str, used_percent: float, free_gb: float, total_gb: float) -> str:
    """A disk warning that names the numbers the user needs to act: how much is free
    now, the total, and how much to free to clear the warning. The clear target is the
    warning threshold (used <= _DISK_WARN), so the same actionable figure is shown for
    both warning and critical - it is what makes the message disappear."""
    to_free = round(max(0.0, (used_percent - _DISK_WARN) / 100.0 * total_gb), 1)
    where = f"{free_gb:.1f} GB free of {total_gb:.1f} GB"
    if suffix == "critical":
        return (f"Your disk is almost full: {where}. Free up about {to_free:.1f} GB to"
                " clear this warning, or Celerp may stop working if space runs out.")
    return (f"Your disk is getting full: {where}. Free up about {to_free:.1f} GB more"
            " to clear this warning.")


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
    cpu_pct = psutil.cpu_percent(interval=0.1)
    disk = psutil.disk_usage("/")

    ram_status, ram_suffix = _threshold(mem.percent, _RAM_WARN, _RAM_CRIT)
    cpu_status, cpu_suffix = _threshold(cpu_pct, _CPU_WARN, None)
    disk_status, disk_suffix = _threshold(disk.percent, _DISK_WARN, _DISK_CRIT)
    disk_free_gb = round(disk.free / _GB, 2)
    disk_total_gb = round(disk.total / _GB, 2)

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
            "free_gb": disk_free_gb,
            "total_gb": disk_total_gb,
            "status": disk_status,
            "message": _disk_message(disk_suffix, disk.percent, disk_free_gb, disk_total_gb) if disk_suffix else None,
        },
        "overall": _worst(ram_status, cpu_status, disk_status),
    }

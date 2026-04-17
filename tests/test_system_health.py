# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from celerp.services.system_health import get_system_health

_GB = 1024 ** 3


def make_mem(percent: float, used_gb: float, total_gb: float) -> MagicMock:
    m = MagicMock()
    m.percent = percent
    m.used = int(used_gb * _GB)
    m.total = int(total_gb * _GB)
    return m


def make_disk(percent: float, free_gb: float, total_gb: float) -> MagicMock:
    d = MagicMock()
    d.percent = percent
    d.free = int(free_gb * _GB)
    d.total = int(total_gb * _GB)
    return d


def _call(mem_pct, mem_used, mem_total, cpu_pct, disk_pct, disk_free, disk_total):
    with (
        patch("psutil.virtual_memory", return_value=make_mem(mem_pct, mem_used, mem_total)),
        patch("psutil.cpu_percent", return_value=cpu_pct),
        patch("psutil.disk_usage", return_value=make_disk(disk_pct, disk_free, disk_total)),
    ):
        return get_system_health()


# -- helpers for standard "all ok" values
_OK_MEM = (50.0, 4.0, 8.0)
_OK_CPU = 30.0
_OK_DISK = (50.0, 50.0, 100.0)


def test_all_ok():
    result = _call(*_OK_MEM, _OK_CPU, *_OK_DISK)
    assert result["ram"]["status"] == "ok"
    assert result["cpu"]["status"] == "ok"
    assert result["disk"]["status"] == "ok"
    assert result["overall"] == "ok"
    assert result["ram"]["message"] is None
    assert result["cpu"]["message"] is None
    assert result["disk"]["message"] is None


def test_ram_warning():
    result = _call(85.0, 6.8, 8.0, _OK_CPU, *_OK_DISK)
    assert result["ram"]["status"] == "warning"
    assert "memory" in result["ram"]["message"].lower()
    assert result["overall"] == "warning"


def test_ram_critical():
    result = _call(92.0, 7.4, 8.0, _OK_CPU, *_OK_DISK)
    assert result["ram"]["status"] == "critical"
    assert "critically" in result["ram"]["message"].lower()
    assert result["overall"] == "critical"


def test_cpu_warning():
    result = _call(*_OK_MEM, 95.0, *_OK_DISK)
    assert result["cpu"]["status"] == "warning"
    assert "processor" in result["cpu"]["message"].lower()


def test_disk_warning():
    result = _call(*_OK_MEM, _OK_CPU, 85.0, 15.0, 100.0)
    assert result["disk"]["status"] == "warning"
    assert "full" in result["disk"]["message"].lower()


def test_disk_critical():
    result = _call(*_OK_MEM, _OK_CPU, 93.0, 7.0, 100.0)
    assert result["disk"]["status"] == "critical"
    assert "almost full" in result["disk"]["message"].lower()
    assert result["overall"] == "critical"


def test_overall_worst_wins():
    # RAM warning + disk critical = overall critical
    result = _call(85.0, 6.8, 8.0, _OK_CPU, 93.0, 7.0, 100.0)
    assert result["ram"]["status"] == "warning"
    assert result["disk"]["status"] == "critical"
    assert result["overall"] == "critical"


def test_overall_warning_no_critical():
    # RAM warning + CPU warning = overall warning
    result = _call(85.0, 6.8, 8.0, 95.0, *_OK_DISK)
    assert result["ram"]["status"] == "warning"
    assert result["cpu"]["status"] == "warning"
    assert result["overall"] == "warning"


def test_message_none_when_ok():
    result = _call(*_OK_MEM, _OK_CPU, *_OK_DISK)
    assert result["ram"]["message"] is None
    assert result["cpu"]["message"] is None
    assert result["disk"]["message"] is None


def test_return_shape():
    result = _call(*_OK_MEM, _OK_CPU, *_OK_DISK)
    assert set(result.keys()) == {"ram", "cpu", "disk", "overall"}
    assert set(result["ram"].keys()) == {"used_percent", "used_gb", "total_gb", "status", "message"}
    assert set(result["cpu"].keys()) == {"used_percent", "status", "message"}
    assert set(result["disk"].keys()) == {"used_percent", "free_gb", "total_gb", "status", "message"}

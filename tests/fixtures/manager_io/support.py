# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Helpers shared by the Manager adapter tests."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from . import specs

HERE = Path(__file__).resolve().parent
BASIC = HERE / "basic.manager"
FX = HERE / "fx.manager"
INVENTORY = HERE / "inventory.manager"
CHECKPOINTS = json.loads((HERE / "checkpoints.json").read_text())

# Measures whose checkpoint values carry a currency: [currency, amount].
_WITH_CURRENCY = {"bank_cash", "document_count", "document_total", "settlement_allocation"}
# Measures that count or measure quantity rather than money: no currency.
_NO_CURRENCY = {"document_status", "inventory_quantity"}
_MEASURES = (
    "debits_equal_credits", "trial_balance", "ar_control", "ar_by_customer", "ap_control", "ap_by_supplier",
    "bank_cash", "inventory_quantity", "inventory_value", "tax_control", "document_count", "document_total",
    "document_status", "settlement_allocation",
)


def artifact(path: Path, name: str | None = None):
    from celerp.importers.adapters.base import Artifact

    data = Path(path).read_bytes()
    return Artifact(path=Path(path), original_name=name or Path(path).name, size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest())


def adapter():
    from celerp.importers.adapters.manager_io import ManagerIOAdapter

    return ManagerIOAdapter()


def ref(label: str) -> str:
    """The source id of a labelled fixture object; `@Type` names a built-in account."""
    return str(specs.T[label[1:]]) if label.startswith("@") else str(specs.k(label))


def expected_rows(section: dict, base_currency: str) -> set[tuple[str, str, str | None, Decimal]]:
    """Checkpoint figures as (measure, key, currency, expected) rows."""
    rows: set[tuple[str, str, str | None, Decimal]] = set()
    for measure in _MEASURES:
        if measure not in section:
            continue
        value = section[measure]
        if measure == "debits_equal_credits":
            rows.add((measure, "", base_currency, Decimal(value)))
            continue
        for label, figure in value.items():
            key = label if measure in ("document_count", "document_total", "document_status",
                                       "settlement_allocation") else ref(label)
            if measure in _WITH_CURRENCY:
                currency, amount = figure
                rows.add((measure, key, currency, Decimal(str(amount))))
            elif measure in _NO_CURRENCY:
                rows.add((measure, key, None, Decimal(str(figure))))
            else:
                rows.add((measure, key, base_currency, Decimal(figure)))
    return rows


def actual_rows(expectations) -> set[tuple[str, str, str | None, Decimal]]:
    rows = set()
    for row in expectations.expectations:
        assert row.tolerance.kind == "exact", row
        rows.add((str(row.measure), row.key, row.currency, Decimal(row.expected)))
    return rows

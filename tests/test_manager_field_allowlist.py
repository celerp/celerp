# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A carried Manager record with a populated field Celerp neither reads nor has classified
stops the scan, naming the field, rather than being carried without what it records."""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal as D

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.adapters.manager_io.book import INVENTORY_BLOCKERS
from celerp.importers.adapters.manager_io.types import CONTENT_TYPES, TYPE_NAMES
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.encoder import _key, guid_message, varint
from fixtures.manager_io.support import adapter, artifact

EXTRA_GUID_FIELD = guid_message(specs.k("CA")) + _key(3, 0) + varint(7)


def _with_line(field: int, index: int, extra: dict):
    def change(fields: dict) -> dict:
        lines = list(fields[field])
        lines[index] = {**lines[index], **extra}
        return {**fields, field: lines}
    return change


# (record label, change to its fields, coverage label, field path named)
CASES = [
    ("INVD", lambda f: {**f, 90: "x"}, "SalesInvoice (unknown field)", "90"),
    ("INVD", _with_line(49, 0, {90: D("1")}), "SalesInvoice (unknown field)", "49.90"),
    ("INVD", _with_line(49, 0, {18: {1: 4, 4: 1}}), "SalesInvoice (unknown field)", "49.18.4"),
    ("INVD", lambda f: {**f, 3: EXTRA_GUID_FIELD}, "SalesInvoice (unknown field)", "3.3"),
    ("CA", lambda f: {**f, 90: 1}, "Customer (unknown field)", "90"),
    ("DN1", _with_line(15, 0, {9: "x"}), "DeliveryNote (unknown field)", "15.9"),
    ("R1", _with_line(11, 0, {90: 1}), "Receipt (unknown field)", "11.90"),
]


def _source(tmp_path, label: str, change):
    objects = [dataclasses.replace(o, fields=change(o.fields)) if o.key == specs.k(label) else o
               for o in specs.inventory_lifecycle_objects()]
    return artifact(specs.write_manager_file(tmp_path / "extra.manager", objects))


@pytest.mark.parametrize("label, change, blocked, path", CASES, ids=[f"{c[0]}:{c[3]}" for c in CASES])
def test_unknown_populated_field_fails_the_scan_naming_it(tmp_path, label, change, blocked, path):
    """RED before the change: the extra field is skipped without a word and the record is
    carried as though it said nothing more."""
    source = _source(tmp_path, label, change)
    (row,) = [r for r in adapter().inspect([source]).coverage if r.source_type == blocked]
    assert row.count == 1 and row.coverage_class == CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER
    assert f"Field {path} " in row.note
    with pytest.raises(ScanError, match=re.escape(f"{blocked} (1)")):
        adapter().build_manifest([source], MigrationDecisions(mode=CIFMode.FULL_HISTORY))


def test_every_carried_type_has_a_field_allowlist():
    """RED before the change: no carried type lists the fields it may hold."""
    from celerp.importers.adapters.manager_io.fields import SCHEMAS

    carried = {TYPE_NAMES[ctype] for ctype, klass in CONTENT_TYPES.items()
               if klass in (CoverageClass.MAPPED, CoverageClass.MAPPED_WITH_LOSS)
               and TYPE_NAMES.get(ctype) and TYPE_NAMES[ctype] not in INVENTORY_BLOCKERS}
    assert carried - set(SCHEMAS) == set()
    assert set(SCHEMAS) - carried == set()


def test_populated_lost_fields_are_disclosed_as_mapped_with_loss():
    """RED before the change: the payer name typed on a receipt and the time a file was
    attached are left behind with no coverage row saying so."""
    from fixtures.manager_io.support import BASIC

    rows = {r.source_type: r for r in adapter().inspect([artifact(BASIC)]).coverage}
    for label in ("Receipt (payer name)", "Attachment (attach time)"):
        assert rows[label].count == 1 and rows[label].coverage_class == CoverageClass.MAPPED_WITH_LOSS, label
        assert rows[label].note
    # Protobuf bookkeeping, such as a date's DateTime kind, is not listed as a loss.
    assert not [label for label in rows if "kind" in label.lower()]

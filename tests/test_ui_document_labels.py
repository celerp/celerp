# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Pages name a document by the number it shows (``ref_id``), never by its storage key.

A storage key with its ``doc:`` prefix cut off reads like a number but is not one: a
document numbered after it was created, or a proforma turned into an invoice, keeps its old
key. The UI therefore never builds a label by cutting the prefix off a key; it reads the
number the API returns for that document."""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"


def test_no_page_labels_a_document_by_its_storage_key():
    hits = [f"{p.relative_to(UI.parent)}:{n}" for p in sorted(UI.rglob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if 'removeprefix("doc:")' in line or "removeprefix('doc:')" in line]
    assert hits == [], hits

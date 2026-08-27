# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Canonical inventory code limits and validation - one source of truth.

The SKU/barcode invariants live here so the event boundary (celerp.events.schemas),
the interactive inventory routes, the allocation service, and the scanner all share
the exact same rules. A comma is Celerp's OR operator in the SKU/search syntax, so a
SKU may never contain one (it would split into separate codes wherever SKUs are
matched). A barcode is a numeric physical-lot identifier and must be unique per
company - enforced at the event boundary (format) and by a partial unique index on
the projections table (uniqueness).
"""

from __future__ import annotations

from typing import Any

MAX_BARCODE_LEN = 64
MAX_SKU_LEN = 255
MAX_SCAN_CODE_LEN = max(MAX_BARCODE_LEN, MAX_SKU_LEN)

# Partial unique index enforcing at-most-one item per (company, barcode). Declared on
# the Projection model so create_all builds it for the test schema, and created on
# existing databases by the barcode-uniqueness migration. Named here so the applier
# can tell a barcode violation apart from the (company_id, entity_id) primary-key race.
BARCODE_UNIQUE_INDEX = "uq_projection_company_item_barcode"

SKU_COMMA_MESSAGE = "SKU cannot contain a comma"
SKU_TOO_LONG_MESSAGE = f"SKU cannot exceed {MAX_SKU_LEN} characters"
BARCODE_NOT_DIGITS_MESSAGE = "Barcode must contain only digits"
BARCODE_TOO_LONG_MESSAGE = f"Barcode cannot exceed {MAX_BARCODE_LEN} characters"
BARCODE_CONFLICT_MESSAGE = "Barcode '{barcode}' already exists"


def reject_comma_sku(sku: Any) -> Any:
    """Reject a comma-bearing SKU. Returns the sku for chaining."""
    if sku is not None and "," in str(sku):
        raise ValueError(SKU_COMMA_MESSAGE)
    return sku


def validate_sku(sku: Any) -> Any:
    """Reject a comma-bearing or over-long SKU. Returns the sku for chaining."""
    reject_comma_sku(sku)
    if sku is not None and len(str(sku)) > MAX_SKU_LEN:
        raise ValueError(SKU_TOO_LONG_MESSAGE)
    return sku


def validate_barcode(barcode: Any) -> Any:
    """Reject a non-digit or over-long barcode. An absent/empty barcode is allowed."""
    if barcode is None:
        return barcode
    s = str(barcode)
    if s == "":
        return barcode
    if not s.isdigit():
        raise ValueError(BARCODE_NOT_DIGITS_MESSAGE)
    if len(s) > MAX_BARCODE_LEN:
        raise ValueError(BARCODE_TOO_LONG_MESSAGE)
    return barcode


class BarcodeConflictError(Exception):
    """A write violated the (company, item, barcode) uniqueness invariant.

    Raised by the projection applier when the DB unique index rejects an insert - the
    final defense for any writer that bypasses the application lock (imports,
    connectors, a future writer). The API layer maps it to 409.
    """

    def __init__(self, barcode: str | None = None):
        self.barcode = barcode
        super().__init__(
            BARCODE_CONFLICT_MESSAGE.format(barcode=barcode) if barcode else "Barcode already exists"
        )


def is_barcode_unique_violation(exc: Exception) -> bool:
    """True when an IntegrityError is the barcode unique-index violation (not the PK race)."""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "constraint_name", None) == BARCODE_UNIQUE_INDEX:
        return True
    # Expression-index violations do not always populate constraint_name across drivers;
    # the index name still appears in the server message.
    return BARCODE_UNIQUE_INDEX in str(exc)

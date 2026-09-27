# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Canonical inventory code limits and validation - one source of truth.

The SKU/barcode invariants live here so the event boundary (celerp.events.schemas),
the interactive inventory routes, the allocation service, and the scanner all share
the exact same rules. A comma is Celerp's OR operator in the SKU/search syntax, so a
SKU may never contain one (it would split into separate codes wherever SKUs are
matched). A barcode is a numeric physical-lot identifier. Format is enforced at the
event boundary schema; physical-code uniqueness is enforced when a write introduces
a code (celerp.services.physical_codes), never by a database index, so existing
duplicate data can always be upgraded and then resolved by the user.
"""

from __future__ import annotations

from typing import Any

MAX_BARCODE_LEN = 64
MAX_SKU_LEN = 255
# RFID EPC codes are alphanumeric/hex tag payloads; 255 covers even long GS1 EPC
# encodings with room to spare. Kept as a named limit so the validator, the schema,
# and the scanner all agree.
MAX_RFID_EPC_LEN = 255
# GTINs are 8/12/13/14-digit product codes; the longest is GTIN-14.
GTIN_LENGTHS = frozenset({8, 12, 13, 14})
MAX_SCAN_CODE_LEN = max(MAX_BARCODE_LEN, MAX_SKU_LEN, MAX_RFID_EPC_LEN)

# Statuses whose items no longer resolve by physical code. A merged source keeps its
# codes for history but is excluded by the resolver and by Doctor's conflict report.
PHYSICAL_CODE_RESOLVE_EXCLUDED_STATUSES = frozenset({"merged"})
# Names of the physical-code unique indexes earlier releases created. Uniqueness is no
# longer an index; the names remain so migrations can drop them and so a stale index
# left on a database still surfaces as a 409 instead of a 500.
LEGACY_BARCODE_UNIQUE_INDEX = "uq_projection_company_item_barcode"
BARCODE_UNIQUE_INDEX = "uq_projection_company_resolvable_item_barcode"
RFID_EPC_UNIQUE_INDEX = "uq_projection_company_item_rfid_epc"

SKU_COMMA_MESSAGE = "SKU cannot contain a comma"
SKU_TOO_LONG_MESSAGE = f"SKU cannot exceed {MAX_SKU_LEN} characters"
BARCODE_NOT_DIGITS_MESSAGE = "Barcode must contain only digits"
BARCODE_TOO_LONG_MESSAGE = f"Barcode cannot exceed {MAX_BARCODE_LEN} characters"
BARCODE_CONFLICT_MESSAGE = "Barcode '{barcode}' already exists"
GTIN_NOT_DIGITS_MESSAGE = "GTIN must contain only digits"
GTIN_LENGTH_MESSAGE = "GTIN must be 8, 12, 13, or 14 digits"
RFID_EPC_TOO_LONG_MESSAGE = f"RFID / EPC cannot exceed {MAX_RFID_EPC_LEN} characters"
RFID_EPC_NOT_ALNUM_MESSAGE = "RFID / EPC must contain only letters and digits"
RFID_EPC_CONFLICT_MESSAGE = "RFID / EPC '{code}' already exists"


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


def validate_gtin(gtin: Any) -> Any:
    """Reject a non-digit or wrong-length GTIN. An absent/empty GTIN is allowed.

    A GTIN identifies a PRODUCT (not one physical lot), so it is stored as a string to
    preserve leading zeros, is not padded, is not checksum-validated, and is not unique.
    Only the digits-only and length {8,12,13,14} format rules are enforced.
    """
    if gtin is None:
        return gtin
    s = str(gtin)
    if s == "":
        return gtin
    if not s.isdigit():
        raise ValueError(GTIN_NOT_DIGITS_MESSAGE)
    if len(s) not in GTIN_LENGTHS:
        raise ValueError(GTIN_LENGTH_MESSAGE)
    return gtin


def normalize_rfid_epc(code: Any) -> Any:
    """Trim and upper-case an RFID / EPC value; leave None/empty as-is.

    The canonical stored and looked-up form of an EPC is trimmed + upper-cased, so a
    scan resolves regardless of the case the reader emits. Applied at every write and
    lookup boundary.
    """
    if code is None:
        return code
    s = str(code).strip()
    if s == "":
        return s
    return s.upper()


def validate_rfid_epc(code: Any) -> Any:
    """Validate and normalize an RFID / EPC. An absent/empty value is allowed.

    Trims and upper-cases (the canonical form), requires alphanumeric characters (hex is
    a subset), and bounds the length. Returns the normalized value for storage.
    """
    if code is None:
        return code
    s = normalize_rfid_epc(code)
    if s == "":
        return s
    if not s.isalnum():
        raise ValueError(RFID_EPC_NOT_ALNUM_MESSAGE)
    if len(s) > MAX_RFID_EPC_LEN:
        raise ValueError(RFID_EPC_TOO_LONG_MESSAGE)
    return s


class CodeConflictError(Exception):
    """Base for a physical-code uniqueness violation (barcode or RFID / EPC).

    One base so the API layer needs a single exception handler that maps every physical
    code collision to 409.
    """


class BarcodeConflictError(CodeConflictError):
    """A write introduced a barcode another item in the company already holds.

    Raised by the event boundary's physical-code check (or by the projection applier if
    a stale unique index from an earlier release rejects a write). The API layer maps
    it to 409.
    """

    def __init__(self, barcode: str | None = None):
        self.barcode = barcode
        super().__init__(
            BARCODE_CONFLICT_MESSAGE.format(barcode=barcode) if barcode else "Barcode already exists"
        )


class RfidEpcConflictError(CodeConflictError):
    """A write introduced an RFID / EPC another item in the company already holds.

    Mirrors BarcodeConflictError. The API layer maps it to 409.
    """

    def __init__(self, code: str | None = None):
        self.code = code
        super().__init__(
            RFID_EPC_CONFLICT_MESSAGE.format(code=code) if code else "RFID / EPC already exists"
        )


def is_barcode_unique_violation(exc: Exception) -> bool:
    """True when an IntegrityError is a stale barcode unique-index violation (not the PK race).

    Current schemas carry no such index; this keeps a database that still has one
    (an unfinished upgrade, a stale test database) answering 409 rather than 500.
    """
    names = (BARCODE_UNIQUE_INDEX, LEGACY_BARCODE_UNIQUE_INDEX)
    orig = getattr(exc, "orig", None)
    if getattr(orig, "constraint_name", None) in names:
        return True
    message = str(exc)
    return any(name in message for name in names)


def is_rfid_epc_unique_violation(exc: Exception) -> bool:
    """True when an IntegrityError is a stale rfid_epc unique-index violation (not the PK race)."""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "constraint_name", None) == RFID_EPC_UNIQUE_INDEX:
        return True
    return RFID_EPC_UNIQUE_INDEX in str(exc)

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Tax codes: one row of a company's tax list (and of a country pack at schema 2), its dated
rate versions, the tax a document line keeps once the code is applied, and a party's dated
tax numbers.

``rate_on`` is the one reader of a code's rate: the version in force on a day, or
``NoRateInForce`` when none is, never a guessed rate. A line's ``LineTaxSnapshot`` is what
posting reads, so editing a code later changes no line that already carries it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from celerp.schemas.numbers import FiniteFloat
from celerp.tax_regimes import TAX_TYPES, tax_name_key

TaxCategory = Literal["standard", "reduced", "zero", "exempt", "out_of_scope", "reverse_charge",
                      "self_assessed_import", "withholding", "perception"]
WithholdingPoint = Literal["document", "payment"]
ReportTag = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")]


class NoRateInForce(ValueError):
    """No version of a code covers the day asked for."""

    def __init__(self, code: "TaxCode", day: dt.date):
        self.code, self.day = code, day
        super().__init__(f"{code.name} has no rate in force on {day.isoformat()}")


class TaxVersion(BaseModel):
    """A rate in force from ``valid_from`` to ``valid_to``, both days included; None is an open
    start or end. ``base_fraction`` is the part of the amount the rate applies to (None: all)."""
    model_config = ConfigDict(extra="forbid")

    rate: FiniteFloat
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    base_fraction: FiniteFloat | None = None


class TaxCode(BaseModel):
    """One tax code. ``id`` is minted by the tax-list writer and never changes; ``category``
    None means the category is not set yet; ``output_account`` and ``input_account`` None post
    to the role default."""
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str = Field(min_length=1, max_length=64)
    tax_type: Literal[TAX_TYPES] = "both"
    category: TaxCategory | None = None
    component: str | None = Field(None, max_length=32)
    output_account: str | None = None
    input_account: str | None = None
    report_tags: list[ReportTag] = Field([], max_length=20)
    cash_basis: StrictBool = False
    withholding_point: WithholdingPoint | None = None
    versions: list[TaxVersion] = Field(min_length=1)
    is_default: StrictBool = False
    is_compound: StrictBool = False
    default_order: int = Field(0, ge=0)
    description: str = Field("", max_length=500)

    @field_validator("tax_type", mode="before")
    @classmethod
    def _unset_side_is_both(cls, value):
        # Every reader of a stored list takes a missing or blank side as both.
        return "both" if value is None or value == "" else value

    @model_validator(mode="after")
    def _consistent(self) -> "TaxCode":
        name = self.name.strip()
        if self.category == "withholding" and self.withholding_point is None:
            raise ValueError(f"{name}: a withholding code needs withholding_point (document or payment)")
        for index, version in enumerate(self.versions):
            if version.rate < 0:
                raise ValueError(f"{name}: a rate cannot be negative ({version.rate:g}); "
                                 "record tax withheld with a withholding code instead")
            if version.base_fraction is not None and not 0 < version.base_fraction <= 1:
                raise ValueError(f"{name}: base_fraction {version.base_fraction:g} must be above 0 and at most 1")
            if version.valid_from and version.valid_to and version.valid_to < version.valid_from:
                raise ValueError(f"{name}: a version cannot end on {version.valid_to} before it starts on "
                                 f"{version.valid_from}")
            if index and version.valid_from is None:
                raise ValueError(f"{name}: only the earliest version may leave valid_from empty")
        for earlier, later in zip(self.versions, self.versions[1:]):
            if earlier.valid_to is None or earlier.valid_to >= later.valid_from:
                ends = earlier.valid_to or "an open end"
                raise ValueError(f"{name}: versions overlap: the version ending {ends} overlaps the version starting "
                                 f"{later.valid_from}")
        return self


def rate_on(code: TaxCode, day: dt.date) -> float:
    """The rate of ``code`` in force on ``day``; NoRateInForce before the first version, after
    the last or in a gap between two."""
    for version in code.versions:
        if (version.valid_from is None or version.valid_from <= day) and (
                version.valid_to is None or day <= version.valid_to):
            return version.rate
    raise NoRateInForce(code, day)


class TaxListRefused(ValueError):
    """A tax list a save would write breaks a rule of the list as a whole."""


def normalized_list(rows: list[dict]) -> list[dict]:
    """A tax list as it is stored: every row a valid TaxCode dump, a row given one bare ``rate``
    (a CSV import, a peer document, a migration) as one open version, a row with no id given a
    new one. TaxListRefused when two rows share an id or a name (as tax_name_key compares
    them); a row that is not a valid code raises its validation error (a ValueError)."""
    stored, ids, names = [], set(), set()
    for raw in rows:
        row = dict(raw)
        if "versions" not in row and "rate" in row:
            row["versions"] = [{"rate": row.pop("rate")}]
        code = TaxCode.model_validate(row)
        code.id = code.id or f"u-{uuid.uuid4().hex[:12]}"
        if code.id in ids:
            raise TaxListRefused(f"{code.name}: id {code.id} belongs to another tax of this list")
        if tax_name_key(code.name) in names:
            raise TaxListRefused(f"{code.name}: name is used by another tax of this list")
        ids.add(code.id)
        names.add(tax_name_key(code.name))
        stored.append(code.model_dump(mode="json"))
    return stored


def list_fingerprint(rows: list[dict]) -> str:
    """Content fingerprint of a stored tax list exactly as the settings API returns it. A
    whole-list save carries the fingerprint of the list it was edited from, so a save based on
    a list that has changed since is refused instead of overwriting the change."""
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LineTaxSnapshot(BaseModel):
    """A tax as a document line (or a document's own charges) carries it. ``code`` is the tax
    name; ``amount`` is signed and recomputed by the server when 0; ``is_compound`` applies the
    rate to the base plus the taxes before it in ``order``. The other fields are copied from the
    code and its version in force when the tax is added: posting reads them, never the live code.
    A snapshot with no ``code_id`` or ``category`` is unclassified; nothing is inferred for it."""
    code: str
    rate: FiniteFloat
    amount: FiniteFloat = 0.0
    order: int = 0
    is_compound: bool = False
    label: str = ""
    code_id: str | None = None
    base_fraction: FiniteFloat | None = None
    category: TaxCategory | None = None
    component: str | None = None
    report_tags: list[str] | None = None
    version_from: dt.date | None = None
    base: FiniteFloat | None = None
    inclusive: bool | None = None
    withholding_point: WithholdingPoint | None = None
    cash_basis: bool | None = None
    account: str | None = None


class TaxIdentity(BaseModel):
    """A tax number a company or contact holds under one scheme from ``valid_from`` (None:
    always)."""
    scheme: str
    number: str
    label: str = ""
    valid_from: dt.date | None = None


def identity_on(rows: list[TaxIdentity], scheme: str, day: dt.date) -> TaxIdentity | None:
    """The number held under ``scheme`` on ``day``: the latest row of that scheme in force by
    then, or None when there is none."""
    held = [row for row in rows if row.scheme == scheme and (row.valid_from is None or row.valid_from <= day)]
    return max(held, key=lambda row: row.valid_from or dt.date.min, default=None)

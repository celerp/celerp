# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What a user can enter on a line: the bodies the line save steps accept. The line editor saves a
document's or a List's lines as ``LineItem`` and a write-off line's entry as ``WriteoffLineBody``.
A record made from another one (record_copy) keeps exactly these keys of each line."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from celerp.schemas.numbers import FiniteFloat
from celerp_docs.taxes import TaxApplication


class LineItem(BaseModel):
    # The line's own stable identity, assigned where lines are written when omitted.
    line_id: str | None = None
    item_id: str | None = None
    entity_id: str | None = None  # alias sent by the frontend; resolved to item_id below
    sku: str | None = None
    # Stamped from the catalog item when the line is added; the identifier a
    # finalized document keeps showing even if the item is later re-barcoded.
    barcode: str | None = None
    name: str | None = None
    description: str | None = None
    quantity: FiniteFloat = 0
    unit: str | None = None
    unit_price: FiniteFloat = 0
    # The line's own discount, taken off its price (document_line_amount) when no line_total is given.
    discount_pct: FiniteFloat | None = None
    tax_rate: FiniteFloat | None = None  # deprecated: kept for backward compat; prefer taxes list
    # None: the line's tax is still for the user to set (set_new_line_taxes); it charges nothing.
    taxes: list[TaxApplication] | None = Field(default_factory=list)
    sell_by: str | None = None
    line_total: FiniteFloat | None = None
    # Purchasing: whether the line's goods are received into stock, as an expense or as an asset.
    receive_as: Literal["stock", "expense", "asset"] | None = None
    # Invoice fulfillment: how much of the linked parcel this line draws, by piece
    # and by weight. Drive the split-on-fulfill (child_pieces / child_weight). Only
    # editable when the parcel actually tracks that measure and splitting is allowed.
    pieces: FiniteFloat | None = None
    weight: FiniteFloat | None = None
    # Purchasing: what the received lot records about the goods.
    attributes: dict | None = None
    # Purchasing: the chart account the line posts to; checked when the bill is finalized.
    account_code: str | None = None
    # Selling a fixed asset: the asset the line sells (an invoice) or credits (a credit note).
    asset_id: str | None = None
    # A credit note line that takes the sold asset back onto the books.
    asset_returned: bool | None = None
    # A credit note line: the invoice line it credits.
    source_line_id: str | None = None
    # The period the line's service covers (ISO dates); a deferred line recognizes over it
    # (celerp.services.recognition). An agreement's bill writes them on each line.
    service_start: str | None = None
    service_end: str | None = None
    # A line that came from outside: where from, and the fund chosen for it (peer_lines).
    origin: str | None = None
    fund_choice: str | None = None
    fund_id: str | None = None
    # A migrated line: the tracking category -> option its source books gave it.
    tracking: dict[str, str] | None = None
    # Entered in the line editor's own columns: customs details, the purchase category and whether
    # the line's lot may be split when it is fulfilled.
    hs_code: str | None = None
    country_of_origin: str | None = None
    category: str | None = None
    allow_splitting: bool | None = None

    @model_validator(mode="after")
    def _resolve_entity_id(self) -> "LineItem":
        """Frontend sends entity_id; normalise to item_id so the stored state is consistent.
        A line naming two different items under the two keys is refused, as every line
        writer refuses it (document_lines.linked_items)."""
        if self.entity_id and self.item_id and self.entity_id != self.item_id:
            raise ValueError(f"This line names two different items ({self.item_id} and {self.entity_id}). "
                             "Keep one item per line.")
        if self.entity_id and not self.item_id:
            self.item_id = self.entity_id
        self.entity_id = None  # never persist entity_id; always use item_id
        return self


class WriteoffLineBody(BaseModel):
    line_id: str | None = None
    item_id: str | None = None
    qty_out: FiniteFloat | None = None
    weight: FiniteFloat | None = None
    pieces: FiniteFloat | None = None
    account: str | None = None
    comment: str | None = None

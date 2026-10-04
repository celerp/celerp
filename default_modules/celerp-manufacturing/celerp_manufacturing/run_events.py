# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The run events that undo a step, registered by this module rather than the core."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from celerp.events.schemas import register_event_type


class MfgOrderReturned(BaseModel):
    # Components returned from a run to the lots they came from, each with the value it took back.
    items: list[dict[str, Any]] = Field(default_factory=list)
    returned_by: str | None = None
    value: str


register_event_type("mfg.order.returned", MfgOrderReturned)


class MfgOrderReceiptUndone(BaseModel):
    lot_item_id: str
    quantity: float
    value: str
    undone_by: str | None = None


class MfgOrderReopened(BaseModel):
    reopened_by: str | None = None


register_event_type("mfg.order.receipt_undone", MfgOrderReceiptUndone)
register_event_type("mfg.order.reopened", MfgOrderReopened)


class MfgOrderWipReconciled(BaseModel):
    # What a run needing reconciliation holds, as the user stated it: each component's value, the
    # total moved onto work in progress, and the account it came off.
    issued: str
    components: list[dict[str, Any]] = Field(default_factory=list)
    account: str | None = None
    wip_account_code: str | None = None
    reconciled_by: str | None = None


register_event_type("mfg.order.wip_reconciled", MfgOrderWipReconciled)


class MfgOrderOutputRepaired(BaseModel):
    # What an older release recorded about a run's output, put right: the quantity it marked
    # received without making a lot, and the product the run makes when the user chose one.
    discarded: float = 0
    output_item_id: str | None = None
    expected_outputs: list[dict[str, Any]] | None = None
    repaired_by: str | None = None


register_event_type("mfg.order.output_repaired", MfgOrderOutputRepaired)

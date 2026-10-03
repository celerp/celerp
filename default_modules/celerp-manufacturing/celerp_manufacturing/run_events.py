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

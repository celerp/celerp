# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed asset event types and the fields each one must carry."""
from __future__ import annotations

from pydantic import BaseModel

from celerp.events.schemas import register_event_type


class _Event(BaseModel):
    model_config = {"extra": "allow"}


class ClassCreated(_Event):
    name: str
    kind: str
    asset_account: str
    accumulated_depreciation_account: str
    depreciation_account: str
    method: str


class Changes(_Event):
    changes: dict


class AssetCreated(_Event):
    number: str
    name: str
    status: str
    op_key: str


class AssetStatus(_Event):
    status: str


class AssetFinancials(_Event):
    class_id: str
    status: str
    available_date: str


class AssetMoved(_Event):
    movement_id: str
    amounts: dict


class MovementRecorded(_Event):
    asset_id: str
    kind: str
    operation: str
    effective_date: str
    posting_date: str
    amounts: dict
    contributions: list
    op_key: str


class SourceClaimed(_Event):
    source: str
    allocation: str
    generation: int
    op_key: str


class SourceReleased(_Event):
    generation: int
    op_key: str


class MaintenanceRecorded(_Event):
    asset_id: str
    source: str
    amount: str


class OperationRecorded(_Event):
    type: str
    intent_hash: str
    result: dict


class OperationUndone(_Event):
    undo_key: str


class RunRecorded(_Event):
    month: str
    status: str
    total: str


class RunReversed(_Event):
    op_key: str


class ReadingRecorded(_Event):
    period: str
    units: str


class SkipRecorded(_Event):
    month: str
    reason: str


class SkipWithdrawn(_Event):
    month: str


class AssetException(_Event):
    exception: dict | None


class CorrectionRequested(_Event):
    asset_id: str
    changes: dict
    classification: str
    requested_by: str


class CorrectionApplied(_Event):
    asset_id: str
    classification: str
    op_key: str


class CorrectionClosed(_Event):
    op_key: str | None = None


EVENTS: dict[str, type[BaseModel]] = {
    "fa.class.created": ClassCreated,
    "fa.class.updated": Changes,
    "fa.asset.created": AssetCreated,
    "fa.asset.updated": Changes,
    "fa.asset.status": AssetStatus,
    "fa.asset.financials": AssetFinancials,
    "fa.asset.moved": AssetMoved,
    "fa.movement.recorded": MovementRecorded,
    "fa.src.claimed": SourceClaimed,
    "fa.src.released": SourceReleased,
    "fa.maint.recorded": MaintenanceRecorded,
    "fa.maint.status": AssetStatus,
    "fa.op.recorded": OperationRecorded,
    "fa.op.undone": OperationUndone,
    "fa.run.prepared": RunRecorded,
    "fa.run.posted": RunRecorded,
    "fa.run.reversed": RunReversed,
    "fa.reading.recorded": ReadingRecorded,
    "fa.skip.recorded": SkipRecorded,
    "fa.skip.withdrawn": SkipWithdrawn,
    "fa.asset.exception": AssetException,
    "fa.correction.requested": CorrectionRequested,
    "fa.correction.applied": CorrectionApplied,
    "fa.correction.withdrawn": CorrectionClosed,
    "fa.correction.undone": CorrectionClosed,
}


def register_events() -> None:
    for name, schema in EVENTS.items():
        register_event_type(name, schema)

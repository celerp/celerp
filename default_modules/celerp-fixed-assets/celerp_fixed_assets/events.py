# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The fixed asset event types and the fields each one must carry."""
from __future__ import annotations

from pydantic import BaseModel

from celerp.events.schemas import (
    EntityFileAttached, EntityFileDeleted, EntityFileDescriptionUpdated, EntityFileTagged, register_event_type)


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


class ReviewRecorded(_Event):
    asset_id: str
    date: str
    outcome: str
    basis: str


class ReconRecorded(_Event):
    key: str
    state: str
    amount: str


class TaxBookRecorded(_Event):
    asset_id: str
    regime: str
    elections: dict


class TaxYearRecorded(_Event):
    regime: str
    year: int
    status: str
    rows: list
    history: list


class YearEndRecorded(_Event):
    year: int
    marks: dict


class Confirmed(_Event):
    confirmed: dict


class ProblemReported(_Event):
    problem: dict


class CheckRecorded(_Event):
    asset_id: str
    user_id: str
    date: str


class CheckRemoved(_Event):
    user_id: str


class ImportRecorded(_Event):
    as_at: str
    state: str
    counts: dict
    totals: dict
    op_keys: list


class ImportRow(_Event):
    batch_id: str
    row: int
    status: str
    values: dict
    errors: list


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
    "fa.review.recorded": ReviewRecorded,
    "fa.review.status": AssetStatus,
    "fa.recon.recorded": ReconRecorded,
    "fa.taxbook.recorded": TaxBookRecorded,
    "fa.taxyear.recorded": TaxYearRecorded,
    "fa.yearend.recorded": YearEndRecorded,
    "fa.asset.file_attached": EntityFileAttached,
    "fa.asset.file_tagged": EntityFileTagged,
    "fa.asset.file_described": EntityFileDescriptionUpdated,
    "fa.asset.file_deleted": EntityFileDeleted,
    "fa.asset.confirmed": Confirmed,
    "fa.asset.problem": ProblemReported,
    "fa.check.recorded": CheckRecorded,
    "fa.check.removed": CheckRemoved,
    "fa.import.recorded": ImportRecorded,
    "fa.import.row": ImportRow,
}


def register_events() -> None:
    for name, schema in EVENTS.items():
        register_event_type(name, schema)

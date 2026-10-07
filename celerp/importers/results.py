# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Per-record import outcomes, shared by every domain import service.

A service reports one `RecordOutcome` per input record. HTTP batch routes fold
the outcomes into their counts and error list; migration sinks turn the same
outcomes into entity mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

OutcomeStatus = Literal["created", "updated", "skipped", "rejected", "failed"]

# The number of error messages a batch route returns, as every batch route always has.
ROUTE_ERROR_LIMIT = 10


@dataclass(frozen=True)
class RecordOutcome:
    """What an import service did with one record.

    rejected: refused by validation and counted as skipped with its reason;
    failed: the write raised, reported but not counted. `message` is the reason, as
    text or as a refusal the UI shows in the reader's language. `entity_type` names
    the Celerp entity written when it differs from the batch's own.
    """
    entity_id: str
    status: OutcomeStatus
    message: str | dict | None = None
    entity_type: str | None = None


@dataclass
class ImportOutcome:
    """Per-record outcomes of one import service call, in input order."""
    records: list[RecordOutcome] = field(default_factory=list)

    def add(self, entity_id: str, status: OutcomeStatus, message: str | dict | None = None) -> None:
        self.records.append(RecordOutcome(entity_id, status, message))

    def count(self, *statuses: OutcomeStatus) -> int:
        return sum(1 for r in self.records if r.status in statuses)

    def route_counts(self, *, cap_rejections: bool = True) -> dict:
        """The batch route response: created, skipped (including rejected), updated, errors.

        Failure messages stop at ROUTE_ERROR_LIMIT. Rejection messages stop there
        too unless the route has always listed every rejection (`cap_rejections=False`).
        """
        errors: list[str | dict] = []
        for r in self.records:
            if r.status not in ("rejected", "failed") or r.message is None:
                continue
            if len(errors) < ROUTE_ERROR_LIMIT or (r.status == "rejected" and not cap_rejections):
                errors.append(r.message)
        return {
            "created": self.count("created"),
            "skipped": self.count("skipped", "rejected"),
            "updated": self.count("updated"),
            "errors": errors,
        }


def failure_reason(exc: BaseException) -> str:
    """The reason a refused row gives the reader: a refusal's own message (an
    HTTPException's detail, or the message of a structured detail), else the error text."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    if detail is not None:
        return str(detail)
    return str(exc)


def message_text(message: str | dict) -> str:
    """An outcome message as English text: a refusal's own message, else the text."""
    return str(message["message"]) if isinstance(message, dict) else str(message)

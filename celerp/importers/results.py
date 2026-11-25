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

from pydantic import BaseModel

OutcomeStatus = Literal["created", "updated", "skipped", "rejected", "failed"]

# The number of error messages a batch route returns, as every batch route always has.
ROUTE_ERROR_LIMIT = 10


class BatchImportResult(BaseModel):
    """What a batch import route answers (ImportOutcome.route_counts): a refused row's
    reason is text, or its keyed refusal (row_refusal)."""
    created: int
    skipped: int
    updated: int = 0
    errors: list[str | dict]


@dataclass(frozen=True)
class RecordOutcome:
    """What an import service did with one record.

    rejected: refused by validation and counted as skipped with its reason;
    failed: the write raised, reported but not counted. `message` is the reason, as
    text or as a refusal the UI shows in the reader's language. `entity_type` names
    the Celerp entity written when it differs from the batch's own; `fix` is what a
    failed write's refusal says to change (a fund choice), for a caller that offers it.
    """
    entity_id: str
    status: OutcomeStatus
    message: str | dict | None = None
    entity_type: str | None = None
    fix: dict | None = None


@dataclass
class ImportOutcome:
    """Per-record outcomes of one import service call, in input order, and what else
    the call changed that removing the records it created would not take back."""
    records: list[RecordOutcome] = field(default_factory=list)
    lasting_effects: set[str] = field(default_factory=set)

    def add(self, entity_id: str, status: OutcomeStatus, message: str | dict | None = None,
            fix: dict | None = None) -> None:
        self.records.append(RecordOutcome(entity_id, status, message, fix=fix))

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


def row_refusal(entity_id: str | dict, why: BaseException | dict) -> str | dict:
    """A refused record's reason naming it (``entity_id``, its id or a refusal such as
    ``import.row_n``): a keyed refusal, given or raised by the write, stays keyed inside
    ``import.record_refused``, so it reads in each reader's language; any other reason as text."""
    from celerp.accounting_roles import catalog_refusal
    from celerp.error_body import as_refusal, keyed

    detail = why if isinstance(why, dict) else as_refusal(getattr(why, "detail", None))
    if isinstance(detail, dict) and keyed(detail):
        return catalog_refusal("import.record_refused", entity_id=entity_id, refusal=detail)
    return f"{message_text(entity_id)}: {failure_reason(why)}"


def keyed_reason(why: BaseException | str | dict) -> dict:
    """A reason as a keyed refusal: the keyed refusal given or raised, else its text as written."""
    from celerp.accounting_roles import catalog_refusal
    from celerp.error_body import as_refusal, keyed

    detail = as_refusal(getattr(why, "detail", None)) if isinstance(why, BaseException) else why
    if isinstance(detail, dict) and keyed(detail):
        return detail
    text = failure_reason(why) if isinstance(why, BaseException) else message_text(why)
    return catalog_refusal("error.refusal_as_written", text=text)


def record_refused(entity_id: str | dict, key: str, /, **params) -> dict:
    """A record refused for the reason ``import.record.<key>`` names, naming the record."""
    from celerp.accounting_roles import catalog_refusal

    return row_refusal(entity_id, catalog_refusal(f"import.record.{key}", **params))


def row_n(index: int, code: str | None = None) -> dict:
    """The name of a file's row that has no id of its own, counted from 1, with the code
    it gives when it gives one."""
    from celerp.accounting_roles import catalog_refusal

    if code:
        return catalog_refusal("import.row_n_code", n=index + 1, code=code)
    return catalog_refusal("import.row_n", n=index + 1)


def kept_refusal(message: str | dict, entity_id: str) -> dict:
    """The reason a refused record kept (row_refusal), without the record's name: its keyed
    refusal, rebuilt from its catalog key, else the text as written."""
    from celerp.accounting_roles import catalog_refusal

    inner = (message.get("params") or {}).get("refusal") if isinstance(message, dict) else None
    if isinstance(inner, dict) and inner.get("message_key"):
        return {**inner, **catalog_refusal(inner["message_key"], **(inner.get("params") or {}))}
    return catalog_refusal("error.refusal_as_written", text=message_text(message).removeprefix(f"{entity_id}: "))


def row_flag(raw: object) -> bool:
    """A spreadsheet cell as a boolean: a real bool as given, else a string read for
    one of the ways a sheet writes "yes"."""
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in {"true", "1", "yes"}


def message_text(message: str | dict) -> str:
    """An outcome message as English text: a refusal's own message, else the text."""
    return str(message["message"]) if isinstance(message, dict) else str(message)

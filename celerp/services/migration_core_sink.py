# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Migration sink for the CIF groups the kernel owns, plus the helpers every sink shares.

Domain import services report one `RecordOutcome` per input record. Their HTTP
batch routes fold the outcomes into the route's counts and error list; their
migration sinks turn the same outcomes into entity mappings. Sinks never commit:
the runner owns the transaction of every batch.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select

from celerp.importers.schema import (
    CIFAttachment,
    CIFCompanyProfile,
    CIFCurrency,
    CIFExchangeRate,
    CIFLocation,
    CIFSourceRecord,
    CIFTaxCode,
    ReconciliationExpectations,
)
from celerp.importers.sinks import (
    DestinationMeasurement,
    SinkBatchResult,
    SinkContext,
    SinkEntityMapping,
    SinkError,
)
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.models.migration import MigrationEntityMap
from celerp.services.currencies import CURRENCY_CODES
from celerp.services.money import currency_dp

OutcomeStatus = Literal["created", "updated", "skipped", "rejected", "failed"]

# The number of error messages a batch route returns, as every batch route always has.
ROUTE_ERROR_LIMIT = 10


@dataclass(frozen=True)
class RecordOutcome:
    """What an import service did with one record.

    rejected: refused by validation and counted as skipped with its reason;
    failed: the write raised, reported but not counted. `entity_type` names the
    Celerp entity written when it differs from the batch's own.
    """
    entity_id: str
    status: OutcomeStatus
    message: str | None = None
    entity_type: str | None = None


@dataclass
class ImportOutcome:
    """Per-record outcomes of one import service call, in input order."""
    records: list[RecordOutcome] = field(default_factory=list)

    def add(self, entity_id: str, status: OutcomeStatus, message: str | None = None) -> None:
        self.records.append(RecordOutcome(entity_id, status, message))

    def count(self, *statuses: OutcomeStatus) -> int:
        return sum(1 for r in self.records if r.status in statuses)

    def route_counts(self, *, cap_rejections: bool = True) -> dict:
        """The batch route response: created, skipped (including rejected), updated, errors.

        Failure messages stop at ROUTE_ERROR_LIMIT. Rejection messages stop there
        too unless the route has always listed every rejection (`cap_rejections=False`).
        """
        errors: list[str] = []
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


def deterministic_id(context: SinkContext, kind: str, external_id: str) -> uuid.UUID:
    """The same Celerp id for the same source record on every attempt of one run."""
    return uuid.uuid5(context.run_id, f"{kind}:{external_id}")


@dataclass(frozen=True)
class ActingMember:
    """The user a migration writes as, with their role and the company settings it is checked against."""
    user: User
    role: str
    settings: dict


async def acting_member(context: SinkContext) -> ActingMember:
    """The migrating user as a member of the target company; a non-member cannot write."""
    session = context.session
    role = (await session.execute(
        select(UserCompany.role).where(
            UserCompany.user_id == context.user_id, UserCompany.company_id == context.company_id,
        )
    )).scalar_one_or_none()
    user = await session.get(User, context.user_id)
    company = await session.get(Company, context.company_id)
    if role is None or user is None or company is None:
        raise PermissionError("The migrating user is not a member of the company.")
    return ActingMember(user, role, dict(company.settings or {}))


async def mapped_targets(
    context: SinkContext, target_entity_type: str, external_ids: Iterable[str | None]
) -> dict[str, str]:
    """Source external id -> Celerp id for records this run already imported."""
    wanted = {e for e in external_ids if e}
    if not wanted:
        return {}
    rows = (await context.session.execute(
        select(MigrationEntityMap.source_external_id, MigrationEntityMap.target_entity_id).where(
            MigrationEntityMap.migration_run_id == context.run_id,
            MigrationEntityMap.target_entity_type == target_entity_type,
            MigrationEntityMap.source_external_id.in_(wanted),
        )
    )).all()
    return {ext: target for ext, target in rows}


async def run_targets(context: SinkContext, target_entity_type: str) -> list[str]:
    """Every Celerp id of one entity type this run imported."""
    return list((await context.session.execute(
        select(MigrationEntityMap.target_entity_id).where(
            MigrationEntityMap.migration_run_id == context.run_id,
            MigrationEntityMap.target_entity_type == target_entity_type,
        )
    )).scalars())


def sink_result(
    records: Sequence[CIFSourceRecord], outcomes: Sequence[RecordOutcome], target_entity_type: str
) -> SinkBatchResult:
    """Turn one outcome per record into counts, mappings and errors."""
    if len(records) != len(outcomes):
        raise ValueError("An import service must report exactly one outcome per record.")
    result = SinkBatchResult()
    for record, outcome in zip(records, outcomes):
        if outcome.status in ("created", "skipped", "updated"):
            status = "created" if outcome.status == "created" else "skipped"
            if status == "created":
                result.created += 1
            else:
                result.skipped += 1
            result.mappings.append(SinkEntityMapping(
                record.source_type, record.source_external_id, outcome.entity_type or target_entity_type,
                outcome.entity_id, status,
            ))
        else:
            result.errors.append(sink_error(record, outcome.message or "The record could not be imported."))
    return result


async def import_prepared(
    prepared: Sequence[object | str],
    write: Callable[[list], Awaitable[ImportOutcome]],
) -> list[RecordOutcome]:
    """Outcomes for records a sink has turned into service inputs.

    `prepared[i]` is the import service's input for the i-th record, or the
    reason that record cannot be imported. The service writes every ready input
    in one call; the reasons become rejections, keeping one outcome per record
    in input order.
    """
    ready = [p for p in prepared if not isinstance(p, str)]
    written = iter((await write(ready)).records if ready else [])
    return [RecordOutcome("", "rejected", p) if isinstance(p, str) else next(written) for p in prepared]


def sink_error(record: CIFSourceRecord, message: str) -> SinkError:
    return SinkError(record.source_type, record.source_external_id, message)


# ── Kernel-owned groups ───────────────────────────────────────────────────────

_GROUP_TARGET = {
    "company": "company",
    "currencies": "currency",
    "exchange_rates": "exchange_rate",
    "tax_codes": "tax",
    "locations": "location",
    "attachments": "attachment",
}


class CoreMigrationSink:
    """Company settings, currencies, exchange rates, tax codes, locations and attachments."""

    key = "celerp"
    groups = frozenset(_GROUP_TARGET)
    batch_size = 500

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult:
        result = SinkBatchResult()
        company = await context.session.get(Company, context.company_id)
        for record in records:
            target_type, target_id, error = await self._import_one(context, company, record)
            if error is not None:
                result.errors.append(sink_error(record, error))
                continue
            already = await mapped_targets(context, target_type, [record.source_external_id])
            status = "skipped" if already or target_type in ("currency", "exchange_rate") else "created"
            if status == "created":
                result.created += 1
            else:
                result.skipped += 1
            result.mappings.append(SinkEntityMapping(
                record.source_type, record.source_external_id, target_type, target_id, status,
            ))
        return result

    async def _import_one(
        self, context: SinkContext, company: Company, record: CIFSourceRecord
    ) -> tuple[str, str, str | None]:
        if isinstance(record, CIFCompanyProfile):
            return "company", str(company.id), _apply_company_profile(company, record)
        if isinstance(record, CIFCurrency):
            return "currency", record.code, _check_currency(record.code)
        if isinstance(record, CIFExchangeRate):
            error = _check_currency(record.from_currency) or _check_currency(record.to_currency)
            return "exchange_rate", f"{record.from_currency}:{record.to_currency}:{record.effective_date}", error
        if isinstance(record, CIFTaxCode):
            return "tax", record.name, _apply_tax_code(company, record)
        if isinstance(record, CIFLocation):
            location_id, error = await _import_location(context, record)
            return "location", str(location_id), error
        if isinstance(record, CIFAttachment):
            return "attachment", record.sha256, (
                "Attachment files are not available to import; the file was not attached."
            )
        return "", "", f"Record type {type(record).__name__} does not belong to the company settings."

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]:
        # Every reconciliation measure belongs to a domain sink.
        return []


def _check_currency(code: str) -> str | None:
    if code.upper() not in CURRENCY_CODES:
        return f"Currency {code} is not supported."
    return None


def _apply_company_profile(company: Company, profile: CIFCompanyProfile) -> str | None:
    currency = profile.base_currency.upper()
    error = _check_currency(currency)
    if error:
        return error
    if profile.money_precision is not None and profile.money_precision != currency_dp(currency):
        return (
            f"The source keeps {currency} amounts to {profile.money_precision} decimal places; "
            f"Celerp keeps {currency} to {currency_dp(currency)}."
        )
    settings = dict(company.settings or {})
    settings["currency"] = currency
    if profile.fiscal_year_start_month is not None:
        settings["fiscal_year_start"] = f"{profile.fiscal_year_start_month:02d}-01"
    company.settings = settings
    return None


def _apply_tax_code(company: Company, tax: CIFTaxCode) -> str | None:
    if tax.inclusive:
        return f"Tax code {tax.name} is tax-inclusive, which Celerp tax rates do not support."
    rate = float(tax.rate_percent)
    settings = dict(company.settings or {})
    taxes = list(settings.get("taxes") or [])
    for existing in taxes:
        if str(existing.get("name", "")).strip().lower() == tax.name.strip().lower():
            if float(existing.get("rate") or 0) != rate:
                return f"A tax named {tax.name} already exists with a different rate."
            return None
    taxes.append({
        "name": tax.name, "rate": rate, "tax_type": "both", "is_default": False,
        "description": "", "is_compound": False, "default_order": 0,
    })
    settings["taxes"] = taxes
    company.settings = settings
    return None


async def _import_location(context: SinkContext, location: CIFLocation) -> tuple[uuid.UUID, str | None]:
    location_id = deterministic_id(context, "location", location.source_external_id)
    if await context.session.get(Location, location_id) is not None:
        return location_id, None
    rows = (await context.session.execute(
        select(Location).where(Location.company_id == context.company_id)
    )).scalars().all()
    if any(r.name == location.name for r in rows):
        return location_id, f"A location named {location.name} already exists."
    context.session.add(Location(
        id=location_id,
        company_id=context.company_id,
        name=location.name,
        type="warehouse",
        address={"text": location.address} if location.address else None,
        is_default=not any(r.is_default for r in rows),
    ))
    await context.session.flush()
    return location_id, None


SINK = CoreMigrationSink()

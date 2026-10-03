# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Migration sink for contacts, written by the same service as the contacts batch route."""

from __future__ import annotations

from collections.abc import Sequence

from celerp.importers.schema import CIFContact, ContactRole, CIFSourceRecord, ReconciliationExpectations
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.services.migration_core_sink import deterministic_id, import_prepared, sink_result
from celerp_contacts import services
from celerp_contacts.services import CONTACT_CREATED, CRMImportRecord

CONTACT = "contact"


class ContactsMigrationSink:
    key = "celerp-contacts"
    groups = frozenset({"contacts"})
    batch_size = 500

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult:
        prepared = [_contact_record(context, r) for r in records]

        async def write(ready: list[CRMImportRecord]):
            return await services.import_contact_records(
                context.session, context.company_id, context.user_id, ready, match_identity=False)

        return sink_result(records, await import_prepared(prepared, write), CONTACT)

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]:
        # Party balances are measured by the accounting sink.
        return []


def _contact_type(roles: list[ContactRole]) -> str:
    customer, supplier = ContactRole.CUSTOMER in roles, ContactRole.SUPPLIER in roles
    if customer and supplier:
        return "both"
    return "vendor" if supplier else "customer"


def _contact_record(context: SinkContext, record: CIFSourceRecord) -> CRMImportRecord | str:
    if not isinstance(record, CIFContact):
        return f"Record type {type(record).__name__} is not a contact."
    if not record.name.strip():
        return f"Contact {record.source_external_id} has no name."
    data = {
        "name": record.name.strip(),
        "email": record.email,
        "phone": record.phone,
        "billing_address": record.address,
        "tax_id": record.tax_id,
        "currency": record.currency.upper() if record.currency else None,
        "contact_type": _contact_type(record.roles),
    }
    return CRMImportRecord(
        entity_id=f"contact:{deterministic_id(context, record.source_type, record.source_external_id)}",
        event_type=CONTACT_CREATED,
        data={k: v for k, v in data.items() if v is not None},
        source="migration",
        idempotency_key=context.idempotency_key(record, "created"),
    )


SINK = ContactsMigrationSink()

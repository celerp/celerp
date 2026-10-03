# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Migration sink for accounts, journals and bank transfers.

Journals and transfers are written by the same journal import service as the
accounting batch route; chart rows and bank accounts by the same helpers as the
account and bank-account routes. Every account keeps its source code, suffixed
where Celerp already holds that code for another account; no Celerp default account
is added. Source control accounts are recorded per posting role so the roles can be
mapped when the migration finishes. Every account figure this sink measures is
debit minus credit.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select

from celerp.importers.results import RecordOutcome
from celerp.importers.schema import (
    AccountControl,
    CIFAccount,
    CIFBankTransfer,
    CIFJournalEntry,
    CIFSourceRecord,
    ReconciliationExpectations,
    ReconciliationMeasure,
)
from celerp.accounting_roles import AccountRole
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.services.account_roles import current_settings, record_source_control, source_controls
from celerp.services.migration_core_sink import (
    deterministic_id,
    import_prepared,
    mapped_targets,
    sink_result,
)
from celerp.services.money import round_money, to_stored_float
from celerp_accounting import import_service
from celerp_accounting.import_service import JOURNAL_CREATED, AccImportRecord
from celerp_accounting.models import Account
from celerp_accounting.routes import (
    _base_currency,
    _build_balances,
    _je_doc_refs,
    _je_rows,
    _line_amounts,
    _line_party,
)

ACCOUNT = "account"
JOURNAL = "journal_entry"
CONTACT = "contact"

# The posting role a source control account served. Tax splits by side: an asset is
# tax paid, a liability tax collected. Bank and cash accounts become bank accounts.
_CONTROL_ROLES = {
    AccountControl.RECEIVABLE: AccountRole.RECEIVABLE,
    AccountControl.PAYABLE: AccountRole.PAYABLE,
    AccountControl.INVENTORY: AccountRole.INVENTORY_PURCHASED,
    AccountControl.RETAINED_EARNINGS: AccountRole.RETAINED_EARNINGS,
}

_ACCOUNT_MEASURES = {
    ReconciliationMeasure.TRIAL_BALANCE,
    ReconciliationMeasure.BANK_CASH,
    ReconciliationMeasure.TAX_CONTROL,
}
_CONTROL_MEASURES = {
    ReconciliationMeasure.AR_CONTROL: AccountRole.RECEIVABLE,
    ReconciliationMeasure.AP_CONTROL: AccountRole.PAYABLE,
}
_PARTY_MEASURES = {
    ReconciliationMeasure.AR_BY_CUSTOMER: AccountRole.RECEIVABLE,
    ReconciliationMeasure.AP_BY_SUPPLIER: AccountRole.PAYABLE,
}


class AccountingMigrationSink:
    key = "celerp-accounting"
    groups = frozenset({"accounts", "journals", "bank_transfers"})
    batch_size = 500

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult:
        accounts = [r for r in records if isinstance(r, CIFAccount)]
        if len(accounts) == len(records):
            return sink_result(records, await _import_accounts(context, accounts), ACCOUNT)
        if any(isinstance(r, CIFAccount) for r in records):
            raise ValueError("A batch holds one CIF group.")
        return sink_result(records, await _import_journals(context, records), JOURNAL)

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]:
        wanted = [e for e in expectations.expectations if e.measure in (
            _ACCOUNT_MEASURES | _CONTROL_MEASURES.keys() | _PARTY_MEASURES.keys()
            | {ReconciliationMeasure.DEBITS_EQUAL_CREDITS}
        )]
        if not wanted:
            return []
        posted = await _je_rows(context.session, context.company_id)
        balances = _build_balances(posted, None, None)
        codes = await mapped_targets(context, ACCOUNT, [e.key for e in wanted])
        settings = await current_settings(context.session, context.company_id)
        contacts = await mapped_targets(context, CONTACT, [e.key for e in wanted if e.measure in _PARTY_MEASURES])
        party_totals: dict[tuple[str, str], Decimal] = {}
        if contacts:
            refs = await _je_doc_refs(context.session, context.company_id, [je_id for je_id, _, _ in posted])
            for je_id, state, _ in posted:
                for entry in state.get("entries", []):
                    amounts = _line_amounts(entry)
                    if amounts is None:
                        continue
                    bucket = (entry.get("account") or "", _line_party(refs, je_id, entry))
                    party_totals[bucket] = party_totals.get(bucket, Decimal(0)) + amounts[0] - amounts[1]

        out: list[DestinationMeasurement] = []
        for e in wanted:
            if e.measure == ReconciliationMeasure.DEBITS_EQUAL_CREDITS:
                actual = sum(balances.values(), Decimal(0))
            elif e.measure in _ACCOUNT_MEASURES:
                if e.key not in codes:
                    continue
                actual = balances.get(codes[e.key], Decimal(0))
            elif e.measure in _CONTROL_MEASURES:
                controls = [codes[e.key]] if e.key in codes else source_controls(settings, _CONTROL_MEASURES[e.measure])
                actual = sum((balances.get(code, Decimal(0)) for code in controls), Decimal(0))
            else:
                if e.key not in contacts:
                    continue
                actual = sum((party_totals.get((code, contacts[e.key]), Decimal(0))
                              for code in source_controls(settings, _PARTY_MEASURES[e.measure])), Decimal(0))
            out.append(DestinationMeasurement(e.measure, e.key, e.currency, actual))
        return out


# ── Accounts ──────────────────────────────────────────────────────────────────

async def _import_accounts(context: SinkContext, accounts: list[CIFAccount]) -> list[RecordOutcome]:
    session = context.session
    resolved = await mapped_targets(
        context, ACCOUNT, [a.source_external_id for a in accounts] + [a.parent_external_id for a in accounts],
    )
    existing = {
        code: acc for code, acc in (await session.execute(
            select(Account.code, Account).where(Account.company_id == context.company_id)
        )).all()
    }
    taken = set(existing)
    outcomes: dict[int, RecordOutcome] = {}
    pending = list(enumerate(accounts))
    while pending:
        waiting = []
        for index, account in pending:
            if account.source_external_id in resolved:
                outcomes[index] = RecordOutcome(resolved[account.source_external_id], "skipped")
                continue
            if account.parent_external_id and account.parent_external_id not in resolved:
                waiting.append((index, account))
                continue
            try:
                code = await _write_account(
                    context, account, resolved.get(account.parent_external_id or ""), existing, taken,
                )
            except Exception as exc:
                outcomes[index] = RecordOutcome("", "failed", f"Account {account.name}: {exc}")
                continue
            resolved[account.source_external_id] = code
            outcomes[index] = RecordOutcome(code, "created")
        if len(waiting) == len(pending):
            for index, account in waiting:
                outcomes[index] = RecordOutcome(
                    "", "rejected",
                    f"Account {account.name}: its parent account {account.parent_external_id} was not imported.",
                )
            break
        pending = waiting
    return [outcomes[i] for i in range(len(accounts))]


async def _write_account(
    context: SinkContext,
    account: CIFAccount,
    parent_code: str | None,
    existing: dict[str, Account],
    taken: set[str],
) -> str:
    session, company_id = context.session, context.company_id
    if account.control in (AccountControl.BANK, AccountControl.CASH):
        code = await import_service.next_bank_account_code(session, company_id, parent_code)
        # The bank keeps its place in the imported chart; no standard header is added.
        await import_service.add_bank_account(
            session, company_id,
            code=code,
            parent_code=parent_code,
            account_name=account.name,
            bank_name=account.name,
            account_number="",
            bank_type="checking",
            currency=(account.currency or await _base_currency(session, company_id)).upper(),
            opening_balance=0.0,
        )
        await session.flush()
        taken.add(code)
        return code

    code = _free_code(account.code or f"M{deterministic_id(context, ACCOUNT, account.source_external_id).hex[:8]}", taken)
    existing[code] = await import_service.create_chart_account(
        session, company_id, code=code, name=account.name, account_type=account.account_type.value,
        parent_code=parent_code, is_active=account.is_active,
    )
    taken.add(code)
    role = _control_role(account)
    if role is not None:
        await record_source_control(session, company_id, role, code)
    return code


def _control_role(account: CIFAccount) -> AccountRole | None:
    if account.control == AccountControl.TAX:
        return AccountRole.TAX_INPUT if account.account_type.value == "asset" else AccountRole.TAX_OUTPUT
    return _CONTROL_ROLES.get(account.control) if account.control else None


def _free_code(code: str, taken: set[str]) -> str:
    """The source code, suffixed when Celerp already uses it for another account."""
    if code not in taken:
        return code
    n = 1
    while f"{code}-{n}" in taken:
        n += 1
    return f"{code}-{n}"


# ── Journals and bank transfers ───────────────────────────────────────────────

async def _import_journals(context: SinkContext, records: Sequence[CIFSourceRecord]) -> list[RecordOutcome]:
    base = (await _base_currency(context.session, context.company_id)).upper()
    account_ids: list[str | None] = []
    contact_ids: list[str | None] = []
    for r in records:
        if isinstance(r, CIFJournalEntry):
            account_ids += [ln.account_external_id for ln in r.lines]
            contact_ids += [ln.contact_external_id for ln in r.lines]
        elif isinstance(r, CIFBankTransfer):
            account_ids += [r.from_account_external_id, r.to_account_external_id]
    codes = await mapped_targets(context, ACCOUNT, account_ids)
    contacts = await mapped_targets(context, CONTACT, contact_ids)
    prepared = [_journal_record(context, r, base, codes, contacts) for r in records]

    async def write(ready: list[AccImportRecord]):
        return await import_service.import_journal_records(
            context.session, context.company_id, context.user_id, ready,
        )

    return await import_prepared(prepared, write)


def _journal_record(
    context: SinkContext,
    record: CIFSourceRecord,
    base: str,
    codes: dict[str, str],
    contacts: dict[str, str],
) -> AccImportRecord | str:
    """The journal import record for one source journal or transfer, or why there is none."""
    if isinstance(record, CIFJournalEntry):
        if record.currency and record.currency.upper() != base:
            return f"Journal {record.source_external_id} is in {record.currency}; only {base} journals can be imported."
        entries = []
        for line in record.lines:
            if line.account_external_id not in codes:
                return f"Journal {record.source_external_id}: account {line.account_external_id} was not imported."
            entry = {
                "account": codes[line.account_external_id],
                "debit": to_stored_float(round_money(line.debit, base)),
                "credit": to_stored_float(round_money(line.credit, base)),
            }
            if line.contact_external_id:
                if line.contact_external_id not in contacts:
                    return f"Journal {record.source_external_id}: contact {line.contact_external_id} was not imported."
                entry["contact"] = contacts[line.contact_external_id]
            entries.append(entry)
        data = {"memo": record.narration or "", "ts": record.entry_date.isoformat(), "entries": entries}
    elif isinstance(record, CIFBankTransfer):
        if record.to_amount is not None and record.to_amount != record.amount:
            return (
                f"Transfer {record.source_external_id} changes currency; "
                "transfers between currencies cannot be imported."
            )
        for ext in (record.from_account_external_id, record.to_account_external_id):
            if ext not in codes:
                return f"Transfer {record.source_external_id}: account {ext} was not imported."
        amount = to_stored_float(round_money(record.amount, base))
        data = {
            "memo": f"Transfer {record.source_ref or record.source_external_id}",
            "ts": record.transfer_date.isoformat(),
            "je_type": "transfer",
            "entries": [
                {"account": codes[record.to_account_external_id], "debit": amount, "credit": 0.0},
                {"account": codes[record.from_account_external_id], "debit": 0.0, "credit": amount},
            ],
        }
    else:
        return f"Record type {type(record).__name__} is not a journal."
    return AccImportRecord(
        entity_id=f"je:migration:{deterministic_id(context, record.source_type, record.source_external_id)}",
        event_type=JOURNAL_CREATED,
        data=data,
        source="migration",
        idempotency_key=context.idempotency_key(record, "created"),
    )


SINK = AccountingMigrationSink()

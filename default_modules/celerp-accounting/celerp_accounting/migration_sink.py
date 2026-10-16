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

from celerp.importers.results import RecordOutcome, failure_reason
from celerp.importers.schema import (
    AccountControl,
    CIFAccount,
    CIFBankTransfer,
    CIFJournalEntry,
    CIFSourceRecord,
    ReconciliationExpectation,
    ReconciliationExpectations,
    ReconciliationMeasure,
)
from celerp.accounting_roles import AccountRole
from celerp.models.projections import Projection
from celerp.services.doc_balance import (
    AWAITING_PAYMENT_STATUSES,
    OPEN_CREDIT_STATUSES,
    doc_value,
    outstanding_balance,
)
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkContext
from celerp.services.account_roles import current_settings, merge_survivors, record_source_control, source_controls
from celerp.services.migration_core_sink import (
    deterministic_id,
    import_prepared,
    mapped_targets,
    run_targets,
    sink_result,
)
from celerp.services.money import checked_exchange_rate, require_doc_rate, round_money, to_stored_float
from celerp_accounting import import_service
from celerp_accounting.import_service import JOURNAL_CREATED, AccImportRecord
from celerp_accounting.models import Account, BankAccount
from celerp_accounting.routes import (
    _base_currency,
    _build_balances,
    _je_doc_refs,
    _je_rows,
    _line_amounts,
    _line_party,
)
from ui.i18n import t

ACCOUNT = "account"
JOURNAL = "journal_entry"
CONTACT = "contact"

_TYPE_ROOTS = {"asset": 1000, "liability": 2000, "equity": 3000, "revenue": 4000, "cogs": 5000, "expense": 6000}

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

_DOCUMENT_RESIDUALS = {
    ReconciliationMeasure.AR_DOCUMENT_RESIDUAL: AccountRole.RECEIVABLE,
    ReconciliationMeasure.AP_DOCUMENT_RESIDUAL: AccountRole.PAYABLE,
}
# Statuses in which a document can still owe: a draft or void document owes nothing,
# whatever its recorded balance.
_OWING_STATUSES = {**AWAITING_PAYMENT_STATUSES, **OPEN_CREDIT_STATUSES}


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
            | _DOCUMENT_RESIDUALS.keys()
            | {ReconciliationMeasure.DEBITS_EQUAL_CREDITS, ReconciliationMeasure.TRIAL_BALANCE_AT}
        )]
        if not wanted:
            return []
        posted = await _je_rows(context.session, context.company_id)
        balances = _build_balances(posted, None, None)
        codes = await mapped_targets(context, ACCOUNT, [_account_key(e) for e in wanted])
        dated: dict[str, dict[str, Decimal]] = {}
        settings = await current_settings(context.session, context.company_id)
        contacts = await mapped_targets(context, CONTACT, [e.key for e in wanted if e.measure in _PARTY_MEASURES])
        party_totals: dict[tuple[str, str], Decimal] = {}
        if contacts:
            refs = await _je_doc_refs(context.session, context.company_id, [je_id for je_id, _, _ in posted])
            survivors = await merge_survivors(context.session, context.company_id)
            for je_id, state, _ in posted:
                for entry in state.get("entries", []):
                    amounts = _line_amounts(entry)
                    if amounts is None:
                        continue
                    bucket = (entry.get("account") or "", _line_party(refs, je_id, entry, survivors))
                    party_totals[bucket] = party_totals.get(bucket, Decimal(0)) + amounts[0] - amounts[1]

        open_ar = Decimal(0)
        open_ap = Decimal(0)
        if any(e.measure in _DOCUMENT_RESIDUALS for e in wanted):
            base = (await _base_currency(context.session, context.company_id)).upper()
            doc_ids = await run_targets(context, "doc")
            found: set[str] = set()
            for start in range(0, len(doc_ids), 500):
                rows = (await context.session.execute(select(Projection).where(
                    Projection.company_id == context.company_id,
                    Projection.entity_id.in_(doc_ids[start:start + 500]),
                    Projection.entity_type == "doc",
                ))).scalars()
                for document in rows:
                    found.add(document.entity_id)
                    state = document.state or {}
                    if doc_value(state, "total") in (None, ""):
                        raise ValueError("An imported document has no measurable total")
                    balance = outstanding_balance(state)
                    if balance is None:
                        raise ValueError("An imported document has no measurable outstanding balance")
                    doc_type = state.get("doc_type")
                    if state.get("status") not in _OWING_STATUSES.get(doc_type, ()):
                        continue
                    balance = round_money(balance * require_doc_rate(state, base), base)
                    if doc_type == "invoice":
                        open_ar += balance
                    elif doc_type == "credit_note":
                        open_ar -= balance
                    elif doc_type == "bill":
                        open_ap += balance
                    elif doc_type == "debit_note":
                        open_ap -= balance
            if len(found) != len(set(doc_ids)):
                raise ValueError("An imported document could not be measured for reconciliation")
        out: list[DestinationMeasurement] = []
        for e in wanted:
            if e.measure == ReconciliationMeasure.DEBITS_EQUAL_CREDITS:
                actual = sum(balances.values(), Decimal(0))
            elif e.measure in _ACCOUNT_MEASURES:
                if e.key not in codes:
                    continue
                actual = balances.get(codes[e.key], Decimal(0))
            elif e.measure == ReconciliationMeasure.TRIAL_BALANCE_AT:
                account, on = e.key.rsplit("@", 1)
                if account not in codes:
                    continue
                if on not in dated:
                    dated[on] = _build_balances(posted, None, on)
                actual = dated[on].get(codes[account], Decimal(0))
            elif e.measure in _DOCUMENT_RESIDUALS:
                role = _DOCUMENT_RESIDUALS[e.measure]
                control = sum((balances.get(code, Decimal(0)) for code in source_controls(settings, role)),
                              Decimal(0))
                actual = (control - open_ar if role == AccountRole.RECEIVABLE else control + open_ap)
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


def _account_key(expectation: ReconciliationExpectation) -> str:
    """The source account an expectation measures; a dated balance keys it as account@date."""
    if expectation.measure == ReconciliationMeasure.TRIAL_BALANCE_AT:
        return expectation.key.rsplit("@", 1)[0]
    return expectation.key


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
                outcomes[index] = RecordOutcome("", "failed", f"Account {account.name}: {failure_reason(exc)}")
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

    code = _free_code(account.code, taken) if account.code else _next_code(account.account_type.value, taken)
    existing[code] = await import_service.create_chart_account(
        session, company_id, code=code, name=account.name, account_type=account.account_type.value,
        parent_code=parent_code, is_active=account.is_active, code_generated=not account.code,
    )
    taken.add(code)
    role = _control_role(account)
    if role is not None:
        await record_source_control(session, company_id, role, code)
    if account.control == AccountControl.EXCHANGE_DIFFERENCE:
        # Celerp books the exchange difference itself when the import applies a foreign
        # payment or note: both sides post to the source's account (resolve_importing).
        for fx_role in (AccountRole.FX_GAIN, AccountRole.FX_LOSS):
            await record_source_control(session, company_id, fx_role, code)
    return code


def _control_role(account: CIFAccount) -> AccountRole | None:
    if account.control == AccountControl.TAX:
        return AccountRole.TAX_INPUT if account.account_type.value == "asset" else AccountRole.TAX_OUTPUT
    return _CONTROL_ROLES.get(account.control) if account.control else None


def _next_code(account_type: str, taken: set[str]) -> str:
    """For an account the source gave no code: the first free number in its type's range of the
    chart, in the chart's own steps of ten where one is free."""
    root = _TYPE_ROOTS[account_type]
    for step in (10, 1):
        for n in range(root + step, root + 1000, step):
            if str(n) not in taken:
                return str(n)
    raise ValueError(t("chart.no_free_account_number", account_type=account_type))


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
    banks = dict((await context.session.execute(
        select(BankAccount.chart_account_code, BankAccount.currency).where(BankAccount.company_id == context.company_id)
    )).all())
    prepared = [_journal_record(context, r, base, codes, contacts, banks) for r in records]

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
    banks: dict[str, str],
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
            if line.fx_currency:
                entry |= {
                    "fx_debit": to_stored_float(line.fx_debit) if line.fx_debit else None,
                    "fx_credit": to_stored_float(line.fx_credit) if line.fx_credit else None,
                    "fx_currency": line.fx_currency.upper(),
                    "fx_rate": to_stored_float(line.fx_rate),
                }
            if line.contact_external_id:
                if line.contact_external_id not in contacts:
                    return f"Journal {record.source_external_id}: contact {line.contact_external_id} was not imported."
                entry["contact"] = contacts[line.contact_external_id]
            entries.append(entry)
        data = {"memo": record.narration or "", "ts": record.entry_date.isoformat(), "entries": entries}
    elif isinstance(record, CIFBankTransfer):
        for ext in (record.from_account_external_id, record.to_account_external_id):
            if ext not in codes:
                return f"Transfer {record.source_external_id}: account {ext} was not imported."
        moved = _transfer_base(record, base, banks.get(codes[record.from_account_external_id], base).upper(),
                               banks.get(codes[record.to_account_external_id], base).upper())
        if isinstance(moved, str):
            return moved
        amount, legs = to_stored_float(moved), []
        for ext, native, side in ((record.to_account_external_id, record.to_amount or record.amount, "debit"),
                                  (record.from_account_external_id, record.amount, "credit")):
            currency = banks.get(codes[ext], base).upper()
            leg = {"account": codes[ext], "debit": amount if side == "debit" else 0.0,
                   "credit": amount if side == "credit" else 0.0}
            if currency != base:
                leg |= {f"fx_{side}": to_stored_float(round_money(native, currency)), "fx_currency": currency,
                        "fx_rate": to_stored_float(checked_exchange_rate(moved / native))}
            legs.append(leg)
        data = {
            "memo": f"Transfer {record.source_ref or record.source_external_id}",
            "ts": record.transfer_date.isoformat(),
            "je_type": "transfer",
            "entries": legs,
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


def _transfer_base(record: CIFBankTransfer, base: str, source: str, dest: str) -> Decimal | str:
    """What a transfer moves in the base currency, or why it cannot be imported. A base
    currency side states it exactly; a transfer within one foreign currency converts at
    its own rate; between two foreign currencies there is no rate to convert at."""
    received = record.to_amount if record.to_amount is not None else record.amount
    label = f"Transfer {record.source_external_id}"
    if source == base and dest == base:
        if received != record.amount:
            return f"{label}: the two sides of a transfer in {base} must be equal."
        return round_money(record.amount, base)
    if dest == base:
        return round_money(received, base)
    if source == base:
        return round_money(record.amount, base)
    if source != dest:
        return f"{label} is between {source} and {dest}; transfers between two foreign currencies cannot be imported."
    if received != record.amount:
        return f"{label}: the two sides of a transfer in {source} must be equal."
    if record.exchange_rate is None:
        return f"{label} is in {source} and carries no exchange rate."
    return round_money(record.amount * record.exchange_rate, base)


SINK = AccountingMigrationSink()

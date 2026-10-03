# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Shared helpers for auto journal entry creation."""


def je_void_data(reason: str, entry_state: dict) -> dict:
    """Payload for acc.journal_entry.voided.

    Always carries the voided entry's own date so the period-lock check
    evaluates the entry's period rather than today; a void without it could
    silently mutate a locked period. That applies to repair flows too: a
    repair that must touch a locked period unlocks it first, on the record.
    """
    ts = entry_state.get("ts") or entry_state.get("created_at")
    return {"reason": reason, "ts": str(ts)[:10] if ts else None}


def je_idempotency_key(doc_id: str, je_type: str, suffix: str) -> str:
    """Canonical doc-scoped idempotency key for auto-JEs.

    Format: "je:{doc_id}:{je_type}:{suffix}".

    Suffix is typically:
      - "c" for acc.journal_entry.created
      - "p" for acc.journal_entry.posted

    Doc-scoped so the same JE can't be emitted twice regardless of trigger source.
    """
    return f"je:{doc_id}:{je_type}:{suffix}"


async def je_minted(session, company_id, doc_id: str, je_type: str) -> bool:
    """Whether an auto-JE was ever created under *je_type* for *doc_id*.

    Payment JE keys embed the payment's index. Deletions used to compact a document's
    payments, renumbering the later ones, so on such a document an index can already
    have keyed another payment's entry; a new entry under that key would silently
    dedupe into it and post nothing.
    """
    from sqlalchemy import select

    from celerp.models.ledger import LedgerEntry
    return (await session.execute(select(LedgerEntry.id).where(
        LedgerEntry.company_id == company_id,
        LedgerEntry.idempotency_key == je_idempotency_key(doc_id, je_type, "c"),
    ).limit(1))).first() is not None


async def unminted_payment_key(session, company_id, doc_id: str, op: str, key: str) -> str:
    """*key*, or its first ``~n`` variant no auto-JE of *op* on *doc_id* was created under.

    For a payment give-back's entry, called once per fresh event: an entry already under
    the key was another payment's, before an older deletion renumbered this one
    (je_minted).
    """
    base, n = key, 0
    while await je_minted(session, company_id, doc_id, f"{op}:{key}"):
        n += 1
        key = f"{base}~{n}"
    return key

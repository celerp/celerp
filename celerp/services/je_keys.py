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

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Shared helpers for auto journal entry creation."""


def je_idempotency_key(doc_id: str, je_type: str, suffix: str) -> str:
    """Canonical doc-scoped idempotency key for auto-JEs.

    Format: "je:{doc_id}:{je_type}:{suffix}".

    Suffix is typically:
      - "c" for acc.journal_entry.created
      - "p" for acc.journal_entry.posted

    Doc-scoped so the same JE can't be emitted twice regardless of trigger source.
    """
    return f"je:{doc_id}:{je_type}:{suffix}"

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The ``tax_compliance`` slot contract. Each entry is ``{"kind", "key", "handler":
"module.path:Name"}``, the shape of ``fixed_asset_rules``; handlers receive copies of the
records they work on and never a database session.

Kinds and what the handler named by each provides:

- ``id_validator``: ``(scheme, number) -> str | None``, the message for a malformed number
- ``legal_fields``: the legal fields a document must carry
- ``return_mapping``: how ledger tax lines map to the boxes of a return
- ``return_exporter``: ``(return) -> (bytes, media_type)``
- ``invoice_serializer``: ``(legal_facts) -> bytes``
- ``authority_transport``: ``send``, ``query`` and ``cancel``, each ``(operation, credential_handle)``
- ``fiscal_fixtures``: records the authority's test environment expects
- ``record_chain``: ``hash_input`` and ``sign`` for chained records
- ``submission_kinds``: the operations the authority accepts
"""
from __future__ import annotations

from typing import Literal

TaxComplianceKind = Literal["id_validator", "legal_fields", "return_mapping", "return_exporter",
                            "invoice_serializer", "authority_transport", "fiscal_fixtures",
                            "record_chain", "submission_kinds"]

# A statutory operation (one submission to a tax authority) is always in exactly one of these.
StatutoryOperationState = Literal["pending", "sent", "accepted", "rejected", "outcome_unknown",
                                  "cancel_pending", "cancelled", "voided"]

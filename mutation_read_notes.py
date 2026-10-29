# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The writes a read request may make (READ_ROUTE_WRITES), which the write observer
(mutation_observer.py) holds each write of a GET or HEAD request to and the census's scan
of an unrequested read route (scripts/mutation_census.py) stops at. Standard library only,
so the census job reads it without the app installed."""

from __future__ import annotations

from dataclasses import dataclass


# The writes a read request may make, by table and statement type: a note that the read
# happened, which a peer's protocol sends as a read, or a credential a read hands out.
# Each names the only columns it may set, the only functions that may make it and, when
# only one route may, that route; any other write of a read request is a finding.
@dataclass(frozen=True)
class ReadNote:
    columns: tuple[str, ...]  # the only columns the statement may set
    sites: tuple[str, ...]  # "<file> <function>" that make the write
    reason: str
    route: str | None = None  # "<METHOD> <path template>" of the only request that may make it


READ_ROUTE_WRITES: dict[tuple[str, str], ReadNote] = {
    ("company_peer_grants", "UPDATE"): ReadNote(
        ("last_used_at",), ("celerp/services/peer_journals.py journal_feed",),
        "a connected company's key stamped with its last sync when that company reads the journal feed, "
        "a GET in the peer protocol between installs"),
    ("session_registry", "UPDATE"): ReadNote(
        ("expiry",), ("celerp/credentials.py issue_token_pair",),
        "a session's expiry slid forward when the middleware refreshes the token of any request, a read too"),
    ("session_registry", "INSERT"): ReadNote(
        ("company_id", "expiry", "jti", "user_id"), ("celerp/credentials.py issue_token_pair",),
        "a session registered again when the middleware refreshes the token of a request whose slot is gone"),
    ("user_auth_state", "UPDATE"): ReadNote(
        ("evicted_by_ip", "updated_at"), ("celerp/services/session_tracker.py pop_evicted_by_ip",),
        "the address that ended a user's sessions, shown once on that user's next request of any kind, "
        "a read too, and cleared as it is shown"),
    ("doc_share_tokens", "INSERT"): ReadNote(
        ("company_id", "created_at", "entity_id", "expires_at", "id", "revoked_at", "token"),
        ("default_modules/celerp-docs/celerp_docs/routes_share.py share_status",),
        "a document's share link minted deactivated the first time an editor opens its Share panel, "
        "which shows the link the cloud signs for it; Share (POST) turns it on",
        route="GET /docs/{entity_id}/share"),
    ("payment_recoveries", "UPDATE"): ReadNote(
        ("generation",), ("celerp/services/payments.py report_recoveries",),
        "Celerp Cloud's confirmation of an earlier System Recovery restore, stored as it answers before "
        "a payment opens; a customer opens the payment from an emailed link, a GET",
        route="GET /pay/{token}"),
}

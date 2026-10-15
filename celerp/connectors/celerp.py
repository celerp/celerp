# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Send approved company contacts using the canonical connector sync runner."""
from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from sqlalchemy import select

from celerp.connectors.base import (
    ConnectorBase, ConnectorCategory, ConnectorContext,
    SyncDirection, SyncEntity, SyncResult,
)
from celerp.connectors.http import RateLimitedClient
from celerp.models.projections import Projection

_PAGE = 100
_FIELDS = frozenset({
    "name", "company_name", "email", "phone", "billing_address",
    "shipping_address", "currency", "contact_type",
})


def _target_url(ctx: ConnectorContext) -> str:
    raw = ctx.store_handle
    if not isinstance(raw, str) or len(raw) > 1024:
        raise ValueError("No receiving Celerp installation is connected")
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == "https" and parsed.hostname is not None
                 and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443) and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The receiving Celerp address is invalid")
    return raw.rstrip("/") + "/company-peers/contacts"


async def _read_page(company_id: uuid.UUID, after: str) -> list[tuple[str, int, dict]]:
    from celerp.db import get_session_ctx
    async with get_session_ctx() as session:
        rows = (await session.execute(select(
            Projection.entity_id, Projection.version, Projection.state,
        ).where(
            Projection.company_id == company_id,
            Projection.entity_type == "contact",
            Projection.entity_id > after,
        ).order_by(Projection.entity_id).limit(_PAGE))).all()
    return [(str(entity_id), int(version), dict(state or {})) for entity_id, version, state in rows]


class CelerpConnector(ConnectorBase):
    name = "celerp"
    display_name = "Celerp Accountant"
    direction = SyncDirection.OUTBOUND
    category = ConnectorCategory.ACCOUNTING
    supported_entities = []
    store_scoped_ids = False
    conflict_strategy = {SyncEntity.CONTACTS: "source"}

    async def sync_products(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def sync_orders(self, ctx: ConnectorContext, since=None) -> SyncResult:
        raise NotImplementedError("Inbound peer synchronization is not supported")

    async def sync_contacts_out(self, ctx: ConnectorContext) -> SyncResult:
        result = SyncResult(entity=SyncEntity.CONTACTS, direction=SyncDirection.OUTBOUND,
                            errors=[], attention=[])
        target = _target_url(ctx)
        if not isinstance(ctx.access_token, str) or not ctx.access_token.startswith("cp1."):
            raise ValueError("No scoped receiving credential is connected")
        company_id = uuid.UUID(ctx.company_id)
        after = ""
        async with RateLimitedClient(public_only=True) as client:
            while True:
                page = await _read_page(company_id, after)
                if not page:
                    break
                for entity_id, revision, state in page:
                    if not isinstance(revision, int) or revision < 1:
                        result.errors.append("Contact revision unavailable")
                        result.attention.append({"id": entity_id, "label": entity_id,
                                                 "reason": "Contact version unavailable"})
                        continue
                    if state.get("is_self"):
                        result.skipped += 1
                        continue
                    if state.get("deleted") or state.get("merged_into"):
                        result.errors.append("Deleted contact needs review")
                        result.attention.append({"id": entity_id, "label": entity_id,
                                                 "reason": "Deleted contact needs review"})
                        continue
                    data = {key: state[key] for key in _FIELDS if key in state and state[key] is not None}
                    if not isinstance(data.get("name"), str) or not data["name"].strip():
                        result.errors.append("Contact name unavailable")
                        result.attention.append({"id": entity_id, "label": entity_id,
                                                 "reason": "Contact name unavailable"})
                        continue
                    try:
                        response = await client.post(target, headers={
                            "Authorization": "Peer " + ctx.access_token,
                        }, json={"source_company_id": str(company_id),
                                 "source_entity_id": entity_id,
                                 "source_revision": revision,
                                 "data": data})
                        if response.status_code != 200:
                            raise ValueError("Receiving installation refused contact update")
                        status = response.json().get("status")
                        if status not in ("created", "updated", "skipped"):
                            raise ValueError("Receiving installation did not acknowledge contact update")
                        result.record(status)
                    except Exception:
                        result.errors.append("Contact delivery incomplete")
                        result.attention.append({"id": entity_id, "label": entity_id,
                                                 "reason": "Receiving installation needs attention"})
                after = page[-1][0]
                if len(page) < _PAGE:
                    break
        return result

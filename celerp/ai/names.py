# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The names a proposed change's record ids are shown by on its confirm card.

A card lists what a change will write; an id there means nothing to the person
confirming. When a proposal is stored, every id it carries (a value under a key
named ``id``, ``ids`` or ending ``_id`` / ``_ids``, at any depth) is looked up and
the record's name kept beside the proposal. Core names the records kept as
projections, a module names its own records through the ``record_names`` slot.
An id no namer knows, or one the asker cannot read, stays shown as the id.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_ID_KEY = re.compile(r"(^|_)ids?$")
# The state fields a record is named by, first present wins.
_NAME_FIELDS = ("doc_number", "name", "sku", "title", "description")
_MAX_IDS = 500


def is_id_key(key: str) -> bool:
    """Whether a field holds record ids."""
    return bool(_ID_KEY.search(str(key)))


def record_ids(value, key: str = "") -> set[str]:
    """Every id held under an id key in ``value``, at any depth."""
    if isinstance(value, dict):
        return set().union(*(record_ids(v, str(k)) for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(record_ids(v, key) for v in value))
    if isinstance(value, str) and value and is_id_key(key):
        return {value}
    return set()


def state_name(state: dict) -> str | None:
    """A record's name from its state: the first named field that holds text."""
    return next((str(state[f]).strip() for f in _NAME_FIELDS
                 if isinstance(state.get(f), (str, int)) and str(state[f]).strip()), None)


async def _projection_names(session: AsyncSession, company_id: uuid.UUID, settings: dict, role: str,
                            ids: set[str]) -> dict[str, str]:
    """Names of the company's projection records whose events this role may read."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp.services.activity_redaction import readable_events

    readable = select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.entity_id.in_(ids))
    rows = (await session.execute(
        select(Projection.entity_id, Projection.state).where(
            Projection.company_id == company_id, Projection.entity_id.in_(ids),
            Projection.entity_id.in_(readable_events(readable, settings, role))))).all()
    return {entity_id: name for entity_id, state in rows if (name := state_name(state or {}))}


async def record_names(session: AsyncSession, company_id: uuid.UUID, settings: dict, role: str,
                       arguments) -> dict[str, str]:
    """{id: name} for the ids in ``arguments`` a namer knows and this role may read."""
    from celerp.modules import slots
    from celerp.modules.slots import resolve_handler

    ids = set(sorted(record_ids(arguments))[:_MAX_IDS])
    if not ids:
        return {}
    names = await _projection_names(session, company_id, settings, role, ids)
    for contribution in slots.get("record_names"):
        left = ids - names.keys()
        if not left:
            break
        try:
            names.update(await resolve_handler(contribution["handler"])(
                session=session, company_id=company_id, settings=settings, role=role, ids=left))
        except Exception as exc:  # a namer that fails leaves its ids shown as ids
            log.warning("record_names %s failed: %s", contribution["handler"], exc)
    return names


async def name_proposals(session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID,
                         records: list) -> list:
    """``records`` with each proposal carrying the names of the ids it holds."""
    from celerp.services.permissions import read_authority

    proposals = [r for r in records if isinstance(r, dict) and "arguments" in r]
    if not proposals:
        return records
    role, settings = await read_authority(session, company_id, user_id)
    for record in proposals:
        record["names"] = await record_names(session, company_id, settings, role, record["arguments"])
    return records

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The names a proposed change's record ids are shown by on its confirm card.

A card lists what a change will write; an id there means nothing to the person
confirming. Each time a proposal is shown, every id it carries (a value under a key
named ``id``, ``ids`` or ending ``_id`` / ``_ids``, at any depth) is looked up for
the person reading it, under the role they hold then, and the record's name shown
beside the proposal; nothing is kept, so a name follows the record and the reader's
permission as they stand. Core names the records kept as projections, a module the
company uses names its own records through the ``record_names`` slot. Core also
names the files a person attached for the assistant and the AI readings of them. An
id no namer knows, or one the reader cannot read, is never shown: the card says its
name is not available.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_ID_KEY = re.compile(r"(^|_)ids?$")
# A value shaped like a record id whatever field holds it: a uuid or an assistant upload.
_ID_VALUE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}|ai_up_[0-9a-f]{32}")
# The state fields a record is named by, first present wins.
_NAME_FIELDS = ("doc_number", "name", "sku", "title", "description")
_MAX_IDS = 500


def is_id_key(key: str) -> bool:
    """Whether a field holds record ids."""
    return bool(_ID_KEY.search(str(key)))


def is_id_value(value) -> bool:
    """Whether a value is shaped like a record id, which means nothing to a person."""
    return isinstance(value, str) and bool(_ID_VALUE.fullmatch(value.strip()))


def record_ids(value, key: str = "") -> set[str]:
    """Every id in ``value``, at any depth: held under an id key or shaped like one."""
    if isinstance(value, dict):
        return set().union(*(record_ids(v, str(k)) for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(record_ids(v, key) for v in value))
    if isinstance(value, str) and value and (is_id_key(key) or is_id_value(value)):
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


def _upload_names(company_id: uuid.UUID, ids: set[str]) -> dict[str, str]:
    """The file names of the company's assistant uploads among ``ids``."""
    from celerp.ai.files import upload_meta

    names = {}
    for file_id in ids:
        meta = upload_meta(file_id, company_id)
        if meta and meta.get("filename"):
            names[file_id] = str(meta["filename"])
    return names


async def _reading_names(session: AsyncSession, company_id: uuid.UUID, ids: set[str]) -> dict[str, str]:
    """The company's AI readings among ``ids``, each named by the files it reads."""
    from celerp.models.ai import AIBatchJob as AIJob

    keys = set()
    for value in ids:
        try:
            keys.add(uuid.UUID(value))
        except ValueError:
            continue
    if not keys:
        return {}
    rows = (await session.execute(select(AIJob.id, AIJob.file_ids).where(
        AIJob.company_id == company_id, AIJob.id.in_(keys)))).all()
    names = {}
    for job_id, file_ids in rows:
        files = _upload_names(company_id, {str(f) for f in file_ids or []})
        if files:
            names[str(job_id)] = ", ".join(files[str(f)] for f in file_ids if str(f) in files)
    return names


async def record_names(session: AsyncSession, company_id: uuid.UUID, settings: dict, role: str,
                       arguments) -> dict[str, str]:
    """{id: name} for the ids in ``arguments`` a namer knows and this role may read."""
    from celerp.modules import slots
    from celerp.modules.registry import uses_module
    from celerp.modules.slots import resolve_handler

    ids = set(sorted(record_ids(arguments))[:_MAX_IDS])
    if not ids:
        return {}
    names = _upload_names(company_id, {i for i in ids if i.startswith("ai_up_")})
    names.update(await _reading_names(session, company_id, ids - names.keys()))
    names.update(await _projection_names(session, company_id, settings, role, ids - names.keys()))
    for contribution in slots.get("record_names"):
        if not uses_module(settings, contribution.get("_module")):
            continue
        left = ids - names.keys()
        if not left:
            break
        try:
            names.update(await resolve_handler(contribution["handler"])(
                session=session, company_id=company_id, settings=settings, role=role, ids=left))
        except Exception as exc:  # a namer that fails leaves its ids shown as ids
            log.warning("record_names %s failed: %s", contribution["handler"], exc)
    return names


async def named_for_reader(session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID,
                           actions: list[dict]) -> list[dict]:
    """``actions`` as ``user_id`` is shown them now: each proposal carries the names of
    the ids it holds that this reader may read under the role they hold."""
    from celerp.services.permissions import read_authority

    if not any("arguments" in a for a in actions):
        return actions
    role, settings = await read_authority(session, company_id, user_id)
    return [{**a, "names": await record_names(session, company_id, settings, role, a["arguments"])}
            if "arguments" in a else a for a in actions]

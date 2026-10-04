# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A database an older release left behind, and the start of this release on it.

``fixtures/pre366/mfg_runs.json`` was captured by driving the older release's own API
(``fixtures/pre366/capture.py``, run from a checkout of that release): its ledger events,
the projection rows its handlers wrote from them, its chart of accounts and its company
settings, exactly as stored, for two companies. Both booked their stock on hand the way that
release did, by viewing the balance sheet before any run was issued. Nothing here passes through this release's handlers, so a test
that loads it starts from what an upgrading installation really holds.

Company ``main`` (components A at 5 and B at 3, product FG with a recipe), runs by name:
- ``settle``: built from a recipe (2 x A + 1 x B), every component issued, nothing received.
- ``dup`` / ``dup_issued``: imported with A required on two lines (1 and 2); the second issued.
- ``zero`` / ``negative``: imported with A required at 0 and at -1; the negative one issued
  (only B moved).

Company ``generic`` (component C at 2), runs by name:
- ``generic_received`` / ``generic_open``: created with an output named by text only (no
  output item); 2 x C issued to each, and 1 of 2 "received" by the first, which made no lot.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

FIXTURE = Path(__file__).parent / "fixtures" / "pre366" / "mfg_runs.json"
# The release an upgrading installation last started on.
OLDER_RELEASE = "1.1.18"
_ROOT = Path(__file__).resolve().parent.parent
_PROJECTION_FLAGS = ("is_available", "is_on_memo", "is_on_marketplace", "is_sync_to_shopify",
                     "is_in_production", "is_expired")


def _when(value: str | None):
    return datetime.fromisoformat(value) if value else None


async def load(session, company: str = "main") -> dict:
    """Store the older release's ``company`` under a fresh company and user id, and return
    the test's view of it: auth headers, ids, and the item and run ids by name."""
    from celerp_accounting.models import Account
    from test_helpers import make_authed_token

    old = json.loads(FIXTURE.read_text())["companies"][company]
    cid, uid = uuid.uuid4(), uuid.uuid4()
    raw = json.dumps(old).replace(old["company_id"], str(cid)).replace(old["user_id"], str(uid))
    data = json.loads(raw)
    session.add(Company(id=cid, name=data["company"]["name"], slug=f"pre366-{cid.hex[:8]}",
                        settings=data["company"]["settings"]))
    session.add(User(id=uid, email=f"admin-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    for a in data["accounts"]:
        session.add(Account(id=uuid.uuid4(), company_id=cid, **a))
    for e in data["ledger"]:
        session.add(LedgerEntry(company_id=cid, entity_id=e["entity_id"], entity_type=e["entity_type"],
                                event_type=e["event_type"], data=e["data"], actor_id=uid if e["actor"] else None,
                                location_id=None, source=e["source"], idempotency_key=e["idempotency_key"],
                                metadata_=e["metadata"], ts=_when(e["ts"])))
        await session.flush()  # ledger ids keep the older release's order
    for p in data["projections"]:
        session.add(Projection(company_id=cid, entity_id=p["entity_id"], entity_type=p["entity_type"],
                               state=p["state"], version=p["version"], location_id=None,
                               created_at=_when(p["created_at"]), updated_at=_when(p["updated_at"]),
                               expires_at=_when(p["expires_at"]), **{k: p[k] for k in _PROJECTION_FLAGS}))
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": cid, "user_id": uid,
            "items": data["items"], "runs": data["runs"]}


async def last_started_on_older_release(session) -> None:
    """Record that the database last started on an older release, before projection
    semantics were versioned."""
    from sqlalchemy import text

    from celerp.migrations._data_reconcile import _META_TABLE, PROJECTION_VERSION_KEY, set_meta
    from celerp.services.dev_release_guard import PROJECTION_SEMANTICS_KEY

    conn = await session.connection()
    await conn.run_sync(lambda c: set_meta(c, PROJECTION_VERSION_KEY, OLDER_RELEASE))
    await conn.run_sync(lambda c: c.execute(text(f"DELETE FROM {_META_TABLE} WHERE key = :k"),
                                            {"k": PROJECTION_SEMANTICS_KEY}))
    await session.commit()


_START_SLOTS = ("on_modules_ready", "inventory_in_production")


@contextmanager
def startup_hooks():
    """The bundled modules' start hooks registered, in the order the module loader loads
    them, for as long as the block runs."""
    from celerp.modules import slots
    from celerp.modules.loader import _topo_sort, read_manifest

    saved = {s: slots.get(s) for s in _START_SLOTS}
    for s in _START_SLOTS:
        slots._slots[s] = []
    pkgs = sorted(p for p in (_ROOT / "default_modules").iterdir() if (p / "__init__.py").exists())
    for pkg in _topo_sort(pkgs, {p.name for p in pkgs}):
        manifest_slots = read_manifest(pkg).get("slots") or {}
        for slot in _START_SLOTS:
            contribs = manifest_slots.get(slot) or []
            for c in contribs if isinstance(contribs, list) else [contribs]:
                slots.register(slot, {**c, "_module": pkg.name})
    try:
        yield
    finally:
        for s, v in saved.items():
            slots._slots[s] = v


async def start() -> None:
    """The normal start of this release on what the older release left (needs the ``client``
    fixture, which routes the start's own sessions to the test's)."""
    from celerp.main import _bring_data_current

    with startup_hooks():
        await _bring_data_current(modules_ready=True)


async def upgraded(session, company: str = "main") -> dict:
    """``company`` as the older release left it, after this release's first start on it."""
    old = await load(session, company)
    await last_started_on_older_release(session)
    await start()
    return old

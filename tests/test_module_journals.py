# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The posting interface Celerp's own modules use for their journals.

A module posts through celerp.services.module_journals only as itself, only for an
operation type it registered while modules were starting, only with the authority the
registry holds for that type, only to accounts the chart accepts, and only from a
record of its own. A third-party module cannot post through it, nor import the
low-level emitter behind it. The same key with the same request returns the journal
already posted; the same key asking for something else is refused.

The fixed-assets module's own code is stood in for by a probe compiled under a file
name inside that module's package, which is how the interface tells modules apart.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from celerp.modules import loader

MODULE = "celerp-fixed-assets"
_REPO = Path(__file__).resolve().parent.parent
_PROBE_FILE = _REPO / "default_modules" / MODULE / "celerp_fixed_assets" / "_journal_probe.py"

_PROBE = '''
from celerp.services import module_journals as mj


def register(types, **kw):
    mj.register_operation_types(kw.pop("module", "celerp-fixed-assets"), types, **kw)


async def post(session, **kw):
    kw.setdefault("module", "celerp-fixed-assets")
    return await mj.post_journal(session, **kw)


async def reverse(session, **kw):
    kw.setdefault("module", "celerp-fixed-assets")
    return await mj.reverse_journal(session, **kw)


async def status(session, **kw):
    kw.setdefault("module", "celerp-fixed-assets")
    return await mj.journal_status(session, **kw)
'''


def _probe(path: Path = _PROBE_FILE) -> dict:
    """The probe's functions, compiled as code of the file at ``path``."""
    namespace: dict = {}
    exec(compile(_PROBE, str(path), "exec"), namespace)
    return namespace


@pytest.fixture
def registry():
    """The operation-type registry, restored after the test."""
    from celerp.services import module_journals as mj

    saved = mj.snapshot_registry()
    yield mj
    mj.restore_registry(saved)


def _refused(exc: pytest.ExceptionInfo, status: int, key: str) -> dict:
    assert exc.value.status_code == status, exc.value.detail
    assert exc.value.detail["message_key"] == key, exc.value.detail
    return exc.value.detail


async def _asset_record(session, company_id, entity_id: str | None = None) -> str:
    """A record under the module's own prefix for a journal to point at."""
    from datetime import datetime, timezone

    from celerp.models.projections import Projection

    entity_id = entity_id or f"fa:asset:{uuid.uuid4()}"
    session.add(Projection(company_id=company_id, entity_id=entity_id, entity_type="fa_asset",
                           state={"code": "FA-0001"}, version=1,
                           updated_at=datetime.now(timezone.utc)))
    await session.flush()
    return entity_id


def _lines(debit: str, credit: str, amount: str, *, moves: bool = False):
    from celerp.services.module_journals import JournalLine

    return [JournalLine(account=debit, debit=Decimal(amount)),
            JournalLine(account=credit, credit=Decimal(amount), moves_recognized=moves)]


async def _post(probe, session, auth, source, *, key=None, operation="s6_capitalize",
                lines=None, user_id=None, company_id=None, intent=None):
    from celerp.services.module_journals import JournalSource

    return await probe["post"](
        session, company_id=company_id or auth["company_id"], user_id=user_id or auth["user_id"],
        key=key or uuid.uuid4().hex, operation=operation, date="2026-01-15", memo="Probe",
        lines=lines if lines is not None else _lines("1210", "1111", "100.00"),
        source=JournalSource(entity_id=source, href="/fixed-assets/1", label="FA-0001"),
        intent=intent or {"what": "probe"})


# ── Caller ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_third_party_caller_refused(session, auth, registry, tmp_path):
    """Code that is not the named module's own is refused by the caller rule: a module
    folder outside the bundled set, and the bundled module's name claimed from there."""
    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    source = await _asset_record(session, auth["company_id"])

    pkg = tmp_path / "modules" / "acme-poster" / "acme_poster"
    pkg.mkdir(parents=True)
    hostile = _probe(pkg / "poster.py")
    for module in ("acme-poster", MODULE):
        with pytest.raises(HTTPException) as exc:
            await _post({"post": lambda s, **kw: hostile["post"](s, module=module, **kw)},
                        session, auth, source)
        detail = _refused(exc, 403, "module_journal.caller")
        assert detail["params"]["module"] == module
    with pytest.raises(HTTPException) as exc:
        await hostile["reverse"](session, company_id=auth["company_id"], user_id=auth["user_id"],
                                 key="any", operation="s6_capitalize", reason="x")
    _refused(exc, 403, "module_journal.caller")


# ── Admission ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("imported", ["celerp.services.auto_je", "celerp.services.module_journals"])
def test_s6_admission_refuses_auto_je_import(tmp_path, monkeypatch, imported):
    """A third-party module importing the low-level emitter, or the posting interface,
    is refused at admission and runs nothing."""
    from celerp.models.base import Base

    before_tables = set(Base.metadata.tables)
    before_path, before_mods = list(sys.path), set(sys.modules)
    base = tmp_path / "modules"
    folder, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
    pkg = base / folder
    (pkg / inner).mkdir(parents=True)
    manifest = {"name": folder, "version": "1.0.0", "api_routes": f"{inner}.routes"}
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    (pkg / inner / "__init__.py").write_text("")
    marker = tmp_path / "ran.txt"
    (pkg / inner / "routes.py").write_text(
        f"from {imported} import *  # noqa: F401,F403\n\n"
        f"def setup_api_routes(app):\n    open({str(marker)!r}, 'a').write('ran')\n")
    monkeypatch.setenv("MODULE_DIR", str(base))
    try:
        loaded = loader.load_all(str(base), {folder})
        assert folder not in {m["name"] for m in loaded}
        assert not loader.is_running(folder)
        assert imported in loader.load_errors()[folder]
        assert not marker.exists()
    finally:
        for key in set(Base.metadata.tables) - before_tables:
            Base.metadata.remove(Base.metadata.tables[key])
        loader._load_errors.pop(folder, None)
        loader._module_tables.clear()
        sys.path[:] = before_path
        for key in set(sys.modules) - before_mods:
            if key.startswith("acme_"):
                sys.modules.pop(key, None)


# ── Authority ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_company_and_user_authority(session, auth, registry):
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company
    from test_helpers import ensure_user

    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    source = await _asset_record(session, auth["company_id"])

    # An unknown operation type.
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, operation="s6_unregistered")
    _refused(exc, 422, "module_journal.unknown_operation")

    # A user of another company, and a member lacking the key the type holds.
    stranger = uuid.uuid4()
    await ensure_user(session, stranger)
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, user_id=stranger)
    assert exc.value.status_code == 403
    operator = uuid.uuid4()
    await ensure_user(session, operator)
    session.add(UserCompany(user_id=operator, company_id=auth["company_id"], role="operator", is_active=True))
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, user_id=operator)
    assert exc.value.status_code == 403 and "Manage accounting" in str(exc.value.detail)

    # The company does not use the module: refused for an ordinary type, posted for a
    # linked-document correction type.
    probe["register"]({"s6_sale_adjust": ("finalize_documents",)}, source_prefix="fa:",
                      document_corrections=("s6_sale_adjust",))
    company = await session.get(Company, auth["company_id"])
    company.settings = {**(company.settings or {}), "enabled_modules": ["celerp-accounting"]}
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source)
    _refused(exc, 409, "module_journal.module_off")
    posted = await _post(probe, session, auth, source, operation="s6_sale_adjust")
    assert posted.je_id.startswith(f"je:mod:{MODULE}:")

    # Registration: a key outside the permission registry, a third-party caller, and any
    # registration once modules are ready.
    with pytest.raises(ValueError, match="permission"):
        probe["register"]({"s6_bad": ("no_such_key",)}, source_prefix="fa:")
    with pytest.raises(ValueError, match="permission"):
        probe["register"]({"s6_bad": ()}, source_prefix="fa:")
    hostile = _probe(Path("/tmp/acme-x/acme_x/poster.py"))
    with pytest.raises(ValueError, match="Celerp's own"):
        hostile["register"]({"s6_other": ("manage_accounting",)}, source_prefix="fa:", module="acme-x")
    registry.freeze_operation_types()
    with pytest.raises(ValueError, match="after modules"):
        probe["register"]({"s6_late": ("manage_accounting",)}, source_prefix="fa:")


# ── Accounts ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_accounts_validated(session, auth, registry):
    from celerp_accounting.models import Account

    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    source = await _asset_record(session, auth["company_id"])
    cid = auth["company_id"]

    for code, key in (("1200", "posting.destination.header"), ("9999", "posting.destination.not_in_chart")):
        with pytest.raises(HTTPException) as exc:
            await _post(probe, session, auth, source, lines=_lines(code, "1111", "10.00"))
        _refused(exc, 422, key)

    await _post(probe, session, auth, source, lines=_lines("1210", "1111", "100.00"))
    await session.execute(update(Account).where(Account.company_id == cid, Account.code == "1210")
                          .values(is_active=False))
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, lines=_lines("1210", "1111", "10.00"))
    _refused(exc, 422, "posting.destination.inactive")

    # Moving what the module recognized off the now inactive account: up to its balance.
    await _post(probe, session, auth, source, lines=_lines("6400", "1210", "60.00", moves=True))
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, lines=_lines("6400", "1210", "50.00", moves=True))
    detail = _refused(exc, 422, "module_journal.moves_more_than_recognized")
    assert detail["params"]["code"] == "1210"
    # An account the module never posted to holds nothing of its own to move.
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, lines=_lines("6400", "1220", "1.00", moves=True))
    _refused(exc, 422, "module_journal.moves_more_than_recognized")


# ── Source and namespace ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_source_identity_and_namespace(session, auth, registry):
    from test_helpers import company_auth

    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    other = await company_auth(session, uuid.uuid4(), uuid.uuid4())
    elsewhere = await _asset_record(session, other["company_id"])
    for source in ("doc:inv-1", elsewhere, "fa:asset:missing"):
        with pytest.raises(HTTPException) as exc:
            await _post(probe, session, auth, source)
        _refused(exc, 422, "module_journal.source")
    own = await _asset_record(session, auth["company_id"])
    from celerp.services.module_journals import JournalSource
    with pytest.raises(HTTPException) as exc:
        await probe["post"](
            session, company_id=auth["company_id"], user_id=auth["user_id"], key="k-href",
            operation="s6_capitalize", date="2026-01-15", memo="Probe",
            lines=_lines("1210", "1111", "1.00"),
            source=JournalSource(entity_id=own, href="https://elsewhere.example/x", label="FA-0001"),
            intent={})
    _refused(exc, 422, "module_journal.source")

    posted = await _post(probe, session, auth, own, key="mine")
    assert posted.je_id == f"je:mod:{MODULE}:mine"
    # Another module's journal under the same key is out of reach: the caller is not it.
    with pytest.raises(HTTPException) as exc:
        await probe["reverse"](session, module="celerp-manufacturing", company_id=auth["company_id"],
                               user_id=auth["user_id"], key="mine", operation="s6_capitalize", reason="x")
    _refused(exc, 403, "module_journal.caller")
    # Its own journal reverses, once.
    first = await probe["reverse"](session, company_id=auth["company_id"], user_id=auth["user_id"],
                                   key="mine", operation="s6_capitalize", reason="Undo")
    again = await probe["reverse"](session, company_id=auth["company_id"], user_id=auth["user_id"],
                                   key="mine", operation="s6_capitalize", reason="Undo")
    assert (first.existing, again.existing) == (False, True)
    assert await probe["status"](session, company_id=auth["company_id"], key="mine") == "void"


# ── Idempotency ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_altered_idempotent_request_refused(session, auth, registry):
    from celerp.models.projections import Projection

    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    source = await _asset_record(session, auth["company_id"])
    first = await _post(probe, session, auth, source, key="same")
    again = await _post(probe, session, auth, source, key="same")
    assert (first.je_id, first.existing, again.existing) == (again.je_id, False, True)
    with pytest.raises(HTTPException) as exc:
        await _post(probe, session, auth, source, key="same", lines=_lines("1210", "1111", "99.00"))
    _refused(exc, 409, "module_journal.key_reused")
    row = await session.get(Projection, (auth["company_id"], first.je_id))
    assert [(e["account"], e["debit"], e["credit"]) for e in row.state["entries"]] == [
        ("1210", 100.0, 0.0), ("1111", 0.0, 100.0)]
    assert row.state["source"] == {"module": MODULE, "entity_id": source, "href": "/fixed-assets/1",
                                   "label": "FA-0001"}


# ── Journal view and manual void ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6_journal_links_its_source_and_manual_void_refuses_it(client, session, auth, registry):
    """The journal names and links the module record a module journal came from, from
    the label the journal kept. The manual void route leaves it to the module."""
    probe = _probe()
    probe["register"]({"s6_capitalize": ("manage_accounting",)}, source_prefix="fa:")
    source = await _asset_record(session, auth["company_id"])
    posted = await _post(probe, session, auth, source, key="shown")
    await session.commit()

    r = await client.get("/accounting/journal", headers=auth["headers"],
                         params={"date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert r.status_code == 200, r.text
    (entry,) = [e for e in r.json()["entries"] if e["je_id"] == posted.je_id]
    assert entry["module_source"] == {"label": "Fixed assets: FA-0001", "href": "/fixed-assets/1"}

    from ui.components.journal import journal_rows
    rows = journal_rows({"entries": [entry]}, items=False)
    assert {(row["source"], row["href"]) for row in rows} == {("Fixed assets: FA-0001", "/fixed-assets/1")}

    r = await client.post(f"/accounting/journal-entries/{posted.je_id}/void", headers=auth["headers"],
                          json={"reason": "review"})
    assert r.status_code == 422, r.text
    assert await probe["status"](session, company_id=auth["company_id"], key="shown") == "posted"

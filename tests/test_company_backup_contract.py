# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Which company a module's undeclared data blocks: only a company that actually holds
rows in a table nobody said how to back up. Another company on the same installation
backs up normally, and the refusal names the module, not its storage."""

from __future__ import annotations

import pytest

from migration_support import auth, code_config, count, maker, real_client, real_engine  # noqa: F401
from test_company_backup import (  # noqa: F401
    _bk_cb,
    _bk_drop,
    _bk_fake_module,
    _bk_local,
    _bk_modules_running,
    _bk_setup,
    _bk_sql,
)
from company_backup_support import company, members, token

pytestmark = pytest.mark.asyncio

_WIDGETS = ("CREATE TABLE zz_widgets (id uuid primary key, "
            "company_id uuid not null references companies(id) on delete cascade, note text)")
_GADGETS = ("CREATE TABLE zz_gadgets (id uuid primary key, "
            "company_id uuid not null references companies(id) on delete cascade, note text)")


async def _two_companies(engine):
    user, a, tok_a = await _bk_setup(engine, "Alpha Trading", "alpha-marker")
    b = await company(engine, user, "Beta Trading", "beta-marker")
    return user, a, tok_a, b, await token(engine, user, b)


async def _row(engine, table: str, cid, note: str) -> None:
    await _bk_sql(engine, f"INSERT INTO {table} (id, company_id, note) VALUES (gen_random_uuid(), :c, :n)",
                  c=str(cid), n=note)


def _no_files(tmp_path) -> None:
    root = tmp_path / "company_backups"
    left = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    assert left == [], left


async def test_undeclared_table_without_rows_blocks_nobody(real_engine, real_client, tmp_path, monkeypatch):
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include"})
    _, a, tok_a, b, tok_b = await _two_companies(real_engine)
    await _bk_sql(real_engine, _WIDGETS)
    await _bk_sql(real_engine, _GADGETS)
    try:
        for tok in (tok_a, tok_b):
            r = await real_client.get("/company-backups/download", headers=auth(tok))
            assert r.status_code == 200, r.text
    finally:
        await _bk_drop(real_engine, "zz_widgets", "zz_gadgets")


async def test_undeclared_rows_block_only_their_company(real_engine, real_client, tmp_path, monkeypatch):
    """Rows for A refuse A by the module's name, never by table; B still backs up."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include"})
    _, a, tok_a, b, tok_b = await _two_companies(real_engine)
    await _bk_sql(real_engine, _WIDGETS)
    await _bk_sql(real_engine, _GADGETS)
    await _row(real_engine, "zz_gadgets", a, "alpha gadget")
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok_a))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "Widgets" in detail and "zz_gadgets" not in detail and "zz-widgets" not in detail, detail
        assert "Nothing was backed up." in detail
        _no_files(tmp_path)
        r = await real_client.get("/company-backups/download", headers=auth(tok_b))
        assert r.status_code == 200, r.text
    finally:
        await _bk_drop(real_engine, "zz_widgets", "zz_gadgets")


async def test_declared_tables_carry_only_their_company(real_engine, real_client, tmp_path, monkeypatch):
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include", "zz_gadgets": "exclude"})
    _, a, tok_a, b, tok_b = await _two_companies(real_engine)
    await _bk_sql(real_engine, _WIDGETS)
    await _bk_sql(real_engine, _GADGETS)
    for cid, name in ((a, "alpha"), (b, "beta")):
        await _row(real_engine, "zz_widgets", cid, f"{name} widget")
        await _row(real_engine, "zz_gadgets", cid, f"{name} gadget")
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok_a))
        assert r.status_code == 200, r.text
        body = members(r.content)
        assert "tables/zz_gadgets.jsonl" not in body
        widgets = body["tables/zz_widgets.jsonl"].decode()
        assert "alpha widget" in widgets and "beta" not in widgets
    finally:
        await _bk_drop(real_engine, "zz_widgets", "zz_gadgets")


async def test_undeclared_module_table_without_company_column_blocks(real_engine, real_client, tmp_path,
                                                                     monkeypatch):
    """A module table with no company column cannot be scoped to a company, so it blocks
    every backup until the module says how it travels."""
    _bk_local(monkeypatch, tmp_path)
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include"})
    _, a, tok_a, b, tok_b = await _two_companies(real_engine)
    await _bk_sql(real_engine, _WIDGETS)
    await _bk_sql(real_engine, "CREATE TABLE zz_cache (id uuid primary key, body text)")
    try:
        for tok in (tok_a, tok_b):
            r = await real_client.get("/company-backups/download", headers=auth(tok))
            assert r.status_code == 409, r.text
            assert "Widgets" in r.json()["detail"] and "zz_cache" not in r.json()["detail"]
        _no_files(tmp_path)
    finally:
        await _bk_drop(real_engine, "zz_widgets", "zz_cache")


async def test_unknown_core_table_blocks_only_company_with_rows(real_engine, real_client, tmp_path, monkeypatch):
    _bk_local(monkeypatch, tmp_path)
    _, a, tok_a, b, tok_b = await _two_companies(real_engine)
    await _bk_sql(real_engine, "CREATE TABLE bk_unknown_things (id uuid primary key, "
                               "company_id uuid not null references companies(id) on delete cascade, note text)")
    try:
        r = await real_client.get("/company-backups/download", headers=auth(tok_a))
        assert r.status_code == 200, r.text
        await _row(real_engine, "bk_unknown_things", a, "alpha thing")
        r = await real_client.get("/company-backups/download", headers=auth(tok_a))
        assert r.status_code == 409, r.text
        assert "bk_unknown_things" not in r.json()["detail"]
        assert "Nothing was backed up." in r.json()["detail"]
        r = await real_client.get("/company-backups/download", headers=auth(tok_b))
        assert r.status_code == 200, r.text
    finally:
        await _bk_drop(real_engine, "bk_unknown_things")


async def test_runtime_audit_finds_undeclared_tables_and_notifies(real_engine, tmp_path, monkeypatch):
    """After startup migrations, an installed module's table that its manifest does not
    name is reported once per company in the notification bell, by module label."""
    from sqlalchemy import select

    from celerp.models.notification import Notification
    _bk_fake_module(tmp_path, monkeypatch, backup={"zz_widgets": "include"})
    _, a, _, b, _ = await _two_companies(real_engine)
    await _bk_sql(real_engine, _WIDGETS)
    await _bk_sql(real_engine, _GADGETS)
    cb = _bk_cb()
    try:
        async with maker(real_engine)() as s:
            assert await cb.undeclared_module_tables(s) == {"zz-widgets": ["zz_gadgets"]}
            assert await cb.notify_undeclared_module_tables(s) == 2
            await s.commit()
            assert await cb.notify_undeclared_module_tables(s) == 0
            await s.commit()
            notes = (await s.execute(select(Notification).where(Notification.company_id.in_([a, b])))).scalars().all()
        assert len(notes) == 2
        assert all("Widgets" in n.title + n.body and "zz_gadgets" not in n.title + n.body for n in notes)
    finally:
        await _bk_drop(real_engine, "zz_widgets", "zz_gadgets")

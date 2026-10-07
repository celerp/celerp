# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""What a company reset removes is read from the live schema, so a table a module adds
later is handled without a hand list: rows reached from the company through foreign keys
go with it, and a table that cannot be placed safely stops the reset before any write."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from company_backup_support import snapshot, token
from migration_support import auth, real_client, real_engine  # noqa: F401
from test_company_reset import RESET, _local_files, _two_companies

pytestmark = pytest.mark.asyncio

NAME = "Harbor Goods Ltd"


async def _ddl(engine, *statements: str) -> None:
    async with engine.begin() as conn:
        for sql in statements:
            await conn.execute(text(sql))


async def _rows(engine, table: str) -> list[str]:
    async with engine.connect() as conn:
        return sorted((await conn.execute(text(f"SELECT note FROM {table}"))).scalars().all())


async def _refused(client, engine, tok: str, says: str) -> None:
    before = await snapshot(engine)
    r = await client.post(RESET, json={"company_name": NAME}, headers=auth(tok))
    assert r.status_code == 409, r.text
    assert says in r.json()["detail"] and r.json()["detail"].endswith("Nothing was deleted.")
    assert await snapshot(engine) == before


async def test_a_module_table_reached_through_a_foreign_key_chain_loses_only_this_companys_rows(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    await _ddl(real_engine,
               "CREATE TABLE zz_bins (id integer PRIMARY KEY, location_id uuid NOT NULL REFERENCES locations(id), "
               "note text)",
               "CREATE TABLE zz_bin_counts (id integer PRIMARY KEY, bin_id integer NOT NULL REFERENCES zz_bins(id), "
               "note text)")
    try:
        async with real_engine.begin() as conn:
            for n, (cid, marker) in enumerate(((a, "alpha"), (b, "bravo")), start=1):
                loc = await conn.scalar(text("SELECT id FROM locations WHERE company_id = :c LIMIT 1"), {"c": cid})
                await conn.execute(text("INSERT INTO zz_bins VALUES (:i, :l, :m)"), {"i": n, "l": loc, "m": marker})
                await conn.execute(text("INSERT INTO zz_bin_counts VALUES (:i, :i, :m)"), {"i": n, "m": marker})

        r = await real_client.post(RESET, json={"company_name": NAME},
                                   headers=auth(await token(real_engine, shared, a)))

        assert r.status_code == 200, r.text
        assert await _rows(real_engine, "zz_bins") == ["bravo"]
        assert await _rows(real_engine, "zz_bin_counts") == ["bravo"]
    finally:
        await _ddl(real_engine, "DROP TABLE IF EXISTS zz_bin_counts", "DROP TABLE IF EXISTS zz_bins")


async def test_company_tables_that_reference_each_other_stop_the_reset_before_any_write(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    await _ddl(real_engine,
               "CREATE TABLE zz_left (id integer PRIMARY KEY, company_id uuid NOT NULL REFERENCES companies(id), "
               "right_id integer, note text)",
               "CREATE TABLE zz_right (id integer PRIMARY KEY, left_id integer REFERENCES zz_left(id), note text)",
               "ALTER TABLE zz_left ADD FOREIGN KEY (right_id) REFERENCES zz_right(id)")
    try:
        async with real_engine.begin() as conn:
            await conn.execute(text("INSERT INTO zz_left VALUES (1, :c, NULL, 'alpha')"), {"c": a})
            await conn.execute(text("INSERT INTO zz_right VALUES (1, 1, 'alpha')"))
            await conn.execute(text("UPDATE zz_left SET right_id = 1"))

        await _refused(real_client, real_engine, await token(real_engine, shared, a), "reference each other")
    finally:
        await _ddl(real_engine, "DROP TABLE IF EXISTS zz_right CASCADE", "DROP TABLE IF EXISTS zz_left CASCADE")


async def test_an_installation_table_referencing_company_data_stops_the_reset_before_any_write(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    await _ddl(real_engine, "ALTER TABLE supporter_badges ADD COLUMN zz_location_id uuid REFERENCES locations(id)")
    try:
        await _refused(real_client, real_engine, await token(real_engine, shared, a),
                       "supporter_badges holds both installation and company data")
    finally:
        await _ddl(real_engine, "ALTER TABLE supporter_badges DROP COLUMN IF EXISTS zz_location_id")


async def test_a_table_added_at_runtime_holding_company_data_it_cannot_identify_fails_closed(
        real_engine, real_client, tmp_path, monkeypatch):
    _local_files(monkeypatch, tmp_path)
    shared, solo, a, b = await _two_companies(real_engine, tmp_path)
    await _ddl(real_engine, "CREATE TABLE zz_module_totals (id integer PRIMARY KEY, company uuid NOT NULL, "
                            "note text)")
    try:
        async with real_engine.begin() as conn:
            await conn.execute(text("INSERT INTO zz_module_totals VALUES (1, :a, 'alpha'), (2, :b, 'bravo')"),
                               {"a": a, "b": b})

        await _refused(real_client, real_engine, await token(real_engine, shared, a),
                       "zz_module_totals could not be identified as company data")
    finally:
        await _ddl(real_engine, "DROP TABLE IF EXISTS zz_module_totals")

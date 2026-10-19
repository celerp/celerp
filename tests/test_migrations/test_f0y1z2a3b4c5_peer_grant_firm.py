# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The firm-name revision gives every key issued before it the name its client was shown
until now: the company the firm's copy was opened from, else the copy's own name."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from .conftest import throwaway_db, upgrade_to

REVISION = "f0y1z2a3b4c5"


def _company(conn, name: str, settings: dict) -> str:
    cid = str(uuid.uuid4())
    conn.execute(text("INSERT INTO companies (id, name, slug, settings, created_at) "
                      "VALUES (:id, :n, :id, CAST(:s AS json), now())"),
                 {"id": cid, "n": name, "s": json.dumps(settings)})
    return cid


def _grant(conn, company_id: str) -> str:
    gid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn.execute(text(
        "INSERT INTO company_peer_grants (id, company_id, source_company_id, token_hash, label, "
        "scopes, created_at, expires_at) VALUES (:id, :c, :s, :h, 'Key', CAST('[]' AS json), :n, :e)"
    ), {"id": gid, "c": company_id, "s": str(uuid.uuid4()), "h": uuid.uuid4().hex * 2,
        "n": now, "e": now + timedelta(days=30)})
    return gid


def test_existing_keys_take_the_name_their_client_was_shown():
    with throwaway_db("peerfirm") as (_, sync_url):
        upgrade_to(sync_url, "e9x0y1z2a3b4")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                opened = _grant(conn, _company(
                    conn, "Firm copy", {"restored_backup": {"opened_from": "Lakeside Bookkeeping"}}))
                blank = _grant(conn, _company(conn, "Plain Firm", {"restored_backup": {"opened_from": "  "}}))
                plain = _grant(conn, _company(conn, "Other Firm", {}))
            upgrade_to(sync_url, REVISION)
            with engine.connect() as conn:
                firms = dict(conn.execute(text("SELECT CAST(id AS text), firm FROM company_peer_grants")).all())
        finally:
            engine.dispose()
    assert firms == {opened: "Lakeside Bookkeeping", blank: "Plain Firm", plain: "Other Firm"}

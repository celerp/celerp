# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Shared steps for the bank statement tests: a company with banks, a statement read
from an uploaded file and imported, the group table, and resolve."""

from __future__ import annotations

import json
import uuid as _uuid
import weakref

from httpx import AsyncClient

from celerp.ai.files import upload_dir
from test_helpers import until

API = "/accounting/reconciliation"


def key() -> str:
    return f"k-{_uuid.uuid4().hex}"


_owners: "weakref.WeakKeyDictionary[AsyncClient, dict]" = weakref.WeakKeyDictionary()


async def company(client: AsyncClient, currency: str = "USD") -> dict:
    """A fresh company's headers, its id and its user id: the first registers its owner
    (the install takes one registration), the next are that owner's further companies."""
    from celerp.services.auth import decode_access_token
    owner = _owners.get(client)
    if owner is None:
        r = await client.post("/auth/register", json={"email": f"bank_{key()}@example.com", "password": "pass1234",
                                                      "name": "Bank Tester", "company_name": f"BankCo {key()}"})
        assert r.status_code == 200, r.text
        h = _owners[client] = {"Authorization": f"Bearer {r.json()['access_token']}"}
    else:
        r = await client.post("/companies", json={"name": f"BankCo {key()}"}, headers=owner)
        assert r.status_code == 200, r.text
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/companies/me", headers=h)).json()
    uid = str(decode_access_token(h["Authorization"].split(" ", 1)[1])["sub"])
    return {"h": h, "company_id": me["id"], "user_id": uid}


async def bank(client: AsyncClient, h: dict, *, name: str = "Main Bank", currency: str = "USD",
               bank_type: str = "checking", opening: float = 0.0, opening_date: str | None = None,
               rate: float | None = None) -> dict:
    body = {"bank_name": name, "account_number": f"****{_uuid.uuid4().int % 10000:04d}",
            "bank_type": bank_type, "currency": currency, "opening_balance": opening}
    if opening_date:
        body["opening_date"] = opening_date
    if rate:
        body["rate"] = rate
    r = await client.post("/accounting/bank-accounts", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def upload(company_id: str, user_id: str, content: bytes, filename: str, content_type: str = "text/csv") -> str:
    file_id = f"ai_up_{_uuid.uuid4().hex}"
    d = upload_dir()
    (d / f"{file_id}.bin").write_bytes(content)
    (d / f"{file_id}.meta").write_text(json.dumps({
        "filename": filename, "content_type": content_type, "size": len(content),
        "company_id": company_id, "user_id": user_id,
    }))
    return file_id


def csv_bytes(rows: list[tuple[str, str, str]], *, balance: bool = False) -> bytes:
    """Date,Description,Amount rows; with ``balance`` the running balance from 0."""
    out = ["Date,Description,Amount" + (",Balance" if balance else "")]
    running = 0.0
    for d, desc, amt in rows:
        running += float(amt) if amt else 0.0
        out.append(f"{d},{desc},{amt}" + (f",{running:.2f}" if balance else ""))
    return ("\n".join(out) + "\n").encode()


async def read(client: AsyncClient, co: dict, files: list[tuple[bytes, str, str]], bank_id: str | None = None,
               idem: str | None = None) -> dict:
    """read-statement over uploaded files; waits for the job and returns the preview."""
    from celerp.ai import jobs
    file_ids = [upload(co["company_id"], co["user_id"], c, n, ct) for c, n, ct in files]
    body = {"file_ids": file_ids, "idempotency_key": idem or key()}
    if bank_id:
        body["bank_id"] = bank_id
    r = await client.post(f"{API}/read-statement", json=body, headers=co["h"])
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    await until(lambda: not jobs.running_tasks())
    p = await client.get(f"{API}/statement-preview/{job_id}", headers=co["h"])
    assert p.status_code == 200, p.text
    return p.json()


async def import_statement(client: AsyncClient, co: dict, preview: dict, *, bank_id: str | None = None,
                           new_bank: dict | None = None, header: dict | None = None,
                           idem: str | None = None, expect: int = 200, tick_all: bool = False) -> dict:
    """Import ``preview``'s statements as reviewed; ``tick_all`` ticks the lines the preview
    left unticked as probably already in the books."""
    statements = []
    for st in preview["statements"]:
        lines = [{**line, "ticked": True} for line in st["lines"]] if tick_all else st["lines"]
        statements.append({"header": {**st["header"], **(header or {})}, "lines": lines})
    body = {"job_id": preview["job_id"], "statements": statements, "idempotency_key": idem or key()}
    if bank_id:
        body["bank_id"] = bank_id
    if new_bank:
        body["new_bank"] = new_bank
    if preview.get("column_map"):
        body["column_map"] = preview["column_map"]
    r = await client.post(f"{API}/import-statement", json=body, headers=co["h"])
    assert r.status_code == expect, r.text
    return r.json()


async def statement(client: AsyncClient, co: dict, bank_id: str, rows: list[tuple[str, str, str]], *,
                    opening: str, closing: str, start: str, end: str, tick_all: bool = False) -> dict:
    """Read and import one CSV statement on ``bank_id``; returns the import answer."""
    preview = await read(client, co, [(csv_bytes(rows), f"st-{end}.csv", "text/csv")], bank_id=bank_id)
    return await import_statement(client, co, preview, bank_id=bank_id, tick_all=tick_all, header={
        "opening_balance": opening, "closing_balance": closing, "period_start": start, "period_end": end})


async def co_of(client: AsyncClient, h: dict) -> dict:
    """The company ``h`` signs in to, in the shape the helpers here take."""
    me = (await client.get("/companies/me", headers=h)).json()
    return {"h": h, "company_id": me["id"], "user_id": user_id(h)}


async def lines_on(client: AsyncClient, co: dict, bank_id: str, rows: list[tuple[str, str, str]], *,
                   opening: str, closing: str, start: str, end: str) -> tuple[str, list[dict]]:
    """Import every line of one statement on ``bank_id``, those that look already booked too:
    its session id and its lines by date, then description."""
    sid = (await statement(client, co, bank_id, rows, opening=opening, closing=closing, start=start, end=end,
                           tick_all=True))["sessions"][0]["id"]
    mine = [line for line in all_lines(await groups(client, co, bank_id, view="all")) if line["statement_id"] == sid]
    return sid, sorted(mine, key=lambda line: (line["line_date"], line["description"]))


async def groups(client: AsyncClient, co: dict, bank_id: str, **params) -> dict:
    r = await client.get(f"{API}/bank/{bank_id}/groups", params=params, headers=co["h"])
    assert r.status_code == 200, r.text
    return r.json()


def all_lines(table: dict) -> list[dict]:
    return [line for g in table["groups"] for line in g["lines"]]


def line_by_text(table: dict, text: str) -> dict:
    return next(line for line in all_lines(table) if text in line["description"])


async def resolve(client: AsyncClient, co: dict, bank_id: str, *, decisions: list | None = None,
                  accept: dict | None = None, idem: str | None = None, headers: dict | None = None):
    body = {"idempotency_key": idem or key()}
    if decisions is not None:
        body["decisions"] = decisions
    if accept is not None:
        body["accept"] = accept
    return await client.post(f"{API}/bank/{bank_id}/resolve", json=body, headers=headers or co["h"])


async def journal(session, co: dict, je_id: str) -> dict:
    """The stored state of one journal entry of ``co``."""
    from sqlalchemy import select
    from celerp.models.projections import Projection
    state = (await session.execute(select(Projection.state).where(
        Projection.company_id == _uuid.UUID(co["company_id"]), Projection.entity_id == je_id))).scalar_one()
    return state


async def bank_row(client: AsyncClient, h: dict, bank_id: str) -> dict:
    r = await client.get(f"/accounting/bank-accounts/{bank_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def user_id(headers: dict) -> str:
    """The user a bearer token signs in (its ``sub`` claim)."""
    import base64
    payload = headers["Authorization"].split(" ", 1)[1].split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"]


async def member(client: AsyncClient, session, co: dict, role: str) -> dict:
    """Headers for a new member of ``co`` holding ``role``."""
    from test_helpers import invite_user
    tok = await invite_user(client, session, co["h"], f"{role}-{_uuid.uuid4().hex[:8]}@example.com", role)
    return {"Authorization": f"Bearer {tok}"}

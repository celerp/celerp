# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Integration validation: three Celerp installations, each its own server process with
its own database, data folder and config, and a stand-in relay process between them.

A client moves its books in from Manager, a fresh installation opens the client's
company copy, the client sends the copy to its accountant by email link, and the
accountant's copy then keeps up with the client both ways through a company key. The
three companies' trial balance, receivables and payables agree at each step.

CI runs this file in its own job (``-m integration``)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from migration_support import auth, posting_choices_from
from test_company_copy_round_trip import _books

pytestmark = pytest.mark.integration

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
MANAGER_FILE = TESTS / "fixtures" / "manager_io" / "basic.manager"
RELAY_HOSTS = ("relay.celerp.com", "celerp-handoffs.sgp1.digitaloceanspaces.com")
FIRM_ADDRESS = "https://firm.integration.test"
MODULES = ("celerp-accounting,celerp-ai,celerp-connectors,celerp-contacts,celerp-sales-funnel,"
           "celerp-dashboard,celerp-docs,celerp-inventory,celerp-labels,celerp-manufacturing,"
           "celerp-reports,celerp-subscriptions,celerp-verticals")
INSTALLATIONS = ("client", "fresh", "firm")
BOOT_SECONDS = 120


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _database(base: str, name: str) -> str:
    parts = urlsplit(base)
    return parts._replace(path=f"{parts.path}_{name}").geturl()


async def _recreate(url: str) -> None:
    import asyncpg
    parts = urlsplit(url.replace("+asyncpg", ""))
    conn = await asyncpg.connect(host=parts.hostname, port=parts.port, user=parts.username,
                                 password=parts.password, database="postgres")
    try:
        name = parts.path.lstrip("/")
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop(url: str) -> None:
    import asyncpg
    parts = urlsplit(url.replace("+asyncpg", ""))
    conn = await asyncpg.connect(host=parts.hostname, port=parts.port, user=parts.username,
                                 password=parts.password, database="postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{parts.path.lstrip("/")}" WITH (FORCE)')
    finally:
        await conn.close()


def _wait_healthy(port: int, process: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + BOOT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Process on port {port} exited:\n{log.read_text()[-4000:]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Process on port {port} did not answer in time:\n{log.read_text()[-4000:]}")


@pytest.fixture(scope="module")
def installs():
    """The relay and the three installations, each its API and UI server, running; yields
    the API ports and the host map. An installation's public address reaches it only
    through the relay, over its gateway connection."""
    base = os.environ["DATABASE_URL"]
    root = Path(tempfile.mkdtemp(prefix="celerp-integration-"))
    ports = {name: _free_port() for name in ("relay", *INSTALLATIONS)}
    ui_ports = {name: _free_port() for name in INSTALLATIONS}
    public = {name: f"{name}.integration.test" for name in INSTALLATIONS}
    gateway_keys = {name: f"integration-{name}-" + secrets.token_hex(8) for name in INSTALLATIONS}
    hosts = {host: ports["relay"] for host in (*RELAY_HOSTS, *public.values())}
    started: list[tuple[int, subprocess.Popen, Path]] = []
    databases = [_database(base, f"int_{name}") for name in INSTALLATIONS]

    def spawn(port: int, args: list[str], log: Path, cwd: Path, env: dict) -> None:
        with log.open("wb") as out:
            process = subprocess.Popen([sys.executable, *args], stdout=out,
                                       stderr=subprocess.STDOUT, cwd=cwd, env=env)
        started.append((port, process, log))

    try:
        spawn(ports["relay"], [str(TESTS / "integration_relay.py"), str(ports["relay"])],
              root / "relay.log", REPO, {
                  "PATH": os.environ["PATH"], "PYTHONPATH": str(REPO),
                  "RELAY_PUBLIC_HOSTS": json.dumps({gateway_keys[n]: public[n] for n in INSTALLATIONS})})
        for name, url in zip(INSTALLATIONS, databases):
            asyncio.run(_recreate(url))
            home = root / name
            (home / "config" / "celerp").mkdir(parents=True)
            # The launcher derives ENABLED_MODULES from this list, so both say the same.
            (home / "config" / "celerp" / "config.toml").write_text(
                "[modules]\nenabled = [" + ", ".join(f'"{m}"' for m in MODULES.split(",")) + "]\n")
            env = {
                "PATH": os.environ["PATH"], "HOME": str(home), "PYTHONPATH": str(REPO),
                "DATABASE_URL": url, "ALLOW_INSECURE_JWT": "true", "ENABLED_MODULES": MODULES,
                "CELERP_DATA_DIR": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
                "CELERP_CONFIG": str(home / "config" / "celerp" / "config.toml"),
                "INTEGRATION_HOSTS": json.dumps(hosts),
            }
            launcher = str(TESTS / "integration_installation.py")
            spawn(ports[name], [launcher, "api", str(ports[name])], root / f"{name}.log", home, env | {
                "GATEWAY_TOKEN": gateway_keys[name],
                "GATEWAY_URL": f"ws://127.0.0.1:{ports['relay']}/ws/connect",
                "GATEWAY_HTTP_URL": "https://relay.celerp.com",
                "CELERP_PUBLIC_URL": f"https://{public[name]}",
                "CELERP_API_PORT": str(ports[name]), "CELERP_UI_PORT": str(ui_ports[name])})
            spawn(ui_ports[name], [launcher, "ui", str(ui_ports[name])], root / f"{name}-ui.log", home,
                  env | {"API_URL": f"http://127.0.0.1:{ports[name]}"})
        for port, process, log in started:
            _wait_healthy(port, process, log)
        yield {"ports": ports, "hosts": hosts, "root": root,
               "databases": dict(zip(INSTALLATIONS, databases))}
    finally:
        for _, process, _ in started:
            process.terminate()
        for _, process, _ in started:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for url in databases:
            asyncio.run(_drop(url))


def _client(installs, name: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{installs['ports'][name]}", timeout=120)


async def _register(api: httpx.AsyncClient, company: str, email: str) -> str:
    r = await api.post("/auth/register", json={
        "company_name": company, "email": email, "name": company + " Owner", "password": "integration-pw1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _switch(api: httpx.AsyncClient, tok: str, company_id: str) -> str:
    r = await api.post(f"/auth/switch-company/{company_id}", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _move_in_from_manager(api: httpx.AsyncClient, tok: str) -> tuple[str, str]:
    """The client moves its Manager books in as a new company, through the wizard's own
    requests, and finishes it; returns the company and a session on it."""
    r = await api.post("/migrations/scan", headers=auth(tok),
                       files=[("files", (MANAGER_FILE.name, MANAGER_FILE.read_bytes()))])
    assert r.status_code == 200, r.text
    scan = r.json()["scan_token"]
    r = await api.post("/migrations/scan/decisions", headers=auth(tok), json={
        "scan_token": scan, "mode": "full_history", "cutover_date": None, "mappings": {}, "prepared_by": None})
    assert r.status_code == 200, r.text
    r = await api.post("/migrations/start-from-scan", headers=auth(tok),
                       json={"scan_token": scan, "company_name": "Demo Trading"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    deadline = time.monotonic() + 300
    while (run := (await api.get(f"/migrations/{run_id}", headers=auth(tok))).json())["status"] not in (
            "ready_to_finalize", "failed", "interrupted", "cancelled"):
        assert time.monotonic() < deadline, run
        await asyncio.sleep(1)
    assert run["status"] == "ready_to_finalize", run
    r = await api.get(f"/migrations/{run_id}/posting-accounts", headers=auth(tok))
    assert r.status_code == 200, r.text
    r = await api.post(f"/migrations/{run_id}/finalize", headers=auth(tok),
                       json=posting_choices_from(r.json()["roles"]))
    assert r.status_code == 200, r.text
    return run["company_id"], await _switch(api, tok, run["company_id"])


@pytest.fixture(scope="module")
def client_company(installs):
    """The client's company, moved in from Manager on the client's installation."""
    async def run():
        async with _client(installs, "client") as api:
            tok = await _register(api, "Client Holding", "client-owner@example.test")
            company_id, tok = await _move_in_from_manager(api, tok)
            books = await _books(api, tok)
            # Real books to compare: postings, and money owed both ways.
            assert len(books["trial_balance"]) >= 3, books
            assert any(books["receivables"][0].values()) and any(books["payables"][0].values()), books
            return {"company_id": company_id, "tok": tok}
    return asyncio.run(run())


async def _follow_link(installs, link: str, dest: Path) -> None:
    """Fetch and decrypt a company copy the way the receiving installation's pages do."""
    sys.path.insert(0, str(TESTS))
    from integration_installation import route_hosts

    from celerp.services.company_handoff_transfer import download_company_handoff
    undo = route_hosts(installs["hosts"])
    try:
        await download_company_handoff(link, dest)
    finally:
        undo()


@pytest.fixture(scope="module")
def firm_copy(installs, client_company):
    """The client emails its company copy to the firm, and the firm opens the link on its
    own installation as a company of its own."""
    from celerp.services.company_handoff_links import parse_handoff_link

    async def run():
        async with _client(installs, "client") as client, _client(installs, "firm") as firm, \
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{installs['ports']['relay']}") as relay:
            key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
            r = await client.post("/company-backups/send-handoff", headers=auth(client_company["tok"]), json={
                "operation_id": str(uuid.uuid4()), "key": key, "firm_name": "Firm Practice",
                "recipient": "books@firm.example", "reply_to": "client-owner@example.test",
                "note": "Our books to date"})
            assert r.status_code == 200, r.text
            books_sent = await _books(client, client_company["tok"])
            [email] = [m for m in (await relay.get("/stand-in/outbox")).json()["items"]
                       if m["recipient"] == "books@firm.example"]

            dest = installs["root"] / "received.celerp-company"
            await _follow_link(installs, email["link"], dest)
            link = parse_handoff_link(email["link"])
            firm_tok = await _register(firm, "Firm Practice", "firm-owner@example.test")
            preview = await firm.post("/company-backups/read", headers=auth(firm_tok),
                                      files={"file": (dest.name, dest.read_bytes())},
                                      data={"mode": "new_company", "handoff_copy_id": str(link.copy_id),
                                            "handoff_proof": link.proof_hash})
            assert preview.status_code == 200, preview.text
            opened = await firm.post("/company-backups/restore", headers=auth(firm_tok), json={
                "upload_token": preview.json()["upload_token"], "mode": "new_company",
                "plan_fingerprint": preview.json().get("plan_fingerprint")})
            assert opened.status_code == 201, opened.text
            company_id = opened.json()["company_id"]
            return {"company_id": company_id, "tok": await _switch(firm, firm_tok, company_id),
                    "books_sent": books_sent, "copy_id": r.json()["id"]}
    return asyncio.run(run())


def test_company_copy_opens_on_a_fresh_installation(installs, client_company):
    """The client's company copy, downloaded and opened as the first company of an
    installation with no users, carries the same books."""
    async def run():
        async with _client(installs, "client") as client, _client(installs, "fresh") as fresh:
            r = await client.get("/company-backups/download", headers=auth(client_company["tok"]))
            assert r.status_code == 200, r.text
            before = await _books(client, client_company["tok"])
            read = await fresh.post("/company-backups/bootstrap/read",
                                    files={"file": ("demo.celerp-company", r.content)})
            assert read.status_code == 200, read.text
            opened = await fresh.post("/company-backups/bootstrap/restore", json={
                "upload_token": read.json()["upload_token"], "name": "First Owner",
                "email": "first@example.test", "password": "first-pw-1234"})
            assert opened.status_code == 201, opened.text
            login = await fresh.post("/auth/login", json={"email": "first@example.test",
                                                          "password": "first-pw-1234"})
            assert login.status_code == 200, login.text
            assert await _books(fresh, login.json()["access_token"]) == before
    asyncio.run(run())


def test_company_copy_link_opens_on_the_firms_installation(installs, client_company, firm_copy):
    """The emailed link is fetched from storage, decrypted, checked with the relay and
    opened on the firm's installation; the firm's company has the books that were sent,
    and the client's list of copies shows the link was opened."""
    async def run():
        async with _client(installs, "client") as client, _client(installs, "firm") as firm:
            assert await _books(firm, firm_copy["tok"]) == firm_copy["books_sent"]
            r = await client.get("/company-backups/handoffs", headers=auth(client_company["tok"]))
            assert r.status_code == 200, r.text
            [row] = [c for c in r.json()["items"] if c["id"] == firm_copy["copy_id"]]
            assert row["status"] == "sent" and row["opened"] is True, row
    asyncio.run(run())


async def _connect(client: httpx.AsyncClient, tok: str, link: str) -> None:
    """Connect the firm's company key as the client's owner does, once the client's
    relay session is live."""
    from celerp.services.peer_access import parse_pairing_link
    address, key = parse_pairing_link(link)
    deadline = time.monotonic() + 60
    while True:
        r = await client.post("/connectors/celerp/credentials", headers=auth(tok), json={
            "consumer_key": key, "consumer_secret": "", "store_url": address})
        assert r.status_code == 200, r.text
        if r.json()["ok"]:
            return
        # Only a relay session still opening is waited out; anything else is a failure.
        assert r.json()["error"] == "relay_error" and time.monotonic() < deadline, r.json()
        await asyncio.sleep(1)


async def _sync(installs, client: httpx.AsyncClient, tok: str, company_id: str) -> list[dict]:
    """Sync now, as the connector page runs it: the whole plan in the background. Returns
    each step's recorded run, read from the rows the connector page shows."""
    import asyncpg
    from celerp.connectors.base import SyncDirection
    from celerp.connectors.celerp import CelerpConnector
    from celerp.connectors.sync_runner import sync_plan
    r = await client.get("/connectors/", headers=auth(tok))
    assert r.status_code == 200, r.text
    steps = len(sync_plan(CelerpConnector(), SyncDirection(
        next(c["direction"] for c in r.json() if c["name"] == "celerp"))))
    conn = await asyncpg.connect(installs["databases"]["client"].replace("+asyncpg", ""))
    try:
        query = ("SELECT entity, status, created_count, updated_count, errors_json, attention_json, finished_at FROM sync_runs"
                 " WHERE company_id = $1 AND connector = 'celerp' AND id > $2 ORDER BY id")
        before = await conn.fetchval("SELECT coalesce(max(id), 0) FROM sync_runs") or 0
        r = await client.post("/connectors/celerp/sync-plan", headers=auth(tok))
        assert r.status_code == 200 and r.json()["ok"], r.text
        # A step whose prerequisite failed is not run, so a plan can end short; it has
        # ended once every run is finished and no step starts for a while after.
        deadline, settled, seen = time.monotonic() + 300, None, -1
        while True:
            rows = await conn.fetch(query, company_id, before)
            finished = rows and all(row["finished_at"] for row in rows)
            if finished and len(rows) == steps:
                break
            settled = settled if finished and len(rows) == seen else time.monotonic()
            seen = len(rows)
            if finished and time.monotonic() - settled > 20:
                break
            if time.monotonic() > deadline:
                pytest.fail("Sync now did not finish:\n" + "\n".join(map(str, map(dict, rows))))
            await asyncio.sleep(1)
    finally:
        await conn.close()
    if len(rows) != steps:
        pytest.fail(f"Sync now ran {len(rows)} of its {steps} steps:\n" + "\n".join(map(str, map(dict, rows))))
    return [{"entity": row["entity"], "ok": row["status"] == "success", "created": row["created_count"],
             "updated": row["updated_count"], "errors": json.loads(row["errors_json"] or "[]")} for row in rows]


async def _accounts(api: httpx.AsyncClient, tok: str) -> tuple[str, str]:
    """A bank account and an expense account from the chart the company moved in with."""
    banks = await api.get("/accounting/bank-accounts", headers=auth(tok))
    chart = await api.get("/accounting/chart", headers=auth(tok))
    assert banks.status_code == 200 and chart.status_code == 200, (banks.text, chart.text)
    expense = next(a["code"] for a in chart.json()["items"] if a["account_type"] == "expense" and a["is_active"])
    return banks.json()["items"][0]["chart_account_code"], expense


async def _journal(api: httpx.AsyncClient, tok: str, amount: float, memo: str) -> str:
    bank, expense = await _accounts(api, tok)
    r = await api.post("/accounting/journal-entries", headers=auth(tok), json={
        "ts": time.strftime("%Y-%m-%d"), "memo": memo, "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": expense, "debit": amount, "credit": 0.0},
                    {"account": bank, "debit": 0.0, "credit": amount}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _invoice(api: httpx.AsyncClient, tok: str, customer: str, total: float, paid: bool) -> None:
    """An invoice finalized and sent, and paid in full into the company's bank when *paid*."""
    r = await api.post("/docs", headers=auth(tok), json={
        "doc_type": "invoice", "contact_id": customer, "total": total,
        "line_items": [{"name": "Bookkeeping hours", "quantity": 1, "unit_price": total, "line_total": total}],
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code in (200, 201), r.text
    doc_id = r.json()["id"]
    for step, body in (("finalize", None), ("send", {"idempotency_key": uuid.uuid4().hex})):
        r = await api.post(f"/docs/{doc_id}/{step}", headers=auth(tok), json=body)
        assert r.status_code == 200, (step, r.text)
    if paid:
        bank, _ = await _accounts(api, tok)
        r = await api.post(f"/docs/{doc_id}/payment", headers=auth(tok), json={
            "amount": total, "payment_date": time.strftime("%Y-%m-%d"), "bank_account": bank,
            "idempotency_key": uuid.uuid4().hex})
        assert r.status_code == 200, r.text


def test_company_key_keeps_the_firms_copy_in_step_both_ways(installs, client_company, firm_copy):
    """The firm gives the client a company key; with it the client's new customer,
    invoices, payment and journal reach the firm's installation, the firm's journal
    reaches the client's, both companies' books agree, and a second sync changes nothing."""
    from celerp.services.peer_access import pairing_link

    async def run():
        async with _client(installs, "client") as client, _client(installs, "firm") as firm:
            tok, firm_tok = client_company["tok"], firm_copy["tok"]
            granted = await firm.post("/company-peers/grants", headers=auth(firm_tok), json={
                "source_company_id": client_company["company_id"], "label": "Demo Trading", "firm": "Firm Practice",
                "scopes": ["contacts", "products", "documents", "inventory", "journals"]})
            assert granted.status_code == 201, granted.text
            await _connect(client, tok, pairing_link(FIRM_ADDRESS, granted.json()["credential"]))

            customer = await client.post("/crm/contacts", headers=auth(tok), json={
                "name": "Harbour Cafe", "contact_type": "customer", "email": "orders@harbour.example"})
            assert customer.status_code in (200, 201), customer.text
            await _invoice(client, tok, customer.json()["id"], 300.0, paid=True)
            await _invoice(client, tok, customer.json()["id"], 120.0, paid=False)
            await _journal(client, tok, 40.0, "Petty cash rent")
            await _journal(firm, firm_tok, 125.0, "Accrue audit fee")

            results = await _sync(installs, client, tok, client_company["company_id"])
            assert [r for r in results if not r["ok"] or r["errors"]] == [], results
            assert await _books(firm, firm_tok) == await _books(client, tok)

            again = await _sync(installs, client, tok, client_company["company_id"])
            assert [r for r in again if r["created"] or r["updated"] or r["errors"]] == [], again
            assert await _books(firm, firm_tok) == await _books(client, tok)
    asyncio.run(run())


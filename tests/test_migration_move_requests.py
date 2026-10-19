# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A firm asks Celerp's team to move a Xero or QuickBooks Online client: the export set
open on the upload page goes to the cloud, the team works it and returns a company copy,
and the firm's list shows each request with its status.

These tests stand in for the cloud with its move-request route contract."""

from __future__ import annotations

import uuid

import httpx
import pytest

from company_backup_support import company, member, owner, token
from migration_support import auth, multipart, real_client, real_engine  # noqa: F401
from test_export_set_adapters import _without, _xero

BASE = "/migrations/move-requests"


class FakeRelay:
    """The cloud's move-request routes, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.created: list[dict] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.submits: list[str] = []
        self.fail_upload: str = ""
        self.refuse: httpx.Response | None = None
        self.no_account = False

    async def request(self, method, path, *, json=None, params=None, content=None, total_s=35):
        if self.no_account:
            return None
        if self.refuse is not None:
            return self.refuse
        if (method, path) == ("POST", "/move-requests"):
            self.created.append(json)
            row = next((r for r in self.rows.values() if r["operation_id"] == json["operation_id"]), None)
            if row is None:
                row = {"id": str(uuid.uuid4()), "status": "uploading", "note": "", "files": [], "stored": True,
                       "created_at": f"2026-10-17T08:{len(self.rows):02d}:00+00:00", "submitted_at": None,
                       **{k: json[k] for k in ("operation_id", "firm_name", "client_name", "source")}}
                self.rows[row["id"]] = row
            return httpx.Response(201, json={"id": row["id"], "status": row["status"]})
        if (method, path) == ("GET", "/move-requests"):
            return httpx.Response(200, json={"items": list(self.rows.values())[::-1]})
        parts = path.strip("/").split("/")
        row = self.rows.get(parts[1]) if len(parts) > 1 else None
        if row is None:
            return httpx.Response(404, json={"detail": "Move request not found"})
        if parts[2:] == ["files"]:
            if params["name"] == self.fail_upload:
                self.fail_upload = ""
                return httpx.Response(502, json={"detail": "Bad gateway"})
            self.uploads.append((row["id"], params["name"], content))
            row["files"].insert(0, {"id": uuid.uuid4().hex, "name": params["name"], "size": len(content),
                                    "received_at": "2026-10-17T08:30:00+00:00"})
            return httpx.Response(201, json=row["files"][0])
        if parts[2:] == ["submit"]:
            self.submits.append(row["id"])
            row["status"] = "received" if row["status"] == "uploading" else row["status"]
            return httpx.Response(200, json={"id": row["id"], "status": row["status"]})
        if parts[2:] == ["withdraw"]:
            row["status"], row["stored"] = "withdrawn", False
            return httpx.Response(200, json={"status": "withdrawn"})
        return httpx.Response(405)


@pytest.fixture
def relay(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.exports import QuickBooksOnlineAdapter, XeroAdapter
    from celerp.importers.adapters.manager_io.adapter import ManagerIOAdapter
    from celerp.services import cloud_entitlement

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(registry, "_ADAPTERS", (ManagerIOAdapter(), XeroAdapter(), QuickBooksOnlineAdapter()))
    fake = FakeRelay()
    monkeypatch.setattr(cloud_entitlement, "authenticated_request", fake.request)
    return fake


async def _owner_headers(engine) -> dict:
    user = await owner(engine)
    return auth(await token(engine, user, await company(engine, user, "Firm books", "move-request-firm")))


async def _draft(client, headers, files) -> dict:
    """The export set as the upload page keeps it: checked, held as a draft."""
    r = await client.post("/migrations/scan", headers=headers, data={"source": "xero", "check": "1"},
                          files=multipart(*files))
    assert r.status_code == 200 and "draft_token" in r.json(), r.text
    return r.json()


def _ask(draft_token: str, **overrides) -> dict:
    return {"operation_id": str(uuid.uuid4()), "firm_name": "Example Accounting", "client_name": "Client Ltd",
            "reply_to": "firm@example.com", "draft_token": draft_token, **overrides}


@pytest.mark.asyncio
async def test_a_firm_sends_the_open_export_set_to_the_team(real_engine, real_client, relay):
    headers = await _owner_headers(real_engine)
    assert (await real_client.get(BASE, headers=headers)).json() == {"items": []}
    files = dict(_xero())
    draft = await _draft(real_client, headers, files.items())
    assert draft["checklist"]["complete"] is True

    body = _ask(draft["draft_token"])
    sent = await real_client.post(BASE, json=body, headers=headers)
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["id"]
    assert sent.json()["status"] == "received"
    # The cloud gets the request with its source, never the draft token, then every file as uploaded.
    assert relay.created == [{k: body[k] for k in ("operation_id", "firm_name", "client_name", "reply_to")}
                             | {"source": "xero"}]
    assert {(name, data) for _, name, data in relay.uploads} == set(files.items())
    assert relay.submits == [request_id]

    # A retry of the same send uploads nothing again.
    again = await real_client.post(BASE, json=body, headers=headers)
    assert again.status_code == 201 and again.json()["id"] == request_id
    assert len(relay.uploads) == len(files)

    listed = (await real_client.get(BASE, headers=headers)).json()["items"]
    assert [(r["id"], r["status"]) for r in listed] == [(request_id, "received")]

    out = await real_client.post(f"{BASE}/{request_id}/withdraw", headers=headers)
    assert out.status_code == 200 and out.json()["status"] == "withdrawn"
    assert (await real_client.get(BASE, headers=headers)).json()["items"][0]["status"] == "withdrawn"


@pytest.mark.asyncio
async def test_an_interrupted_send_resumes_with_the_files_still_to_go(real_engine, real_client, relay):
    headers = await _owner_headers(real_engine)
    files = dict(_xero())
    draft = await _draft(real_client, headers, files.items())
    body = _ask(draft["draft_token"])
    relay.fail_upload = "General Ledger.txt"
    failed = await real_client.post(BASE, json=body, headers=headers)
    assert failed.status_code == 502 and "Retry" in failed.json()["detail"]
    assert relay.submits == []
    resumed = await real_client.post(BASE, json=body, headers=headers)
    assert resumed.status_code == 201, resumed.text
    names = [name for _, name, _ in relay.uploads]
    assert sorted(names) == sorted(files) and len(names) == len(set(names))
    assert relay.submits == [resumed.json()["id"]]


@pytest.mark.asyncio
async def test_a_send_is_refused_with_the_reason(real_engine, real_client, relay):
    user = await owner(real_engine)
    firm = await company(real_engine, user, "Firm books", "move-request-refusals")
    headers = auth(await token(real_engine, user, firm))

    short = await _draft(real_client, headers, _without("General Ledger.txt"))
    incomplete = await real_client.post(BASE, json=_ask(short["draft_token"]), headers=headers)
    assert incomplete.status_code == 422
    assert "General Ledger is missing" in incomplete.json()["detail"]
    assert "before asking us to move this client" in incomplete.json()["detail"]
    gone = await real_client.post(BASE, json=_ask("x" * 43), headers=headers)
    assert gone.status_code == 410 and "Upload" in gone.json()["detail"]
    none = await real_client.post(BASE, json=_ask(""), headers=headers)
    assert none.status_code == 422 and "export files" in none.json()["detail"]
    for bad in ({"client_name": "  "}, {"firm_name": ""}, {"reply_to": "not an email"},
                {"operation_id": "not-a-uuid"}):
        assert (await real_client.post(BASE, json=_ask(short["draft_token"], **bad),
                                       headers=headers)).status_code == 422, bad
    assert relay.created == []

    whole = await _draft(real_client, headers, _xero())
    relay.no_account = True
    needed = await real_client.post(BASE, json=_ask(whole["draft_token"]), headers=headers)
    assert needed.status_code == 428 and "free Celerp account" in needed.json()["detail"]
    relay.no_account = False
    relay.refuse = httpx.Response(403, json={"detail": "Use your verified account email so we can reply to you"})
    mismatch = await real_client.post(BASE, json=_ask(whole["draft_token"]), headers=headers)
    assert mismatch.status_code == 403 and "verified account email" in mismatch.json()["detail"]
    relay.refuse = None

    # Another user's draft is not theirs to send, and only the owner asks at all.
    staff = await owner(real_engine, "staff@example.com", "Staff")
    await member(real_engine, staff, firm, "admin")
    others = auth(await token(real_engine, staff, firm, "admin"))
    some_id = str(uuid.uuid4())
    for method, path in (("get", BASE), ("post", BASE), ("post", f"{BASE}/{some_id}/withdraw")):
        kw = {"json": _ask(whole["draft_token"])} if method == "post" else {}
        r = await getattr(real_client, method)(path, headers=others, **kw)
        assert r.status_code == 403, (method, path)
    assert (await real_client.get(BASE)).status_code == 401
    assert relay.created == []

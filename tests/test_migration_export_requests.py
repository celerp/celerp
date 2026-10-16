# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A firm emails a client the export checklist of a migration and brings the files the
client uploads into the migration upload.

The client's browser encrypts each file with a key that travels only in the emailed link
and in this installation, so the relay stores files it cannot read. These tests stand in
for the relay with its route contract and for object storage with a mock transport."""

from __future__ import annotations

import hashlib
import os
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from company_backup_support import company, member, owner, token
from migration_support import auth, multipart, real_client, real_engine  # noqa: F401
from test_export_set_adapters import _without, _xero

MAGIC = b"CELERP-COPY\x01"
BASE = "/migrations/export-requests"


def _encrypt(secret: bytes, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(secret).encrypt(nonce, data, MAGIC)


class FakeRelay:
    """The relay's export-request routes, in memory: it keeps the key's proof, never the key."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.posted: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.refuse: httpx.Response | None = None
        self.no_account = False
        self.host = "sfo2.digitaloceanspaces.com"
        self.fetch_status = 200

    def client_uploads(self, request_id: str, secret: bytes, name: str, data: bytes) -> None:
        file_id = str(uuid.uuid4())
        self.objects[f"/backup/export-requests/{uuid.UUID(request_id).hex}/{file_id}"] = _encrypt(secret, data)
        self.rows[request_id]["files"].insert(0, {"id": file_id, "name": name, "size": len(data),
                                                  "received_at": "2026-10-17T09:00:00+00:00"})

    async def request(self, method, path, *, json=None, params=None, total_s=35):
        if self.no_account:
            return None
        if self.refuse is not None:
            return self.refuse
        if (method, path) == ("POST", "/export-requests"):
            self.posted.append(json)
            same = next((r for r in self.rows.values() if r["operation_id"] == json["operation_id"]), None)
            if same is None:
                same = {"id": str(uuid.uuid4()), "link_id": str(uuid.uuid4()), "status": "sent", "files": [],
                        "proof": hashlib.sha256(_decode(json["key"])).hexdigest(), "stored": True,
                        "created_at": "2026-10-17T08:00:00+00:00", "expires_at": "2026-10-31T08:00:00+00:00",
                        **{k: json[k] for k in ("operation_id", "firm_name", "client_name", "source", "items")}}
                self.rows[same["id"]] = same
            return httpx.Response(201, json={k: same[k] for k in ("id", "link_id", "status")})
        if (method, path) == ("GET", "/export-requests"):
            items = [{k: v for k, v in r.items() if k != "proof"} for r in self.rows.values()]
            return httpx.Response(200, json={"items": items[::-1]})
        parts = path.strip("/").split("/")
        row = self.rows.get(parts[1]) if len(parts) > 1 else None
        if row is None:
            return httpx.Response(404, json={"detail": "Export request not found"})
        if parts[2:] == ["revoke"]:
            row["status"], row["stored"] = "revoked", False
            return httpx.Response(200, json={"status": "revoked"})
        if parts[2] == "files" and parts[4:] == ["fetch"]:
            if self.fetch_status != 200:
                return httpx.Response(self.fetch_status, json={"detail": "This request is closed"})
            if parts[3] not in {f["id"] for f in row["files"]}:
                return httpx.Response(404, json={"detail": "File not found"})
            url = (f"https://{self.host}/backup/export-requests/{uuid.UUID(row['id']).hex}/{parts[3]}"
                   "?X-Amz-Signature=synthetic")
            return httpx.Response(200, json={"download_url": url, "expires_in": 600})
        return httpx.Response(405)

    def storage(self, request: httpx.Request) -> httpx.Response:
        self.downloads.append(request.url.path)
        data = self.objects.get(request.url.path)
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)


def _decode(key: str) -> bytes:
    from celerp.services.company_handoff_sender import decode_key
    return decode_key(key)


@pytest.fixture
def relay(monkeypatch, tmp_path):
    from celerp.config import settings
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.exports import QuickBooksOnlineAdapter, XeroAdapter
    from celerp.importers.adapters.manager_io.adapter import ManagerIOAdapter
    from celerp.services import cloud_entitlement, migration_export_requests

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(registry, "_ADAPTERS", (ManagerIOAdapter(), XeroAdapter(), QuickBooksOnlineAdapter()))
    fake = FakeRelay()
    monkeypatch.setattr(cloud_entitlement, "authenticated_request", fake.request)
    real = httpx.AsyncClient
    monkeypatch.setattr(migration_export_requests.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(fake.storage), **kw))
    return fake


async def _owner_token(engine) -> str:
    user = await owner(engine)
    return await token(engine, user, await company(engine, user, "Firm books", "export-request-firm"))


def _ask(**overrides) -> dict:
    return {"operation_id": str(uuid.uuid4()), "firm_name": "Example Accounting", "client_name": "Client Ltd",
            "recipient": "client@example.com", "reply_to": "firm@example.com", "source": "xero", **overrides}


@pytest.mark.asyncio
async def test_a_client_uploads_the_checklist_and_the_files_join_the_migration_upload(
        real_engine, real_client, relay):
    from celerp.importers.adapters.exports import XeroAdapter
    headers = auth(await _owner_token(real_engine))
    assert (await real_client.get(BASE, headers=headers)).json() == {"items": []}

    body = _ask()
    sent = await real_client.post(BASE, json=body, headers=headers)
    assert sent.status_code == 201, sent.text
    request_id = sent.json()["id"]
    assert "key" not in sent.text
    posted = relay.posted[0]
    # The email lists every file the source's checklist asks for, with its menu and dates.
    wanted = [i for i in XeroAdapter().checklist().view()["items"] if i["status"] != "not_needed"]
    assert posted["items"] == [{"label": i["label"], "menu": i["menu"],
                                "range": "" if i["range"] == "--" else i["range"]} for i in wanted]
    assert {k: posted[k] for k in body} == body
    secret = _decode(posted["key"])

    # A retry of the same send uses the same key, so the link already emailed still opens.
    again = await real_client.post(BASE, json=body, headers=headers)
    assert again.json()["id"] == request_id and relay.posted[1]["key"] == posted["key"]

    files = dict(_without("General Ledger.txt"))
    for name, data in files.items():
        relay.client_uploads(request_id, secret, name, data)
    listed = (await real_client.get(BASE, headers=headers)).json()["items"]
    assert listed[0]["id"] == request_id and listed[0]["readable"] is True
    assert {f["name"] for f in listed[0]["files"]} == set(files)

    added = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert added.status_code == 200, added.text
    draft = added.json()
    assert {u["name"] for u in draft["checklist"]["uploads"]} == set(files)
    assert "General Ledger is missing" in " ".join(draft["checklist"]["problems"])
    assert len(relay.downloads) == len(files)

    # The rest arrives through the upload page: the draft becomes the scan.
    ledger = dict(_xero())["General Ledger.txt"]
    scan = await real_client.post("/migrations/scan", files=multipart(("General Ledger.txt", ledger)),
                                  data={"source": "xero", "draft": draft["draft_token"]}, headers=headers)
    assert scan.status_code == 200 and "scan_token" in scan.json(), scan.text

    revoked = await real_client.post(f"{BASE}/{request_id}/revoke", headers=headers)
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    listed = (await real_client.get(BASE, headers=headers)).json()["items"]
    assert listed[0]["status"] == "revoked" and listed[0]["readable"] is False   # the key is forgotten


@pytest.mark.asyncio
async def test_added_files_join_the_draft_already_open(real_engine, real_client, relay):
    headers = auth(await _owner_token(real_engine))
    files = dict(_xero())
    first = await real_client.post("/migrations/scan", headers=headers, data={"source": "xero", "check": "1"},
                                   files=multipart(*((n, d) for n, d in files.items()
                                                     if n != "General Ledger.txt")))
    draft = first.json()["draft_token"]
    request_id = (await real_client.post(BASE, json=_ask(), headers=headers)).json()["id"]
    relay.client_uploads(request_id, _decode(relay.posted[0]["key"]), "General Ledger.txt",
                         files["General Ledger.txt"])
    added = await real_client.post(f"{BASE}/{request_id}/add", json={"draft_token": draft}, headers=headers)
    assert added.status_code == 200, added.text
    assert "scan_token" in added.json()     # the set is whole: it is read like any upload


@pytest.mark.asyncio
async def test_a_send_is_refused_with_the_reason(real_engine, real_client, relay):
    user = await owner(real_engine)
    firm = await company(real_engine, user, "Firm books", "export-request-refusals")
    headers = auth(await token(real_engine, user, firm))

    manager = await real_client.post(BASE, json=_ask(source="manager_io"), headers=headers)
    assert manager.status_code == 422 and "has no list of files to export" in manager.json()["detail"]
    for bad in ({"recipient": "not an email"}, {"client_name": "  "}, {"firm_name": ""},
                {"operation_id": "not-a-uuid"}):
        assert (await real_client.post(BASE, json=_ask(**bad), headers=headers)).status_code == 422, bad
    assert relay.posted == []

    relay.no_account = True
    needed = await real_client.post(BASE, json=_ask(), headers=headers)
    assert needed.status_code == 428 and "free Celerp account" in needed.json()["detail"]
    relay.no_account = False
    relay.refuse = httpx.Response(403, json={"detail": "Use your verified account email as the reply-to address"})
    mismatch = await real_client.post(BASE, json=_ask(), headers=headers)
    assert mismatch.status_code == 403 and "verified account email" in mismatch.json()["detail"]
    relay.refuse = httpx.Response(502, json={"detail": "The email could not be sent. Try again"})
    unsent = await real_client.post(BASE, json=_ask(), headers=headers)
    assert unsent.status_code == 502 and "Retry" in unsent.json()["detail"]

    staff = await owner(real_engine, "staff@example.com", "Staff")
    await member(real_engine, staff, firm, "admin")
    others = auth(await token(real_engine, staff, firm, "admin"))
    some_id = str(uuid.uuid4())
    for method, path in (("get", BASE), ("post", BASE), ("post", f"{BASE}/{some_id}/add"),
                         ("post", f"{BASE}/{some_id}/revoke")):
        kw = {"json": _ask()} if method == "post" else {}
        r = await getattr(real_client, method)(path, headers=others, **kw)
        assert r.status_code == 403, (method, path)
    assert (await real_client.get(BASE)).status_code == 401


@pytest.mark.asyncio
async def test_files_that_cannot_be_read_are_never_added(real_engine, real_client, relay, tmp_path):
    from celerp.services import company_backup_files as files
    headers = auth(await _owner_token(real_engine))
    request_id = (await real_client.post(BASE, json=_ask(), headers=headers)).json()["id"]
    secret = _decode(relay.posted[0]["key"])

    empty = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert empty.status_code == 422 and "No files have arrived" in empty.json()["detail"]

    relay.client_uploads(request_id, os.urandom(32), "Contacts.csv", b"*ContactName\r\n")   # another key
    wrong = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert wrong.status_code == 422 and "Contacts.csv" in wrong.json()["detail"]
    scans = tmp_path / "migration_scans"
    assert not scans.exists() or not any(scans.iterdir())      # no draft was made of an unreadable file

    relay.rows[request_id]["files"].clear()
    relay.client_uploads(request_id, secret, "Contacts.csv", b"*ContactName\r\n")
    relay.host = "attacker.example.com"
    elsewhere = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert elsewhere.status_code == 502 and relay.downloads == []
    relay.host = "sfo2.digitaloceanspaces.com"

    relay.fetch_status = 410
    closed = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert closed.status_code == 410 and "closed" in closed.json()["detail"]
    relay.fetch_status = 200

    # An installation that does not hold the key cannot read the files and says so.
    files.forget_export_request_key(relay.rows[request_id]["operation_id"])
    keyless = await real_client.post(f"{BASE}/{request_id}/add", json={}, headers=headers)
    assert keyless.status_code == 409 and "does not hold the key" in keyless.json()["detail"]
    listed = (await real_client.get(BASE, headers=headers)).json()["items"]
    assert listed[0]["readable"] is False
    unknown = await real_client.post(f"{BASE}/{uuid.uuid4()}/add", json={}, headers=headers)
    assert unknown.status_code == 404


def test_the_key_is_kept_privately_and_swept_of_partial_writes(tmp_path, monkeypatch):
    from celerp.config import settings
    from celerp.services import company_backup_files as files
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    op = str(uuid.uuid4())
    assert files.export_request_key(op) is None
    files.remember_export_request_key(op, "k" * 43)
    assert files.export_request_key(op) == "k" * 43
    folder = files.export_requests_dir()
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    stored = next(folder.glob("*.json"))
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        files.remember_export_request_key("../escape", "k" * 43)
    (folder / "left.json.partial").write_text("{}")
    (folder / "download-abc").mkdir()
    files.sweep_transient_files()
    assert sorted(p.name for p in folder.iterdir()) == [stored.name]
    files.forget_export_request_key(op)
    assert files.export_request_key(op) is None

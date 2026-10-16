# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Real encrypted streaming and ambiguous retry semantics for company handoffs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import httpx
import pytest

from celerp.services import company_handoff_sender as sender
from celerp.services.company_handoff_crypto import decrypt_file
from celerp.services.company_handoff_links import parse_handoff_link


def _run(coro):
    return asyncio.run(coro)


def _private_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(sender.files, "stage_path", lambda owner, token: tmp_path / (owner + token + ".upload"))
    monkeypatch.setattr(sender.files, "export_path", lambda company: tmp_path / (uuid.uuid4().hex + ".export"))


def _fake_cloud(monkeypatch, copy_id, *, state=None, destination=None):
    calls = []
    url = destination or (f"https://sfo2.digitaloceanspaces.com/backup/handoffs/"
                          f"{copy_id.hex}.upload?X-Amz-Signature=synthetic")
    lookup = {"value": state}

    async def request(method, path, *, json=None, params=None, total_s):
        calls.append((method, path, json, params))
        if method == "GET":
            return httpx.Response(200, json={"items": [lookup["value"]] if lookup["value"] else []})
        if path == "/handoffs":
            return httpx.Response(201, json={"id": str(copy_id), "status": "pending",
                                             "upload_url": url})
        return httpx.Response(200, json={"sent": True, "id": str(copy_id)})

    monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", request)
    return calls, lookup


def _send(plain, company_id, operation_id, secret, **other):
    async def export(destination):
        destination.write_bytes(plain.read_bytes())
    return sender.send_company_backup(
        export=other.pop("export", export), company_id=company_id,
        operation_id=operation_id, secret=secret, firm_name="Example Accounting",
        recipient="client@example.com", reply_to="firm@example.com", note="Your books", **other)


def test_encrypt_upload_and_cleanup(tmp_path, monkeypatch):
    _private_staging(monkeypatch, tmp_path)
    source, restored = tmp_path / "source", tmp_path / "restored"
    source.write_bytes(b"synthetic financial records" * 700)
    copy_id, op_id, company_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = bytes(range(32))
    sent = []
    def storage(req):
        sent.append((req.headers, req.content))
        return httpx.Response(200)
    real = httpx.AsyncClient
    monkeypatch.setattr(sender.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(storage), **kw))
    calls, _ = _fake_cloud(monkeypatch, copy_id)
    result = _run(_send(source, company_id, op_id, secret))
    assert len(sent) == 1 and sent[0][0]["content-length"] == str(len(sent[0][1]))
    cipher = tmp_path / "cipher"
    cipher.write_bytes(sent[0][1])
    decrypt_file(cipher, restored, secret)
    assert restored.read_bytes() == source.read_bytes()
    assert calls[0][0] == "GET" and calls[1][1] == "/handoffs"
    assert calls[1][2]["proof_hash"] == hashlib.sha256(secret).hexdigest()
    assert calls[1][2]["backup_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert secret.hex() not in json.dumps(calls[1][2])
    assert parse_handoff_link(result["link"]).copy_id == copy_id
    assert calls[2][2]["link"] == result["link"]
    assert not list(tmp_path.glob("*.upload"))
    assert not list(tmp_path.glob("*.export"))


def test_lost_upload_retry_uses_original_encrypted_snapshot(tmp_path, monkeypatch):
    _private_staging(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"original books" * 300)
    copy_id, op_id, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = bytes(range(32))
    calls, lookup = _fake_cloud(monkeypatch, copy_id)
    attempts = []
    real = httpx.AsyncClient

    def storage(req):
        attempts.append(req.content)
        return httpx.Response(502 if len(attempts) == 1 else 200)
    monkeypatch.setattr(sender.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(storage), **kw))
    with pytest.raises(sender.HandoffSendError, match="Retry"):
        _run(_send(source, cid, op_id, secret))
    assert len(list(tmp_path.glob("*.upload"))) == 1
    source.write_bytes(b"NEW AND DIFFERENT BOOKS")
    lookup["value"] = {"id": str(copy_id), "operation_id": str(op_id),
                       "company_id": str(cid), "firm_name": "Example Accounting", "status": "pending"}
    async def no_export(destination):
        pytest.fail("retry must not export a different backup")
    _run(_send(source, cid, op_id, secret, export=no_export))
    assert attempts[0] == attempts[1]
    assert not list(tmp_path.glob("*.upload"))


def test_known_success_replays_without_export_or_put(tmp_path, monkeypatch):
    _private_staging(monkeypatch, tmp_path)
    cid, op, copy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    secret = bytes(range(32))
    state = {"id": str(copy_id), "company_id": str(cid), "operation_id": str(op),
             "firm_name": "Example Accounting", "status": "sent"}
    calls, _ = _fake_cloud(monkeypatch, copy_id, state=state)
    monkeypatch.setattr(sender.httpx, "AsyncClient", lambda **kw: pytest.fail("no upload"))
    async def no_export(destination):
        pytest.fail("no export")
    result = _run(_send(tmp_path / "nonexistent", cid, op, secret, export=no_export))
    assert result["id"] == str(copy_id)
    assert [x[0] for x in calls] == ["GET", "POST"]


@pytest.mark.parametrize("bad", [
    "https://localhost/handoffs/example.upload",
    "https://127.0.0.1/private",
    "https://sfo2.digitaloceanspaces.com.attacker.example/x",
    "file:///etc/passwd",
])
def test_untrusted_upload_url_never_used(tmp_path, monkeypatch, bad):
    _private_staging(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"safe backup")
    copy_id = uuid.uuid4()
    calls, _ = _fake_cloud(monkeypatch, copy_id, destination=bad)
    monkeypatch.setattr(sender.httpx, "AsyncClient", lambda **kw: pytest.fail("no PUT"))
    with pytest.raises(sender.HandoffSendError, match="destination"):
        _run(_send(source, uuid.uuid4(), uuid.uuid4(), bytes(range(32))))
    assert len(calls) == 2  # lookup + creation, never mail
    assert len(list(tmp_path.glob("*.upload"))) == 1  # retry retains ciphertext


def test_send_link_without_connect_asks_for_a_free_account(tmp_path, monkeypatch):
    """No Celerp account on this installation, or one the relay has not verified: the
    send stops before anything is staged and asks for a free account, never Connect."""
    _private_staging(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.write_bytes(b"books")

    async def no_account(method, path, **kw):
        return None
    monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", no_account)
    with pytest.raises(sender.HandoffAccountNeeded) as missing:
        _run(_send(plain, uuid.uuid4(), uuid.uuid4(), bytes(32)))
    assert "free Celerp account" in str(missing.value) and "Connect" not in str(missing.value)

    async def unverified(method, path, **kw):
        if method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(403, json={"detail": "Verify your Celerp account before sending a company copy"})
    monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", unverified)
    with pytest.raises(sender.HandoffAccountNeeded):
        _run(_send(plain, uuid.uuid4(), uuid.uuid4(), bytes(32)))


def test_relay_refusal_reason_reaches_the_sender(tmp_path, monkeypatch):
    """A limit the relay names (50 active copies, daily email limit) is shown as is, with
    its way out, instead of a generic retry message."""
    _private_staging(monkeypatch, tmp_path)
    plain = tmp_path / "plain"
    plain.write_bytes(b"books")
    limit = "50 active copies is the limit. Revoke an old copy or download one."

    async def full(method, path, **kw):
        if method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(409, json={"detail": limit})
    monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", full)
    with pytest.raises(sender.HandoffSendError) as refused:
        _run(_send(plain, uuid.uuid4(), uuid.uuid4(), bytes(32)))
    assert str(refused.value) == limit

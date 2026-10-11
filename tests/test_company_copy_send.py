# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A sent company copy is encrypted and contains exactly the selected company's backup."""
import hashlib
import uuid

import httpx
import pytest

from celerp.services import company_copy
from celerp.services.company_copy_crypto import decrypt_copy, secret_from_fragment


@pytest.mark.asyncio
async def test_company_copy_sent_with_local_encryption(tmp_path, monkeypatch):
    company_id = uuid.uuid4()
    original = b"test-company-backup-contents" * 64
    unencrypted = tmp_path / "company.celerp-company"
    calls = []
    ciphertext = []

    monkeypatch.setattr(company_copy.files, "export_path", lambda cid: unencrypted)

    async def fake_export(cid, dest, **kwargs):
        assert cid == company_id
        dest.write_bytes(original)

    monkeypatch.setattr(company_copy.backup, "export_company_snapshot", fake_export)

    async def fake_upload(url, headers, path):
        assert url == "https://uploads.example.net/opaque-object"
        ciphertext.append(path.read_bytes())
        assert headers["Content-Length"] == str(len(ciphertext[0]))

    monkeypatch.setattr(company_copy, "_upload", fake_upload)

    handoff_id = str(uuid.uuid4())
    async def relay(method, path, **kwargs):
        calls.append((method, path, kwargs))
        req = httpx.Request(method, "https://relay.celerp.com" + path)
        if path == "/handoffs":
            return httpx.Response(201, request=req, json={
                "id": handoff_id, "upload_url": "https://uploads.example.net/opaque-object",
                "upload_headers": {"Content-Length": str(len(original) + 40)},
            })
        if path == f"/handoffs/{handoff_id}/send":
            return httpx.Response(200, request=req, json={"sent": True})
        raise AssertionError(path)

    monkeypatch.setattr(company_copy.cloud_entitlement, "authenticated_request", relay)
    sent = await company_copy.send_company_copy(
        company_id, recipient="customer@example.com", note="Books for you",
        firm_name="Accounting Firm", reply_to="firm@example.com",
    )

    assert sent["id"] == handoff_id
    fragment = sent["link"].split("#", 1)[1]
    assert len(ciphertext) == 1 and original not in ciphertext[0]
    assert calls[0][2]["json"]["company_id"] == str(company_id)
    assert calls[0][2]["json"]["proof_hash"] == hashlib.sha256(secret_from_fragment(fragment)).hexdigest()
    assert calls[1][2]["json"]["link"] == sent["link"]
    assert calls[1][2]["json"]["note"] == "Books for you"
    encrypted = tmp_path / "test.enc"
    encrypted.write_bytes(ciphertext[0])
    restored = tmp_path / "restored"
    decrypt_copy(encrypted, restored, secret_from_fragment(fragment))
    assert restored.read_bytes() == original
    assert not unencrypted.exists() and not (tmp_path / "company.celerp-company.encrypted").exists()


@pytest.mark.asyncio
async def test_not_sent_on_cloud_failure(tmp_path, monkeypatch):
    original = tmp_path / "company.celerp-company"
    monkeypatch.setattr(company_copy.files, "export_path", lambda cid: original)
    async def fake_export(cid, dest, **kwargs):
        dest.write_bytes(b"sample")
    monkeypatch.setattr(company_copy.backup, "export_company_snapshot", fake_export)
    async def relay(*args, **kwargs):
        return None
    monkeypatch.setattr(company_copy.cloud_entitlement, "authenticated_request", relay)
    with pytest.raises(Exception):
        await company_copy.send_company_copy(
            uuid.uuid4(), recipient="person@example.com", reply_to="firm@example.com",
            firm_name="Firm", note="",
        )
    assert not original.exists()

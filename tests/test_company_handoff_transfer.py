# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""An untrusted handoff cannot introduce a second restore path or partial file."""
import asyncio
import json
import uuid

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from celerp.services.company_handoff_crypto import encrypt_file
from celerp.services.company_handoff_links import handoff_page_link
from celerp.services import company_handoff_transfer as transfer


def _run_with_remote(monkeypatch, copy_id, content: bytes, *, grant_url=None, status=200,
                     content_length=None, download_status=200):
    url = grant_url or f"https://sfo2.digitaloceanspaces.com/example-copies/handoffs/{copy_id.hex}.bin?sig=synthetic"
    requests = []

    def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(status, json={"download_url": url})
        assert request.method == "GET" and str(request.url) == url
        headers = {} if content_length is None else {"content-length": str(content_length)}
        return httpx.Response(download_status, content=content, headers=headers)

    real = httpx.AsyncClient
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    return requests


def test_authenticated_download_publishes_original_backup(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    original = tmp_path / "source"
    original.write_bytes(b"synthetic company backup" * 5000)
    encrypted = tmp_path / "encrypted"
    encrypt_file(original, encrypted, secret)
    calls = _run_with_remote(monkeypatch, hid, encrypted.read_bytes())
    dest = tmp_path / "restored"
    asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), dest))
    assert dest.read_bytes() == original.read_bytes()
    assert [c.method for c in calls] == ["POST", "GET"]
    assert 'proof' in calls[0].content.decode()
    assert secret.hex() not in calls[0].content.decode()
    assert not list(tmp_path.glob(".celerp-handoff-*"))


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/handoffs/123.bin",
    "https://localhost/internal",
    "https://sfo2.digitaloceanspaces.com.evil.example/bucket/handoffs/123.bin",
    "https://other.example/handoffs/123.bin",
    "file:///etc/passwd",
])
def test_untrusted_storage_url_refused_before_get(tmp_path, monkeypatch, url):
    hid, secret = uuid.uuid4(), bytes(range(32))
    calls = _run_with_remote(monkeypatch, hid, b"ignored", grant_url=url)
    dest = tmp_path / "restored"
    with pytest.raises(transfer.HandoffDownloadError):
        asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), dest))
    assert not dest.exists()
    assert [c.method for c in calls] == ["POST"]
    assert not list(tmp_path.glob(".celerp-handoff-*"))


def test_corrupt_encrypted_copy_does_not_replace_destination(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    src, blob, dest = (tmp_path / n for n in ("plain", "encrypted", "target"))
    src.write_bytes(b"financial records" * 200)
    encrypt_file(src, blob, secret)
    damaged = bytearray(blob.read_bytes())
    damaged[len(damaged) // 2] ^= 1
    _run_with_remote(monkeypatch, hid, bytes(damaged))
    dest.write_bytes(b"existing")
    with pytest.raises(InvalidTag):
        asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), dest))
    assert dest.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".celerp-handoff-*"))
    assert not list(tmp_path.glob(".celerp-copy-*"))


def test_oversized_claim_refused_before_receiving_bytes(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    _run_with_remote(monkeypatch, hid, b"small", content_length=transfer.MAX_CIPHERTEXT + 1)
    with pytest.raises(transfer.HandoffDownloadError, match="too large"):
        asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), tmp_path / "target"))
    assert not (tmp_path / "target").exists()


def test_expired_handoff_does_not_create_local_copy(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    calls = _run_with_remote(monkeypatch, hid, b"", status=404)
    with pytest.raises(transfer.HandoffDownloadError, match="not available"):
        asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), tmp_path / "target"))
    assert len(calls) == 1
    assert not list(tmp_path.glob(".celerp-handoff-*"))


def test_lost_grant_response_reuses_same_operation(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    original = tmp_path / "source"
    original.write_bytes(b"financial backup" * 600)
    encrypted = tmp_path / "encrypted"
    encrypt_file(original, encrypted, secret)
    calls = []
    signed = (f"https://sfo2.digitaloceanspaces.com/example-copies/"
              f"handoffs/{hid.hex}.bin?sig=test")

    def remote(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                raise httpx.ReadError("response lost after grant was created")
            return httpx.Response(200, json={"download_url": signed})
        assert request.method == "GET" and str(request.url) == signed
        return httpx.Response(200, content=encrypted.read_bytes())

    real = httpx.AsyncClient
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(remote), **kw))
    restored = tmp_path / "restored"
    asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), restored))
    assert restored.read_bytes() == original.read_bytes()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert uuid.UUID(calls[0]["operation_id"])
    assert calls[0]["proof"] == transfer.parse_handoff_link(
        handoff_page_link(hid, secret)).proof_hash
    assert not list(tmp_path.glob(".celerp-handoff-*"))


def test_transient_service_error_reuses_grant_operation(tmp_path, monkeypatch):
    hid, secret = uuid.uuid4(), bytes(range(32))
    source = tmp_path / "original"
    source.write_bytes(b"file" * 100)
    ciphertext = tmp_path / "cipher"
    encrypt_file(source, ciphertext, secret)
    requests = []
    target_url = f"https://sfo2.digitaloceanspaces.com/x/handoffs/{hid.hex}.bin?sig=test"

    def remote(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests.append(json.loads(request.content))
            return (httpx.Response(503) if len(requests) == 1 else
                    httpx.Response(200, json={"download_url": target_url}))
        return httpx.Response(200, content=ciphertext.read_bytes())

    real = httpx.AsyncClient
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(remote), **kw))
    out = tmp_path / "restored"
    asyncio.run(transfer.download_company_handoff(handoff_page_link(hid, secret), out))
    assert out.read_bytes() == source.read_bytes()
    assert requests[0] == requests[1]


@pytest.mark.asyncio
async def test_archive_verification_is_bound_to_original_content(monkeypatch):
    from celerp.services.company_handoff_transfer import verify_company_handoff, HandoffDownloadError
    copy_id = uuid.uuid4()
    proof, original = "a" * 64, "b" * 64
    requests = []
    def handler(req):
        requests.append(req)
        supplied = __import__("json").loads(req.content)
        if supplied == {"proof": proof, "backup_sha256": original}:
            return httpx.Response(200, json={"verified": True})
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    await verify_company_handoff(str(copy_id), proof, original)
    with pytest.raises(HandoffDownloadError):
        await verify_company_handoff(str(copy_id), proof, "c" * 64)
    with pytest.raises(HandoffDownloadError):
        await verify_company_handoff("../bad", proof, original)
    assert len(requests) == 2

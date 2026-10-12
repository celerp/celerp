# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""An untrusted handoff cannot introduce a second restore path or partial file."""
import asyncio
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

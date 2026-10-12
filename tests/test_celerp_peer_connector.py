"""Accountant outgoing sync uses the connector plan and safe delivery boundaries."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from celerp.connectors.base import ConnectorContext, SyncDirection
from celerp.connectors.registry import get
from celerp.connectors.sync_runner import supported_outbound, sync_plan
from celerp.connectors import celerp as peer


def test_celerp_is_outbound_only_and_registered():
    connector = get("celerp")
    assert supported_outbound(connector) == ["contacts_out"]
    assert sync_plan(connector, SyncDirection.OUTBOUND) == ["contacts_out"]
    assert sync_plan(connector, SyncDirection.INBOUND) == []


@pytest.mark.parametrize("url", [
    "http://example.com", "https://example.com/other", "https://user@host.test",
    "https://host.test:8443", "https://host.test/?query=1", "file:///etc/passwd",
    "https://host.test#fragment", "https://host.test@127.0.0.1",
])
def test_invalid_peer_url_is_rejected(url):
    with pytest.raises(ValueError):
        peer._target_url(ConnectorContext(company_id=str(uuid.uuid4()),
                                          store_handle=url, access_token="cp1.valid"))


@pytest.mark.asyncio
async def test_versioned_contact_outbound_and_retry_reporting(monkeypatch):
    sent = []
    company = str(uuid.uuid4())
    first = [("contact:a", 3, {"name": "A", "contact_type": "vendor", "private_note": "not sent"}),
             ("contact:b", 2, {"name": "B"})]
    async def read_page(company_id, after, since):
        assert str(company_id) == company
        assert since is None
        return first if not after else []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"public_only": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            assert url == "https://peer.example/company-peers/contacts"
            sent.append(kwargs)
            return SimpleNamespace(status_code=200 if len(sent) == 1 else 409,
                                   json=lambda: {"status": "updated"})
    from celerp.connectors import sync_runner
    async def no_prior_sync(*args):
        return None
    monkeypatch.setattr(sync_runner, "_last_success_watermark", no_prior_sync)
    monkeypatch.setattr(peer, "_read_page", read_page)
    monkeypatch.setattr(peer, "RateLimitedClient", Client)
    connector = peer.CelerpConnector()
    result = await connector.sync_contacts_out(ConnectorContext(
        company_id=company, store_handle="https://peer.example", access_token="cp1.secret"))
    assert result.updated == 1
    assert len(result.errors) == 1
    assert result.attention == [{"id": "contact:b", "label": "contact:b",
                                 "reason": "Receiving installation needs attention"}]
    assert sent[0]["json"]["source_revision"] == 3
    assert sent[0]["json"]["data"]["name"] == "A"
    assert sent[0]["json"]["data"]["contact_type"] == "vendor"
    assert sent[0]["json"]["data"]["email"] is None
    assert "private_note" not in sent[0]["json"]["data"]
    assert sent[0]["headers"]["Authorization"] == "Peer cp1.secret"


def test_peer_credential_form_uses_key_and_https_origin():
    from ui.routes.settings_connectors import _local_connector_entry, _store_url_error
    assert _local_connector_entry("celerp")["auth_type"] == "api_key"
    assert _store_url_error("https://accountant.example.com", "celerp") is None
    for url in ("", "http://accountant.example.com", "https://accountant.example.com/path",
                "https://accountant.example.com?x=1", "https://user@accountant.example.com"):
        assert _store_url_error(url, "celerp") is not None


@pytest.mark.asyncio
async def test_contact_sync_resumes_from_last_successful_run(monkeypatch):
    from datetime import datetime, timezone
    from celerp.connectors import sync_runner

    watermark = datetime(2026, 10, 12, tzinfo=timezone.utc)
    seen = []
    async def last_success(company_id, connector, entity):
        assert connector == "celerp" and entity == "contacts_out"
        return watermark
    async def read_page(company_id, after, since):
        seen.append((str(company_id), after, since))
        return []
    monkeypatch.setattr(sync_runner, "_last_success_watermark", last_success)
    monkeypatch.setattr(peer, "_read_page", read_page)
    result = await peer.CelerpConnector().sync_contacts_out(ConnectorContext(
        company_id=str(uuid.uuid4()), store_handle="https://peer.example",
        access_token="cp1.secret",
    ))
    assert result.ok
    assert len(seen) == 1 and seen[0][1:] == ("", watermark)

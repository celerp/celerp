# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Installation-wide pages answer only the installation owner.

A company admin who is not the installation owner can post to these addresses
directly; the UI turns them away before it reads the upload, calls the API or
anything outside, or writes a file.
"""
from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from test_helpers import make_test_token

_ZIP = {"file": ("m.zip", b"PK" + b"\0" * (2 * 1024 * 1024), "application/zip")}
_BACKUP = {"file": ("b.celerp-backup", b"\0" * (2 * 1024 * 1024), "application/octet-stream")}

ROUTES = [
    ("GET", "/modules/acme/delete-options", {}),
    ("POST", "/modules/acme/delete", {}),
    ("POST", "/modules/import", {"files": _ZIP}),
    ("POST", "/modules/import-path", {"json": {"path": "/tmp/acme"}}),
    ("POST", "/modules/community-download", {"data": {"id": "acme"}}),
    ("POST", "/modules/community-import", {"data": {"id": "acme", "path": "x.zip"}}),
    ("POST", "/modules/restart", {}),
    ("POST", "/modules/buy", {"data": {"id": "acme"}}),
    ("POST", "/modules/marketplace-download", {"data": {"id": "acme"}}),
    ("POST", "/modules/marketplace-install", {"data": {"slug": "acme", "ref": "mp_" + "0" * 32}}),
    ("GET", "/settings/billing-portal", {}),
    ("POST", "/settings/cloud-activate", {}),
    ("POST", "/settings/cloud-send-otp", {"data": {"claim_email": "a@example.com"}}),
    ("POST", "/settings/cloud-claim", {"data": {"claim_email": "a@example.com", "otp": "123456"}}),
    ("POST", "/settings/cloud-disconnect", {}),
    ("POST", "/settings/cloud-accept-tos", {}),
    ("POST", "/settings/partner-claim/accept", {"data": {"claim_token": "x"}}),
    ("POST", "/settings/cloud/test-db", {"data": {"db_url": "postgresql://h/db"}}),
    ("POST", "/settings/cloud/test-storage", {"data": {"s3_bucket": "b"}}),
    ("POST", "/settings/cloud/save-infra", {"data": {"db_url": "postgresql://h/db"}}),
    ("POST", "/settings/cloud/restore-db", {}),
    ("GET", "/backup/list", {}),
    ("POST", "/backup/trigger", {}),
    ("GET", "/backup/export", {}),
    ("GET", "/backup/export/acme", {}),
    ("POST", "/backup/restore/acme", {}),
    ("POST", "/backup/import", {"files": _BACKUP}),
    ("POST", "/backup/import/continue", {"data": {"confirmation_id": "x", "digest": "y"}}),
    ("POST", "/account/email", {"data": {"email": "a@example.com"}}),
    ("GET", "/account/google", {}),
    ("GET", "/account/poll", {}),
    ("POST", "/system/update", {}),
    ("POST", "/system/update/check", {}),
    ("PATCH", "/system/update/settings", {"json": {"auto": True}}),
]


@pytest.fixture
def side_effects(tmp_path, monkeypatch):
    """Every outgoing request and every file the request leaves behind."""
    sent: list[str] = []

    async def send(self, request, **kw):
        sent.append(f"{request.method} {request.url}")
        raise httpx.ConnectError("no network in this test", request=request)

    spool, data = tmp_path / "spool", tmp_path / "data"
    spool.mkdir()
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    monkeypatch.setenv("CELERP_DATA_DIR", str(data))
    yield sent, spool, data


@pytest.mark.parametrize("hx", [False, True], ids=["page", "htmx"])
@pytest.mark.parametrize("method,path,kw", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_company_admin_who_is_not_the_installation_owner_is_turned_away_untouched(
        side_effects, method, path, kw, hx):
    sent, spool, data = side_effects
    from ui.app import app

    company = {"current_role": "admin", "settings": {}}
    headers = {"HX-Request": "true"} if hx else {}
    with patch("ui.api_client.installation_owner", new=AsyncMock(return_value=False), create=True), \
         patch("ui.api_client.get_company", new=AsyncMock(return_value=company)):
        client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
        client.cookies.set("celerp_token", make_test_token(role="admin"))
        r = client.request(method, path, headers=headers, **kw)

    assert sent == []
    assert list(spool.iterdir()) == []
    assert not data.exists()
    if hx:
        assert r.status_code == 204
        assert "Only the installation owner can do this." in r.headers["HX-Trigger"]
    else:
        assert r.status_code == 403
        assert r.text == "Only the installation owner can do this."

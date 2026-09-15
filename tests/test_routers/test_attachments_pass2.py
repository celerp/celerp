# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Cluster 5 - attachment authorization (Phase E).

Target (post-fix) behavior:
- Attachments are served only through an authenticated API route that scopes to
  the caller's own company. At merge-base ea480c48 the API mounts
  ``data_dir/static`` anonymously at ``/static`` (main.py:514), so an
  unauthenticated probe or a cross-tenant token retrieves any company's file.
  RED at base (returns 200 instead of 401 / 404).
- The route resolves only inside ``data_dir/static/attachments/<caller company>``
  and rejects traversal.
"""

from __future__ import annotations

import uuid

import pytest

from celerp.config import settings


async def _register(client, name: str, email: str) -> tuple[str, str]:
    """Register a company; return (bearer token, company_id)."""
    reg = await client.post(
        "/auth/register",
        json={"company_name": name, "email": email, "name": "Admin", "password": "pw"},
    )
    assert reg.status_code == 200, reg.text
    token = reg.json()["access_token"]
    me = await client.get("/companies/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return token, me.json()["id"]


def _write_attachment(company_id: str, filename: str, content: bytes) -> None:
    d = settings.data_dir / "static" / "attachments" / str(company_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(content)


@pytest.mark.asyncio
async def test_attachment_requires_auth_and_serves_own_file(client):
    token, cid = await _register(client, "AttachCo", f"attach-{uuid.uuid4().hex}@example.com")
    payload = b"\x89PNG\r\n\x1a\nOWNBYTES"
    _write_attachment(cid, "pic.png", payload)
    url = f"/static/attachments/{cid}/pic.png"

    # Anonymous access is rejected (no more open mount).
    anon = await client.get(url)
    assert anon.status_code == 401, f"anonymous attachment fetch must be rejected, got {anon.status_code}"

    # The owning company gets its own file back.
    h = {"Authorization": f"Bearer {token}"}
    ok = await client.get(url, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.content == payload

    # Traversal out of the company directory is refused.
    trav = await client.get(f"/static/attachments/{cid}/..", headers=h)
    assert trav.status_code == 404


@pytest.mark.asyncio
async def test_attachment_cross_tenant_returns_404(client):
    token, _cid = await _register(client, "TenantA", f"a-{uuid.uuid4().hex}@example.com")
    other = str(uuid.uuid4())
    # The file exists on disk under a different company's directory.
    _write_attachment(other, "secret.png", b"OTHER-TENANT-SECRET")

    # A valid token scoped to its own company cannot read another company's
    # attachment even when the file is present.
    r = await client.get(
        f"/static/attachments/{other}/secret.png",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, f"cross-tenant fetch must 404, got {r.status_code}"

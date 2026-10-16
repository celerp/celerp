# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Handoff export authority follows the canonical company owner context."""
import base64
import uuid

import pytest

from company_backup_support import company, owner, token
from migration_support import real_client, real_engine  # noqa: F401


@pytest.mark.asyncio
async def test_handoff_export_is_owned_and_company_scoped(real_engine, real_client, monkeypatch, tmp_path):
    from celerp.services import company_handoff_sender, company_backup
    user = await owner(real_engine)
    company_id = await company(real_engine, user, "Owner books", "synthetic-sender")
    bearer = await token(real_engine, user, company_id)
    captures = []

    async def export(cid, destination):
        captures.append(("export", cid))
        destination.write_bytes(b"synthetic company archive")

    async def send(**kwargs):
        captures.append(("send", kwargs["company_id"]))
        dest = tmp_path / "source.celerp-company"
        await kwargs["export"](dest)
        return {"id": str(uuid.uuid4()), "link": "synthetic-link"}

    monkeypatch.setattr(company_backup, "export_company_snapshot", export)
    monkeypatch.setattr(company_handoff_sender, "send_company_backup", send)
    body = {
        "operation_id": str(uuid.uuid4()),
        "key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "firm_name": "Owner books", "recipient": "client@example.com",
        "reply_to": "verified@example.com", "note": "Your copy",
    }
    refused = await real_client.post("/company-backups/send-handoff", json=body)
    assert refused.status_code == 401
    assert not captures
    accepted = await real_client.post("/company-backups/send-handoff", json=body,
                                      headers={"Authorization": f"Bearer {bearer}"})
    assert accepted.status_code == 200, accepted.text
    assert captures == [("send", company_id), ("export", company_id)]

@pytest.mark.asyncio
async def test_handoff_activity_and_revoke_are_company_scoped(real_engine, real_client, monkeypatch):
    from celerp.services import company_handoff_sender
    user = await owner(real_engine)
    first = await company(real_engine, user, "Company One", "handoff-owner-one")
    second = await company(real_engine, user, "Company Two", "handoff-owner-two")
    bearer = await token(real_engine, user, first)
    first_copy, second_copy = str(uuid.uuid4()), str(uuid.uuid4())
    calls = []

    async def cloud(method, path, *, params=None, **kw):
        calls.append((method, path, params))
        if method == "GET":
            assert params == {"company_id": str(first)}
            return {"items": [{"id": first_copy, "link_id": first_copy, "company_id": str(first),
                               "status": "sent", "opened": True, "stored": True,
                               "downloaded": False, "company_opened": False,
                               "expires_at": "2026-10-25T00:00:00+00:00"}]}
        assert method == "POST"
        if path == f"/handoffs/{first_copy}/new-link":
            raise company_handoff_sender.HandoffRefused(
                "This copy is no longer stored. Use Resend to send a new copy", 410)
        assert path in (f"/handoffs/{first_copy}/revoke", f"/handoffs/{first_copy}/restore")
        return {"id": first_copy}

    monkeypatch.setattr(company_handoff_sender, "_cloud", cloud)
    url = "/company-backups/handoffs"
    assert (await real_client.get(url)).status_code == 401
    headers = {"Authorization": f"Bearer {bearer}"}
    response = await real_client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["id"] == first_copy
    # Without the key this installation kept at sending, there is no link to copy.
    assert response.json()["items"][0]["link"] is None
    bad = await real_client.post(f"{url}/{second_copy}/revoke", headers=headers)
    assert bad.status_code == 404
    assert not any(method == "POST" for method, _, _ in calls)
    good = await real_client.post(f"{url}/{first_copy}/revoke", headers=headers)
    assert good.status_code == 200 and good.json()["id"] == first_copy
    assert ("POST", f"/handoffs/{first_copy}/revoke", None) in calls
    undone = await real_client.post(f"{url}/{first_copy}/restore", headers=headers)
    assert undone.status_code == 200
    assert ("POST", f"/handoffs/{first_copy}/restore", None) in calls
    for action in ("restore", "new-link"):
        assert (await real_client.post(f"{url}/{first_copy}/{action}",
                                       json={"operation_id": str(uuid.uuid4())})).status_code == 401
    bad_body = await real_client.post(f"{url}/{first_copy}/new-link", json={"operation_id": "x"},
                                      headers=headers)
    assert bad_body.status_code == 422
    gone = await real_client.post(f"{url}/{first_copy}/new-link",
                                  json={"operation_id": str(uuid.uuid4())}, headers=headers)
    assert gone.status_code == 410 and "Resend" in gone.json()["detail"]


@pytest.mark.asyncio
async def test_handoff_activity_rejects_cross_company_cloud_response(real_engine, real_client, monkeypatch):
    from celerp.services import company_handoff_sender
    user = await owner(real_engine)
    first = await company(real_engine, user, "Company One", "handoff-other-one")
    second = await company(real_engine, user, "Company Two", "handoff-other-two")
    bearer = await token(real_engine, user, first)
    calls = []

    async def wrong_company(method, path, *, params=None, **kw):
        calls.append(method)
        return {"items": [{"id": str(uuid.uuid4()), "link_id": str(uuid.uuid4()),
                           "company_id": str(second), "status": "sent"}]}

    monkeypatch.setattr(company_handoff_sender, "_cloud", wrong_company)
    headers = {"Authorization": f"Bearer {bearer}"}
    assert (await real_client.get("/company-backups/handoffs", headers=headers)).status_code == 502
    assert (await real_client.post(f"/company-backups/handoffs/{uuid.uuid4()}/revoke",
                                   headers=headers)).status_code == 502
    assert calls == ["GET", "GET"]


@pytest.mark.asyncio
async def test_send_link_without_connect_is_refused_with_account_needed(real_engine, real_client, monkeypatch):
    """With no Celerp account the API answers 428 so the page can offer a free account
    in place; nothing is exported."""
    from celerp.services import company_backup, cloud_entitlement
    user = await owner(real_engine)
    company_id = await company(real_engine, user, "No account books", "no-account-sender")
    bearer = await token(real_engine, user, company_id)
    exported = []

    async def export(cid, destination):
        exported.append(cid)
        destination.write_bytes(b"synthetic company archive")

    async def no_account(*a, **kw):
        return None
    monkeypatch.setattr(company_backup, "export_company_snapshot", export)
    monkeypatch.setattr(cloud_entitlement, "authenticated_request", no_account)
    body = {
        "operation_id": str(uuid.uuid4()),
        "key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "firm_name": "Owner books", "recipient": "client@example.com",
        "reply_to": "verified@example.com", "note": "",
    }
    r = await real_client.post("/company-backups/send-handoff", json=body,
                               headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 428, r.text
    assert "free Celerp account" in r.json()["detail"]
    assert not exported

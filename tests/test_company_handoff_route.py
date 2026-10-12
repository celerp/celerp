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

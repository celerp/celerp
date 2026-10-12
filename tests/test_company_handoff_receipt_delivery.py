# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Only completed company imports can produce cloud handoff attribution."""
import io
import json
import uuid

import httpx
import pytest

from celerp.services import company_backup_files as files
from celerp.services import company_handoff_receipts as receipts
from celerp.gateway.state import activate_payload


@pytest.fixture
def upload(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "company_backups_dir", lambda: tmp_path)
    stage = files.stage_path("owner", uuid.uuid4().hex)
    files.stage_upload(io.BytesIO(b"valid sample"), stage, limit=100, owner="owner",
                       mode="settings", file_name="source.celerp-company")
    files.bind_handoff(stage, "63b7e069-21b0-451c-a954-29c730b24541", "a" * 64)
    return stage


@pytest.mark.asyncio
async def test_report_only_after_restore_and_retry_lost_response(upload, monkeypatch):
    calls = []
    fail = True

    def handle(request):
        nonlocal fail
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/handoffs/63b7e069-21b0-451c-a954-29c730b24541/imported"
        assert json.loads(request.content) == {"proof": "a" * 64}
        return httpx.Response(503 if fail else 200, json={"imported": not fail})

    original = httpx.AsyncClient
    monkeypatch.setattr(receipts.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr("celerp.gateway.state.relay_http_url", lambda: "https://relay.celerp.com")
    assert await receipts.report_completed_handoff(upload) is False
    assert not calls
    files.finish_stage(upload, uuid.uuid4())
    assert await receipts.report_completed_handoff(upload) is False
    assert len(calls) == 1
    assert not files.pending_handoffs(reported=True)
    assert "handoff_receipt" not in activate_payload(str(uuid.uuid4()), include_handoff=True)
    fail = False
    await receipts.retry_completed_handoffs()
    assert len(calls) == 2
    payload = activate_payload(str(uuid.uuid4()), include_handoff=True)
    assert payload["handoff_id"] == "63b7e069-21b0-451c-a954-29c730b24541"
    assert payload["handoff_receipt"] == "a" * 64
    assert "handoff_receipt" not in activate_payload(str(uuid.uuid4()))
    assert await receipts.report_completed_handoff(upload) is True
    assert len(calls) == 2
    files.acknowledge_handoff(payload["handoff_id"])
    assert "handoff_receipt" not in activate_payload(str(uuid.uuid4()), include_handoff=True)


@pytest.mark.asyncio
async def test_report_never_marks_failed_restore(upload, monkeypatch):
    attempts = []
    monkeypatch.setattr(receipts.httpx, "AsyncClient", lambda **kw: attempts.append(kw))
    assert await receipts.report_completed_handoff(upload) is False
    await receipts.retry_completed_handoffs()
    assert not attempts
    files.discard_stage(upload)
    assert not files.pending_handoffs()

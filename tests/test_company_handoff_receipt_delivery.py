# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Completed restores retain attribution until authenticated activation acknowledges it."""
import io
import uuid

import pytest

from celerp.services import company_backup_files as files
from celerp.gateway.state import activate_payload


@pytest.fixture
def upload(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "company_backups_dir", lambda: tmp_path)
    stage = files.stage_path("owner", uuid.uuid4().hex)
    files.stage_upload(io.BytesIO(b"synthetic backup"), stage, limit=100, owner="owner",
                       mode="settings", file_name="source.celerp-company")
    files.bind_handoff(stage, "63b7e069-21b0-451c-a954-29c730b24541", "a" * 64)
    return stage


def test_attribution_only_after_finished_restore_and_activation_ack(upload):
    instance = str(uuid.uuid4())
    assert "handoff_receipt" not in activate_payload(instance, include_handoff=True)
    files.finish_stage(upload, uuid.uuid4())
    assert not upload.exists()
    payload = activate_payload(instance, include_handoff=True)
    assert payload["handoff_id"] == "63b7e069-21b0-451c-a954-29c730b24541"
    assert payload["handoff_receipt"] == "a" * 64
    assert "handoff_receipt" not in activate_payload(instance)
    assert files.pending_handoffs()  # survives an unconfirmed activation response
    files.acknowledge_handoff(payload["handoff_id"])
    assert not files.pending_handoffs()
    assert "handoff_receipt" not in activate_payload(instance, include_handoff=True)


def test_failed_restore_never_creates_attribution(upload):
    assert not files.pending_handoffs()
    files.discard_stage(upload)
    assert not files.pending_handoffs()
    assert "handoff_receipt" not in activate_payload(str(uuid.uuid4()), include_handoff=True)

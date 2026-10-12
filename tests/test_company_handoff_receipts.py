# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Private, retryable attribution receipts for verified company restores."""
import io
import os
import time
import uuid

import pytest

from celerp.services import company_backup_files as files


def _staged(tmp_path, monkeypatch, *, handoff=False):
    monkeypatch.setattr(files, "company_backups_dir", lambda: tmp_path)
    stage = files.stage_path("owner", uuid.uuid4().hex)
    files.stage_upload(io.BytesIO(b"a valid synthetic backup"), stage,
                       limit=1024, owner="owner", mode="settings", file_name="example.celerp-company")
    if handoff:
        files.bind_handoff(stage, str(uuid.UUID("63b7e069-21b0-451c-a954-29c730b24541")), "a" * 64)
    return stage


def test_receipt_survives_restore_and_restart_without_plaintext(tmp_path, monkeypatch):
    stage = _staged(tmp_path, monkeypatch, handoff=True)
    assert files.pending_handoffs() == []
    files.finish_stage(stage, uuid.uuid4())
    assert not stage.exists()
    assert len(files.pending_handoffs()) == 1
    receipt = files.pending_handoffs()[0][1]
    assert receipt == {"copy_id": "63b7e069-21b0-451c-a954-29c730b24541", "proof": "a" * 64}
    assert "secret" not in str(files.stage_facts(stage))
    files.sweep_transient_files()
    assert len(files.pending_handoffs()) == 1
    files.mark_handoff_reported(stage)
    assert len(files.pending_handoffs(reported=True)) == 1
    assert not files.pending_handoffs(reported=False)
    files.acknowledge_handoff(receipt["copy_id"])
    assert files.pending_handoffs() == []
    assert files.restored_company(stage)


def test_receipt_binding_rejects_other_operation_without_changes(tmp_path, monkeypatch):
    stage = _staged(tmp_path, monkeypatch, handoff=True)
    original = files.stage_facts(stage)
    for copy_id, proof in [("bad", "a" * 64), (str(uuid.uuid4()), "invalid"),
                           (str(uuid.uuid4()), "b" * 64)]:
        with pytest.raises(ValueError):
            files.bind_handoff(stage, copy_id, proof)
        assert files.stage_facts(stage) == original
    files.discard_stage(stage)
    assert files.pending_handoffs() == []


def test_only_completed_receipt_survives_one_day_purge(tmp_path, monkeypatch):
    pending = _staged(tmp_path, monkeypatch, handoff=True)
    finished = _staged(tmp_path, monkeypatch, handoff=True)
    files.finish_stage(finished, uuid.uuid4())
    metadata_pending = pending.with_suffix(files.META_SUFFIX)
    metadata_done = finished.with_suffix(files.META_SUFFIX)
    before = time.time() - files.UPLOAD_TTL_SECONDS - 100
    for p in (pending, metadata_pending, metadata_done):
        os.utime(p, (before, before))
    files.purge_expired_uploads()
    assert not pending.exists() and not metadata_pending.exists()
    assert metadata_done.exists()
    assert len(files.pending_handoffs()) == 1
    older = time.time() - files.RECEIPT_TTL_SECONDS - 100
    os.utime(metadata_done, (older, older))
    files.purge_expired_uploads()
    assert not metadata_done.exists()
    assert files.pending_handoffs() == []

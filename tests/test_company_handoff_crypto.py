# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Local company-copy encryption and authenticated file publication."""
from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from celerp.services.company_handoff_crypto import decrypt_file, encrypt_file


def test_copy_encrypted_round_trip_with_multiple_chunks(tmp_path):
    src, blob, restored = (tmp_path / n for n in ("backup", "cipher", "restored"))
    original = os.urandom(2_500_000)
    src.write_bytes(original)
    key = os.urandom(32)
    assert encrypt_file(src, blob, key) == blob.stat().st_size
    assert original not in blob.read_bytes()
    decrypt_file(blob, restored, key)
    assert restored.read_bytes() == original


def test_invalid_key_or_modified_blob_never_publishes_plaintext(tmp_path):
    src, blob, restored = (tmp_path / n for n in ("backup", "cipher", "restored"))
    src.write_bytes(b"CONFIDENTIAL-COMPANY-COPY" * 500)
    encrypt_file(src, blob, os.urandom(32))
    restored.write_bytes(b"existing")
    with pytest.raises(InvalidTag):
        decrypt_file(blob, restored, os.urandom(32))
    assert restored.read_bytes() == b"existing"
    raw = bytearray(blob.read_bytes())
    raw[len(raw)//2] ^= 1
    blob.write_bytes(raw)
    with pytest.raises(InvalidTag):
        decrypt_file(blob, restored, os.urandom(32))
    assert restored.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".celerp-copy-*"))


def test_truncated_and_wrong_format_fail_closed(tmp_path):
    source, dest = tmp_path / "cipher", tmp_path / "restored"
    key = os.urandom(32)
    for data in (b"", b"BAD", b"WRONG-FORMAT" + b"x" * 100):
        source.write_bytes(data)
        with pytest.raises(ValueError):
            decrypt_file(source, dest, key)
        assert not dest.exists()


def test_rejected_oversized_source_does_not_replace_destination(tmp_path, monkeypatch):
    import celerp.services.company_handoff_crypto as crypto

    source, dest = tmp_path / "source", tmp_path / "cipher"
    source.write_bytes(b"A" * 17)
    dest.write_bytes(b"existing")
    monkeypatch.setattr(crypto, "MAX_PLAINTEXT", 10)
    with pytest.raises(ValueError):
        encrypt_file(source, dest, os.urandom(32))
    assert dest.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".celerp-copy-*"))


def test_ciphertext_altered_with_correct_key_does_not_publish(tmp_path):
    source, blob, restored = (tmp_path / n for n in ("backup", "cipher", "restored"))
    source.write_bytes(b"test data" * 500)
    key = os.urandom(32)
    encrypt_file(source, blob, key)
    data = bytearray(blob.read_bytes())
    data[-17] ^= 1
    blob.write_bytes(data)
    with pytest.raises(InvalidTag):
        decrypt_file(blob, restored, key)
    assert not restored.exists()

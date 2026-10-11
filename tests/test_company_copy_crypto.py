# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company-copy ciphertext round trips and fails without an intact key and file."""
from __future__ import annotations

import os

import pytest

from celerp.services.company_copy_crypto import (
    decrypt_copy, encrypt_copy, new_secret, secret_from_fragment,
)


def test_streamed_encrypted_company_copy(tmp_path):
    source = tmp_path / "company.celerp-company"
    body = os.urandom(2 * 1024 * 1024 + 113)
    source.write_bytes(body)
    ciphertext = tmp_path / "company.enc"
    restored = tmp_path / "restored.celerp-company"
    key, fragment = new_secret()
    assert secret_from_fragment(fragment) == key
    assert encrypt_copy(source, ciphertext, key) == ciphertext.stat().st_size
    assert body not in ciphertext.read_bytes()
    decrypt_copy(ciphertext, restored, secret_from_fragment(fragment))
    assert restored.read_bytes() == body


@pytest.mark.parametrize("change", ["wrong_key", "tampered", "truncated"])
def test_copy_is_rejected_atomically(tmp_path, change):
    original = tmp_path / "company"
    original.write_bytes(os.urandom(300_000))
    encrypted = tmp_path / "encrypted"
    destination = tmp_path / "destination"
    key, _ = new_secret()
    encrypt_copy(original, encrypted, key)
    if change == "wrong_key":
        key, _ = new_secret()
    else:
        contents = bytearray(encrypted.read_bytes())
        if change == "tampered":
            contents[100] ^= 1
        else:
            contents = contents[:-4]
        encrypted.write_bytes(contents)
    with pytest.raises((ValueError, Exception)):
        decrypt_copy(encrypted, destination, key)
    assert not destination.exists()


def test_rejects_malformed_secret_and_size_without_writing(tmp_path, monkeypatch):
    from celerp.services import company_copy_crypto as c
    for value in ("", "a" * 43, "B" * 42, "!" * 43, "A" * 44):
        if value == "a" * 43:
            # A valid base64url key can look like repeated letters.
            continue
        with pytest.raises(ValueError):
            secret_from_fragment(value)
    source = tmp_path / "large"
    source.write_bytes(b"x")
    output = tmp_path / "output"
    monkeypatch.setattr(c, "MAX_ENCRYPTED_BYTES", 2)
    with pytest.raises(ValueError):
        c.encrypt_copy(source, output, b"x" * 32)
    assert not output.exists()

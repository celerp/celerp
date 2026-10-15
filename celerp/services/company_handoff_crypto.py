# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Streaming encryption for company handoff handoffs.

The decrypted file is published only after its authentication tag verifies.
Company data import continues through the existing backup restore service.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MAGIC = b"CELERP-COPY\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CHUNK = 1024 * 1024
MAX_CIPHERTEXT = 1024 ** 3
MAX_PLAINTEXT = MAX_CIPHERTEXT - len(_MAGIC) - _NONCE_BYTES - _TAG_BYTES


def _key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("A company handoff key must be 32 bytes")


def _temp(destination: Path) -> tuple[int, Path]:
    fd, name = tempfile.mkstemp(prefix=".celerp-copy-", dir=destination.parent)
    return fd, Path(name)


def encrypt_file(source: Path, destination: Path, key: bytes) -> int:
    """Encrypt a bounded file with a fresh nonce, returning ciphertext bytes."""
    _key(key)
    if source.stat().st_size > MAX_PLAINTEXT:
        raise ValueError("Company copy exceeds the handoff size limit")
    nonce = os.urandom(_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_MAGIC)
    fd, temporary = _temp(destination)
    try:
        with source.open("rb") as plain, os.fdopen(fd, "wb") as encrypted:
            encrypted.write(_MAGIC + nonce)
            total = 0
            while chunk := plain.read(_CHUNK):
                total += len(chunk)
                if total > MAX_PLAINTEXT:
                    raise ValueError("Company copy exceeds the handoff size limit")
                encrypted.write(encryptor.update(chunk))
            encrypted.write(encryptor.finalize())
            encrypted.write(encryptor.tag)
            encrypted.flush()
            os.fsync(encrypted.fileno())
        if temporary.stat().st_size > MAX_CIPHERTEXT:
            raise ValueError("Company copy exceeds the handoff size limit")
        os.replace(temporary, destination)
        return destination.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Authenticate the whole file before exposing a decrypted company backup."""
    _key(key)
    size = source.stat().st_size
    overhead = len(_MAGIC) + _NONCE_BYTES + _TAG_BYTES
    if size < overhead or size > MAX_CIPHERTEXT:
        raise ValueError("Invalid company handoff file size")
    with source.open("rb") as encrypted:
        header = encrypted.read(len(_MAGIC) + _NONCE_BYTES)
        if header[:len(_MAGIC)] != _MAGIC:
            raise ValueError("Unsupported company handoff file")
        nonce = header[len(_MAGIC):]
        encrypted.seek(-_TAG_BYTES, os.SEEK_END)
        tag = encrypted.read(_TAG_BYTES)
        encrypted.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(_MAGIC)
        fd, temporary = _temp(destination)
        try:
            with os.fdopen(fd, "wb") as plain:
                remaining = size - overhead
                while remaining:
                    chunk = encrypted.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise ValueError("Truncated company handoff file")
                    remaining -= len(chunk)
                    plain.write(decryptor.update(chunk))
                plain.write(decryptor.finalize())
                plain.flush()
                os.fsync(plain.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

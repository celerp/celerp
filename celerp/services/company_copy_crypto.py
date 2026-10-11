# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Stream encrypted company copies without buffering company data in memory."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"CELERP-COPY\x01"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
MAX_ENCRYPTED_BYTES = 1024 ** 3


def new_secret() -> tuple[bytes, str]:
    key = secrets.token_bytes(32)
    return key, base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def secret_from_fragment(value: str) -> bytes:
    if len(value) != 43 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in value):
        raise ValueError("Invalid copy key")
    try:
        key = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid copy key") from exc
    if len(key) != 32 or base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii") != value:
        raise ValueError("Invalid copy key")
    return key


def encrypt_copy(source: Path, destination: Path, key: bytes) -> int:
    if len(key) != 32:
        raise ValueError("Invalid copy key")
    total = source.stat().st_size + len(MAGIC) + NONCE_SIZE + TAG_SIZE
    if total > MAX_ENCRYPTED_BYTES:
        raise ValueError("Company copy is over the 1 GB limit")
    nonce = secrets.token_bytes(NONCE_SIZE)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as inp, destination.open("xb") as out:
        out.write(MAGIC + nonce)
        for block in iter(lambda: inp.read(CHUNK_SIZE), b""):
            out.write(cipher.update(block))
        out.write(cipher.finalize())
        out.write(cipher.tag)
    return total


def decrypt_copy(source: Path, destination: Path, key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("Invalid copy key")
    size = source.stat().st_size
    overhead = len(MAGIC) + NONCE_SIZE + TAG_SIZE
    if size < overhead or size > MAX_ENCRYPTED_BYTES:
        raise ValueError("Invalid encrypted company copy")
    try:
        with source.open("rb") as inp:
            if inp.read(len(MAGIC)) != MAGIC:
                raise ValueError("Invalid encrypted company copy")
            nonce = inp.read(NONCE_SIZE)
            inp.seek(-TAG_SIZE, os.SEEK_END)
            tag = inp.read(TAG_SIZE)
            inp.seek(len(MAGIC) + NONCE_SIZE)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            remaining = size - overhead
            with destination.open("xb") as out:
                while remaining:
                    block = inp.read(min(CHUNK_SIZE, remaining))
                    if not block:
                        raise ValueError("Encrypted company copy is incomplete")
                    out.write(decryptor.update(block))
                    remaining -= len(block)
                out.write(decryptor.finalize())
    except Exception:
        destination.unlink(missing_ok=True)
        raise

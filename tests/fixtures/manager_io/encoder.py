# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Independent writer for synthetic Manager business files.

A message is a dict of field number to value. Values encode by Python type:
bool and non-negative int as varints, str as UTF-8, bytes as raw
length-delimited data, uuid.UUID as protobuf-net's Guid message, Decimal as
its decimal message, date as its DateTime message (whole days), a nested dict
as a sub-message and a list as a repeated field. None, False, 0 and empty
strings are omitted, as protobuf-net omits defaults.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

SCHEMA_GUID = uuid.UUID("a9a71e47-82b3-49db-8aec-898adb460a80")
SCHEMA_VERSION = 419
_EPOCH = date(1970, 1, 1)


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _key(number: int, wire: int) -> bytes:
    return varint((number << 3) | wire)


def _length(number: int, payload: bytes) -> bytes:
    return _key(number, 2) + varint(len(payload)) + payload


def guid_message(value: uuid.UUID) -> bytes:
    raw = value.bytes_le
    return _key(1, 1) + raw[:8] + _key(2, 1) + raw[8:]


def decimal_message(value: Decimal) -> bytes:
    sign, digits, exponent = value.as_tuple()
    scale = max(0, -exponent)
    mantissa = int("".join(map(str, digits)) or "0") * (10 ** max(0, exponent))
    lo, hi = mantissa & ((1 << 64) - 1), mantissa >> 64
    out = b""
    if lo:
        out += _key(1, 0) + varint(lo)
    if hi:
        out += _key(2, 0) + varint(hi)
    sign_scale = (scale << 1) | (1 if sign and mantissa else 0)
    if sign_scale:
        out += _key(3, 0) + varint(sign_scale)
    return out


def datetime_message(value: date) -> bytes:
    days = (value - _EPOCH).days
    zigzag = (days << 1) ^ (days >> 63)
    return _key(1, 0) + varint(zigzag) if days else b""


def encode(fields: dict[int, object]) -> bytes:
    out = bytearray()
    for number in sorted(fields):
        out += _field(number, fields[number])
    return bytes(out)


def _field(number: int, value: object) -> bytes:
    if value is None or value is False or value == "" or (type(value) is int and value == 0):
        return b""
    if isinstance(value, list):
        return b"".join(_field(number, v) for v in value)
    if value is True:
        return _key(number, 0) + varint(1)
    if isinstance(value, int):
        return _key(number, 0) + varint(value)
    if isinstance(value, str):
        return _length(number, value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return _length(number, bytes(value))
    if isinstance(value, uuid.UUID):
        return _length(number, guid_message(value))
    if isinstance(value, Decimal):
        return _length(number, decimal_message(value))
    if isinstance(value, date):
        return _length(number, datetime_message(value))
    if isinstance(value, dict):
        return _length(number, encode(value))
    raise TypeError(f"Cannot encode {type(value).__name__} in field {number}.")


@dataclass(frozen=True)
class Obj:
    key: uuid.UUID
    content_type: uuid.UUID
    fields: dict[int, object] | None = None
    raw: bytes | None = None                  # exact payload, for malformed-content tests

    def content(self) -> bytes:
        return self.raw if self.raw is not None else encode(self.fields or {})


@dataclass(frozen=True)
class Blob:
    key: uuid.UUID
    name: str
    content_type: str
    content: bytes


def write_manager_file(
    path: Path,
    objects: list[Obj],
    blobs: tuple[Blob, ...] = (),
    changes: int = 0,
    schema_version: int = SCHEMA_VERSION,
) -> Path:
    """Write a Manager business file with the given objects, legacy blobs and change-history rows."""
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'CREATE TABLE "Objects" ("Key" TEXT PRIMARY KEY NOT NULL, "ContentType" TEXT, "Content" BLOB, '
            '"Timestamp" INTEGER) WITHOUT ROWID'
        )
        conn.execute('CREATE INDEX "ix_Objects_ContentType" ON "Objects" ("ContentType")')
        conn.execute('CREATE TABLE "Blobs" ("Key" TEXT PRIMARY KEY NOT NULL, "Name" TEXT, "ContentType" TEXT, "Content" BLOB)')
        conn.execute(
            'CREATE TABLE "Changes" ("Key" TEXT PRIMARY KEY NOT NULL, "Commit" TEXT, "Object" TEXT, "User" TEXT, '
            '"Timestamp" INTEGER, "ContentTypeBefore" TEXT, "ContentTypeAfter" TEXT, "ContentBefore" BLOB, "ContentAfter" BLOB)'
        )
        ticks = 639_000_000_000_000_000
        schema = Obj(SCHEMA_GUID, SCHEMA_GUID, {1: schema_version})
        for index, obj in enumerate([schema, *objects]):
            conn.execute(
                'INSERT INTO "Objects" VALUES (?, ?, ?, ?)',
                (str(obj.key), str(obj.content_type), obj.content(), ticks + index),
            )
        for blob in blobs:
            conn.execute('INSERT INTO "Blobs" VALUES (?, ?, ?, ?)', (str(blob.key), blob.name, blob.content_type, blob.content))
        for index, obj in enumerate(objects[:changes]):
            conn.execute(
                'INSERT INTO "Changes" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(uuid.uuid5(obj.key, "change")), str(uuid.uuid5(obj.key, "commit")), str(obj.key),
                    "user@example.com", ticks + index, None, str(obj.content_type), None, obj.content(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path

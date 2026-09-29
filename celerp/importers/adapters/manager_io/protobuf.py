# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Bounded protobuf wire-format reader for Manager object payloads.

This reads the protobuf wire format only: field numbers, wire types, varints
and length-delimited bytes. It never instantiates a type named by the source.
Every length is checked against the bytes actually present before it is used,
and the payload size, nesting depth, repeated element count and total decoded
field count are capped, so a hostile payload cannot make the reader allocate
or loop without bound. A payload that breaks a rule raises `DecodeError`
with a short diagnostic that never contains payload bytes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

VARINT, FIXED64, LENGTH, FIXED32 = 0, 1, 2, 5


class DecodeError(ValueError):
    """A payload that is malformed or exceeds a resource limit."""


@dataclass(frozen=True)
class DecodeLimits:
    max_bytes: int = 4 * 1024 * 1024          # one object payload
    max_depth: int = 16                       # nested message levels
    max_repeated: int = 10_000                # elements of one repeated field
    max_fields: int = 200_000                 # fields decoded across one object, nested included


DEFAULT_LIMITS = DecodeLimits()


@dataclass
class _Budget:
    remaining: int


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    end = len(data)
    for _ in range(10):
        if pos >= end:
            raise DecodeError("Truncated varint.")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if result >> 64:
                raise DecodeError("Varint exceeds 64 bits.")
            return result, pos
        shift += 7
    raise DecodeError("Varint longer than 10 bytes.")


@dataclass
class Message:
    """One decoded message level. Nested messages decode on demand, within the same limits."""
    fields: dict[int, list[int | bytes]]
    depth: int
    limits: DecodeLimits
    budget: _Budget = field(repr=False)

    def values(self, number: int) -> list[int | bytes]:
        return self.fields.get(number, [])

    def last(self, number: int) -> int | bytes | None:
        found = self.fields.get(number)
        return found[-1] if found else None

    def int(self, number: int, default: int | None = None) -> int | None:
        value = self.last(number)
        if value is None:
            return default
        if not isinstance(value, int):
            raise DecodeError(f"Field {number} is not an integer.")
        return value

    def signed(self, number: int, default: int | None = None) -> int | None:
        """A two's-complement int32/int64 varint (protobuf `int32`/`int64`)."""
        value = self.int(number)
        if value is None:
            return default
        return value - (1 << 64) if value >> 63 else value

    def bool(self, number: int) -> bool:
        return bool(self.int(number, 0))

    def bytes(self, number: int) -> bytes | None:
        value = self.last(number)
        if value is not None and not isinstance(value, bytes):
            raise DecodeError(f"Field {number} is not length-delimited.")
        return value

    def str(self, number: int) -> str | None:
        raw = self.bytes(number)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecodeError(f"Field {number} is not valid UTF-8.") from exc

    def message(self, number: int) -> "Message | None":
        raw = self.bytes(number)
        return None if raw is None else self._nested(raw)

    def messages(self, number: int) -> list["Message"]:
        out = []
        for raw in self.values(number):
            if not isinstance(raw, bytes):
                raise DecodeError(f"Field {number} is not length-delimited.")
            out.append(self._nested(raw))
        return out

    def guid(self, number: int) -> uuid.UUID | None:
        nested = self.message(number)
        return None if nested is None else read_guid(nested)

    def decimal(self, number: int) -> Decimal | None:
        nested = self.message(number)
        return None if nested is None else read_decimal(nested)

    def date(self, number: int) -> date | None:
        nested = self.message(number)
        return None if nested is None else read_datetime(nested).date()

    def _nested(self, raw: bytes) -> "Message":
        return _decode(raw, self.depth + 1, self.limits, self.budget)


def _decode(data: bytes, depth: int, limits: DecodeLimits, budget: _Budget) -> Message:
    if depth > limits.max_depth:
        raise DecodeError(f"Nesting deeper than {limits.max_depth} levels.")
    fields: dict[int, list[int | bytes]] = {}
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0 or number > 0x1FFFFFFF:
            raise DecodeError("Invalid field number.")
        if wire == VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == FIXED64:
            if pos + 8 > end:
                raise DecodeError("Truncated fixed64 field.")
            value = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        elif wire == FIXED32:
            if pos + 4 > end:
                raise DecodeError("Truncated fixed32 field.")
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        elif wire == LENGTH:
            length, pos = _read_varint(data, pos)
            if length > end - pos:
                raise DecodeError("Length exceeds the remaining payload.")
            value = data[pos:pos + length]
            pos += length
        else:
            raise DecodeError(f"Unsupported wire type {wire}.")
        bucket = fields.setdefault(number, [])
        if len(bucket) >= limits.max_repeated:
            raise DecodeError(f"More than {limits.max_repeated} repeated elements.")
        budget.remaining -= 1
        if budget.remaining < 0:
            raise DecodeError(f"More than {limits.max_fields} decoded fields in one object.")
        bucket.append(value)
    return Message(fields=fields, depth=depth, limits=limits, budget=budget)


def decode(data: bytes, limits: DecodeLimits = DEFAULT_LIMITS) -> Message:
    """Decode the top level of one object payload."""
    if len(data) > limits.max_bytes:
        raise DecodeError(f"Object payload larger than {limits.max_bytes} bytes.")
    return _decode(bytes(data), 0, limits, _Budget(limits.max_fields))


# ── protobuf-net `bcl` encodings for .NET value types ─────────────────────────

def read_guid(msg: Message) -> uuid.UUID:
    """bcl.Guid: fixed64 lo (1) and hi (2) hold Guid.ToByteArray() little-endian."""
    lo = msg.int(1, 0)
    hi = msg.int(2, 0)
    return uuid.UUID(bytes_le=lo.to_bytes(8, "little") + hi.to_bytes(8, "little"))


_MAX_DECIMAL_SCALE = 28


def read_decimal(msg: Message) -> Decimal:
    """bcl.Decimal: lo (1) 64 bits, hi (2) 32 bits, signScale (3): bit 0 sign, bits 1-8 scale."""
    lo = msg.int(1, 0)
    hi = msg.int(2, 0)
    sign_scale = msg.int(3, 0)
    if hi >> 32:
        raise DecodeError("Decimal high word exceeds 32 bits.")
    scale = (sign_scale >> 1) & 0xFF
    if scale > _MAX_DECIMAL_SCALE:
        raise DecodeError("Decimal scale exceeds 28.")
    magnitude = (hi << 64) | lo
    value = Decimal(magnitude).scaleb(-scale)
    return -value if sign_scale & 1 else value


_TICKS_PER_UNIT = {0: 864_000_000_000, 1: 36_000_000_000, 2: 600_000_000, 3: 10_000_000, 4: 10_000, 5: 1}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def read_datetime(msg: Message) -> datetime:
    """bcl.DateTime: zigzag value (1) in units of scale (2) from the Unix epoch."""
    raw = msg.int(1, 0)
    value = (raw >> 1) ^ -(raw & 1)
    scale = msg.int(2, 0)
    if scale not in _TICKS_PER_UNIT:
        raise DecodeError("Unsupported DateTime scale.")
    ticks = value * _TICKS_PER_UNIT[scale]
    try:
        return _EPOCH + timedelta(microseconds=ticks // 10)
    except OverflowError as exc:
        raise DecodeError("DateTime out of range.") from exc

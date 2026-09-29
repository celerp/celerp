# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Private first-party registry of migration sinks.

A sink writes one group of CIF entities into the Celerp domain that owns them,
through that domain's shared import service, and measures the destination for
reconciliation. The migration runner depends only on this interface, never on
module internals. Registration is limited to bundled first-party modules.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from celerp.importers.importer import MAX_BATCH_SIZE
from celerp.importers.schema import (
    CIFSourceRecord,
    ReconciliationExpectations,
    ReconciliationMeasure,
)

# CIF bundle group -> the Celerp module that must be installed to receive it.
SINK_MODULES: dict[str, str] = {
    "company": "celerp",
    "currencies": "celerp",
    "exchange_rates": "celerp",
    "tax_codes": "celerp",
    "locations": "celerp",
    "attachments": "celerp",
    "accounts": "celerp-accounting",
    "journals": "celerp-accounting",
    "bank_transfers": "celerp-accounting",
    "contacts": "celerp-contacts",
    "items": "celerp-inventory",
    "inventory_adjustments": "celerp-inventory",
    "documents": "celerp-docs",
    "settlements": "celerp-docs",
}


@dataclass(frozen=True)
class SinkContext:
    session: AsyncSession
    company_id: uuid.UUID
    user_id: uuid.UUID
    run_id: uuid.UUID

    def idempotency_key(self, record: CIFSourceRecord, operation: str) -> str:
        return f"migration:{self.run_id}:{record.source_type}:{record.source_external_id}:{operation}"


@dataclass(frozen=True)
class SinkEntityMapping:
    source_type: str
    source_external_id: str
    target_entity_type: str
    target_entity_id: str
    status: str                               # created | skipped


@dataclass(frozen=True)
class SinkError:
    source_type: str
    source_external_id: str
    message: str


@dataclass
class SinkBatchResult:
    created: int = 0
    skipped: int = 0
    errors: list[SinkError] = field(default_factory=list)
    mappings: list[SinkEntityMapping] = field(default_factory=list)


@dataclass(frozen=True)
class DestinationMeasurement:
    measure: ReconciliationMeasure
    key: str
    currency: str | None
    actual: Decimal


class MigrationSink(Protocol):
    key: str
    groups: frozenset[str]                    # CIF bundle groups this sink accepts
    batch_size: int                           # at most MAX_BATCH_SIZE

    async def import_batch(self, context: SinkContext, records: Sequence[CIFSourceRecord]) -> SinkBatchResult: ...

    async def reconcile(
        self, context: SinkContext, expectations: ReconciliationExpectations
    ) -> list[DestinationMeasurement]: ...


class MissingSinkError(Exception):
    """A CIF group has no registered sink because its Celerp module is not installed."""

    def __init__(self, group: str) -> None:
        self.group = group
        self.module = SINK_MODULES[group]
        super().__init__(f"This migration needs the {self.module} module, which is not installed.")


_SINKS: dict[str, MigrationSink] = {}
_CORE_DIR = Path(__file__).resolve().parents[1]


class UntrustedSinkError(ValueError):
    """A sink offered by code that is not the bundled first-party module owning its groups."""


def _defining_module(sink: MigrationSink) -> str | None:
    """The Celerp module whose code defines `sink`: `celerp` for the core, else the bundled
    module folder holding its code, or None when that folder is not first-party by content."""
    from celerp.modules.loader import is_first_party

    origin = Path(type(sink).import_batch.__code__.co_filename).resolve()
    if origin.is_relative_to(_CORE_DIR):
        return "celerp"
    folder = origin.parent.parent
    return folder.name if is_first_party(folder) else None


def register_sink(sink: MigrationSink) -> None:
    unknown = sink.groups - SINK_MODULES.keys()
    if unknown:
        raise ValueError(f"Sink {sink.key!r} declares unknown CIF groups {sorted(unknown)}.")
    owners = {SINK_MODULES[g] for g in sink.groups}
    if owners != {sink.key} or _defining_module(sink) != sink.key:
        raise UntrustedSinkError(
            f"Sink {sink.key!r} is not the bundled first-party {'/'.join(sorted(owners))} module; "
            "only bundled Celerp modules can receive a migration."
        )
    if not 0 < sink.batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Sink {sink.key!r} batch size must be between 1 and {MAX_BATCH_SIZE}.")
    taken = {g for s in _SINKS.values() if s.key != sink.key for g in s.groups} & sink.groups
    if taken:
        raise ValueError(f"CIF groups {sorted(taken)} already have a sink.")
    _SINKS[sink.key] = sink


def sink_for(group: str) -> MigrationSink:
    """The sink that accepts `group`, or MissingSinkError naming the module to install."""
    for sink in _SINKS.values():
        if group in sink.groups:
            return sink
    raise MissingSinkError(group)

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
from collections import Counter
from collections.abc import Callable, Sequence
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
    # The content of one source attachment by its source id; blocking, raises ScanError.
    read_attachment: Callable[[str], bytes]

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


# How many records a batch failure or contract breach names; the rest are counted.
REPORTED_RECORD_LIMIT = 10


def record_identities(keys: Sequence[tuple[str, str]]) -> str:
    """Source identities (type and external id, never content) for a message, capped."""
    shown = ", ".join(f"{t} {e}" for t, e in keys[:REPORTED_RECORD_LIMIT])
    more = len(keys) - REPORTED_RECORD_LIMIT
    return f"{shown} and {more} more" if more > 0 else shown


class SinkContractError(Exception):
    """A sink result that does not report exactly one outcome for each record it was given."""


class MigrationBatchError(Exception):
    """One or more records of a batch could not be imported, so none of the batch is kept."""

    def __init__(self, errors: Sequence[SinkError]) -> None:
        self.errors = list(errors)
        reasons = "; ".join(f"{e.source_type} {e.source_external_id}: {e.message}"
                            for e in self.errors[:REPORTED_RECORD_LIMIT])
        more = len(self.errors) - REPORTED_RECORD_LIMIT
        count = f"{len(self.errors)} record{'s' if len(self.errors) != 1 else ''}"
        super().__init__(f"{count} could not be imported, so this batch was not saved. {reasons}"
                         + (f"; and {more} more." if more > 0 else "."))


def validate_batch_result(records: Sequence[CIFSourceRecord], result: SinkBatchResult) -> None:
    """Accept a batch only when the sink reported exactly one outcome per input record,
    keyed by source identity, and every outcome is a mapping to a Celerp record.

    Raises SinkContractError for a missing, duplicate, unexpected, conflicting or
    empty outcome, then MigrationBatchError when any record was rejected or failed."""
    expected = [(r.source_type, r.source_external_id) for r in records]
    inputs = set(expected)
    reported = Counter((o.source_type, o.source_external_id) for o in [*result.mappings, *result.errors])
    problems = []
    if missing := [k for k in expected if not reported[k]]:
        problems.append(f"no outcome for {record_identities(missing)}")
    if repeated := [k for k in expected if reported[k] > 1]:
        problems.append(f"more than one outcome for {record_identities(repeated)}")
    if unexpected := [k for k in reported if k not in inputs]:
        problems.append(f"an outcome for {record_identities(unexpected)}, which was not in the batch")
    if empty := [(m.source_type, m.source_external_id) for m in result.mappings
                 if m.status not in ("created", "skipped") or not m.target_entity_id]:
        problems.append(f"no Celerp record for {record_identities(empty)}")
    statuses = Counter(m.status for m in result.mappings)
    if (result.created, result.skipped) != (statuses["created"], statuses["skipped"]):
        problems.append("created and skipped counts that do not match its mappings")
    if problems:
        raise SinkContractError(f"The migration sink reported {'; '.join(problems)}.")
    if result.errors:
        raise MigrationBatchError(result.errors)


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
    from celerp.modules.loader import first_party_owner

    origin = Path(type(sink).import_batch.__code__.co_filename).resolve()
    if origin.is_relative_to(_CORE_DIR):
        return "celerp"
    return first_party_owner(origin)


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

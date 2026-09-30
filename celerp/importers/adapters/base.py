# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Source adapter contract.

An adapter recognises a source system's artifacts by content (never by file
extension alone), describes what they contain without writing anything, and
converts them into one CIF manifest plus independently computed source-side
reconciliation expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from celerp.importers.schema import (
    CIFCoverageEntry,
    CIFImportManifest,
    CIFMode,
    ReconciliationExpectations,
)


@dataclass(frozen=True)
class ArtifactSpec:
    """One kind of file a source accepts. Extensions are UI hints only."""
    key: str
    label: str
    extensions: tuple[str, ...]
    max_bytes: int


@dataclass(frozen=True)
class Artifact:
    """An uploaded file held in Celerp-owned storage."""
    path: Path
    original_name: str                        # sanitized basename, display only
    size_bytes: int
    sha256: str


ArtifactSet = list[Artifact]


class ScanError(Exception):
    """A source cannot be read. The message is shown to the user as written."""


class SourceRevisionError(Exception):
    """A source is recognised but saved at a file format revision this adapter does not
    read. Not a ScanError: detection must refuse it with its own message rather than
    treat the file as unrecognised. The message is shown to the user as written."""


@dataclass(frozen=True)
class DetectionResult:
    matched: bool
    reason: str | None = None                 # why the artifacts were not recognised, or a legacy-format note


@dataclass(frozen=True)
class MappingQuestion:
    """A genuinely ambiguous source-to-Celerp mapping the user must decide."""
    key: str                                  # stable decision key, stored in MigrationDecisions.mappings
    source_type: str
    source_external_id: str
    label: str
    options: tuple[str, ...]
    suggested: str | None = None


@dataclass(frozen=True)
class SourceScan:
    """Read-only description of a source, shown before anything is written."""
    source_system: str
    source_schema_version: str | None
    company_name: str | None
    base_currency: str | None
    period_start: date | None
    period_end: date | None
    currencies: tuple[str, ...]
    object_counts: dict[str, int]
    features: tuple[str, ...]
    coverage: tuple[CIFCoverageEntry, ...]
    questions: tuple[MappingQuestion, ...] = ()
    lock_date: date | None = None             # the source's accounting lock date, if it has one


@dataclass(frozen=True)
class MigrationDecisions:
    """The user's choices for one migration."""
    mode: CIFMode
    cutover_date: date | None = None
    mappings: dict[str, str] = field(default_factory=dict)
    prepared_by: str | None = None


class SourceAdapter(Protocol):
    key: str
    display_name: str
    artifact_specs: tuple[ArtifactSpec, ...]
    adapter_version: str

    def detect(self, artifacts: ArtifactSet) -> DetectionResult: ...

    def inspect(self, artifacts: ArtifactSet) -> SourceScan: ...

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest: ...

    def source_expectations(
        self, artifacts: ArtifactSet, decisions: MigrationDecisions
    ) -> ReconciliationExpectations: ...

    def read_attachment(self, artifacts: ArtifactSet, key: str) -> bytes:
        """The content of one attachment the manifest carries, by its source id.
        Raises ScanError for any key the scan did not accept."""
        ...

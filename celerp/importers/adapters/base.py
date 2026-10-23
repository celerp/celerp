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
    """A source is recognised but cannot be read as it is: saved at a file format revision
    this adapter does not read, or damaged. Not a ScanError: detection must refuse it with its own message rather than
    treat the file as unrecognised. The message is shown to the user as written."""


# What converting a damaged source value raises, in any reader: a number that is not one
# (decimal's ArithmeticError), a date, text or structure that does not parse, a value of another
# shape than the source writes, a reference to a record that is not there, an empty value where
# the source requires one, or a value nested past the parser's depth. A reader that meets one
# refuses the source as damaged, and nothing read from it is kept.
CONVERSION_ERRORS = (ArithmeticError, ValueError, TypeError, KeyError, AttributeError, RecursionError)


@dataclass(frozen=True)
class DetectionResult:
    matched: bool
    reason: str | None = None                 # why the artifacts were not recognised, or a legacy-format note


# A question with this key chooses which of the source's companies to import; its answer is
# the company's name.
COMPANY_QUESTION = "company"


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
    # Which versions of the source's files the adapter reads and how to get an older file
    # there, shown where the import says what it accepts.
    accepts: str

    def detect(self, artifacts: ArtifactSet) -> DetectionResult: ...

    def inspect(self, artifacts: ArtifactSet, company: str | None = None) -> SourceScan:
        """The scan of a source. For a source that asks the COMPANY_QUESTION, `company` is the
        answer the scan describes (None describes the suggested one); others ask nothing and
        ignore it."""
        ...

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest: ...

    def source_expectations(
        self, artifacts: ArtifactSet, decisions: MigrationDecisions
    ) -> ReconciliationExpectations: ...

    def read_attachment(self, artifacts: ArtifactSet, decisions: MigrationDecisions, key: str) -> bytes:
        """The content of one attachment the manifest built with these decisions carries, by
        its source id. Raises ScanError for any key that manifest does not carry."""
        ...

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Export-set source adapters: one export set in, one CIF manifest out. Each source
names its reader, the records its reader counts but does not carry, and its display name."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from celerp.importers.adapters.base import (
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MigrationDecisions,
    ScanError,
    SourceScan,
)
from celerp.importers.adapters.exports import files, qbo, xero
from celerp.importers.adapters.exports.book import Book
from celerp.importers.adapters.exports.bundle import Source, build_bundle, coverage
from celerp.importers.adapters.exports.ledger import Ledger, build_ledger
from celerp.importers.adapters.exports.reconcile import expectations_from
from celerp.importers.schema import (
    CIFCoverageEntry,
    CIFImportManifest,
    CIFMode,
    CoverageClass,
    ReconciliationExpectations,
)
from ui.i18n import t

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)


class ExportSetAdapter:
    """One source read from its export set. A subclass names the source."""
    key: str
    display_name: str
    accepts_key: str
    read: Callable[[ArtifactSet], Book]            # the source's reader
    matches: Callable[[ArtifactSet], bool]         # whether any uploaded file is laid out as this source's
    check: Callable[[ArtifactSet], files.SetCheck]  # the upload against the source's export checklist
    adapter_version = "1"
    # Records the export counts but no migration carries, by coverage label.
    skipped: dict[str, CoverageClass] = {}

    @property
    def artifact_specs(self) -> tuple[ArtifactSpec, ...]:
        return (ArtifactSpec("export_set", f"{self.display_name} export files", (".csv", ".txt", ".xlsx", ".zip"),
                             files.LIMITS.file_bytes),)

    @property
    def accepts(self) -> str:
        return t(self.accepts_key)

    def checklist(self) -> files.SetCheck:
        """Every file the source exports for a migration, before any is uploaded."""
        return self.check([])

    def _read(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> tuple[Book, Ledger]:
        book = self.read(artifacts)
        return book, build_ledger(book, decisions)

    def _coverage(self, ledger: Ledger) -> list[CIFCoverageEntry]:
        return coverage(ledger, self.skipped)

    def detect(self, artifacts: ArtifactSet) -> DetectionResult:
        return DetectionResult(self.matches(artifacts))

    def inspect(self, artifacts: ArtifactSet) -> SourceScan:
        book, ledger = self._read(artifacts, FULL)
        dated = sorted(j.date for j in book.journals)
        features = [name for name, present in (
            ("foreign_currency", any(book.is_foreign(c) for c in book.currencies)),
            ("inventory", bool(book.items)), ("tax", bool(book.tax_codes)),
        ) if present]
        return SourceScan(
            source_system=self.key, source_schema_version=None,
            company_name=book.company_name or None, base_currency=book.base,
            period_start=dated[0] if dated else None, period_end=dated[-1] if dated else None,
            currencies=tuple(sorted(book.currencies)), object_counts=dict(book.object_counts),
            features=tuple(features), coverage=tuple(self._coverage(ledger)),
        )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        book, ledger = self._read(artifacts, decisions)
        return CIFImportManifest(
            source=book.company_name or artifacts[0].original_name,
            source_system=self.key,
            source_schema_version=None,
            adapter_version=self.adapter_version,
            mode=decisions.mode,
            cutover_date=decisions.cutover_date if decisions.mode == CIFMode.CUTOVER else None,
            exported_at=datetime.now(timezone.utc),
            bundle=build_bundle(ledger, Source(self.key)),
            coverage=self._coverage(ledger),
            reconciliation_expectations=expectations_from(book, ledger),
        )

    def source_expectations(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> ReconciliationExpectations:
        return expectations_from(*self._read(artifacts, decisions))

    def read_attachment(self, artifacts: ArtifactSet, decisions: MigrationDecisions, key: str) -> bytes:
        """An export set carries no attachments."""
        raise ScanError("No such attachment in this export set.")


class XeroAdapter(ExportSetAdapter):
    key = xero.SOURCE_SYSTEM
    display_name = xero.SOURCE_NAME
    accepts_key = "migration.xero_accepts"
    skipped = {xero.DELETED: CoverageClass.IGNORED_NON_BUSINESS}
    read = staticmethod(xero.read_xero)
    matches = staticmethod(xero.matches)
    check = staticmethod(xero.check)


class QuickBooksOnlineAdapter(ExportSetAdapter):
    key = qbo.SOURCE_SYSTEM
    display_name = qbo.SOURCE_NAME
    accepts_key = "migration.qbo_accepts"
    read = staticmethod(qbo.read_qbo)
    matches = staticmethod(qbo.matches)
    check = staticmethod(qbo.check)

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Xero source adapter: one export set in, one CIF manifest out."""

from __future__ import annotations

from datetime import datetime, timezone

from celerp.importers.adapters.base import (
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MigrationDecisions,
    ScanError,
    SourceScan,
)
from celerp.importers.adapters.exports import files
from celerp.importers.adapters.exports.book import Book
from celerp.importers.adapters.exports.bundle import Source, build_bundle, coverage
from celerp.importers.adapters.exports.ledger import Ledger, build_ledger
from celerp.importers.adapters.exports.reconcile import expectations_from
from celerp.importers.adapters.exports.xero import DELETED, SOURCE_SYSTEM, matches, read_xero
from celerp.importers.schema import (
    CIFCoverageEntry,
    CIFImportManifest,
    CIFMode,
    CoverageClass,
    ReconciliationExpectations,
)
from ui.i18n import t

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
# Records the export counts but no migration carries: deleted invoices and bills.
SKIPPED = {DELETED: CoverageClass.IGNORED_NON_BUSINESS}


def _coverage(ledger: Ledger) -> list[CIFCoverageEntry]:
    return coverage(ledger, SKIPPED)


def _read(artifacts: ArtifactSet, decisions: MigrationDecisions) -> tuple[Book, Ledger]:
    book = read_xero(artifacts)
    return book, build_ledger(book, decisions)


class XeroAdapter:
    key = SOURCE_SYSTEM
    display_name = "Xero"
    adapter_version = "1"
    artifact_specs = (ArtifactSpec("export_set", "Xero export files", (".csv", ".txt", ".xlsx", ".zip"),
                                   files.LIMITS.file_bytes),)

    @property
    def accepts(self) -> str:
        return t("migration.xero_accepts")

    def detect(self, artifacts: ArtifactSet) -> DetectionResult:
        return DetectionResult(matches(artifacts))

    def inspect(self, artifacts: ArtifactSet) -> SourceScan:
        book, ledger = _read(artifacts, FULL)
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
            features=tuple(features), coverage=tuple(_coverage(ledger)),
        )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        book, ledger = _read(artifacts, decisions)
        return CIFImportManifest(
            source=book.company_name or artifacts[0].original_name,
            source_system=self.key,
            source_schema_version=None,
            adapter_version=self.adapter_version,
            mode=decisions.mode,
            cutover_date=decisions.cutover_date if decisions.mode == CIFMode.CUTOVER else None,
            exported_at=datetime.now(timezone.utc),
            bundle=build_bundle(ledger, Source(self.key)),
            coverage=_coverage(ledger),
            reconciliation_expectations=expectations_from(book, ledger),
        )

    def source_expectations(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> ReconciliationExpectations:
        return expectations_from(*_read(artifacts, decisions))

    def read_attachment(self, artifacts: ArtifactSet, key: str) -> bytes:
        """An export set carries no attachments."""
        raise ScanError("No such attachment in this export set.")

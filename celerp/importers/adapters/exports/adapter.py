# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Export-set source adapters: one export set in, one CIF manifest out. Each source
names its reader, the records its reader counts but does not carry, and its display name.

Every step that reads the set passes one conversion boundary: a value the reader cannot
convert (CONVERSION_ERRORS) refuses the set as damaged, in the user's language, never as a
server error."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

from celerp.importers.adapters.base import (
    CONVERSION_ERRORS,
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MappingQuestion,
    MigrationDecisions,
    ScanError,
    SourceRevisionError,
    SourceScan,
)
from celerp.importers.adapters.exports import files, qbo, sage50, xero
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
DAMAGED = "migration.err_export_damaged"
T = TypeVar("T")


class ExportSetAdapter:
    """One source read from its export set. A subclass names the source."""
    key: str
    display_name: str
    accepts_key: str
    read: Callable[[ArtifactSet], Book]            # the source's reader
    matches: Callable[[ArtifactSet], bool]         # whether any uploaded file is laid out as this source's
    check_files: Callable[[ArtifactSet], files.SetCheck]  # the upload against the source's export checklist
    adapter_version = "1"
    # Records the export counts but no migration carries, by coverage label, and the catalog
    # note that says why for any of them.
    skipped: dict[str, CoverageClass] = {}
    notes: dict[str, str] = {}

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

    def _guarded(self, step: Callable[[], T]) -> T:
        """The conversion boundary every step that reads the set passes."""
        try:
            return step()
        except CONVERSION_ERRORS as exc:
            raise SourceRevisionError(t(DAMAGED, source=self.display_name)) from exc

    def _book(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> Book:
        """The set's books as the decisions read them. A source that asks a question reads its answer here."""
        return self.read(artifacts)

    def _questions(self, book: Book) -> tuple[MappingQuestion, ...]:
        """What the user must decide about the set before it is imported; most sources ask nothing."""
        return ()

    def _read(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> tuple[Book, Ledger]:
        def read() -> tuple[Book, Ledger]:
            book = self._book(artifacts, decisions)
            return book, build_ledger(book, decisions)
        return self._guarded(read)

    def _coverage(self, ledger: Ledger) -> list[CIFCoverageEntry]:
        return coverage(ledger, self.skipped, self.notes)

    def detect(self, artifacts: ArtifactSet) -> DetectionResult:
        return DetectionResult(self._guarded(lambda: self.matches(artifacts)))

    def check(self, artifacts: ArtifactSet) -> files.SetCheck:
        """The upload against the source's export checklist."""
        return self._guarded(lambda: self.check_files(artifacts))

    def inspect(self, artifacts: ArtifactSet, company: str | None = None) -> SourceScan:
        book, ledger = self._read(artifacts, FULL)
        dated = sorted(j.date for j in book.journals)
        features = [name for name, present in (
            ("foreign_currency", any(book.is_foreign(c) for c in book.currencies)),
            ("inventory", bool(book.items)), ("tax", bool(book.tax_codes)),
        ) if present]
        return SourceScan(
            source_system=self.key, source_schema_version=None,
            company_name=book.company_name or None, base_currency=book.base or None,
            period_start=dated[0] if dated else None, period_end=dated[-1] if dated else None,
            currencies=tuple(sorted(book.currencies)), object_counts=dict(book.object_counts),
            features=tuple(features), coverage=tuple(self._coverage(ledger)), questions=self._questions(book),
        )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        book, ledger = self._read(artifacts, decisions)
        return self._guarded(lambda: self._manifest(artifacts, decisions, book, ledger))

    def _manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions, book: Book,
                  ledger: Ledger) -> CIFImportManifest:
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
        book, ledger = self._read(artifacts, decisions)
        return self._guarded(lambda: expectations_from(book, ledger))

    def read_attachment(self, artifacts: ArtifactSet, decisions: MigrationDecisions, key: str) -> bytes:
        """An export set carries no attachments."""
        raise ScanError(t("migration.err_attachment_not_found"))


class XeroAdapter(ExportSetAdapter):
    key = xero.SOURCE_SYSTEM
    display_name = xero.SOURCE_NAME
    accepts_key = "migration.xero_accepts"
    skipped = {xero.DELETED: CoverageClass.IGNORED_NON_BUSINESS}
    read = staticmethod(xero.read_xero)
    matches = staticmethod(xero.matches)
    check_files = staticmethod(xero.check)


class QuickBooksOnlineAdapter(ExportSetAdapter):
    key = qbo.SOURCE_SYSTEM
    display_name = qbo.SOURCE_NAME
    accepts_key = "migration.qbo_accepts"
    read = staticmethod(qbo.read_qbo)
    matches = staticmethod(qbo.matches)
    check_files = staticmethod(qbo.check)


class Sage50Adapter(ExportSetAdapter):
    key = sage50.SOURCE_SYSTEM
    display_name = sage50.SOURCE_NAME
    accepts_key = "migration.sage50_accepts"
    skipped = {sage50.EMPLOYEE: CoverageClass.UNSUPPORTED_NONFINANCIAL, sage50.STOCK: CoverageClass.UNSUPPORTED_NONFINANCIAL,
               sage50.UNASSIGNED_TAX: CoverageClass.MAPPED_WITH_LOSS}
    notes = {sage50.EMPLOYEE: "migration.sage50_employee_note", sage50.STOCK: "migration.sage50_stock_note",
             sage50.UNASSIGNED_TAX: "migration.sage50_tax_unassigned_note"}
    matches = staticmethod(sage50.matches)
    check_files = staticmethod(sage50.check)

    def _book(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> Book:
        return sage50.read_sage50(artifacts, decisions.mappings.get(sage50.CURRENCY_QUESTION))

    def _questions(self, book: Book) -> tuple[MappingQuestion, ...]:
        return (sage50.question(book.base or None),)

    def _coverage(self, ledger: Ledger) -> list[CIFCoverageEntry]:
        """The shared coverage, and each journal not uploaded with what leaving it out means."""
        absent = [CIFCoverageEntry(source_type=sage50.ABSENT.format(layout.label), count=0,
                                   coverage_class=CoverageClass.UNSUPPORTED_NONFINANCIAL,
                                   note=t(sage50.ABSENT_NOTES[layout.kind]))
                  for layout in sage50.JOURNALS if layout.kind in ledger.book.absent]
        return sorted(super()._coverage(ledger) + absent, key=lambda r: r.source_type)

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        sage50.chosen_currency(decisions)
        return super().build_manifest(artifacts, decisions)

    def source_expectations(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> ReconciliationExpectations:
        sage50.chosen_currency(decisions)
        return super().source_expectations(artifacts, decisions)

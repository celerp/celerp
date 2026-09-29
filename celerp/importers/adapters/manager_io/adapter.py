# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Manager.io source adapter: one business file in, one CIF manifest out."""

from __future__ import annotations

from datetime import datetime, timezone

from celerp.importers.adapters.base import (
    Artifact,
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MigrationDecisions,
    ScanError,
    SourceScan,
)
from celerp.importers.adapters.manager_io import attachments
from celerp.importers.adapters.manager_io.book import Book, read_book
from celerp.importers.adapters.manager_io.ledger import build_ledger
from celerp.importers.adapters.manager_io.mappings import SOURCE_SYSTEM, build_bundle, carried
from celerp.importers.adapters.manager_io.reconcile import expectations_from
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
from celerp.importers.schema import CIFImportManifest, CIFMode, ReconciliationExpectations

MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024


def _single(artifacts: ArtifactSet) -> Artifact:
    if len(artifacts) != 1:
        raise ScanError("Upload exactly one Manager business file.")
    return artifacts[0]


def _refuse_blockers(book: Book) -> None:
    blocking = book.blocking_rows
    if blocking:
        listed = ", ".join(f"{row.source_type} ({row.count})" for row in blocking)
        raise ScanError(f"This company cannot be migrated yet: {listed}.")


class ManagerIOAdapter:
    key = SOURCE_SYSTEM
    display_name = "Manager.io"
    adapter_version = "1"
    artifact_specs = (ArtifactSpec("business_file", "Manager business file", (".manager",), MAX_FILE_BYTES),)

    def detect(self, artifacts: ArtifactSet) -> DetectionResult:
        try:
            with ManagerReader(_single(artifacts).path):
                return DetectionResult(True)
        except ScanError as exc:
            return DetectionResult(False, str(exc))

    def inspect(self, artifacts: ArtifactSet) -> SourceScan:
        with ManagerReader(_single(artifacts).path) as reader:
            book = read_book(reader)
            attachments.screen(book, reader)
        dated = book.dated_records()
        features = [name for name, present in (
            ("foreign_currency", any(book.is_foreign(c) for c in book.currencies)),
            ("inventory", bool(book.items)), ("tax", bool(book.tax_codes)),
            ("attachments", bool(book.attachments)),
        ) if present]
        return SourceScan(
            source_system=self.key,
            source_schema_version=None if book.schema_version is None else str(book.schema_version),
            company_name=book.company_name, base_currency=book.base_code,
            period_start=dated[0][0] if dated else None, period_end=dated[-1][0] if dated else None,
            currencies=tuple(sorted({book.base_code or "", *(c.code for c in book.currencies.values())} - {""})),
            object_counts=dict(book.object_counts), features=tuple(features), coverage=tuple(book.coverage()),
        )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        artifact = _single(artifacts)
        with ManagerReader(artifact.path) as reader:
            book = read_book(reader)
            screened = attachments.screen(book, reader)
        _refuse_blockers(book)
        ledger = build_ledger(book, decisions)
        attachments.drop_uncarried(book, screened, carried(book, ledger))
        summary: dict = {
            "audit_history": dict(book.history),
            "attachments": {
                "accepted": len(screened.accepted),
                "rejected": [{"source_external_id": key, "reason": reason}
                             for key, reason in sorted(screened.rejected.items())],
            },
        }
        if any(book.is_foreign(c) for c in book.currencies):
            summary["fx"] = {"realized": str(ledger.realized), "unrealized": ledger.unrealized}
        return CIFImportManifest(
            source=book.company_name or artifact.original_name,
            source_system=self.key,
            source_schema_version=None if book.schema_version is None else str(book.schema_version),
            adapter_version=self.adapter_version,
            mode=decisions.mode,
            cutover_date=decisions.cutover_date if decisions.mode == CIFMode.CUTOVER else None,
            exported_at=datetime.now(timezone.utc),
            bundle=build_bundle(book, ledger, screened),
            coverage=book.coverage(),
            source_summary=summary,
            reconciliation_expectations=expectations_from(book, ledger),
        )

    def source_expectations(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> ReconciliationExpectations:
        with ManagerReader(_single(artifacts).path) as reader:
            book = read_book(reader)
        _refuse_blockers(book)
        return expectations_from(book, build_ledger(book, decisions))

    def read_attachment(self, artifacts: ArtifactSet, key: str) -> bytes:
        """The content of one attachment the scan accepts. Raises ScanError for any other key."""
        with ManagerReader(_single(artifacts).path) as reader:
            return attachments.read_attachment(read_book(reader), reader, key)

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Odoo source adapter: one database manager backup in, one CIF manifest out.

The books are read into the shared `Book` and carried by the same ledger, bundle and
reconciliation as the export sets; the stock moves and attached files the backup holds
are added to the bundle here."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from celerp.importers.adapters.base import (
    Artifact,
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MigrationDecisions,
    ScanError,
    SourceScan,
)
from celerp.importers.adapters.exports.bundle import Source, build_bundle, coverage
from celerp.importers.adapters.exports.ledger import Ledger, build_ledger
from celerp.importers.adapters.exports.reconcile import expectations_from
from celerp.importers.adapters.odoo import attachments
from celerp.importers.adapters.odoo.book import (
    DRAFT_ENTRY, LOT, NO_CONTACT, OFF_BALANCE_LINE, SOURCE_NAME, SOURCE_SYSTEM, AttachmentRef, OdooBook,
    read_odoo,
)
from celerp.importers.adapters.odoo.dump import Backup
from celerp.importers.schema import (
    CIFAttachment,
    CIFCoverageEntry,
    CIFImportManifest,
    CIFInventoryAdjustment,
    CIFMode,
    CoverageClass,
    ReconciliationExpectations,
)
from ui.i18n import t

MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
SKIPPED = {DRAFT_ENTRY: CoverageClass.IGNORED_NON_BUSINESS, NO_CONTACT: CoverageClass.IGNORED_NON_BUSINESS,
           OFF_BALANCE_LINE: CoverageClass.UNSUPPORTED_NONFINANCIAL, LOT: CoverageClass.UNSUPPORTED_NONFINANCIAL}
LOCATION = "Stock location"
LOCATION_NOTE = "Celerp keeps each item at one location; the stock of every internal location is added together."
OPENING = "Opening balances"


def _single(artifacts: ArtifactSet) -> Artifact:
    if len(artifacts) != 1:
        raise ScanError(t("migration.err_odoo_not_backup"))
    return artifacts[0]


def _read(artifacts: ArtifactSet, decisions: MigrationDecisions) -> tuple[OdooBook, Ledger, attachments.Screened]:
    with Backup(_single(artifacts).path) as backup:
        odoo = read_odoo(backup)
        ledger = build_ledger(odoo.book, decisions)
        screened = attachments.screen(backup, odoo.attachments, _carried(ledger))
    return odoo, ledger, screened


@lru_cache(maxsize=4)
def _attachment_refs(path: Path, sha256: str) -> dict[str, AttachmentRef]:
    """The files one backup holds, read once per backup however many the import fetches."""
    with Backup(path) as backup:
        return read_odoo(backup).attachments


def _carried(ledger: Ledger) -> set[tuple[str, str]]:
    """The records the migration imports that can hold files, as (source type, id)."""
    book = ledger.book
    return {*((book.documents[k].source_type, k) for k in ledger.documents),
            *(("Contact", name) for name in book.contacts), *(("Item", code) for code in book.items)}


def _stock(odoo: OdooBook, ledger: Ledger) -> list[CIFInventoryAdjustment]:
    """Each stock move and valuation in the history; on a cutover, the position each item held
    at the cutover date, then the moves after it."""
    def adjustment(source_type: str, key: str, ref: str | None, kind: str, on, item: str, quantity: Decimal,
                   value: Decimal | None) -> CIFInventoryAdjustment:
        return CIFInventoryAdjustment(source_system=SOURCE_SYSTEM, source_type=source_type, source_external_id=key,
                                      source_ref=ref or None, kind=kind, adjustment_date=on, item_external_id=item,
                                      quantity=quantity, value=value)

    cutover = ledger.cutover
    held: dict[str, tuple[Decimal, Decimal | None]] = {}
    for m in odoo.moves:
        if cutover is not None and m.date <= cutover:
            quantity, value = held.get(m.item, (Decimal(0), None if m.value is None else Decimal(0)))
            held[m.item] = (quantity + m.quantity, None if value is None or m.value is None else value + m.value)
    opening = [adjustment(OPENING, f"{ledger.opening_key}:{item}", None, "opening", cutover, item, quantity, value)
               for item, (quantity, value) in sorted(held.items()) if quantity or value]
    return opening + [adjustment(m.source_type, m.key, m.ref, "adjustment", m.date, m.item, m.quantity, m.value)
                      for m in odoo.moves if cutover is None or m.date > cutover]


def _coverage(odoo: OdooBook, ledger: Ledger, screened: attachments.Screened) -> list[CIFCoverageEntry]:
    rows = coverage(ledger, SKIPPED)
    moved: dict[str, int] = defaultdict(int)
    for m in odoo.moves:
        moved[m.source_type] += 1
    rows += [CIFCoverageEntry(source_type=label, count=count, coverage_class=CoverageClass.MAPPED)
             for label, count in moved.items()]
    if odoo.locations > 1:
        rows.append(CIFCoverageEntry(source_type=LOCATION, count=odoo.locations,
                                     coverage_class=CoverageClass.MAPPED_WITH_LOSS, note=LOCATION_NOTE))
    if screened.accepted:
        rows.append(CIFCoverageEntry(source_type="Attachment", count=len(screened.accepted),
                                     coverage_class=CoverageClass.MAPPED))
    if screened.rejected:
        rows.append(CIFCoverageEntry(source_type=attachments.REJECTED, count=len(screened.rejected),
                                     coverage_class=CoverageClass.MAPPED_WITH_LOSS, note=attachments.REJECTED_NOTE))
    if odoo.system_files:
        rows.append(CIFCoverageEntry(source_type=attachments.IGNORED, count=odoo.system_files,
                                     coverage_class=CoverageClass.IGNORED_NON_BUSINESS, note=attachments.IGNORED_NOTE))
    return sorted(rows, key=lambda r: r.source_type)


class OdooAdapter:
    key = SOURCE_SYSTEM
    display_name = SOURCE_NAME
    adapter_version = "1"
    artifact_specs = (ArtifactSpec("backup", "Odoo backup", (".zip",), MAX_FILE_BYTES),)

    @property
    def accepts(self) -> str:
        return t("migration.odoo_accepts")

    def detect(self, artifacts: ArtifactSet) -> DetectionResult:
        try:
            with Backup(_single(artifacts).path):
                return DetectionResult(True)
        except ScanError as exc:
            return DetectionResult(False, str(exc))

    def inspect(self, artifacts: ArtifactSet) -> SourceScan:
        odoo, ledger, screened = _read(artifacts, FULL)
        book = odoo.book
        dated = sorted([j.date for j in book.journals] + [d.date for d in book.documents.values()]
                       + [m.date for m in odoo.moves])
        features = [name for name, present in (
            ("foreign_currency", any(book.is_foreign(c) for c in book.currencies)),
            ("inventory", bool(odoo.moves or book.stock)), ("tax", bool(book.tax_codes)),
            ("attachments", bool(screened.accepted)),
        ) if present]
        return SourceScan(
            source_system=self.key, source_schema_version=str(odoo.version),
            company_name=book.company_name or None, base_currency=book.base or None,
            period_start=dated[0] if dated else None, period_end=dated[-1] if dated else None,
            currencies=book.currencies, object_counts=dict(book.object_counts), features=tuple(features),
            coverage=tuple(_coverage(odoo, ledger, screened)),
        )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        odoo, ledger, screened = _read(artifacts, decisions)
        book = odoo.book
        bundle = build_bundle(ledger, Source(self.key))
        bundle.inventory_adjustments = _stock(odoo, ledger)
        bundle.attachments = [
            CIFAttachment(**Source(self.key)("Attachment", a.key), file_name=a.name,
                          declared_content_type=a.content_type, size_bytes=a.size, sha256=a.sha256,
                          target_source_type=a.target_type, target_source_external_id=a.target,
                          item_image=a.item_image)
            for a in screened.accepted.values()]
        return CIFImportManifest(
            source=book.company_name or _single(artifacts).original_name,
            source_system=self.key,
            source_schema_version=str(odoo.version),
            adapter_version=self.adapter_version,
            mode=decisions.mode,
            cutover_date=decisions.cutover_date if decisions.mode == CIFMode.CUTOVER else None,
            exported_at=datetime.now(timezone.utc),
            bundle=bundle,
            coverage=_coverage(odoo, ledger, screened),
            source_summary={"attachments": {
                "accepted": len(screened.accepted),
                "rejected": [{"source_external_id": key, "reason": reason}
                             for key, reason in sorted(screened.rejected.items())],
            }},
            reconciliation_expectations=expectations_from(book, ledger),
        )

    def source_expectations(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> ReconciliationExpectations:
        odoo, ledger, _ = _read(artifacts, decisions)
        return expectations_from(odoo.book, ledger)

    def read_attachment(self, artifacts: ArtifactSet, key: str) -> bytes:
        """The content of one file the scan accepts. Raises ScanError for any other key."""
        artifact = _single(artifacts)
        with Backup(artifact.path) as backup:
            return attachments.read_attachment(backup, _attachment_refs(artifact.path, artifact.sha256), key)

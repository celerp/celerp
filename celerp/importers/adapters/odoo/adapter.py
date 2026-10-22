# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Odoo source adapter: one database manager backup in, one CIF manifest out.

The books are read into the shared `Book` and carried by the same ledger, bundle and
reconciliation as the export sets; the stock moves and attached files the backup holds
are added to the bundle here."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from celerp.importers.adapters.base import (
    COMPANY_QUESTION,
    Artifact,
    ArtifactSet,
    ArtifactSpec,
    DetectionResult,
    MappingQuestion,
    MigrationDecisions,
    ScanError,
    SourceRevisionError,
    SourceScan,
)
from celerp.importers.adapters.exports.bundle import Source, build_bundle, coverage
from celerp.importers.adapters.exports.ledger import Ledger, build_ledger
from celerp.importers.adapters.exports.reconcile import expectations_from
from celerp.importers.adapters.odoo import attachments
from celerp.importers.adapters.odoo.book import (
    DRAFT_ENTRY, LOT, NO_CONTACT, OFF_BALANCE_LINE, SOURCE_NAME, SOURCE_SYSTEM, UNSTOCKED_LOT, OdooBook,
    read_odoo,
)
from celerp.importers.adapters.odoo.dump import Backup, freed, too_large
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
           OFF_BALANCE_LINE: CoverageClass.UNSUPPORTED_NONFINANCIAL, UNSTOCKED_LOT: CoverageClass.IGNORED_NON_BUSINESS}
LOCATION = "Stock location"
OPENING = "Opening balances"
OTHER_COMPANY = "Entry of another company"
OTHER_COMPANY_NOTE = "migration.odoo_other_company_note"


def _single(artifacts: ArtifactSet) -> Artifact:
    if len(artifacts) != 1:
        raise ScanError(t("migration.err_odoo_not_backup"))
    return artifacts[0]


_READING = threading.Lock()


@lru_cache(maxsize=1)
def _books(path: Path, sha256: str, company: str | None) -> OdooBook:
    """The books of one company of a backup, read from the dump once for every step of a run
    (the scan, the manifest, the expectations and each file the import fetches)."""
    with Backup(path) as backup:
        return read_odoo(backup, company)


@contextmanager
def _opened(artifacts: ArtifactSet, decisions: MigrationDecisions) -> Iterator[tuple[OdooBook, Backup]]:
    """The book of the chosen company and its backup, open, for every step that reads them.
    Running out of memory anywhere while they are used, in the book or in what is built from
    it, refuses the backup as too large, as the reader does, and the refused book is not kept."""
    artifact = _single(artifacts)
    try:
        with Backup(artifact.path) as backup:
            yield _books(artifact.path, artifact.sha256, decisions.mappings.get(COMPANY_QUESTION)), backup
    except MemoryError as exc:
        freed(exc)
    else:
        return
    _books.cache_clear()
    raise too_large(SourceRevisionError)


@contextmanager
def _read(artifacts: ArtifactSet,
          decisions: MigrationDecisions) -> Iterator[tuple[OdooBook, Ledger, attachments.Screened]]:
    """The book, its ledger for these decisions and the files it carries. Every run on a backup
    shares its book and rebuilds the ledger on it, so one run at a time reads and uses them."""
    with _READING, _opened(artifacts, decisions) as (odoo, backup):
        ledger = build_ledger(odoo.book, decisions)
        screened = attachments.screen(backup, odoo.attachments, _carried(ledger))
        yield odoo, ledger, screened


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
    moved: dict[str, set[str]] = defaultdict(set)
    for m in odoo.moves:
        moved[m.source_type].add(m.record)
    carried = {**{label: len(records) for label, records in moved.items()}, LOCATION: odoo.locations, LOT: odoo.lots}
    rows += [CIFCoverageEntry(source_type=label, count=count, coverage_class=CoverageClass.MAPPED)
             for label, count in carried.items() if count]
    if screened.accepted:
        rows.append(CIFCoverageEntry(source_type="Attachment", count=len(screened.accepted),
                                     coverage_class=CoverageClass.MAPPED))
    if screened.rejected:
        rows.append(CIFCoverageEntry(source_type=attachments.REJECTED, count=len(screened.rejected),
                                     coverage_class=CoverageClass.MAPPED_WITH_LOSS, note=t(attachments.REJECTED_NOTE)))
    if odoo.other_entries:
        rows.append(CIFCoverageEntry(source_type=OTHER_COMPANY, count=odoo.other_entries,
                                     coverage_class=CoverageClass.IGNORED_NON_BUSINESS, note=t(OTHER_COMPANY_NOTE)))
    if odoo.system_files:
        rows.append(CIFCoverageEntry(source_type=attachments.IGNORED, count=odoo.system_files,
                                     coverage_class=CoverageClass.IGNORED_NON_BUSINESS, note=t(attachments.IGNORED_NOTE)))
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

    def inspect(self, artifacts: ArtifactSet, company: str | None = None) -> SourceScan:
        decisions = FULL if company is None else MigrationDecisions(FULL.mode, mappings={COMPANY_QUESTION: company})
        with _read(artifacts, decisions) as (odoo, ledger, screened):
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
                questions=(MappingQuestion(
                    key=COMPANY_QUESTION, source_type="Company", source_external_id="company",
                    label=t("migration.odoo_company_question"), options=odoo.companies,
                    suggested=company or odoo.companies[0]),) if odoo.companies else (),
            )

    def build_manifest(self, artifacts: ArtifactSet, decisions: MigrationDecisions) -> CIFImportManifest:
        with _read(artifacts, decisions) as (odoo, ledger, screened):
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
        with _read(artifacts, decisions) as (odoo, ledger, _):
            return expectations_from(odoo.book, ledger)

    def read_attachment(self, artifacts: ArtifactSet, decisions: MigrationDecisions, key: str) -> bytes:
        """The content of one file of the chosen company that the scan accepts. Raises
        ScanError for any other key."""
        with _opened(artifacts, decisions) as (odoo, backup):
            return attachments.read_attachment(backup, odoo.attachments, key)

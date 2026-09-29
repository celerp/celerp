# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager.io adapter: detection, safe reading, bounded decoding, attachments and mapping semantics."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.encoder import Blob, Obj, write_manager_file
from fixtures.manager_io.support import BASIC, CHECKPOINTS, FX, adapter, artifact, ref

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)


def _coverage(scan_or_manifest) -> dict[str, tuple[int, CoverageClass, str | None]]:
    return {row.source_type: (row.count, row.coverage_class, row.note) for row in scan_or_manifest.coverage}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manager_rejects_corrupt_or_non_manager_sqlite(tmp_path):
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader

    manager = adapter()

    # Content decides, not the extension: a Manager file renamed .sqlite is recognised.
    renamed = tmp_path / "books.sqlite"
    shutil.copyfile(BASIC, renamed)
    assert manager.detect([artifact(renamed)]).matched is True

    # An ordinary SQLite database renamed .manager is not.
    other = tmp_path / "other.manager"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.commit()
    conn.close()
    result = manager.detect([artifact(other)])
    assert result.matched is False
    with pytest.raises(ScanError, match="not a Manager business file"):
        manager.inspect([artifact(other)])

    # An Objects table without Manager's schema object is not a Manager file either.
    lookalike = tmp_path / "lookalike.manager"
    conn = sqlite3.connect(lookalike)
    conn.execute('CREATE TABLE "Objects" ("Key" TEXT PRIMARY KEY, "ContentType" TEXT, "Content" BLOB, "Timestamp" INTEGER)')
    conn.commit()
    conn.close()
    assert manager.detect([artifact(lookalike)]).matched is False
    with pytest.raises(ScanError, match="not a Manager business file"):
        manager.inspect([artifact(lookalike)])

    # A damaged database fails quick_check and is rejected, never partly read.
    damaged = tmp_path / "damaged.manager"
    data = bytearray(BASIC.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    data[page_size:] = b"\xa5" * (len(data) - page_size)
    damaged.write_bytes(bytes(data))
    assert manager.detect([artifact(damaged)]).matched is False
    with pytest.raises(ScanError, match="damaged"):
        manager.inspect([artifact(damaged)])

    # The older non-SQLite format is recognised as such and the user is told how to resave it.
    legacy = tmp_path / "old.manager"
    legacy.write_bytes(b"MNGR|" + b"\x00" * 64)
    result = manager.detect([artifact(legacy)])
    assert result.matched is False
    assert "older Manager format" in (result.reason or "")
    with pytest.raises(ScanError, match="save it, and upload the saved file"):
        manager.inspect([artifact(legacy)])

    # Not SQLite at all.
    text = tmp_path / "notes.manager"
    text.write_bytes(b"plain text, not a database")
    assert manager.detect([artifact(text)]).matched is False

    # The source opens read-only with extension loading disabled, and a full scan leaves it byte-identical.
    source = tmp_path / "source.manager"
    shutil.copyfile(BASIC, source)
    before = _sha(source)
    with ManagerReader(source) as reader:
        assert reader.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute("DELETE FROM Objects")
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute("SELECT load_extension('does-not-exist')").fetchone()
    source.chmod(0o444)
    art = artifact(source)
    manager.inspect([art])
    manager.build_manifest([art], FULL)
    manager.source_expectations([art], FULL)
    assert _sha(source) == before
    assert not list(tmp_path.glob("source.manager-*")), "a journal or WAL file was created beside the source"


def test_manager_decoder_resource_limits(tmp_path):
    from celerp.importers.adapters.manager_io.protobuf import DecodeError, DecodeLimits, decode

    def nested(depth: int) -> bytes:
        payload = b"\x08\x01"
        for _ in range(depth):
            payload = b"\x0a" + bytes([len(payload)]) + payload
        return payload

    def deep_message(depth: int):
        message = decode(nested(depth))
        for _ in range(depth):
            message = message.message(1)
        return message

    hostile = {
        "length beyond payload": (b"\x0a\xff\xff\xff\x0f\x01", DecodeLimits()),
        "truncated varint": (b"\x08\xff\xff", DecodeLimits()),
        "varint over ten bytes": (b"\x08" + b"\xff" * 11, DecodeLimits()),
        "unsupported wire type": (b"\x0b\x0c", DecodeLimits()),
        "field number zero": (b"\x00\x01", DecodeLimits()),
        "oversize payload": (b"\x08\x01" * 64, DecodeLimits(max_bytes=100)),
        "repeated beyond cap": (b"\x08\x01" * 11, DecodeLimits(max_repeated=10)),
        "field budget": (b"\x08\x01\x10\x01\x18\x01", DecodeLimits(max_fields=2)),
    }
    for label, (payload, limits) in hostile.items():
        with pytest.raises(DecodeError) as caught:
            decode(payload, limits)
        message = str(caught.value)
        assert len(message) <= 120, label
        assert "\\x" not in message and "b'" not in message, label

    # Nesting is bounded when a nested message is read, not only at the top level.
    assert deep_message(10).int(1) == 1
    shallow = DecodeLimits(max_depth=4)
    message = decode(nested(10), shallow)
    with pytest.raises(DecodeError, match="Nesting"):
        for _ in range(10):
            message = message.message(1)

    # A Manager file with one malformed and one oversize financial object: each object fails on its
    # own with a bounded note, the scan marks it blocking, and no partial value reaches a manifest.
    from celerp.importers.adapters.manager_io.protobuf import DEFAULT_LIMITS

    broken = specs.obj("SalesInvoice", "BROKEN", {})
    broken = Obj(broken.key, broken.content_type, raw=b"\x0a\xff\xff\xff\x0f\x01")
    huge = specs.obj("JournalEntry", "HUGE", {})
    huge = Obj(huge.key, huge.content_type, raw=b"\x1a" + b"\x80\x80\x80\x02" + b"\x00" * (DEFAULT_LIMITS.max_bytes + 1))
    path = write_manager_file(tmp_path / "hostile.manager", [*specs.basic_objects(), broken, huge], specs.basic_blobs())
    art = artifact(path)
    scan = adapter().inspect([art])
    coverage = _coverage(scan)
    for source_type in ("SalesInvoice (unreadable)", "JournalEntry (unreadable)"):
        count, klass, note = coverage[source_type]
        assert (count, klass) == (1, CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER), source_type
        assert note and len(note) <= 300 and "\\x" not in note
    assert coverage["SalesInvoice"][0] == 3 and coverage["JournalEntry"][0] == 1
    with pytest.raises(ScanError, match="cannot be migrated"):
        adapter().build_manifest([art], FULL)


def test_manager_attachment_handling_is_untrusted_and_reported(tmp_path):
    png = specs.PNG
    missing_target = specs.k("NO-SUCH-OBJECT")
    oversize = png + b"\x00" * (25 * 1024 * 1024)
    cases = {
        "TRAVERSAL": ("../../outside/receipt.png", png, specs.k("INV1")),
        "ABSOLUTE": ("/var/tmp/receipt.png", png, specs.k("INV1")),
        "CONTROL": ("receipt\x07scan.png", png, specs.k("INV1")),
        "OVERSIZE": ("large.png", oversize, specs.k("INV1")),
        "MISMATCH": ("statement.pdf", png, specs.k("INV1")),
        "NOTARGET": ("orphan.png", png, missing_target),
    }
    objects = [*specs.basic_objects()]
    blobs = [*specs.basic_blobs()]
    for label, (name, content, target) in cases.items():
        objects.append(specs.attachment_object(label, name, content, target))
        blobs.append(Blob(specs.k(label), name, "image/png", content))
    # Current Manager versions keep attachment content outside the business file.
    objects.append(specs.attachment_object("EXTERNAL", "outside.png", png, specs.k("INV1")))
    path = write_manager_file(tmp_path / "attachments.manager", objects, tuple(blobs))
    art = artifact(path)
    manager = adapter()

    coverage = _coverage(manager.inspect([art]))
    assert coverage["Attachment"][:2] == (1, CoverageClass.MAPPED)
    assert coverage["Attachment (rejected)"][:2] == (7, CoverageClass.MAPPED_WITH_LOSS)

    manifest = manager.build_manifest([art], FULL)
    (accepted,) = manifest.bundle.attachments
    assert accepted.source_external_id == ref("ATT1")
    assert accepted.file_name == "receipt-scan.png"
    assert accepted.declared_content_type == "image/png"
    assert accepted.sha256 == hashlib.sha256(png).hexdigest()
    assert (accepted.target_source_type, accepted.target_source_external_id) == ("SalesInvoice", ref("INV1"))
    assert manager.read_attachment([art], ref("ATT1")) == png

    rejected = {row["source_external_id"]: row["reason"] for row in manifest.source_summary["attachments"]["rejected"]}
    assert set(rejected) == {ref(label) for label in [*cases, "EXTERNAL"]}
    assert all(reason for reason in rejected.values())
    assert "target" in rejected[ref("NOTARGET")]
    for label in [*cases, "EXTERNAL"]:
        with pytest.raises(ScanError):
            manager.read_attachment([art], ref(label))

    # A rejected attachment never turns the run into a false full success, and never blocks the ledger.
    assert ("Attachment (rejected)", CoverageClass.MAPPED_WITH_LOSS) in {
        (row.source_type, row.coverage_class) for row in manifest.coverage
    }
    assert len(manifest.bundle.documents) == 7
    assert manifest.source_summary["attachments"]["accepted"] == 1


def test_manager_mapping_preserves_source_semantics():
    manifest = adapter().build_manifest([artifact(BASIC)], FULL)
    bundle = manifest.bundle
    figures = CHECKPOINTS["basic"]

    assert manifest.source_system == "manager_io"
    assert all(record.source_system == "manager_io" for record in bundle.source_records())
    assert (bundle.company.name, bundle.company.base_currency, bundle.company.money_precision) == (
        figures["company"]["name"], figures["company"]["base_currency"], figures["company"]["money_precision"])

    # Two accounts with the same display name stay distinct by source identity.
    sales = sorted((a for a in bundle.accounts if a.name == "Sales"), key=lambda a: a.code)
    assert [(a.source_external_id, a.code, a.account_type) for a in sales] == [
        (ref("S1"), "4000", "revenue"), (ref("S2"), "4010", "revenue")]
    accounts = {a.source_external_id: a for a in bundle.accounts}
    assert accounts[ref("EQ")].account_type == "equity"
    assert accounts[ref("OFF")].account_type == "expense"
    assert (accounts[ref("OPB")].account_type, accounts[ref("OPB")].control) == ("asset", "bank")
    assert accounts[ref("@BalanceSheetAccountsReceivableAccount")].control == "receivable"
    assert accounts[ref("@BalanceSheetAccountsPayableAccount")].control == "payable"
    assert accounts[ref("@BalanceSheetTaxPayableAccount")].control == "tax"

    # A customer and a supplier sharing name and email stay two contacts.
    acme = sorted((c for c in bundle.contacts if c.name == "Acme Trading"), key=lambda c: c.roles[0])
    assert [(c.source_type, c.source_external_id, c.roles) for c in acme] == [
        ("Customer", ref("CA"), ["customer"]), ("Supplier", ref("SA"), ["supplier"])]
    assert acme[0].email == acme[1].email == "accounts@acme.example.com"

    # A bank transfer imports once, with both sides, and nowhere else.
    (transfer,) = bundle.bank_transfers
    assert (transfer.source_external_id, transfer.from_account_external_id, transfer.to_account_external_id,
            transfer.amount) == (ref("IAT1"), ref("OPB"), ref("PC"), Decimal("15"))
    others = [*bundle.journals, *bundle.settlements]
    assert not [r for r in others if ref("IAT1") in r.source_external_id]

    # Paid status and amounts derive from imported settlements and linked notes.
    documents = {d.source_external_id: d for d in bundle.documents}
    for label, expected in figures["full_history"]["documents"].items():
        doc = documents[ref(label)]
        assert doc.status == expected["status"], label
        for field in ("total", "tax_total", "amount_paid", "amount_outstanding"):
            assert Decimal(getattr(doc, field)) == Decimal(expected[field]), (label, field)
    receipt = next(s for s in bundle.settlements if s.source_external_id == ref("R1"))
    assert (receipt.settlement_type, receipt.bank_account_external_id, receipt.contact_external_id) == (
        "receipt", ref("OPB"), ref("CA"))
    assert {(a.document_external_id, a.amount) for a in receipt.allocations} == {
        (ref("INV1"), Decimal("110")), (ref("INV2"), Decimal("20"))}
    assert documents[ref("INV1")].contact_external_id == ref("CA")
    assert documents[ref("BILL1")].contact_external_id == ref("SA")
    assert documents[ref("CN1")].metadata["applies_to"] == ref("INV2")

    # Tax keeps its rate, inclusive treatment and control account.
    (vat,) = bundle.tax_codes
    assert (vat.source_external_id, vat.rate_percent, vat.account_external_id) == (
        ref("VAT"), Decimal("10"), ref("@BalanceSheetTaxPayableAccount"))
    inclusive = documents[ref("INV2")]
    exclusive = documents[ref("INV1")]
    assert inclusive.metadata["amounts_include_tax"] is True
    assert exclusive.metadata["amounts_include_tax"] is False
    (line,) = inclusive.line_items
    assert (line.tax_code_external_id, line.tax_amount, line.total_price) == (ref("VAT"), Decimal("5"), Decimal("50"))
    (line,) = exclusive.line_items
    assert (line.quantity, line.unit_price, line.tax_amount, line.total_price) == (
        Decimal("2"), Decimal("50"), Decimal("10"), Decimal("100"))
    (line,) = documents[ref("INV3")].line_items
    assert (line.discount, line.total_price) == (Decimal("50"), Decimal("200"))

    # A receipt against income accounts keeps its ledger effect as a journal, reported as a loss of form.
    fallback = next(j for j in bundle.journals if j.source_external_id == f"{ref('R2')}:journal")
    assert {(l.account_external_id, l.debit, l.credit) for l in fallback.lines} == {
        (ref("PC"), Decimal("22"), Decimal("0")), (ref("S2"), Decimal("0"), Decimal("20")),
        (ref("@BalanceSheetTaxPayableAccount"), Decimal("0"), Decimal("2"))}
    assert _coverage(manifest)["Receipt (journal fallback)"][:2] == (1, CoverageClass.MAPPED_WITH_LOSS)

    # Item stock derives from transactions: each purchase line moves its stock and value, and
    # full history adds no stock snapshot.
    (item,) = bundle.items
    assert (item.source_external_id, item.sku, item.name, item.status) == (ref("WID"), "WID-1", "Widget", "available")
    bill_lines = [l for d in bundle.documents if d.doc_type == "bill" for l in d.line_items if l.item_external_id]
    assert sorted(l.quantity for l in bill_lines) == [Decimal("5"), Decimal("10")]
    assert sorted((a.kind, a.source_type, a.item_external_id, a.quantity, a.value)
                  for a in bundle.inventory_adjustments) == [
        ("adjustment", "PurchaseInvoice", ref("WID"), l.quantity, l.total_price)
        for l in sorted(bill_lines, key=lambda l: l.quantity)]

    # Source change history is run provenance only: counted, never imported as events or attributed to a user.
    assert manifest.source_summary["audit_history"] == {"changes": specs.BASIC_CHANGES, "emails": 0}
    dumped = manifest.model_dump_json()
    assert "user@example.com" not in dumped
    assert not [r for r in bundle.source_records() if r.source_type in ("Changes", "Emails")]


def test_check_manager_content_types_all_classified(tmp_path):
    from scripts.diagnostics.manager_probe import check_manager_content_types_all_classified

    assert check_manager_content_types_all_classified() == []

    # The committed synthetic files are exactly what the spec encodes.
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
    from celerp.importers.sample import SAMPLE_ARTIFACT, SAMPLE_COMPANY_NAME

    def rows(path: Path) -> dict[str, tuple[str, bytes | None]]:
        with ManagerReader(path) as reader:
            return {row.key: (row.content_type, row.content) for row in reader.objects()}

    fresh = {
        BASIC: specs.build_basic(tmp_path / "basic.manager"),
        FX: specs.build_fx(tmp_path / "fx.manager"),
        SAMPLE_ARTIFACT: specs.build_basic(tmp_path / "sample.manager", company=SAMPLE_COMPANY_NAME),
    }
    for committed, rebuilt in fresh.items():
        assert rows(committed) == rows(rebuilt), f"{committed.name} is stale; rebuild it with the fixture builder"

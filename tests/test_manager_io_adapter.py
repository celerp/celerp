# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Manager.io adapter: detection, safe reading, bounded decoding, attachments and mapping semantics."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from celerp.importers.adapters.base import MigrationDecisions, ScanError
from celerp.importers.schema import CIFMode, CoverageClass
from fixtures.manager_io import specs
from fixtures.manager_io.encoder import Blob, Obj, write_manager_file
from fixtures.manager_io.support import BASIC, CHECKPOINTS, FX, actual_rows, adapter, artifact, ref

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
    # The readable invoices are still classified: INV2 was entered including tax.
    assert (coverage["SalesInvoice"][0], coverage["SalesInvoice (amounts including tax)"][0]) == (2, 1)
    assert coverage["JournalEntry"][0] == 1
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
    assert (line.discount_percent, line.total_price) == (Decimal("20"), Decimal("200"))

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


# ── Cutover partition ─────────────────────────────────────────────────────────
# Each case writes the shared masters, owner funding on 2026-01-02 and the case's records, then
# checks the cutover migration against full history at the final date: the opening journal
# plus every imported record ends at the full-history position, account by account and party
# by party, and the stock it carries ends at the full-history stock.

CUT = specs.CUTOVER_DATE
BEFORE, AFTER = date(2026, 1, 30), date(2026, 2, 1)
_POSITIONS = {"debits_equal_credits", "trial_balance", "ar_control", "ap_control", "ar_by_customer",
              "ap_by_supplier", "bank_cash", "inventory_quantity", "inventory_value", "tax_control"}


def _sale(label: str, day: date, qty: str = "1", price: str = "100") -> Obj:
    return specs.obj("SalesInvoice", label, {1: day, 2: label, 3: specs.k("CA"), 49: [
        {2: specs.k("S1"), 17: "Consulting", 18: Decimal(qty), 19: Decimal(price), 21: specs.k("VAT")}]})


def _bill(label: str, day: date, qty: str) -> Obj:
    return specs.obj("PurchaseInvoice", label, {1: day, 2: label, 3: specs.k("SA"), 23: [
        {1: specs.k("WID"), 17: "Widgets", 18: Decimal(qty), 19: Decimal("4")}]})


def _receipt(label: str, day: date, invoice: str, amount: str) -> Obj:
    ca = specs.k("CA")
    return specs.obj("Receipt", label, {1: day, 2: label, 3: specs.PAID_BY_CUSTOMER, 4: ca, 7: specs.k("OPB"), 11: [
        {2: specs.AR, 3: ca, 4: specs.k(invoice), 18: Decimal(amount)}]})


def _payment(label: str, day: date, bill: str, amount: str) -> Obj:
    sa = specs.k("SA")
    return specs.obj("Payment", label, {1: day, 2: label, 3: specs.PAID_BY_SUPPLIER, 5: sa, 7: specs.k("OPB"), 11: [
        {2: specs.AP, 7: sa, 8: specs.k(bill), 18: Decimal(amount)}]})


def _expense(label: str, day: date, amount: str = "20") -> Obj:
    return specs.obj("JournalEntry", label, {1: day, 2: label, 3: "Stationery", 14: [
        {1: specs.k("OFF"), 13: Decimal(amount)},
        {1: specs.CASH_AT_BANK, 29: specs.k("OPB"), 14: Decimal(amount)}]})


def _transfer(label: str, day: date, amount: str = "25") -> Obj:
    return specs.obj("InterAccountTransfer", label, {1: day, 6: label, 2: specs.k("OPB"), 8: Decimal(amount),
                                                     3: specs.k("PC"), 9: Decimal(amount)})


def _position(postings) -> dict:
    out: dict = {}
    for p in postings:
        out[(p.account, p.contact)] = out.get((p.account, p.contact), Decimal(0)) + p.amount
    return {key: amount for key, amount in out.items() if amount}


def _cutover_case(tmp_path, *records: Obj):
    """(cutover manifest, its ledger) for one case, after checking it against full history."""
    from celerp.importers.adapters.manager_io.book import read_book
    from celerp.importers.adapters.manager_io.ledger import build_ledger, stock
    from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader

    path = write_manager_file(tmp_path / "case.manager", [
        *specs.masters(), specs.funding("JE0", "JE-0", date(2026, 1, 2), Decimal("500")), *records])
    cutover = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUT)
    manager, art = adapter(), [artifact(path)]
    manifest = manager.build_manifest(art, cutover)
    with ManagerReader(path) as reader:
        book = read_book(reader)
    ledger, full = build_ledger(book, cutover), build_ledger(book, FULL)

    assert _position(ledger.opening + ledger.imported_postings()) == _position(full.postings)
    positions = {r for r in actual_rows(manifest.reconciliation_expectations) if r[0] in _POSITIONS}
    assert positions == {r for r in actual_rows(manager.source_expectations(art, FULL)) if r[0] in _POSITIONS}
    carried_stock: dict[str, tuple[Decimal, Decimal]] = {}
    for adj in manifest.bundle.inventory_adjustments:
        qty, value = carried_stock.get(adj.item_external_id, (Decimal(0), Decimal(0)))
        carried_stock[adj.item_external_id] = (qty + adj.quantity, value + adj.value)
    assert carried_stock == {item: held for item, held in stock(full.postings).items() if any(held)}
    return manifest, ledger


def _ids(manifest) -> set[str]:
    return {r.source_external_id for r in manifest.bundle.source_records()}


def _opening(manifest) -> dict[str, Decimal]:
    (entry,) = [j for j in manifest.bundle.journals if j.source_type == "OpeningBalances"]
    assert entry.entry_date == CUT
    out: dict[str, Decimal] = {}
    for line in entry.lines:
        out[line.account_external_id] = out.get(line.account_external_id, Decimal(0)) + line.debit - line.credit
    return out


def test_cutover_record_the_day_before_is_opening_only(tmp_path):
    manifest, _ = _cutover_case(tmp_path, _expense("JEX", BEFORE))
    assert ref("JEX") not in _ids(manifest)
    assert _opening(manifest)[ref("OFF")] == Decimal("20")


def test_cutover_record_on_the_cutover_date_is_before_it(tmp_path):
    # Pre-cutover means on or before the date: a journal on it is in the opening, and an
    # invoice on it still open is carried with the opening net of it.
    manifest, ledger = _cutover_case(tmp_path, _expense("JEX", CUT), _sale("INVX", CUT))
    assert ref("JEX") not in _ids(manifest)
    assert _opening(manifest)[ref("OFF")] == Decimal("20")
    assert ledger.documents == [ref("INVX")]
    assert ref("S1") not in _opening(manifest)


def test_cutover_record_the_day_after_is_native(tmp_path):
    manifest, _ = _cutover_case(tmp_path, _sale("INVX", AFTER))
    assert ref("INVX") in _ids(manifest)
    assert ref("S1") not in _opening(manifest)


def test_cutover_closed_invoice_before_is_opening_only(tmp_path):
    manifest, ledger = _cutover_case(tmp_path, _sale("INVX", BEFORE), _receipt("RX", BEFORE, "INVX", "110"))
    assert not {ref("INVX"), ref("RX")} & _ids(manifest)
    assert ledger.documents == []
    assert _opening(manifest)[ref("S1")] == Decimal("-100")
    assert ref("@BalanceSheetAccountsReceivableAccount") not in _opening(manifest)


def test_cutover_open_invoice_before_is_carried_net_of_opening(tmp_path):
    manifest, ledger = _cutover_case(tmp_path, _sale("INVX", BEFORE), _receipt("RX", BEFORE, "INVX", "30"))
    (doc,) = manifest.bundle.documents
    assert doc.source_external_id == ref("INVX")
    assert (doc.amount_paid, doc.amount_outstanding, doc.status) == (Decimal("30"), Decimal("80"), "awaiting_payment")
    (settlement,) = manifest.bundle.settlements
    assert settlement.source_external_id == ref("RX")
    # The invoice and its part payment are imported, so the opening carries neither.
    assert ref("S1") not in _opening(manifest)
    assert ref("@BalanceSheetAccountsReceivableAccount") not in _opening(manifest)
    assert _opening(manifest)[ref("OPB")] == Decimal("500")


def test_cutover_invoice_settled_after_is_carried_and_settlement_imported(tmp_path):
    manifest, _ = _cutover_case(tmp_path, _sale("INVX", BEFORE), _receipt("RX", AFTER, "INVX", "110"))
    (doc,) = manifest.bundle.documents
    assert (doc.source_external_id, doc.status, doc.amount_outstanding) == (ref("INVX"), "paid", Decimal("0"))
    (settlement,) = manifest.bundle.settlements
    assert settlement.source_external_id == ref("RX")
    assert [(a.document_external_id, a.amount) for a in settlement.allocations] == [(ref("INVX"), Decimal("110"))]
    assert _opening(manifest)[ref("OPB")] == Decimal("500")


def test_cutover_journal_after_is_present(tmp_path):
    manifest, _ = _cutover_case(tmp_path, _expense("JEX", AFTER))
    (journal,) = [j for j in manifest.bundle.journals if j.source_external_id == ref("JEX")]
    assert journal.entry_date == AFTER
    assert ref("OFF") not in _opening(manifest)


def test_cutover_transfer_after_is_present(tmp_path):
    manifest, _ = _cutover_case(tmp_path, _transfer("IATX", AFTER))
    (transfer,) = manifest.bundle.bank_transfers
    assert (transfer.source_external_id, transfer.amount) == (ref("IATX"), Decimal("25"))
    assert ref("PC") not in _opening(manifest)


def test_cutover_stock_is_opening_plus_later_movements(tmp_path):
    # The first bill is paid before the cutover, so its stock is part of the opening position.
    manifest, _ = _cutover_case(tmp_path, _bill("BILLX", BEFORE, "10"), _payment("PX", BEFORE, "BILLX", "40"),
                                _bill("BILLY", AFTER, "5"))
    moves = sorted((a.kind, a.adjustment_date, a.quantity, a.value) for a in manifest.bundle.inventory_adjustments)
    assert moves == [("adjustment", AFTER, Decimal("5"), Decimal("20")),
                     ("opening", CUT, Decimal("10"), Decimal("40"))]


def test_manifest_refuses_a_mapped_record_it_does_not_carry(monkeypatch):
    # The adapter checks its own bundle against its verdicts: a mapped source record with no
    # representation stops the build instead of reaching a destination without it.
    from celerp.importers.adapters.manager_io import mappings

    monkeypatch.setattr(mappings, "_transfers", lambda book, ledger: [])
    with pytest.raises(ScanError, match="InterAccountTransfer"):
        adapter().build_manifest([artifact(BASIC)], FULL)


# ── Settlement lines with no document ─────────────────────────────────────────
# A customer or supplier line that names a document settles it; one that names only the
# contact is money on account, carried in the settlement's journal fallback with the
# contact on the receivable or payable line and the bank on the other side.

def _settlement_file(tmp_path, kind: str, lines: list[tuple[str | None, str]]) -> Path:
    """Masters, one invoice or bill of 110, and one receipt or payment of the given
    (document label or None, amount) lines."""
    ca, sa, opb = specs.k("CA"), specs.k("SA"), specs.k("OPB")
    if kind == "Receipt":
        doc = specs.obj("SalesInvoice", "DOC", {1: date(2026, 1, 10), 2: "INV-1", 3: ca, 49: [
            {2: specs.k("S1"), 17: "Consulting", 18: Decimal("1"), 19: Decimal("100"), 21: specs.k("VAT")}]})
        raw = [{2: specs.AR, 3: ca, 18: Decimal(amount), **({4: specs.k(d)} if d else {})} for d, amount in lines]
        record = specs.obj("Receipt", "SET", {1: date(2026, 1, 20), 2: "R-1", 3: specs.PAID_BY_CUSTOMER, 4: ca,
                                             7: opb, 11: raw})
    else:
        doc = specs.obj("PurchaseInvoice", "DOC", {1: date(2026, 1, 10), 2: "BILL-1", 3: sa, 23: [
            {2: specs.k("OFF"), 17: "Supplies", 18: Decimal("1"), 19: Decimal("100"), 21: specs.k("VAT")}]})
        raw = [{2: specs.AP, 7: sa, 18: Decimal(amount), **({8: specs.k(d)} if d else {})} for d, amount in lines]
        record = specs.obj("Payment", "SET", {1: date(2026, 1, 20), 2: "P-1", 3: specs.PAID_BY_SUPPLIER, 5: sa,
                                             7: opb, 11: raw})
    return write_manager_file(tmp_path / "settlement.manager", [
        *specs.masters(), specs.funding("JE0", "JE-0", date(2026, 1, 2), Decimal("500")), doc, record])


def _bank_and_party(manifest, kind: str) -> tuple[Decimal, Decimal]:
    """The bank and customer or supplier movement the bundle carries for the settlement."""
    party = ref("@BalanceSheetAccountsReceivableAccount" if kind == "Receipt" else "@BalanceSheetAccountsPayableAccount")
    sign = 1 if kind == "Receipt" else -1
    bank = party_total = Decimal(0)
    for s in manifest.bundle.settlements:
        bank += sign * s.amount
        party_total -= sign * s.amount
    for j in manifest.bundle.journals:
        if j.source_external_id != f"{ref('SET')}:journal":
            continue
        for line in j.lines:
            if line.account_external_id == ref("OPB"):
                bank += line.debit - line.credit
            if line.account_external_id == party:
                party_total += line.debit - line.credit
                assert line.contact_external_id == ref("CA" if kind == "Receipt" else "SA")
    return bank, party_total


@pytest.mark.parametrize("kind", ["Receipt", "Payment"])
@pytest.mark.parametrize("lines, settled, on_account", [
    ([("DOC", "110")], "110", "0"),
    ([(None, "50")], "0", "50"),
    ([("DOC", "60"), (None, "40")], "60", "40"),
], ids=["allocated", "on_account", "mixed"])
def test_settlement_on_account_lines_are_a_contact_journal(tmp_path, kind, lines, settled, on_account):
    manifest = adapter().build_manifest([artifact(_settlement_file(tmp_path, kind, lines))], FULL)
    settled, on_account = Decimal(settled), Decimal(on_account)
    settlements = [s for s in manifest.bundle.settlements if s.source_external_id == ref("SET")]
    fallback = [j for j in manifest.bundle.journals if j.source_external_id == f"{ref('SET')}:journal"]
    if settled:
        (s,) = settlements
        assert s.amount == settled
        assert [(a.document_external_id, a.amount) for a in s.allocations] == [(ref("DOC"), settled)]
    else:
        assert settlements == []
    assert len(fallback) == (1 if on_account else 0)
    # The source bank and customer or supplier movement, represented once across both.
    sign = 1 if kind == "Receipt" else -1
    assert _bank_and_party(manifest, kind) == (sign * (settled + on_account), -sign * (settled + on_account))
    coverage = _coverage(manifest)
    if on_account:
        count, klass, note = coverage[f"{kind} (journal fallback)"]
        assert (count, klass) == (1, CoverageClass.MAPPED_WITH_LOSS)
        assert "on account" in note
        assert all(klass != CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER for _, klass, _ in coverage.values())
    else:
        assert f"{kind} (journal fallback)" not in coverage


# ── Attachment targets ────────────────────────────────────────────────────────

def test_attachments_move_only_to_records_that_hold_files(tmp_path):
    # Celerp attaches files to contacts, documents and items. A file on a receipt, payment,
    # transfer, journal or debit note has no destination: it is reported, never put in the
    # manifest, and never stops the migration.
    art = artifact(specs.build_attachment_targets(tmp_path / "targets.manager"))
    manifest = adapter().build_manifest([art], FULL)
    moved = {(a.source_external_id, a.target_source_type, a.target_source_external_id)
             for a in manifest.bundle.attachments}
    assert moved == {(ref("ATT1"), "SalesInvoice", ref("INV1")), (ref("ACON"), "Customer", ref("CA")),
                     (ref("AITEM"), "InventoryItem", ref("WID"))}
    coverage = _coverage(manifest)
    assert coverage["Attachment"][:2] == (3, CoverageClass.MAPPED)
    count, klass, note = coverage["Attachment (record cannot hold files)"]
    assert (count, klass) == (5, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    assert "not moved" in note
    assert "Attachment (rejected)" not in coverage
    rejected = {row["source_external_id"] for row in manifest.source_summary["attachments"]["rejected"]}
    assert rejected == {ref(label) for label in ("AREC", "AFALL", "ATRF", "AJE", "ADN")}
    assert len(manifest.bundle.documents) == 7


# ── Master data fidelity ──────────────────────────────────────────────────────

def test_master_attributes_celerp_cannot_hold_are_each_reported(tmp_path):
    # An inactive item becomes an archived item, which Celerp represents exactly. Celerp
    # has no inactive state for contacts, tax codes or currencies and no default purchase
    # price for items, so each of those is reported as lost, per record.
    art = artifact(specs.build_inactive_masters(tmp_path / "inactive.manager"))
    manifest = adapter().build_manifest([art], FULL)
    bundle = manifest.bundle
    items = {i.source_external_id: i for i in bundle.items}
    assert (items[ref("WID")].status, items[ref("OLD")].status) == ("available", "archived")
    # The purchase price never becomes a cost: inventory cost comes only from the bills.
    assert [(i.cost_per_unit, i.total_cost) for i in items.values()] == [(None, None)] * 2
    assert all(i.metadata == {} for i in items.values())
    assert all(c.metadata == {} for c in bundle.contacts)

    coverage = _coverage(manifest)
    masters = ("Customer", "Supplier", "TaxCode", "ForeignCurrency", "InventoryItem")
    lost = {label: (count, klass) for label, (count, klass, _) in coverage.items()
            if label.startswith(tuple(f"{m} (" for m in masters))}
    assert lost == {
        "Customer (inactive)": (1, CoverageClass.MAPPED_WITH_LOSS),
        "Supplier (inactive)": (1, CoverageClass.MAPPED_WITH_LOSS),
        "TaxCode (inactive)": (1, CoverageClass.MAPPED_WITH_LOSS),
        "ForeignCurrency (inactive)": (1, CoverageClass.MAPPED_WITH_LOSS),
        "InventoryItem (purchase price not moved)": (1, CoverageClass.MAPPED_WITH_LOSS),
    }
    assert coverage["InventoryItem"][:2] == (1, CoverageClass.MAPPED)
    assert coverage["Customer"][:2] == (1, CoverageClass.MAPPED)


# ── Document lines ────────────────────────────────────────────────────────────

def test_document_line_forms_carry_their_exact_fields_and_report_the_rest(tmp_path):
    # A percentage discount has an exact Celerp line field; a fixed discount amount and
    # amounts entered including tax do not. Their accounting is exact either way, so each
    # is carried in the line total and reported as a loss, never blocked.
    manifest = adapter().build_manifest([artifact(specs.build_line_variants(tmp_path / "lines.manager"))], FULL)
    lines = {d.source_external_id: d.line_items[0] for d in manifest.bundle.documents}
    assert (lines[ref("LPCT")].discount_percent, lines[ref("LPCT")].total_price) == (Decimal("20"), Decimal("200"))
    assert (lines[ref("LFIX")].discount_percent, lines[ref("LFIX")].unit_price,
            lines[ref("LFIX")].total_price) == (None, Decimal("100"), Decimal("85"))
    assert (lines[ref("LINC")].unit_price, lines[ref("LINC")].tax_amount,
            lines[ref("LINC")].total_price) == (Decimal("50"), Decimal("5"), Decimal("50"))
    assert (lines[ref("LRND")].unit_price, lines[ref("LRND")].tax_amount,
            lines[ref("LRND")].total_price) == (Decimal("3.335"), Decimal("1.00"), Decimal("10.01"))

    coverage = _coverage(manifest)
    assert coverage["SalesInvoice"][:2] == (4, CoverageClass.MAPPED)
    count, klass, note = coverage["SalesInvoice (line discount amount)"]
    assert (count, klass) == (1, CoverageClass.MAPPED_WITH_LOSS)
    assert "line total" in note
    count, klass, note = coverage["SalesInvoice (amounts including tax)"]
    assert (count, klass) == (1, CoverageClass.MAPPED_WITH_LOSS)
    assert "excluding tax" in note

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Odoo backups: one database manager zip read as one company's books.

The fixture is a fictitious company's first four months, written as Odoo 13, 14 and 17
lay their tables out (tests/fixtures/odoo_backup/build.py)."""

from __future__ import annotations

import gzip
import io
import json
import random
import tracemalloc
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from functools import cache

import pytest
from sqlalchemy import text

from celerp.importers.adapters.base import COMPANY_QUESTION, MigrationDecisions, ScanError, SourceRevisionError
from celerp.importers.adapters.odoo import attachments
from celerp.importers.adapters.odoo.adapter import OTHER_COMPANY, OTHER_COMPANY_NOTE
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io.support import BASIC, artifact
from fixtures.odoo_backup import build
from migration_support import count, load_run, maker, real_engine, scan_upload  # noqa: F401
from test_migration_e2e import _passing, migrate_set
from ui.i18n import t

FULL = MigrationDecisions(mode=CIFMode.FULL_HISTORY)
CUTOVER = date(2024, 2, 29)
VERSIONS = (13, 14, 15, 16, 17)
D = Decimal


@cache
def _backup(version: int) -> bytes:
    return build.backup(version)


def _file(tmp_path, data: bytes, name: str = "harbour.zip") -> list:
    path = tmp_path / name
    path.write_bytes(data)
    return [artifact(path)]


def _adapter():
    from celerp.importers.adapters.odoo import OdooAdapter

    return OdooAdapter()


def _zip(members: dict[str, bytes | str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def _source_balances(on: date | None = None) -> dict[str, Decimal]:
    """Each account's balance from the fixture's own posted entries, debit positive. Off-balance
    accounts are outside the books."""
    off_balance = {i for i, _, _, kind, _ in build.ACCOUNTS if kind == "off_balance"}
    out: dict[str, Decimal] = defaultdict(Decimal)
    for move in build.moves():
        if move.state == "posted" and (on is None or date.fromisoformat(move.date) <= on):
            for line in move.lines:
                if line.account not in off_balance:
                    out[build.CODES[line.account]] += line.balance
    return dict(out)


# ── Recognition ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_an_odoo_backup_is_recognised_by_content_alone(tmp_path, version):
    from celerp.importers.adapters.registry import detect_adapter

    files = _file(tmp_path, _backup(version), "download")
    assert detect_adapter(files).key == "odoo"
    scan = _adapter().inspect(files)
    assert (scan.company_name, scan.base_currency, scan.source_schema_version) == (build.COMPANY, "USD", str(version))
    assert scan.currencies == ("EUR", "USD")
    assert (scan.period_start, scan.period_end) == (date(2024, 1, 5), date(2024, 4, 20))
    assert set(scan.features) == {"foreign_currency", "inventory", "tax", "attachments"}


def test_only_a_real_odoo_backup_is_recognised(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter
    from fixtures.xero_export import build as xero

    real = zipfile.ZipFile(io.BytesIO(_backup(17)))
    dump, manifest = real.read("dump.sql"), json.loads(real.read("manifest.json"))
    strangers = {
        "no manifest": _zip({"dump.sql": dump}),
        "not an odoo manifest": _zip({"dump.sql": dump, "manifest.json": json.dumps({"version": "17.0"})}),
        "manifest not json": _zip({"dump.sql": dump, "manifest.json": "odoo_dump"}),
        "not a zip": b"-- PostgreSQL database dump\n",
    }
    for label, data in strangers.items():
        result = _adapter().detect(_file(tmp_path, data, f"{label}.zip"))
        assert result.matched is False, label
        assert "not an Odoo backup" in result.reason, label
    # A backup without its dump, or with one that is not a database dump, is one, damaged: it is
    # refused with that reason rather than as a stranger.
    for label, members in {"no dump": {}, "not a database dump": {"dump.sql": "SELECT 1;\n"}}.items():
        with pytest.raises(SourceRevisionError, match="cannot be read"):
            _adapter().detect(_file(tmp_path, _zip({**members, "manifest.json": json.dumps(manifest)}), f"{label}.zip"))
    # Two files, a Manager business file and an export set are not a backup; each keeps its own adapter.
    assert _adapter().detect([*_file(tmp_path, _backup(17)), *_file(tmp_path, _backup(14), "b.zip")]).matched is False
    assert _adapter().detect([artifact(BASIC)]).matched is False
    assert detect_adapter([artifact(BASIC)]).key == "manager_io"
    xero_zip = _zip({name: data for name, data in xero.files().items()})
    assert _adapter().detect(_file(tmp_path, xero_zip, "xero.zip")).matched is False
    assert detect_adapter(_file(tmp_path, xero_zip, "xero.zip")).key == "xero"


def _rezipped(*folders: str) -> bytes:
    """The fixture backup extracted and zipped again into each of `folders`, the way Finder,
    Explorer or Archive Utility zip a folder, with the resource files macOS adds."""
    real = zipfile.ZipFile(io.BytesIO(_backup(17)))
    return _zip({**{f"{folder}/{name}": real.read(name) for folder in folders for name in real.namelist()},
                 "__MACOSX/harbour/._dump.sql": b"\x00\x05\x16\x07", "harbour/._manifest.json": b"\x00"})


def test_a_backup_zipped_again_inside_its_folder_is_read_whole(tmp_path):
    files = _file(tmp_path, _rezipped("harbour"))
    assert _adapter().detect(files).matched
    original = _adapter().build_manifest(_file(tmp_path, _backup(17), "original.zip"), FULL)
    manifest = _adapter().build_manifest(files, FULL)
    assert manifest.bundle.model_dump() == original.bundle.model_dump()     # the filestore is found too
    assert _adapter().read_attachment(files, FULL, "attachment:1") == build.PDF

    two = _adapter().detect(_file(tmp_path, _rezipped("harbour", "harbour copy"), "two.zip"))
    assert two.matched is False and "not an Odoo backup" in two.reason


def test_a_pg_dump_custom_format_backup_is_refused_with_its_own_message(tmp_path):
    from celerp.importers.adapters.registry import detect_adapter

    custom = b"PGDMP\x01\x0e\x00" + bytes(64)
    for label, data in {"bare": custom, "zipped": _zip({"harbour.dump": custom}),
                        "zipped in a folder": _zip({"backups/harbour.dump": custom, "backups/notes.txt": "x"})}.items():
        with pytest.raises(SourceRevisionError, match="pg_dump custom format.*choose the zip format"):
            detect_adapter(_file(tmp_path, data, "harbour.dump"))
    # A backup whose manifest is Odoo's with a dump in this format is refused the same way.
    with pytest.raises(SourceRevisionError, match="pg_dump custom format.*choose the zip format"):
        detect_adapter(_file(tmp_path, _zip({"dump.sql": custom, "manifest.json": json.dumps(build.manifest(17))})))
    # Deeper than one folder it is just some zip, and not recognised.
    deeper = _adapter().detect(_file(tmp_path, _zip({"a/b/harbour.dump": custom}), "deeper.zip"))
    assert deeper.matched is False and "not an Odoo backup" in deeper.reason


@pytest.mark.parametrize("version", (12, 18))
def test_a_backup_from_an_unsupported_odoo_version_is_refused_with_its_own_message(tmp_path, version):
    from celerp.importers.adapters.registry import detect_adapter

    data = build.backup(17, manifest_=build.manifest(version))
    with pytest.raises(SourceRevisionError, match=f"Odoo {version}.*Odoo 13 to 17"):
        detect_adapter(_file(tmp_path, data))


def test_translated_names_from_odoo_16_are_read_in_english(tmp_path):
    from celerp.importers.adapters.odoo.dump import Backup

    tables = build.tables(17)
    raw = {row[0]: row[6] for row in tables.rows["account_account"][1]}
    assert json.loads(raw[1]) == {"en_US": "Bank"}             # the dump holds JSON
    scan = _adapter().build_manifest(_file(tmp_path, _backup(17)), FULL)
    assert {a.code: a.name for a in scan.bundle.accounts}["101000"] == "Bank"
    assert {i.sku: i.name for i in scan.bundle.items} == {"SRV": "Consulting", "WID-1": "Widget"}
    assert {i.sku: i.description for i in scan.bundle.items}["WID-1"] == "Widget for sale"
    assert sorted(t.name for t in scan.bundle.tax_codes) == ["15%", "15% [2]", "PST 7%", "WHT 3%", "WHT 3% [5]"]

    with Backup(tmp_path / "harbour.zip") as backup:
        assert backup.text('{"es_ES": "Banco"}') == "Banco"     # no English name: the first one stored
        assert backup.text("plain") == "plain"
        backup.version = 15
        assert backup.text('{"en_US": "Bank"}') == '{"en_US": "Bank"}'   # before 16 a name is plain text
        assert backup.lot_table == "stock_production_lot"


def _uninstall(version: int, *modules: str, tables: tuple[str, ...]) -> bytes:
    """The backup of a database without `modules`: the manifest does not list them and the
    dump holds none of `tables`, the tables they make."""
    out = build.tables(version)
    for name in [n for n in out.rows if n.startswith(tables)]:
        del out.rows[name]
    installed = tuple(m for m in build.INSTALLED if m not in modules)
    return build.backup(version, out, build.manifest(version, installed))


def test_a_backup_without_accounting_is_refused(tmp_path):
    data = _uninstall(17, "account", "stock_account", tables=("account_account", "account_move", "account_tax",
                                                               "account_partial", "account_payment", "stock_valuation"))
    with pytest.raises(ScanError, match="Accounting is not installed"):
        _adapter().inspect(_file(tmp_path, data))


def _cut(dump: str, at: str, keep: int = 0) -> str:
    """The dump up to where `at` starts, and `keep` characters more."""
    return dump[:dump.index(at) + keep]


def _damaged() -> dict[str, str]:
    dump = build.dump_sql(build.tables(17))
    copy = "COPY public.account_move_line ("
    row = dump.index("\n", dump.index(copy)) + 1
    return {
        # The upload stopped after the books, before the stock.
        "truncated between tables": _cut(dump, "--\n-- Data for Name: stock_location"),
        "truncated inside a table": _cut(dump, copy, dump.index("\n", row) + 1 - dump.index(copy)),
        "truncated inside a row": _cut(dump, copy, row + 5 - dump.index(copy)),
        "a row short of a field": dump[:row] + dump[row:].replace("\t", " ", 1),
        "a table left without its data": dump.replace(_cut(dump[dump.index("--\n-- Data for Name: stock_quant;"):],
                                                           "--\n-- Data for Name: stock_lot"), ""),
    }


@pytest.mark.parametrize("damage", list(_damaged()))
def test_a_damaged_or_incomplete_dump_is_refused_before_anything_is_read(tmp_path, damage):
    data = _zip({"dump.sql": _damaged()[damage], "manifest.json": json.dumps(build.manifest(17))})
    with pytest.raises(ScanError, match="incomplete or damaged"):
        _adapter().inspect(_file(tmp_path, data))


def _read_tables(version: int) -> tuple[str, ...]:
    """Every table the import reads of a database with Invoicing and Inventory installed."""
    common = (
        "res_company", "res_currency", "res_partner", "ir_attachment", "uom_uom", "product_product",
        "product_template", "account_analytic_account", "account_account", "account_tax",
        "account_tax_repartition_line", "account_move", "account_move_line", "account_move_line_account_tax_rel",
        "account_partial_reconcile", "account_payment", "stock_location", "stock_move", "stock_move_line",
        "stock_quant", "stock_valuation_layer")
    if version >= 16:
        return (*common, "account_analytic_plan", "stock_lot")
    return (*common, "ir_model_data", "account_account_type", "account_analytic_tag",
            "account_analytic_tag_account_move_line_rel", "stock_production_lot")


@pytest.mark.parametrize("version, table", [(v, n) for v in VERSIONS for n in _read_tables(v)])
def test_an_installed_module_missing_a_table_is_a_damaged_backup(tmp_path, version, table):
    """The manifest lists the modules installed, and each makes its tables even when they hold
    no rows: one missing is a backup cut or edited, never a module left out."""
    tables = build.tables(version)
    del tables.rows[table]
    with pytest.raises(ScanError, match="incomplete or damaged"):
        _adapter().inspect(_file(tmp_path, build.backup(version, tables)))


@pytest.mark.parametrize("version", VERSIONS)
def test_a_backup_with_inventory_installed_and_its_stock_removed_is_refused(tmp_path, version):
    tables = build.tables(version)
    for name in [n for n in tables.rows if n.startswith("stock_")]:
        del tables.rows[name]
    with pytest.raises(ScanError, match="incomplete or damaged"):
        _adapter().build_manifest(_file(tmp_path, build.backup(version, tables)), FULL)


@pytest.mark.parametrize("version", VERSIONS)
def test_a_module_that_is_not_installed_is_absent_not_damage(tmp_path, version):
    """A table Odoo never made, because its module is not installed, is not damage: a backup
    without Inventory imports its books without stock."""
    data = _uninstall(version, "stock", "stock_account", tables=("stock_",))
    manifest = _adapter().build_manifest(_file(tmp_path, data), FULL)
    assert manifest.bundle.inventory_adjustments == [] and manifest.bundle.documents
    assert len(manifest.bundle.documents) == len(_adapter().build_manifest(
        _file(tmp_path, _backup(version), "whole.zip"), FULL).bundle.documents)


def test_a_table_the_import_does_not_read_may_be_missing(tmp_path):
    tables = build.tables(17)
    del tables.rows["ir_ui_view"]
    assert _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle.documents


@pytest.mark.parametrize("modules", [None, {}, ["base", "account", "stock"], {"account": "17.0.1.0"}])
def test_a_manifest_that_does_not_list_the_installed_modules_is_refused(tmp_path, modules):
    """Without the list of modules installed, a missing table cannot be told from a missing
    module, so nothing proves the backup whole."""
    manifest = build.manifest(17)
    if modules is None:
        del manifest["modules"]
    else:
        manifest["modules"] = modules
    with pytest.raises(SourceRevisionError, match="incomplete or damaged"):
        _adapter().inspect(_file(tmp_path, build.backup(17, manifest_=manifest)))


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["odoo", None])
@pytest.mark.parametrize("modules", [None, {"account": "17.0.1.0"}])
async def test_a_manifest_without_the_modules_is_refused_as_a_damaged_odoo_backup(client, monkeypatch, tmp_path,
                                                                                   source, modules):
    """A manifest with Odoo's version and database name is an Odoo backup, chosen or detected:
    without its modules it is refused with the reason it cannot be read, not as another file."""
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    manifest = build.manifest(17)
    if modules is None:
        del manifest["modules"]
    else:
        manifest["modules"] = modules
    r = await scan_upload(client, build.backup(17, manifest_=manifest), name="harbour.zip", source=source)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_odoo_dump_damaged", "en")


def _corrupt(data: bytes, at: bytes, offset: int = 0, garbage: bytes = b"\xff") -> bytes:
    """`data` with `garbage` written where `at` starts, `offset` bytes in."""
    start = data.index(at) + offset
    return data[:start] + garbage + data[start + len(garbage):]


def _stored(dump: bytes) -> bytes:
    """A backup zip whose dump is stored as it is, so its bytes can be changed in place."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("dump.sql", dump)
        z.writestr("manifest.json", json.dumps(build.manifest(17)))
        z.writestr(f"filestore/{build.store_name(_FILED)}", _FILED)
    return out.getvalue()


# The content of a file the fixture keeps in the filestore only.
_FILED = next(content for *_, content, in_db, in_store in build.ATTACHMENTS if in_store and not in_db)


def _packed(dump: str, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    """A backup zip with `dump`, packed with `method`, and the fixture's manifest."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as z:
        z.writestr("dump.sql", dump)
        z.writestr("manifest.json", json.dumps(build.manifest(17)))
    return out.getvalue()


def _packed_dump(data: bytes) -> tuple[int, int]:
    """Where the packed dump starts in a backup zip whose first file is the dump, and its length."""
    info = zipfile.ZipFile(io.BytesIO(data)).getinfo("dump.sql")
    name_length, extra_length = int.from_bytes(data[26:28], "little"), int.from_bytes(data[28:30], "little")
    return info.header_offset + 30 + name_length + extra_length, info.compress_size


def _broken(data: bytes) -> bytes:
    """`data` with 64 bytes in the middle of its packed dump overwritten."""
    start, length = _packed_dump(data)
    middle = start + length // 2
    return data[:middle] + b"\xff" * 64 + data[middle + 64:]


def _encrypted(data: bytes) -> bytes:
    """`data` with every file flagged as encrypted, as in a zip protected with a password."""
    out = bytearray(data)
    for signature, flags in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = out.find(signature)
        while at != -1:
            out[at + flags] |= 1
            at = out.find(signature, at + 4)
    return bytes(out)


def _unreadable() -> dict[str, tuple[bytes, str | None]]:
    sql = build.dump_sql(build.tables(17))
    dump = sql.encode()
    whole = build.backup(17)
    gzipped = io.BytesIO()
    with gzip.GzipFile(fileobj=gzipped, mode="wb") as g:
        g.write(dump)
    damaged, backup_damaged = "migration.err_odoo_dump_damaged", "migration.err_odoo_backup_damaged"
    return {
        # A name in a row that is not UTF-8, which every Odoo database is.
        "invalid UTF-8 in a row": (_stored(_corrupt(dump, b"Lakeshore Trading Co", 4, b"\xc3\x28")), damaged),
        # A byte of the dump changed after it was zipped: its checksum no longer matches.
        "a changed byte in the dump": (_corrupt(_stored(dump), b"Lakeshore Trading Co", 0, b"l"), backup_damaged),
        "a changed byte in a filestore file": (_corrupt(_stored(dump), _FILED, 0, bytes([_FILED[0] ^ 1])),
                                               backup_damaged),
        # A damaged stream can unpack to wrong bytes before its checksum is reached.
        "a changed byte at the start of the dump": (_corrupt(_stored(dump), b"PostgreSQL database dump", 0, b"p"),
                                                    backup_damaged),
        "a broken compressed stream": (_broken(whole), backup_damaged),
        # Other tools than Odoo zip with bzip2 or LZMA too.
        "a broken bzip2 stream": (_broken(_packed(sql, zipfile.ZIP_BZIP2)), backup_damaged),
        "a broken LZMA stream": (_broken(_packed(sql, zipfile.ZIP_LZMA)), backup_damaged),
        "a password on the zip": (_encrypted(whole), "migration.err_odoo_backup_encrypted"),
        "an upload cut short": (whole[:len(whole) // 2], backup_damaged),
        "no dump": (_zip({"manifest.json": json.dumps(build.manifest(17))}), backup_damaged),
        # Not a zip: the upload is refused as a file of another kind, as any other would be.
        "a gzipped dump": (gzipped.getvalue(), None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", list(_unreadable()))
async def test_a_backup_that_cannot_be_read_is_refused_with_its_reason(client, monkeypatch, tmp_path, damage):
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    data, message = _unreadable()[damage]
    r = await scan_upload(client, data, name="harbour.zip", source="odoo")
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (t(message, "en") if message else
                                  '"harbour.zip" is not a Odoo file. Accepted: Odoo backup (.zip).')


def test_a_backup_zipped_with_bzip2_or_lzma_is_read_as_one_zipped_by_odoo(tmp_path):
    sql = build.dump_sql(build.tables(17))
    whole = _adapter().build_manifest(_file(tmp_path, _packed(sql)), FULL).bundle
    for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
        assert _adapter().build_manifest(_file(tmp_path, _packed(sql, method), f"{method}.zip"), FULL).bundle == whole


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_any_changed_byte_is_read_or_refused_with_a_reason(tmp_path, method):
    """One bit of the backup flipped anywhere, 150 times: detection and the reading either
    succeed or refuse with the reason; no other error escapes."""
    whole = _packed(build.dump_sql(build.tables(17)), method)
    rng = random.Random(method)
    escaped = []
    for i in range(150):
        at = rng.randrange(len(whole))
        files = _file(tmp_path, whole[:at] + bytes([whole[at] ^ (1 << rng.randrange(8))]) + whole[at + 1:], f"{i}.zip")
        try:
            if _adapter().detect(files).matched:
                _adapter().build_manifest(files, FULL)
        except (ScanError, SourceRevisionError):
            pass
        except Exception as exc:  # noqa: BLE001 - the test lists every error that escapes
            escaped.append((at, type(exc).__name__, str(exc)[:80]))
    assert not escaped, escaped[:8]


@pytest.mark.asyncio
async def test_a_dump_that_unpacks_far_larger_than_its_zip_is_refused_before_it_is_read(client, monkeypatch, tmp_path):
    """A dump packed a thousand to one, as a run of one character packs, is not what a database
    makes: it is refused from the size the zip declares, before a byte of it is unpacked."""
    from celerp.config import settings
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    sql = build.dump_sql(build.tables(17))
    padded = sql.replace("SET statement_timeout", "-- " + "a" * 20 * 1024 ** 2 + "\nSET statement_timeout", 1)
    opened = []
    monkeypatch.setattr(dump.Backup, "tables", lambda *a, **k: opened.append(a) or {})
    r = await scan_upload(client, _packed(padded), name="harbour.zip", source="odoo")
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_file_too_large", "en") and not opened


def test_a_dump_larger_than_the_budget_is_refused_and_one_within_it_is_read(tmp_path, monkeypatch):
    from celerp.importers.adapters.odoo import dump

    data = _backup(17)
    size = zipfile.ZipFile(io.BytesIO(data)).getinfo("dump.sql").file_size
    monkeypatch.setattr(dump, "MAX_DUMP_BYTES", size)
    assert _adapter().build_manifest(_file(tmp_path, data), FULL).bundle.documents
    monkeypatch.setattr(dump, "MAX_DUMP_BYTES", size - 1)
    with pytest.raises(SourceRevisionError, match="too large"):
        _adapter().inspect(_file(tmp_path, data, "again.zip"))


def test_a_manifest_larger_than_any_odoo_writes_is_refused_before_it_is_read(tmp_path, monkeypatch):
    from celerp.importers.adapters.odoo import dump

    data = _backup(17)
    size = zipfile.ZipFile(io.BytesIO(data)).getinfo("manifest.json").file_size
    monkeypatch.setattr(dump, "MAX_MANIFEST", size)
    assert _adapter().inspect(_file(tmp_path, data))
    monkeypatch.setattr(dump, "MAX_MANIFEST", size - 1)
    with pytest.raises(SourceRevisionError, match="too large"):
        _adapter().inspect(_file(tmp_path, data, "again.zip"))


MI = 1024 ** 2


def _amplified(contacts: int, size: int) -> bytes:
    """A backup with `contacts` more contacts, each named with `size` characters of its own,
    and lines of random text before them so that the dump unpacks about 90 times its packed
    size: inside MAX_RATIO, as a file made to fill the server's memory would be."""
    tables = build.tables(17)
    sql = build.dump_sql(tables)
    start = sql.index("COPY public.res_partner (")
    at = sql.index("\\.\n", start)
    columns = tables.rows["res_partner"][0]
    rng = random.Random(7)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z, z.open("dump.sql", "w", force_zip64=True) as d:
        d.write(sql[:start].encode())
        d.write(("-- " + rng.randbytes(contacts * size // 90).hex() + "\n").encode())
        d.write(sql[start:at].encode())
        for i in range(contacts):
            values = {"id": str(900000 + i), "name": f"{i:x}".rjust(size, "a"), "active": "t"}
            d.write(("\t".join(values.get(c, "\\N") for c in columns) + "\n").encode())
        d.write(sql[at:].encode())
    with zipfile.ZipFile(out, "a") as z:
        z.writestr("manifest.json", json.dumps(build.manifest(17)))
    return out.getvalue()


def test_rows_needing_more_memory_than_the_budget_are_refused_with_what_is_held_bounded(tmp_path, monkeypatch):
    """A file packed inside every size limit can still unpack to rows that fill the server's
    memory: what the rows read take is counted, and the backup is refused once it passes
    MAX_HELD, holding about that much and never what the file unpacks to. In miniature: a
    budget of 4 MiB and 64 MiB of names."""
    from celerp.importers.adapters.odoo import dump

    data = _amplified(64, MI)
    info = zipfile.ZipFile(io.BytesIO(data)).getinfo("dump.sql")
    assert 50 < info.file_size / info.compress_size < dump.MAX_RATIO
    monkeypatch.setattr(dump, "MAX_HELD", 4 * MI)
    assert _adapter().build_manifest(_file(tmp_path, _backup(17)), FULL).bundle.documents
    tracemalloc.start()
    try:
        with pytest.raises(ScanError) as refused:
            _adapter().inspect(_file(tmp_path, data, "amplified.zip"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en", limit=f"{4 * MI / 1024 ** 3:g}")
    assert peak < 16 * MI, f"held {peak / MI:.0f} MiB"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["the dump", "the books"])
async def test_running_out_of_memory_while_reading_is_refused_as_too_large(client, monkeypatch, tmp_path, stage):
    from celerp.config import settings
    from celerp.importers.adapters.odoo import book, dump

    def exhausted(*_):
        raise MemoryError

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(*((dump, "_field", exhausted) if stage == "the dump" else (book.steps, "roles_from_ledger", exhausted)))
    r = await scan_upload(client, _backup(17), name="harbour.zip", source="odoo")
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_odoo_backup_too_large", "en", limit=f"{dump.MAX_HELD / 1024 ** 3:g}")


def _longest_line(version: int) -> int:
    return max(len(line) for line in build.dump_sql(build.tables(version)).split("\n"))


def test_a_record_longer_than_the_line_limit_is_refused(tmp_path, monkeypatch):
    from celerp.importers.adapters.odoo import dump

    tables = build.tables(17)
    columns, rows = tables.rows["res_partner"]
    rows[0][columns.index("name")] = "x" * 5000
    monkeypatch.setattr(dump, "MAX_LINE", 4000)
    with pytest.raises(ScanError) as refused:
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))
    assert str(refused.value) == t("migration.err_odoo_record_too_large", "en", limit=f"{4000 / 10 ** 6:g}")
    assert "4000" not in str(refused.value) and "MB" not in str(refused.value)


def _with_attachment(**values) -> build.Tables:
    """The fixture with one more file, on a contact Celerp imports."""
    tables = build.tables(17)
    columns, rows = tables.rows["ir_attachment"]
    if "index_content" in values:  # the text Odoo extracts from a text file it indexes, uncapped
        columns.append("index_content")
        for r in rows:
            r.append(None)
    row = dict.fromkeys(columns)
    row.update({"id": 99001, "res_id": 7, "company_id": 1, "file_size": 13, "name": "export.txt",
                "res_model": "res.partner", "type": "binary", "mimetype": "text/plain", **values})
    rows.append([row[c] for c in columns])
    return tables


def _read(tmp_path, tables: build.Tables, name: str):
    manifest = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables), name), FULL)
    return manifest.bundle, manifest.source_summary


def test_a_long_column_the_import_drops_does_not_count_toward_the_line_limit(tmp_path, monkeypatch):
    """Odoo keeps the text of an indexed text file in its ir_attachment row; the import never
    keeps that column, so however long it is the backup reads as if it were short."""
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(dump, "MAX_LINE", _longest_line(17))
    short = _read(tmp_path, _with_attachment(index_content="w", store_fname=build.store_name(b"Net 30 days.\n"),
                                             checksum=None), "short.zip")
    long = _read(tmp_path, _with_attachment(index_content="w" * 3 * dump.MAX_LINE,
                                            store_fname=build.store_name(b"Net 30 days.\n"), checksum=None), "long.zip")
    assert long == short
    assert "attachment:99001" in {a.source_external_id for a in long[0].attachments}


@pytest.mark.parametrize("size", ["over the file limit", "over the line limit"])
def test_a_file_in_the_database_too_large_to_import_is_rejected_alone(tmp_path, monkeypatch, size):
    """A file kept in the database larger than the import takes is rejected alone and listed with
    the others left out, whether its row fits the line limit or not; the rest reads as it would
    without the file's content. In miniature: files up to 100 bytes."""
    from celerp.importers.adapters.odoo import book, dump

    longest = _longest_line(17)
    monkeypatch.setattr(dump, "MAX_LINE", longest)
    monkeypatch.setattr(book, "MAX_ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 100)
    content = bytes(101 if size == "over the file limit" else longest)
    assert (len(content) * 2 > longest) == (size == "over the line limit")
    stored = _read(tmp_path, _with_attachment(db_datas="\\x" + content.hex()), "stored.zip")
    missing = _read(tmp_path, _with_attachment(), "missing.zip")
    assert stored == missing
    rejected = {r["source_external_id"]: r["reason"] for r in stored[1]["attachments"]["rejected"]}
    assert rejected["attachment:99001"] == t("migration.odoo_attachment_unreadable", "en")


def test_an_attachment_row_too_long_to_read_is_rejected_alone(tmp_path, monkeypatch):
    """An attachment whose kept text is longer than the line limit is left out by its row id and
    listed with the others left out; the backup is not refused and everything else reads."""
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(dump, "MAX_LINE", _longest_line(17))
    bundle, summary = _read(tmp_path, _with_attachment(name="n" * 2 * dump.MAX_LINE), "long.zip")
    plain, plain_summary = _read(tmp_path, build.tables(17), "plain.zip")
    assert bundle == plain
    rejected = {r["source_external_id"]: r["reason"] for r in summary["attachments"]["rejected"]}
    assert rejected == {r["source_external_id"]: r["reason"] for r in plain_summary["attachments"]["rejected"]} | {
        "attachment:99001": t("migration.odoo_attachment_unreadable", "en")}


def test_an_attachment_row_too_long_to_read_without_an_id_refuses_the_backup(tmp_path, monkeypatch):
    """A row left out is listed by its id; one whose leading field is not an id is not listed
    (a long one would be held for every such row) but refuses the backup."""
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(dump, "MAX_LINE", _longest_line(17))
    with pytest.raises(ScanError) as refused:
        _read(tmp_path, _with_attachment(id="9" * 20, name="n" * 2 * dump.MAX_LINE), "long.zip")
    assert str(refused.value) == t("migration.err_odoo_record_too_large", "en",
                                   limit=f"{_longest_line(17) / 10 ** 6:g}")


def test_a_row_of_another_table_too_long_to_read_refuses_the_backup_even_in_pieces(tmp_path, monkeypatch):
    """Over the line limit outside attachments the backup is refused, also when the long value
    sits after columns the import drops."""
    from celerp.importers.adapters.odoo import dump

    tables = build.tables(17)
    columns, rows = tables.rows["res_partner"]
    columns.insert(1, "comment_dropped")
    for r in rows:
        r.insert(1, "c" * 3000)
    rows[0][columns.index("name")] = "x" * 5000
    monkeypatch.setattr(dump, "MAX_LINE", 4000)
    with pytest.raises(ScanError) as refused:
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))
    assert str(refused.value) == t("migration.err_odoo_record_too_large", "en", limit=f"{4000 / 10 ** 6:g}")


def test_a_long_line_the_import_does_not_read_is_passed_over_and_the_rest_read(tmp_path, monkeypatch):
    """A long line in a table the import does not read, or outside any table's data, is read in
    pieces and dropped; the lines after it are read as they are."""
    from celerp.importers.adapters.odoo import dump

    tables = build.tables(17)
    columns, rows = tables.rows["ir_ui_view"]
    rows[0][columns.index("arch_db")] = "x" * 50_000
    sql = build.dump_sql(tables).replace("SET statement_timeout", "-- " + "c" * 50_000 + "\nSET statement_timeout", 1)
    whole = _adapter().build_manifest(_file(tmp_path, _backup(17)), FULL).bundle
    monkeypatch.setattr(dump, "MAX_LINE", _longest_line(17))
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(build.backup(17)), "r") as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            z.writestr(info.filename, sql if info.filename == "dump.sql" else src.read(info))
    read = _adapter().build_manifest(_file(tmp_path, out.getvalue(), "long.zip"), FULL).bundle
    assert read == whole


@pytest.mark.parametrize("version", VERSIONS)
def test_the_longest_line_of_a_dump_at_the_limit_is_read(tmp_path, monkeypatch, version):
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(dump, "MAX_LINE", _longest_line(version))
    assert _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle.documents


def test_a_document_whose_lines_do_not_add_up_is_refused(tmp_path):
    tables = build.tables(17)
    columns, rows = tables.rows["account_move"]
    row = next(r for r in rows if r[columns.index("name")] == "INV/2024/00002")
    row[columns.index("amount_total")] = "231.00"
    with pytest.raises(ScanError, match="INV/2024/00002 do not add up"):
        _adapter().inspect(_file(tmp_path, build.backup(17, tables)))


# ── Masters ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_chart_taxes_contacts_and_products(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    accounts = {a.code: (a.account_type.value, a.control and a.control.value) for a in bundle.accounts}
    assert accounts == {
        "101000": ("asset", "bank"), "101401": ("asset", "bank"), "101402": ("asset", "bank"),
        "121000": ("asset", "receivable"), "211000": ("liability", "payable"), "251000": ("liability", "tax"),
        "131000": ("asset", "tax"), "400000": ("revenue", None), "600000": ("expense", None),
        "500000": ("cogs", None), "110100": ("asset", None), "110200": ("asset", None),
        "999999": ("equity", "retained_earnings"), "300000": ("equity", None),
        "441000": ("revenue", "exchange_difference"), "641000": ("expense", None),
        "115000": ("asset", "tax"), "215000": ("liability", "tax"),
    }
    assert {(t.name, t.rate_percent, t.account_external_id) for t in bundle.tax_codes} == {
        ("15%", D(15), "251000"), ("15% [2]", D(15), "131000"), ("PST 7%", D(7), "251000"),
        ("WHT 3%", D(-3), "115000"), ("WHT 3% [5]", D(-3), "215000")}
    contacts = {c.name: (sorted(c.roles), c.email) for c in bundle.contacts}
    # A contact person is its company; internal users and the company itself are not contacts;
    # a second contact of the same name is told apart by its id.
    assert contacts == {
        "Lakeshore Trading Co": (["customer"], "accounts@lakeshore.example"),
        "Lakeshore Trading Co [11]": (["customer"], "other@lakeshore.example"),
        "Pine & Co": (["customer"], "hello@pine.example"),
        "Northwind Wholesale": (["supplier"], "billing@northwind.example"),
    }
    lakeshore = next(c for c in bundle.contacts if c.name == "Lakeshore Trading Co")
    assert lakeshore.address == "1 Quay Road, PO1 1AA Portsmouth"
    assert next(c for c in bundle.contacts if c.name == "Northwind Wholesale").tax_id == "GB123456789"
    assert {(i.sku, i.name, i.retail_price, i.status) for i in bundle.items} == {
        ("WID-1", "Widget", D(50), "available"), ("SRV", "Consulting", D(100), "available")}


# ── Documents, payments and journals ─────────────────────────────────────────

def _documents(manifest) -> list[tuple]:
    return sorted((d.contact_external_id, d.issue_date, d.status, d.total, d.amount_outstanding)
                  for d in manifest.bundle.documents)


@pytest.mark.parametrize("renamed", [
    7,      # another customer's invoice, from another sales journal, numbered INV/2024/00001 too
    17,     # the same customer's later invoice numbered INV/2024/00001 again
])
def test_invoices_sharing_a_number_are_each_carried_as_documents(tmp_path, renamed):
    tables = build.tables(17)
    columns, rows = tables.rows["account_move"]
    next(r for r in rows if r[0] == renamed)[columns.index("name")] = "INV/2024/00001"
    before = _adapter().build_manifest(_file(tmp_path, _backup(17)), FULL)
    after = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables), "renamed.zip"), FULL)
    # Every invoice is still a document of its own customer with what it was owed, and the
    # journals left to fall back to are the same ones.
    assert len(after.bundle.documents) == len(before.bundle.documents)
    assert _documents(after) == _documents(before)
    assert sorted(j.source_external_id for j in after.bundle.journals) == \
        sorted(j.source_external_id for j in before.bundle.journals)


@pytest.mark.parametrize("version", VERSIONS)
def test_invoices_bills_payments_and_journals(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    assert {k: (d.doc_type.value, d.status, d.total, d.amount_outstanding) for k, d in docs.items()} == {
        "P:BILL/2024/01/0001": ("bill", "paid", D("345.00"), D(0)),
        "S:INV/2024/00001": ("invoice", "paid", D("345.00"), D(0)),
        "S:INV/2024/00002": ("invoice", "awaiting_payment", D("230.00"), D("130.00")),
        "S:RINV/2024/00001": ("credit_note", "awaiting_payment", D("57.50"), D("57.50")),
        "P:BILL/2024/03/0001": ("bill", "awaiting_payment", D("230.00"), D("195.50")),
        "P:RBILL/2024/03/0001": ("debit_note", "paid", D("34.50"), D(0)),
        "S:INV/2024/00004": ("invoice", "paid", D(100), D(0)),
        "S:Draft 14": ("invoice", "draft", D("115.00"), D(0)),
        "S:INV/2024/00003": ("invoice", "void", D("57.50"), D(0)),
        "S:INV/2024/00005": ("invoice", "awaiting_payment", D("179.50"), D("179.50")),
        "S:INV/2024/00006": ("invoice", "awaiting_payment", D(200), D(194)),
        "P:BILL/2024/04/0001": ("bill", "awaiting_payment", D(112), D(112)),
    }
    invoice = docs["S:INV/2024/00001"]
    assert invoice.contact_external_id == "Lakeshore Trading Co"
    assert [(ln.item_external_id, ln.quantity, ln.unit_price, ln.total_price, ln.account_external_id)
            for ln in invoice.line_items] == [("WID-1", D(4), D(50), D(200), "400000"),
                                              ("SRV", D(1), D(100), D(100), "400000")]
    assert _line_taxes(invoice) == [[("15%", "251000", D(30))], [("15%", "251000", D(15))]]
    assert docs["S:INV/2024/00002"].contact_external_id == "Pine & Co"       # billed to its contact person
    assert docs["P:RBILL/2024/03/0001"].metadata == {"applications": {"P:BILL/2024/03/0001": "34.50"}}
    assert docs["S:RINV/2024/00001"].metadata == {}                          # open, applied to nothing
    euro = docs["S:INV/2024/00004"]
    assert (euro.currency, euro.exchange_rate) == ("EUR", D("1.1"))

    settlements = {s.source_external_id: (s.settlement_type.value, s.amount, s.bank_account_external_id,
                                          s.allocations[0].document_external_id) for s in bundle.settlements}
    assert settlements == {
        "J:6": ("receipt", D(345), "101401", "S:INV/2024/00001"),
        "J:8": ("receipt", D(100), "101401", "S:INV/2024/00002"),
        "J:10": ("payment", D(345), "101402", "P:BILL/2024/01/0001"),
        "J:18": ("receipt", D(100), "101401", "S:INV/2024/00004"),
    }
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.12")
    journals = {j.source_external_id: j for j in bundle.journals}
    # Entries that are no document or payment; the draft entry is not carried. The EUR payment
    # posts at its own rate where Celerp relieves the invoice at the invoice's, so the difference
    # rides on it as a fallback, and Odoo's exchange difference entry is carried as itself.
    assert set(journals) == {"J:1", "J:3", "J:5", "J:11", "J:19", "J:20", "J:21", "J:18:journal"}
    assert journals["J:18:journal"].fallback is True
    assert (journals["J:1"].source_type, journals["J:1"].source_ref) == ("Journal Entry", "Capital paid in")


@pytest.mark.parametrize("version", VERSIONS)
def test_only_lines_on_off_balance_accounts_are_counted_off_balance(tmp_path, version):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    rows = {r.source_type: (r.count, r.coverage_class) for r in manifest.coverage}
    # The guarantee's two lines; the invoice's section and note lines are layout, not items.
    assert rows["Off-balance journal item"] == (2, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    invoice = next(d for d in manifest.bundle.documents if d.source_external_id == "S:INV/2024/00001")
    assert len(invoice.line_items) == 2
    assert "J:26" not in {j.source_external_id for j in manifest.bundle.journals}


# ── Taxes on document lines ───────────────────────────────────────────────────

def _line_taxes(doc) -> list[list[tuple]]:
    return [[(x.tax_code_external_id, x.tax_account_external_id, x.amount) for x in ln.taxes]
            for ln in doc.line_items]


@pytest.mark.parametrize("version", VERSIONS)
def test_each_tax_on_a_line_posts_to_its_own_account_as_odoo_split_it(tmp_path, version):
    """A line with two taxes carries both; a bill's withholding is a tax of its own on its own
    account; a sale's withholding is tax the customer kept back, applied to the invoice from
    the withholding account, because a sale holds no tax below zero."""
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    docs = {d.source_external_id: d for d in bundle.documents}
    two = docs["S:INV/2024/00005"]
    assert _line_taxes(two) == [[("15%", "251000", D(15)), ("PST 7%", "251000", D(7))],
                                [("15%", "251000", D("7.50"))]]
    assert (two.total, two.tax_total, two.amount_outstanding) == (D("179.50"), D("29.50"), D("179.50"))

    withheld = docs["S:INV/2024/00006"]
    assert _line_taxes(withheld) == [[]]
    assert (withheld.total, withheld.tax_total, withheld.amount_outstanding) == (D(200), D(0), D(194))
    assert withheld.metadata == {"withheld": {"115000": "6.00"}}

    bill = docs["P:BILL/2024/04/0001"]
    assert _line_taxes(bill) == [[("15% [2]", "131000", D(15)), ("WHT 3% [5]", "215000", D(-3))]]
    assert (bill.total, bill.tax_total, bill.amount_outstanding) == (D(112), D(12), D(112))
    assert all(x.tax_account_external_id for d in docs.values() for ln in d.line_items for x in ln.taxes)
    assert {(t.name, t.rate_percent, t.account_external_id) for t in bundle.tax_codes} >= {
        ("PST 7%", D(7), "251000"), ("WHT 3%", D(-3), "115000"), ("WHT 3% [5]", D(-3), "215000")}


def test_odoo_rounding_a_tax_on_the_whole_document_lands_on_that_tax(tmp_path):
    """Odoo can round each tax on the document's total: the difference lands on the last
    line carrying that tax, so each tax's total is Odoo's."""
    tables = build.tables(17)
    cols = tables.rows["account_move_line"][0]
    pst = next(r for r in _lines_of(tables, 22) if r[cols.index("display_type")] == "tax"
               and r[cols.index("tax_line_id")] == build.PROVINCIAL_TAX)
    standard = next(r for r in _lines_of(tables, 22) if r[cols.index("display_type")] == "tax"
                    and r[cols.index("tax_line_id")] == build.SALE_TAX)
    # 0.01 moves from the standard tax to the provincial one; the total stays.
    _set(tables, pst, balance="-7.01", amount_currency="-7.01", credit="7.01")
    _set(tables, standard, balance="-22.49", amount_currency="-22.49", credit="22.49")
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    doc = next(d for d in bundle.documents if d.source_external_id == "S:INV/2024/00005")
    assert _line_taxes(doc) == [[("15%", "251000", D(15)), ("PST 7%", "251000", D("7.01"))],
                                [("15%", "251000", D("7.49"))]]


def test_a_tax_split_keeps_the_currencys_cents_when_odoo_stored_fewer(tmp_path):
    """Odoo writes floats and Postgres keeps the scale written, so a 105.00 line with 15% and
    7% tax can be stored as 105.0 and 128.1. Each tax is still split to the cent: 15.75 and
    7.35, and the next line's 15% stays 7.50."""
    tables = build.tables(17)
    by_id = {r[0]: r for r in tables.rows["account_move_line"][1]}
    _set(tables, by_id[52], price_unit="105.0", price_subtotal="105.0", price_total="128.1", credit="105.0",
         balance="-105.0", amount_currency="-105.0")
    _set(tables, by_id[54], credit="23.25", balance="-23.25", amount_currency="-23.25")
    _set(tables, by_id[55], credit="7.35", balance="-7.35", amount_currency="-7.35")
    _set(tables, by_id[56], debit="185.6", balance="185.6", amount_currency="185.6", amount_residual="185.6")
    mcols, moves = tables.rows["account_move"]
    move = next(m for m in moves if m[0] == 22)
    for column, value in (("amount_tax", "30.6"), ("amount_total", "185.6"), ("amount_residual", "185.6")):
        move[mcols.index(column)] = value
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    doc = next(d for d in bundle.documents if d.source_external_id == "S:INV/2024/00005")
    assert _line_taxes(doc) == [[("15%", "251000", D("15.75")), ("PST 7%", "251000", D("7.35"))],
                                [("15%", "251000", D("7.50"))]]
    assert (doc.total, doc.tax_total) == (D("185.6"), D("30.6"))


def test_a_tax_split_rounds_half_up_like_odoo(tmp_path):
    """Odoo rounds each tax half-up: a 10.70 line at 15% and 7% books 1.605 as 1.61 and 0.749 as
    0.75 (13.06 in all)."""
    tables = build.tables(17)
    by_id = {r[0]: r for r in tables.rows["account_move_line"][1]}
    _set(tables, by_id[52], price_unit="10.70", price_subtotal="10.70", price_total="13.06", credit="10.70",
         balance="-10.70", amount_currency="-10.70")
    _set(tables, by_id[54], credit="9.11", balance="-9.11", amount_currency="-9.11")
    _set(tables, by_id[55], credit="0.75", balance="-0.75", amount_currency="-0.75")
    _set(tables, by_id[56], debit="70.56", balance="70.56", amount_currency="70.56", amount_residual="70.56")
    mcols, moves = tables.rows["account_move"]
    move = next(m for m in moves if m[0] == 22)
    for column, value in (("amount_tax", "9.86"), ("amount_total", "70.56"), ("amount_residual", "70.56")):
        move[mcols.index(column)] = value
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    doc = next(d for d in bundle.documents if d.source_external_id == "S:INV/2024/00005")
    assert _line_taxes(doc) == [[("15%", "251000", D("1.61")), ("PST 7%", "251000", D("0.75"))],
                                [("15%", "251000", D("7.50"))]]
    assert (doc.total, doc.tax_total) == (D("70.56"), D("9.86"))


# ── Payments matched by reconciliation ────────────────────────────────────────

def _lines_of(tables: build.Tables, move: int) -> list[list]:
    cols, rows = tables.rows["account_move_line"]
    return [r for r in rows if r[cols.index("move_id")] == move]


def _set(tables: build.Tables, row: list, **values) -> None:
    cols = tables.rows["account_move_line"][0]
    for name, value in values.items():
        row[cols.index(name)] = value


def _party_line(tables: build.Tables, move: int) -> list:
    cols = tables.rows["account_move_line"][0]
    return next(r for r in _lines_of(tables, move) if r[cols.index("account_id")] in (4, 5))


def _resize(tables: build.Tables, move: int, amount: Decimal) -> None:
    """Every line of a two-line base currency move moved to `amount` either way."""
    cols = tables.rows["account_move_line"][0]
    for r in _lines_of(tables, move):
        new = amount if D(r[cols.index("balance")]) > 0 else -amount
        _set(tables, r, balance=f"{new}", amount_currency=f"{new}", debit=f"{max(new, D(0))}",
             credit=f"{max(-new, D(0))}")


def _reconcile(tables: build.Tables, debit: int, credit: int, amount: str, *own: str) -> None:
    """One more partial reconciliation between two moves' customer or supplier lines."""
    rows = tables.rows["account_partial_reconcile"][1]
    rows.append([len(rows) + 90, _party_line(tables, debit)[0], _party_line(tables, credit)[0], 1, amount,
                 "2024-04-30", *(own or (amount, amount))])


def _statement_line(tables: build.Tables, move: int) -> None:
    """A move posted by a bank statement line matched straight to documents: no account.payment."""
    pcols, payments = tables.rows["account_payment"]
    payments[:] = [p for p in payments if p[pcols.index("move_id")] != move]
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == move)[mcols.index("payment_id")] = None
    for r in _lines_of(tables, move):
        _set(tables, r, payment_id=None)


def _book(tmp_path, tables: build.Tables):
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL).bundle
    docs = {d.source_external_id: (d.status, d.amount_outstanding) for d in bundle.documents}
    settled = {s.source_external_id: (s.amount, [(a.document_external_id, a.amount) for a in s.allocations])
               for s in bundle.settlements}
    return bundle, docs, settled


def test_a_bank_statement_line_matched_to_an_invoice_pays_it(tmp_path):
    """Odoo's bank reconciliation matches a statement line straight to the invoice."""
    tables = build.tables(17)
    _statement_line(tables, 6)
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert settled["J:6"] == (D(345), [("S:INV/2024/00001", D(345))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:6").source_type == "Customer Payment"
    assert "J:6" not in {j.source_external_id for j in bundle.journals}


def test_one_payment_for_two_invoices_settles_each_by_what_was_applied(tmp_path):
    """Lakeshore pays INV/2024/00001 and 100.00 of INV/2024/00002 with one payment of 445.00."""
    tables = build.tables(17)
    mcols, moves = tables.rows["account_move"]
    second = next(m for m in moves if m[0] == 7)
    second[mcols.index("partner_id")] = second[mcols.index("commercial_partner_id")] = 7
    for r in _lines_of(tables, 7):
        _set(tables, r, partner_id=7)
    pcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[pcols.index("credit_move_id")] != _party_line(tables, 8)[0]]
    _set(tables, _party_line(tables, 8), amount_residual="-100.00")       # Pine's payment, applied to nothing
    _resize(tables, 6, D(445))
    _reconcile(tables, 7, 6, "100")
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert docs["S:INV/2024/00002"] == ("awaiting_payment", D(130))
    assert settled["J:6"] == (D(445), [("S:INV/2024/00001", D(345)), ("S:INV/2024/00002", D(100))])
    assert "J:8" not in settled and "J:8" in {j.source_external_id for j in bundle.journals}


def test_an_overpayment_settles_the_invoice_and_leaves_the_rest_as_credit(tmp_path):
    """400.00 received against a 345.00 invoice: Odoo reconciles 345.00 and leaves 55.00 open
    credit, which stays on the customer's balance as the payment's journal."""
    tables = build.tables(17)
    _resize(tables, 6, D(400))
    _set(tables, _party_line(tables, 6), amount_residual="-55.00")
    pcols, payments = tables.rows["account_payment"]
    next(p for p in payments if p[pcols.index("move_id")] == 6)[pcols.index("amount")] = "400"
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00001"] == ("paid", D(0))
    assert settled["J:6"] == (D(345), [("S:INV/2024/00001", D(345))])
    credit = next(j for j in bundle.journals if j.source_external_id == "J:6:journal")
    assert credit.fallback is True
    assert {(ln.account_external_id, ln.contact_external_id, ln.debit - ln.credit) for ln in credit.lines} == {
        ("121000", "Lakeshore Trading Co", D(-55)), ("101401", None, D(55))}


def test_a_base_currency_payment_of_a_foreign_invoice_is_allocated_in_the_invoice_currency(tmp_path):
    """INV/2024/00004 is EUR 100 at 1.10. The customer pays USD 55 from a USD account and Odoo
    reconciles EUR 50 of it: Celerp allocates EUR 50 at 1.10 and leaves EUR 50 open."""
    tables = build.tables(17)
    for r in _lines_of(tables, 18):
        _set(tables, r, currency_id=build.USD)
    _resize(tables, 18, D(55))
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    next(m for m in moves if m[0] == 17)[mcols.index("amount_residual")] = "50.00"
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("move_id")] == 18)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, "55"
    _set(tables, _party_line(tables, 17), amount_residual="55.00")
    rcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[rcols.index("amount")] not in ("110", "2")]
    tables.rows["account_move"][1][:] = [m for m in moves if m[0] != 19]            # no exchange difference
    tables.rows["account_move_line"][1][:] = [r for r in tables.rows["account_move_line"][1]
                                              if r not in _lines_of(tables, 19)]
    _reconcile(tables, 17, 18, "55", "50", "55")
    bundle, docs, settled = _book(tmp_path, tables)
    assert settled["J:18"] == (D(50), [("S:INV/2024/00004", D(50))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.1")
    assert docs["S:INV/2024/00004"] == ("awaiting_payment", D(50))


def test_a_base_currency_overpayment_of_a_foreign_invoice_settles_at_the_rate_odoo_applied(tmp_path):
    """The customer pays USD 66 from a USD account and Odoo reconciles EUR 50 / USD 55 of it
    with INV/2024/00004 (EUR 100 at 1.10). The settlement is EUR 50 at 1.10, the receipt books
    no exchange difference, and the USD 11 Odoo left open stays on the customer."""
    tables = build.tables(17)
    for r in _lines_of(tables, 18):
        _set(tables, r, currency_id=build.USD)
    _resize(tables, 18, D(66))
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    next(m for m in moves if m[0] == 17)[mcols.index("amount_residual")] = "50.00"
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("move_id")] == 18)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, "66"
    _set(tables, _party_line(tables, 17), amount_residual="55.00")
    _set(tables, _party_line(tables, 18), amount_residual="-11.00")
    rcols, partials = tables.rows["account_partial_reconcile"]
    partials[:] = [p for p in partials if p[rcols.index("amount")] not in ("110", "2")]
    _reconcile(tables, 17, 18, "55", "50", "55")
    bundle, docs, settled = _book(tmp_path, tables)
    assert settled["J:18"] == (D(50), [("S:INV/2024/00004", D(50))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.1")
    assert docs["S:INV/2024/00004"] == ("awaiting_payment", D(50))
    credit = next(j for j in bundle.journals if j.source_external_id == "J:18:journal")
    assert {(ln.account_external_id, ln.contact_external_id, ln.debit - ln.credit) for ln in credit.lines} == {
        ("121000", "Lakeshore Trading Co", D(-11)), ("101401", None, D(11))}


@pytest.mark.parametrize("currency, places, total, applied", [
    ("JPY", 0, "10001", D("5001")), ("EUR", 2, "100.01", D("50.01")), ("KWD", 3, "100.001", D("50.001"))])
@pytest.mark.parametrize("vendor", [False, True])
def test_odoo_13_rounds_a_base_currency_payment_of_a_foreign_document_in_its_currency(
        tmp_path, currency, places, total, applied, vendor):
    """Odoo 13 states a base currency reconciliation of a foreign invoice or bill only in the base
    currency. USD 55 of the USD 110 document is half of it, which falls on half the currency's
    smallest unit and rounds up, as Celerp rounds money, on either side of the ledger."""
    tables = build.tables(13)
    ccols, currencies = tables.rows["res_currency"]
    eur = next(c for c in currencies if c[0] == build.EUR)
    eur[ccols.index("name")], eur[ccols.index("decimal_places")] = currency, places
    product, party = _lines_of(tables, 17)
    _set(tables, product, amount_currency=f"-{total}", price_unit=total, price_subtotal=total, price_total=total)
    _set(tables, party, amount_currency=total)
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 17)[mcols.index("amount_total")] = total
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    _resize(tables, 18, D(55))
    for r in _lines_of(tables, 18):
        _set(tables, r, currency_id=build.USD)
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("currency_id")] == build.EUR)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, "55"
    moves[:] = [m for m in moves if m[0] != 19]
    lcols, lines = tables.rows["account_move_line"]
    lines[:] = [r for r in lines if r[lcols.index("move_id")] != 19]
    tables.rows["account_partial_reconcile"][1][:] = []
    if vendor:                                          # the same figures as a bill paid to a supplier
        for r in _lines_of(tables, 17) + _lines_of(tables, 18):
            new = -D(r[lcols.index("balance")])
            _set(tables, r, balance=f"{new}", amount_currency=f"{-D(r[lcols.index('amount_currency')])}",
                 debit=f"{max(new, D(0))}", credit=f"{max(-new, D(0))}", partner_id=10,
                 account_id=5 if r[lcols.index("account_id")] == 4 else r[lcols.index("account_id")])
        for m in moves:
            if m[0] in (17, 18):
                m[mcols.index("partner_id")] = m[mcols.index("commercial_partner_id")] = 10
        next(m for m in moves if m[0] == 17)[mcols.index("type")] = "in_invoice"
        pay[pcols.index("partner_id")], pay[pcols.index("partner_type")] = 10, "supplier"
        pay[pcols.index("payment_type")] = "outbound"
        _reconcile(tables, 18, 17, "55", "0", None)
    else:
        _reconcile(tables, 17, 18, "55", "0", None)
    bundle = _adapter().build_manifest(_file(tmp_path, build.backup(13, tables)), FULL).bundle
    receipt = next(s for s in bundle.settlements if s.source_external_id == "J:18")
    assert [a.amount for a in receipt.allocations] == [applied]
    doc = next(d for d in bundle.documents if d.source_external_id.endswith("INV/2024/00004"))
    assert doc.amount_outstanding == D(total) - applied


def _base_bank_receipt(tables: build.Tables, bank: str, applied: str, writeoff: str | None = None) -> None:
    """INV/2024/00004 (EUR 100 at 1.10, USD 110) paid in full by a USD receipt of `bank` from the
    USD bank, Odoo applying USD `applied` to it, with an optional bank charge line written off
    against the invoice. No exchange difference entry."""
    cols, rows = tables.rows["account_move_line"]
    mcols, moves = tables.rows["account_move"]
    next(m for m in moves if m[0] == 18)[mcols.index("currency_id")] = build.USD
    pcols, payments = tables.rows["account_payment"]
    pay = next(p for p in payments if p[pcols.index("move_id")] == 18)
    pay[pcols.index("currency_id")], pay[pcols.index("amount")] = build.USD, bank
    bank_line, party_line = _lines_of(tables, 18)
    _set(tables, bank_line, currency_id=build.USD, debit=bank, credit="0", balance=bank, amount_currency=bank,
         amount_residual=bank)
    _set(tables, party_line, currency_id=build.USD, debit="0", credit=applied, balance=f"-{applied}",
         amount_currency=f"-{applied}", amount_residual="0")
    if writeoff is not None:
        charge = list(bank_line)
        rows.append(charge)
        _set(tables, charge, id=48, account_id=9, debit=writeoff, balance=writeoff, amount_currency=writeoff,
             amount_residual="0", name="Bank charge")
    partials = tables.rows["account_partial_reconcile"][1]
    partials[:] = [p for p in partials if p[0] not in (5, 6)]
    partials.append([5, _party_line(tables, 17)[0], party_line[0], 1, applied, "2024-04-20", "100", applied])
    moves[:] = [m for m in moves if m[0] != 19]
    rows[:] = [r for r in rows if r[cols.index("move_id")] != 19]


def test_a_base_currency_receipt_with_a_write_off_pays_a_foreign_invoice(tmp_path):
    """USD 105 received and USD 5 written off as a bank charge settle INV/2024/00004 in full: Odoo
    applies USD 110 (EUR 100). The settlement is EUR 100 at 1.10, and the receipt's journal moves
    the charge off the bank, so every account matches Odoo and every journal balances."""
    tables = build.tables(17)
    _base_bank_receipt(tables, "105", "110", writeoff="5")
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00004"] == ("paid", D(0))
    assert settled["J:18"] == (D(100), [("S:INV/2024/00004", D(100))])
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.1")
    rest = next(j for j in bundle.journals if j.source_external_id == "J:18:journal")
    assert {(ln.account_external_id, ln.debit - ln.credit) for ln in rest.lines} == {
        ("600000", D(5)), ("101401", D(-5))}
    for j in bundle.journals:
        assert sum(ln.debit - ln.credit for ln in j.lines) == 0, j.source_external_id


def test_a_base_currency_receipt_without_a_write_off_settles_at_the_bank_amount(tmp_path):
    """USD 108 received and applied to EUR 100: the settlement's rate is 1.08 and every journal balances."""
    tables = build.tables(17)
    _base_bank_receipt(tables, "108", "108")
    bundle, docs, settled = _book(tmp_path, tables)
    assert docs["S:INV/2024/00004"] == ("paid", D(0))
    assert next(s for s in bundle.settlements if s.source_external_id == "J:18").exchange_rate == D("1.08")
    for j in bundle.journals:
        assert sum(ln.debit - ln.credit for ln in j.lines) == 0, j.source_external_id


@pytest.mark.asyncio
async def test_a_statement_line_reconciled_invoice_migrates_paid(real_engine, monkeypatch, tmp_path):
    tables = build.tables(17)
    _statement_line(tables, 6)
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, tables))],
                                      {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)

# ── Credit notes applied in part ──────────────────────────────────────────────

def _note_for_pine(applied: bool = True) -> build.Tables:
    """The open credit note RINV/2024/00001 (57.50) issued to Pine & Co, which Odoo shows
    applied 20.00 to Pine & Co's INV/2024/00002: the invoice owes 110.00, the note 37.50 open.
    Without `applied` the reconciliation is missing while Odoo still states those amounts."""
    tables = build.tables(17)
    mcols, moves = tables.rows["account_move"]
    note = next(m for m in moves if m[0] == 9)
    note[mcols.index("partner_id")] = note[mcols.index("commercial_partner_id")] = 8
    for r in _lines_of(tables, 9):
        _set(tables, r, partner_id=8)
    if applied:
        _reconcile(tables, 7, 9, "20.00")
    next(m for m in moves if m[0] == 7)[mcols.index("amount_residual")] = "110.00"
    next(m for m in moves if m[0] == 9)[mcols.index("amount_residual")] = "37.50"
    _set(tables, _party_line(tables, 7), amount_residual="110.00")
    _set(tables, _party_line(tables, 9), amount_residual="-37.50")
    return tables


def test_a_credit_note_applied_in_part_relieves_the_invoice_by_that_amount(tmp_path):
    bundle, docs, _ = _book(tmp_path, _note_for_pine())
    assert docs["S:INV/2024/00002"] == ("awaiting_payment", D(110))
    assert docs["S:RINV/2024/00001"] == ("awaiting_payment", D("37.50"))
    notes = {d.source_external_id: d.metadata for d in bundle.documents}
    assert notes["S:RINV/2024/00001"] == {"applications": {"S:INV/2024/00002": "20.00"}}
    assert notes["P:RBILL/2024/03/0001"] == {"applications": {"P:BILL/2024/03/0001": "34.50"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
async def test_a_credit_note_applied_in_part_migrates_with_both_open_amounts(real_engine, monkeypatch, tmp_path,
                                                                             decisions):
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _note_for_pine()))],
                                      decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    owed = {r["key"]: D(r["celerp"]) for r in run.reconciliation["rows"] if r["check"] == "document_residual"}
    assert (owed["S:INV/2024/00002"], owed["S:RINV/2024/00001"]) == (D(110), D("37.50"))


@pytest.mark.asyncio
async def test_a_wrong_open_amount_on_one_document_fails_its_own_check(real_engine, monkeypatch, tmp_path):
    """Odoo states the open amounts of an application the backup does not hold. The invoice
    (+20.00) and the note (-20.00) cancel in every total, so only each document's own check
    can notice, and it names both."""
    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _note_for_pine(False)))],
                                      {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    failing = {(r["check"], r["key"]) for r in run.reconciliation["rows"] if r["result"] not in ("pass", "rounding")}
    assert failing == {("document_residual", "S:INV/2024/00002"), ("document_residual", "S:RINV/2024/00001")}


# ── Analytic distributions ────────────────────────────────────────────────────

# The consulting line of INV/2024/00001 as tracking: from Odoo 16 by plan with each share,
# before as its one analytic account and its tags.
ANALYTIC = {16: {"Projects": "Project Alpha 60%, Project Beta 40%", "Regions": "North"},
            13: {"Analytic account": "Project Alpha", "Analytic tag": "North"}}


def _analytic(version: int) -> dict[str, str]:
    return ANALYTIC[16 if version >= 16 else 13]


@pytest.mark.parametrize("version", VERSIONS)
def test_analytic_distributions_ride_on_document_lines_and_are_reported(tmp_path, version):
    from celerp.importers.adapters.exports.bundle import TRACKING
    from celerp.importers.adapters.odoo.book import read_odoo
    from celerp.importers.adapters.odoo.dump import Backup

    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    docs = {d.source_external_id: d for d in manifest.bundle.documents}
    assert [ln.tracking for ln in docs["S:INV/2024/00001"].line_items] == [{}, _analytic(version)]
    assert all(not ln.tracking for d in docs.values() if d.source_external_id != "S:INV/2024/00001"
               for ln in d.line_items)
    row = next(r for r in manifest.coverage if r.source_type == TRACKING)
    assert (row.count, row.coverage_class) == (2, CoverageClass.UNSUPPORTED_NONFINANCIAL)
    assert row.note == t("migration.coverage_tracking_note")
    with Backup(_file(tmp_path, _backup(version))[0].path) as backup:
        journal = next(j for j in read_odoo(backup).book.journals if j.key == "J:20")
    region = "Regions" if version >= 16 else "Analytic tag"
    assert [ln.tracking for ln in journal.lines] == [{}, {region: "North"}]


@pytest.mark.asyncio
async def test_each_migrated_line_keeps_its_analytic_distribution(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    docs = {d["doc_number"]: d for d in (await _projections(real_engine, run, "doc")).values()}
    assert [ln.get("tracking") for ln in docs["INV/2024/00001"]["line_items"]] == [None, _analytic(17)]


# ── Stock and files ───────────────────────────────────────────────────────────

LOT_1, LOT_2, SHELF = "WID-1#LOT-0001", "WID-1#LOT-0002", "WID-1#LOT-0001@WH/Stock/Shelf 2"


@pytest.mark.parametrize("version", VERSIONS)
def test_stock_moves_each_lot_at_each_location(tmp_path, version):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    moved = [(a.source_external_id, a.adjustment_date, a.item_external_id, a.quantity, a.value)
             for a in manifest.bundle.inventory_adjustments]
    # A receipt's value is shared by its lots, a move between shelves takes its share of the
    # lot's cost along, a cost with no move falls on the stock held then, and a move not yet
    # done is not carried.
    assert moved == [
        ("stock:1", date(2024, 1, 10), LOT_1, D(6), D(180)),
        ("stock:2", date(2024, 1, 10), LOT_2, D(4), D(120)),
        ("stock:3", date(2024, 1, 20), LOT_2, D(-4), D(-120)),
        ("stock:4:to", date(2024, 2, 1), SHELF, D(2), D(60)),
        ("stock:4:from", date(2024, 2, 1), LOT_1, D(-2), D(-60)),
        ("stock:5", date(2024, 3, 1), LOT_1, D(1), D(30)),
        (f"valuation:5:{LOT_1}", date(2024, 3, 15), LOT_1, D(0), D("7.14")),
        (f"valuation:5:{SHELF}", date(2024, 3, 15), SHELF, D(0), D("2.86")),
    ]
    assert sum(a.value for a in manifest.bundle.inventory_adjustments) == D(220)
    coverage = {r.source_type: (r.count, r.coverage_class) for r in manifest.coverage}
    assert coverage["Stock location"] == (2, CoverageClass.MAPPED)
    assert coverage["Lot or serial number"] == (2, CoverageClass.MAPPED)
    assert coverage["Lot or serial number never stocked"] == (1, CoverageClass.IGNORED_NON_BUSINESS)
    assert coverage["Item"] == (2, CoverageClass.MAPPED)
    assert coverage["Draft or cancelled journal entry"] == (1, CoverageClass.IGNORED_NON_BUSINESS)


@pytest.mark.parametrize("version", VERSIONS)
def test_each_lot_and_location_is_stock_of_its_product(tmp_path, version):
    bundle = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL).bundle
    # The location the company's stock starts in comes first, so it is Celerp's default.
    assert [loc.name for loc in bundle.locations] == ["WH/Stock", "WH/Stock/Shelf 2"]
    items = [(i.source_external_id, i.sku, i.product_external_id, i.batch_no, i.location_name)
             for i in bundle.items]
    assert items == [
        ("SRV", "SRV", None, None, "WH/Stock"), ("WID-1", "WID-1", None, None, "WH/Stock"),
        (LOT_1, "WID-1", "WID-1", "LOT-0001", "WH/Stock"), (SHELF, "WID-1", "WID-1", "LOT-0001", "WH/Stock/Shelf 2"),
        (LOT_2, "WID-1", "WID-1", "LOT-0002", "WH/Stock")]


def _receipt(version: int, lot_quantities: tuple[str, str], ordered: str = "10", ordered_in_units: str = "10",
             line_units: tuple[int | None, int | None] = (build.UNITS, build.UNITS)) -> list:
    """The first receipt as Odoo records it done: `ordered` in the move's unit of measure (and
    `ordered_in_units` in the product's), and what actually arrived of each lot on its lines,
    each counted in its own unit."""
    tables = build.tables(version)
    columns, rows = tables.rows["stock_move"]
    move = next(r for r in rows if r[0] == 1)
    move[columns.index("product_uom_qty")], move[columns.index("product_qty")] = ordered, ordered_in_units
    columns, rows = tables.rows["stock_move_line"]
    done = columns.index("quantity" if version >= 17 else "qty_done")
    for row, quantity, unit in zip((r for r in rows if r[columns.index("move_id")] == 1), lot_quantities, line_units):
        row[done], row[columns.index("product_uom_id")] = quantity, unit
        if version >= 17:
            row[columns.index("quantity_product_uom")] = f"{D(quantity) * (12 if unit == build.DOZENS else 1)}"
    return tables


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("arrived, values", [
    (("4", "3"), (D("171.43"), D("128.57"))),       # 7 of 10 ordered arrived
    (("8", "4"), (D(200), D(100))),                 # 12 of 10 ordered arrived
    (("1", "9"), (D(30), D(270))),                  # the lots' split differs from the order's
])
def test_a_done_move_carries_what_actually_moved_of_each_lot(tmp_path, version, arrived, values):
    tables = _receipt(version, arrived)
    moved = {a.source_external_id: (a.item_external_id, a.quantity, a.value) for a in
             _adapter().build_manifest(_file(tmp_path, build.backup(version, tables)), FULL).bundle.inventory_adjustments}
    # The receipt's value is what Odoo valued it at, shared by what each lot received.
    assert moved["stock:1"] == (LOT_1, D(arrived[0]), values[0])
    assert moved["stock:2"] == (LOT_2, D(arrived[1]), values[1])


@pytest.mark.parametrize("version", VERSIONS)
def test_a_done_move_in_another_unit_of_measure_carries_the_products_units(tmp_path, version):
    # 1 dozen ordered, 1 dozen arrived: the lines count dozens, so 0.5 and 0.5 dozen are 6 and 6 units.
    tables = _receipt(version, ("0.5", "0.5"), ordered="1", ordered_in_units="12", line_units=(build.DOZENS,) * 2)
    moved = {a.source_external_id: a.quantity for a in
             _adapter().build_manifest(_file(tmp_path, build.backup(version, tables)), FULL).bundle.inventory_adjustments}
    assert (moved["stock:1"], moved["stock:2"]) == (D(6), D(6))


@pytest.mark.parametrize("version", VERSIONS)
def test_each_move_line_counts_in_its_own_unit_of_measure(tmp_path, version):
    # 1 dozen ordered: LOT-0001's line counts 0.5 dozen and LOT-0002's counts 6 units, so each
    # lot received 6 units and half the receipt's 300.
    tables = _receipt(version, ("0.5", "6"), ordered="1", ordered_in_units="12", line_units=(build.DOZENS, build.UNITS))
    manifest = _adapter().build_manifest(_file(tmp_path, build.backup(version, tables)), FULL)
    moved = {a.source_external_id: (a.item_external_id, a.quantity, a.value) for a in manifest.bundle.inventory_adjustments}
    assert moved["stock:1"] == (LOT_1, D(6), D(150))
    assert moved["stock:2"] == (LOT_2, D(6), D(150))


def test_move_lines_without_a_known_unit_count_in_the_moves_unit(tmp_path):
    # Lines that record no unit of measure count in the move's unit, dozens here.
    tables = _receipt(13, ("0.5", "0.5"), ordered="1", ordered_in_units="12", line_units=(None, None))
    moved = {a.source_external_id: a.quantity for a in
             _adapter().build_manifest(_file(tmp_path, build.backup(13, tables)), FULL).bundle.inventory_adjustments}
    assert (moved["stock:1"], moved["stock:2"]) == (D(6), D(6))


def test_cutover_carries_each_items_position_then_the_moves_after(tmp_path):
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(17)),
                                         MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER))
    assert [(a.kind, a.adjustment_date, a.item_external_id, a.quantity, a.value)
            for a in manifest.bundle.inventory_adjustments] == [
        ("opening", CUTOVER, LOT_1, D(4), D(120)), ("opening", CUTOVER, SHELF, D(2), D(60)),
        ("adjustment", date(2024, 3, 1), LOT_1, D(1), D(30)),
        ("adjustment", date(2024, 3, 15), LOT_1, D(0), D("7.14")),
        ("adjustment", date(2024, 3, 15), SHELF, D(0), D("2.86"))]


@pytest.mark.parametrize("version", VERSIONS)
def test_files_on_documents_contacts_and_products_are_screened(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    manifest = _adapter().build_manifest(files, FULL)
    attached = {a.source_external_id: (a.file_name, a.target_source_type, a.target_source_external_id, a.item_image)
                for a in manifest.bundle.attachments}
    assert attached == {
        "attachment:1": ("INV-2024-00001.pdf", "Customer Invoice", "S:INV/2024/00001", False),
        "attachment:2": ("contract.txt", "Contact", "Lakeshore Trading Co", False),
        "attachment:3": ("widget.png", "Item", "WID-1", False),
        "attachment:7": ("WID-1.png", "Item", "WID-1", True),               # the product's own image
        "attachment:9": ("terms.txt", "Contact", "Northwind Wholesale", False),     # kept in the database
    }
    rejected = {r["source_external_id"]: r["reason"] for r in manifest.source_summary["attachments"]["rejected"]}
    assert set(rejected) == {"attachment:4", "attachment:5", "attachment:6", "attachment:10", "attachment:12"}
    assert "isn't brought into Celerp" in rejected["attachment:4"]          # on a journal entry
    assert "isn't brought into Celerp" in rejected["attachment:12"]         # on a quotation
    assert "contents aren't accepted" in rejected["attachment:5"]           # an executable
    assert "contents aren't accepted" in rejected["attachment:6"]           # not the JPEG it claims
    assert "missing from the backup" in rejected["attachment:10"]

    assert _adapter().read_attachment(files, FULL, "attachment:1") == build.PDF
    assert _adapter().read_attachment(files, FULL, "attachment:9") == b"Payment on delivery.\n"
    assert _adapter().read_attachment(files, FULL, "attachment:7") == build.PNG
    for key in ("attachment:4", "attachment:8", "attachment:11", "attachment:99"):
        with pytest.raises(ScanError):
            _adapter().read_attachment(files, FULL, key)
    with pytest.raises(ScanError, match="This attachment is not in the uploaded file."):
        _adapter().read_attachment(files, FULL, "attachment:99")


@pytest.mark.parametrize("version", VERSIONS)
def test_every_file_in_the_backup_is_carried_or_counted(tmp_path, version):
    """Odoo's interface files and the smaller copies it keeps of each picture are counted as
    not business files; every other file is carried or listed as left out."""
    manifest = _adapter().build_manifest(_file(tmp_path, _backup(version)), FULL)
    rows = {r.source_type: r for r in manifest.coverage}
    assert (rows["Attachment"].count, rows[attachments.REJECTED].count) == (5, 5)
    ignored = rows[attachments.IGNORED]
    assert (ignored.count, ignored.coverage_class) == (2, CoverageClass.IGNORED_NON_BUSINESS)
    assert rows["Attachment"].count + rows[attachments.REJECTED].count + ignored.count == len(build.ATTACHMENTS)


@pytest.mark.asyncio
async def test_the_odoo_product_image_becomes_the_items_picture(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    [item] = [i for i in (await _projections(real_engine, run, "item")).values()
              if i.get("sku") == "WID-1" and not i.get("catalog_item_id")]
    assert sorted((f["filename"], f["is_hero"], f["document_tag"]) for f in item["files"]) == [
        ("WID-1.png", True, "product_images"), ("widget.png", False, "product_images")]


@pytest.mark.asyncio
async def test_each_lot_migrates_as_stock_of_its_product_where_odoo_keeps_it(real_engine, monkeypatch, tmp_path):
    from test_migration_e2e import _projections

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(17))], {"mode": "full_history"},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with real_engine.connect() as conn:
        places = dict((await conn.execute(text("SELECT id::text, name FROM locations WHERE company_id = :c"),
                                          {"c": run.company_id})).all())
    items = await _projections(real_engine, run, "item")
    [product] = [k for k, i in items.items() if i.get("sku") == "WID-1" and not i.get("catalog_item_id")]
    stock = sorted((i.get("batch_no"), places[i["location_id"]], i["quantity"], i["catalog_item_id"])
                   for i in items.values() if i.get("catalog_item_id"))
    assert stock == [("LOT-0001", "WH/Stock", 5, product), ("LOT-0001", "WH/Stock/Shelf 2", 2, product),
                     ("LOT-0002", "WH/Stock", 0, product)]
    assert items[product]["quantity"] == 0


# ── Several companies in one backup ───────────────────────────────────────────

GROUP = "Harbour Group Holdings"


def _second_company() -> build.Tables:
    """The fixture with a second company, Harbour Group Holdings, whose only books are the
    capital entry MISC/2024/01/0001, posted to its own copies of the two accounts it uses."""
    tables = build.tables(17)
    tables.rows["res_company"][1].append([2, GROUP, 900, build.USD, None, None, "12"])
    p_cols, partners = tables.rows["res_partner"]
    partners.append([{"id": 900, "name": GROUP, "commercial_partner_id": 900, "active": "t",
                      "is_company": "t", "customer_rank": 0, "supplier_rank": 0}.get(c) for c in p_cols])
    a_cols, accounts = tables.rows["account_account"]
    used = {r[tables.rows["account_move_line"][0].index("account_id")] for r in _lines_of(tables, 1)}
    for row in [r for r in accounts if r[a_cols.index("id")] in used]:
        copy = list(row)
        copy[a_cols.index("id")] += 1000
        copy[a_cols.index("company_id")] = 2
        accounts.append(copy)
    m_cols, moves = tables.rows["account_move"]
    next(r for r in moves if r[0] == 1)[m_cols.index("company_id")] = 2
    for row in _lines_of(tables, 1):
        _set(tables, row, company_id=2, account_id=row[tables.rows["account_move_line"][0].index("account_id")] + 1000)
    return tables


def _as(company: str) -> MigrationDecisions:
    return MigrationDecisions(mode=CIFMode.FULL_HISTORY, mappings={COMPANY_QUESTION: company})


def test_a_backup_of_two_companies_asks_which_one_to_import(tmp_path):
    files = _file(tmp_path, build.backup(17, _second_company()))
    scan = _adapter().inspect(files)
    (question,) = scan.questions
    assert (question.key, question.options, question.suggested) == (COMPANY_QUESTION, (build.COMPANY, GROUP),
                                                                    build.COMPANY)
    assert scan.company_name == build.COMPANY
    other = {r.source_type: r for r in scan.coverage}[OTHER_COMPANY]
    assert (other.count, other.coverage_class) == (1, CoverageClass.IGNORED_NON_BUSINESS)
    assert other.note == t(OTHER_COMPANY_NOTE) != OTHER_COMPANY_NOTE      # read from the catalogs

    group = _adapter().inspect(files, GROUP)
    assert group.company_name == GROUP
    assert group.questions[0].suggested == GROUP                # the scan describes the company chosen
    assert group.object_counts["Journal entry"] == 1 and group.object_counts["Invoice or bill"] == 0
    assert {r.source_type: r.count for r in group.coverage}[OTHER_COMPANY] == len(build.moves()) - 1

    with pytest.raises(ScanError):
        _adapter().inspect(files, "Another Co")


def test_a_backup_of_one_company_asks_nothing(tmp_path):
    scan = _adapter().inspect(_file(tmp_path, _backup(17)))
    assert scan.questions == ()
    assert OTHER_COMPANY not in {r.source_type for r in scan.coverage}


def test_the_chosen_company_is_the_only_one_imported(tmp_path):
    files = _file(tmp_path, build.backup(17, _second_company()))
    manifest = _adapter().build_manifest(files, _as(GROUP))
    assert manifest.source == GROUP
    assert [j.source_external_id for j in manifest.bundle.journals] == ["J:1"]
    assert manifest.bundle.documents == [] and manifest.bundle.attachments == []
    with pytest.raises(ScanError):                              # a file on the other company's records
        _adapter().read_attachment(files, _as(GROUP), "attachment:1")

    harbour = _adapter().build_manifest(files, _as(build.COMPANY))
    assert "J:1" not in {j.source_external_id for j in harbour.bundle.journals}
    assert _adapter().read_attachment(files, _as(build.COMPANY), "attachment:1") == build.PDF


@pytest.mark.asyncio
async def test_each_company_of_a_backup_migrates_into_its_own_company(real_engine, monkeypatch, tmp_path):
    from celerp.models.company import Company

    run, rejected = await migrate_set(real_engine, [("harbour.zip", build.backup(17, _second_company()))],
                                      {"mode": "full_history", "mappings": {COMPANY_QUESTION: GROUP}},
                                      monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with maker(real_engine)() as s:
        assert (await s.get(Company, run.company_id)).name == GROUP     # the review proposed the chosen company


# ── Reconciliation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", VERSIONS)
def test_expectations_come_from_the_source(tmp_path, version):
    expectations = _adapter().source_expectations(_file(tmp_path, _backup(version)), FULL).expectations
    by = {(e.measure, e.key): e.expected for e in expectations}
    end = _source_balances()
    assert by[(M.DEBITS_EQUAL_CREDITS, "")] == 0
    for code, balance in end.items():
        assert by[(M.TRIAL_BALANCE, code)] == balance, code
    assert by[(M.TRIAL_BALANCE_AT, "121000@2024-02-29")] == _source_balances(date(2024, 2, 29))["121000"]
    assert by[(M.BANK_CASH, "101000")] == D(10345)
    assert by[(M.AR_CONTROL, "121000")] == D("446.00")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co")] == D("136.50")
    assert by[(M.AR_BY_CUSTOMER, "Pine & Co")] == D("309.50")
    assert by[(M.AR_BY_CUSTOMER, "Lakeshore Trading Co [11]")] == 0
    assert by[(M.AP_BY_SUPPLIER, "Northwind Wholesale")] == D("-307.50")
    assert by[(M.AR_DOCUMENT_RESIDUAL, "")] == 0                  # open invoices less open credit = AR
    assert by[(M.AP_DOCUMENT_RESIDUAL, "")] == 0
    assert by[(M.INVENTORY_QUANTITY, "WID-1")] == D(7)           # what the quants hold, not the moves
    assert by[(M.INVENTORY_VALUE, "WID-1")] == D(220)
    # Each lot at each location holds what its quants hold; Odoo values stock per product only.
    assert {k: v for (m, k), v in by.items() if m == M.INVENTORY_QUANTITY and k != "WID-1"} == {
        LOT_1: D(5), SHELF: D(2), LOT_2: D(0)}
    assert {k for m, k in by if m == M.INVENTORY_VALUE} == {"WID-1"}
    assert by[(M.DOCUMENT_COUNT, "invoice")] == D(6)
    assert by[(M.DOCUMENT_RESIDUAL, "S:INV/2024/00002")] == D(130)
    assert by[(M.DOCUMENT_RESIDUAL, "S:RINV/2024/00001")] == D("57.50")
    assert by[(M.DOCUMENT_RESIDUAL, "S:INV/2024/00001")] == 0


@pytest.mark.parametrize("version", VERSIONS)
def test_reading_a_backup_twice_gives_the_same_manifest(tmp_path, version):
    files = _file(tmp_path, _backup(version))
    first = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    again = _adapter().build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    assert first == again


# ── A large backup ────────────────────────────────────────────────────────────

BULK = 10_000
LINE_BYTES = 900        # the bound per move line read; a row as a dict of every kept column took about 1,950


def _peak(tmp_path, lines: int) -> tuple[int, object]:
    """The most memory reading the books of a backup with an entry of `lines` lines takes."""
    from celerp.importers.adapters.odoo.book import read_odoo
    from celerp.importers.adapters.odoo.dump import Backup

    path = tmp_path / f"bulk-{lines}.zip"
    path.write_bytes(build.backup(14, build.tables(14, lines)))
    with Backup(path) as backup:
        tracemalloc.start()
        try:
            odoo = read_odoo(backup)
            return tracemalloc.get_traced_memory()[1], odoo
        finally:
            tracemalloc.stop()


def test_the_memory_of_reading_the_books_grows_by_a_bounded_size_per_move_line(tmp_path):
    """A row keeps only the columns read and a value its table repeats is held once, and of
    ir_model_data, where Odoo names each record, only the account types are kept."""
    small, _ = _peak(tmp_path, 2_000)
    large, odoo = _peak(tmp_path, 2_000 + BULK)
    assert max(len(j.lines) for j in odoo.book.journals) == 2_000 + BULK
    assert (large - small) / BULK < LINE_BYTES


def test_a_run_reads_the_dump_once_for_every_step(tmp_path, monkeypatch):
    from celerp.importers.adapters.odoo import adapter as odoo
    from celerp.importers.adapters.odoo.dump import Backup

    reads, tables = [], Backup.tables
    monkeypatch.setattr(Backup, "tables", lambda self, *a, **kw: reads.append(1) or tables(self, *a, **kw))
    odoo._books.cache_clear()
    files, cutover = _file(tmp_path, _backup(17)), MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER)
    adapter = _adapter()
    adapter.inspect(files)
    full = adapter.build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    at_cutover = adapter.build_manifest(files, cutover).model_dump(exclude={"exported_at"})
    adapter.source_expectations(files, FULL)
    assert adapter.read_attachment(files, FULL, "attachment:9") == b"Payment on delivery.\n"
    assert len(reads) == 1
    # A file kept in the database is held only when it is attached to a record Celerp holds.
    refs = odoo._books(files[0].path, files[0].sha256, None).attachments
    assert {k for k, r in refs.items() if r.stored is not None} == {"attachment:9"}

    # Each step builds on the book it shares as on one read afresh.
    assert adapter.build_manifest(files, FULL).model_dump(exclude={"exported_at"}) == full
    odoo._books.cache_clear()
    assert adapter.build_manifest(files, cutover).model_dump(exclude={"exported_at"}) == at_cutover
    assert len(reads) == 2


def test_runs_on_one_upload_at_once_build_what_each_builds_alone(tmp_path, monkeypatch):
    """A full history manifest and a cutover manifest of one backup built at the same time, the
    full history one bundled while the cutover is halfway through building its ledger on the
    book they share, come out as each does on its own."""
    import threading

    from celerp.importers.adapters.exports import ledger
    from celerp.importers.adapters.odoo import adapter as odoo

    files, cutover = _file(tmp_path, _backup(17)), MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER)
    adapter = _adapter()
    odoo._books.cache_clear()
    alone = {d.mode: adapter.build_manifest(files, d).model_dump(exclude={"exported_at"}) for d in (FULL, cutover)}

    halfway, bundled = threading.Event(), threading.Event()
    link_notes, bundle = ledger._link_notes, odoo.build_bundle

    def paused_link_notes(book):
        link_notes(book)
        if threading.current_thread() is cut:
            halfway.set()
            bundled.wait(2)

    def build_bundle(led, source):
        if led.cutover is None:
            cut.start()
            halfway.wait(2)             # the cutover is halfway through its ledger, unless held back
        try:
            return bundle(led, source)
        finally:
            bundled.set()

    monkeypatch.setattr(ledger, "_link_notes", paused_link_notes)
    monkeypatch.setattr(odoo, "build_bundle", build_bundle)
    together = {}
    cut = threading.Thread(target=lambda: together.setdefault(
        CIFMode.CUTOVER, adapter.build_manifest(files, cutover).model_dump(exclude={"exported_at"})))
    together[CIFMode.FULL_HISTORY] = adapter.build_manifest(files, FULL).model_dump(exclude={"exported_at"})
    cut.join()
    assert together == alone


# ── End to end ────────────────────────────────────────────────────────────────

async def _events(engine, company_id) -> int:
    return await count(engine, "ledger", "company_id = :c", c=company_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [{"mode": "full_history"},
                                       {"mode": "cutover", "cutover_date": CUTOVER.isoformat()}])
@pytest.mark.parametrize("version", VERSIONS)
async def test_an_odoo_backup_migrates_and_reconciles(real_engine, monkeypatch, tmp_path, version, decisions):
    """The backup migrates through the real runner with nothing rejected and every
    reconciliation check passing; running the whole import again writes nothing new."""
    from celerp.services import migrations

    run, rejected = await migrate_set(real_engine, [("harbour.zip", _backup(version))], decisions, monkeypatch,
                                      tmp_path)
    assert rejected == []
    _passing(run)
    measures = {r["check"] for r in run.reconciliation["rows"]}
    assert {"debits_equal_credits", "trial_balance_at", "ar_by_customer", "ap_by_supplier", "bank_cash",
            "inventory_quantity", "inventory_value", "ar_document_residual"} <= measures

    events = await _events(real_engine, run.company_id)
    maps = await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=run.id)
    async with real_engine.begin() as conn:
        await conn.execute(text("UPDATE migration_runs SET phase_state = '{}' WHERE id = :r"), {"r": run.id})
    await migrations.run_migration(run.id)
    again = await load_run(real_engine, run.id)
    _passing(again)
    assert all(entry["status"] == "done" for entry in again.phase_state.values())
    assert await _events(real_engine, run.company_id) == events
    assert await count(real_engine, "migration_entity_maps", "migration_run_id = :r", r=run.id) == maps
    async with maker(real_engine)() as s:
        report = await migrations.run_view(s, again)
    assert all(e["coverage_class"] != "unsupported_financial_blocker" for e in report["coverage"])

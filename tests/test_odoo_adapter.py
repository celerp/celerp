# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Odoo backups: one database manager zip read as one company's books.

The fixture is a fictitious company's first four months, written as Odoo 13, 14 and 17
lay their tables out (tests/fixtures/odoo_backup/build.py)."""

from __future__ import annotations

import ast
import gc
import gzip
import hashlib
import io
import json
import os
import random
import re
import sys
import tracemalloc
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest
from sqlalchemy import text

from celerp.importers.adapters.base import COMPANY_QUESTION, MigrationDecisions, ScanError, SourceRevisionError
from celerp.importers.adapters.odoo import attachments
from celerp.importers.adapters.odoo.adapter import OTHER_COMPANY, OTHER_COMPANY_NOTE
from celerp.importers.schema import CIFMode, CoverageClass, ReconciliationMeasure as M
from fixtures.manager_io.support import BASIC, artifact
from fixtures.odoo_backup import build
from migration_support import count, load_run, maker, real_engine, recognition_peak, scan_upload  # noqa: F401
from test_helpers import fastest_cpu_seconds
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
        backup.version = 15
        assert backup.lot_table == "stock_production_lot"


def test_the_manifest_names_its_version_as_text_or_none():
    from celerp.importers.adapters.odoo.dump import _major

    assert _major({"version": "17.0"}) == 17 and _major({"major_version": "saas~16.3", "version": "16.3"}) == 16
    assert _major({"version": ["17.0"]}) is None and _major({"version": {"17.": 1}}) is None and _major({}) is None


def test_a_translated_name_is_its_english_one_else_the_first_stored():
    from celerp.importers.adapters.odoo.dump import translation

    assert translation('{"en_US": "Bank", "th_TH": "ธนาคาร"}') == "Bank"
    assert translation('{"es_ES": "Banco", "fr_FR": "Banque"}') == "Banco"   # no English name: the first one stored
    assert translation('{"en_US": "\\u0e18"}') == "\u0e18"


# Translations Odoo never writes: each refuses the backup as damaged.
_NOT_TRANSLATED = {"object": '{"en_US": {"a": 1}}', "list": '{"en_US": ["Widget", "Gadget"]}', "number": '{"en_US": 12}',
                   "null and bool": '{"en_US": null, "th_TH": true}', "empty": "{}", "a string": '"Widget"',
                   "an array": '["Widget"]', "not JSON": '{"en_US": "Widget"', "NaN": '{"en_US": NaN}'}


@pytest.mark.parametrize("shape", list(_NOT_TRANSLATED))
def test_a_translated_name_of_another_shape_is_refused(shape):
    from celerp.importers.adapters.odoo.dump import translation

    assert translation(_NOT_TRANSLATED[shape]) is None


LANGUAGES = ("en_US", "th_TH", "fr_FR", "de_DE", "es_ES", "it_IT", "ja_JP", "vi_VN")
_SALES_TEXT = ("Sales description " * 40)[:600]


def _in_every_language(text: str) -> str:
    """`text` as Odoo 16 on keeps a name translated into each of a catalog's eight LANGUAGES, Thai among them."""
    return json.dumps({lang: f"{'สินค้า ' if lang == 'th_TH' else ''}{text} {lang}" for lang in LANGUAGES},
                      ensure_ascii=False)


def test_a_catalog_in_eight_languages_is_charged_only_the_names_it_keeps(tmp_path, monkeypatch):
    """Only the English name of a translated value is kept, so only it is charged: a catalog
    whose every name and 600-character description is stored in eight languages fits the budget
    at 84,000 products and more (what it fitted before names were read from their JSON), and each
    product is still charged at least the two names it keeps."""
    from celerp.importers.adapters.odoo import book, dump

    charged = {}
    for n in (0, 2_000):
        with dump.Backup(_shaped(tmp_path / f"catalog-{n}.zip", monkeypatch, "catalog", n)) as backup:
            odoo = book.read_odoo(backup)
            charged[n] = backup.held
    per_product = (charged[2_000] - charged[0]) / 2_000
    kept = odoo.book.items["P0001999"]
    assert (kept.name, kept.description) == ("Part 1999 en_US", _SALES_TEXT + "1999 en_US")
    assert per_product >= sys.getsizeof(kept.name) + sys.getsizeof(kept.description)
    assert dump.MAX_HELD // per_product >= 84_000, f"{per_product:.0f} B a product"


# JSON a few bytes a value long that decodes into objects many times its size (the numbers vary, so
# the zip holding them is not refused for packing too tightly).
_SWOLLEN_JSON = {
    "numbers": '{"en_US": "x", "k": [' + ",".join(random.Random(1).choices("123456789", k=1_000_000)) + "]}",
    "strings": '{"en_US": "x", "k": [' + ",".join(['"ab"'] * 500_000) + "]}",
    "names": "{" + ",".join(f'"{i:x}": ""' for i in range(500_000)) + "}",
    "lists": '{"en_US": "x", "k": [' + ",".join(["[]"] * 1_000_000) + "]}",
    "objects": '{"en_US": "x", "k": [' + ",".join(["{}"] * 1_000_000) + "]}",
    # ASCII text whose one escape decodes the whole string to four or two bytes a character
    "an emoji escaped": '{"en_US": "\\ud83d\\ude00' + "a" * 2_000_000 + '"}',
    "a Thai letter escaped": '{"en_US": "\\u0e01' + "a" * 2_000_000 + '"}',
    "a key with an emoji escaped": '{"\\ud83d\\ude00' + "a" * 2_000_000 + '": 100}',
}


@pytest.mark.parametrize("shape", list(_SWOLLEN_JSON))
def test_what_decoding_json_holds_is_never_more_than_its_measure(shape):
    from celerp.importers.adapters.odoo.dump import json_size, json_value

    for value in (_SWOLLEN_JSON[shape], _in_every_language(_SALES_TEXT), '{"1": 100.0}'):
        tracemalloc.start()
        try:
            json_value(value)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak <= json_size(value), f"{peak / json_size(value):.2f}x"


def _parameters_no_caller_passes() -> list[str]:
    """Every parameter with a default, of a function in the Odoo adapter, that no call in the app
    passes a value for. Passing on a parameter of one's own counts only once that one is passed."""
    import celerp

    root = Path(celerp.__file__).parent
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in root.rglob("*.py")}
    calls = []   # (the call, the parameters of the function it is made in)
    for tree in trees.values():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                own = {(fn.name, a.arg) for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
                calls += [(c, own) for c in ast.walk(fn) if isinstance(c, ast.Call)]
    unpassed = {}   # (function, parameter) -> where it is declared, with its position past self
    for path in sorted((root / "importers/adapters/odoo").glob("*.py")):
        for fn in ast.walk(trees[path]):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = fn.args.posonlyargs + fn.args.args
            skip = 1 if positional and positional[0].arg in {"self", "cls"} else 0
            first = len(positional) - len(fn.args.defaults)
            where = f"{path.relative_to(root)}:{fn.lineno}"
            unpassed |= {(fn.name, a.arg): (where, i - skip) for i, a in enumerate(positional) if i >= first}
            unpassed |= {(fn.name, a.arg): (where, None) for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d}

    def passes(call, own, name, parameter, at) -> bool:
        if (getattr(call.func, "attr", None) or getattr(call.func, "id", None)) != name:
            return False
        values = [k.value for k in call.keywords if k.arg in {parameter, None}]
        if at is not None and (len(call.args) > at or any(isinstance(a, ast.Starred) for a in call.args)):
            values.append(call.args[min(at, len(call.args) - 1)])
        return any(not (isinstance(v, ast.Name) and any(p == v.id and (f, p) in unpassed for f, p in own))
                   for v in values)

    while passed := [key for key, (_, at) in unpassed.items()
                     if any(passes(call, own, *key, at) for call, own in calls)]:
        for key in passed:
            del unpassed[key]
    return sorted(f"{where} {name}({parameter})" for (name, parameter), (where, _) in unpassed.items())


def test_every_optional_parameter_of_the_adapter_is_passed_by_some_caller():
    """A parameter no caller passes never varies (e.g. a width every caller left at one): it is
    removed, not kept at its default."""
    assert _parameters_no_caller_passes() == []


@pytest.mark.parametrize("table, column", [("product_template", "name"), ("account_move_line", "analytic_distribution")])
def test_json_that_would_decode_past_the_budget_is_refused_before_it_is_decoded(tmp_path, monkeypatch, table, column):
    """A translated name or an analytic distribution is decoded from JSON, which can hold a hundred
    times its text as objects: one whose decoding would take the read past the budget is refused
    as too large before it is decoded."""
    from celerp.importers.adapters.odoo import book, dump

    tables = build.tables(17)
    _damage(tables, table, column, _SWOLLEN_JSON["numbers"])
    (tmp_path / "swollen.zip").write_bytes(build.backup(17, tables))
    monkeypatch.setattr(dump, "MAX_HELD", 48 * MI)
    tracemalloc.start()
    try:
        with dump.Backup(tmp_path / "swollen.zip") as backup, pytest.raises(ScanError) as refused:
            book.read_odoo(backup)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en")
    assert peak < 32 * MI, f"held {peak / MI:.0f} MiB"


@pytest.mark.parametrize("version", (16, 17))
def test_every_translated_column_read_refuses_a_name_not_of_its_shape(tmp_path, version):
    """Every name and description the dump declares jsonb is checked as its row is read: one of
    another shape (here the review's four, which were imported as a Python repr, a number or an
    empty name) refuses the backup as damaged before anything is imported."""
    from celerp.importers.adapters.odoo.book import COLUMN_KINDS, COLUMNS, TABLES

    jsonb = [(table, column) for table, columns in build.tables(version).jsonb.items() if table in TABLES
             for column in columns if column in COLUMNS]
    assert all(column in COLUMN_KINDS for _, column in jsonb)   # every jsonb column kept has a kind
    translated = [(table, column) for table, column in jsonb if COLUMN_KINDS[column] == "translated"]
    assert {c for _, c in translated} == {"name", "description_sale"} and len(translated) == 7
    for table, column in translated:
        for shape in ("object", "list", "number", "null and bool"):
            tables = build.tables(version)
            _damage(tables, table, column, _NOT_TRANSLATED[shape])
            with pytest.raises(ScanError, match="incomplete or damaged"):
                _adapter().build_manifest(_file(tmp_path, build.backup(version, tables), f"{table}.zip"), FULL)


def test_a_name_the_dump_declares_text_is_read_as_written_at_every_version(tmp_path):
    """Only a column the dump declares jsonb holds a translation: a contact's name that happens
    to be written as JSON is that contact's name, never decoded."""
    tables = build.tables(17)
    _damage(tables, "res_partner", "name", '{"en_US": {"a": 1}}', 7)
    scan = _adapter().build_manifest(_file(tmp_path, build.backup(17, tables)), FULL)
    assert '{"en_US": {"a": 1}}' in {c.name for c in scan.bundle.contacts}


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
    creates = re.compile(r"(?ms)^CREATE TABLE .*?^\);\n")
    end = dump.rindex("--\n-- PostgreSQL database dump complete")
    return {
        # The upload stopped after the books, before the stock.
        "truncated between tables": _cut(dump, "--\n-- Data for Name: stock_location"),
        "truncated inside a table": _cut(dump, copy, dump.index("\n", row) + 1 - dump.index(copy)),
        "truncated inside a row": _cut(dump, copy, row + 5 - dump.index(copy)),
        "a row short of a field": dump[:row] + dump[row:].replace("\t", " ", 1),
        "a table left without its data": dump.replace(_cut(dump[dump.index("--\n-- Data for Name: stock_quant;"):],
                                                           "--\n-- Data for Name: stock_lot"), ""),
        # Data whose tables are not declared first: which of their columns hold translated names is unknown.
        "data only, without its tables": creates.sub("", dump),
        "its tables declared after their data": creates.sub("", dump[:end]) + "".join(creates.findall(dump)) + dump[end:],
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


def _damage(tables: build.Tables, table: str, column: str, value: str | None, row_id: int | None = None) -> None:
    """`value` written into `column` of the first row of `table`, or of the row with `row_id`."""
    columns, rows = tables.rows[table]
    row = next(r for r in rows if row_id is None or r[columns.index("id")] == row_id)
    row[columns.index(column)] = value


_DEEP_NAME = '{"en_US": ' * 20_000 + '"x"' + "}" * 20_000  # past the JSON parser's depth, within the size checks
DAMAGED_FIELDS = {
    "debit not a number": ("account_move_line", "debit", "not-a-number", None),
    "inline file not hex": ("ir_attachment", "db_datas", "\\xGG", 9),
    "inline file half a byte": ("ir_attachment", "db_datas", "\\xABC", 9),
    "debit NaN": ("account_move_line", "debit", "NaN", None),
    "debit past any amount": ("account_move_line", "debit", "1E+999999", None),
    "entry date not a day": ("account_move", "date", "2024-13-45", None),
    "entry without a date": ("account_move", "date", None, None),
    "entry date as an ISO week": ("account_move", "date", "2024-W03-1", None),
    "entry date without dashes": ("account_move", "date", "20240115", None),
    "stock move time as an ISO week": ("stock_move", "date", "2024-W03-1 10:00:00", None),
    "contact id not an id": ("res_partner", "id", "abc", 7),
    "line id not an id": ("account_move_line", "id", "x1", None),
    "unit ratio of zero": ("uom_uom", "factor", "0", None),
    "unit rounding of zero": ("uom_uom", "rounding", "0", None),
    "sale price infinite": ("product_template", "list_price", "Infinity", None),
    "account name nested too deep": ("account_account", "name", _DEEP_NAME, 1),
    "stock quantity not a number": ("stock_quant", "quantity", "lots", None),
    "analytic share NaN": ("account_move_line", "analytic_distribution", '{"1": NaN}', None),
    "analytic share infinite": ("account_move_line", "analytic_distribution", '{"1": Infinity}', None),
    "analytic share minus infinite": ("account_move_line", "analytic_distribution", '{"1": -Infinity}', None),
    "analytic share past any amount": ("account_move_line", "analytic_distribution", '{"1": 1e999}', None),
    "analytic share as text": ("account_move_line", "analytic_distribution", '{"1": "NaN"}', None),
    "analytic shares not by account": ("account_move_line", "analytic_distribution", "[100]", None),
    "debit smaller than any amount": ("account_move_line", "debit", "1E-999999", None),
    "debit with more fraction digits than any amount": ("account_move_line", "debit", "0." + "1" * 49, None),
    "analytic share smaller than any amount": ("account_move_line", "analytic_distribution", '{"1": 1e-999999}', None),
    "analytic share with more fraction digits than any amount": ("account_move_line", "analytic_distribution",
                                                                 '{"1": 0.' + "1" * 49 + "}", None),
    "analytic key naming an account twice": ("account_move_line", "analytic_distribution", '{"1,2,1": 100}', None),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(DAMAGED_FIELDS))
async def test_a_field_that_does_not_convert_refuses_the_backup_as_damaged(client, monkeypatch, tmp_path, case):
    """A value Odoo's database could not hold (a number that is not one, a day that is not one,
    a missing date, an id that is not one, a unit with no ratio) is a damaged backup: the scan
    refuses it with the reason, never fails, never reads it as something else, and keeps none of it."""
    from celerp.config import settings
    from celerp.importers.adapters.odoo.adapter import _books

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    tables = build.tables(17)
    _damage(tables, *DAMAGED_FIELDS[case])
    _books.cache_clear()
    r = await scan_upload(client, build.backup(17, tables), name="harbour.zip", source="odoo")
    assert r.status_code == 422, r.text[:300]
    assert r.json()["detail"] == t("migration.err_odoo_dump_damaged", "en")
    assert _books.cache_info().currsize == 0


def _read_columns(version: int) -> dict[str, list[tuple[str, list]]]:
    """Each column this reader keeps that the fixture fills at `version`: its tables and values."""
    from celerp.importers.adapters.odoo.book import COLUMNS, TABLES

    out: dict[str, list[tuple[str, list]]] = defaultdict(list)
    for table, (columns, rows) in build.tables(version).rows.items():
        if table in (*TABLES, "stock_lot", "stock_production_lot"):
            for i, column in enumerate(columns):
                if column in COLUMNS:
                    out[column].append((table, [row[i] for row in rows if row[i] is not None]))
    return out


# Text columns whose values in the fixture happen to be digits.
_DIGIT_TEXT = {"code"}


@pytest.mark.parametrize("version", VERSIONS)
def test_every_column_read_holding_numbers_or_dates_is_checked_as_it_is_read(version):
    """A column added to the reader whose values are numbers or dates is checked with them, so a
    damaged value in it is refused instead of failing the scan or being read as nonsense."""
    from celerp.importers.adapters.odoo.book import COLUMN_KINDS

    def parses(value, kind) -> bool:
        try:
            kind(str(value))
        except (ArithmeticError, ValueError):
            return False
        return True

    def holds_numbers(value) -> bool:
        """JSON with a number in it."""
        found = []
        try:
            json.loads(str(value), parse_float=found.append, parse_int=found.append)
        except ValueError:
            return False
        return bool(found)

    for column, filled in _read_columns(version).items():
        values = [v for _, vs in filled for v in vs]
        if values and column not in _DIGIT_TEXT and (all(parses(v, Decimal) for v in values)
                                                     or all(parses(v, date.fromisoformat) for v in values)):
            assert column in COLUMN_KINDS, column
        if values and any(holds_numbers(v) for v in values) and not all(parses(v, Decimal) for v in values):
            assert COLUMN_KINDS.get(column) == "json", column


_NOT_OF_KIND = {"id": ("x1",), "number": ("NaN",), "date": ("2024-13-45", "2024-W03-1", "20240115"),
                "bytes": ("\\xGG",), "json": ('{"1": NaN}',)}


# Odoo keeps JSON in a column read from Odoo 16 on (analytic_distribution).
@pytest.mark.parametrize(("version", "kind"), [(v, k) for v in (13, 17) for k in _NOT_OF_KIND if (v, k) != (13, "json")])
def test_every_id_number_and_date_column_read_refuses_a_value_not_of_its_kind(tmp_path, version, kind):
    """For each id, number, date, file content and JSON column the reader keeps, a value not of its kind in
    every table holding it refuses the backup as damaged before anything is imported, whether or
    not the import goes on to use that value."""
    from celerp.importers.adapters.odoo.book import COLUMN_KINDS

    columns = [c for c in _read_columns(version) if COLUMN_KINDS.get(c) == kind]
    assert columns
    for column in columns:
        for value in _NOT_OF_KIND[kind]:
            tables = build.tables(version)
            for table, (names, rows) in tables.rows.items():
                if column in names:
                    for row in rows:
                        row[names.index(column)] = value
            with pytest.raises((ScanError, SourceRevisionError), match="incomplete or damaged"):
                _adapter().inspect(_file(tmp_path, build.backup(version, tables), f"{column}.zip"))


def test_a_value_is_read_as_its_kind_as_postgresql_writes_it():
    from celerp.importers.adapters.odoo.dump import KINDS

    assert all(map(KINDS["id"], ("1", "42", "9" * 19))) and not any(map(KINDS["id"], ("-1", "1.0", "x", "\u0661", "9" * 20)))
    assert all(map(KINDS["number"], ("0", "-12.50", "345", "1e-05", "0.333333333333333", "1" + "0" * 19,
                                     "1.1102230246251565e-16", "1.2345678901234567e-32", "0." + "0" * 47 + "1")))
    assert not any(map(KINDS["number"], ("NaN", "Infinity", "-Infinity", "1E+999999", "1" + "0" * 20, "", "x",
                                         "1E-999999", "1e-49", "0." + "1" * 49, "0E-49", "1.2345678901234567e-33")))
    assert all(map(KINDS["date"], ("2024-01-05", "2024-01-10 09:00:00", "2024-01-10 09:00:00.123456")))
    assert not any(map(KINDS["date"], ("2024-13-45", "", "05/01/2024", "x", "2024-W03-1", "20240115", "2024-01-15T09:00:00",
                                       "2024-01-15 09:00", "2024-01-15 09:00:00+07:00", "2024-01-15 09:00:00.1234567",
                                       "\u0662024-01-15")))
    assert all(map(KINDS["bytes"], ("\\x", "\\x00ff", "\\xDEADbeef")))
    assert not any(map(KINDS["bytes"], ("\\xGG", "\\xABC", "\\x0 ", "", "00ff", "x00")))
    assert all(map(KINDS["json"], ('{"1": 100.0}', '{"1,2": 50, "3": 12.5}', "{}", '{"1": 1e-05}')))
    assert not any(map(KINDS["json"], ('{"1": NaN}', '{"1": Infinity}', '{"1": -Infinity}', '{"1": 1e999}',
                                       '{"1": 1' + "0" * 20 + "}", '{"1": 1e-999999}', '{"1": ', "", _DEEP_NAME)))


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


def _directory_entry(data: bytes | bytearray, name: str) -> int:
    """Where `name`'s record starts in the zip's central directory, the index at its end."""
    at = zipfile.ZipFile(io.BytesIO(bytes(data))).start_dir
    while data[at:at + 4] == b"PK\x01\x02":
        lengths = [int.from_bytes(data[at + o:at + o + 2], "little") for o in (28, 30, 32)]
        if data[at + 46:at + 46 + lengths[0]] == name.encode():
            return at
        at += 46 + sum(lengths)
    raise KeyError(name)


def _misindexed(damage: str) -> bytes:
    """A whole backup whose central directory record of the manifest is damaged."""
    data = bytearray(_stored(build.dump_sql(build.tables(17)).encode()))
    at = _directory_entry(data, "manifest.json")
    if damage == "a name that differs from its header":
        data[at + 46] = ord("o")
    elif damage == "a newer zip version than any reader unpacks":
        data[at + 6:at + 8] = (84).to_bytes(2, "little")
    else:  # a name flagged UTF-8 that is not
        data[at + 9] |= 0x08
        data[at + 46] = 0xE6
    return bytes(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["odoo", None])
@pytest.mark.parametrize("damage", ["a name that differs from its header", "a newer zip version than any reader unpacks",
                                    "a name flagged UTF-8 that is not"])
async def test_a_damaged_index_of_the_zip_is_refused_whether_odoo_was_chosen_or_detected(
        client, monkeypatch, tmp_path, damage, source):
    """The zip's index is read by every reader that tries the upload: whichever one does, the
    damage is a refusal with a reason, never a server error."""
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    r = await scan_upload(client, _misindexed(damage), name="harbour.zip", source=source)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    if damage != "a name that differs from its header":  # the zip names no manifest: not recognisable as Odoo's
        assert detail == t("migration.err_odoo_backup_damaged", "en")
    elif source:
        assert detail == '"harbour.zip" is not a Odoo file. Accepted: Odoo backup (.zip).'
    else:
        assert detail.startswith('Celerp cannot read "harbour.zip" yet.')


@pytest.mark.asyncio
async def test_any_changed_byte_in_the_index_of_the_zip_is_refused_with_a_reason(client, monkeypatch, tmp_path):
    """One bit of the zip's index flipped, 200 times, with Odoo chosen and detected: each upload
    is read or refused with a reason by whichever reader tries it; no server error."""
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    whole = _stored(build.dump_sql(build.tables(17)).encode())
    start = zipfile.ZipFile(io.BytesIO(whole)).start_dir
    rng = random.Random(8)
    escaped = []
    for i in range(200):
        at = rng.randrange(start, len(whole))
        data = whole[:at] + bytes([whole[at] ^ (1 << rng.randrange(8))]) + whole[at + 1:]
        r = await scan_upload(client, data, name=f"{i}.zip", source=("odoo", None)[i % 2])
        if r.status_code not in (200, 422):
            escaped.append((at, r.status_code, r.text[:80]))
    assert not escaped, escaped[:8]


def _lzma_dictionary(data: bytes, size: int) -> bytes:
    """`data`, a zip whose dump is packed with LZMA, with the dictionary size its entry declares
    set to `size`: the decoder keeps up to that much of the unpacked dump resident."""
    info = zipfile.ZipFile(io.BytesIO(data)).getinfo("dump.sql")
    header = info.header_offset
    start = header + 30 + int.from_bytes(data[header + 26:header + 28], "little") + int.from_bytes(
        data[header + 28:header + 30], "little")
    # The entry's LZMA properties: version (2 bytes), their size (2), lc/lp/pb (1), then the dictionary size.
    return data[:start + 5] + size.to_bytes(4, "little") + data[start + 9:]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["odoo", None])
@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
async def test_a_dump_packed_with_a_method_odoo_does_not_use_is_refused_before_it_is_unpacked(
        client, monkeypatch, tmp_path, method, source):
    """Odoo zips its backup with deflate. A dump packed otherwise is refused as one Celerp cannot
    open, before a byte is unpacked: an LZMA entry declares its own dictionary, up to 4 GB, and
    the decoder keeps that much of the dump resident whatever the import's budget."""
    from celerp.config import settings
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    sql = build.dump_sql(build.tables(17))
    padded = sql.replace("SET statement_timeout", "".join(
        f"-- {random.Random(k).getrandbits(512):0128x}\n" for k in range(40_000)) + "SET statement_timeout", 1)
    data = _packed(padded, method)
    if method == zipfile.ZIP_LZMA:
        data = _lzma_dictionary(data, 1024 ** 3)
    opened = []
    monkeypatch.setattr(dump.Backup, "tables", lambda *a, **k: opened.append(a) or {})
    peaks = recognition_peak(monkeypatch)
    r = await scan_upload(client, data, name="harbour.zip", source=source)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_odoo_backup_damaged", "en") and not opened
    assert peaks and max(peaks) < 16 * MI, peaks


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


def _many_entries(count: int, name=lambda k: f"filestore/{k:08d}", backup: bool = True) -> bytes:
    """A zip of `count` empty files named by `name`, after a whole backup when `backup`."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        if backup:
            z.writestr("manifest.json", json.dumps(build.manifest(17)))
            z.writestr("dump.sql", build.dump_sql(build.tables(17)))
        for k in range(count):
            z.writestr(name(k), b"")
    return out.getvalue()


CRAFTED_DIRECTORIES = {
    "300,000 empty files": lambda backup: _many_entries(300_000, backup=backup),
    "2,000 files with 30,000 character names": lambda backup: _many_entries(
        2_000, lambda k: f"filestore/{k:08d}" + "a" * 30_000, backup),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["odoo", None])
@pytest.mark.parametrize("backup", [True, False])
@pytest.mark.parametrize("shape", list(CRAFTED_DIRECTORIES))
async def test_a_zip_whose_directory_lists_more_than_the_server_holds_is_refused_before_it_is_parsed(
        client, monkeypatch, tmp_path, shape, backup, source):
    """Opening a zip builds an object for every file its directory lists. A directory larger than
    the server holds for one upload is refused from the size the end of the zip declares, before
    it is parsed, whether the zip holds a backup or not and whether Odoo was chosen or detected."""
    from celerp.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    data = CRAFTED_DIRECTORIES[shape](backup)
    peaks = recognition_peak(monkeypatch)
    r = await scan_upload(client, data, name="harbour.zip", source=source)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_file_too_large", "en")
    assert peaks and max(peaks) < 16 * MI, f"{len(data) / MI:.0f} MiB upload, peaks {peaks}"


def test_a_backup_with_a_large_filestore_is_read(tmp_path):
    """A filestore of 120,000 files, named as Odoo names them, is more than a 2 GB backup of real
    files holds: its directory is within the budget and the backup is read."""
    data = _many_entries(120_000, lambda k: f"filestore/{k % 256:02x}/{k:040x}")
    assert _adapter().detect(_file(tmp_path, data)).matched
    assert _adapter().inspect(_file(tmp_path, data, "again.zip")).object_counts


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
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en")
    assert peak < 16 * MI, f"held {peak / MI:.0f} MiB"


def _named(names: list[str]) -> bytes:
    """The fixture's backup with a contact more for each of `names`."""
    tables = build.tables(17)
    columns, rows = tables.rows["res_partner"]
    for i, name in enumerate(names):
        row = dict.fromkeys(columns)
        row.update(id=900000 + i, name=name, active=True)
        rows.append([row[c] for c in columns])
    return build.backup(17, tables)


@pytest.mark.parametrize("first", [0x61, 0x0E01, 0x1F600], ids=["latin", "thai", "emoji"])
def test_the_budget_counts_text_as_python_holds_it(tmp_path, monkeypatch, first):
    """Python holds a string at one, two or four bytes a character, by its widest one: Thai at
    two, an emoji at four. 30 contacts each named with a quarter million characters of their
    own, under a 10 MiB budget: in Latin letters the names hold 7.5 MiB and are read; in Thai
    or emoji they hold 15 or 30 MiB and are refused, never held past the budget."""
    from celerp.importers.adapters.odoo import book, dump

    monkeypatch.setattr(dump, "MAX_HELD", 10 * MI)
    rng = random.Random(7)
    path = tmp_path / "named.zip"
    path.write_bytes(_named(["".join(chr(first + rng.randrange(26)) for _ in range(MI // 4)) for _ in range(30)]))
    tracemalloc.start()
    try:
        with dump.Backup(path) as backup:
            read = lambda: backup.tables((*book.TABLES, backup.lot_table), book.COLUMNS, book.KEPT_ROWS,  # noqa: E731
                                         book.required_tables(backup.modules, backup.version))
            if first == 0x61:
                assert len(read()["res_partner"]) > 30
            else:
                with pytest.raises(ScanError, match="too large"):
                    read()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1.5 * dump.MAX_HELD, f"held {peak / MI:.0f} MiB"


def test_a_large_book_kept_in_thai_reads_with_room_to_spare(tmp_path, monkeypatch):
    """A large company's decade, a million journal items with 20,000 contacts and 10,000
    products, every name in Thai (two bytes a character as Python holds it), is charged under
    85% of the budget with what is built from it. In proportion: a fiftieth of that book under a
    fiftieth of 85% of the budget."""
    from celerp.importers.adapters.odoo import book, dump

    def thai(lines: int) -> list:
        amounts = [D(f"{k % 9973 + 1}.{k % 100:02d}") for k in range(lines // 2)]
        booked = [build.Line(account, sign * a, name=f"โอนเงินระหว่างบัญชี {k} จาก {len(amounts)}")
                  for k, a in enumerate(amounts) for account, sign in ((1, 1), (14, -1))]
        return [build.Move(25, "MISC/2024/05/0001", "entry", "posted", "2024-05-01", booked, ref="โอนเงินจำนวนมาก")]

    monkeypatch.setattr(build, "_bulk", thai)
    tables = build.tables(17, bulk_lines=20_000)
    columns, rows = tables.rows["res_partner"]
    for i in range(400):
        row = dict.fromkeys(columns)
        row.update(id=500000 + i, company_id=1, name=f"บริษัท สยามการค้า จำกัด สาขา {i}", city="กรุงเทพมหานคร",
                   street=f"{i} ถนนสุขุมวิท แขวงคลองเตย", commercial_partner_id=500000 + i, active=True)
        rows.append([row[c] for c in columns])
    columns, rows = tables.rows["product_template"]
    for i in range(200):
        row = dict(zip(columns, rows[0]))
        row.update(id=500000 + i, name=json.dumps({"en_US": f"ข้าวหอมมะลิ ถุง 5 กิโลกรัม รุ่น {i}"}, ensure_ascii=False),
                   description_sale=json.dumps({"en_US": f"ข้าวหอมมะลิคุณภาพดี สำหรับขาย รุ่น {i}"}, ensure_ascii=False))
        rows.append([row[c] for c in columns])
    monkeypatch.setattr(dump, "MAX_HELD", dump.MAX_HELD * 85 // 100 // 50)
    path = tmp_path / "thai.zip"
    path.write_bytes(build.backup(17, tables))
    with dump.Backup(path) as backup:
        read = book.read_odoo(backup).book
    assert len(read.journals[-1].lines) == 20_000


# The shapes a book can take, by the family of tables each fills (book.FAMILIES): every family
# has at least one, so what the read builds from any table is measured against what it is charged.
SHAPES = {
    "company": ("companies",),
    "chart": ("accounts", "posted", "long_account_span"),
    "contacts": ("contacts", "duplicate_names"),
    "items": ("items", "catalog"),
    "taxes": ("taxes", "taxed"),
    "journals": ("ordinary", "shared", "thai", "entries", "invoices", "long", "wide", "span"),
    "payments": ("paid", "chain"),
    "analytics": ("analytic", "analytic_repeat"),
    "stock": ("receipts", "lots", "transfers", "quants", "revalued", "layers", "fan", "drained", "long_code_lots",
              "long_code_revaluations", "long_location_lots"),
    "attachments": ("files",),
}
LONG = 200_000  # characters of the one long value a "long" shape repeats across its records


def _long(seed: int) -> str:
    return "".join(random.Random(seed).choices("abcdefghijklmnopqrstuvwxyz0123456789", k=LONG))


def _shaped(path, monkeypatch, shape: str, n: int):
    path.write_bytes(build.backup(17, _shaped_tables(monkeypatch, shape, n)))
    return path


def _shaped_tables(monkeypatch, shape: str, n: int) -> build.Tables:
    """The fixture's tables grown by `n` rows in one of the SHAPES. Journals, in about `n`
    journal items: one entry of items each with its own label and amount ("ordinary"), the same
    in Thai, one entry of items sharing every value but their id ("shared"), entries of two items
    ("entries"), one-line invoices ("invoices"), invoices of a hundred untaxed lines ("long") or
    of ten lines with two taxes each ("wide"), or two entries `n` months apart ("span"). The
    others add `n` of their rows: invoices each paid by its own payment ("paid"), or each paid half
    by the payment before it and half by its own, so every payment links two ("chain"), items each
    spread over the same fifty analytic accounts ("analytic"), companies, accounts, accounts each posted
    to by an entry of its own ("posted"), contacts, items, items each named and described in the
    eight LANGUAGES of a catalog ("catalog"), taxes, one
    invoice of a line for each of the taxes ("taxed"), files kept in the database, and
    stock: receipts of one product, receipts each of its own lot ("lots"), each then moved to
    another location ("transfers"), lots held only by a count ("quants"), products each received
    and revalued ("revalued"), revaluations of one product ("layers"), or `n` lots of one
    product each revalued, every revaluation spread over every lot ("fan"), or `n` lots of one
    product each received and shipped, then `n` revaluations of it ("drained"). The long shapes
    repeat one value of LONG characters across their records: an account's name over `n` month
    ends ("long_account_span"), the name of `n` customers ("duplicate_names"), a product's code
    over `n` lots ("long_code_lots") or `n` revaluations ("long_code_revaluations"), a
    location's name over `n` lots received into it ("long_location_lots"), and fifty analytic
    accounts sharing one name, spread by the longest share a number holds in each of `n` journal
    items ("analytic_repeat")."""
    invoiced = {"invoices": (1, build.SALE_TAX), "long": (100, None), "wide": (10, (build.SALE_TAX, build.PROVINCIAL_TAX))}
    bulk, lines = build._bulk, 0
    if shape in invoiced:
        count, taxes = invoiced[shape]
        items = count + len(taxes if isinstance(taxes, tuple) else [taxes] if taxes else []) + 1
        bulk = lambda n: [build._invoice(1000 + k, f"INV/2025/{k:07d}", "out_invoice", "2024-04-01", 9,  # noqa: E731
                                         [(build.CONSULTING if i % 2 else build.WIDGET, i + 1, D(100 + i), taxes)
                                          for i in range(count)]) for k in range(n // items)]
    elif shape in ("taxes", "taxed"):
        monkeypatch.setattr(build, "TAXES", build.TAXES + [(6 + k, f"Tax {k}", "sale", D(k % 20 + 1), 6)
                                                           for k in range(n)])
        if shape == "taxed":
            bulk = lambda _: [build._invoice(1000, "INV/2025/0000001", "out_invoice", "2024-04-01", 9,  # noqa: E731
                                             [(build.CONSULTING, 1, D(100 + k), 6 + k) for k in range(n)])]
    elif shape == "entries":
        bulk = lambda n: [build.Move(30000 + k, f"MISC/2024/{k:07d}", "entry", "posted", "2024-05-01",  # noqa: E731
                                     [build.Line(a, s * D(f"{k % 997 + 1}.00"), name=f"Transfer {k}")
                                      for a, s in ((1, 1), (14, -1))], ref=f"T{k}") for k in range(n // 2)]
    elif shape in ("span", "long_account_span"):
        bulk = lambda n: [build._entry(30000 + k, f"MISC/{k}", f"{2024 + (n * k) // 12}-{(n * k) % 12 + 1:02d}-01",  # noqa: E731
                                       [(1, D(10)), (14, D(-10))]) for k in (0, 1)]
    elif shape == "shared":
        bulk = lambda n: [build.Move(25, "MISC/2024/05/0001", "entry", "posted", "2024-05-01",  # noqa: E731
                                     [build.Line(a, s * D("1.00"), name="Transfer")
                                      for _ in range(n // 2) for a, s in ((1, 1), (14, -1))], ref="Bulk")]
    elif shape == "thai":
        bulk = lambda n: [build.Move(25, "MISC/2024/05/0001", "entry", "posted", "2024-05-01",  # noqa: E731
                                     [build.Line(a, s * D(f"{k % 9973 + 1}.{k % 100:02d}"),
                                                 name=f"โอนเงินระหว่างบัญชี {k} จาก {n // 2}")
                                      for k in range(n // 2) for a, s in ((1, 1), (14, -1))], ref="โอนเงินจำนวนมาก")]
    elif shape == "paid":
        bulk = lambda n: [m for k in range(n) for m in (  # noqa: E731
            build._invoice(100000 + 2 * k, f"INV/2025/{k:07d}", "out_invoice", "2024-04-01", 9,
                           [(build.CONSULTING, 1, D(100), build.SALE_TAX)]),
            build._entry(100001 + 2 * k, f"PBNK1/2025/{k:07d}", "2024-04-05", [(2, D(115)), (4, D(-115), None, 9)],
                         partner=9, payment=("customer", "inbound")))]
        monkeypatch.setattr(build, "PARTIALS", build.PARTIALS + [(100000 + 2 * k, 100001 + 2 * k, D(115))
                                                                 for k in range(n)])
    elif shape == "chain":
        bulk = lambda n: [m for k in range(n) for m in (  # noqa: E731
            build._invoice(100000 + 2 * k, f"INV/2024/{k:07d}", "out_invoice", "2024-01-10", 9,
                           [(build.CONSULTING, 1, D(100), None)]),
            build._entry(100001 + 2 * k, f"PBNK1/2024/{k:07d}", "2024-01-20", [(2, D(100)), (4, D(-100), None, 9)],
                         partner=9, payment=("customer", "inbound")))]
        monkeypatch.setattr(build, "PARTIALS", build.PARTIALS + [(100000 + 2 * (k + j), 100001 + 2 * k, D(50))
                                                                 for k in range(n) for j in (0, 1) if k + j < n])
    elif shape == "posted":
        bulk = lambda n: [build._entry(30000 + k, f"MISC/{k}", "2024-05-01", [(1000 + k, D(10)), (14, D(-10))])  # noqa: E731
                          for k in range(n)]
    elif shape == "analytic":
        spread = [(100 + a, f"Project {a} of the spread", 1) for a in range(50)]
        monkeypatch.setattr(build, "ANALYTIC_ACCOUNTS", build.ANALYTIC_ACCOUNTS + spread)
        monkeypatch.setattr(build, "ANALYTIC", {**build.ANALYTIC, **{(25, i): {a: D(2) for a, *_ in spread}
                                                                     for i in range(n)}})
    if shape in (*SHAPES["journals"], *SHAPES["payments"], *SHAPES["analytics"], "long_account_span"):
        lines = n
    monkeypatch.setattr(build, "_bulk", bulk)
    tables = build.tables(17, bulk_lines=lines)
    _grow(tables, shape, n)
    return tables


def _grow(tables, shape: str, n: int) -> None:
    """Adds the rows of a shape outside the journals to the fixture's tables."""
    def add(table: str, **values) -> None:
        columns, rows = tables.rows[table]
        rows.append([values.get(c) for c in columns])

    def name(text: str) -> str:
        return json.dumps({"en_US": text})

    def receipt(k: int, product: int, lot: int | None = None, qty: str = "1", dest: int = 8, src: int = 1) -> None:
        on, ref = f"2024-01-11 {k // 3600 % 24:02d}:{k // 60 % 60:02d}:{k % 60:02d}", f"WH/IN/{k:07d}"
        mid = 1000 + k
        add("stock_move", id=mid, name=ref, reference=ref, date=on, product_id=product, product_uom_qty=qty,
            product_qty=qty, location_id=src, location_dest_id=dest, state="done", company_id=1)
        add("stock_move_line", id=mid, move_id=mid, product_id=product, lot_id=lot, location_id=src,
            location_dest_id=dest, product_uom_id=build.UNITS, quantity=qty, quantity_product_uom=qty, reference=ref,
            date=on, state="done", company_id=1)
        if src == 1:
            add("stock_valuation_layer", id=mid, company_id=1, product_id=product, stock_move_id=mid, quantity=qty,
                value=f"{k % 97 + 1}.00", description=ref, create_date=on)

    def revaluation(k: int, product: int) -> None:
        add("stock_valuation_layer", id=900000 + k, company_id=1, product_id=product, quantity="0",
            value=f"{k % 97 + 1}.25", description="Product value manually modified",
            create_date=f"2024-0{k % 8 + 2}-02 09:00:00")

    def lot(k: int) -> int:
        add("stock_lot", id=5000 + k, name=f"SN-{k:06d}", product_id=build.WIDGET, company_id=1)
        return 5000 + k

    def edit(table: str, row_id: int, **values) -> None:
        columns, rows = tables.rows[table]
        row = next(r for r in rows if r[columns.index("id")] == row_id)
        for column, value in values.items():
            row[columns.index(column)] = value

    long = _long(n) if shape.startswith("long_") or shape in ("duplicate_names", "analytic_repeat") else ""
    if shape == "long_account_span":
        edit("account_account", 1, name=name(long))
    elif shape in ("long_code_lots", "long_code_revaluations"):
        edit("product_product", build.WIDGET, default_code=long)
    elif shape == "long_location_lots":
        edit("stock_location", 9, complete_name=long)
    elif shape == "analytic_repeat":
        # Each item's distribution is its own, and spreads the longest share a number holds over
        # fifty accounts sharing one long name.
        from celerp.importers.adapters.odoo import dump

        analytic_columns, analytic_rows = tables.rows["account_analytic_account"]
        first = analytic_rows[0]
        for a in range(50):
            analytic_rows.append([200 + a if c == "id" else name(long[:LONG // 50]) if c == "name" else v
                                  for c, v in zip(analytic_columns, first)])
        columns, rows = tables.rows["account_move_line"]
        spread = ",".join(str(200 + a) for a in range(50))
        share = "9" * (dump.MAX_MAGNITUDE - 1) + "." + "9" * dump.MAX_SCALE
        for k, row in enumerate(r for r in rows if r[columns.index("move_id")] == 25):
            row[columns.index("analytic_distribution")] = f'{{"{spread}": {share}, "9{k:08d}": 0}}'
    for k in range(n):
        if shape == "companies":
            add("res_company", id=1000 + k, name=f"Branch {k}", partner_id=1, currency_id=build.USD)
        elif shape in ("accounts", "posted"):
            add("account_account", id=1000 + k, company_id=1, code=f"7{k:06d}", account_type="income",
                internal_group="income", name=name(f"Sales {k}"), deprecated="f", reconcile="f")
        elif shape == "duplicate_names":
            add("res_partner", id=700000 + k, name=long, commercial_partner_id=700000 + k, active="t",
                is_company="t", customer_rank=1, supplier_rank=0)
        elif shape == "contacts":
            add("res_partner", id=1000 + k, name=f"Customer {k}", commercial_partner_id=1000 + k, active="t",
                is_company="t", customer_rank=1, supplier_rank=0, email=f"c{k}@example.com", city="Portsmouth")
        elif shape == "items":
            add("product_template", id=1000 + k, categ_id=1, uom_id=build.UNITS, type="consu", detailed_type="consu",
                name=name(f"Part {k}"), description_sale=name(f"Part {k} for sale"), list_price="5", active="t")
            add("product_product", id=1000 + k, product_tmpl_id=1000 + k, default_code=f"P{k:07d}", active="t")
        elif shape == "catalog":
            add("product_template", id=1000 + k, categ_id=1, uom_id=build.UNITS, type="consu", detailed_type="consu",
                name=_in_every_language(f"Part {k}"), description_sale=_in_every_language(_SALES_TEXT + str(k)),
                list_price="5", active="t")
            add("product_product", id=1000 + k, product_tmpl_id=1000 + k, default_code=f"P{k:07d}", active="t")
        elif shape == "files":
            content = f"Note {k}\n".encode() * 20
            add("ir_attachment", id=1000 + k, res_id=7, company_id=1, file_size=len(content), name=f"note-{k}.txt",
                res_model="res.partner", type="binary", checksum=build.hashlib.sha1(content).hexdigest(),
                mimetype="text/plain", db_datas="\\x" + content.hex())
        elif shape == "receipts":
            receipt(k, build.WIDGET)
        elif shape in ("lots", "long_code_lots"):
            receipt(k, build.WIDGET, lot(k))
        elif shape == "long_location_lots":
            receipt(k, build.WIDGET, lot(k), dest=9)
        elif shape == "transfers":
            receipt(k, build.WIDGET, lot(k))
            receipt(n + k, build.WIDGET, 5000 + k, dest=9, src=8)
        elif shape == "quants":
            add("stock_quant", id=1000 + k, product_id=build.WIDGET, company_id=1, location_id=9, lot_id=lot(k),
                quantity="1", reserved_quantity="0")
        elif shape == "revalued":
            add("product_template", id=1000 + k, categ_id=1, uom_id=build.UNITS, type="product",
                detailed_type="product", name=name(f"Part {k}"), list_price="5", active="t")
            add("product_product", id=1000 + k, product_tmpl_id=1000 + k, default_code=f"P{k:07d}", active="t")
            receipt(k, 1000 + k, qty="2")
            revaluation(k, 1000 + k)
        elif shape in ("layers", "long_code_revaluations"):
            revaluation(k, build.WIDGET)
        elif shape == "fan":
            receipt(k, build.WIDGET, lot(k))
            revaluation(k, build.WIDGET)
        elif shape == "drained":
            receipt(k, build.WIDGET, lot(k))
            receipt(n + k, build.WIDGET, 5000 + k, dest=1, src=8)
            revaluation(k, build.WIDGET)


def _read_whole(path, decisions=FULL):
    from celerp.importers.adapters.odoo import OdooAdapter
    from celerp.importers.adapters.odoo import adapter as odoo_adapter

    odoo_adapter._books.cache_clear()
    try:
        return OdooAdapter().build_manifest([artifact(path)], decisions)
    finally:
        odoo_adapter._books.cache_clear()


# Each shape at a size the per-test time limit reads three times over.
SIZES = {"companies": 8_000, "accounts": 2_000, "posted": 2_000, "chain": 2_000, "contacts": 5_000, "items": 5_000, "taxes": 2_000, "taxed": 1_000,
         "span": 1_000, "paid": 2_000, "analytic": 10_000, "receipts": 5_000, "lots": 5_000, "transfers": 3_000,
         "quants": 5_000, "revalued": 2_000, "layers": 5_000, "fan": 150, "drained": 3_000, "files": 2_000,
         "long_account_span": 400, "duplicate_names": 400, "long_code_lots": 400, "long_code_revaluations": 400,
         "long_location_lots": 400, "analytic_repeat": 400, "catalog": 2_000}


def test_every_family_of_tables_read_has_a_shape():
    """A table family the reader reads with no shape would go unmeasured against its charge."""
    from celerp.importers.adapters.odoo import book

    assert SHAPES.keys() == book.FAMILIES.keys()
    assert all(SHAPES.values())


@pytest.mark.timeout(240)  # three full reads of a 30,000-row book, the last under tracemalloc: about 60s alone under coverage
@pytest.mark.parametrize("shape", [shape for shapes in SHAPES.values() for shape in shapes])
def test_a_book_the_budget_just_admits_reads_within_two_and_a_half_times_it(tmp_path, monkeypatch, shape):
    """The budget charges what the read builds (from each row, document, document line, stock
    move, holding and month end), not only the rows, so a book it just admits reads within two
    and a half times it whatever its tables hold (one-line invoices built seven times what their
    rows were counted at, a valuation spread over many lots hundreds of times), and a byte less
    refuses the book. In miniature, under the budget each book is charged."""
    from celerp.importers.adapters.odoo import book, dump

    (tmp_path / "warm.zip").write_bytes(_backup(17))
    _read_whole(tmp_path / "warm.zip")  # what any first read loads once is not this book's
    path = _shaped(tmp_path / f"{shape}.zip", monkeypatch, shape, SIZES.get(shape, 30_000))
    with dump.Backup(path) as backup:
        book.read_odoo(backup)
        charged = backup.held
    monkeypatch.setattr(dump, "MAX_HELD", charged - 1)
    with dump.Backup(path) as backup, pytest.raises(ScanError) as refused:
        book.read_odoo(backup)
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en")
    monkeypatch.setattr(dump, "MAX_HELD", charged)
    tracemalloc.start()
    try:
        assert _read_whole(path).bundle.journals
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 2.5 * charged, f"read took {peak / charged:.2f}x the budget"


# Each table the reader reads -> the shape making the most records out of one of its rows: a text
# value grown in every row of the table is repeated across those records. A table the version-17
# fixture does not hold (an Odoo 13 to 15 one) has no text column there to grow.
GROWN_BY = {"res_company": "companies", "res_currency": "invoices", "account_account": "span",
            "account_account_type": "posted", "ir_model_data": "posted", "res_partner": "contacts",
            "product_product": "lots", "product_template": "lots", "uom_uom": "receipts", "account_tax": "taxed",
            "account_tax_repartition_line": "taxed", "account_move": "invoices", "account_move_line": "entries",
            "account_move_line_account_tax_rel": "wide", "account_partial_reconcile": "paid",
            "account_payment": "paid", "account_analytic_account": "analytic", "account_analytic_plan": "analytic",
            "account_analytic_tag": "analytic", "account_analytic_tag_account_move_line_rel": "analytic",
            "stock_location": "transfers", "stock_move": "transfers", "stock_move_line": "transfers",
            "stock_quant": "quants", "stock_valuation_layer": "fan", "stock_lot": "lots", "ir_attachment": "files"}
GROWN = 60  # records of the shape
_NOT_TEXT = re.compile(r"-?\d+(\.\d+)?|\d{4}-\d\d-\d\d.*|[tf]|\\x.*", re.S)


def _text_columns() -> list[tuple[str, str]]:
    """Every (table, column) the reader keeps whose values in the fixture are text: not an id,
    number, date, flag or file content."""
    from celerp.importers.adapters.odoo import book

    tables = build.tables(17)
    out = []
    for table in sorted(GROWN_BY.keys() & tables.rows.keys()):
        columns, rows = tables.rows[table]
        for column in sorted(book.COLUMNS & set(columns)):
            values = [str(r[columns.index(column)]) for r in rows if r[columns.index(column)] is not None]
            if values and not all(_NOT_TEXT.fullmatch(v) for v in values):
                out.append((table, column))
    return out


def test_every_table_read_is_grown_by_a_shape():
    from celerp.importers.adapters.odoo import book

    assert GROWN_BY.keys() == {*book.TABLES, "stock_lot"}
    assert set(GROWN_BY.values()) <= {shape for shapes in SHAPES.values() for shape in shapes}


@pytest.mark.parametrize("table, column", _text_columns())
def test_any_text_the_reader_keeps_repeated_across_many_records_reads_within_two_and_a_half_times_its_charge(
        tmp_path, monkeypatch, table, column):
    """Whatever text a backup repeats, the read holds it once or charges every copy it builds:
    one value of LONG characters in a column of every row of a table, across many records made
    from those rows, reads within two and a half times what it is charged, or is refused."""
    from celerp.importers.adapters.odoo import book, dump

    (tmp_path / "warm.zip").write_bytes(_backup(17))
    _read_whole(tmp_path / "warm.zip")
    tables = _shaped_tables(monkeypatch, GROWN_BY[table], GROWN)
    long = _long(len(column))
    columns, rows = tables.rows[table]
    at = columns.index(column)
    for row in rows:
        translated = str(row[at]).startswith('{"en_US"')
        row[at] = json.dumps({"en_US": long}) if translated else long
    path = tmp_path / "grown.zip"
    path.write_bytes(build.backup(17, tables))
    try:
        with dump.Backup(path) as backup:
            book.read_odoo(backup)
            charged = backup.held
        monkeypatch.setattr(dump, "MAX_HELD", charged)
        tracemalloc.start()
        try:
            _read_whole(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    except (ScanError, SourceRevisionError):
        return
    assert peak < 2.5 * charged, f"read took {peak / charged:.2f}x the budget"


# Each column holding JSON -> the shape growing its keys and numbers to the most the reader takes.
GROWN_JSON = {"analytic_distribution": "analytic_repeat"}


def test_every_json_column_read_is_grown_by_a_shape():
    """A JSON column added to the reader with no shape would go unmeasured against its charge:
    test_a_book_the_budget_just_admits_reads_within_two_and_a_half_times_it reads each shape."""
    from celerp.importers.adapters.odoo.book import COLUMN_KINDS

    assert GROWN_JSON.keys() == {column for column, kind in COLUMN_KINDS.items() if kind == "json"}
    assert set(GROWN_JSON.values()) <= {shape for shapes in SHAPES.values() for shape in shapes}


# Distributions Odoo never writes, each in every distributed line: (times account 1 is named in
# the key, the share, the length of account 1's name or None for the fixture's).
_ANALYTIC_AMPLIFIED = {
    "a share smaller than any amount": (1, "1e-999999", None),
    "a share smaller than any amount, its account named 200 times": (200, "1e-999999", None),
    "a long-named account named 1000 times": (1000, "50", 200_000),
    "a long-named account named 5000 times": (5000, "50", 200_000),
}


@pytest.mark.parametrize("case", list(_ANALYTIC_AMPLIFIED))
def test_an_analytic_distribution_odoo_never_writes_is_refused_before_its_tracking_is_built(tmp_path, case):
    """A key naming an account more than once, or a share smaller than any amount, is a damaged
    backup: refused before any tracking is built from it, so a few kilobytes never build labels
    of hundreds of megabytes (one copy of the name per mention, a share spelled in a million digits)."""
    mentions, share, length = _ANALYTIC_AMPLIFIED[case]
    tables = build.tables(17)
    columns, rows = tables.rows["account_move_line"]
    at = columns.index("analytic_distribution")
    for row in rows:
        if row[at]:
            row[at] = f'{{"{",".join(["1"] * mentions)}": {share}}}'
    if length:
        _damage(tables, "account_analytic_account", "name", json.dumps({"en_US": "A" * length}), 1)
    path = tmp_path / "amplified.zip"
    path.write_bytes(build.backup(17, tables))
    tracemalloc.start()
    try:
        with pytest.raises((ScanError, SourceRevisionError), match="incomplete or damaged"):
            _read_whole(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 60 * 1024 ** 2, f"a {len(path.read_bytes())} byte upload built {peak >> 20} MB"


def test_attachments_sharing_one_file_kept_in_the_database_read_within_two_and_a_half_times_its_charge(
        tmp_path, monkeypatch):
    """The database keeps a file shared by many records once; the read holds it once as the
    database wrote it, so a hundred records sharing a 1 MiB file read within two and a half times
    their charge, and each file is decoded only when it is checked or imported."""
    from celerp.importers.adapters.odoo import book, dump

    (tmp_path / "warm.zip").write_bytes(_backup(17))
    _read_whole(tmp_path / "warm.zip")
    tables = build.tables(17)
    columns, rows = tables.rows["ir_attachment"]
    content = b"%PDF-1.7\n" + os.urandom(1024 ** 2)
    for k in range(100):
        row = dict.fromkeys(columns)
        row.update(id=90000 + k, res_id=7, company_id=1, file_size=len(content), name=f"scan-{k}.pdf",
                   res_model="res.partner", type="binary", mimetype="application/pdf",
                   checksum=hashlib.sha1(content).hexdigest(), db_datas="\\x" + content.hex())
        rows.append([row[c] for c in columns])
    path = tmp_path / "shared.zip"
    path.write_bytes(build.backup(17, tables))
    with dump.Backup(path) as backup:
        book.read_odoo(backup)
        charged = backup.held
    monkeypatch.setattr(dump, "MAX_HELD", charged)
    tracemalloc.start()
    try:
        with dump.Backup(path) as backup:
            odoo = book.read_odoo(backup)
            peak = tracemalloc.get_traced_memory()[1]
            screened = attachments.screen(backup, odoo.attachments, {("Contact", odoo.attachments["attachment:90000"].target)})
    finally:
        tracemalloc.stop()
    assert peak < 2.5 * charged, f"read took {peak / charged:.2f}x the charge"
    # Decoded one at a time when checked, each the file Odoo recorded.
    assert all(screened.accepted[f"attachment:{90000 + k}"].sha256 == hashlib.sha256(content).hexdigest()
               for k in range(100))


def _peak_rss() -> int | None:
    """The most memory this process has held since _reset_peak_rss, in bytes; None where the
    system does not say (only Linux does)."""
    try:
        with open("/proc/self/status") as f:
            return next(int(line.split()[1]) * 1024 for line in f if line.startswith("VmHWM:"))
    except OSError:
        return None


def _reset_peak_rss() -> bool:
    try:
        with open("/proc/self/clear_refs", "w") as f:
            f.write("5")
    except OSError:
        return False
    return True


@pytest.mark.asyncio
async def test_a_long_value_copied_into_every_record_is_refused_before_the_copies_are_built(client, monkeypatch,
                                                                                            tmp_path):
    """A product with a code of a million characters, revalued four hundred times: each
    revaluation's record names the code again. The 650 KB upload would make the read hold four
    hundred copies of it, a gigabyte; each copy is charged as it is built, so the backup is refused
    as too large, in the user's language, once they reach the budget (here a sixteenth of it)."""
    import sys
    from celerp.config import settings
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(dump, "MAX_HELD", dump.MAX_HELD // 16)
    monkeypatch.setattr(sys.modules[__name__], "LONG", 1_000_000)
    data = _shaped(tmp_path / "code.zip", monkeypatch, "long_code_revaluations", 400).read_bytes()
    assert len(data) < 700_000
    gc.collect()
    measured = _reset_peak_rss()
    before = _peak_rss()
    tracemalloc.start()
    try:
        r = await scan_upload(client, data, name="code.zip", source="odoo", headers={"Accept-Language": "th"})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_odoo_backup_too_large", "th")
    assert peak < 2.5 * dump.MAX_HELD
    if measured:
        print(f"peak RSS {_peak_rss() / 2**20:.0f} MiB, {(_peak_rss() - before) / 2**20:.0f} MiB above the start")
        assert _peak_rss() - before < 2.5 * dump.MAX_HELD


@pytest.mark.asyncio
async def test_a_stock_valuation_spread_far_past_its_upload_is_refused_before_it_is_built(client, monkeypatch, tmp_path):
    """Fifteen hundred lots of one product, each revalued, spread every revaluation over every
    lot: a 70 KB upload that would build over two million stock moves. The moves are charged as
    they are built, so the backup is refused as too large, in the user's language, once they
    reach the budget (here a sixteenth of it), never past it."""
    from celerp.config import settings
    from celerp.importers.adapters.odoo import dump

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(dump, "MAX_HELD", dump.MAX_HELD // 16)
    data = _shaped(tmp_path / "fan.zip", monkeypatch, "fan", 1_500).read_bytes()
    assert len(data) < 100_000
    tracemalloc.start()
    try:
        r = await scan_upload(client, data, name="fan.zip", source="odoo", headers={"Accept-Language": "th"})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == t("migration.err_odoo_backup_too_large", "th")
    assert peak < 2.5 * dump.MAX_HELD


# Per family, the shape whose read could grow faster than its rows, at the smaller of two sizes
# (one its read takes a thirtieth of a second or more for, so the timing is not lost in noise).
TIMED = {"companies": 30_000, "accounts": 4_000, "posted": 4_000, "contacts": 10_000, "items": 10_000,
         "taxed": 500, "invoices": 7_500, "span": 2_000, "paid": 1_000, "chain": 500, "analytic": 5_000,
         "revalued": 1_500, "transfers": 1_500, "drained": 1_500, "files": 6_000}


def test_every_family_of_tables_read_is_timed():
    assert {next(f for f, shapes in SHAPES.items() if shape in shapes) for shape in TIMED} == SHAPES.keys()


@pytest.mark.parametrize("shape", TIMED)
def test_reading_eight_times_the_rows_takes_about_eight_times_as_long(tmp_path, monkeypatch, shape):
    """Reading a book takes time in proportion to it, whatever its tables hold: no step looks
    through every record of a kind for each record (each journal found every document, each
    revaluation every holding, each line every account, each hop along a chain of settlements
    every settlement). The chain is read at a cutover, where what one settlement carries
    carries the documents it links. Eight times the rows within 22 times the time: in
    proportion it is eight, in the square of them 64, and 22 is halfway between them. A busy
    machine stretches the larger read up to twice as much as the smaller, its caches shared,
    so the sizes are far enough apart for that to stay clear of both. The fastest of three
    reads of each, in CPU time, read in turn."""
    decisions = MigrationDecisions(mode=CIFMode.CUTOVER, cutover_date=CUTOVER) if shape == "chain" else FULL
    paths = []
    for n in (TIMED[shape], 8 * TIMED[shape]):
        paths.append(_shaped(tmp_path / f"{shape}-{n}.zip", monkeypatch, shape, n))
        monkeypatch.undo()
    small, large = fastest_cpu_seconds(*(lambda path=path: _read_whole(path, decisions) for path in paths))
    assert large < 22 * small, (small, large)


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
    assert r.json()["detail"] == t("migration.err_odoo_backup_too_large", "en")


@pytest.mark.parametrize("step, where", [
    ("inspect", "_coverage"), ("build_manifest", "build_bundle"), ("source_expectations", "expectations_from"),
    ("read_attachment", "attachments.read_attachment"), ("inspect", "attachments.screen"), ("inspect", "build_ledger"),
])
def test_running_out_of_memory_anywhere_after_the_backup_is_opened_is_refused_as_too_large(tmp_path, monkeypatch,
                                                                                           step, where):
    """What is built from the rows read (the ledger, the bundle, the expectations, the files)
    can need more memory than the rows: running out there is the same refusal as in the reader."""
    from celerp.importers.adapters.odoo import adapter

    def exhausted(*_, **__):
        raise MemoryError

    owner, name = (attachments, where.split(".")[1]) if "." in where else (adapter, where)
    monkeypatch.setattr(owner, name, exhausted)
    files = _file(tmp_path, _backup(17), f"{step}-{name}.zip")
    with pytest.raises(SourceRevisionError) as refused:
        getattr(_adapter(), step)(*{"inspect": (files,), "read_attachment": (files, FULL, "attachment:1")}.get(
            step, (files, FULL)))
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en")


@pytest.mark.parametrize("step", ["build_ledger", "build_bundle"])
def test_a_book_refused_as_too_large_is_not_kept(tmp_path, monkeypatch, step):
    """A backup refused as too large is never used, so its book is not kept for the next step;
    a book read in full is kept for every step of the run."""
    from celerp.importers.adapters.odoo import adapter

    files = _file(tmp_path, _backup(17))
    adapter._books.cache_clear()
    _adapter().source_expectations(files, FULL)
    assert adapter._books.cache_info().currsize == 1

    def exhausted(*_, **__):
        raise MemoryError

    monkeypatch.setattr(adapter, step, exhausted)
    with pytest.raises(SourceRevisionError) as refused:
        _adapter().build_manifest(files, FULL)
    assert str(refused.value) == t("migration.err_odoo_backup_too_large", "en")
    assert adapter._books.cache_info().currsize == 0, "the refused book is still held"


class _Held:
    """What a step holds when memory runs out in it."""


@pytest.mark.asyncio
@pytest.mark.parametrize("owner, step", [("dump", "_field"), ("book.steps", "roles_from_ledger"),
                                         ("adapter", "build_ledger")])
async def test_the_too_large_refusal_is_built_after_what_ran_out_is_freed(client, monkeypatch, tmp_path, owner, step):
    """The refusal reads its language's messages, which can need memory of its own: it is built
    only once what the failing step held is freed, so a language not yet read still refuses in
    that language (a 422), never a server error."""
    import weakref

    import ui.i18n
    from celerp.config import settings
    from celerp.importers.adapters.odoo import adapter, book, dump

    held = []

    def exhausted(*_, **__):
        holding = _Held()
        held.append(weakref.ref(holding))
        raise MemoryError

    load = ui.i18n._cached_load

    read = []

    def reading(lang):
        if any(ref() is not None for ref in held):
            raise MemoryError
        read.append(lang)
        return load(lang)

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr({"dump": dump, "book.steps": book.steps, "adapter": adapter}[owner], step, exhausted)
    load.cache_clear()
    monkeypatch.setattr(ui.i18n, "_cached_load", reading)
    r = await scan_upload(client, _backup(17), name="harbour.zip", source="odoo", headers={"Accept-Language": "th"})
    assert held, "the step never ran"
    assert r.status_code == 422, r.text
    assert "th" in read
    assert r.json()["detail"] == t("migration.err_odoo_backup_too_large", "th")


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

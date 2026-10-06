# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Import, backup-restore and migration messages say what went wrong and what to do,
in the user's language, without file formats, internal names or raw values."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ui.i18n import set_lang, t

_ROOT = Path(__file__).resolve().parents[1]
_LOCALES = _ROOT / "ui" / "locales"
_LANGS = ("en", "th", "de", "fr", "es", "it", "pt", "id", "vi", "ja", "ar", "am")


def _en(key: str) -> str:
    return json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))[key]


@pytest.fixture
def thai():
    set_lang("th")
    yield
    set_lang("en")


def _xlsx(rows: list[list], *, title: str = "Items") -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# -- Spreadsheet uploads ------------------------------------------------------

def _tabular_error(fn, *args, **kw) -> str:
    from celerp.importers.tabular import TabularError
    with pytest.raises(TabularError) as ei:
        fn(*args, **kw)
    return str(ei.value)


def test_too_many_rows_says_how_to_fix(monkeypatch):
    from celerp.importers import tabular
    monkeypatch.setattr(tabular, "MAX_ROWS", 1)
    msg = _tabular_error(tabular.read_csv, "sku\na\nb\n")
    assert msg == t("import.err_too_many_rows", "en", n_rows=2, max=1)


def test_unnamed_column_names_its_letter():
    from celerp.importers import tabular
    msg = _tabular_error(tabular.read_csv, "sku,,name\na,x,b\n")
    assert msg == t("import.err_column_unnamed", "en", column="B")


def test_missing_header_row_says_choose_another():
    from celerp.importers import tabular
    msg = _tabular_error(tabular.read_csv, "sku\na\n", header_row=5)
    assert msg == t("import.err_header_row_missing", "en")


def test_not_a_workbook_says_save_as_xlsx_or_csv():
    from celerp.importers import tabular
    assert _tabular_error(tabular.read_xlsx, b"not a zip", sheet=None) == t("import.err_not_xlsx", "en")


def test_formula_says_paste_as_values():
    from celerp.importers import tabular
    msg = _tabular_error(tabular.read_xlsx, _xlsx([["sku", "qty"], ["a", "=1+1"]]), sheet=None)
    assert msg == t("import.err_formula", "en")


def test_unknown_sheet_says_choose_another():
    from celerp.importers import tabular
    msg = _tabular_error(tabular.read_xlsx, _xlsx([["sku"], ["a"]]), sheet="Prices")
    assert msg == t("import.err_no_sheet", "en", sheet="'Prices'")


def test_unsupported_type_names_the_type():
    from celerp.importers import tabular
    msg = _tabular_error(tabular.read_table, b"x", "notes.pdf")
    assert msg == t("import.err_unsupported_type", "en", type=".pdf")


@pytest.mark.asyncio
async def test_upload_over_limit_names_the_limit_in_mb():
    from celerp.importers import tabular
    from celerp.importers.tabular import TabularError

    class _Upload:
        def __init__(self):
            self._left = 3

        async def read(self, n):
            self._left -= 1
            return b"x" * n if self._left >= 0 else b""

    with pytest.raises(TabularError) as ei:
        await tabular.read_upload_bytes(_Upload(), limit=2 * 1024 * 1024)
    assert str(ei.value) == t("import.err_file_over_limit", "en", mb=2)


def test_workbook_message_follows_the_user_language(thai):
    from celerp.importers import tabular
    assert _tabular_error(tabular.read_xlsx, b"not a zip", sheet=None) == t("import.err_not_xlsx", "th")


# -- Backup restore -----------------------------------------------------------

def test_non_backup_file_says_choose_a_celerp_backup(tmp_path):
    from celerp.services.backup_import import validate_archive
    path = tmp_path / "notes.celerp-backup"
    path.write_bytes(b"plain text")
    with pytest.raises(ValueError) as ei:
        validate_archive(path)
    assert str(ei.value) == t("error.restore_not_backup", "en")


def test_backup_without_database_says_incomplete(tmp_path):
    from celerp.services.backup_import import validate_archive
    path = tmp_path / "part.celerp-backup"
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps({"celerp_version": "0.0.0"}).encode()
        info = tarfile.TarInfo("meta.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError) as ei:
        validate_archive(path)
    assert str(ei.value) == t("error.restore_incomplete", "en")


def test_unreadable_backup_version_says_nothing_changed():
    from celerp.services.backup_import import _refuse_newer_archive
    with pytest.raises(ValueError) as ei:
        _refuse_newer_archive("not-a-version!")
    assert str(ei.value) == t("error.restore_version_unreadable", "en", recorded="'not-a-version!'")


# -- Moving a company into Celerp ---------------------------------------------

def test_prepared_by_limits_say_how_to_fix():
    from celerp.services.migrations import MigrationError, validate_prepared_by
    with pytest.raises(MigrationError) as ei:
        validate_prepared_by("x" * 201)
    assert ei.value.detail == {"prepared_by": t("migration.err_prepared_by_too_long", "en", max=200)}
    with pytest.raises(MigrationError) as ei:
        validate_prepared_by("a\nb")
    assert ei.value.detail == {"prepared_by": t("migration.err_prepared_by_one_line", "en")}


def test_illegal_action_shows_the_status_label_in_the_user_language(thai):
    from celerp.services.migrations import _illegal
    err = _illegal("start", SimpleNamespace(status="running"))
    assert err.detail == t("migration.err_cannot_start", "th", status=t("migration.status.running", "th"))


def test_cutover_before_records_names_the_first_day():
    from datetime import date
    from celerp.services.migrations import _cutover_date
    scan = SimpleNamespace(scan=SimpleNamespace(period_start=date(2026, 1, 1), period_end=date(2026, 3, 31)))
    with pytest.raises(ValueError) as ei:
        _cutover_date(scan, "2025-12-31")
    assert str(ei.value) == t("migration.err_cutover_before_start", "en", date="2026-01-01")
    with pytest.raises(ValueError) as ei:
        _cutover_date(scan, "31/12/2025")
    assert str(ei.value) == t("migration.err_cutover_format", "en")


def test_one_business_file_at_a_time():
    from celerp.importers.adapters.base import ScanError
    from celerp.importers.adapters.manager_io.adapter import _single
    with pytest.raises(ScanError) as ei:
        _single([object(), object()])
    assert str(ei.value) == t("migration.err_one_business_file", "en")


def test_store_failure_says_check_disk_space():
    from celerp.services import migration_scan_store as store
    assert t(store.STORE_FAILED, "en") == _en("migration.err_store_failed")


@pytest.mark.parametrize("key", [
    "migration.err_not_found", "migration.err_owner_only", "migration.err_bootstrapped",
    "migration.err_newer_revision", "error.restore_pg_newer", "import.err_xlsx_open",
    "migration.attachment_too_large", "error.contacts_not_found",
])
def test_every_language_has_the_message(key):
    for lang in _LANGS:
        raw = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        assert raw.get(key), (lang, key)
        assert "—" not in raw[key], (lang, key)


def test_unused_import_messages_are_gone():
    for lang in _LANGS:
        raw = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("flash.import_aborted", "msg.location_col_hint"):
            assert key not in raw, (lang, key)

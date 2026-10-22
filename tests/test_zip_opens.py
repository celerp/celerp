# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every zip the app opens from an upload: a damaged one is refused in the user's language, and
one whose directory is beyond what opening it may hold is refused before it is opened."""

from __future__ import annotations

import ast
import io
import struct
import types
import zipfile
from pathlib import Path

import pytest

from ui.i18n import t

ROOT = Path(__file__).resolve().parent.parent
ATTACHMENTS = "default_modules/celerp-inventory/celerp_inventory/routes_attachments.py"


def _zip64_tail(disk: int, record_at: int) -> bytes:
    """A ZIP64 end locator naming `disk` and a record at `record_at`, then an empty end record."""
    return (struct.pack("<4sLQL", b"PK\x06\x07", disk, record_at, 1)
            + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 0))


# End records zipfile cannot parse, or whose directory is larger than the file holding it: zipfile
# refuses each when it opens the file, so each is damaged, never too large.
DAMAGED_TAILS = {
    "on_two_disks": _zip64_tail(1, 0),
    "record_outside_the_file": _zip64_tail(0, 10 ** 9),
    "record_missing": _zip64_tail(0, 0),
    "directory_of_4_gib_in_22_bytes": struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 5, 5, 0xFFFFFFF0, 0, 0),
    "directory_of_300_mib_after_a_body": b"PK\x03\x04" + bytes(200)
    + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 9, 9, 300 * 1024 ** 2, 0, 0),
}

# Every function that opens an uploaded zip, each behind the zip preflight, and each way an upload
# reaches it.
PREFLIGHT_CALLERS = {
    "celerp/importers/tabular.py:_xlsx_lines": ("read_table", "read_table_at_header", "workbook_lines"),
    "celerp/importers/adapters/exports/files.py:_members": ("scan", "scan:xero", "scan:quickbooks_online"),
    "celerp/importers/adapters/odoo/dump.py:__init__": ("scan:odoo",),
    "celerp/modules/importer.py:install_from_zip": ("install_from_zip",),
    "celerp/services/company_backup.py:read_backup": ("read_backup",),
    f"{ATTACHMENTS}:bulk_attach_files": ("files/bulk",),
}
WAYS = [way for ways in PREFLIGHT_CALLERS.values() for way in ways]

# The zip opens that read no upload, so neither the preflight nor a damaged-zip refusal applies.
TRUSTED_OPENS = {
    "celerp/services/company_backup.py:_export_company": "writes the company copy it makes",
    "celerp/services/company_backup.py:_within_limits": "re-reads a copy read_backup opened and checked, or one just "
                                                        "written here",
    "celerp/services/company_backup.py:_scan_rows": "re-reads a copy read_backup opened and checked whole",
    "celerp/services/company_backup.py:restore_company": "re-reads a copy read_backup opened and checked whole",
    "celerp/services/company_backup.py:_copy_facts": "re-reads a copy read_backup opened and checked whole",
}


def _names(node: ast.AST) -> set[str]:
    return {getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(node)} - {None}


def _zip_opens() -> dict[str, list[tuple[ast.AST, list[ast.AST]]]]:
    """Each function opening a zip in the app, with every ZipFile call in it and its ancestors."""
    found: dict[str, list] = {}

    def visit(node, path, ancestors):
        if isinstance(node, ast.Call) and "ZipFile" in {getattr(node.func, "id", None),
                                                        getattr(node.func, "attr", None)}:
            func = next(a for a in reversed(ancestors) if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)))
            found.setdefault(f"{path}:{func.name}", []).append((node, ancestors))
        for child in ast.iter_child_nodes(node):
            visit(child, path, [*ancestors, node])

    for file in [*(ROOT / "celerp").rglob("*.py"), *(ROOT / "default_modules").rglob("*.py")]:
        visit(ast.parse(file.read_text(encoding="utf-8")), file.relative_to(ROOT).as_posix(), [])
    return found


def _handled(call: ast.AST, ancestors: list[ast.AST]) -> bool:
    """Whether the call runs in the body of a try with a handler catching ZIP_ERRORS."""
    for parent, child in zip(ancestors, [*ancestors[1:], call]):
        if isinstance(parent, ast.Try) and child in parent.body and any(
                h.type is not None and "ZIP_ERRORS" in _names(h.type) for h in parent.handlers):
            return True
    return False


def test_every_zip_the_app_opens_from_an_upload_is_preflighted_handled_and_probed():
    """A new zip open is covered on its own: it must sit in a try catching ZIP_ERRORS, in a function
    running zip_directory_fits, and be probed below with every damaged tail, or be named as trusted."""
    opens = _zip_opens()
    assert set(TRUSTED_OPENS) <= set(opens), "a trusted open no longer exists"
    for where, calls in opens.items():
        if where in TRUSTED_OPENS:
            continue
        assert all(_handled(call, ancestors) for call, ancestors in calls), f"{where} opens a zip outside ZIP_ERRORS"
        func = next(a for a in reversed(calls[0][1]) if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)))
        assert "zip_directory_fits" in _names(func), f"{where} opens a zip without the preflight"
    assert set(opens) - set(TRUSTED_OPENS) == set(PREFLIGHT_CALLERS)


async def _bulk(client, data: bytes):
    r = await client.post("/auth/register", json={"company_name": "ZipCo", "email": "zips@example.com",
                                                  "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return await client.post("/items/files/bulk", files={"file": ("photos.zip", data, "application/zip")},
                             headers={"Authorization": f"Bearer {r.json()['access_token']}"})


async def _refusal(way: str, data: bytes, client, monkeypatch, tmp_path) -> str:
    """The message `way` refuses `data` with."""
    from celerp.config import settings
    from celerp.importers import tabular
    from celerp.importers.adapters.exports import files
    from celerp.modules.importer import ModuleImportError, install_from_zip
    from celerp.services.company_backup import BackupError, read_backup
    from migration_support import scan_upload

    reads = {"read_table": lambda: tabular.read_table(data, "items.xlsx"),
             "read_table_at_header": lambda: tabular.read_table_at_header(data, "items.xlsx"),
             "workbook_lines": lambda: tabular.workbook_lines(data, files.LIMITS),
             "install_from_zip": lambda: install_from_zip(data)}
    if way in reads:
        with pytest.raises((tabular.TabularError, ModuleImportError)) as refused:
            reads[way]()
        return str(refused.value)
    if way == "read_backup":
        path = tmp_path / "copy.zip"
        path.write_bytes(data)
        with pytest.raises(BackupError) as refused:
            read_backup(path)
        assert refused.value.status_code == 422
        return refused.value.detail
    if way == "files/bulk":
        r = await _bulk(client, data)
    else:
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        r = await scan_upload(client, data, name="books.zip", source=way.partition(":")[2] or None)
    assert r.status_code == 422, r.text
    return r.json()["detail"]


def _unreadable(way: str) -> tuple[str, ...]:
    """What `way` says of a zip it cannot read, as main says it."""
    if way.startswith("scan"):
        return ('Celerp cannot read "books.zip" yet.',) if way == "scan" else ('"books.zip" is not a',)
    return {"install_from_zip": (t("module_import.not_zip"),),
            "read_backup": (t("company_backup.err_damaged"), t("company_backup.err_not_a_backup")),
            "files/bulk": (t("inventory.err_zip_invalid"),)}.get(way, (t("import.err_not_xlsx"),))


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", list(DAMAGED_TAILS))
@pytest.mark.parametrize("way", WAYS)
async def test_a_zip_whose_end_records_do_not_parse_is_refused_as_unreadable(client, monkeypatch, tmp_path, way, tail):
    """Every spreadsheet import, every source scan, a module install, a company copy and a bulk
    attachment zip read such a file as zipfile does: unreadable, never a server error."""
    message = await _refusal(way, DAMAGED_TAILS[tail], client, monkeypatch, tmp_path)
    assert message.startswith(_unreadable(way)), message


def _damaged_member(name: str, damage: str) -> bytes:
    """A zip holding one file `name` that zipfile lists but cannot unpack: a stream that is not LZMA,
    or a compression method it does not know."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_LZMA) as z:
        z.writestr(name, b"{}" * 4096)
    data = bytearray(out.getvalue())
    body = 30 + len(name)
    if damage == "lzma_stream":
        data[body + 9:body + 40] = b"\xff" * 31
    else:
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, 8, 99)
        struct.pack_into("<H", data, central + 10, 99)
    return bytes(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["lzma_stream", "unknown_method"])
@pytest.mark.parametrize("way", ["install_from_zip", "read_backup"])
async def test_a_zip_whose_file_does_not_unpack_is_refused_as_damaged(client, monkeypatch, tmp_path, way, damage):
    """The file a module install or a company copy reads first does not unpack: damaged, never a
    server error."""
    data = _damaged_member("__init__.py" if way == "install_from_zip" else "manifest.json", damage)
    message = await _refusal(way, data, client, monkeypatch, tmp_path)
    expected = t("module_import.not_zip") if way == "install_from_zip" else t("company_backup.err_damaged")
    assert message == expected


def _many_files(count: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for k in range(count):
            z.writestr(f"{k:08d}.csv", b"")
    return out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["install_from_zip", "read_backup", "files/bulk"])
async def test_a_zip_listing_more_files_than_opening_it_may_hold_is_refused_unopened(client, monkeypatch, tmp_path,
                                                                                   way):
    """300,000 empty files, offered as a module, a company copy or a bulk attachment zip: each is
    refused from the size its directory declares, before zipfile builds an entry for any of them."""
    import importlib

    data = _many_files(300_000)

    def unopened(*args, **kwargs):
        raise AssertionError("the zip was opened")

    for module in ("celerp.modules.importer", "celerp.services.company_backup", "celerp_inventory.routes_attachments"):
        monkeypatch.setattr(importlib.import_module(module), "zipfile",
                            types.SimpleNamespace(**{**vars(zipfile), "ZipFile": unopened}))
    message = await _refusal(way, data, client, monkeypatch, tmp_path)
    assert message == {"install_from_zip": t("module_import.too_many_files"),
                       "read_backup": t("company_backup.err_too_large"),
                       "files/bulk": t("inventory.err_zip_too_many_files", max=5000)}[way]

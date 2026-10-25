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

from company_backup_support import repacked
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


async def _owner(client) -> str:
    r = await client.post("/auth/register", json={"company_name": "ZipCo", "email": "zips@example.com",
                                                  "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _bulk(client, data: bytes, token: str | None = None):
    return await client.post("/items/files/bulk", files={"file": ("photos.zip", data, "application/zip")},
                             headers={"Authorization": f"Bearer {token or await _owner(client)}"})


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


# The calls that read a file out of a zip, and where the one check they go through lives.
ZIP_READS = {"open", "read", "extract", "extractall"}
MEMBER_CHECK = "celerp/importers/tabular.py:open_zip_member"


def _zip_bound(tree: ast.AST) -> set[str]:
    """Every name a file binds to a zip: a parameter typed ZipFile, and the target of a with or an
    assignment opening one."""
    bound: set[str] = set()

    def opens(node) -> bool:
        return isinstance(node, ast.Call) and "ZipFile" in _names(node.func)

    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None and "ZipFile" in _names(node.annotation):
            bound.add(node.arg)
        elif isinstance(node, ast.withitem) and opens(node.context_expr) and node.optional_vars is not None:
            bound |= _names(node.optional_vars) - {"self"}
        elif isinstance(node, ast.Assign) and opens(node.value):
            bound |= {getattr(t, "id", None) or t.attr for t in node.targets}
    return bound


def _zip_reads() -> dict[str, list[ast.Call]]:
    """Each function reading a file out of a zip in the app, with the calls doing it. A write
    (`open(name, "w")`) is not a read."""
    found: dict[str, list] = {}
    for file in [*(ROOT / "celerp").rglob("*.py"), *(ROOT / "default_modules").rglob("*.py")]:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        bound = _zip_bound(tree)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ZIP_READS
                        and (getattr(node.func.value, "id", None) or getattr(node.func.value, "attr", None)) in bound
                        and not any(isinstance(a, ast.Constant) and a.value == "w" for a in node.args)):
                    found.setdefault(f"{file.relative_to(ROOT).as_posix()}:{func.name}", []).append(node)
    return found


def test_every_file_the_app_reads_out_of_a_zip_goes_through_the_member_check():
    """A new reader is covered on its own: only open_zip_member opens a file of a zip, so none is
    unpacked with a password or a method outside ZIP_METHODS. A workbook, which openpyxl unpacks
    itself, is checked file by file before it is handed over."""
    assert set(_zip_reads()) == {MEMBER_CHECK}
    loads = [f"{f.relative_to(ROOT).as_posix()}:{func.name}"
             for f in [*(ROOT / "celerp").rglob("*.py"), *(ROOT / "default_modules").rglob("*.py")]
             for func in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
             if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) and "load_workbook" in _names(func)]
    assert loads == ["celerp/importers/tabular.py:_xlsx_lines"]
    func = next(n for n in ast.walk(ast.parse((ROOT / "celerp/importers/tabular.py").read_text(encoding="utf-8")))
                if isinstance(n, ast.FunctionDef) and n.name == "_xlsx_lines")
    assert "check_zip_member" in _names(func)
    # Every function opening an uploaded zip reads its files through the check (bulk attachments
    # checks each file up front, then reads it).
    for where in PREFLIGHT_CALLERS:
        path, _, name = where.partition(":")
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        assert _names(tree) & {"open_zip_member", "read_zip_member", "check_zip_member", "_open"}, where


def _plain_zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in entries.items():
            z.writestr(name, body)
    return out.getvalue()


def _workbook() -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    book.active.append(["sku", "name"])
    book.active.append(["A-1", "Widget"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


PACKINGS = {"lzma": (zipfile.ZIP_LZMA, False), "bzip2": (zipfile.ZIP_BZIP2, False),
            "deflate_with_password": (zipfile.ZIP_DEFLATED, True), "stored_with_password": (zipfile.ZIP_STORED, True)}
MODULE = (b'PLUGIN_MANIFEST = {"name": "zip-check", "version": "1.0.0", "display_name": "Zip check", '
          b'"license": "MIT", "author": "Example", "description": "", "depends": [], "api_version": "1"}\n')


@pytest.mark.asyncio
@pytest.mark.parametrize("packing", list(PACKINGS))
@pytest.mark.parametrize("way", ["read_table", "export_set", "install_from_zip", "files/bulk"])
async def test_a_file_packed_with_another_method_or_a_password_is_refused_before_it_is_unpacked(
        client, monkeypatch, tmp_path, way, packing):
    """A workbook, an export set, a module and a bulk attachment zip whose files are packed with a
    method outside ZIP_METHODS, or with a password, are refused with the route's unreadable
    message before a byte is unpacked. The same zips deflated or stored are read."""
    from celerp.importers import tabular
    from celerp.importers.adapters.base import ScanError
    from celerp.importers.adapters.exports import files
    from celerp.modules import importer

    method, password = PACKINGS[packing]
    plain = {"read_table": _workbook, "export_set": lambda: _plain_zip({"Contacts.csv": b"Name\nHarbour\n"}),
             "install_from_zip": lambda: _plain_zip({"zipcheck/__init__.py": MODULE}),
             "files/bulk": lambda: _plain_zip({"A-1.png": b"\x89PNG\r\n\x1a\n" + bytes(64)})}[way]()
    token = await _owner(client) if way == "files/bulk" else None
    monkeypatch.setattr(importer, "_finish", lambda *a, **k: {"name": "zip-check"})
    unpacked = []
    real_open = zipfile.ZipFile.open
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, name, mode="r", *a, **k: (
        unpacked.append(name) if mode == "r" else None) or real_open(self, name, mode, *a, **k))

    async def refusal(data: bytes) -> str | None:
        if way == "export_set":
            path = tmp_path / "books.zip"
            path.write_bytes(data)
            try:
                files._members("books.zip", path)
            except ScanError as exc:
                return str(exc)
            return None
        if way == "files/bulk":
            r = await _bulk(client, data, token)
            return r.json()["detail"] if r.status_code == 422 else None
        try:
            tabular.read_table(data, "items.xlsx") if way == "read_table" else importer.install_from_zip(data)
        except (tabular.TabularError, importer.ModuleImportError) as exc:
            return str(exc)
        return None

    for kept in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
        assert await refusal(repacked(plain, kept)) is None
    data = repacked(plain, method, password=password)
    unpacked.clear()
    message = await refusal(data)
    expected = {"read_table": t("import.err_not_xlsx"), "install_from_zip": t("module_import.not_zip"),
                "export_set": t("migration.err_export_unreadable", name="books.zip"),
                "files/bulk": t("inventory.err_zip_invalid")}[way]
    assert message == expected
    assert not unpacked, unpacked


_GROWTH = """
import io, sys, zipfile
from celerp.modules import importer
from celerp.services import company_backup
where, path, name, sha, rows = sys.argv[1:]
data = open(path, "rb").read()


def rss(field):
    status = open("/proc/self/status").read()
    return int(status.split(field + ":")[1].split()[0])


open("/proc/self/clear_refs", "w").write("5")  # the peak so far, starting the app included, is reset to now
base = rss("VmRSS")
try:
    if where == "backup":
        company_backup._check_member(company_backup._Budget(), zipfile.ZipFile(io.BytesIO(data)), name, sha,
                                     rows=int(rows))
    else:
        importer.install_from_zip(data)
except Exception:
    pass
print((rss("VmHWM") - base) // 1024)
"""


@pytest.mark.timeout(240)
@pytest.mark.skipif(not Path("/proc/self/clear_refs").exists(), reason="peak RSS is reset through Linux procfs")
@pytest.mark.parametrize("method", [zipfile.ZIP_LZMA, zipfile.ZIP_DEFLATED], ids=["lzma", "deflate"])
@pytest.mark.parametrize("where", ["backup", "module"])
def test_a_large_lzma_dictionary_does_not_grow_the_server(tmp_path, where, method):
    """A company copy member or a module file of 128 MiB, packed with LZMA whose entry declares a
    1 GiB dictionary, is refused unpacked; read, it held about 2.7 times its size. The same content
    deflated streams within the same bound. Peak RSS growth of a fresh process, from once it has
    started."""
    import hashlib
    import os
    import subprocess
    import sys

    line = b'{"id": "%08d", "kind": "item.created", "data": {"name": "Widget"}}\n'
    body = b"".join(line % k for k in range(128 * 1024 ** 2 // len(line % 0)))
    name = "tables/events.jsonl" if where == "backup" else "pkg/events.jsonl"
    data = _plain_zip({"pkg/__init__.py" if where == "module" else "manifest.json": b"", name: body})
    path = tmp_path / "upload.zip"
    path.write_bytes(repacked(data, method))
    env = {**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'tests'}", "ALLOW_INSECURE_JWT": "true"}
    out = subprocess.run([sys.executable, "-c", _GROWTH, where, str(path), name, hashlib.sha256(body).hexdigest(),
                          str(body.count(b"\n"))], cwd=ROOT, env=env, check=True, capture_output=True, text=True,
                         timeout=200).stdout
    assert int(out.split()[-1]) < 64, f"{where} grew by {out.split()[-1]} MiB"

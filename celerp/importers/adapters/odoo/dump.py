# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read an Odoo backup as Odoo's database manager writes it: one zip holding
`manifest.json`, `dump.sql` (a plain PostgreSQL dump) and `filestore/`, at its root or in
the one folder of a zip made again from the extracted backup.

The tables are read straight from the dump's COPY blocks, so no database server and no
Odoo install are needed. Odoo 13 to 17 differ in a few places this module evens out:
from 16 a translated name is stored as JSON keyed by language, and the lot table
`stock_production_lot` became `stock_lot`.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path

from celerp.importers.adapters.base import ScanError, SourceRevisionError
from ui.i18n import t

MANIFEST = "manifest.json"
DUMP = "dump.sql"
FILESTORE = "filestore/"
DUMP_HEADER = "-- PostgreSQL database dump"
OLDEST, NEWEST = 13, 17
LANGUAGE = "en_US"

Row = dict[str, str | None]

_COPY = re.compile(r'^COPY (?:"?public"?\.)?"?(\w+)"? \((.*)\) FROM stdin;$')
_ESCAPE = re.compile(r"\\(?:([0-7]{1,3})|x([0-9a-fA-F]{1,2})|(.))")
_ESCAPED = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _unescape(match: re.Match) -> str:
    octal, hexa, char = match.groups()
    if octal:
        return chr(int(octal, 8))
    if hexa:
        return chr(int(hexa, 16))
    return _ESCAPED.get(char, char)


def _field(value: str) -> str | None:
    if value == "\\N":
        return None
    return _ESCAPE.sub(_unescape, value) if "\\" in value else value


def _major(manifest: dict) -> int | None:
    found = re.search(r"(\d+)\.", str(manifest.get("major_version") or manifest.get("version") or ""))
    return int(found.group(1)) if found else None


def _root(archive: zipfile.ZipFile) -> str | None:
    """Where the backup sits in the zip: at its root as Odoo writes it, or in the one folder
    a zip made again from the extracted backup puts it in (the files macOS adds are not
    counted). None when the zip holds no backup or more than one."""
    names = {n for n in archive.namelist()
             if not n.startswith("__MACOSX/") and not n.rsplit("/", 1)[-1].startswith("._")}
    if {MANIFEST, DUMP} <= names:
        return ""
    folders = {n.split("/", 1)[0] for n in names}
    if len(folders) == 1 and "/" in next(iter(names), ""):
        root = folders.pop() + "/"
        if {root + MANIFEST, root + DUMP} <= names:
            return root
    return None


def _manifest(archive: zipfile.ZipFile, root: str) -> dict | None:
    """The backup's manifest, or None when the files are not an Odoo backup."""
    try:
        manifest = json.loads(archive.read(root + MANIFEST))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(manifest, dict) or "odoo_dump" not in manifest or _major(manifest) is None:
        return None
    with archive.open(root + DUMP) as dump:
        head = dump.read(256).decode("utf-8", "replace")
    return manifest if DUMP_HEADER in head else None


class Backup:
    """One Odoo backup zip, open for reading."""

    def __init__(self, path: Path) -> None:
        try:
            self.archive = zipfile.ZipFile(path)
            self.root = _root(self.archive)
            manifest = None if self.root is None else _manifest(self.archive, self.root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ScanError(t("migration.err_odoo_not_backup")) from exc
        if manifest is None:
            self.archive.close()
            raise ScanError(t("migration.err_odoo_not_backup"))
        self.version = _major(manifest)
        if not OLDEST <= self.version <= NEWEST:
            self.archive.close()
            raise SourceRevisionError(t("migration.err_odoo_version", version=self.version))
        self.database = str(manifest.get("db_name") or "")

    def __enter__(self) -> Backup:
        return self

    def __exit__(self, *exc) -> None:
        self.archive.close()

    @property
    def lot_table(self) -> str:
        return "stock_lot" if self.version >= 16 else "stock_production_lot"

    def tables(self, names: Iterable[str], columns: frozenset[str]) -> dict[str, list[Row]]:
        """The rows of each named table the dump holds, in dump order, with only the named
        columns. A table the dump does not hold (its module is not installed) is absent."""
        wanted = set(names)
        out: dict[str, list[Row]] = {}
        with self.archive.open(self.root + DUMP) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            rows: list[Row] | None = None
            kept: list[tuple[int, str]] = []
            for line in lines:
                if rows is not None:
                    if line == "\\.\n":
                        rows = None
                        continue
                    values = line.rstrip("\n").split("\t")
                    rows.append({name: _field(values[i]) for i, name in kept})
                    continue
                if not line.startswith("COPY "):
                    continue
                match = _COPY.match(line.rstrip("\n"))
                if match and match.group(1) in wanted:
                    header = [c.strip().strip('"') for c in match.group(2).split(",")]
                    kept = [(i, name) for i, name in enumerate(header) if name in columns]
                    rows = out.setdefault(match.group(1), [])
        return out

    def text(self, value: str | None) -> str:
        """A name as Odoo shows it in English. From Odoo 16 a translated name is stored as
        JSON keyed by language; earlier names are plain text."""
        if not value:
            return ""
        if self.version >= 16 and value.startswith("{"):
            try:
                names = json.loads(value)
            except ValueError:
                return value
            if isinstance(names, dict) and names:
                return str(names.get(LANGUAGE) or next(iter(names.values())) or "")
        return value

    def stored_file(self, store_fname: str, limit: int) -> bytes | None:
        """The content of one filestore file, or None when the backup does not hold it or it
        is larger than `limit`. The name comes from the dump and is untrusted."""
        name = self.root + FILESTORE + store_fname
        if ".." in store_fname.split("/") or store_fname.startswith("/"):
            return None
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > limit:
            return None
        return self.archive.read(info)

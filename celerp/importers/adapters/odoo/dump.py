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
import lzma
import re
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import IO

from celerp.importers.adapters.base import ScanError, SourceRevisionError
from ui.i18n import t

MANIFEST = "manifest.json"
DUMP = "dump.sql"
FILESTORE = "filestore/"
DUMP_HEADER = "-- PostgreSQL database dump"
DUMP_COMPLETE = "-- PostgreSQL database dump complete"  # the line pg_dump ends a whole dump with
CUSTOM_FORMAT = b"PGDMP"  # how a pg_dump custom format file starts
ZIP_ENTRY = b"PK\x03\x04"  # how each file in a zip starts; Odoo writes the dump first
UNREADABLE = "migration.err_odoo_backup_damaged"
ENCRYPTED = "migration.err_odoo_backup_encrypted"
DAMAGED = "migration.err_odoo_dump_damaged"
TOO_LARGE = "migration.err_file_too_large"
# What one backup may make the server unpack and hold. A database's dump packs about ten to one;
# a far higher ratio is a file made to unpack large, not a database. The longest line a wanted
# table holds is an attachment stored in its row: up to 25 MB as hex, twice the characters.
MAX_DUMP_BYTES = 20 * 1024 ** 3  # ten times the largest upload
MAX_RATIO = 100
MAX_LINE = 64 * 1024 ** 2  # characters
MAX_MANIFEST = 1024 ** 2  # Odoo's is a few kilobytes
# How a file in a zip fails to unpack, whichever way it is packed: a stream that does not
# decompress or ends early, a failed checksum, or a method or option this reader cannot unpack.
BROKEN = (EOFError, NotImplementedError, OSError, lzma.LZMAError, zipfile.BadZipFile, zlib.error)
PASSWORD = 0x1  # the zip flag of a file packed with a password
OLDEST, NEWEST = 13, 17
LANGUAGE = "en_US"


class Row(tuple):
    """One row of a table with only the columns kept, read by column name. Each table's rows
    share one index of its columns, so a row costs a tuple rather than a dict."""
    __slots__ = ()
    index: dict[str, int] = {}

    def __getitem__(self, name: str) -> str | None:
        return tuple.__getitem__(self, self.index[name])

    def get(self, name: str, default: str | None = None) -> str | None:
        at = self.index.get(name)
        return default if at is None else tuple.__getitem__(self, at)


_CREATE = re.compile(r'^CREATE TABLE (?:"?public"?\.)?"?(\w+)"? \($')
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


@contextmanager
def _intact(error: type[Exception] = ScanError) -> Iterator[None]:
    """Reading the zip: a file in it that does not unpack (BROKEN), or a dump that is not
    UTF-8 text (every Odoo database is), is refused with `error`."""
    try:
        yield
    except UnicodeDecodeError as exc:
        raise error(t(DAMAGED)) from exc
    except BROKEN as exc:
        raise error(t(UNREADABLE)) from exc


def _open(archive: zipfile.ZipFile, name: str) -> IO[bytes]:
    """One file of the zip, open for reading. Raises SourceRevisionError when it is packed with
    a password, which a backup must be made again without."""
    info = archive.getinfo(name)
    if info.flag_bits & PASSWORD:
        raise SourceRevisionError(t(ENCRYPTED))
    return archive.open(info)


def _cut_short(path: Path) -> bool:
    """Whether a file that does not open as a zip starts as an Odoo backup does, with the
    dump or the manifest, so its end was lost in the upload or copy."""
    with open(path, "rb") as f:
        head = f.read(1024)
    name = head[30:30 + int.from_bytes(head[26:28], "little")].decode("utf-8", "replace")
    return head.startswith(ZIP_ENTRY) and name.count("/") <= 1 and name.rsplit("/", 1)[-1] in (DUMP, MANIFEST)


def _root(archive: zipfile.ZipFile) -> str | None:
    """Where the backup's manifest sits in the zip: at its root as Odoo writes it, or in the
    one folder a zip made again from the extracted backup puts it in (the files macOS adds
    are not counted). None when the zip holds no manifest or more than one."""
    names = {n for n in archive.namelist()
             if not n.startswith("__MACOSX/") and not n.rsplit("/", 1)[-1].startswith("._")}
    if MANIFEST in names:
        return ""
    folders = {n.split("/", 1)[0] for n in names}
    if len(folders) == 1 and "/" in next(iter(names), ""):
        root = folders.pop() + "/"
        if root + MANIFEST in names:
            return root
    return None


def _manifest(archive: zipfile.ZipFile, root: str) -> dict | None:
    """The backup's manifest, or None when the files are not an Odoo backup. Raises
    SourceRevisionError when they are one without its dump, or with a manifest or dump that
    would unpack beyond what the server reads; the sizes are the ones the zip declares, which
    unpacking never goes past."""
    if archive.getinfo(root + MANIFEST).file_size > MAX_MANIFEST:
        raise SourceRevisionError(t(TOO_LARGE))
    try:
        with _open(archive, root + MANIFEST) as f:
            manifest = json.loads(f.read())
    except ValueError:
        return None
    if not isinstance(manifest, dict) or "odoo_dump" not in manifest or _major(manifest) is None:
        return None
    try:
        info = archive.getinfo(root + DUMP)
    except KeyError as exc:
        raise SourceRevisionError(t(UNREADABLE)) from exc
    if info.file_size > min(MAX_DUMP_BYTES, MAX_RATIO * info.compress_size):
        raise SourceRevisionError(t(TOO_LARGE))
    with _open(archive, info.filename) as dump:
        head = dump.read(256).decode("utf-8", "replace")
    return manifest if DUMP_HEADER in head else None


def _refuse_custom_format(files: Iterable[Callable[[], IO[bytes]]]) -> None:
    """Refuse Odoo's other backup format, pg_dump custom, with its own message: the file
    itself, or a file zipped at the root or in one folder. `files` open each candidate; one
    that does not open is not that format."""
    for open_file in files:
        try:
            with _intact(), open_file() as f:
                custom = f.read(len(CUSTOM_FORMAT)) == CUSTOM_FORMAT
        except (ScanError, SourceRevisionError):
            continue
        if custom:
            raise SourceRevisionError(t("migration.err_odoo_custom_format"))


class Backup:
    """One Odoo backup zip, open for reading."""

    def __init__(self, path: Path) -> None:
        try:
            self.archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            _refuse_custom_format([partial(open, path, "rb")])
            if _cut_short(path):
                raise SourceRevisionError(t(UNREADABLE)) from exc
            raise ScanError(t("migration.err_odoo_not_backup")) from exc
        except OSError as exc:
            raise ScanError(t("migration.err_odoo_not_backup")) from exc
        # A damaged backup is still recognised as one, so detection gives the reason it cannot be read.
        try:
            with _intact(SourceRevisionError):
                self.root = _root(self.archive)
                manifest = None if self.root is None else _manifest(self.archive, self.root)
        except SourceRevisionError:
            self.archive.close()
            raise
        if manifest is None:
            with self.archive:
                _refuse_custom_format(partial(_open, self.archive, n) for n in self.archive.namelist()
                                      if n.count("/") <= 1 and not n.endswith("/"))
            raise ScanError(t("migration.err_odoo_not_backup"))
        self.version = _major(manifest)
        if not OLDEST <= self.version <= NEWEST:
            self.archive.close()
            raise SourceRevisionError(t("migration.err_odoo_version", version=self.version))
        self.database = str(manifest.get("db_name") or "")
        # The modules installed, as Odoo lists them in every backup's manifest; they decide which
        # tables the dump must hold. A list without base, which every database has, is not one.
        modules = manifest.get("modules")
        if not isinstance(modules, dict) or "base" not in modules:
            self.archive.close()
            raise ScanError(t(DAMAGED))
        self.modules = frozenset(modules)

    def __enter__(self) -> Backup:
        return self

    def __exit__(self, *exc) -> None:
        self.archive.close()

    @property
    def lot_table(self) -> str:
        return "stock_lot" if self.version >= 16 else "stock_production_lot"

    def tables(self, names: Iterable[str], columns: frozenset[str],
               where: Mapping[str, Callable[[Row], bool]] | None = None,
               required: Iterable[str] = ()) -> dict[str, list[Row]]:
        """The rows of each named table the dump holds, in dump order, with only the named
        columns, and of a table in `where` only the rows its test keeps. A table the dump does
        not hold (its module is not installed) is absent. The dump is streamed and a value
        repeated within a table is held once, and a line longer than MAX_LINE is never held
        whole: one outside the wanted tables' rows is read in pieces and dropped. Raises
        ScanError, before any row is used, when a wanted row is that long or the dump is cut
        short or damaged: it does not unpack, has no closing line, a table's data never ends or
        never starts, a row has more or fewer fields than its table, or a `required` table (one
        an installed module makes) is missing."""
        wanted, where = set(names) | set(required), where or {}
        out: dict[str, list[Row]] = {}
        made: set[str] = set()
        copying: str | None = None
        complete = False
        with _intact(), _open(self.archive, self.root + DUMP) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            rows: list[Row] | None = None
            for line in iter(partial(lines.readline, MAX_LINE + 1), ""):
                if len(line) > MAX_LINE and not line.endswith("\n"):
                    if rows is not None:
                        raise ScanError(t("migration.err_odoo_record_too_large", limit=f"{MAX_LINE / 1024 ** 2:g}"))
                    while (rest := lines.readline(MAX_LINE)) and not rest.endswith("\n"):
                        pass
                    continue
                if copying is not None:
                    if line == "\\.\n":
                        copying = rows = None
                    elif rows is not None:
                        values = line.rstrip("\n").split("\t")
                        if len(values) != width or not line.endswith("\n"):
                            raise ScanError(t(DAMAGED))
                        fields = [_field(values[i]) for i in kept]
                        row = shape(map(seen.setdefault, fields, fields))
                        if keep is None or keep(row):
                            rows.append(row)
                    continue
                if line.startswith(DUMP_COMPLETE):
                    complete = True
                elif created := _CREATE.match(line):
                    made.add(created.group(1))
                elif line.startswith("COPY ") and (match := _COPY.match(line.rstrip("\n"))):
                    copying = match.group(1)
                    if copying in wanted:
                        header = [c.strip().strip('"') for c in match.group(2).split(",")]
                        width, kept = len(header), [i for i, name in enumerate(header) if name in columns]
                        shape = type("Row", (Row,), {"__slots__": (), "index": {header[i]: n for n, i in enumerate(kept)}})
                        keep, seen = where.get(copying), {}
                        rows = out.setdefault(copying, [])
        if copying is not None or not complete or ((made & wanted) | set(required)) - set(out):
            raise ScanError(t(DAMAGED))
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
        is larger than `limit`. The name comes from the dump and is untrusted. Raises
        SourceRevisionError when the file does not unpack or is packed with a password."""
        name = self.root + FILESTORE + store_fname
        if ".." in store_fname.split("/") or store_fname.startswith("/"):
            return None
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > limit:
            return None
        # A file that does not unpack refuses the whole backup, not just the file: the zip is damaged.
        with _intact(SourceRevisionError), _open(self.archive, name) as f:
            return f.read()

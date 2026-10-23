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
import math
import re
import sys
import traceback
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import IO

from celerp.importers.adapters.base import ScanError, SourceRevisionError
from celerp.importers.tabular import ZIP_ERRORS, ZipMemberRefused, open_zip_member, zip_directory_fits
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
HELD_TOO_LARGE = "migration.err_odoo_backup_too_large"
# What one backup may make the server unpack and hold. A database's dump packs about ten to one;
# a far higher ratio is a file made to unpack large, not a database. The most text one row keeps
# is an attachment stored in it: up to 25 MB as hex, twice the characters, and the rest of its row.
MAX_DUMP_BYTES = 20 * 1024 ** 3  # ten times the largest upload
MAX_RATIO = 100
MAX_LINE = 64_000_000  # characters of the columns kept from one row
MAX_MANIFEST = 1024 ** 2  # Odoo's is a few kilobytes
ID_DIGITS = 19  # the most a PostgreSQL bigint id writes
# The largest amount or quantity read, as a power of ten: no book holds one larger, and one
# larger cannot be carried to the cent in Decimal's 28 digits.
MAX_MAGNITUDE = 20
# The most fraction digits of a number read. PostgreSQL writes a float in at most 17 significant
# digits, so this reads any down to 1e-31, far below a cent of any amount; a number written with
# more, such as 1e-999999, is not one a database holds, and would be spelled in as many digits.
MAX_SCALE = 48
# What the rows read may take of the server's memory, in bytes, counted as Python holds them:
# each row a tuple of its kept values, each distinct value a string held once in its table, at
# its own size (one, two or four bytes a character, by its widest one, and its header).
MAX_HELD = 1024 ** 3
ROW_BYTES, FIELD_BYTES = 64, 8
# What the read builds from its rows, beyond the rows, is charged to the same budget, so whatever
# the rows are the whole read stays within about two and a half times it. Measured in bytes
# traced per unit of large books: a journal item builds about 1600 (its row is counted at 300 to
# 400), a line's tax about 1070 (its row about 130). An invoice line builds about 2700 and its
# invoice about 4900 (the move row about 600): what a document adds is charged once the read
# knows a move is one, before it is built, so an entry's items are not charged as invoice lines.
# Masters build their record, its bundle entry and the balances it is checked against: a contact
# about 2900, an item about 2100, an account about 2800; a stock row adds its place in an index.
# What rows do not bound is charged where the read builds it: a stock move about 1500 (with its
# inventory adjustment), a holding of a lot or a location about 3200 (its bundle item and expected
# count), and each month end from the first record to the last, every account's balance at about
# 1100 each.
BUILT_BYTES = {"account_move_line": 450, "account_move": 700, "account_move_line_account_tax_rel": 400,
               "account_account": 1000, "res_partner": 1000, "product_product": 500, "ir_attachment": 250,
               "stock_move": 100, "stock_move_line": 100, "stock_valuation_layer": 100}
DOCUMENT_BYTES, DOCUMENT_LINE_BYTES = 1000, 500
STOCK_MOVE_BYTES, HOLDING_BYTES, MONTH_ACCOUNT_BYTES = 700, 1200, 600
# A string the read builds per record out of values the dump holds once (a name told apart by its
# id, a key, an address, a label) is a new copy, so a long value repeated
# across many records is held many times. Each is charged at its size as Python holds it,
# TEXT_COPIES times: the shared exports code copies it again (a holding key into its inventory
# label and opening adjustment, a document key into its fallback journal, a label per month end).
TEXT_COPIES = 2
# Each id of an analytic distribution's key is split out to be looked up: a string of its own,
# its place in a list and in the set of those seen, about this many bytes.
KEY_ID_BYTES = 120
# Reading a line holds it a few times over while it is split (the decoded text, the line, its
# fields): the longest line read is charged LINE_COPIES times its size, once.
LINE_COPIES = 4
# Decoding JSON takes about two kilobytes whatever it decodes, and builds an object for each value,
# however short its text: a number or an empty list two characters long is a hundred bytes or more.
JSON_DECODER_BYTES, JSON_VALUE_BYTES = 4096, 128
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
_COLUMN = re.compile(r'^\s+"?(\w+)"? (\w+)')  # a column as CREATE TABLE declares it: its name and type
_COPY = re.compile(r'^COPY (?:"?public"?\.)?"?(\w+)"? \((.*)\) FROM stdin;$')
_ESCAPE = re.compile(r"\\(?:([0-7]{1,3})|x([0-9a-fA-F]{1,2})|(.))")
_ESCAPED = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _id(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) <= ID_DIGITS


_PLAIN = re.compile(rf"-?\d{{1,{MAX_MAGNITUDE}}}(?:\.\d{{1,{MAX_SCALE}}})?").fullmatch  # how most numbers are written


def _number(value: str) -> bool:
    """A finite number below MAX_MAGNITUDE with at most MAX_SCALE fraction digits, as PostgreSQL
    writes a numeric or a float."""
    if _PLAIN(value):
        return True
    try:
        number = Decimal(value)
    except ArithmeticError:
        return False
    return number.is_finite() and number.adjusted() < MAX_MAGNITUDE and number.as_tuple().exponent >= -MAX_SCALE


# The forms PostgreSQL writes a date and a timestamp in. datetime.fromisoformat also reads ISO week
# dates and forms without separators, which a database never writes.
_DATE_FORM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)?").fullmatch


def _date(value: str) -> bool:
    """A real date, or date and time, in a form PostgreSQL writes."""
    if not _DATE_FORM(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _json_number(text: str) -> Decimal:
    if not _number(text):
        raise ValueError("a number no database column holds")
    return Decimal(text)


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number")


def json_value(text: str):
    """JSON as PostgreSQL holds it, every number a finite Decimal of the number kind. Python's
    json reads NaN, Infinity and 1e999 (as an infinite float), which PostgreSQL never writes:
    each raises ValueError, as does text that is not JSON."""
    return json.loads(text, parse_float=_json_number, parse_int=_json_number, parse_constant=_no_constant)


def json_size(text: str) -> int:
    """At most what decoding `text` (json_value) holds at once: the decoder's own, twice the text,
    for the strings copied out of it, at four bytes a character when it holds an escape (one
    escaped emoji makes the whole decoded string that wide), and JSON_VALUE_BYTES for each value
    it can hold, one more than its commas, colons and opening brackets."""
    return (JSON_DECODER_BYTES + 2 * text_size([text], width=4 if "\\u" in text else 1)
            + JSON_VALUE_BYTES * (1 + sum(map(text.count, ",:[{"))))


def translation(value: str) -> str | None:
    """The English name in a translated value, JSON keyed by language as Odoo keeps a translated
    name from 16 on, else the first one stored; None when the value is not a non-empty object
    whose every name is a string."""
    try:
        names = json_value(value)
    except (ValueError, RecursionError):
        return None
    if not isinstance(names, dict) or not names or not all(isinstance(name, str) for name in names.values()):
        return None
    return names.get(LANGUAGE) or next(iter(names.values()))


def _json(value: str) -> bool:
    try:
        json_value(value)
    except (ValueError, RecursionError):
        return False
    return True


_HEX = re.compile(r"[0-9a-fA-F]*").fullmatch


def _bytes(value: str) -> bool:
    """A file's content as PostgreSQL writes it: hex after \\x, two characters a byte."""
    return value.startswith("\\x") and len(value) % 2 == 0 and _HEX(value, 2) is not None


# What each kind of column holds when the database wrote it.
KINDS: dict[str, Callable[[str], bool]] = {"id": _id, "number": _number, "date": _date, "bytes": _bytes,
                                          "json": _json}
# The kind of a column holding a name Odoo translates: from 16 a jsonb column (see translation),
# before that, and in a table where Odoo does not translate it, plain text.
TRANSLATED = "translated"


_ASCII_HEADER = sys.getsizeof("")
_HEADER = sys.getsizeof("\u0100") - 4  # of a string with a character outside ASCII: less it and its end


def _width(text: str) -> int:
    """The bytes each character of `text` takes as Python holds it: one, two or four, by its widest."""
    return 1 if text.isascii() else min(4, (sys.getsizeof(text) - _HEADER) // (len(text) + 1))


def text_size(parts: Sequence[str], sep: str = "", width: int = 1) -> int:
    """The size, as Python holds it, of `parts` joined by `sep`: one, two or four bytes a character
    by the widest of theirs (at least `width`), and its header."""
    length = sum(map(len, parts)) + len(sep) * max(len(parts) - 1, 0)
    if width == 1 and sep.isascii() and all(map(str.isascii, parts)):
        return _ASCII_HEADER + length
    return _HEADER + (length + 1) * max(width, _width(sep), *map(_width, parts))


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
    """The Odoo major version the manifest names, as text ("17.0"); None when it names none."""
    version = manifest.get("major_version") or manifest.get("version")
    found = re.search(r"(\d+)\.", version) if isinstance(version, str) else None
    return int(found.group(1)) if found else None


@contextmanager
def _intact(error: type[Exception] = ScanError) -> Iterator[None]:
    """Reading the zip: a file in it that does not unpack (ZIP_ERRORS), or a dump that is not
    UTF-8 text (every Odoo database is), is refused with `error`, and so is running out of memory."""
    try:
        yield
    except UnicodeDecodeError as exc:
        raise error(t(DAMAGED)) from exc
    except ZIP_ERRORS as exc:
        raise error(t(UNREADABLE)) from exc
    except MemoryError as exc:
        freed(exc)
    else:
        return
    raise too_large(error)


def freed(exc: BaseException) -> None:
    """Free what the steps that failed held (running out of memory, or on a damaged value), so
    the refusal (which may read its language's messages) is built with that memory back. The caller raises the refusal once
    out of its `except` block, so the refusal does not carry the failure along with it."""
    traceback.clear_frames(exc.__traceback__)


def too_large(error: type[Exception] = ScanError) -> Exception:
    """The refusal of a backup whose rows need more memory than MAX_HELD, or than the server has."""
    return error(t(HELD_TOO_LARGE))


def too_long() -> ScanError:
    """The refusal of a backup with a row keeping more than MAX_LINE characters."""
    return ScanError(t("migration.err_odoo_record_too_large", limit=f"{MAX_LINE / 10 ** 6:g}"))


def _long_row(first: str, lines: IO[str], limits: Mapping[int, float]) -> tuple[list[str], bool, bool]:
    """A row longer than MAX_LINE, read in pieces from its first: its fields, one not in `limits`
    (kept column index -> its cap in characters, inf when uncapped) as empty and one longer than
    its cap as NULL; whether the fields kept add up to more than MAX_LINE characters (then the
    fields after are empty too), and whether the line ends."""
    fields: list[str] = []
    text: list[str] = []
    length = kept = 0
    over = ended = False

    def close() -> None:
        nonlocal kept
        limit = limits.get(len(fields))
        held = limit is not None and length <= limit
        fields.append("" if limit is None or over else "".join(text) if held else "\\N")
        kept += length if held else 0

    piece = first
    while piece and not ended:
        ended = piece.endswith("\n")
        for j, part in enumerate(piece.removesuffix("\n").split("\t")):
            if j:
                close()
                text, length = [], 0
            length += len(part)
            limit = limits.get(len(fields))
            if limit is None or over or length > limit:
                text = []
            else:
                text.append(part)
                if kept + length > MAX_LINE:
                    over, text = True, []
        piece = "" if ended else lines.readline(MAX_LINE)
    close()
    return fields, over, ended


def _open(archive: zipfile.ZipFile, name: str) -> IO[bytes]:
    """One file of the zip, open for reading. Raises SourceRevisionError when it is packed with
    a password, which a backup must be made again without, or with a method Odoo does not zip
    with (see ZIP_METHODS)."""
    try:
        return open_zip_member(archive, name)
    except ZipMemberRefused as exc:
        raise SourceRevisionError(t(ENCRYPTED if exc.password else UNREADABLE)) from exc


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
    SourceRevisionError when they are one without its dump, with a dump that does not start as
    a plain SQL dump (damaged, as a broken stream can unpack to wrong bytes before its checksum
    is reached, or pg_dump custom format), or with a manifest or dump that would unpack beyond
    what the server reads; the sizes are the ones the zip declares, which unpacking never goes past."""
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
    if DUMP_HEADER not in head:
        _refuse_custom_format([partial(_open, archive, info.filename)])
        raise SourceRevisionError(t(UNREADABLE))
    return manifest


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
            with open(path, "rb") as file:
                fits = zip_directory_fits(file)
            if not fits:
                raise SourceRevisionError(t(TOO_LARGE))
            self.archive = zipfile.ZipFile(path)
        except OSError as exc:
            raise ScanError(t("migration.err_odoo_not_backup")) from exc
        except ZIP_ERRORS as exc:
            _refuse_custom_format([partial(open, path, "rb")])
            if _cut_short(path):
                raise SourceRevisionError(t(UNREADABLE)) from exc
            raise ScanError(t("migration.err_odoo_not_backup")) from exc
        except MemoryError as exc:
            freed(exc)
            fits = False
        if not fits:
            raise too_large(SourceRevisionError)
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
            raise SourceRevisionError(t(DAMAGED))
        self.modules = frozenset(modules)
        self.held = self.longest = 0

    def hold(self, size: int) -> None:
        """Counts `size` bytes more held from this backup, and refuses it past MAX_HELD."""
        self.held += size
        if self.held > MAX_HELD:
            raise too_large()

    def fits(self, size: int) -> None:
        """Refuses the backup when `size` bytes more, held only for a moment, would take what it
        holds past MAX_HELD."""
        if self.held + size > MAX_HELD:
            raise too_large()

    def decodable(self, text: str) -> str:
        """`text`, JSON, once decoding it (json_size) fits the budget (fits)."""
        self.fits(json_size(text))
        return text

    def hold_text(self, *parts: str, sep: str = "") -> None:
        """Counts a string built per record from values the backup holds, `parts` joined by `sep`,
        before it is built, so building it never takes more than the budget allows."""
        self.hold(TEXT_COPIES * text_size(parts, sep))

    def held_text(self, *parts: str, sep: str = "") -> str:
        """`parts` joined by `sep`, a string built per record from values the backup holds,
        charged as held before it is built."""
        self.hold_text(*parts, sep=sep)
        return sep.join(parts)

    def __enter__(self) -> Backup:
        return self

    def __exit__(self, *exc) -> None:
        self.archive.close()

    @property
    def lot_table(self) -> str:
        return "stock_lot" if self.version >= 16 else "stock_production_lot"

    def tables(self, names: Iterable[str], columns: frozenset[str],
               where: Mapping[str, Callable[[Row], bool]] | None = None,
               required: Iterable[str] = (), capped: Mapping[str, int] | None = None,
               may_leave_out: Iterable[str] = (), kinds: Mapping[str, str] | None = None) -> dict[str, list[Row]]:
        """The rows of each named table the dump holds, in dump order, with only the named
        columns, and of a table in `where` only the rows its test keeps. A table the dump does
        not hold (its module is not installed) is absent. A value of a `capped` column longer
        than its cap, in characters, is read as NULL. The dump is streamed and a value repeated
        within a table is held once, and a line longer than MAX_LINE is never held whole: it is
        read in pieces, keeping only the wanted rows' kept columns. A row of a `may_leave_out`
        table keeping more than MAX_LINE characters is left out and its leading field (its id)
        listed in `self.left_out` under its table. Raises ScanError, before any row is used,
        when a row of another wanted table, or one whose leading field is not an id, keeps that
        much, when the rows kept, with what is built from them (BUILT_BYTES), would take more
        than MAX_HELD of memory, or when the dump is cut short or damaged: it does not unpack,
        has no closing line, a table's data never ends or never starts, a wanted table's data comes
        before its CREATE TABLE (which declares the columns holding translated names), a row has
        more or fewer fields than its table, a kept column of a kind in `kinds` (column -> a key of KINDS) holds
        a value not of that kind, or a `required` table (one an installed module makes) is missing.
        A kept column of the TRANSLATED kind that the dump declares jsonb is read as its English
        name (the first stored when it has none): one that is not a non-empty JSON object of
        strings is damaged. A value of the json kind, or a translated one, is decoded only when
        decoding it fits the budget (decodable), and a translated one is charged only for the name kept."""
        wanted, where, capped, may_leave_out = set(names) | set(required), where or {}, capped or {}, set(may_leave_out)
        kinds = kinds or {}
        self.left_out: dict[str, list[str]] = {}
        out: dict[str, list[Row]] = {}
        made: set[str] = set()
        jsonb: dict[str, set[str]] = {}            # wanted table -> its kept columns declared jsonb
        declaring: str | None = None
        copying: str | None = None
        complete = False
        with _intact(), _open(self.archive, self.root + DUMP) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            rows: list[Row] | None = None
            for line in iter(partial(lines.readline, MAX_LINE + 1), ""):
                if (size := sys.getsizeof(line)) > self.longest:
                    self.hold(LINE_COPIES * (size - self.longest))
                    self.longest = size
                if len(line) > MAX_LINE and not line.endswith("\n"):
                    if rows is None:
                        while (rest := lines.readline(MAX_LINE)) and not rest.endswith("\n"):
                            pass
                        continue
                    values, over, ended = _long_row(line, lines, dict(zip(kept, limits)))
                    if len(values) != width or not ended:
                        raise ScanError(t(DAMAGED))
                    if over:
                        if copying not in may_leave_out or not _id(values[0]):
                            raise too_long()
                        self.left_out.setdefault(copying, []).append(values[0])
                        continue
                    line = "\t".join(values) + "\n"
                if copying is not None:
                    if line == "\\.\n":
                        copying = rows = None
                    elif rows is not None:
                        values = line.rstrip("\n").split("\t")
                        if len(values) != width or not line.endswith("\n"):
                            raise ScanError(t(DAMAGED))
                        fields = [_field(values[i]) if len(values[i]) <= limit else None for i, limit in zip(kept, limits)]
                        for n in decoded:
                            if fields[n] is not None:
                                self.decodable(fields[n])
                        for n, valid in checked:
                            if fields[n] is not None and not valid(fields[n]):
                                raise ScanError(t(DAMAGED))
                        for n in translated:
                            if fields[n] is not None:
                                if (name := translation(fields[n])) is None:
                                    raise ScanError(t(DAMAGED))
                                fields[n] = name
                        if keep is not None and not keep(shape(fields)):
                            continue
                        self.hold(row_bytes + sum(map(sys.getsizeof, {f for f in fields if f is not None and f not in seen})))
                        rows.append(shape(map(seen.setdefault, fields, fields)))
                    continue
                if declaring is not None:
                    if line.startswith(")"):
                        declaring = None
                    elif (column := _COLUMN.match(line)) and column.group(2) == "jsonb" and column.group(1) in columns:
                        jsonb.setdefault(declaring, set()).add(column.group(1))
                    continue
                if line.startswith(DUMP_COMPLETE):
                    complete = True
                elif created := _CREATE.match(line):
                    made.add(created.group(1))
                    declaring = created.group(1) if created.group(1) in wanted else None
                elif line.startswith("COPY ") and (match := _COPY.match(line.rstrip("\n"))):
                    copying = match.group(1)
                    if copying in wanted and copying not in made:
                        raise ScanError(t(DAMAGED))
                    if copying in wanted:
                        header = [c.strip().strip('"') for c in match.group(2).split(",")]
                        width, kept = len(header), [i for i, name in enumerate(header) if name in columns]
                        checked = [(n, KINDS[kinds[header[i]]]) for n, i in enumerate(kept) if kinds.get(header[i]) in KINDS]
                        translated = [n for n, i in enumerate(kept)
                                      if kinds.get(header[i]) == TRANSLATED and header[i] in jsonb.get(copying, ())]
                        decoded = [n for n, i in enumerate(kept) if kinds.get(header[i]) == "json"] + translated
                        limits = [capped.get(header[i], math.inf) for i in kept]
                        shape = type("Row", (Row,), {"__slots__": (), "index": {header[i]: n for n, i in enumerate(kept)}})
                        keep, seen = where.get(copying), {}
                        row_bytes = ROW_BYTES + FIELD_BYTES * len(kept) + BUILT_BYTES.get(copying, 0)
                        rows = out.setdefault(copying, [])
        if copying is not None or not complete or ((made & wanted) | set(required)) - set(out):
            raise ScanError(t(DAMAGED))
        return out

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

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read a source's export set: the files a user downloads from its export menus.

Each upload is a comma or tab separated text file, a workbook, or a plain zip
archive of them. Every file is matched to one of the source's layouts by its
header row and, for a report, the title printed above it; never by its name. A
set that is missing a required file, holds a file no layout matches, or holds a
file twice is refused with a message naming each file.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from celerp.importers import tabular
from celerp.importers.adapters.base import ArtifactSet, ScanError
from ui.i18n import t

# A ledger export runs to several hundred thousand rows; the set as a whole is bounded
# by the scan store's upload limit.
LIMITS = tabular.Limits(rows=2_000_000, cells=40_000_000, file_bytes=512 * 1024 * 1024,
                        uncompressed=1024 * 1024 * 1024)
MAX_MEMBERS = 200
HEADER_SEARCH = 20
ZERO = Decimal(0)


@dataclass(frozen=True)
class Layout:
    """One kind of file in a source's export set."""
    kind: str
    label: str                                 # the name the source's export menu gives it
    columns: frozenset[str]                    # normalized header cells the file must carry
    title: str | None = None                   # a report's title, printed above its header
    many: bool = False                         # the source writes it in batches or per year


@dataclass
class Table:
    name: str                                  # the uploaded file, or archive and member
    layout: Layout
    above: list[list[str]]                     # the report lines above the header
    header: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)

    def value(self, row: dict[str, str], column: str) -> str:
        return row.get(column, "").strip()


def normalize(cell: str) -> str:
    return " ".join(cell.strip().lstrip("*").lower().split())


def _text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ScanError(t("migration.err_export_unreadable", name="?"))


def _lines(name: str, data: bytes) -> list[list[str]]:
    try:
        if data[:2] == b"PK":
            return tabular.workbook_lines(data, LIMITS)
        text = _text(data)
        first = text.split("\n", 1)[0]
        return tabular.delimited_lines(text, "\t" if first.count("\t") > first.count(",") else ",", LIMITS)
    except tabular.TabularError as exc:
        raise ScanError(t("migration.err_export_unreadable_because", name=name, reason=str(exc))) from exc


def _is_workbook(archive: zipfile.ZipFile) -> bool:
    return "[Content_Types].xml" in archive.namelist()


def _members(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """The files an upload holds: itself, or each file of a plain zip archive."""
    if data[:2] != b"PK":
        return [(name, data)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScanError(t("migration.err_export_unreadable", name=name)) from exc
    if _is_workbook(archive):
        return [(name, data)]
    infos = [i for i in archive.infolist() if not i.is_dir() and not i.filename.split("/")[-1].startswith(".")
             and "__MACOSX" not in i.filename]
    if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > LIMITS.uncompressed:
        raise ScanError(t("migration.err_export_archive_too_large", name=name))
    return [(f"{name}: {i.filename.split('/')[-1]}", archive.read(i)) for i in infos]


def _match(lines: list[list[str]], layouts: tuple[Layout, ...]) -> tuple[Layout, int] | None:
    """The layout a file's lines carry and the index of its header line. A report layout
    whose title is printed above its header wins over one matched on columns alone."""
    found: list[tuple[int, int, Layout, int]] = []
    for index, line in enumerate(lines[:HEADER_SEARCH]):
        cells = {normalize(c) for c in line if c.strip()}
        titles = {normalize(c) for above in lines[:index] for c in above if c.strip()}
        for layout in layouts:
            if not layout.columns <= cells:
                continue
            if layout.title and normalize(layout.title) not in titles:
                continue
            found.append((bool(layout.title), len(layout.columns), layout, index))
    if not found:
        return None
    _, _, layout, index = max(found, key=lambda f: (f[0], f[1], -f[3]))
    return layout, index


def read_set(artifacts: ArtifactSet, layouts: tuple[Layout, ...]) -> tuple[list[Table], list[str]]:
    """Every file of the set matched to its layout, and the names of files no layout matches."""
    tables: list[Table] = []
    unknown: list[str] = []
    for artifact in artifacts:
        for name, data in _members(artifact.original_name, artifact.path.read_bytes()):
            lines = _lines(name, data)
            matched = _match(lines, layouts)
            if matched is None:
                unknown.append(name)
                continue
            layout, index = matched
            header = [normalize(c) for c in lines[index]]
            table = Table(name, layout, lines[:index], header)
            for line in lines[index + 1:]:
                if any(c.strip() for c in line):
                    table.rows.append({h: (line[i] if i < len(line) else "") for i, h in enumerate(header) if h})
            tables.append(table)
    return tables, unknown


def refuse_incomplete(tables: list[Table], unknown: list[str], required: list[Layout]) -> None:
    """Refuse a set missing a required file or holding an unrecognised one or a duplicate,
    naming what was recognised so the user sees what to add or take out."""
    found = ", ".join(f"{tb.name} ({tb.layout.label})" for tb in tables) or "--"
    problems: list[str] = []
    missing = [layout.label for layout in required if not any(tb.layout.kind == layout.kind for tb in tables)]
    if missing:
        problems.append(t("migration.err_export_missing", files=", ".join(missing)))
    if unknown:
        problems.append(t("migration.err_export_unrecognised", files=", ".join(unknown)))
    seen: dict[str, str] = {}
    for tb in tables:
        if tb.layout.many:
            continue
        if tb.layout.kind in seen:
            problems.append(t("migration.err_export_twice", label=tb.layout.label,
                              files=f"{seen[tb.layout.kind]}, {tb.name}"))
        seen[tb.layout.kind] = tb.name
    if problems:
        raise ScanError(" ".join([*problems, t("migration.err_export_recognised", files=found)]))


# ── Cell values ───────────────────────────────────────────────────────────────

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$")
_SLASHED = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def amount(value: str, where: str = "") -> Decimal:
    """A number as the source prints it: thousands separators, a leading currency sign and
    a negative in brackets are accepted; an empty cell is zero."""
    text = value.strip().replace(",", "")
    if not text:
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").lstrip("$£€¥").strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ScanError(t("migration.err_export_number", value=value, where=where)) from exc
    if not number.is_finite():
        raise ScanError(t("migration.err_export_number", value=value, where=where))
    return -number if negative else number


def money(value: str, where: str = "") -> Decimal:
    """An amount at cents: a workbook stores money as a binary fraction."""
    return amount(value, where).quantize(Decimal("0.01"))


class DateReader:
    """Reads the set's slashed dates in one order, day first or month first, decided from
    every date the set prints: a part above 12 can only be a day."""

    def __init__(self, samples: list[str]) -> None:
        day_first = month_first = False
        for text in samples:
            m = _SLASHED.match(text.strip())
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                day_first |= a > 12
                month_first |= b > 12
        if day_first and month_first:
            raise ScanError(t("migration.err_export_dates_mixed"))
        slashed = any(_SLASHED.match(s.strip()) for s in samples)
        if slashed and not (day_first or month_first):
            raise ScanError(t("migration.err_export_dates_ambiguous"))
        self.day_first = day_first

    def __call__(self, value: str, where: str = "") -> date:
        text = value.strip()
        m = _ISO.match(text)
        try:
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            m = _SLASHED.match(text)
            if m:
                a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return date(year, b, a) if self.day_first else date(year, a, b)
            return report_date(text)
        except (ValueError, ScanError) as exc:
            raise ScanError(t("migration.err_export_date", value=value, where=where)) from exc


def report_date(text: str) -> date:
    """A date as a report heading prints it: 31 Dec 2025 or 31 December 2025."""
    text = text.strip().removeprefix("As at").removeprefix("as at").strip()
    for pattern in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    m = _ISO.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise ScanError(t("migration.err_export_date", value=text, where=""))

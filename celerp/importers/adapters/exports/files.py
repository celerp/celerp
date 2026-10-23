# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read a source's export set: the files a user downloads from its export menus.

Each upload is a comma or tab separated text file, a workbook, or a plain zip
archive of them. Every file is matched to one of the source's layouts by its
header row and, for a report, the title printed above it; never by its name. The
set is checked against the source's checklist: each file is received, missing,
optional, not needed, or pending until the ledger shows whether it is needed. A set
that is missing a required file, holds a file no layout matches or a file twice, a file
whose values cannot each be told by their heading (a column headed twice, a value under
none), or a report exported a period at a time whose periods overlap or leave a gap, is
refused with a message naming each file and what to export instead.
"""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from celerp.importers import tabular
from celerp.importers.adapters.base import CONVERSION_ERRORS, ArtifactSet, ScanError
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
    menu: str = ""                             # where the source's menus export it, in the source's words
    range_key: str = ""                        # the dates to choose when exporting it; a list has none
    joined: bool = False                       # exported a period at a time: the periods must join
    optional: bool = False                     # carried when present, never required
    absent: frozenset[str] = frozenset()       # columns another layout carries and this one never does


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


def _text(name: str, data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ScanError(t("migration.err_export_unreadable", name=name))


def _lines(name: str, data: bytes) -> list[list[str]]:
    try:
        if data[:2] == b"PK":
            return tabular.workbook_lines(data, LIMITS)
        text = _text(name, data)
        first = text.split("\n", 1)[0]
        return tabular.delimited_lines(text, "\t" if first.count("\t") > first.count(",") else ",", LIMITS)
    except tabular.TabularError as exc:
        raise ScanError(t("migration.err_export_unreadable_because", name=name, reason=str(exc))) from exc
    except (csv.Error, SyntaxError, *CONVERSION_ERRORS) as exc:
        # A cell past the parser's size limit, a workbook part that is not well-formed XML
        # (ElementTree's ParseError is a SyntaxError), or a value its parser cannot hold.
        raise ScanError(t("migration.err_export_unreadable", name=name)) from exc


def _is_workbook(archive: zipfile.ZipFile) -> bool:
    return "[Content_Types].xml" in archive.namelist()


def _members(name: str, path: Path) -> list[tuple[str, bytes]]:
    """The files an upload holds: itself, or each file of a plain zip archive. A zip's directory
    is checked before it is read, and only its files are read, never the archive whole."""
    with open(path, "rb") as file:
        if file.read(2) != b"PK":
            return [(name, path.read_bytes())]
        if not tabular.zip_directory_fits(file):
            raise ScanError(t("migration.err_export_archive_too_large", name=name))
        try:
            with zipfile.ZipFile(file) as archive:
                if _is_workbook(archive):
                    return [(name, path.read_bytes())]
                infos = [i for i in archive.infolist() if not i.is_dir()
                         and not i.filename.split("/")[-1].startswith(".") and "__MACOSX" not in i.filename]
                if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > LIMITS.uncompressed:
                    raise ScanError(t("migration.err_export_archive_too_large", name=name))
                return [(f"{name}: {i.filename.split('/')[-1]}", tabular.read_zip_member(archive, i)) for i in infos]
        except tabular.ZIP_ERRORS as exc:
            raise ScanError(t("migration.err_export_unreadable", name=name)) from exc


def heading(cell: str, aliases: dict[str, str]) -> str:
    """A header cell as the layouts name it: normalized, under its alias where another version
    of the source heads it differently. Only header cells are read so; data never is."""
    name = normalize(cell)
    return aliases.get(name, name)


def _match(lines: list[list[str]], layouts: tuple[Layout, ...],
           aliases: dict[str, str]) -> tuple[Layout, int] | None:
    """The layout a file's lines carry and the index of its header line. A report layout
    whose title is printed above its header wins over one matched on columns alone."""
    found: list[tuple[int, int, Layout, int]] = []
    for index, line in enumerate(lines[:HEADER_SEARCH]):
        cells = {heading(c, aliases) for c in line if c.strip()}
        titles = {normalize(c) for above in lines[:index] for c in above if c.strip()}
        for layout in layouts:
            if not layout.columns <= cells or layout.absent & cells:
                continue
            if layout.title and normalize(layout.title) not in titles:
                continue
            found.append((bool(layout.title), len(layout.columns), layout, index))
    if not found:
        return None
    _, _, layout, index = max(found, key=lambda f: (f[0], f[1], -f[3]))
    return layout, index


@dataclass(frozen=True)
class Stranger:
    """An uploaded file no layout matches, with its lines, or why it could not be read."""
    name: str
    lines: list[list[str]]
    reason: str = ""
    aliases: dict[str, str] = field(default_factory=dict)   # the source's other names for its columns


def read_set(artifacts: ArtifactSet, layouts: tuple[Layout, ...],
             aliases: dict[str, str] | None = None) -> tuple[list[Table], list[Stranger]]:
    """Every file of the set matched to its layout, and the files no layout matches. `aliases`
    names a column by the name its layouts use where another version of the source heads it
    differently (normalized names)."""
    tables: list[Table] = []
    strangers: list[Stranger] = []
    for artifact in artifacts:
        try:
            if artifact.size_bytes > LIMITS.file_bytes:
                raise ScanError(t("migration.err_file_too_large"))
            members = _members(artifact.original_name, artifact.path)
        except ScanError as exc:
            strangers.append(Stranger(artifact.original_name, [], str(exc)))
            continue
        for name, data in members:
            try:
                lines = _lines(name, data)
            except ScanError as exc:
                strangers.append(Stranger(name, [], str(exc)))
                continue
            matched = _match(lines, layouts, aliases or {})
            if matched is None:
                strangers.append(Stranger(name, lines[:HEADER_SEARCH], aliases=aliases or {}))
                continue
            layout, index = matched
            header = [heading(c, aliases or {}) for c in lines[index]]
            try:
                tables.append(_table(name, layout, lines, index, header))
            except ScanError as exc:
                strangers.append(Stranger(name, [], str(exc)))
    return tables, strangers


def _table(name: str, layout: Layout, lines: list[list[str]], index: int, header: list[str]) -> Table:
    """The file's rows keyed by its header. Every value must sit under a heading of its own, so
    none is lost or read as another: a column headed twice, or a value under no heading, refuses
    the file. A report's first column may have no heading; it is read under ""."""
    seen: set[str] = set()
    for column, h in enumerate(header):
        if h and h in seen:
            raise ScanError(t("migration.err_export_column_twice", name=name, column=lines[index][column].strip()))
        seen.add(h)
    table = Table(name, layout, lines[:index], header)
    for line in lines[index + 1:]:
        if not any(c.strip() for c in line):
            continue
        stray = next((c.strip() for column, c in enumerate(line)
                      if column and c.strip() and (column >= len(header) or not header[column])), "")
        if stray:
            raise ScanError(t("migration.err_export_value_unheaded", name=name, value=stray))
        table.rows.append({h: (line[i] if i < len(line) else "") for i, h in enumerate(header) if h or i == 0})
    return table


def of(tables: list[Table], layout: Layout) -> list[Table]:
    return [tb for tb in tables if tb.layout.kind == layout.kind]


# ── The checklist ────────────────────────────────────────────────────────────

RECEIVED, MISSING, PENDING, OPTIONAL, NOT_NEEDED = "received", "missing", "pending", "optional", "not_needed"


@dataclass(frozen=True)
class Item:
    """One file of the checklist: what the upload so far holds of it."""
    layout: Layout
    status: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class SetCheck:
    """An upload checked against its source's checklist: every file the source can export,
    and every problem that keeps the set from being read."""
    source: str
    warning: str
    items: tuple[Item, ...]
    problems: tuple[str, ...]
    recognised: str

    @property
    def complete(self) -> bool:
        return not self.problems

    def view(self) -> dict:
        return {
            "source": self.source, "warning": self.warning, "complete": self.complete,
            "problems": list(self.problems),
            "items": [{"kind": i.layout.kind, "label": i.layout.label, "menu": i.layout.menu,
                       "range": t(i.layout.range_key) if i.layout.range_key else "--",
                       "status": i.status, "status_label": t(f"migration.export_status_{i.status}"),
                       "files": list(i.files)} for i in self.items],
        }

    def refuse(self) -> None:
        """Refuse the set while anything is wrong, naming what was recognised so the user
        sees what to add or take out."""
        if self.problems:
            raise ScanError(" ".join([*self.problems, t("migration.err_export_recognised", files=self.recognised)]))


def check_set(tables: list[Table], strangers: list[Stranger], layouts: tuple[Layout, ...], required: list[Layout],
              *, decider: Layout, source: str, warning_key: str) -> SetCheck:
    """The checklist for an upload. A file the source needs only when the ledger posts to
    it is pending until the *decider* arrives."""
    have = {tb.layout.kind for tb in tables}
    # A file named for a layout that could not be read is reported once, as unreadable.
    unreadable = {_letters(s.name.split(": ")[-1].rsplit(".", 1)[0]) for s in strangers if s.reason}
    items: list[Item] = []
    problems: list[str] = []
    for layout in layouts:
        names = tuple(tb.name for tb in of(tables, layout))
        if names:
            status = RECEIVED
        elif layout in required:
            status = MISSING
            if _letters(layout.label) not in unreadable:
                problems.append(t("migration.err_export_missing_from", label=layout.label, menu=layout.menu))
        elif layout.optional:
            status = OPTIONAL
        else:
            status = NOT_NEEDED if decider.kind in have else PENDING
        items.append(Item(layout, status, names))
    company = company_name(tables)
    problems += [_stranger_problem(s, layouts, source, company) for s in strangers]
    problems += _duplicates(tables)
    problems += _joins(tables, layouts)
    recognised = ", ".join(f"{tb.name} ({tb.layout.label})" for tb in tables) or "--"
    return SetCheck(source, t(warning_key), tuple(items), tuple(problems), recognised)


def _letters(name: str) -> str:
    """A file or layout name reduced to its letters: "Trial Balance 2024" and
    "TrialBalance" read the same."""
    return "".join(c for c in name.lower() if c.isalpha())


def _stranger_problem(stranger: Stranger, layouts: tuple[Layout, ...], source: str, company: str) -> str:
    """Why a file is not part of the set: unreadable, a listed file short of columns, a
    report the set does not use, or a file of something else entirely."""
    if stranger.reason:
        return stranger.reason
    best: tuple[int, Layout, frozenset[str]] | None = None
    for index, line in enumerate(stranger.lines):
        cells = {heading(c, stranger.aliases) for c in line if c.strip()}
        titles = {normalize(c) for above in stranger.lines[:index] for c in above if c.strip()}
        for layout in layouts:
            if layout.title and normalize(layout.title) not in titles:
                continue
            found = len(layout.columns & cells)
            if found >= 2 and found * 2 >= len(layout.columns) and (best is None or found > best[0]):
                best = (found, layout, layout.columns - cells)
    if best is not None:
        _, layout, lacking = best
        key = "migration.err_export_lacks_column" if len(lacking) == 1 else "migration.err_export_lacks_columns"
        return t(key, name=stranger.name, label=layout.label, columns=", ".join(sorted(lacking)), menu=layout.menu)
    title = _report_title(stranger.lines, company)
    if title:
        return t("migration.err_export_wrong_report", name=stranger.name, title=title, source=source)
    return t("migration.err_export_unknown_file", name=stranger.name, source=source)


def _report_title(lines: list[list[str]], company: str) -> str:
    """A report's title: the line above its dated heading that is not the company's name."""
    for index, line in enumerate(lines[:HEADER_SEARCH]):
        if any(report_heading(c) for c in line if c.strip()):
            above = [cells[0] for cells in ([c.strip() for c in ln if c.strip()] for ln in lines[:index]) if cells]
            return next((c for c in above if normalize(c) != normalize(company)), "")
    return ""


def _duplicates(tables: list[Table]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for tb in tables:
        if tb.layout.many:
            continue
        if tb.layout.kind in seen:
            problems.append(t("migration.err_export_twice", label=tb.layout.label,
                              files=f"{seen[tb.layout.kind]}, {tb.name}"))
        seen[tb.layout.kind] = tb.name
    return problems


def short_date(on: date) -> str:
    return f"{on.day} {on:%b %Y}"


def _joins(tables: list[Table], layouts: tuple[Layout, ...]) -> list[str]:
    """A report exported a period at a time: each file's period starts the day after the
    one before it ends."""
    problems: list[str] = []
    day = timedelta(days=1)
    for layout in (lt for lt in layouts if lt.joined):
        spans: list[tuple[date, date, str]] = []
        for tb in of(tables, layout):
            try:
                spans.append((*period(tb), tb.name))
            except ScanError as exc:
                problems.append(str(exc))
        spans.sort()
        for (_, end, earlier), (start, later_end, later) in zip(spans, spans[1:]):
            if later_end <= end:
                problems.append(t("migration.err_export_period_inside", later=later, earlier=earlier))
            elif start <= end:
                problems.append(t("migration.err_export_period_overlap", later=later, earlier=earlier,
                                  start=short_date(start), again=short_date(end + day)))
            elif start > end + day:
                problems.append(t("migration.err_export_period_gap", label=layout.label,
                                  start=short_date(end + day), end=short_date(start - day)))
    return problems


def report_heading(text: str) -> bool:
    return normalize(text).startswith(("as at", "as of", "for the", "from ")) or _PERIOD.match(text.strip()) is not None


def company_name(tables: list[Table]) -> str:
    """The company a report names above its header: the line that is neither its title nor its date."""
    for tb in tables:
        if tb.layout.title:
            for line in tb.above:
                cells = [c.strip() for c in line if c.strip()]
                if cells and normalize(cells[0]) != normalize(tb.layout.title) and not report_heading(cells[0]):
                    return cells[0]
    return ""


def as_at(tb: Table) -> date:
    """The date a report states its balances at: As at 31 Dec 2025, or As of."""
    for line in tb.above:
        for cell in line:
            if normalize(cell).startswith(("as at", "as of")):
                return report_date(cell)
    raise ScanError(t("migration.err_export_date", value=tb.layout.title or tb.name, where=tb.name))


def period(tb: Table) -> tuple[date, date]:
    """The period a report covers, from its heading: 1 January 2024 - 30 June 2024."""
    for line in tb.above:
        for cell in line:
            m = _PERIOD.match(cell.strip())
            if m:
                return report_date(m.group(1)), report_date(m.group(2))
    raise ScanError(t("migration.err_export_date", value=tb.layout.title or tb.name, where=tb.name))


# ── Cell values ───────────────────────────────────────────────────────────────

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$")
_SLASHED = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})$")
# A plain number, as a file prints it once its sign, brackets, separators and currency are
# taken off; a workbook writes a tiny fraction with an exponent.
_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$", re.IGNORECASE)
# The currency signs and codes a source prints beside an amount.
CURRENCY_SIGNS = ("$", "£", "€", "¥", "ETB", "Br", "ብር")
# The largest amount read, in digits before the point: no book holds a quadrillion, and a
# longer one is a damaged value that would overflow the money arithmetic.
MAX_DIGITS = 15
# The places past which a printed amount is a workbook's binary fraction, not the source's figure.
NOISE = 9
_PERIOD = re.compile(r"^(\d{1,2} \w+ \d{4}|\w+ \d{1,2}, \d{4}) ?[-\u2013] ?(\d{1,2} \w+ \d{4}|\w+ \d{1,2}, \d{4})$")


def _unsigned(text: str) -> str:
    """`text` without a currency sign or code printed before or after it."""
    for sign in CURRENCY_SIGNS:
        if text.startswith(sign):
            return text[len(sign):].strip()
        if text.endswith(sign):
            return text[:-len(sign)].strip()
    return text


def amount(value: str, where: str = "") -> Decimal:
    """A number as the source prints it: thousands separators, a currency sign or code before
    or after it and a negative in brackets are accepted; an empty cell is zero. Anything else,
    or a number of more than MAX_DIGITS digits, is refused by name."""
    text = value.strip().replace(",", "")
    if not text:
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    sign = text[0] if text[:1] in "-+" else ""
    text = sign + _unsigned(text[len(sign):].strip())
    if not _NUMBER.match(text):
        raise ScanError(t("migration.err_export_number", value=value, where=where))
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ScanError(t("migration.err_export_number", value=value, where=where)) from exc
    if number and number.adjusted() >= MAX_DIGITS:
        raise ScanError(t("migration.err_export_number", value=value, where=where))
    return -number if negative else number


def money(value: str, where: str = "") -> Decimal:
    """An amount exactly as the file prints it, never rounded to a currency: whether it fits its
    currency's minor units is checked once the currency is known (steps.check_precision). A
    workbook stores money as a binary fraction, 0.1 + 0.2 written 0.30000000000000004; no currency
    keeps more than four places, so a value printed past NOISE places is read at NOISE places."""
    number = amount(value, where)
    if number.as_tuple().exponent < -NOISE:
        number = number.quantize(Decimal(1).scaleb(-NOISE)).normalize()
    # Written as places, never as an exponent: 1e3 is 1000.
    return number.quantize(Decimal(1)) if number.as_tuple().exponent > 0 else number


class DateReader:
    """Reads the set's slashed dates in one order, day first or month first, decided from
    every date the set prints: a part above 12 can only be a day. A source that always writes
    one order states it as `day_first`. A two-digit year is read as Python reads one: 69 to 99
    in the 1900s, 00 to 68 in the 2000s."""

    def __init__(self, samples: list[str], day_first: bool | None = None) -> None:
        if day_first is not None:
            self.day_first = day_first
            return
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
                if len(m.group(3)) == 2:
                    year += 1900 if year >= 69 else 2000
                return date(year, b, a) if self.day_first else date(year, a, b)
            return report_date(text)
        except (ValueError, ScanError) as exc:
            raise ScanError(t("migration.err_export_date", value=value, where=where)) from exc


def report_date(text: str) -> date:
    """A date as a report heading prints it: 31 Dec 2025 or 31 December 2025, after As at or As of."""
    text = text.strip()
    if normalize(text).startswith(("as at", "as of")):
        text = text[5:].strip()
    for pattern in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    m = _ISO.match(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise ScanError(t("migration.err_export_date", value=text, where=""))

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Read-only access to a Manager business file.

A current Manager business file is an SQLite database whose `Objects` table
holds one row per business object: its key, the GUID of its content type and
a protobuf payload. This reader trusts nothing about the file: it checks the
SQLite header itself, opens the copy read-only with extension loading
disabled, runs `quick_check`, inspects the schema before reading any table and
issues only static, parameterised queries. The file is never written.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from celerp.importers.adapters.base import ScanError, SourceRevisionError
from celerp.importers.adapters.manager_io.protobuf import DEFAULT_LIMITS, DecodeError, decode
from ui.i18n import t

log = logging.getLogger(__name__)

SQLITE_HEADER = b"SQLite format 3\x00"
LEGACY_HEADER = b"MNGR|"
SCHEMA_TYPE = "a9a71e47-82b3-49db-8aec-898adb460a80"
OBJECT_COLUMNS = ("Key", "ContentType", "Content", "Timestamp")

NOT_MANAGER = "This file is not a Manager business file."
LEGACY_FORMAT = (
    "This file is in an older Manager format that Celerp cannot read. Open it in a recent version "
    "of Manager, save it, and upload the saved file."
)
DAMAGED = "This Manager business file is damaged and cannot be read. Open it in Manager to check it, then upload it again."

# The file format revisions this reader decodes. Manager stamps its format revision in the
# schema object and changes object layouts between revisions without notice, so a file is
# read only at a revision the adapter was built and tested against: the synthetic fixtures
# and the reference books are all written at 419. A revision below the range is refused with
# the upgrade Manager itself performs on open; one above it, or one that cannot be read, is
# refused rather than decoded on the chance its layouts did not change.
SUPPORTED_SCHEMA_MIN = 419
SUPPORTED_SCHEMA_MAX = 419

OLDER_REVISION = (
    "This Manager business file was saved by an older version of Manager (file format {version}). "
    "Open it in the latest version of Manager, which updates the file, then save it and upload the saved file."
)
NEWER_REVISION = "migration.err_newer_revision"
UNKNOWN_REVISION = (
    "Celerp cannot read the file format version of this Manager business file. Open it in the latest "
    "version of Manager, save it, and upload the saved file."
)


@dataclass(frozen=True)
class ObjectRow:
    key: str
    content_type: str
    content: bytes | None                     # None when the payload is larger than the object cap
    size: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sniff(path: Path) -> str | None:
    """The reason a file cannot be a current Manager file, judged from its first bytes, or None."""
    try:
        with path.open("rb") as fh:
            head = fh.read(len(SQLITE_HEADER))
    except OSError:
        return NOT_MANAGER
    if head.startswith(LEGACY_HEADER):
        return LEGACY_FORMAT
    if head != SQLITE_HEADER:
        return NOT_MANAGER
    return None


class ManagerReader:
    """A read-only view of one Manager business file. Use as a context manager."""

    def __init__(self, path: Path, max_object_bytes: int = DEFAULT_LIMITS.max_bytes):
        self.path = Path(path)
        self.max_object_bytes = max_object_bytes
        self._conn: sqlite3.Connection | None = None
        self.schema_version: int | None = None

    def __enter__(self) -> "ManagerReader":
        reason = sniff(self.path)
        if reason:
            raise ScanError(reason)
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ScanError(NOT_MANAGER) from exc
        self._conn = conn
        try:
            if hasattr(conn, "enable_load_extension"):
                conn.enable_load_extension(False)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            self._check_integrity()
            self._check_structure()
            self.schema_version = self._read_schema_version()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ManagerReader is not open.")
        return self._conn

    def _check_integrity(self) -> None:
        try:
            result = self.conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise ScanError(DAMAGED) from exc
        if result != [("ok",)]:
            raise ScanError(DAMAGED)

    def _check_structure(self) -> None:
        try:
            kind = self.conn.execute(
                "SELECT type FROM sqlite_master WHERE name = ?", ("Objects",)
            ).fetchone()
            if kind is None or kind[0] != "table":
                raise ScanError(NOT_MANAGER)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info('Objects')")}
        except sqlite3.DatabaseError as exc:
            raise ScanError(DAMAGED) from exc
        if not set(OBJECT_COLUMNS) <= columns:
            raise ScanError(NOT_MANAGER)

    def _read_schema_version(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT Content FROM Objects WHERE Key = ? AND ContentType = ? AND length(Content) <= 64",
                (SCHEMA_TYPE, SCHEMA_TYPE),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ScanError(DAMAGED) from exc
        if row is None:
            raise ScanError(NOT_MANAGER)
        try:
            version = decode(bytes(row[0] or b"")).int(1, 0)
        except DecodeError as exc:
            raise SourceRevisionError(UNKNOWN_REVISION) from exc
        if not version:
            raise SourceRevisionError(UNKNOWN_REVISION)
        if version < SUPPORTED_SCHEMA_MIN:
            raise SourceRevisionError(OLDER_REVISION.format(version=version))
        if version > SUPPORTED_SCHEMA_MAX:
            raise SourceRevisionError(t(NEWER_REVISION))
        return version

    def has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def counts_by_type(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT ContentType, count(*) FROM Objects GROUP BY ContentType")
        return {str(ctype): int(n) for ctype, n in rows}

    def objects(self) -> Iterator[ObjectRow]:
        """Every object row. A payload over the object cap is not loaded: its content is None."""
        rows = self.conn.execute(
            "SELECT Key, ContentType, CASE WHEN length(Content) <= ? THEN Content END, length(Content) "
            "FROM Objects ORDER BY ContentType, Key",
            (self.max_object_bytes,),
        )
        for key, ctype, content, size in rows:
            yield ObjectRow(
                key=str(key),
                content_type=str(ctype),
                content=None if content is None and size else bytes(content or b""),
                size=int(size or 0),
            )

    def blob(self, key: str, max_bytes: int) -> tuple[bytes | None, int] | None:
        """Legacy in-file attachment content: (content or None when over the cap, size), or None if absent."""
        if not self.has_table("Blobs"):
            return None
        row = self.conn.execute(
            "SELECT CASE WHEN length(Content) <= ? THEN Content END, length(Content) FROM Blobs WHERE Key = ?",
            (max_bytes, key),
        ).fetchone()
        if row is None:
            return None
        content, size = row
        return (None if content is None else bytes(content)), int(size or 0)

    def row_count(self, table: str) -> int:
        """Rows in one of Manager's history tables. Only fixed table names are accepted."""
        if table not in ("Changes", "Emails"):
            raise ValueError(f"Unknown history table {table!r}.")
        if not self.has_table(table):
            return 0
        query = {"Changes": "SELECT count(*) FROM Changes", "Emails": "SELECT count(*) FROM Emails"}[table]
        return int(self.conn.execute(query).fetchone()[0])

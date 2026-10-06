# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Screen Manager attachments. Every name, size, hash and content type in the file is untrusted.

An attachment is accepted only when its name is a plain file name with an
extension of a type Celerp stores, it belongs to a carried record that Celerp can attach files
to (a contact, a document or an item), its content is
stored in the business file within the size cap, the stored hash matches the
content, and the content's signature matches the extension. Anything else is
rejected with a reason and reported; a rejected attachment never blocks the
ledger. A file on a record that cannot hold files (a receipt, payment,
transfer, journal entry or debit note) is reported as not moved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from celerp.importers.adapters.base import ScanError
from celerp.importers.adapters.manager_io.book import AttachmentRef, Book
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
from celerp.importers.schema import CoverageClass
from celerp.services.attachments import accepts_mime
from ui.i18n import t

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_NAME = 255

# Extension -> (declared content type, accepted leading signatures). Text types carry no signature.
FILE_TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", (b"RIFF",)),
    "pdf": ("application/pdf", (b"%PDF-",)),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    "txt": ("text/plain", ()),
}
# Record types an attachment can belong to: the ones that become Celerp contacts,
# documents and items, the entities Celerp attaches files to.
TARGET_TYPES = frozenset({"SalesInvoice", "PurchaseInvoice", "CreditNote", "Customer", "Supplier", "InventoryItem"})
# Carried record types that become ledger entries, which hold no files.
NOT_ATTACHABLE = frozenset({"DebitNote", "Receipt", "Payment", "InterAccountTransfer", "JournalEntry"})
REJECTED = "Attachment (rejected)"
NOT_MOVED = "Attachment (record cannot hold files)"


@dataclass(frozen=True)
class Accepted:
    key: str
    name: str
    content_type: str
    size: int
    sha256: str
    target: str
    target_type: str


@dataclass
class Screened:
    accepted: dict[str, Accepted] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)      # key -> reason


def _extension(name: str) -> str | None:
    """The accepted extension of a plain file name, or None when the name is unsafe or the type is not accepted."""
    if not name or len(name) > MAX_NAME or name in (".", "..") or "/" in name or "\\" in name:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return None
    _, dot, ext = name.rpartition(".")
    ext = ext.lower()
    return ext if dot and ext in FILE_TYPES and accepts_mime(FILE_TYPES[ext][0]) else None


def _signature_matches(ext: str, content: bytes) -> bool:
    _, signatures = FILE_TYPES[ext]
    if not signatures:
        if b"\x00" in content:
            return False
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    if ext == "webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return any(content.startswith(sig) for sig in signatures)


def _check(book: Book, reader: ManagerReader, ref: AttachmentRef) -> tuple[Accepted, bytes]:
    """The accepted attachment and its content. Raises ScanError with the rejection reason."""
    ext = _extension(ref.name)
    if ext is None:
        raise ScanError(t("migration.attachment_bad_name"))
    target_type = book.names.get(ref.target or "")
    if target_type not in TARGET_TYPES or book.is_blocked(ref.target):
        raise ScanError(t("migration.attachment_no_target"))
    if ref.size > MAX_ATTACHMENT_BYTES:
        raise ScanError(t("migration.attachment_too_large"))
    stored = reader.blob(ref.key, MAX_ATTACHMENT_BYTES)
    if stored is None:
        raise ScanError(t("migration.attachment_external"))
    content, size = stored
    if content is None:
        raise ScanError(t("migration.attachment_too_large"))
    digest = hashlib.sha256(content)
    if ref.sha256 is not None and digest.digest() != ref.sha256:
        raise ScanError(t("migration.attachment_damaged"))
    if not _signature_matches(ext, content):
        raise ScanError(t("migration.attachment_type_mismatch"))
    accepted = Accepted(ref.key, ref.name, FILE_TYPES[ext][0], size, digest.hexdigest(), ref.target, target_type)
    return accepted, content


def screen(book: Book, reader: ManagerReader) -> Screened:
    """Check every decoded attachment and record a rejected one in its own coverage row."""
    screened = Screened()
    for key, ref in book.attachments.items():
        if book.is_blocked(key):
            continue
        if book.names.get(ref.target or "") in NOT_ATTACHABLE:
            screened.rejected[key] = "Its record is a payment, transfer or journal entry, which cannot hold files."
            book.accept("Attachment", key, label=NOT_MOVED, klass=CoverageClass.UNSUPPORTED_NONFINANCIAL,
                        note=("Files attached to receipts, payments, transfers, journal entries and debit notes "
                              "are not moved to Celerp. Keep a copy of them from the source."))
            continue
        try:
            screened.accepted[key] = _check(book, reader, ref)[0]
        except ScanError as rejected:
            screened.rejected[key] = str(rejected)
            book.accept("Attachment", key, label=REJECTED, klass=CoverageClass.MAPPED_WITH_LOSS,
                        note="Rejected attachments are listed in the run summary and not imported.")
    return screened


def read_attachment(book: Book, reader: ManagerReader, key: str) -> bytes:
    """The content of one accepted attachment. Raises ScanError for any other key."""
    ref = book.attachments.get(key)
    if ref is None or book.is_blocked(key):
        raise ScanError("No such attachment in this business file.")
    return _check(book, reader, ref)[1]


def drop_uncarried(book: Book, screened: Screened, carried: set[str]) -> None:
    """Reject accepted attachments whose target record this migration does not import."""
    for key, accepted in list(screened.accepted.items()):
        if accepted.target not in carried:
            del screened.accepted[key]
            screened.rejected[key] = "Its target record is not imported by this migration."
            book.accept("Attachment", key, label=REJECTED, klass=CoverageClass.MAPPED_WITH_LOSS,
                        note="Rejected attachments are listed in the run summary and not imported.")

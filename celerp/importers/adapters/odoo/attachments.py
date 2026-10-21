# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Screen the files attached to Odoo records. Every name, size, checksum and content
type in the backup is untrusted.

A file is accepted only when it belongs to a contact, product or document the migration
imports, its name is a plain file name of a type Celerp stores, its content is in the
backup within the size cap, Odoo's checksum matches the content and the content starts
as its type's does. Anything else is left out with a reason and reported; a file left
out never blocks the ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from celerp.importers.adapters.attachment_files import (
    FILE_TYPES,
    MAX_ATTACHMENT_BYTES,
    accepted_extension,
    signature_matches,
)
from celerp.importers.adapters.base import ScanError
from celerp.importers.adapters.odoo.book import AttachmentRef
from celerp.importers.adapters.odoo.dump import Backup
from ui.i18n import t

REJECTED = "Attachment (left out)"
REJECTED_NOTE = "migration.odoo_attachments_rejected_note"
IGNORED = "Attachment (not a business file)"
IGNORED_NOTE = "migration.odoo_attachments_ignored_note"


@dataclass(frozen=True)
class Accepted:
    key: str
    name: str
    content_type: str
    size: int
    sha256: str
    target: str
    target_type: str
    item_image: bool


@dataclass
class Screened:
    accepted: dict[str, Accepted] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)      # key -> reason


def _check(backup: Backup, ref: AttachmentRef, carried: set[tuple[str, str]]) -> tuple[Accepted, bytes]:
    """The accepted file and its content. Raises ScanError with the reason it is left out."""
    if ref.too_long:
        raise ScanError(t("migration.odoo_attachment_unreadable"))
    if ref.target_type is None or (ref.target_type, ref.target) not in carried:
        raise ScanError(t("migration.odoo_attachment_no_target"))
    ext = accepted_extension(ref.name)
    if ext is None:
        raise ScanError(t("migration.odoo_attachment_type"))
    content = ref.stored if ref.stored is not None else (
        backup.stored_file(ref.store_fname, MAX_ATTACHMENT_BYTES) if ref.store_fname else None)
    if content is None or len(content) > MAX_ATTACHMENT_BYTES:
        raise ScanError(t("migration.odoo_attachment_unreadable"))
    if ref.checksum and hashlib.sha1(content).hexdigest() != ref.checksum:
        raise ScanError(t("migration.odoo_attachment_unreadable"))
    if not signature_matches(ext, content):
        raise ScanError(t("migration.odoo_attachment_type"))
    accepted = Accepted(ref.key, ref.name, FILE_TYPES[ext][0], len(content), hashlib.sha256(content).hexdigest(),
                        ref.target, ref.target_type, ref.item_image)
    return accepted, content


def screen(backup: Backup, refs: dict[str, AttachmentRef], carried: set[tuple[str, str]]) -> Screened:
    """Check every file against the records the migration imports, as (source type, id)."""
    screened = Screened()
    for key, ref in sorted(refs.items()):
        try:
            screened.accepted[key] = _check(backup, ref, carried)[0]
        except ScanError as rejected:
            screened.rejected[key] = str(rejected)
    return screened


def read_attachment(backup: Backup, refs: dict[str, AttachmentRef], key: str) -> bytes:
    """The content of one file attached to a record Celerp holds. Raises ScanError for any other key."""
    ref = refs.get(key)
    if ref is None or ref.target_type is None:
        raise ScanError(t("migration.err_attachment_not_found"))
    return _check(backup, ref, {(ref.target_type, ref.target)})[1]

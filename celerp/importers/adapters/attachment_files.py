# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The file types a migration carries as attachments, and the checks every source's files
pass before one is accepted. A source's own name, size, hash and content type for a file
are untrusted: a file is accepted only when its name is a plain file name with an
extension of a type Celerp stores and its content starts as that type's content does."""

from __future__ import annotations

from celerp.services.attachments import accepts_mime

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


def accepted_extension(name: str) -> str | None:
    """The accepted extension of a plain file name, or None when the name is unsafe or the type is not accepted."""
    if not name or len(name) > MAX_NAME or name in (".", "..") or "/" in name or "\\" in name:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return None
    _, dot, ext = name.rpartition(".")
    ext = ext.lower()
    return ext if dot and ext in FILE_TYPES and accepts_mime(FILE_TYPES[ext][0]) else None


def signature_matches(ext: str, content: bytes) -> bool:
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

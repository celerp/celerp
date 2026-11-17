# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI file I/O: single source of truth for upload directory and file loading."""

from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

from celerp.config import settings
from ui.i18n import t

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Files whose owning module parses them: the model is told the file id, never sent the bytes.
TABULAR_UPLOAD_TYPES = frozenset({"text/csv", XLSX_CONTENT_TYPE, "application/x-ofx", "application/vnd.intu.qfx"})
AGENT_UPLOAD_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"}) | TABULAR_UPLOAD_TYPES
# The type each accepted kind of file is kept as, by its extension: a browser without an entry
# for a kind (OFX and QFX often) sends a generic type, which would be refused.
UPLOAD_TYPE_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                         ".pdf": "application/pdf", ".csv": "text/csv", ".xlsx": XLSX_CONTENT_TYPE,
                         ".ofx": "application/x-ofx", ".qfx": "application/vnd.intu.qfx"}
# The file picker's accept list for every kind the assistant takes.
UPLOAD_ACCEPT = ",".join(UPLOAD_TYPE_BY_SUFFIX)


def upload_content_type(filename: str | None, sent: str | None) -> str:
    """The type to keep an uploaded file as: its extension's when it is a kind the
    assistant takes, else the type the browser sent."""
    return UPLOAD_TYPE_BY_SUFFIX.get(Path(filename or "").suffix.lower()) or sent or "application/octet-stream"
_UPLOAD_ID_RE = re.compile(r"^ai_up_[0-9a-f]{32}$")


def upload_dir() -> Path:
    """Return (and lazily create) the AI upload directory."""
    path = settings.data_dir / "ai_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_upload(company_id: uuid.UUID, user_id: uuid.UUID, filename: str | None, content_type: str,
                content: bytes) -> str:
    """Keep an uploaded file for the assistant and return its file id.

    The metadata naming the company is written before the content, so every stored body
    carries its company and is deleted with it. A failed write leaves nothing behind."""
    file_id = f"ai_up_{uuid.uuid4().hex}"
    d = upload_dir()
    bin_path, meta_path = d / f"{file_id}.bin", d / f"{file_id}.meta"
    meta = {"filename": filename, "content_type": content_type, "size": len(content),
            "company_id": str(company_id), "user_id": str(user_id)}
    try:
        meta_path.write_text(json.dumps(meta))
        bin_path.write_bytes(content)
    except BaseException:
        bin_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise
    return file_id


MAX_UPLOAD_FILES = 20
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def keep_uploads(session, company_id: uuid.UUID, user_id: uuid.UUID, files: list) -> list[str]:
    """Check and keep a request's uploaded files (``UploadFile``), returning their file ids:
    at most MAX_UPLOAD_FILES, each up to MAX_UPLOAD_BYTES and of AGENT_UPLOAD_TYPES. The
    company is held while they are written, so a company reset waits and then deletes them."""
    from fastapi import HTTPException

    from celerp.services.company_lock import hold_company
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=t("error.batch_too_many", max=MAX_UPLOAD_FILES))
    for file in files:
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=t("ai.err_file_too_large", name=file.filename))
        if file.content_type not in AGENT_UPLOAD_TYPES:
            raise HTTPException(status_code=400, detail=t("ai.err_file_type_named", name=file.filename))
    if not await hold_company(session, company_id):
        raise HTTPException(status_code=404, detail=t("error.company_unavailable"))
    try:
        return [save_upload(company_id, user_id, f.filename, f.content_type, await f.read()) for f in files]
    finally:
        await session.rollback()


def delete_company_uploads(company_id) -> None:
    """Delete every upload kept for ``company_id``; files already gone count as deleted.
    Raises when a file cannot be deleted."""
    d = settings.data_dir / "ai_uploads"
    if not d.is_dir():
        return
    for meta_path in d.glob("ai_up_*.meta"):
        try:
            owner = json.loads(meta_path.read_text()).get("company_id")
        except FileNotFoundError:
            continue
        except (ValueError, AttributeError):
            continue  # not written by save_upload: no body was stored after it
        if owner == str(company_id):
            # The metadata goes last, so a retry after a failure still finds the upload.
            meta_path.with_suffix(".bin").unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)


def _file_paths_and_meta(
    file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None,
) -> tuple[Path, dict]:
    """Resolve an owned transient upload without reading its potentially large body."""
    if not isinstance(file_id, str) or not _UPLOAD_ID_RE.fullmatch(file_id):
        raise FileNotFoundError(t("error.file_not_found"))
    d = upload_dir()
    bin_path = d / f"{file_id}.bin"
    meta_path = d / f"{file_id}.meta"
    if not bin_path.exists() or not meta_path.exists():
        raise FileNotFoundError(t("error.file_not_found"))
    meta = json.loads(meta_path.read_text())
    if meta.get("company_id") != str(company_id):
        raise PermissionError(t("error.file_not_accessible"))
    if user_id is not None and meta.get("user_id") != str(user_id):
        raise PermissionError(t("error.file_not_accessible"))
    return bin_path, meta


def upload_meta(file_id: str, company_id: uuid.UUID, user_id: uuid.UUID) -> dict | None:
    """The metadata of the upload ``file_id`` this user made in this company, or None when
    there is no such upload (another person's upload reads as none)."""
    try:
        return _file_paths_and_meta(file_id, company_id, user_id)[1]
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def load_file(file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None) -> tuple[bytes, dict]:
    """Load file bytes + metadata after company/user ownership validation."""
    bin_path, meta = _file_paths_and_meta(file_id, company_id, user_id)
    return bin_path.read_bytes(), meta


def load_file_for_llm(file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None) -> dict:
    """Build the model attachment record while avoiding unused tabular base64.

    Tabular files (CSV, XLSX, OFX) are represented to the model by filename/file_id only
    and are parsed by canonical local tools, so reading and base64-expanding their bytes here is wasted
    memory. Images/PDFs still carry their bytes because the model consumes them.
    """
    bin_path, meta = _file_paths_and_meta(file_id, company_id, user_id)
    media_type = meta.get("content_type", "image/jpeg")
    data = ""
    if media_type not in TABULAR_UPLOAD_TYPES:
        data = base64.b64encode(bin_path.read_bytes()).decode("utf-8")
    return {
        "media_type": media_type,
        "data": data,
        "filename": meta.get("filename", file_id),
        "file_id": file_id,
    }

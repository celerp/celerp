# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI file I/O: single source of truth for upload directory and file loading."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from celerp.config import settings

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
AGENT_UPLOAD_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "text/csv",
    XLSX_CONTENT_TYPE,
})


def upload_dir() -> Path:
    """Return (and lazily create) the AI upload directory."""
    path = settings.data_dir / "ai_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_paths_and_meta(
    file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None,
) -> tuple[Path, dict]:
    """Resolve an owned transient upload without reading its potentially large body."""
    d = upload_dir()
    bin_path = d / f"{file_id}.bin"
    meta_path = d / f"{file_id}.meta"
    if not bin_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"File {file_id} not found")
    meta = json.loads(meta_path.read_text())
    if meta.get("company_id") != str(company_id):
        raise PermissionError(f"File {file_id} not accessible")
    if user_id is not None and meta.get("user_id") != str(user_id):
        raise PermissionError(f"File {file_id} not accessible")
    return bin_path, meta


def load_file(file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None) -> tuple[bytes, dict]:
    """Load file bytes + metadata after company/user ownership validation."""
    bin_path, meta = _file_paths_and_meta(file_id, company_id, user_id)
    return bin_path.read_bytes(), meta


def load_file_for_llm(file_id: str, company_id: uuid.UUID, user_id: uuid.UUID | None = None) -> dict:
    """Build the model attachment record while avoiding unused tabular base64.

    CSV/XLSX are represented to the model by filename/file_id only and are parsed by
    canonical local tools, so reading and base64-expanding their bytes here is wasted
    memory. Images/PDFs still carry their bytes because the model consumes them.
    """
    bin_path, meta = _file_paths_and_meta(file_id, company_id, user_id)
    media_type = meta.get("content_type", "image/jpeg")
    data = ""
    if media_type not in {"text/csv", XLSX_CONTENT_TYPE}:
        data = base64.b64encode(bin_path.read_bytes()).decode("utf-8")
    return {
        "media_type": media_type,
        "data": data,
        "filename": meta.get("filename", file_id),
        "file_id": file_id,
    }

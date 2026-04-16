# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""AI file I/O — single source of truth for upload directory and file loading."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from celerp.config import settings


def upload_dir() -> Path:
    """Return (and lazily create) the AI upload directory."""
    path = settings.data_dir / "ai_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_file(file_id: str, company_id: uuid.UUID) -> tuple[bytes, dict]:
    """Load file bytes + metadata.

    Raises FileNotFoundError if file does not exist.
    Raises PermissionError if file belongs to a different company.
    """
    d = upload_dir()
    bin_path = d / f"{file_id}.bin"
    meta_path = d / f"{file_id}.meta"
    if not bin_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"File {file_id} not found")
    meta = json.loads(meta_path.read_text())
    if meta.get("company_id") != str(company_id):
        raise PermissionError(f"File {file_id} not accessible")
    return bin_path.read_bytes(), meta


def load_file_for_llm(file_id: str, company_id: uuid.UUID) -> dict:
    """Load file as base64 for LLM consumption.

    Returns {"media_type": str, "data": str}.
    """
    data_bytes, meta = load_file(file_id, company_id)
    return {
        "media_type": meta.get("content_type", "image/jpeg"),
        "data": base64.b64encode(data_bytes).decode("utf-8"),
    }

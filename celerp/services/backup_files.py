# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Incremental file backup — manifest-based change detection + tar.gz archive.

Walks configured directories (attachments, ai_uploads), builds a manifest
of all files, diffs against the previous manifest, and produces a tar.gz
containing only changed files plus the full manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass, asdict
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ManifestEntry:
    path: str
    sha256: str
    size: int
    mtime: float


def build_manifest(dirs: list[Path]) -> list[ManifestEntry]:
    """Walk directories and build a manifest entry for each file."""
    entries: list[ManifestEntry] = []
    for base in dirs:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            stat = p.stat()
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            entries.append(ManifestEntry(
                path=str(p),
                sha256=h,
                size=stat.st_size,
                mtime=stat.st_mtime,
            ))
    return entries


def diff_manifests(
    current: list[ManifestEntry],
    previous: list[ManifestEntry],
) -> list[Path]:
    """Compare current manifest against previous, return paths of changed files.

    Change detection (fast path first):
      1. New file (path not in previous) → include
      2. Size changed → include (skip hashing comparison)
      3. mtime changed, size same → compare sha256, include if different
      4. Size + mtime unchanged → skip
    """
    prev_map: dict[str, ManifestEntry] = {e.path: e for e in previous}
    changed: list[Path] = []

    for entry in current:
        prev = prev_map.get(entry.path)
        if prev is None:
            # New file
            changed.append(Path(entry.path))
        elif entry.size != prev.size:
            # Size changed
            changed.append(Path(entry.path))
        elif entry.mtime != prev.mtime and entry.sha256 != prev.sha256:
            # mtime changed + content actually different
            changed.append(Path(entry.path))

    return changed


def build_file_archive(
    changed_paths: list[Path],
    full_manifest: list[ManifestEntry],
    base_dirs: list[Path],
) -> bytes | None:
    """Build tar.gz archive with manifest.json + changed files.

    Returns None if no changes (skip-if-clean).
    """
    if not changed_paths:
        return None

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Add manifest.json
        manifest_json = json.dumps(
            [asdict(e) for e in full_manifest],
            indent=2,
        ).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_json)
        tar.addfile(info, io.BytesIO(manifest_json))

        # Add changed files with relative paths
        for fpath in changed_paths:
            if not fpath.exists():
                continue
            # Find relative path from any base dir
            rel = None
            for base in base_dirs:
                try:
                    rel = str(fpath.relative_to(base.parent))
                    break
                except ValueError:
                    continue
            if rel is None:
                rel = fpath.name
            tar.add(str(fpath), arcname=rel)

    return buf.getvalue()


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Load manifest from JSON file. Returns empty list if file missing."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [ManifestEntry(**e) for e in data]
    except Exception:
        log.warning("Failed to load manifest from %s, treating as empty", path)
        return []


def save_manifest(entries: list[ManifestEntry], path: Path) -> None:
    """Save manifest to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2))


async def run_file_backup(label: str | None = None):
    """Orchestrate: manifest build → diff → archive → encrypt → upload.

    Returns BackupResult.
    """
    from celerp.services.backup import BackupResult, _parse_key, encrypt, upload_to_relay
    from celerp.config import settings

    if not settings.backup_encryption_key:
        return BackupResult(ok=False, size_bytes=0, error="BACKUP_ENCRYPTION_KEY is not configured")

    try:
        dirs = [Path("static/attachments"), Path("data/ai_uploads")]
        manifest_path = Path("data/backup_manifest.json")

        current = build_manifest(dirs)
        previous = load_manifest(manifest_path)
        changed = diff_manifests(current, previous)

        archive = build_file_archive(changed, current, dirs)
        if archive is None:
            log.info("File backup: no changes detected, skipping upload")
            return BackupResult(ok=True, size_bytes=0)

        key = _parse_key(settings.backup_encryption_key)
        blob = encrypt(archive, key)
        await upload_to_relay(blob, backup_type="files", label=label)

        save_manifest(current, manifest_path)
        return BackupResult(ok=True, size_bytes=len(blob))
    except Exception as exc:
        return BackupResult(ok=False, size_bytes=0, error=str(exc))

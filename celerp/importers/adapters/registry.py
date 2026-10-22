# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The fixed set of built-in source adapters. Only shipped sources appear here."""

from __future__ import annotations

from celerp.importers.adapters.base import ArtifactSet, SourceAdapter
from celerp.importers.adapters.exports import QuickBooksOnlineAdapter, XeroAdapter
from celerp.importers.adapters.manager_io import ManagerIOAdapter
from celerp.importers.adapters.odoo import OdooAdapter

# Single-file sources first: their detection reads one header, an export set's reads every file.
_ADAPTERS: tuple[SourceAdapter, ...] = (ManagerIOAdapter(), OdooAdapter(), XeroAdapter(), QuickBooksOnlineAdapter())


def list_adapters() -> tuple[SourceAdapter, ...]:
    return _ADAPTERS


def get_adapter(key: str) -> SourceAdapter | None:
    return next((a for a in _ADAPTERS if a.key == key), None)


def detect_adapter(artifacts: ArtifactSet) -> SourceAdapter | None:
    """The first adapter whose content detection accepts the artifacts."""
    return next((a for a in _ADAPTERS if a.detect(artifacts).matched), None)

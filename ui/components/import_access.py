# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Who sees an Import button. Shared by the list pages that show the button and the
dashboard card that links to it, so the card never offers a link to a page whose
Import button the user cannot see."""

from __future__ import annotations

from celerp.services.permissions import role_has_permission


def can_import_documents(settings: dict, role: str) -> bool:
    """Documents import is open to anyone who may import data or edit documents."""
    return (role_has_permission(settings, role, "import_export_data")
            or role_has_permission(settings, role, "edit_documents"))

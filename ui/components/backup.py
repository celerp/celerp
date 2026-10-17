# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""DRY backup button builders with tooltips.

Used on System Recovery (settings.py _system_recovery_content).
"""

from __future__ import annotations

from fasthtml.common import A, Button, Div, Input
from ui.i18n import t, get_lang


# Tooltip i18n keys, resolved at render time (see cloud_backup_buttons /
# local_backup_buttons). Values here are keys, never English text (R1).
_TOOLTIP_KEYS = {
    "snapshot": "settings.backup_snapshot_tooltip",
    "download": "settings.backup_download_tooltip",
    "import": "settings.backup_import_tooltip",
}


def cloud_backup_buttons(
    *,
    enc_ok: bool,
    gw_ok: bool,
    flash_target_id: str = "backup-flash",
    cls: str = "flex-row gap-sm flex-wrap mt-lg",
) -> Div:
    """Cloud snapshot trigger button (database + files in one deduplicated snapshot)."""
    return Div(
        Button(t("btn.backup_now"),
            hx_post="/backup/trigger",
            hx_target=f"#{flash_target_id}",
            hx_swap="outerHTML",
            hx_disabled_elt="this",
            cls="btn btn--primary",
            disabled=not (enc_ok and gw_ok),
            title=t(_TOOLTIP_KEYS["snapshot"]),
        ),
        cls=cls,
    )


def local_backup_buttons(
    *,
    import_input_id: str = "backup-import-input",
    flash_target_id: str = "backup-flash",
    btn_size: str = "",
    as_list: bool = False,
    cls: str = "flex-row gap-sm flex-wrap",
) -> Div | list:
    """Local-only backup buttons (download + import). For unconnected or cloud page card."""
    size_cls = f" btn--{btn_size}" if btn_size else ""
    elements = [
        A(t("settings.download_backup"),
            href="/backup/export",
            cls=f"btn btn--secondary{size_cls}",
            title=t(_TOOLTIP_KEYS["download"]),
        ),
        Button(t("btn.import_backup"),
            # The picker opens straight from the click: the browser stops a click
            # from opening it once the user has spent a few seconds on a dialog.
            onclick=f"document.getElementById('{import_input_id}').click()",
            cls=f"btn btn--secondary{size_cls}",
            title=t(_TOOLTIP_KEYS["import"]),
            id="backup-import-btn",
        ),
        Input(
            type="file", id=import_input_id, name="file",
            accept=".celerp-backup",
            hx_post="/backup/import", hx_encoding="multipart/form-data",
            hx_target=f"#{flash_target_id}", hx_swap="outerHTML",
            hx_disabled_elt="#backup-import-btn",
            # Confirm once a file is chosen: the import replaces the whole
            # installation. Cancelling clears the choice so the same file can be
            # picked again. Translated text arrives via data-* attributes (R2),
            # never spliced into the JS source string.
            data_confirm_text=t("system_recovery.confirm"),
            data_restoring_text=t("auth.restoring"),
            hx_on__confirm="event.preventDefault();if(confirm(this.dataset.confirmText))event.detail.issueRequest(true);else this.value='';",
            hx_on__before_request="var btn=document.getElementById('backup-import-btn');if(btn){btn.disabled=true;btn.textContent=this.dataset.restoringText;}",
            style="display:none",
        ),
    ]
    return elements if as_list else Div(*elements, cls=cls)

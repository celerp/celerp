# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""DRY backup button builders with tooltips.

Used in settings.py (_backup_tab) and settings_cloud.py (_backup_summary_card).
"""

from __future__ import annotations

from fasthtml.common import A, Button, Div, Input
from ui.i18n import t, get_lang


_TOOLTIPS = {
    "snapshot": "Upload an encrypted, deduplicated snapshot of your database and files to the cloud. Only changed files are uploaded.",
    "download": "Download a complete backup archive (database + all files) to your computer. Not encrypted - store it securely.",
    "import": "Restore from a previously downloaded .celerp-backup archive. This will overwrite your current data.",
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
            title=_TOOLTIPS["snapshot"],
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
            title=_TOOLTIPS["download"],
        ),
        Button(t("btn.import_backup"),
            onclick=f"document.getElementById('{import_input_id}').click()",
            cls=f"btn btn--secondary{size_cls}",
            title=_TOOLTIPS["import"],
            id="backup-import-btn",
        ),
        Input(
            type="file", id=import_input_id, name="file",
            accept=".celerp-backup",
            hx_post="/backup/import", hx_encoding="multipart/form-data",
            hx_target=f"#{flash_target_id}", hx_swap="outerHTML",
            hx_disabled_elt="#backup-import-btn",
            onchange="var btn=document.getElementById('backup-import-btn');if(btn){btn.disabled=true;btn.textContent='Restoring\u2026';}",
            style="display:none",
        ),
    ]
    return elements if as_list else Div(*elements, cls=cls)

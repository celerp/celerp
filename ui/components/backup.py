# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""DRY backup button builders with tooltips.

Used in settings.py (_backup_tab) and settings_cloud.py (_backup_summary_card).
"""

from __future__ import annotations

from fasthtml.common import A, Button, Div, Input
from ui.i18n import t, get_lang


_TOOLTIPS = {
    "db": "Upload an encrypted copy of your database to the cloud. Does not include files or attachments.",
    "files": "Upload encrypted copies of your attachments and uploaded files to the cloud. Does not include the database.",
    "download": "Download a complete backup archive (database + all files) to your computer. Not encrypted - store it securely.",
    "import": "Restore from a previously downloaded .celerp-backup archive. This will overwrite your current data.",
}


def cloud_backup_buttons(
    *,
    enc_ok: bool,
    gw_ok: bool,
    flash_target_id: str = "backup-flash",
    import_input_id: str = "backup-import-input",
    cls: str = "flex-row gap-sm flex-wrap mt-lg",
) -> Div:
    """Full set of backup action buttons (cloud + local). For connected state."""
    return Div(
        Button(t("btn.backup_database_now"),
            hx_post="/backup/trigger?type=database",
            hx_target=f"#{flash_target_id}",
            hx_swap="outerHTML",
            cls="btn btn--primary",
            disabled=not (enc_ok and gw_ok),
            title=_TOOLTIPS["db"],
        ),
        Button(t("btn.backup_files_now"),
            hx_post="/backup/trigger?type=files",
            hx_target=f"#{flash_target_id}",
            hx_swap="outerHTML",
            cls="btn btn--secondary",
            disabled=not (enc_ok and gw_ok),
            title=_TOOLTIPS["files"],
        ),
        *local_backup_buttons(
            import_input_id=import_input_id,
            flash_target_id=flash_target_id,
            as_list=True,
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
        ),
        Input(
            type="file", id=import_input_id, name="file",
            accept=".celerp-backup",
            hx_post="/backup/import", hx_encoding="multipart/form-data",
            hx_target=f"#{flash_target_id}", hx_swap="outerHTML",
            style="display:none",
        ),
    ]
    return elements if as_list else Div(*elements, cls=cls)

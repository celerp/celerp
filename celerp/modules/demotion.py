# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Surface a demoted first-party module in the notification bell.

A bundled default whose live content no longer matches the committed first-party
lock is demoted to an untrusted third-party module (celerp.modules.loader:
is_first_party). That is a real, instance-wide state an admin should see from any
page - not a line buried in the boot console and not a banner that only appears
on /modules. This bridges the loader's per-module verdict to the company-scoped
notification bell: one deduped, company-wide notice per demoted module.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

_CATEGORY = "modules"


def _title(name: str) -> str:
    return f"Module '{name}' is no longer verified"


def _body(name: str) -> str:
    return (
        f"The bundled module '{name}' no longer matches its signed first-party "
        "entry, so it now runs as an untrusted third-party module. If you did not "
        "modify it, reinstall Celerp to restore the original."
    )


async def notify_demoted_modules(session: AsyncSession, demoted_names: list[str]) -> int:
    """Create a company-wide bell notice for each demoted module, per company.

    Deduped on the standing notice so at most one stands per company per module:
    a still-demoted module re-notifies on the next boot only after the prior one
    was dismissed (persistent-until-fixed, exactly like the banner it replaces),
    and a reboot while it still stands creates nothing new. Caller commits.
    Returns the number of notifications created."""
    from celerp.notifications import service as notif_service

    created = 0
    for name in demoted_names:
        created += await notif_service.notify_every_company(
            session, _CATEGORY, _title(name), _body(name), action_url="/modules")
    return created

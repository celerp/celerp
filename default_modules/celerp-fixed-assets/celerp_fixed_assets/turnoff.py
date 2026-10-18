# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What stands open when a company turns fixed assets off.

A sale waiting on its invoice blocks turning off: completing or cancelling it is an
asset operation, and none can happen while the module is off. Corrections waiting for
approval, prepared runs and open reconciliation differences are listed and kept, to be
picked up when the module is on again.
"""
from __future__ import annotations

from celerp_fixed_assets import runs, yearend
from celerp_fixed_assets.calc.periods import parse_period


async def _prepared_runs(session, company_id) -> list[dict]:
    return [{"label": ("fixed_assets.turn_off_run_prepared", {"month": runs._long(parse_period(r["month"]))}),
             "href": "/fixed-assets/runs"}
            for r in await runs.records(session, company_id) if r["status"] == "prepared"]


async def preflight(*, session, company_id) -> dict:
    """{"blocking": [...], "listed": [...]}, each item {"label", "href"}."""
    from ui.i18n import t

    def shown(items: list[dict]) -> list[dict]:
        return [{"label": t(item["label"][0], **item["label"][1]), "href": item["href"]} for item in items]

    return {
        "blocking": shown(await yearend.pending_sales(session, company_id)),
        "listed": shown([*await yearend.waiting_corrections(session, company_id),
                         *await _prepared_runs(session, company_id),
                         *await yearend.open_differences(session, company_id)]),
    }

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Doctor: a read-only report on the last start and the record checks.

It never repairs anything. When the last start held the records back, the failed
steps and their errors come first, so they can be read and passed on with a bug report.
"""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, page_title
from ui.config import get_token as _token
from ui.i18n import get_lang, refusal_text, t


def _start_section(report: dict | None, lang: str) -> FT:
    if report is None:
        return Div(H2(t("doctor.last_start", lang)), P(t("doctor.start_ok", lang)),
                   cls="settings-card", id="doctor-start")
    failures = [part for f in report["failures"]
                for part in (Dt(refusal_text(f["step"])), Dd(Code(refusal_text(f["error"]), cls="doctor-error")))]
    return Div(
        H2(refusal_text(report["title"])),
        Dl(*failures, cls="doctor-failures"),
        P(refusal_text(report["what_to_do"])),
        cls="settings-card", id="doctor-start",
    )


def _checks_section(checks: dict | None, unavailable: str, lang: str) -> FT:
    body = P(unavailable) if checks is None else Table(
        Thead(Tr(Th(t("doctor.check", lang)), Th(t("doctor.found", lang), cls="cell--number"))),
        Tbody(*[Tr(Td(r["check"]), Td(str(r["found"]), cls="cell--number"), cls="data-row")
                for r in checks["results"]]),
        cls="data-table",
    )
    return Div(H2(t("doctor.record_checks", lang)), body, cls="settings-card", id="doctor-checks")


def setup_routes(app):

    @app.get("/doctor")
    async def doctor_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            start = (await api.get_start_report(token))["held_back"]
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            # The last start's report is for admins, like the record checks.
            start_section = Div(P(t("doctor.start_ask_admin", lang) if e.status == 403 else e.detail),
                                cls="settings-card", id="doctor-start")
        else:
            start_section = _start_section(start, lang)
        checks, unavailable = None, ""
        try:
            checks = await api.doctor_report(token)
        except APIError as e:
            # Admin Tools runs the checks, for admins only; without it there are none to run.
            unavailable = (t("doctor.checks_off", lang) if e.status == 404
                           else t("doctor.ask_admin", lang) if e.status == 403
                           else e.detail)
        return await base_shell(
            page_header(t("doctor.title", lang)),
            P(t("doctor.intro", lang)),
            start_section,
            _checks_section(checks, unavailable, lang),
            title=page_title("doctor.title"),
            nav_active="doctor",
            lang=lang,
            request=request,
        )

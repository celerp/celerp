# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The Donors page: each donor's gifts received and kept in a period, and their statements.

The period is in the URL (``from``, ``to``; this financial year by default). Each donor shows
their total, each gift and the record it came from, their email address or "No email", whether
the period's statement under the current wording went out, and the statement PDF. Send emails
every donor not yet sent the statement once the confirmation is ticked; Send again on a donor
emails only that donor. A sent email cannot be recalled from the donor's mailbox, so sending
is recorded on the contact and a corrected statement goes out under new wording or by Send again.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError, error_message
from ui.components.encode import safe_url
from ui.components.report_kit import plain_error_response
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY, doc_href, empty_state_cta, fmt_money
from ui.config import get_token as _token
from ui.i18n import get_lang, sentence, t
from ui.routes.action_answers import _done, _redrawn, _refused, _refused_redrawn
from ui.routes.reports import _date_filter_bar, _get_company, _parse_dates
from ui.routes.settings import _check_permission

PAGE = "/donors"
SEND_URL = "/donors/statements/send"
WORDING_URL = "/donors/statements/wording"
_WORDING_ID = "donor-wording"
_NUM = "cell--number"


def _page_url(date_from: str, date_to: str) -> str:
    return f"{PAGE}?{urlencode({'from': date_from, 'to': date_to})}"


def _period_vals(date_from: str, date_to: str) -> dict:
    return {"from": date_from, "to": date_to}


def _gift_line(gift: dict, currency: str | None) -> FT:
    source = (A(gift.get("ref") or EMPTY, href=safe_url(doc_href(gift["doc_id"]))) if gift.get("doc_id")
              else gift.get("ref") or EMPTY)
    return Div(f"{gift['date']} ", source, f" {fmt_money(gift['amount'], currency)}")


def _statement_cell(row: dict, period: tuple[str, str]) -> FT:
    """Sent or not; Send again names the number of the send it asks for, so the same press arriving
    twice sends once."""
    if not row["sends"]:
        return Td(t("donors.statement_not_sent"))
    return Td(Span(t("donors.statement_sent"), cls="badge badge--paid"), " ",
              Button(t("btn.send_again"), hx_post=SEND_URL, hx_swap="none",
                     hx_vals={**_period_vals(*period), "confirm": "on", "resend": row["contact_id"],
                              "seen": row["sends"]},
                     hx_confirm=t("donors.confirm_resend", name=row["name"]), cls="btn btn--secondary btn--sm"))


def donors_table(rows: list[dict], period: tuple[str, str], currency: str | None) -> FT:
    """Donors largest total first: who, each gift and its source, total, email, statement, PDF."""
    if not rows:
        return empty_state_cta(t("donors.empty"))

    def _row(row: dict) -> FT:
        pdf = f"/donors/{row['contact_id']}/statement.pdf?{urlencode(_period_vals(*period))}"
        return Tr(
            Td(A(row["name"], href=safe_url(f"/contacts/{row['contact_id']}"))),
            Td(*[_gift_line(g, currency) for g in row.get("gifts") or []]),
            Td(fmt_money(row["total"], currency), cls=_NUM),
            Td(row["email"] or t("donors.no_email")),
            _statement_cell(row, period),
            Td(A(t("donors.pdf"), href=safe_url(pdf), target="_blank")),
        )

    head = [Th(t("donors.th_donor")), Th(t("donors.th_gifts")), Th(t("th.total"), cls=_NUM),
            Th(t("th.email")), Th(t("donors.th_statement")), Th(t("donors.pdf"))]
    return Div(Table(Thead(Tr(*head)), Tbody(*[_row(r) for r in rows]), cls="data-table"), cls="table-scroll-wrap")


def wording_card(words: dict, saved: bool = False) -> FT:
    """The statement wording, saved where it is shown."""
    return Div(
        H3(t("donors.wording_title")),
        Form(Textarea(words["text"], name="text", rows="3", cls="form-input"),
             P(t("donors.wording_hint"), cls="settings-hint"),
             Button(t("btn.save"), type="submit", cls="btn btn--secondary btn--sm"),
             P(t("donors.wording_saved"), cls="text-muted") if saved else "",
             hx_put=WORDING_URL, hx_target=f"#{_WORDING_ID}", hx_swap="outerHTML"),
        id=_WORDING_ID, cls="settings-card mt-md",
    )


def send_card(period: tuple[str, str]) -> FT:
    """Send the period's statements; the API refuses a send without the confirmation ticked."""
    return Div(
        H3(t("donors.send_title")),
        P(t("donors.send_hint"), cls="settings-hint"),
        Form(*[Input(type="hidden", name=k, value=v) for k, v in _period_vals(*period).items()],
             Label(Input(type="checkbox", name="confirm", value="on"), " ", t("donors.confirm_send")),
             Button(t("donors.btn_send"), type="submit", cls="btn btn--primary"),
             hx_post=SEND_URL, hx_swap="none"),
        cls="settings-card mt-md",
    )


def _sent_message(results: list[dict], resend: list[str]) -> tuple[str, str]:
    """The toast after a send, read from each donor's result: how many went out, how many were
    skipped for no email or already sent, and who could not be reached."""
    by_state = {state: [r for r in results if r.get("state") == state] for state in ("sent", "no_email", "failed")}
    fresh = [r for r in by_state["sent"] if not r.get("repeat")]
    if resend:
        parts = [sentence("donors.done_resent", name=r["name"]) for r in fresh]
    else:
        parts = [sentence("donors.done_sent", sent=len(fresh), skipped=len(by_state["no_email"]))]
    if already := len(by_state["sent"]) - len(fresh):
        parts.append(sentence("donors.done_already", already=already))
    if by_state["failed"]:
        parts.append(sentence("donors.done_failed", names=", ".join(r["name"] for r in by_state["failed"])))
    return " ".join(parts), "warning" if by_state["failed"] else "success"


async def _signed_in_with(request: Request) -> Response | None:
    """Sign-in, then both permissions the page reads with: contacts and payments."""
    if not _token(request):
        return RedirectResponse("/login", status_code=302)
    for key in ("view_contacts", "view_payments"):
        if (r := await _check_permission(request, key, page_view=True)):
            return r
    return None


def setup_routes(app):

    @app.get(PAGE)
    async def donors_page(request: Request):
        if (away := await _signed_in_with(request)):
            return away
        token = _token(request)
        lang = get_lang(request)
        title = t("nav.donors", lang)
        try:
            company = await _get_company(token)
            d_from, d_to, preset = _parse_dates(request, company)
            data = await api.donor_statements_preview(token, d_from, d_to)
            rows = data.get("donors") or []
            body = Div(
                donors_table(rows, (d_from, d_to), company.get("currency")),
                wording_card(data["wording"]),
                send_card((d_from, d_to)) if rows else "",
            )
            bar = _date_filter_bar(PAGE, d_from, d_to, preset, settings_link="/settings/general?tab=company", lang=lang)
        except APIError as e:
            api.raise_if_signed_out(e)
            bar, body = "", Div(error_message(e), cls="error-banner")
        content = Div(page_header(title), P(t("donors.intro", lang), cls="text-muted"), bar, body, cls="page-content")
        return await base_shell(content, title=title, request=request, nav_active="donors")

    @app.get("/donors/{contact_id}/statement.pdf")
    async def statement_pdf(request: Request, contact_id: str):
        if (away := await _signed_in_with(request)):
            return away
        q = request.query_params
        try:
            pdf = await api.donor_statement_pdf(_token(request), contact_id, q.get("from", ""), q.get("to", ""))
        except APIError as e:
            return plain_error_response(e)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": "inline; filename=statement.pdf"})

    @app.post(SEND_URL)
    async def send_statements(request: Request):
        """Send the period's statements (or one donor's again) and load the page again, where each
        donor's statement reads as stored, with a toast saying what went out."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        d_from, d_to = str(form.get("from") or ""), str(form.get("to") or "")
        resend = {str(form.get("resend")): str(form.get("seen") or "")} if form.get("resend") else {}
        try:
            answer = await api.send_donor_statements(token, d_from, d_to, form.get("confirm") == "on", resend)
        except APIError as e:
            return _refused(e)
        return _done(_page_url(d_from, d_to), *_sent_message(answer.get("results") or [], resend))

    @app.put(WORDING_URL)
    async def save_wording(request: Request):
        """Save the wording and show it as stored, or keep what was typed with the refusal."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        text = str((await request.form()).get("text") or "")

        async def stored(saved: bool = True):
            return wording_card(await api.get_donor_wording(token), saved=saved)

        try:
            saved = await api.set_donor_wording(token, text)
        except APIError as e:
            return await _refused_redrawn(e, lambda: stored(saved=False))
        return await _redrawn(stored, lambda: wording_card(saved, saved=True), t("donors.wording_saved"))

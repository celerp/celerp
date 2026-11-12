# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sync accounts: records a connected company sent that wait for an account, each with
its fix, or for a fund on each line, each posted with the funds chosen for it; the
accounts sync added here, each kept, archived or removed; and the first review of a new
client's accounts. Every action answers in place."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from celerp.services.money import document_line_amount
from celerp.services.peer_lines import UNRESTRICTED, waits_for_funds
from ui.components.attrs import hx_vals
from ui.components.funds import fund_options
from ui.components.shell import flash, toast_header
from ui.components.table import EMPTY, fmt_money, searchable_select
from ui.config import get_token as _token
from ui.i18n import get_lang, refusal_text, t
from ui.routes.financial_reports import ledger_path
from ui.components.encode import html_response, safe_url

PAGE = "/accounting/sync-accounts"
_SWAP = {"hx_target": "#sync-accounts", "hx_swap": "outerHTML"}


def _name(account: dict) -> str:
    return f"{account['code']} {account.get('name') or ''}".strip()


def _choices(chart: list[dict], account_type: str | None) -> list[tuple[str, str]]:
    """Active accounts of the held account's type; archived ones are never offered."""
    return [(a["code"], _name(a)) for a in chart
            if a.get("is_active", True) and (not account_type or a.get("account_type") == account_type)]


def _hold_fix(item: dict, chart: list[dict], lang: str) -> FT:
    """The fix a held record's reason calls for: use the matching account, add it under a
    free code, unarchive it, or choose the posting account; then any other account."""
    fix = item.get("fix") or {}
    kind, other, local = fix.get("kind"), fix.get("other") or {}, fix.get("local")
    url = f"{PAGE}/holds/{quote(item['id'], safe='')}"
    if fix.get("fixed"):
        return Span(t("sync_accounts.fixed", lang))
    if kind == "role":
        return A(t("sync_accounts.choose_posting", lang), href=safe_url(fix.get("href") or "/settings/accounting#posting-accounts"),
                 cls="btn btn--secondary btn--xs")
    by_code = {a["code"]: a for a in chart if a.get("is_active", True)}
    quick = []
    if kind == "collision" and local in by_code:
        quick.append(Button(t("sync_accounts.use", lang, account=_name(by_code[local])), hx_post=url,
                            hx_vals=hx_vals({"action": "use", "account": local}),
                            cls="btn btn--xs btn--primary", **_SWAP))
    if kind == "archived" and local:
        quick.append(Button(t("sync_accounts.unarchive", lang, code=local), hx_post=url,
                            hx_vals=hx_vals({"action": "unarchive"}), cls="btn btn--xs btn--primary", **_SWAP))
    if kind == "collision" and fix.get("add") and fix.get("suggested"):
        quick.append(Button(t("sync_accounts.add_as", lang, code=fix["suggested"]), hx_post=url,
                            hx_vals=hx_vals({"action": "add", "code": fix["suggested"]}),
                            cls="btn btn--xs btn--secondary", **_SWAP))
    use_other = Form(
        searchable_select("account", _choices(chart, other.get("account_type")),
                          aria_label=t("sync_accounts.use_other", lang)),
        Hidden(name="action", value="use"),
        Button(t("sync_accounts.use_other", lang), type="submit", cls="btn btn--xs btn--secondary"),
        hx_post=url, cls="flex-row align-center gap-sm", **_SWAP,
    )
    add_new = Form(
        Input(name="code", value=fix.get("suggested") or other.get("code") or "", cls="input input--sm",
              aria_label=t("th.code", lang), size="8"),
        Hidden(name="action", value="add"),
        Button(t("sync_accounts.add_new", lang), type="submit", cls="btn btn--xs btn--secondary"),
        hx_post=url, cls="flex-row align-center gap-sm", **_SWAP,
    ) if fix.get("add") and kind != "collision" else ""
    return Div(Div(*quick, cls="flex-row gap-sm") if quick else "", use_other, add_new, cls="flex-col gap-sm")


def _fund_balances(held: dict, accounts: dict[str, str], fund_names: dict[str, str], lang: str) -> FT | str:
    """A held line's account and what each fund holds on it, muted beside its Fund select."""
    code = held.get("account")
    if not code:
        return ""
    name = accounts.get(code, code)
    parts = [f"{fund_names.get(fund, fund)} {fmt_money(amount)}" for fund, amount in (held.get("balances") or {}).items()]
    return Div(t("fund.account_balances", lang, account=name, balances=", ".join(parts)) if parts
               else t("fund.account_no_balance", lang, account=name), cls="text-muted")


def _fund_hold(item: dict, chart: list[dict], funds: list[dict], lang: str, kept: dict | None) -> FT:
    """A record held for a fund on each line: its lines, each with a Fund select at -- and
    its account's balance per fund, "Set all outside lines to" above them, and Post, which is
    never disabled (the books judge the choices). ``kept`` holds a refused Post's choices
    and request, so the user changes what was refused and posts the same request again."""
    fix, kept = item["fix"], kept or {}
    journal = fix.get("kind") == "funds"
    lines = [line for line in (fix.get("entries") if journal else (fix.get("sent") or {}).get("line_items")) or []
             if isinstance(line, dict)]
    held = item.get("fund_lines") or []
    accounts = {a["code"]: _name(a) for a in chart}
    fund_names = {UNRESTRICTED: t("label.unrestricted", lang), **{f["id"]: f["name"] for f in funds}}
    options = fund_options(funds)

    def choice(name: str, label: str) -> FT:
        return searchable_select(name, options, value=kept.get(name) or "", placeholder=EMPTY, aria_label=label)

    def money(value) -> FT:
        return Td(fmt_money(value) if value else "", cls="cell--money")

    rows = []
    for n, line in enumerate(lines, 1):
        fund = Td(choice(f"line_{n}", t("th.fund", lang)),
                  _fund_balances(held[n - 1] if n <= len(held) else {}, accounts, fund_names, lang))
        if journal:
            rows.append(Tr(Td(accounts.get(line.get("account"), line.get("account") or EMPTY)),
                           money(line.get("debit")), money(line.get("credit")), fund))
        else:
            code = held[n - 1].get("account") if n <= len(held) else None
            rows.append(Tr(Td(line.get("name") or line.get("description") or EMPTY),
                           Td(accounts.get(code, code) if code else EMPTY),
                           money(document_line_amount(line, (fix.get("sent") or {}).get("currency"))), fund))
    head = ((Th(t("th.account", lang)), Th(t("th.debit", lang), cls="cell--number"),
             Th(t("th.credit", lang), cls="cell--number")) if journal
            else (Th(t("th.description", lang)), Th(t("th.account", lang)), Th(t("th.total", lang), cls="cell--number")))
    return Form(
        P(t("sync_accounts.funds_hint", lang), cls="settings-hint"),
        Div(Label(t("fund.set_all_outside", lang), cls="form-label"),
            choice("all", t("fund.set_all_outside", lang)), cls="fund-set-all gap-sm"),
        Table(Thead(Tr(*head, Th(t("th.fund", lang)))), Tbody(*rows), cls="data-table"),
        Hidden(name="revision", value=str(fix.get("revision") or 0)),
        Hidden(name="request_key", value=kept.get("request_key") or uuid.uuid4().hex),
        Button(t("btn.post", lang), type="submit", cls="btn btn--primary btn--sm"),
        hx_post=f"{PAGE}/holds/{quote(item['id'], safe='')}/funds", cls="flex-col gap-sm", **_SWAP,
    )


def _holds(items: list[dict], chart: list[dict], lang: str, funds: list[dict] | None = None,
           kept: dict[str, dict] | None = None) -> FT | str:
    """The held records, each with its fix; ``kept`` maps a record to a refused Post's form."""
    if not items:
        return ""
    return Div(
        H3(t("sync_accounts.holds_title", lang)),
        P(t("sync_accounts.holds_hint", lang), cls="settings-hint"),
        Table(
            Thead(Tr(Th(t("th.date", lang)), Th(t("sync_accounts.record", lang)),
                     Th(t("sync_accounts.why", lang)), Th(t("th.actions", lang)))),
            Tbody(*[Tr(Td((item.get("at") or "")[:10] or EMPTY), Td(item.get("label") or EMPTY),
                       Td(item.get("reason") or EMPTY),
                       Td(_fund_hold(item, chart, funds or [], lang, (kept or {}).get(item["id"]))
                          if waits_for_funds(item.get("fix")) else _hold_fix(item, chart, lang)), cls="data-row")
                    for item in items]),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _added_actions(item: dict, lang: str) -> FT:
    """Keep closes the notice; an unused account can be removed, any other archived."""
    url = f"{PAGE}/added/{quote(item['id'], safe='')}"
    keep = Button(t("sync_accounts.keep", lang), hx_post=url, hx_vals=hx_vals({"action": "keep"}),
                  cls="btn btn--xs btn--secondary", **_SWAP)
    if item.get("removable"):
        settle = Button(t("sync_accounts.remove", lang), hx_post=url, hx_vals=hx_vals({"action": "remove"}),
                        hx_confirm=t("sync_accounts.remove_confirm", lang, account=item.get("label") or ""),
                        cls="btn btn--xs btn--outline", **_SWAP)
    else:
        settle = Button(t("sync_accounts.archive", lang), hx_post=url, hx_vals=hx_vals({"action": "archive"}),
                        cls="btn btn--xs btn--outline", **_SWAP)
    open_account = A(t("sync_accounts.open_account", lang), href=ledger_path(item["code"]),
                     cls="btn btn--xs btn--ghost") if item.get("code") else ""
    why = P(item["why"], cls="settings-hint") if item.get("why") else ""
    return Div(Div(open_account, keep, settle, cls="flex-row gap-sm"), why)


def _added(items: list[dict], lang: str) -> FT | str:
    if not items:
        return ""
    return Div(
        H3(t("sync_accounts.added_title", lang)),
        P(t("sync_accounts.added_hint", lang), cls="settings-hint"),
        Table(
            Thead(Tr(Th(t("th.date", lang)), Th(t("sync_accounts.account", lang)),
                     Th(t("sync_accounts.why", lang)), Th(t("th.actions", lang)))),
            Tbody(*[Tr(Td((item.get("at") or "")[:10] or EMPTY), Td(item.get("label") or EMPTY),
                       Td(item.get("reason") or EMPTY), Td(_added_actions(item, lang)), cls="data-row")
                    for item in items]),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _review_answer(account: dict, chart: list[dict], lang: str) -> FT:
    """The client's account answered with one of these books' accounts or added as new;
    an answer from a previous client is preselected and says so."""
    add = f"add:{account['add_code']}"
    options = [*_choices(chart, account.get("account_type")),
               (add, t("sync_accounts.add_as_new", lang, code=account["add_code"]))]
    return Div(
        searchable_select(f"answer_{account['id']}", options, value=account.get("suggested") or "",
                          aria_label=_name(account)),
        P(t("sync_accounts.from_previous", lang), cls="settings-hint") if account.get("previous") else "",
    )


def _review(company: dict, chart: list[dict], lang: str) -> FT:
    accounts = company.get("accounts") or []
    return Div(
        H3(t("sync_accounts.review_title", lang, client=company.get("client") or EMPTY)),
        P(t("sync_accounts.review_count_one" if len(accounts) == 1 else "sync_accounts.review_count",
            lang, n=len(accounts)), cls="settings-hint"),
        Form(
            Hidden(name="source_company_id", value=company["source_company_id"]),
            Table(
                Thead(Tr(Th(t("sync_accounts.their_account", lang)), Th(t("sync_accounts.why", lang)),
                         Th(t("sync_accounts.your_account", lang)))),
                Tbody(*[Tr(Td(_name(a)), Td(a.get("why") or EMPTY), Td(_review_answer(a, chart, lang)),
                           cls="data-row") for a in accounts]),
                cls="data-table",
            ),
            Div(Button(t("sync_accounts.confirm", lang), type="submit", name="finish", value="1",
                       cls="btn btn--primary btn--sm"),
                Button(t("sync_accounts.save_later", lang), type="submit", cls="btn btn--secondary btn--sm"),
                cls="flex-row gap-sm"),
            hx_post=f"{PAGE}/review", **_SWAP,
        ),
        cls="settings-card",
    )


async def sync_accounts_body(token: str, lang: str, error: str | None = None,
                             kept: dict[str, dict] | None = None) -> FT:
    """The page body, read fresh after every action; a read failure says so in place.
    ``kept`` maps a record held for funds to the choices of its refused Post."""
    notice = [flash(error)] if error else []
    try:
        held = await api.peer_holds(token)
        review = (await api.peer_review(token)).get("companies") or []
        chart = (await api.get_chart(token)).get("items") or []
        funds = ((await api.list_funds(token)).get("items") or []
                 if any(waits_for_funds(i.get("fix")) for i in held.get("items") or []) else [])
    except api.APIError as exc:
        return Div(*notice, flash(refusal_text(exc.detail)), id="sync-accounts")
    # A record waiting for a review is listed with the review, and again once it is fixed.
    holds = [i for i in held.get("items") or []
             if (i.get("fix") or {}).get("kind") != "review" or i["fix"].get("fixed")]
    added = held.get("added") or []
    if not (holds or added or review):
        return Div(*notice, P(t("sync_accounts.nothing", lang), cls="empty-state-msg"), id="sync-accounts")
    return Div(*notice, *[_review(c, chart, lang) for c in review], _holds(holds, chart, lang, funds, kept),
               _added(added, lang), id="sync-accounts")


def _answers(form) -> list[dict]:
    """Each answered row; a blank answer is left for later."""
    answers = []
    for key, value in form.items():
        if not key.startswith("answer_") or not value:
            continue
        account = key.removeprefix("answer_")
        answers.append({"account": account, "add": value[4:]} if value.startswith("add:")
                       else {"account": account, "use": value})
    return answers


def setup_routes(app) -> None:

    @app.get(PAGE)
    async def sync_accounts_page(request: Request):
        from ui.components.shell import base_shell, page_header
        from ui.routes.settings import _check_permission

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_accounting", page_view=True)):
            return r
        lang = get_lang(request)
        return await base_shell(
            page_header(t("sync_accounts.title", lang)),
            P(t("sync_accounts.intro", lang), cls="settings-hint"),
            await sync_accounts_body(token, lang),
            title=f"{t('sync_accounts.title', lang)} - Celerp", nav_active="accounting", request=request,
        )

    @app.post(PAGE + "/holds/{item_id}")
    async def sync_accounts_fix(request: Request, item_id: str):
        """Fix a held record's account; on the client the sync runs at once so the record
        posts now, on the firm it posts on the connected company's next sync."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        body = {k: form[k] for k in ("action", "account", "code") if form.get(k)}
        try:
            await api.fix_peer_hold(token, item_id, body)
        except api.APIError as exc:
            return await sync_accounts_body(token, lang, refusal_text(exc.detail))
        try:
            started = (await api.start_connector_sync(token, "celerp")).get("ok")
        except api.APIError:
            started = False
        message = t("sync_accounts.fixed_syncing" if started else "sync_accounts.fixed_next_sync", lang)
        return html_response(await sync_accounts_body(token, lang), headers=toast_header(message, "success"))

    @app.post(PAGE + "/holds/{item_id}/funds")
    async def sync_accounts_post_funds(request: Request, item_id: str):
        """Post a record held for funds with the choices made on its lines; it posts now, as
        this user. A refusal is shown in place with the choices kept."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = dict(await request.form())
        lines = {key.removeprefix("line_"): value for key, value in form.items()
                 if key.startswith("line_") and key.removeprefix("line_").isdigit() and value}
        body = {"revision": int(form.get("revision") or 0), "request_key": form.get("request_key") or "",
                "choices": {**({"all": form["all"]} if form.get("all") else {}), "lines": lines}}
        try:
            listed = (await api.peer_holds(token)).get("items") or []
            label = next((i.get("label") for i in listed if i.get("id") == item_id), None) or EMPTY
            await api.post_peer_hold_funds(token, item_id, body)
        except api.APIError as exc:
            return await sync_accounts_body(token, lang, refusal_text(exc.detail), kept={item_id: form})
        return html_response(await sync_accounts_body(token, lang),
                             headers=toast_header(t("sync_accounts.posted", lang, record=label), "success"))

    @app.post(PAGE + "/added/{item_id}")
    async def sync_accounts_added(request: Request, item_id: str):
        """Keep, archive or remove an account sync added."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        action = (await request.form()).get("action") or ""
        try:
            listed = (await api.peer_holds(token)).get("added") or []
            label = next((a.get("label") for a in listed if a.get("id") == item_id), None) or EMPTY
            await api.settle_added_account(token, item_id, action)
        except api.APIError as exc:
            return await sync_accounts_body(token, lang, refusal_text(exc.detail))
        message = t(f"sync_accounts.{action}_done", lang, account=label)
        return html_response(await sync_accounts_body(token, lang), headers=toast_header(message, "success"))

    @app.post(PAGE + "/review")
    async def sync_accounts_review(request: Request):
        """Save the review answers; Confirm accounts also ends the review."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        finish = bool(form.get("finish"))
        body = {"source_company_id": form.get("source_company_id") or "", "finish": finish,
                "answers": _answers(form)}
        try:
            await api.answer_peer_review(token, body)
        except api.APIError as exc:
            return await sync_accounts_body(token, lang, refusal_text(exc.detail))
        message = t("sync_accounts.review_done" if finish else "sync_accounts.review_saved", lang)
        return html_response(await sync_accounts_body(token, lang), headers=toast_header(message, "success"))

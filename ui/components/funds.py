# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The fund controls of a document that came from outside, in a company with funds in use:
"Fund for this document" in its header, a Fund cell on each line and "Set all outside
lines to" above the lines. Each is a click-to-edit field over the searchable fund select;
a save answers with every control again, so the others follow the one that changed."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from fasthtml.common import *

import ui.api_client as api
from celerp.services.journal_accounts import funds_enabled
from celerp.services.peer_lines import UNRESTRICTED, origin_key, outside_fund_needs
from celerp.services.permissions import role_has_permission
from ui.api_client import APIError
from ui.components.attrs import hx_vals
from ui.components.encode import js
from ui.components.icons import icon
from ui.components.table import EMPTY, searchable_select
from ui.i18n import t

DOCUMENT = "document"  # the target naming the document's own choice
ALL = "all"  # the target naming Set all outside lines


def fund_options(funds: list[dict]) -> list[tuple[str, str]]:
    """What a fund may be chosen from: Unrestricted, then every active fund."""
    return [(UNRESTRICTED, t("label.unrestricted")),
            *((f["id"], f["name"]) for f in funds if not f.get("archived"))]


def chosen_fund(value) -> str | None:
    """A fund select's value as a line's ``fund_id``: none for Unrestricted or no choice."""
    value = str(value or "").strip()
    return None if value in ("", UNRESTRICTED) else value


async def funds_in_use(token: str, settings: dict) -> list[dict] | None:
    """The company's funds when it has funds turned on, so a line form shows a Fund
    choice; None when it has not."""
    return (await api.list_funds(token)).get("items") or [] if funds_enabled(settings) else None


def origin_badge(origin) -> FT:
    """The "From <source>" tag of a record or line that came from outside."""
    return Span(t("fund.from_source", source=t(origin_key(origin))), cls="badge badge--origin")


@dataclass(frozen=True)
class FundView:
    """What the fund controls of one document show: its funds (archived ones too, so a
    stored choice still reads by name), its own choice, and whether they take a choice."""
    doc: dict
    funds: list[dict]
    editable: bool

    @classmethod
    def of(cls, doc: dict, funds: list[dict], settings: dict, role: str) -> "FundView":
        """The view a user with ``role`` has. The controls take a choice until the record posts:
        a draft, or an accounting app's open document, which posts nothing of its own until a
        local payment; the server refuses a record that has posted."""
        return cls(doc, funds, editable=role_has_permission(settings, role, "edit_documents")
                   and (doc.get("status") == "draft" or doc.get("origin") == "accounting_system"))

    @property
    def entity_id(self) -> str:
        return self.doc["entity_id"]

    def name(self, value) -> str:
        if not value:
            return EMPTY
        if value == UNRESTRICTED:
            return t("label.unrestricted")
        return next((f["name"] for f in self.funds if f["id"] == value), value)

    def url(self, target: str, *, edit: bool = False) -> str:
        return f"/docs/{self.entity_id}/funds/{target}" + ("/edit" if edit else "")

    def line(self, target: str) -> dict | None:
        return next((li for li in self.doc.get("line_items") or []
                     if isinstance(li, dict) and li.get("line_id") == target), None)

    def chosen(self, target: str):
        """The choice stored for ``target``: the document's, or a line's own."""
        if target == DOCUMENT:
            return self.doc.get("fund_choice")
        return (self.line(target) or {}).get("fund_choice")

    def waiting(self) -> bool:
        """Whether a line from outside has no choice of its own or the document's."""
        return any(n is not None for n in outside_fund_needs(self.doc))


def shows_funds(doc: dict) -> bool:
    """Whether ``doc`` came from outside or carries a line that did."""
    return bool(doc.get("origin")) or any(isinstance(li, dict) and li.get("origin")
                                          for li in doc.get("line_items") or [])


def _field(view: FundView, target: str, dom_id: str, *content, oob: bool = False) -> FT:
    """A control's display state: click to edit when the document takes a choice."""
    attrs = dict(hx_get=view.url(target, edit=True), hx_target="this", hx_swap="outerHTML",
                 hx_trigger="click", title=t("documents.click_to_edit"), cls="editable-cell") if view.editable else {}
    return Div(*content, id=dom_id, **attrs, **({"hx_swap_oob": "true"} if oob else {}))


def document_field(view: FundView, *, oob: bool = False) -> FT:
    """The value of "Fund for this document", with where the document came from, or that its
    connection's standing default chose it."""
    origin = view.doc.get("origin")
    tag = (Span(t("fund.from_default", source=t(origin_key(origin))), cls="badge badge--origin")
           if str(view.doc.get("fund_choice_source") or "").startswith("default:") else origin_badge(origin))
    return _field(view, DOCUMENT, "doc-fund", Span(view.name(view.doc.get("fund_choice"))), " ", tag, oob=oob)


def document_meta(view: FundView) -> FT:
    return Div(Div(t("fund.document_label"), cls="meta-label"), document_field(view), cls="meta-cell")


def _local_select(view: FundView, li: dict) -> FT:
    """A local draft line's fund, picked in the line editor's row and saved with its lines.
    An archived fund the line still carries stays offered, so it reads by name."""
    value = li.get("fund_id") or UNRESTRICTED
    options = fund_options(view.funds)
    if value not in dict(options):
        options.append((value, view.name(value)))
    return searchable_select("fund_id", options, value=value, placeholder=EMPTY, aria_label=t("th.fund"),
                             cls_extra="cell-input cell-input--xs", onchange="celerpAutoSave()")


def line_cell(view: FundView, li: dict, *, oob: bool = False) -> FT:
    """A line's Fund cell: on a line from outside its own choice, -- until one is made (and,
    muted, the document's fund it uses meanwhile); on a local line the fund it carries, picked
    in the row while the document is a draft. A credit note line takes the fund of the line it
    credits, so it only reads."""
    line_id = li.get("line_id") or ""
    if not li.get("origin"):  # a line not saved yet has no id to be found by
        picks = view.editable and view.doc.get("status") == "draft" and not li.get("source_line_id")
        return Div(_local_select(view, li) if picks else view.name(li.get("fund_id") or UNRESTRICTED),
                   **({"id": f"fund-cell-{line_id}"} if line_id else {}), **({"hx_swap_oob": "true"} if oob else {}))
    own, document = li.get("fund_choice"), view.doc.get("fund_choice")
    uses = Span(t("fund.uses", fund=view.name(document)), cls="text-muted") if document and not own else None
    return _field(view, line_id, f"fund-cell-{line_id}", Span(view.name(own)), " ", origin_badge(li.get("origin")),
                  *((" ", uses) if uses else ()), oob=oob)


def _save_attrs(view: FundView) -> dict:
    # The page's version pins the choice to the document it shows; a page without the line
    # editor's version uses the one it was drawn from.
    version = view.doc.get("version")
    return dict(hx_post=f"/docs/{view.entity_id}/funds", hx_target="this", hx_swap="outerHTML",
                hx_vals=f"js:{{expected_version: window._celerpEntityVersion ?? {int(version or 0)}}}")


def set_all(view: FundView, *, value: str = "", error: str | None = None, oob: bool = False) -> FT:
    """"Set all outside lines to", while a line from outside waits; an empty holder otherwise."""
    content = ()
    if view.editable and view.waiting():
        content = (Form(
            Label(t("fund.set_all_outside"), cls="form-label"),
            searchable_select("value", fund_options(view.funds), value=value, placeholder=EMPTY,
                              aria_label=t("fund.set_all_outside")),
            Input(type="hidden", name="target", value=ALL),
            Button(t("btn.apply"), type="submit", cls="btn btn--secondary btn--sm"),
            Span(error, cls="field-error", role="alert") if error else None,
            cls="fund-set-all gap-sm", **_save_attrs(view)),)
    return Div(*content, id="fund-set-all", **({"hx_swap_oob": "true"} if oob else {}))


def editor(view: FundView, target: str, *, value: str | None = None, error: str | None = None) -> FT:
    """The fund select of ``target``, saving on change; Escape puts the display back. After a
    refusal it keeps the selection, with the reason and Try again."""
    dom_id = "doc-fund" if target == DOCUMENT else f"fund-cell-{target}"
    current = view.chosen(target) if value is None else value
    restore = js("if(event.key==='Escape'){{htmx.ajax('GET',{0},{{target:'#'+{1},swap:'outerHTML'}});}}",
                 view.url(target), dom_id)
    return Form(
        searchable_select("value", fund_options(view.funds), value=current or "", placeholder=EMPTY,
                          aria_label=t("fund.document_label") if target == DOCUMENT else t("th.fund"),
                          autofocus=True),
        Input(type="hidden", name="target", value=target),
        *((Span(error, cls="field-error", role="alert"), " ",
           Button(t("btn.try_again"), type="submit", cls="btn btn--ghost btn--xs")) if error else ()),
        id=dom_id, cls="editable-cell editable-cell--editing", onkeydown=restore,
        hx_trigger="change, submit", **_save_attrs(view))


def saved(view: FundView, target: str) -> tuple:
    """The answer to a save: the control that was saved, then every other one out of band."""
    lines = [li for li in view.doc.get("line_items") or [] if isinstance(li, dict) and li.get("line_id")]
    pieces = {DOCUMENT: lambda oob: document_field(view, oob=oob), ALL: lambda oob: set_all(view, oob=oob),
              **{li["line_id"]: (lambda oob, li=li: line_cell(view, li, oob=oob)) for li in lines}}
    if not view.doc.get("origin"):
        pieces.pop(DOCUMENT)
    main = pieces.pop(target)(False) if target in pieces else ""
    return (main, *(piece(True) for piece in pieces.values()))


# The origins a connection's page in Settings > Connectors holds a default for: the Celerp
# connection holds connected companies, clients and the accountant; an accounting app
# (origin accounting_system) holds its own, keyed by its platform.
_CELERP_ORIGINS = ("connected_company", "client", "accountant")


def connection_defaults_of(items: list[dict], platform: str) -> list[dict]:
    """The connections whose standing fund the page of ``platform`` shows."""
    if platform == "celerp":
        return [i for i in items if i.get("origin") in _CELERP_ORIGINS]
    return [i for i in items if i.get("origin") == "accounting_system" and i.get("source_key") == platform]


@dataclass(frozen=True)
class DefaultsView:
    """What the standing funds on one connection's page show: the funds (archived ones too,
    so a stored choice reads by name), the users by id (who set each default) and whether
    this user may change them."""
    platform: str
    funds: list[dict]
    users: dict[str, str]
    editable: bool

    def url(self, item: dict, *, edit: bool = False) -> str:
        query = urlencode({"origin": item["origin"], "source_key": item["source_key"], **({"edit": 1} if edit else {})})
        return f"/settings/connectors/{self.platform}/fund-default?{query}"

    def name(self, value) -> str:
        return FundView({}, self.funds, False).name(value)

    def user(self, user_id) -> str:
        return self.users.get(user_id or "", EMPTY)


def _default_id(item: dict) -> str:
    return f"fund-default-{item['origin']}-{re.sub(r'[^A-Za-z0-9_-]', '-', str(item['source_key']))}"


def _default_values(item: dict, **extra) -> dict:
    return {"origin": item["origin"], "source_key": item["source_key"],
            "expected_version": int(item.get("version") or 0), **extra}


def connection_default(view: DefaultsView, item: dict, *, saving: dict | None = None,
                       error: str | None = None) -> FT:
    """One connection's "Default fund for documents from <source>": the fund (click to edit
    with the fund permission), who set it and when, Clear while one is set, and Inactive when
    its setter can no longer choose funds. ``saving`` holds a refused save's form, kept in the
    editor with the reason and Try again."""
    dom_id = _default_id(item)
    label = Div(t("fund.default_label", source=item.get("label") or item["source_key"]), cls="meta-label")
    if saving is not None:
        restore = js("if(event.key==='Escape'){{htmx.ajax('GET',{0},{{target:'#'+{1},swap:'outerHTML'}});}}",
                     view.url(item), dom_id)
        return Div(label, Form(
            searchable_select("value", fund_options(view.funds), value=saving.get("value") or item.get("fund_choice") or "",
                              placeholder=EMPTY, aria_label=t("fund.default_label", source=item.get("label") or ""),
                              autofocus=True),
            *(Hidden(name=k, value=str(v)) for k, v in _default_values(
                item, request_key=saving.get("request_key") or uuid.uuid4().hex).items()),
            *((Span(error, cls="field-error", role="alert"), " ",
               Button(t("btn.try_again"), type="submit", cls="btn btn--ghost btn--xs")) if error else ()),
            cls="editable-cell editable-cell--editing", onkeydown=restore, hx_trigger="change, submit",
            hx_post=f"/settings/connectors/{view.platform}/fund-default", hx_target=f"#{dom_id}", hx_swap="outerHTML"),
            id=dom_id, cls="meta-cell")
    choice = item.get("fund_choice")
    value = (Div(Span(view.name(choice)), hx_get=view.url(item, edit=True), hx_target=f"#{dom_id}",
                 hx_swap="outerHTML", hx_trigger="click", title=t("documents.click_to_edit"), cls="editable-cell")
             if view.editable else Div(Span(view.name(choice))))
    notes = []
    if choice:
        notes.append(Span(t("fund.default_set_by", user=view.user(item.get("set_by")), date=(item.get("set_at") or "")[:10]),
                          cls="text-muted"))
        if not item.get("active"):
            notes.append(Span(t("fund.default_inactive", user=view.user(item.get("set_by"))), cls="badge badge--warning"))
    if not view.editable:
        notes.append(Span(t("fund.default_read_only"), cls="text-muted"))
    elif choice:
        notes.append(Button(t("btn.clear"), type="button", cls="btn btn--ghost btn--xs",
                            hx_post=f"/settings/connectors/{view.platform}/fund-default",
                            hx_vals=hx_vals(_default_values(item, value="", request_key=uuid.uuid4().hex)),
                            hx_target=f"#{dom_id}", hx_swap="outerHTML"))
    return Div(label, value, *(Div(n) for n in notes), id=dom_id, cls="meta-cell")


def connection_defaults_card(view: DefaultsView, items: list[dict]) -> FT | str:
    """The standing funds of a connection's page; nothing when it has no connection to hold one."""
    if not items:
        return ""
    return Div(H3(t("fund.defaults_title")), P(t("fund.defaults_hint"), cls="settings-hint"),
               *(connection_default(view, item) for item in items), cls="settings-card flex-col gap-sm")


async def fund_waiting_banner(token: str, settings: dict, role: str) -> FT | str:
    """The outside records waiting for a fund choice, for those who choose funds: Choose funds
    opens the documents to choose for, Sync accounts the held items. Shown on the dashboard
    and the fund reports; absent at 0, with funds off, and when the count cannot be read."""
    if not funds_enabled(settings) or not role_has_permission(settings, role, "manage_accounting"):
        return ""
    try:
        waiting = await api.get_fund_waiting(token)
    except APIError:
        return ""
    count = waiting.get("count") or 0
    if not count:
        return ""
    sync = [" ", A(t("sync_accounts.title"), href="/accounting/sync-accounts")] if waiting.get("held") else []
    return Div(Span(icon("info"), cls="info-banner-icon"),
               Span(t("fund.waiting.count_one") if count == 1 else t("fund.waiting.count", count=count), " ",
                    A(t("fund.waiting.link"), href="/docs?fund_choice=needed"), *sync),
               cls="info-banner", id="fund-waiting-banner")

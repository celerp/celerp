# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The fund controls of a document that came from outside, in a company with funds in use:
"Fund for this document" in its header, a Fund cell on each line and "Set all outside
lines to" above the lines. Each is a click-to-edit field over the searchable fund select;
a save answers with every control again, so the others follow the one that changed."""
from __future__ import annotations

from dataclasses import dataclass

from fasthtml.common import *

from celerp.services.peer_lines import UNRESTRICTED, origin_key, outside_fund_needs
from celerp.services.permissions import role_has_permission
from ui.components.encode import js
from ui.components.table import EMPTY, searchable_select
from ui.i18n import t

DOCUMENT = "document"  # the target naming the document's own choice
ALL = "all"  # the target naming Set all outside lines


def fund_options(funds: list[dict]) -> list[tuple[str, str]]:
    """What a fund may be chosen from: Unrestricted, then every active fund."""
    return [(UNRESTRICTED, t("label.unrestricted")),
            *((f["id"], f["name"]) for f in funds if not f.get("archived"))]


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
    """The value of "Fund for this document", with where the document came from."""
    return _field(view, DOCUMENT, "doc-fund", Span(view.name(view.doc.get("fund_choice"))), " ",
                  origin_badge(view.doc.get("origin")), oob=oob)


def document_meta(view: FundView) -> FT:
    return Div(Div(t("fund.document_label"), cls="meta-label"), document_field(view), cls="meta-cell")


def line_cell(view: FundView, li: dict, *, oob: bool = False) -> FT:
    """A line's Fund cell: on a line from outside its own choice, -- until one is made (and,
    muted, the document's fund it uses meanwhile); on a local line the fund it carries."""
    line_id = li.get("line_id") or ""
    if not li.get("origin"):  # a line not saved yet has no id to be found by
        return Div(view.name(li.get("fund_id") or UNRESTRICTED), **({"id": f"fund-cell-{line_id}"} if line_id else {}),
                   **({"hx_swap_oob": "true"} if oob else {}))
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

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings -> Side menu: the owner hides and reorders the company's menu entries and groups.

The choices live in ``company.settings["nav_overrides"]`` (see ``celerp.services.nav_overrides``)
and name entries and groups by stable ids, never by their labels. Hiding is about what the menu
offers: it does not take a page away from a role that may open it, and it cannot show an entry a
role lacks permission for. Settings is always shown.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, flash, module_nav, page_header, page_title
from ui.config import get_role as _get_role, get_token as _token
from ui.i18n import get_lang, t
from ui.nav_overrides import apply_nav_overrides, entry_id, group_id, protected
from ui.routes.settings_general import _section_breadcrumb
from celerp.services.nav_overrides import FIELDS, KEY
from celerp.services.permissions import role_has_permission

_EDITOR_ID = "menu-editor"


def _entries(settings: dict) -> list[dict]:
    """Every entry the company's modules offer, once per key, in the order the company chose (hidden
    ones included, so they can be shown again)."""
    seen: set[str] = set()
    items = []
    for item in module_nav(settings):
        ident = entry_id(item)
        if ident and ident not in seen:
            seen.add(ident)
            items.append(item)
    chosen = dict(settings.get(KEY) or {})
    chosen["hidden_entries"], chosen["hidden_groups"] = [], []
    return apply_nav_overrides(items, {KEY: chosen})


def _model(settings: dict) -> tuple[list[dict], list[tuple[str, str, list[dict]]]]:
    """The editor's rows: the top-level entries, then each group as (id, declared name, entries)."""
    items = _entries(settings)
    top = [i for i in items if not i.get("group")]
    groups: dict[str, tuple[str, list[dict]]] = {}
    for item in items:
        if item.get("group"):
            groups.setdefault(group_id(item["group"]), (item["group"], []))[1].append(item)
    return top, [(gid, name, members) for gid, (name, members) in groups.items()]


def _entry_label(item: dict) -> str:
    label_key = item.get("label_key")
    return t(label_key) if label_key else item.get("label", "") or entry_id(item)


def _group_label(gid: str, name: str) -> str:
    key = f"nav.group.{gid}"
    label = t(key)
    return name if label == key else label


def _move_buttons(kind: str, ident: str, first: bool, last: bool) -> FT:
    def button(direction: str, glyph, label_key: str, disabled: bool) -> FT:
        return Button(
            glyph, type="button", cls="btn btn--ghost btn--xs", disabled=disabled,
            title=t(label_key), aria_label=t(label_key),
            hx_post="/settings/menu/move", hx_vals={"kind": kind, "id": ident, "direction": direction},
            hx_target=f"#{_EDITOR_ID}", hx_swap="outerHTML")
    return Span(
        button("up", "↑", "settings_menu.move_up", first),
        button("down", "↓", "settings_menu.move_down", last),
        cls="menu-editor__move")


def _hide_button(kind: str, ident: str, hidden: bool) -> FT:
    return Button(
        t("settings_menu.show" if hidden else "settings_menu.hide"), type="button", cls="btn btn--secondary btn--xs",
        hx_post="/settings/menu/hide", hx_vals={"kind": kind, "id": ident, "hidden": "" if hidden else "1"},
        hx_target=f"#{_EDITOR_ID}", hx_swap="outerHTML")


def _entry_row(item: dict, hidden: set[str], first: bool, last: bool) -> FT:
    ident = entry_id(item)
    is_hidden = ident in hidden
    return Div(
        Span(_entry_label(item), cls="menu-editor__label"),
        Span(t("settings_menu.hidden_badge"), cls="menu-editor__badge") if is_hidden else "",
        Span(t("settings_menu.always_shown"), cls="menu-editor__badge") if protected(item)
        else _hide_button("entry", ident, is_hidden),
        _move_buttons("entry", ident, first, last),
        cls=f"menu-editor__row {'menu-editor__row--hidden' if is_hidden else ''}", data_entry=ident)


def _editor(settings: dict, notice: FT | str = "") -> FT:
    top, groups = _model(settings)
    chosen = settings.get(KEY) or {}
    hidden_entries, hidden_groups = set(chosen.get("hidden_entries") or []), set(chosen.get("hidden_groups") or [])
    sections: list[FT] = []
    if top:
        sections.append(Div(
            H3(t("settings_menu.main_menu"), cls="settings-section-title"),
            *[_entry_row(i, hidden_entries, n == 0, n == len(top) - 1) for n, i in enumerate(top)],
            cls="menu-editor__section"))
    for n, (gid, name, members) in enumerate(groups):
        is_hidden = gid in hidden_groups
        sections.append(Div(
            Div(
                H3(_group_label(gid, name), cls="settings-section-title"),
                Span(t("settings_menu.hidden_badge"), cls="menu-editor__badge") if is_hidden else "",
                _hide_button("group", gid, is_hidden),
                _move_buttons("group", gid, n == 0, n == len(groups) - 1),
                cls="menu-editor__group-head"),
            *[_entry_row(i, hidden_entries, k == 0, k == len(members) - 1) for k, i in enumerate(members)],
            cls=f"menu-editor__section {'menu-editor__section--hidden' if is_hidden else ''}", data_group=gid))
    return Div(
        notice,
        *sections,
        Div(
            Button(t("settings_menu.reset"), type="button", cls="btn btn--secondary",
                   hx_post="/settings/menu/reset", hx_target=f"#{_EDITOR_ID}", hx_swap="outerHTML",
                   hx_confirm=t("settings_menu.reset_confirm")),
            cls="mt-md"),
        id=_EDITOR_ID, cls="settings-card menu-editor")


def _current(chosen: dict) -> dict[str, list[str]]:
    return {field: list(chosen.get(field) or []) for field in FIELDS}


def _swap(ids: list[str], ident: str, direction: str) -> list[str]:
    ids = list(ids)
    at = ids.index(ident)
    to = at - 1 if direction == "up" else at + 1
    if 0 <= to < len(ids):
        ids[at], ids[to] = ids[to], ids[at]
    return ids


def _after(settings: dict, chosen: dict) -> dict:
    return {**settings, KEY: {k: v for k, v in chosen.items() if v}}


def setup_routes(app):

    async def _gate(request: Request):
        """(token, settings) for the owner, else a response to return."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302), None
        try:
            company = await api.get_company(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302), None
            return flash(str(e.detail)), None
        settings = company.get("settings") or {}
        if not role_has_permission(settings, _get_role(request), "manage_permissions"):
            return flash(t("settings_menu.owner_only")), None
        return token, settings

    async def _save(request: Request, change) -> FT:
        """Apply ``change(chosen, settings)`` to the saved choices and redraw the editor."""
        token, settings = await _gate(request)
        if settings is None:
            return token
        try:
            chosen = _current((await api.get_nav_overrides(token)))
            change(chosen, settings, await request.form())
            saved = await api.patch_nav_overrides(token, chosen)
        except APIError as e:
            return _editor(settings, flash(str(e.detail)))
        return _editor(_after(settings, saved))

    @app.get("/settings/menu")
    async def settings_menu_page(request: Request):
        token, settings = await _gate(request)
        if settings is None:
            if isinstance(token, RedirectResponse):
                return token
            body = token
        else:
            body = _editor(settings)
        lang = get_lang(request)
        return await base_shell(
            _section_breadcrumb("settings_menu.title"),
            page_header(t("settings_menu.title", lang)),
            P(t("settings_menu.hint", lang), cls="settings-hint"),
            body,
            title=page_title("settings_menu.title"),
            nav_active="settings",
            lang=lang,
            request=request,
        )

    @app.post("/settings/menu/hide")
    async def settings_menu_hide(request: Request):
        def change(chosen: dict, settings: dict, form) -> None:
            kind, ident, hide = str(form.get("kind")), str(form.get("id")), bool(form.get("hidden"))
            field = "hidden_groups" if kind == "group" else "hidden_entries"
            chosen[field] = [i for i in chosen[field] if i != ident] + ([ident] if hide else [])
        return await _save(request, change)

    @app.post("/settings/menu/move")
    async def settings_menu_move(request: Request):
        def change(chosen: dict, settings: dict, form) -> None:
            kind, ident, direction = str(form.get("kind")), str(form.get("id")), str(form.get("direction"))
            top, groups = _model(_after(settings, chosen))
            if kind == "group":
                gids = [g for g, _name, _members in groups]
                if ident in gids:
                    chosen["group_order"] = _swap(gids, ident, direction)
                return
            sequences = [[entry_id(i) for i in top]] + [[entry_id(i) for i in m] for _g, _n, m in groups]
            moved = [_swap(ids, ident, direction) if ident in ids else ids for ids in sequences]
            chosen["entry_order"] = [i for ids in moved for i in ids]
        return await _save(request, change)

    @app.post("/settings/menu/reset")
    async def settings_menu_reset(request: Request):
        token, settings = await _gate(request)
        if settings is None:
            return token
        try:
            await api.reset_nav_overrides(token)
        except APIError as e:
            return _editor(settings, flash(str(e.detail)))
        return _editor({k: v for k, v in settings.items() if k != KEY})

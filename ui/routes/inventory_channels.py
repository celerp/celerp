# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An item's sales channels: its state on each connected store and the actions on it.

One block per item and channel shows where the product stands in that store (or ``--``
when it is not linked) with the actions that apply: Publish, Unpublish, Stop and Resume.
The same block sits in the catalog's Channels column and on the item's channel page, and
every action swaps it in place. Publishing an item whose SKU the store already has asks
first, in the block itself, before the store product is linked."""
from __future__ import annotations

import re
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import ui.api_client as api
from celerp.services.permissions import role_has_permission
from ui.api_client import APIError
from ui.config import get_role as _get_role, get_token as _token
from ui.i18n import get_lang, refusal_text, t

ACTIONS = ("publish", "unpublish", "stop", "resume")


def channel_for(platform: str) -> dict | None:
    """The catalog_channel contribution for ``platform``, if a running module offers it."""
    from celerp.modules.slots import get as get_slot
    return next((c for c in get_slot("catalog_channel") if c.get("id") == platform), None)


def channel_status(channel: dict, state: dict) -> tuple[str | None, tuple[str, ...]]:
    """What the block says about the item in the store (None when it is not linked) and
    the actions that apply to it."""
    publishable = bool(channel.get("can_create"))
    if state.get("ambiguous"):
        return t("inventory.channel_ambiguous"), ()
    if not state.get("linked"):
        return None, ("publish",) if publishable else ()
    if state.get("remote_deleted"):
        return t("inventory.channel_missing"), ("publish",) if publishable else ()
    if not state.get("enabled"):
        return t("inventory.channel_stopped"), ("resume",)
    remote = state.get("remote_status")
    if remote == "publish":
        return t("inventory.channel_published"), ("unpublish", "stop")
    if remote == "draft":
        return t("inventory.channel_draft"), ("publish", "stop")
    return t("inventory.channel_syncing"), ("stop",)


def _block_id(entity_id: str, platform: str) -> str:
    return f"channel-{re.sub(r'[^a-zA-Z0-9]', '-', entity_id)}-{platform}"


def _action_url(entity_id: str, platform: str, action: str) -> str:
    return f"/api/items/{quote(entity_id, safe='')}/channel-sync/{platform}/{action}"


def channel_block(entity_id: str, channel: dict, state: dict, *, can_write: bool,
                  message: str | None = None, confirm: str | None = None) -> FT:
    """One channel's block for one item. ``confirm`` asks before linking a store product
    with the item's SKU; ``message`` explains why the last action did not apply."""
    platform = str(channel.get("id") or "")
    label = str(channel.get("label") or platform)
    block_id = _block_id(entity_id, platform)
    swap = {"hx_target": f"#{block_id}", "hx_swap": "outerHTML"}
    status, actions = channel_status(channel, state)
    if confirm:
        controls = [
            Span(confirm, cls="catalog-channel__confirm"),
            Button(t("inventory.channel_link"), type="button", cls="btn btn--primary btn--xs",
                   hx_post=_action_url(entity_id, platform, "publish"), hx_vals='{"confirm_link": "1"}', **swap),
            Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--xs",
                   hx_get=f"/api/items/{quote(entity_id, safe='')}/channel/{platform}", **swap),
        ]
    else:
        controls = [
            Button(t(f"inventory.channel_action_{action}"), type="button", cls="btn btn--secondary btn--xs",
                   hx_post=_action_url(entity_id, platform, action), **swap)
            for action in actions if can_write
        ]
    return Div(
        Span(label, cls="catalog-channel__label"),
        Span(status or "--", cls="catalog-channel__state"),
        *controls,
        *([Span(message, cls="flash flash--warning catalog-channel__message")] if message else []),
        id=block_id, cls="catalog-channel-block",
    )


def can_write_channel(channel: dict, settings: dict, role: str) -> bool:
    return role_has_permission(settings, role, channel.get("write_permission") or "adjust_inventory")


def channels_cell(entity_id: str, channels: list[dict], state_map: dict, settings: dict, role: str) -> FT:
    """The catalog's Channels cell: one block per channel the company is connected to."""
    return Td(
        Div(*[channel_block(entity_id, ch, state_map.get(str(ch.get("id"))) or {},
                            can_write=can_write_channel(ch, settings, role)) for ch in channels],
            cls="catalog-channels"),
        id=f"cell-{entity_id.replace(':', '-')}-_channels",
        cls="cell cell--channels", data_col="_channels",
    )


def _bulk_result(message: str, cls: str, *extra) -> FT:
    return Div(P(message, cls=f"flash {cls}"), *extra, id="bulk-action-result")


def _bulk_confirmation(platform: str, asks: list[dict]) -> FT:
    """The items whose SKU the store already has, each with its store product, and one
    Link all that publishes them by linking those products."""
    return Div(
        Ul(*[Li(a.get("message") or a.get("sku")) for a in asks]),
        Form(
            *[Input(type="hidden", name="selected", value=a["entity_id"]) for a in asks],
            Input(type="hidden", name="confirm_link", value="1"),
            Button(t("inventory.channel_link_all"), type="submit", cls="btn btn--primary btn--sm"),
            hx_post=f"/api/items/bulk/channel-sync/{platform}/publish",
            hx_target="#bulk-action-result", hx_swap="outerHTML",
        ),
        cls="catalog-channel__confirm",
    )


def setup_routes(app) -> None:

    async def _block(token: str, entity_id: str, channel: dict, role: str, **kw) -> FT:
        item, company = await api.get_item(token, entity_id), await api.get_company(token)
        state = (item.get("_channel_state") or {}).get(str(channel.get("id"))) or {}
        return channel_block(entity_id, channel, state,
                             can_write=can_write_channel(channel, company.get("settings") or {}, role), **kw)

    @app.get("/api/items/{entity_id}/channel/{platform}")
    async def item_channel_block(request: Request, entity_id: str, platform: str):
        """The item's block for one channel, as it stands now."""
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        channel = channel_for(platform)
        if channel is None:
            return Response("", status_code=404)
        try:
            return await _block(token, entity_id, channel, _get_role(request))
        except APIError as e:
            return Span(refusal_text(e.detail) or str(e.detail), cls="flash flash--error")

    @app.post("/api/items/bulk/channel-sync/{platform}/{action}")
    async def bulk_item_channel_sync(request: Request, platform: str, action: str):
        """Run one channel action on the selected items. Items whose SKU the store
        already has are listed with one Link all; the rest are done. Registered before
        the one-item route, whose {entity_id} would otherwise take "bulk"."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        channel = channel_for(platform)
        if channel is None or action not in ACTIONS:
            return Response("", status_code=404)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return _bulk_result(t("flash.no_items_selected"), "flash--warning")
        try:
            result = await api.set_connector_item_sync(
                token, platform, entity_ids, action,
                confirm_link=entity_ids if form.get("confirm_link") else None)
        except APIError as e:
            return _bulk_result(refusal_text(e.detail) or str(e.detail), "flash--error")
        asks = result.get("needs_confirmation") or []
        errors = result.get("errors") or []
        done = t("inventory.channel_bulk_done", n=result.get("updated", 0), service=channel.get("label"))
        extra = [*([P("; ".join(errors), cls="flash flash--warning")] if errors else []),
                 *([_bulk_confirmation(platform, asks)] if asks else [])]
        if extra:
            return _bulk_result(done, "flash--success", *extra)
        return HTMLResponse(to_xml(_bulk_result(done, "flash--success")), headers={"HX-Refresh": "true"})

    @app.post("/api/items/{entity_id}/channel-sync/{platform}/{action}")
    async def item_channel_sync(request: Request, entity_id: str, platform: str, action: str):
        """Run one channel action on one item and redraw its block. A store product with
        the item's SKU is linked only once the person confirms it in the block."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        channel = channel_for(platform)
        if channel is None or action not in ACTIONS:
            return Response("", status_code=404)
        form = await request.form()
        role = _get_role(request)
        try:
            result = await api.set_connector_item_sync(
                token, platform, [entity_id], action,
                confirm_link=[entity_id] if form.get("confirm_link") else None)
        except APIError as e:
            return await _block(token, entity_id, channel, role, message=refusal_text(e.detail) or str(e.detail))
        asks = result.get("needs_confirmation") or []
        errors = result.get("errors") or []
        return await _block(token, entity_id, channel, role,
                            confirm=asks[0]["message"] if asks else None,
                            message="; ".join(errors) or None)

    @app.get("/inventory/{entity_id}/channel/{platform}")
    async def item_channel_page(request: Request, entity_id: str, platform: str):
        """The item on one channel: where it stands in the store and what can be done."""
        from ui.components.shell import base_shell, page_header
        from ui.components.table import breadcrumbs

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        channel = channel_for(platform)
        item_href = f"/inventory/{quote(entity_id, safe='')}"
        if channel is None:
            return RedirectResponse(item_href, status_code=302)
        lang = get_lang(request)
        try:
            block = await _block(token, entity_id, channel, _get_role(request))
            item = await api.get_item(token, entity_id)
        except APIError as e:
            if e.status_code == 404:
                return RedirectResponse("/inventory", status_code=302)
            block, item = Span(refusal_text(e.detail) or str(e.detail), cls="flash flash--error"), {}
        name = item.get("name") or item.get("sku") or entity_id
        service = str(channel.get("label") or platform)
        return await base_shell(
            breadcrumbs([(t("nav.inventory", lang), "/inventory"), (name, item_href), (service, None)]),
            page_header(t("inventory.channel_page_title", lang, name=name, service=service),
                        A(t("inventory.back_to_item", lang), href=item_href, cls="btn btn--secondary btn--sm")),
            P(t("inventory.channel_page_hint", lang, service=service), cls="settings-hint"),
            Div(block, cls="settings-card"),
            title=f"{name} - {service}",
            nav_active="inventory",
            request=request,
        )

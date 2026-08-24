# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → Inventory: Locations, Category Library, Bulk Files, Import History."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, page_title, flash
from ui.components.table import EMPTY
from ui.components.cloud_gate import digest_upsell_modal

# Vertical tag groups in the category library. The module-level dict holds i18n
# KEYS, never English, so a non-English request gets translated group headings;
# labels resolve at render time via t("enum.vertical_tag.<tag>") (see _tag_label).
_VERTICAL_TAGS: tuple[str, ...] = (
    "agricultural", "artwork", "automotive", "books_media",
    "coins_precious_metals", "consulting", "cosmetics", "electronics",
    "fashion", "food_beverage", "furniture", "gems_jewelry", "hardware",
    "property_rental", "saas", "watches_accessories", "wine_spirits", "other",
)
_TAG_LABELS: dict[str, str] = {tag: f"enum.vertical_tag.{tag}" for tag in _VERTICAL_TAGS}
from ui.config import COOKIE_NAME
from ui.i18n import t, get_lang

from ui.routes.settings import (
    _token,
    _check_permission,
    _category_row,
    _locations_tab,
    _import_history_tab,
    _bulk_attach_tab,
    _cat_schema_display_cell,
    _load_cat_schema_sorted,
)
from ui.routes.settings_general import _section_breadcrumb


def _tag_label(tag: str) -> str:
    """Render-time display label for a vertical tag group; falls back to the raw
    tag when no catalog key is registered (a data value with no shipped label)."""
    key = _TAG_LABELS.get(tag)
    return t(key) if key else tag


def _inventory_tabs(active: str, lang: str = "en") -> FT:
    tabs: list[tuple[str, str]] = [
        ("locations", t("settings.tab_locations", lang)),
        ("categories", t("settings.tab_categories", lang)),
        ("units", t("page.units", lang)),
        ("reorder", t("settings_inventory.tab_reorder", lang)),
        ("bulk-attach", t("settings.tab_bulk_attach", lang)),
        ("import-history", t("settings.tab_import_history", lang)),
    ]
    return Div(
        *[
            A(label, href=f"/settings/inventory?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )




def _unit_display_cell(unit_name: str, field: str, value) -> FT:
    """Click-to-edit cell for a unit row field (label or decimals)."""
    display = str(value) if (value is not None and str(value).strip() != "") else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title=t("settings.click_to_edit"),
        hx_get=f"/settings/units/{unit_name}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _unit_type_select(name_attr: str, selected: str = "quantity") -> FT:
    """Reusable unit_type dropdown."""
    return Select(
        Option(t("enum.unit_type.weight"), value="weight", selected=(selected == "weight")),
        Option(t("enum.unit_type.pieces"), value="pieces", selected=(selected == "pieces")),
        Option(t("enum.unit_type.quantity"), value="quantity", selected=(selected == "quantity")),
        name=name_attr,
        cls="input-sm",
    )


def _units_tab(units: list[dict], from_import: str = "") -> FT:
    """Units settings tab - table of units with inline edit + add form."""
    rows = []
    for u in units:
        uname = u.get("name", "")
        utype = u.get("unit_type", "quantity")
        rows.append(Tr(
            Td(uname, cls="cell cell--mono"),
            _unit_display_cell(uname, "label", u.get("label", "")),
            _unit_display_cell(uname, "decimals", u.get("decimals", 0)),
            _unit_display_cell(uname, "unit_type", utype),
            Td(
                Button(
                    "✕",
                    cls="btn btn--danger btn--xs",
                    hx_delete=f"/settings/units/{uname}",
                    hx_confirm=t("settings_inventory.delete_unit_confirm", name=uname),
                    hx_swap="none",
                    hx_on__after_request="window.location.href='/settings/inventory?tab=units'",
                ),
                cls="cell",
            ),
            cls="data-row",
        ))

    add_row = Tr(
        Td(Input(name="name", placeholder=t("settings_inventory.unit_name_placeholder"), required=True, cls="input-sm",
                 pattern="[a-z0-9_]+", title=t("settings_inventory.unit_name_pattern_title")),
           cls="cell"),
        Td(Span(t("settings_inventory.unit_label_auto"), style="color:var(--c-text2);font-size:12px"), cls="cell"),
        Td(Input(name="decimals", type="number", min="0", max="6", value="0", cls="input-sm"), cls="cell"),
        Td(_unit_type_select("unit_type"), cls="cell"),
        Td(Button(t("btn._add"), type="submit", cls="btn btn--primary btn--xs"), cls="cell"),
        cls="data-row",
    )

    return_banner = (
        Div(
            Span(t("settings_inventory.unit_added_prompt"), style="font-size:13px;"),
            A(t("settings_inventory.return_to_import"), href="javascript:window.close()",
              cls="btn btn--secondary btn--sm"),
            cls="settings-card",
            style="display:flex;align-items:center;gap:10px;padding:8px 14px;margin-bottom:8px;",
        )
        if from_import else ""
    )

    return Div(
        return_banner,
        H3(t("page.units"), cls="settings-section-title"),
        P(t("inv.configure_measurement_units_available_for_inventor"), cls="settings-hint"),
        Form(
            Table(
                Thead(Tr(Th(t("th.name")), Th(t("th.label")), Th(t("th.decimals")), Th(t("th.type")), Th(""))),
                Tbody(*rows),
                Tfoot(add_row),
                cls="data-table",
            ),
            hx_post="/settings/units/add",
            hx_swap="none",
            hx_on__after_request="window.location.href='/settings/inventory?tab=units'",
        ),
        P(t("settings_inventory.unit_name_hint"), cls="form-hint"),
        cls="settings-card",
    )


def _reorder_tab(company: dict, saved: bool = False, upsell: bool = False) -> FT:
    """Reorder-alert preferences: enable the daily low-stock digest and opt into email."""
    _enabled = company.get("reorder_alerts_enabled")
    alerts_enabled = True if _enabled is None else bool(_enabled)
    email_enabled = bool(company.get("reorder_alert_email"))
    _method = str(company.get("inventory_method") or "fifo").lower()
    _method_opts = [("fifo", t("enum.inventory_method.fifo")),
                    ("fefo", t("enum.inventory_method.fefo")),
                    ("lifo", t("enum.inventory_method.lifo"))]
    return Div(
        flash(t("settings_inventory.settings_saved"), "success") if saved else "",
        Form(
            H3(t("settings_inventory.stock_cutting_method"), cls="settings-section-title"),
            P(t("settings_inventory.stock_cutting_hint"), cls="settings-hint"),
            Div(
                Label(t("settings_inventory.default_method"), For="inventory_method", cls="form-label"),
                Select(
                    *[Option(lbl, value=val, selected=(val == _method)) for val, lbl in _method_opts],
                    name="inventory_method", id="inventory_method", cls="form-input input--medium",
                ),
                cls="form-group",
            ),
            P(t("settings_inventory.lifo_gaap_note"),
              cls="settings-hint"),
            H3(t("settings_inventory.reorder_alerts"), cls="settings-section-title mt-lg"),
            P(t("settings_inventory.reorder_alerts_hint"),
              cls="settings-hint"),
            Div(
                Label(
                    Input(type="checkbox", name="reorder_alerts_enabled", value="1", checked=alerts_enabled),
                    Span(t("settings_inventory.notify_reorder_point")),
                    cls="settings-toggle",
                ),
                cls="form-group",
            ),
            Div(
                Label(
                    Input(type="checkbox", name="reorder_alert_email", value="1", checked=email_enabled),
                    Span(t("settings_inventory.email_digest_owner")),
                    cls="settings-toggle",
                ),
                cls="form-group",
            ),
            Div(Button(t("btn.save"), type="submit", cls="btn btn--primary"), cls="form-actions"),
            method="post", action="/settings/inventory/reorder", cls="settings-card",
        ),
        P(t("settings_inventory.reorder_point_hint"), cls="settings-hint"),
        digest_upsell_modal() if upsell else "",
    )


def _categories_tab(
    cat_schemas: dict,
    cat_schemas_company: dict,
    vert_categories: list[dict],
    vert_presets: list[dict],
    cat: str = "",
    cat_display_names: dict | None = None,
) -> FT:
    """Categories settings tab - 3-section layout.

    When `cat` is set and present in cat_schemas: renders the field editor.
    Otherwise: renders the library view (Your Categories + Quick Setup + Browse Library).
    """
    from urllib.parse import quote as _q
    from collections import defaultdict as _dd

    applied_names = set(cat_schemas_company.keys())

    # ── Field editor mode ─────────────────────────────────────────────
    if cat:
        if cat not in cat_schemas:
            return Div(
                P(t("settings_inventory.category_not_found", cat=cat), cls="error-banner"),
                A(t("settings.cat_fields_back"), href="/settings/inventory?tab=categories",
                  cls="btn btn--secondary"),
                cls="settings-card",
            )

        enc = _q(cat, safe="")
        sorted_fields = _load_cat_schema_sorted(cat_schemas[cat])

        def _cat_row(idx: int, f: dict) -> FT:
            return Tr(
                _cat_schema_display_cell(cat, idx, "position", f),
                _cat_schema_display_cell(cat, idx, "label", f),
                _cat_schema_display_cell(cat, idx, "type", f),
                _cat_schema_display_cell(cat, idx, "required", f),
                _cat_schema_display_cell(cat, idx, "editable", f),
                _cat_schema_display_cell(cat, idx, "show_in_table", f),
                _cat_schema_display_cell(cat, idx, "options", f),
                Td(
                    Button("✕", cls="btn btn--danger btn--xs",
                           hx_delete=f"/settings/cat-schema/{enc}/{idx}",
                           hx_confirm=t("settings_inventory.delete_field_confirm", name=f.get('key', idx)),
                           hx_swap="none",
                           hx_on__after_request=f"window.location.href='/settings/inventory?tab=categories&cat={enc}'"),
                    cls="cell",
                ),
                cls="data-row",
            )

        add_row = Tr(
            Td(
                Button(t("btn.add_field"), cls="btn btn--secondary btn--xs",
                       hx_post=f"/settings/cat-schema/{enc}/add",
                       hx_swap="none",
                       hx_on__after_request=f"window.location.href='/settings/inventory?tab=categories&cat={enc}'"),
                colspan="9", cls="p-sm",
            ),
        )

        return Div(
            Div(
                A(t("settings.cat_fields_back"), href="/settings/inventory?tab=categories",
                  cls="btn btn--secondary btn--xs"),
                cls="mb-md",
            ),
            H3(t("settings_inventory.category_fields_heading", cat=cat), cls="settings-section-title"),
            P(t("settings_inventory.category_fields_hint", cat=cat), cls="settings-hint"),
            Table(
                Thead(Tr(Th("#"), Th(t("th.label")), Th(t("th.doc_type")),
                         Th(t("th.required")), Th(t("th.editable")), Th(t("th.show_in_table")),
                         Th(t("th.options")), Th(""))),
                Tbody(*[_cat_row(i, f) for i, f in enumerate(sorted_fields)], add_row),
                cls="data-table sticky-head",
            ),
            cls="settings-card",
        )

    # ── Library view ──────────────────────────────────────────────────

    # Shared result div targeted by Browse Library
    result_div = Div(id="verticals-apply-result")

    # Build preset lookup: preset name → preset object (preset name == vertical tag)
    # (kept for potential future use; actual tag→preset mapping is built in Section C)

    # ── Section A: Your Categories ────────────────────────────────────
    _dn = cat_display_names or {}
    if applied_names:
        applied_rows = [
            _category_row(name, _dn.get(name, name), len(cat_schemas.get(name, [])))
            for name in sorted(applied_names)
        ]
        add_row = Tr(
            Td(
                Form(
                    Input(type="text", name="new_category_name",
                          placeholder=t("settings.new_category_name"),
                          cls="form-input form-input--sm cat-add-input"),
                    Button(t("settings.add_category"), type="submit", cls="btn btn--secondary btn--sm"),
                    hx_post="/settings/categories",
                    hx_target="#your-cats-section",
                    hx_swap="outerHTML",
                    cls="cat-add-form",
                ),
                colspan="3", cls="cell",
            ),
        )
        your_cats_body = Table(
            Thead(Tr(Th(t("th.category")), Th(t("page.fields"), cls="th--center your-cats-fields"), Th("", cls="th--action your-cats-action"))),
            Tbody(*applied_rows, add_row),
            cls="data-table your-cats-table",
        )
    else:
        your_cats_body = Div(
            P(t("settings.no_categories_applied"), cls="settings-hint"),
            Form(
                Input(type="text", name="new_category_name",
                      placeholder=t("settings.new_category_name"),
                      cls="form-input form-input--sm cat-add-input"),
                Button(t("settings.add_category"), type="submit", cls="btn btn--secondary btn--sm"),
                hx_post="/settings/categories",
                hx_target="#your-cats-section",
                hx_swap="outerHTML",
                cls="cat-add-form",
            ),
        )

    section_a = Div(
        H3(t("settings.your_categories"), cls="settings-section-title"),
        P(t("settings.categories_hint"), cls="settings-hint"),
        your_cats_body,
        cls="mb-xl",
        id="your-cats-section",
    )

    # ── Section C: Browse & Add Categories ───────────────────────────
    if vert_categories:
        groups: dict[str, list[dict]] = _dd(list)
        for vc in sorted(vert_categories, key=lambda c: c.get("display_name", "")):
            tag = (vc.get("vertical_tags") or ["other"])[0]
            groups[tag].append(vc)

        # Build tag → preset map dynamically from what tags each preset's categories cover
        # (preset name may differ from tag, e.g. preset "gemstones" covers tag "gems_jewelry")
        tag_to_preset: dict[str, dict] = {}
        cat_tag_map: dict[str, str] = {vc.get("name", ""): (vc.get("vertical_tags") or ["other"])[0]
                                       for vc in vert_categories}
        for p in vert_presets:
            for cname_in_preset in (p.get("categories") or []):
                vtag = cat_tag_map.get(cname_in_preset)
                if vtag and vtag not in tag_to_preset:
                    tag_to_preset[vtag] = p

        group_sections = []
        for tag in sorted(groups.keys(), key=_tag_label):
            cats_in_group = groups[tag]
            rows = []
            for vc in cats_in_group:
                cname = vc.get("name", "")
                cdisplay = vc.get("display_name", cname)
                already = cdisplay in applied_names or cname in applied_names
                rows.append(Tr(
                    Td(cdisplay, cls="cell"),
                    Td(
                        Span(t("settings._applied"), cls="badge badge--active") if already else
                        Form(
                            Input(type="hidden", name="name", value=cname),
                            Button(t("btn._add"), type="submit", cls="btn btn--primary btn--xs"),
                            hx_post="/settings/verticals/apply-category",
                            hx_target="#verticals-apply-result",
                            hx_swap="outerHTML",
                            hx_on__after_request="window.location.href='/settings/inventory?tab=categories'",
                        ),
                        cls="cell cell--action",
                    ),
                    cls="data-row vert-cat-row",
                    data_name=cdisplay.lower(),
                ))
            group_sections.append(
                Details(
                    Summary(
                        Span(_tag_label(tag), cls="vert-group-label"),
                        *(
                            [Form(
                                Input(type="hidden", name="vertical", value=tag_to_preset[tag].get("name", "")),
                                Button(t("btn.apply_preset"), type="submit",
                                       cls="btn btn--secondary btn--xs vert-preset-btn",
                                       onclick="event.stopPropagation()"),
                                hx_post="/settings/verticals/apply-preset",
                                hx_target="#verticals-apply-result",
                                hx_swap="outerHTML",
                                hx_on__after_request="window.location.href='/settings/inventory?tab=categories'",
                            )]
                            if tag in tag_to_preset else []
                        ),
                        cls="vert-group-heading",
                    ),
                    Table(
                        Thead(Tr(Th(t("th.category")), Th("", cls="th--action"))),
                        Tbody(*rows),
                        cls="data-table vert-cat-table",
                    ),
                    cls="vert-group",
                )
            )

        browse_content = Div(
            P(t("settings.browse_library_hint"), cls="settings-hint"),
            Input(type="text", id="cat-search", placeholder=t("settings_inventory.search_categories_placeholder"),
                  oninput="filterCats(this.value)", cls="form-input form-input--sm",
                  style="max-width:280px;margin-bottom:12px"),
            *group_sections,
            Script("""function filterCats(q) {
  q = q.toLowerCase();
  document.querySelectorAll('.vert-cat-row').forEach(function(row) {
    row.style.display = row.dataset.name.includes(q) ? '' : 'none';
  });
  document.querySelectorAll('.vert-group').forEach(function(g) {
    var vis = Array.from(g.querySelectorAll('.vert-cat-row')).some(r => r.style.display !== 'none');
    g.style.display = vis ? '' : 'none';
  });
}"""),
        )
    else:
        browse_content = Div(P(t("settings_inventory.no_library_categories"), cls="settings-hint"))

    section_c = Details(
        Summary(t("settings.browse_library")),
        browse_content,
        cls="cat-section-details",
    )

    return Div(
        result_div,
        section_a,
        section_c,
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/inventory")
    async def settings_inventory_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_module_settings")):
            return r
        tab = request.query_params.get("tab", "locations")
        cat = request.query_params.get("cat", "")
        from_import = request.query_params.get("from_import", "")

        # Backward-compat redirects for old tab names
        if tab in {"category-library", "verticals", "schema"}:
            dest = "/settings/inventory?tab=categories"
            if cat:
                dest += f"&cat={cat}"
            return RedirectResponse(dest, status_code=302)

        try:
            locations = (await api.get_locations(token)).get("items", [])
            import_batches = (await api.list_import_batches(token)).get("batches", [])
            cat_schemas = await api.get_all_category_schemas(token)
            if tab == "categories":
                cat_schemas_company = await api.get_company_category_schemas(token)
                cat_display_names = await api.get_category_display_names(token)
                if not cat:
                    vert_categories = await api.list_verticals_categories(token)
                    vert_presets = await api.list_verticals_presets(token)
                else:
                    vert_categories, vert_presets = [], []
            else:
                cat_schemas_company = {}
                cat_display_names = {}
                vert_categories, vert_presets = [], []
            units = await api.get_units(token) if tab == "units" else []
            company = await api.get_company(token) if tab == "reorder" else {}
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            locations, import_batches, cat_schemas, cat_schemas_company = [], [], {}, {}
            cat_display_names = {}
            vert_categories, vert_presets = [], []
            units = []
            company = {}

        lang = get_lang(request)

        if tab == "locations":
            content = _locations_tab(locations, lang=lang)
        elif tab == "categories":
            content = _categories_tab(cat_schemas, cat_schemas_company, vert_categories, vert_presets, cat, cat_display_names)
        elif tab == "units":
            content = _units_tab(units, from_import=from_import)
        elif tab == "reorder":
            content = _reorder_tab(
                company,
                saved=request.query_params.get("saved") == "1",
                upsell=request.query_params.get("upsell") == "1",
            )
        elif tab == "bulk-attach":
            content = _bulk_attach_tab()
        elif tab == "import-history":
            content = _import_history_tab(import_batches)
        else:
            content = _locations_tab(locations, lang=lang)
            tab = "locations"

        return await base_shell(
            _section_breadcrumb(t("nav.inventory", lang)),
            page_header(t("settings_inventory.page_header", lang)),
            _inventory_tabs(tab, lang=lang),
            content,
            title=page_title("page.settings"),
            nav_active="settings-inventory",
            lang=lang,
            request=request,
        )

    # ── Reorder alerts ────────────────────────────────────────────────

    @app.post("/settings/inventory/reorder")
    async def settings_inventory_reorder_save(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_module_settings")):
            return r
        form = await request.form()
        enabled = str(form.get("reorder_alerts_enabled") or "") in ("1", "on", "true")
        email = str(form.get("reorder_alert_email") or "") in ("1", "on", "true")
        method = str(form.get("inventory_method") or "fifo").lower()
        if method not in ("fifo", "fefo", "lifo"):
            method = "fifo"
        # Read the stored digest state before patching so an off->on flip can be
        # detected below. A failed read is treated as already-on: never nag.
        try:
            prior_email = bool((await api.get_company(token)).get("reorder_alert_email"))
        except APIError:
            prior_email = True
        try:
            await api.patch_company(token, {
                "reorder_alerts_enabled": enabled,
                "reorder_alert_email": email,
                "inventory_method": method,
            })
        except APIError:
            pass
        # A non-paid user turning the digest on for the first time gets a one-time
        # nudge toward hands-off Connect delivery. Paid = a live relay tunnel on a
        # paid tier (a free instance can be connected via a share). A status-read
        # failure skips the nudge and never blocks the save that already happened.
        try:
            status = await api.get_relay_status(token)
            paid = bool(status.get("connected")) and status.get("tier") not in (None, "", "free")
        except APIError:
            paid = True
        dest = "/settings/inventory?tab=reorder&saved=1"
        if email and not prior_email and not paid:
            dest += "&upsell=1"
        return RedirectResponse(dest, status_code=303)

    # ── Units CRUD ────────────────────────────────────────────────────

    @app.post("/settings/units/add")
    async def units_add(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        name = (str(form.get("name") or "")).strip().lower().replace(" ", "_")
        label = (str(form.get("label") or "")).strip() or name.capitalize()
        try:
            decimals = int(form.get("decimals") or 0)
        except (ValueError, TypeError):
            decimals = 0
        current = await api.get_units(token)
        unit_type = (str(form.get("unit_type") or "quantity")).strip()
        if unit_type not in {"weight", "pieces", "quantity"}:
            unit_type = "quantity"
        current.append({"name": name, "label": label, "decimals": decimals, "unit_type": unit_type})
        await api.patch_units(token, current)
        return RedirectResponse("/settings/inventory?tab=units", status_code=303)

    @app.delete("/settings/units/{name}")
    async def units_delete(name: str, request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        current = await api.get_units(token)
        updated = [u for u in current if u.get("name") != name]
        await api.patch_units(token, updated)
        return RedirectResponse("/settings/inventory?tab=units", status_code=303)

    @app.get("/settings/units/{name}/{field}/edit")
    async def units_field_edit(name: str, field: str, request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        units = await api.get_units(token)
        unit = next((u for u in units if u.get("name") == name), None)
        if unit is None:
            return Td(EMPTY, cls="cell")
        value = unit.get(field, "")
        if field == "decimals":
            inp = Input(
                name="value", type="number", min="0", max="6",
                value=str(value), cls="input-sm",
                hx_patch=f"/settings/units/{name}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur, keydown[key=='Enter']",
            )
        elif field == "unit_type":
            sel = _unit_type_select("value", selected=str(value))
            sel.attrs.update({
                "hx-patch": f"/settings/units/{name}/{field}",
                "hx-target": "closest td",
                "hx-swap": "outerHTML",
                "hx-include": "this",
                "hx-trigger": "change",
            })
            inp = sel
        else:
            inp = Input(
                name="value", type="text", value=str(value), cls="input-sm",
                hx_patch=f"/settings/units/{name}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur, keydown[key=='Enter']",
            )
        return Td(inp, cls="cell")

    @app.patch("/settings/units/{name}/{field}")
    async def units_field_patch(name: str, field: str, request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        raw = str(form.get("value") or "").strip()
        units = await api.get_units(token)
        updated = []
        for u in units:
            if u.get("name") == name:
                u = dict(u)
                if field == "decimals":
                    try:
                        u["decimals"] = int(raw)
                    except (ValueError, TypeError):
                        pass
                else:
                    u[field] = raw
            updated.append(u)
        await api.patch_units(token, updated)
        # Re-fetch to get clean value and return display cell
        fresh = await api.get_units(token)
        unit = next((u for u in fresh if u.get("name") == name), {})
        value = unit.get(field, "")
        return _unit_display_cell(name, field, value)

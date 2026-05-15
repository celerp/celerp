# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings → Inventory: Locations, Category Library, Bulk Files, Import History."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY

# Human-readable labels for vertical tag groups in the category library
_TAG_LABELS: dict[str, str] = {
    "agricultural": "Agricultural",
    "artwork": "Artwork",
    "automotive": "Automotive",
    "books_media": "Books & Media",
    "coins_precious_metals": "Coins & Precious Metals",
    "consulting": "Consulting",
    "cosmetics": "Cosmetics",
    "electronics": "Electronics",
    "fashion": "Fashion",
    "food_beverage": "Food & Beverage",
    "furniture": "Furniture",
    "gems_jewelry": "Gems & Jewelry",
    "hardware": "Hardware",
    "property_rental": "Property & Rental",
    "saas": "SaaS",
    "watches_accessories": "Watches & Accessories",
    "wine_spirits": "Wine & Spirits",
    "other": "Other",
}
from ui.config import COOKIE_NAME
from ui.i18n import t, get_lang

from ui.routes.settings import (
    _token,
    _check_role,
    _locations_tab,
    _import_history_tab,
    _bulk_attach_tab,
    _cat_schema_display_cell,
    _load_cat_schema_sorted,
)
from ui.routes.settings_general import _section_breadcrumb


def _inventory_tabs(active: str, lang: str = "en") -> FT:
    tabs: list[tuple[str, str]] = [
        ("locations", t("settings.tab_locations", lang)),
        ("categories", t("settings.tab_categories", lang)),
        ("units", "Units"),
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
    display = str(value) if (value is not None and str(value).strip() != "") else "—"
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/units/{unit_name}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _unit_type_select(name_attr: str, selected: str = "quantity") -> FT:
    """Reusable unit_type dropdown."""
    return Select(
        Option("Weight", value="weight", selected=(selected == "weight")),
        Option("Pieces", value="pieces", selected=(selected == "pieces")),
        Option("Quantity", value="quantity", selected=(selected == "quantity")),
        name=name_attr,
        cls="input-sm",
    )


def _units_tab(units: list[dict]) -> FT:
    """Units settings tab — table of units with inline edit + add form."""
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
                    hx_confirm=f"Delete unit '{uname}'?",
                    hx_swap="none",
                    hx_on__after_request="window.location.href='/settings/inventory?tab=units'",
                ),
                cls="cell",
            ),
            cls="data-row",
        ))

    add_form = Form(
        Tr(
            Td(Input(name="name", placeholder="piece", required=True, cls="input-sm"), cls="cell"),
            Td(Input(name="label", placeholder="Piece", required=True, cls="input-sm"), cls="cell"),
            Td(Input(name="decimals", type="number", min="0", max="6", value="0", cls="input-sm"), cls="cell"),
            Td(_unit_type_select("unit_type"), cls="cell"),
            Td(Button(t("btn._add"), type="submit", cls="btn btn--primary btn--xs"), cls="cell"),
            cls="data-row",
        ),
        hx_post="/settings/units/add",
        hx_swap="none",
        hx_on__after_request="window.location.href='/settings/inventory?tab=units'",
    )

    return Div(
        H3(t("page.units"), cls="settings-section-title"),
        P(t("inv.configure_measurement_units_available_for_inventor"), cls="settings-hint"),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.label")), Th(t("th.decimals")), Th("Type"), Th(""))),
            Tbody(*rows, add_form),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _categories_tab(
    cat_schemas: dict,
    vert_categories: list[dict],
    vert_presets: list[dict],
    cat: str = "",
) -> FT:
    """Categories settings tab - 3-section layout.

    When `cat` is set and present in cat_schemas: renders the field editor.
    Otherwise: renders the library view (Your Categories + Quick Setup + Browse Library).
    """
    from urllib.parse import quote as _q
    from collections import defaultdict as _dd

    applied_names = set(cat_schemas.keys())

    # ── Field editor mode ─────────────────────────────────────────────
    if cat:
        if cat not in cat_schemas:
            return Div(
                P(f"Category '{cat}' not found.", cls="error-banner"),
                A(t("settings.cat_fields_back"), href="/settings/inventory?tab=categories",
                  cls="btn btn--secondary"),
                cls="settings-card",
            )

        enc = _q(cat, safe="")
        sorted_fields = _load_cat_schema_sorted(cat_schemas[cat])

        def _cat_row(idx: int, f: dict) -> FT:
            return Tr(
                _cat_schema_display_cell(cat, idx, "position", f),
                _cat_schema_display_cell(cat, idx, "key", f),
                _cat_schema_display_cell(cat, idx, "label", f),
                _cat_schema_display_cell(cat, idx, "type", f),
                _cat_schema_display_cell(cat, idx, "required", f),
                _cat_schema_display_cell(cat, idx, "editable", f),
                _cat_schema_display_cell(cat, idx, "show_in_table", f),
                _cat_schema_display_cell(cat, idx, "options", f),
                Td(
                    Button("✕", cls="btn btn--danger btn--xs",
                           hx_delete=f"/settings/cat-schema/{enc}/{idx}",
                           hx_confirm=f"Delete field '{f.get('key', idx)}'?",
                           hx_swap="none",
                           hx_on__after_request=f"window.location.href='/settings/inventory?tab=categories&cat={enc}'"),
                    cls="cell",
                ),
                cls="data-row",
            )

        add_row = Tr(
            Td(colspan="9", cls="p-sm", children=[
                Button(t("btn.add_field"), cls="btn btn--secondary btn--xs",
                       hx_post=f"/settings/cat-schema/{enc}/add",
                       hx_swap="none",
                       hx_on__after_request=f"window.location.href='/settings/inventory?tab=categories&cat={enc}'"),
            ]),
        )

        return Div(
            Div(
                A(t("settings.cat_fields_back"), href="/settings/inventory?tab=categories",
                  cls="btn btn--secondary btn--xs"),
                cls="mb-md",
            ),
            H3(f"{cat} Fields", cls="settings-section-title"),
            P(f"Attribute fields for the '{cat}' category. Click a cell to edit.", cls="settings-hint"),
            Table(
                Thead(Tr(Th("#"), Th(t("th.key")), Th(t("th.label")), Th(t("th.doc_type")),
                         Th(t("th.required")), Th(t("th.editable")), Th(t("th.show_in_table")),
                         Th(t("th.options")), Th(""))),
                Tbody(*[_cat_row(i, f) for i, f in enumerate(sorted_fields)], add_row),
                cls="data-table",
            ),
            cls="settings-card",
        )

    # ── Library view ──────────────────────────────────────────────────

    # Shared result div targeted by both Quick Setup and Browse Library
    result_div = Div(id="verticals-apply-result")

    # ── Section A: Your Categories ────────────────────────────────────
    if applied_names:
        applied_rows = [
            Tr(
                Td(name, cls="cell"),
                Td(str(len(cat_schemas.get(name, []))), cls="cell"),
                Td(
                    A(t("settings.edit"),
                      href=f"/settings/inventory?tab=categories&cat={_q(name, safe='')}",
                      cls="btn btn--secondary btn--xs"),
                    # TODO: add Remove button once DELETE /settings/verticals/remove-category endpoint exists
                    cls="cell",
                ),
                cls="data-row",
            )
            for name in sorted(applied_names)
        ]
        your_cats_body = Table(
            Thead(Tr(Th(t("th.category")), Th("Fields"), Th(""))),
            Tbody(*applied_rows),
            cls="data-table",
        )
    else:
        your_cats_body = P(t("settings.no_categories_applied"), cls="settings-hint")

    section_a = Div(
        H3(t("settings.your_categories"), cls="settings-section-title"),
        P(t("settings.categories_hint"), cls="settings-hint"),
        your_cats_body,
        cls="mb-xl",
    )

    # ── Section B: Quick Setup ────────────────────────────────────────
    preset_cards = []
    for p in vert_presets:
        pname = p.get("name", "")
        pdisplay = p.get("display_name", pname)
        n_cats = len(p.get("categories", []))
        preset_cards.append(
            Div(
                Div(
                    Strong(pdisplay, cls="vert-preset-name"),
                    Span(f"{n_cats} categories", cls="vert-preset-count"),
                    cls="vert-preset-info",
                ),
                Form(
                    Input(type="hidden", name="vertical", value=pname),
                    Button(t("btn.apply_preset"), type="submit", cls="btn btn--secondary btn--xs"),
                    hx_post="/settings/verticals/apply-preset",
                    hx_target="#verticals-apply-result",
                    hx_swap="outerHTML",
                    hx_on__after_request="window.location.href='/settings/inventory?tab=categories'",
                ),
                cls="vert-preset-card",
            )
        )

    section_b = Details(
        Summary(t("settings.quick_setup")),
        Div(
            P(t("settings.quick_setup_hint"), cls="settings-hint"),
            Div(*preset_cards, cls="vert-preset-strip") if preset_cards else P("No presets available.", cls="settings-hint"),
        ),
        open=(len(applied_names) == 0),
        cls="cat-section-details",
    )

    # ── Section C: Browse & Add Categories ───────────────────────────
    if vert_categories:
        groups: dict[str, list[dict]] = _dd(list)
        for vc in sorted(vert_categories, key=lambda c: c.get("display_name", "")):
            tag = (vc.get("vertical_tags") or ["other"])[0]
            groups[tag].append(vc)

        group_sections = []
        for tag in sorted(groups.keys(), key=lambda t_: _TAG_LABELS.get(t_, t_)):
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
                        cls="cell",
                    ),
                    cls="data-row vert-cat-row",
                    data_name=cdisplay.lower(),
                ))
            group_sections.append(
                Div(
                    H4(_TAG_LABELS.get(tag, tag), cls="vert-group-heading"),
                    Table(
                        Thead(Tr(Th(t("th.category")), Th(""))),
                        Tbody(*rows),
                        cls="data-table vert-cat-table",
                    ),
                    cls="vert-group",
                )
            )

        browse_content = Div(
            P(t("settings.browse_library_hint"), cls="settings-hint"),
            Input(type="text", id="cat-search", placeholder="Search categories...",
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
        browse_content = Div(P("No categories available in the library.", cls="settings-hint"))

    section_c = Details(
        Summary(t("settings.browse_library")),
        browse_content,
        cls="cat-section-details",
    )

    return Div(
        result_div,
        section_a,
        section_b,
        section_c,
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/inventory")
    async def settings_inventory_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        tab = request.query_params.get("tab", "locations")
        cat = request.query_params.get("cat", "")

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
            if tab == "categories" and not cat:
                vert_categories = await api.list_verticals_categories(token)
                vert_presets = await api.list_verticals_presets(token)
            else:
                vert_categories, vert_presets = [], []
            units = await api.get_units(token) if tab == "units" else []
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            locations, import_batches, cat_schemas = [], [], {}
            vert_categories, vert_presets = [], []
            units = []

        lang = get_lang(request)

        if tab == "locations":
            content = _locations_tab(locations, lang=lang)
        elif tab == "categories":
            content = _categories_tab(cat_schemas, vert_categories, vert_presets, cat)
        elif tab == "units":
            content = _units_tab(units)
        elif tab == "bulk-attach":
            content = _bulk_attach_tab()
        elif tab == "import-history":
            content = _import_history_tab(import_batches)
        else:
            content = _locations_tab(locations, lang=lang)
            tab = "locations"

        return base_shell(
            _section_breadcrumb("Inventory"),
            page_header("Inventory Settings"),
            _inventory_tabs(tab, lang=lang),
            content,
            title="Settings - Celerp",
            nav_active="settings-inventory",
            lang=lang,
            request=request,
        )

    # ── Units CRUD ────────────────────────────────────────────────────

    @app.post("/settings/units/add")
    async def units_add(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        name = (str(form.get("name") or "")).strip()
        label = (str(form.get("label") or "")).strip()
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
            return Td("—", cls="cell")
        value = unit.get(field, "")
        if field == "decimals":
            inp = Input(
                name="value", type="number", min="0", max="6",
                value=str(value), cls="input-sm",
                hx_patch=f"/settings/units/{name}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="change, keydown[key=='Enter']",
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
                hx_trigger="change, keydown[key=='Enter']",
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

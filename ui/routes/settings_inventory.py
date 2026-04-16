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
        ("category-library", "Category Library"),
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


def _units_tab(units: list[dict]) -> FT:
    """Units settings tab — table of units with inline edit + add form."""
    rows = []
    for u in units:
        uname = u.get("name", "")
        rows.append(Tr(
            Td(uname, cls="cell cell--mono"),
            _unit_display_cell(uname, "label", u.get("label", "")),
            _unit_display_cell(uname, "decimals", u.get("decimals", 0)),
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
            Thead(Tr(Th(t("th.name")), Th(t("th.label")), Th(t("th.decimals")), Th(""))),
            Tbody(*rows, add_form),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _category_library_tab(
    cat_schemas: dict,
    vert_categories: list[dict],
    vert_presets: list[dict],
) -> FT:
    """Category Library tab - unified schema+verticals UX.

    Layout:
    1. Applied schemas at top (with field count, Edit, Remove buttons)
    2. Preset selector: dropdown + Apply Preset button
    3. Add from library: category list grouped by vertical tag
    4. Default fields link at bottom
    """
    applied_names = set(cat_schemas.keys())

    # ── 1. Applied schemas ────────────────────────────────────────────
    if applied_names:
        schema_cards = []
        for name in sorted(applied_names):
            fields = cat_schemas.get(name, [])
            n_fields = len(fields)
            from urllib.parse import quote as _q
            schema_cards.append(
                Div(
                    Div(
                        Strong(name, cls="schema-card-name"),
                        Span(
                            f"{n_fields} field{'s' if n_fields != 1 else ''}",
                            cls="settings-hint ml-sm",
                        ),
                        cls="schema-card-info",
                    ),
                    Div(
                        A(t("settings.edit"),
                            href=f"/settings/inventory?tab=category-library&cat={_q(name, safe='')}",
                            cls="btn btn--secondary btn--xs",
                        ),
                        Button(t("btn.remove"),
                            cls="btn btn--danger btn--xs ml-xs",
                            hx_delete=f"/settings/cat-schema/{_q(name, safe='')}/schema",
                            hx_confirm=f"Remove '{name}' category schema? All associated attribute data will be lost.",
                            hx_swap="none",
                            hx_on__after_request="window.location.href='/settings/inventory?tab=category-library'",
                        ),
                        cls="schema-card-actions",
                    ),
                    cls="schema-card",
                )
            )
        applied_section = Div(
            H3(t("page.applied_schemas"), cls="settings-section-title"),
            P(t("inv.these_category_schemas_are_active_on_your_inventor"), cls="settings-hint"),
            *schema_cards,
            cls="mb-xl",
        )
    else:
        applied_section = Div(
            H3(t("page.applied_schemas"), cls="settings-section-title"),
            P(t("inv.no_category_schemas_applied_yet_use_the_options_be"), cls="settings-hint"),
            cls="mb-xl",
        )

    # ── Check if we are in "edit" mode for a specific category ────────
    # (Rendered separately by the page handler — this function handles list view only)

    # ── 2. Preset selector ────────────────────────────────────────────
    if vert_presets:
        preset_opts = [Option(t("inv._select_a_preset"), value="", disabled=True, selected=True)]
        for p in vert_presets:
            pname = p.get("name", "")
            pdisplay = p.get("display_name", pname)
            n_cats = len(p.get("categories", []))
            preset_opts.append(Option(f"{pdisplay} ({n_cats} categories)", value=pname))
        preset_section = Div(
            H3(t("page.apply_a_preset"), cls="settings-section-title"),
            P(t("inv.seeds_all_category_schemas_for_a_business_vertical"), cls="settings-hint"),
            Div(
                Form(
                    Select(*preset_opts, name="vertical", cls="preset-selector-select"),
                    Button(t("btn.apply_preset"), type="submit", cls="btn btn--primary"),
                    hx_post="/settings/verticals/apply-preset",
                    hx_target="#verticals-apply-result",
                    hx_swap="outerHTML",
                    cls="preset-selector",
                ),
                Div(id="verticals-apply-result"),
            ),
            cls="mb-xl",
        )
    else:
        preset_section = ""

    # ── 3. Add from library ───────────────────────────────────────────
    if vert_categories:
        from collections import defaultdict as _dd
        groups: dict[str, list[dict]] = _dd(list)
        for cat in sorted(vert_categories, key=lambda c: c.get("display_name", "")):
            tag = (cat.get("vertical_tags") or ["other"])[0]
            groups[tag].append(cat)

        group_sections = []
        for tag in sorted(groups.keys(), key=lambda t_: _TAG_LABELS.get(t_, t_)):
            cats_in_group = groups[tag]
            rows = []
            for cat in cats_in_group:
                cname = cat.get("name", "")
                cdisplay = cat.get("display_name", cname)
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
                        ),
                        cls="cell",
                    ),
                    cls="data-row",
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

        library_section = Div(
            H3(t("page.add_from_library"), cls="settings-section-title"),
            P(
                "Add individual category schemas to your inventory. "
                "Each category enriches items with type-specific attributes.",
                cls="settings-hint",
            ),
            Div(id="verticals-apply-result"),
            *group_sections,
            cls="mb-xl",
        )
    else:
        library_section = ""

    # ── 4. Default fields link ────────────────────────────────────────
    default_fields_section = Div(
        H3(t("page.default_item_fields"), cls="settings-section-title"),
        P(t("inv.edit_the_global_fields_shown_for_all_items_regardl"), cls="settings-hint"),
        A(t("inv.edit_default_fields"), href="/settings/inventory?tab=category-library&cat=__global__",
          cls="btn btn--secondary"),
        cls="mt-sm",
    )

    return Div(
        applied_section,
        preset_section,
        library_section,
        default_fields_section,
        cls="settings-card",
    )


def _category_edit_tab(category: str, cat_schemas: dict) -> FT:
    """Inline schema editor for a specific category (or __global__ for default fields)."""
    from urllib.parse import quote as _q

    if category == "__global__":
        # Redirect to legacy schema tab - global schema lives in settings/general flow
        return Div(
            P(t("inv.global_default_item_fields_are_managed_in_the_item"), cls="settings-hint"),
            A(t("btn._back_to_category_library"), href="/settings/inventory?tab=category-library",
              cls="btn btn--secondary"),
            cls="settings-card",
        )

    if category not in cat_schemas:
        return Div(
            P(f"Category '{category}' not found.", cls="error-banner"),
            A(t("btn._back_to_category_library"), href="/settings/inventory?tab=category-library",
              cls="btn btn--secondary"),
            cls="settings-card",
        )

    enc = _q(category, safe="")
    fields_raw = cat_schemas[category]
    sorted_fields = _load_cat_schema_sorted(fields_raw)

    def _cat_row(idx: int, f: dict) -> FT:
        return Tr(
            _cat_schema_display_cell(category, idx, "position", f),
            _cat_schema_display_cell(category, idx, "key", f),
            _cat_schema_display_cell(category, idx, "label", f),
            _cat_schema_display_cell(category, idx, "type", f),
            _cat_schema_display_cell(category, idx, "required", f),
            _cat_schema_display_cell(category, idx, "editable", f),
            _cat_schema_display_cell(category, idx, "show_in_table", f),
            _cat_schema_display_cell(category, idx, "options", f),
            Td(
                Button("✕", cls="btn btn--danger btn--xs",
                       hx_delete=f"/settings/cat-schema/{enc}/{idx}",
                       hx_confirm=f"Delete field '{f.get('key', idx)}'?",
                       hx_swap="none",
                       hx_on__after_request=f"window.location.href='/settings/inventory?tab=category-library&cat={enc}'"),
                cls="cell",
            ),
            cls="data-row",
        )

    add_row = Tr(
        Td(colspan="9", cls="p-sm", children=[
            Button(t("btn.add_field"), cls="btn btn--secondary btn--xs",
                   hx_post=f"/settings/cat-schema/{enc}/add",
                   hx_swap="none",
                   hx_on__after_request=f"window.location.href='/settings/inventory?tab=category-library&cat={enc}'"),
        ]),
    )

    return Div(
        Div(
            A(t("btn._back_to_category_library"), href="/settings/inventory?tab=category-library",
              cls="btn btn--secondary btn--xs"),
            cls="mb-md",
        ),
        H3(f"Edit: {category}", cls="settings-section-title"),
        P(f"Attribute fields for the '{category}' category. Click a cell to edit.", cls="settings-hint"),
        Table(
            Thead(Tr(Th("#"), Th(t("th.key")), Th(t("th.label")), Th(t("th.doc_type")), Th(t("th.required")),
                     Th(t("th.editable")), Th(t("th.show_in_table")), Th(t("th.options")), Th(""))),
            Tbody(*[_cat_row(i, f) for i, f in enumerate(sorted_fields)], add_row),
            cls="data-table",
        ),
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

        try:
            locations = (await api.get_locations(token)).get("items", [])
            import_batches = (await api.list_import_batches(token)).get("batches", [])
            cat_schemas = await api.get_all_category_schemas(token)
            if tab == "category-library" and not cat:
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
        elif tab == "category-library":
            if cat:
                content = _category_edit_tab(cat, cat_schemas)
            else:
                content = _category_library_tab(cat_schemas, vert_categories, vert_presets)
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
        current.append({"name": name, "label": label, "decimals": decimals})
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

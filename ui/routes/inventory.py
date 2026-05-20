# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
import csv
import io
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError, _flatten_item_attrs
from ui.components.files import _files_section as _shared_files_section
from ui.components.shell import base_shell, page_header
from ui.components.table import data_table, search_bar, pagination, EMPTY, breadcrumbs, status_cards, empty_state_cta, add_new_option
from ui.config import get_token as _token, API_BASE as _api_base
from ui.i18n import t, get_lang
from celerp.services.units import is_weight_unit, is_pieces_unit

_DEFAULT_PER_PAGE = 50

_BULK_SPLIT_JS = """
function splitRecalcMotherWeight(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentWeight = parseFloat(form.dataset.parentWeight || '0');
  var decimals = parseInt(form.dataset.weightDecimals || '2', 10);
  var childVal = parseFloat(input.value) || 0;
  var mw = form.querySelector('.mother-weight-display');
  if (mw) mw.textContent = Math.max(0, parentWeight - childVal).toFixed(decimals);
}
function splitClampWeight(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentWeight = parseFloat(form.dataset.parentWeight || '0');
  var decimals = parseInt(form.dataset.weightDecimals || '2', 10);
  var childVal = Math.min(Math.max(0, parseFloat(input.value) || 0), parentWeight);
  input.value = childVal.toFixed(decimals);
  var mw = form.querySelector('.mother-weight-display');
  if (mw) mw.textContent = Math.max(0, parentWeight - childVal).toFixed(decimals);
}
function splitRecalcMotherPieces(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentPieces = parseFloat(form.dataset.parentPieces || '0');
  var childP = parseFloat(input.value) || 0;
  var mp = form.querySelector('.mother-pieces-display');
  if (mp) mp.textContent = String(Math.round(Math.max(0, parentPieces - childP)));
}
function splitClampPieces(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentPieces = parseFloat(form.dataset.parentPieces || '0');
  var maxVal = Math.max(0, parentPieces - 1);
  var childP = Math.min(Math.max(0, parseFloat(input.value) || 0), maxVal);
  input.value = String(Math.round(childP));
  var mp = form.querySelector('.mother-pieces-display');
  if (mp) mp.textContent = String(Math.round(Math.max(0, parentPieces - childP)));
}
function bulkSplitAutoLoad() {
  var checked = document.querySelector('.row-select:checked');
  if (!checked) return;
  var entityId = checked.dataset.entityId || checked.value;
  if (!entityId) return;
  var url = '/api/items/bulk/split-preview?entity_id=' + encodeURIComponent(entityId);
  htmx.ajax('GET', url, { target: '#bulk-split-preview', swap: 'innerHTML' })
    .then(function() { if (window.htmx) htmx.process(document.getElementById('bulk-split-preview')); });
}
function bulkSplitChildQtyChanged(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentQty = parseFloat(form.dataset.parentQty || '0');
  var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
  var epsilon = decimals > 0 ? Math.pow(10, -decimals) : 1;
  var childQty = Math.min(Math.max(0, parseFloat(input.value) || 0), parentQty - epsilon);
  input.value = childQty.toFixed(decimals);
  var motherQtyDisplay = form.querySelector('.mother-qty-display');
  if (motherQtyDisplay) motherQtyDisplay.textContent = Math.max(0, parentQty - childQty).toFixed(decimals);
}
function bulkSplitSkuChanged(input) {
  // SKU is a free-text field; no server call needed.
}
function bulkSplitSubmit(formEl) {
  var existing = formEl.querySelector('input[name="entity_id"]');
  if (!existing || !existing.value) {
    var checked = document.querySelector('.row-select:checked');
    var entityId = checked ? (checked.dataset.entityId || checked.value) : '';
    if (!existing) {
      var inp = document.createElement('input'); inp.type = 'hidden'; inp.name = 'entity_id'; inp.value = entityId;
      formEl.appendChild(inp);
    } else { existing.value = entityId; }
  }
  return true;
}
"""




def _parse_params(request: Request) -> dict:
    q = request.query_params
    try:
        per_page = int(q.get("per_page", _DEFAULT_PER_PAGE))
    except (ValueError, TypeError):
        per_page = _DEFAULT_PER_PAGE
    try:
        page = int(q.get("page", 1))
    except (ValueError, TypeError):
        page = 1
    raw_cols = q.getlist("cols") or q.get("cols", "").split(",")
    cols = [c for part in raw_cols for c in part.split(",") if c]
    return {
        "q": q.get("q", ""),
        "skus": q.get("skus", ""),  # comma-separated exact SKU filter
        "page": max(1, page),
        "status": q.get("status", ""),
        "category": q.get("category", ""),
        "inventory_type": q.get("inventory_type", ""),
        "sort": q.get("sort", ""),
        "dir": q.get("dir", "desc"),
        "per_page": max(1, per_page),
        "cols": cols,
    }


def _base_state(p: dict, include_page: bool = False) -> dict:
    state = {}
    for k in ("q", "skus", "status", "category", "inventory_type", "sort", "dir"):
        if p.get(k):
            state[k] = p[k]
    if p.get("per_page") and p["per_page"] != _DEFAULT_PER_PAGE:
        state["per_page"] = str(p["per_page"])
    if p.get("cols"):
        state["cols"] = ",".join(p["cols"])
    if include_page and p.get("page", 1) > 1:
        state["page"] = str(p["page"])
    return state


async def _inventory_content(
    token: str,
    p: dict,
    schema: list[dict],
    cat_schemas: dict,
    col_prefs: dict,
    company: dict,
    locations: list[dict],
    col_manager_open: bool = False,
    lang: str = "en",
) -> FT:
    """Build the #inventory-content fragment (tabs + valuation + cards + table + pagination).

    Shared by GET /inventory (full page), GET /inventory/content (HTMX partial), and
    GET /inventory/search (legacy alias). All tab/sort/search HTMX actions target
    #inventory-content so the entire dynamic section re-renders consistently.
    """
    try:
        valuation = await api.get_valuation(token, category=p.get("category") or None, status=p.get("status") or None)
        params: dict = {"limit": p["per_page"], "offset": (p["page"] - 1) * p["per_page"]}
        if p["q"]:
            params["q"] = p["q"]
        if p.get("skus"):
            params["skus"] = p["skus"]
        if p["status"]:
            params["status"] = p["status"]
        if p["category"]:
            params["category"] = p["category"]
        if p.get("inventory_type"):
            params["inventory_type"] = p["inventory_type"]
        if p["sort"]:
            params["sort"] = p["sort"]
            params["dir"] = p["dir"]
        items_resp, units_resp = await asyncio.gather(
            api.list_items(token, params),
            api.get_units(token),
        )
        items = items_resp.get("items", [])
        unit_names: list[str] = [u["name"] for u in units_resp if u.get("name")]
        units_map: dict[str, dict] = {u["name"]: u for u in units_resp if u.get("name")}
        try:
            category_label_map: dict = await api.get_category_display_names(token)
        except Exception:
            category_label_map = {}
    except APIError:
        valuation, items, unit_names, units_map, category_label_map = {}, [], [], {}, {}

    currency = company.get("currency")
    vertical = company.get("settings", {}).get("vertical", "") if isinstance(company.get("settings"), dict) else ""

    category_counts = valuation.get("category_counts", {})
    total_scoped = valuation.get("total_scoped_count", sum(category_counts.values()))
    count_by_status = valuation.get("count_by_status", {})
    active_cat = p.get("category", "")
    eff_schema = _effective_schema(schema, cat_schemas, active_cat)
    visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, p.get("cols") or [])
    # Inject resolved cols into URL state so sort links and pagination always carry
    # the exact column set being rendered, even when it came from col_prefs not URL params.
    p_with_cols = {**p, "cols": visible_cols}
    extra_params = urlencode(_base_state(p_with_cols))
    total_items = valuation.get("item_count", 0)

    return Div(
        _category_tabs(category_counts, p, total_scoped=total_scoped),
        _inventory_type_tabs(p),
        _valuation_bar(valuation, currency, lang, status=p.get("status", "")),
        _inventory_status_cards(count_by_status, p.get("status", ""), vertical, p, lang=lang),
        _bulk_toolbar(locations, p, total_items),
        Div(
            _column_manager(eff_schema, p, active_cat, visible_cols, keep_open=col_manager_open),
            cls="column-manager-row",
        ),
        data_table(
            eff_schema,
            items,
            entity_type="inventory",
            show_cols=visible_cols or None,
            sort_key=p["sort"],
            sort_dir=p["dir"],
            sort_url="/inventory/content",
            extra_params=_base_state(p_with_cols),
            currency=currency,
            sort_target="#inventory-content",
            auto_hide_empty=False,
            cell_renderers=_inventory_cell_renderers(eff_schema, unit_names, units_map, category_label_map),
            hidden_fields=set(_PAIRED_TABLE.values()),
        ) if items else _inventory_empty_state(p),
        pagination(p["page"], valuation.get("item_count", 0), p["per_page"], "/inventory", extra_params),
        Div(id="modal-container"),
        id="inventory-content",
    )


def setup_routes(app):

    @app.get("/inventory")
    async def inventory_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        p = _parse_params(request)

        try:
            schema, cat_schemas, col_prefs, company, loc_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_all_category_schemas(token),
                api.get_column_prefs(token),
                api.get_company(token),
                api.get_locations(token),
            )
            locations = loc_resp.get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            schema, cat_schemas, col_prefs, company, locations = [], {}, {}, {}, []

        currency = company.get("currency")
        lang = get_lang(request)
        vertical = company.get("settings", {}).get("vertical", "") if isinstance(company.get("settings"), dict) else ""
        active_cat = p.get("category", "")
        eff_schema = _effective_schema(schema, cat_schemas, active_cat)
        visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, p.get("cols") or [])

        content = await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=lang)

        return base_shell(
            page_header(
                t("page.inventory", lang),
                search_bar(
                    placeholder=t("msg.search_inventory_placeholder", lang),
                    target="#inventory-content",
                    url="/inventory/content",
                ),
                A(t("btn.import", lang), href="/inventory/import", cls="btn btn--secondary"),
                Button(t("btn.add_item", lang), hx_post="/inventory/create-blank", hx_swap="none", cls="btn btn--primary"),
                A(t("btn.export_csv", lang), href="/inventory/export/csv", cls="btn btn--secondary"),
                A(t("inv.customize_fields"), href="/settings/inventory?tab=category-library", cls="btn btn--ghost btn--sm"),
            ),
            content,
            Script(_BULK_SPLIT_JS),
            title="Inventory - Celerp",
            nav_active="inventory",
            lang=lang,
            request=request,
        )

    @app.get("/inventory/content")
    async def inventory_content(request: Request):
        """HTMX partial: returns #inventory-content fragment (tabs + cards + valuation + table).

        Used by category tabs, status tabs, search, sort, and pagination so all state stays consistent.
        Direct (non-HTMX) navigation to this URL redirects to the full inventory page so users
        can bookmark/refresh sort/filter URLs without getting a bare HTML fragment.
        """
        if not request.headers.get("HX-Request"):
            qs = request.url.query
            return RedirectResponse(f"/inventory{'?' + qs if qs else ''}", status_code=302)
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        p = _parse_params(request)
        try:
            schema, cat_schemas, col_prefs, company, loc_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_all_category_schemas(token),
                api.get_column_prefs(token),
                api.get_company(token),
                api.get_locations(token),
            )
            locations = loc_resp.get("items", [])
        except APIError as e:
            schema, cat_schemas, col_prefs, company, locations = [], {}, {}, {}, []
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=get_lang(request))

    @app.post("/inventory/columns")
    async def inventory_columns(request: Request):
        """Save column prefs and return updated #inventory-content fragment."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        cols = [v.strip() for v in form.getlist("cols") if v.strip()]
        cat_pref = str(form.get("_cat_pref", "__all__")).strip() or "__all__"
        # Save column prefs for this view
        try:
            existing_prefs = await api.get_column_prefs(token)
        except APIError:
            existing_prefs = {}
        existing_prefs[cat_pref] = cols
        try:
            await api.patch_column_prefs(token, existing_prefs)
        except APIError:
            pass
        # Rebuild params from hidden form fields (category, status, etc.)
        p = {
            "q": str(form.get("q", "")).strip(),
            "page": 1,
            "status": str(form.get("status", "")).strip(),
            "category": str(form.get("category", "")).strip(),
            "sort": str(form.get("sort", "")).strip(),
            "dir": str(form.get("dir", "desc")).strip() or "desc",
            "per_page": int(form.get("per_page", _DEFAULT_PER_PAGE) or _DEFAULT_PER_PAGE),
            "cols": [],  # cols are now saved in prefs; don't pass via URL
        }
        try:
            schema, cat_schemas, col_prefs, company, loc_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_all_category_schemas(token),
                api.get_column_prefs(token),
                api.get_company(token),
                api.get_locations(token),
            )
            locations = loc_resp.get("items", [])
        except APIError:
            schema, cat_schemas, col_prefs, company, locations = [], {}, {}, {}, []
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, col_manager_open=True, lang=get_lang(request))

    @app.get("/inventory/search")
    async def inventory_search(request: Request):
        """Legacy search endpoint — now delegates to /inventory/content for full fragment swap."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        p = _parse_params(request)
        try:
            schema, cat_schemas, col_prefs, company, loc_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_all_category_schemas(token),
                api.get_column_prefs(token),
                api.get_company(token),
                api.get_locations(token),
            )
            locations = loc_resp.get("items", [])
        except APIError as e:
            schema, cat_schemas, col_prefs, company, locations = [], {}, {}, {}, []
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=get_lang(request))

    @app.get("/inventory/export/csv")
    async def inventory_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        p = _parse_params(request)
        params: dict = {}
        if p["q"]:
            params["q"] = p["q"]
        if p["status"]:
            params["status"] = p["status"]
        if p["category"]:
            params["category"] = p["category"]
        if p.get("inventory_type"):
            params["inventory_type"] = p["inventory_type"]
        try:
            data = await api.export_items_csv(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = b"error\n" + e.detail.encode()
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=items.csv"},
        )

    @app.get("/inventory/import")
    async def inventory_import_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        return base_shell(
            page_header(
                t("page.import_inventory", lang),
                A(t("btn.back", lang), href="/inventory", cls="btn btn--secondary"),
                A(t("btn.download_template", lang), href="/inventory/import/template", cls="btn btn--secondary"),
            ),
            _import_upload_form(),
            P(t("inv.custom_columns_in_your_csv_will_be_imported_as_ite"),
                A(t("inv.manage_fields"), href="/settings/inventory?tab=category-library"),
                cls="import-hint mt-sm",
            ),
            title="Import Inventory - Celerp",
            nav_active="inventory",
            lang=lang,
            request=request,
        )

    @app.get("/inventory/import/template")
    async def inventory_import_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            price_lists = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
        spec = _build_import_spec(price_lists)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=spec.cols)
        writer.writeheader()
        # Write one empty example row
        writer.writerow({c: "" for c in spec.cols})
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=items_template.csv"},
        )

    @app.post("/inventory/import/preview")
    async def inventory_import_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header(t("page.import_inventory", lang)),
                _import_upload_form(error=err),
                title="Import Inventory - Celerp",
                nav_active="inventory",
                lang=lang,
                request=request,
            )

        cols = list(rows[0].keys()) if rows else []
        if not cols:
            return base_shell(
                page_header(t("page.import_inventory", lang)),
                _import_upload_form(error="CSV file has no columns."),
                title="Import Inventory - Celerp",
                nav_active="inventory",
                lang=lang,
                request=request,
            )

        # Stash the raw CSV and show column mapping UI
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)

        # Fetch price lists + category attribute keys
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            price_lists = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
        spec = _build_import_spec(price_lists)
        cat_schemas = await api.get_all_category_schemas(token)
        cat_attrs = _union_category_attr_keys(cat_schemas)

        return base_shell(
            page_header(t("page.import_inventory", lang)),
            column_mapping_form(
                csv_cols=cols,
                target_cols=spec.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/inventory/import/mapped",
                back_href="/inventory/import",
                required_targets=spec.required,
                category_attrs=cat_attrs,
            ),
            title="Import Inventory - Celerp",
            nav_active="inventory",
            lang=lang,
            request=request,
        )

    @app.post("/inventory/import/mapped")
    async def inventory_import_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header(t("page.import_inventory", lang)),
                _import_upload_form(error="CSV data expired. Please re-upload."),
                title="Import Inventory - Celerp",
                nav_active="inventory",
                lang=lang,
                request=request,
            )

        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            price_lists = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
        spec = _build_import_spec(price_lists)

        # Parse original CSV columns for validation
        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])

        # Validate mapping before applying
        mapping_errors = validate_column_mapping(
            form, original_cols, core_fields=_CORE_ITEM_COLS,
        )
        if mapping_errors:
            # Re-render the mapping form with errors and preserved form values
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            cat_schemas = await api.get_all_category_schemas(token)
            cat_attrs = _union_category_attr_keys(cat_schemas)
            return base_shell(
                page_header(t("page.import_inventory", lang)),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=spec.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/inventory/import/mapped",
                    back_href="/inventory/import",
                    required_targets=spec.required,
                    category_attrs=cat_attrs,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Inventory - Celerp",
                nav_active="inventory",
                lang=lang,
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)

        # Re-stash the remapped CSV for downstream steps
        csv_ref = _stash_csv(remapped_csv)

        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else spec.cols)
        validate, cell_renderers = await _build_item_validator(token)

        return base_shell(
            page_header(t("page.import_inventory", lang)),
            _csv_validation_result(
                rows=rows,
                cols=cols,
                validate=validate,
                confirm_action="/inventory/import/confirm",
                error_report_action="/inventory/import/errors",
                back_href="/inventory/import",
                revalidate_action="/inventory/import/revalidate",
                has_mapping=True,
                upsert_label="SKU or barcode",
                cell_renderers=cell_renderers,
            ),
            title="Import Inventory - Celerp",
            nav_active="inventory",
            lang=lang,
            request=request,
        )

    @app.post("/inventory/import/revalidate")
    async def inventory_import_revalidate(request: Request):
        """Apply inline fixes and re-validate; import if clean."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return _import_upload_form(error="CSV data expired. Please re-upload.")
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _IMPORT_SPEC.cols
        rows = _apply_fixes(form, rows, cols)
        # Re-stash the patched CSV so downstream confirm/errors can read it
        csv_ref = _stash_csv(_rows_to_csv(rows, cols))
        validate, cell_renderers = await _build_item_validator(token)
        return _csv_validation_result(
            rows=rows,
            cols=cols,
            validate=validate,
            confirm_action="/inventory/import/confirm",
            error_report_action="/inventory/import/errors",
            back_href="/inventory/import",
            revalidate_action="/inventory/import/revalidate",
            has_mapping=True,
            upsert_label="SKU or barcode",
            cell_renderers=cell_renderers,
        )

    @app.post("/inventory/import/errors")
    async def inventory_import_errors(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _IMPORT_SPEC.cols
        validate, _ = await _build_item_validator(token)
        return error_report_response(rows, cols, validate, "inventory_errors.csv")

    @app.post("/inventory/import/confirm")
    async def inventory_import_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        import uuid

        form = await request.form()
        upsert = form.get("upsert") == "1"
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        # Build location name→id map.
        # Rules:
        # 1. If location_name is empty/absent and there is exactly one location → use it (default).
        # 2. If location_name is empty/absent and there are multiple locations → abort with clear error.
        # 3. If location_name is present but not in the map → auto-create it as a warehouse.
        try:
            loc_resp = await api.get_locations(token)
            existing_locs = loc_resp.get("items", [])
        except APIError:
            existing_locs = []

        location_map: dict[str, str] = {l["name"]: l["id"] for l in existing_locs}

        # Determine default location (used when location_name is blank/absent)
        default_location_id: str | None = None
        if len(existing_locs) == 1:
            default_location_id = existing_locs[0]["id"]
        else:
            # Use the location marked is_default, or the first one if none marked
            for loc in existing_locs:
                if loc.get("is_default"):
                    default_location_id = loc["id"]
                    break

        # Collect all unique location names used in CSV that need creating
        loc_names_needed = {
            str(row.get("location_name", "")).strip()
            for row in rows
            if str(row.get("location_name", "")).strip()
            and str(row.get("location_name", "")).strip() not in location_map
        }
        for loc_name_new in loc_names_needed:
            try:
                created = await api.create_location(token, {"name": loc_name_new, "type": "warehouse"})
                location_map[loc_name_new] = created["id"]
            except APIError:
                pass  # Will fall back to default; individual row will still try to proceed

        records: list[dict] = []

        # Build category → default_sell_by map for sell_by fallback at import time.
        _cat_sell_by: dict[str, str] = {}
        try:
            vert_cats = await api.list_verticals_categories(token)
            _cat_sell_by = {
                c["name"]: c["default_sell_by"]
                for c in vert_cats
                if c.get("default_sell_by")
            }
        except Exception:
            pass  # Non-critical; if unavailable, sell_by remains None

        # Defensive assertion: sell_by is validated in the revalidate cycle via
        # _build_item_validator. If any row is still missing it here, the validator
        # has a bug - this is an internal error, not a user error.
        missing_sell_by = [
            str(row.get("sku") or row.get("name") or f"row {i + 1}")
            for i, row in enumerate(rows)
            if not (
                str(row.get("sell_by", "")).strip()
                or _cat_sell_by.get(str(row.get("category", "")).strip())
            )
        ]
        if missing_sell_by:
            raise RuntimeError(
                f"BUG: {len(missing_sell_by)} row(s) reached confirm with missing sell_by "
                f"({', '.join(missing_sell_by[:5])}). Validator did not catch them."
            )

        for row in rows:
            sku = str(row.get("sku", "")).strip()
            name = str(row.get("name", "")).strip()
            loc_name = str(row.get("location_name", "")).strip()

            # Resolve location_id: explicit name → map; blank → default
            if loc_name:
                location_id = location_map.get(loc_name)
            else:
                location_id = default_location_id

            # No guard for sku/name here - the validation pipeline (validate_cell /
            # _build_item_validator) is the sole authority. sku is optional (auto-assigned);
            # name is in spec.required and would have been caught at revalidate time.
            if not location_id:
                return import_abort_panel(
                    message=(
                        "Import aborted: could not determine a location for one or more rows. "
                        "Your CSV has no location_name column and there are multiple locations configured. "
                        "Please add a location_name column or set a default location in Settings → Inventory."
                    ),
                    import_more_href="/inventory/import",
                    back_href="/inventory",
                    has_mapping=True,
                )

            qty_raw = str(row.get("quantity", "0")).strip()
            try:
                qty = float(qty_raw) if qty_raw else 0.0
            except ValueError:
                qty = 0.0

            def _flt(key: str, _row: dict = row) -> float | None:
                raw = str(_row.get(key, "")).strip()
                if not raw:
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            # All columns not in the core field set are treated as attributes
            attrs: dict = {}
            for k, v in row.items():
                if k not in _CORE_ITEM_COLS and not k.endswith("_price") and v is not None:
                    v_str = str(v).strip()
                    if v_str:
                        attrs[k] = v_str

            data = {
                "sku": sku,
                "name": name,
                "quantity": qty,
                "category": str(row.get("category", "")).strip() or None,
                "weight": _flt("weight") or _flt("weight_ct"),
                "weight_unit": str(row.get("weight_unit", "")).strip() or None,
                "sell_by": str(row.get("sell_by", "")).strip() or _cat_sell_by.get(str(row.get("category", "")).strip()) or None,
                "barcode": str(row.get("barcode", "")).strip() or None,
                "hs_code": str(row.get("hs_code", "")).strip() or None,
                "short_description": str(row.get("short_description", "")).strip() or None,
                "description": str(row.get("description", "")).strip() or None,
                "notes": str(row.get("notes", "")).strip() or None,
                "location_id": location_id,
                "attributes": attrs,
            }
            # status, created_at, updated_at intentionally omitted:
            # status is always set to available by the backend on creation.
            # created_at/updated_at are system-generated; backend enforces this.
            # Extract price fields dynamically (any column ending in _price)
            for col_key in row:
                if col_key.endswith("_price") and _flt(col_key) is not None:
                    data[col_key] = _flt(col_key)
            barcode = data["barcode"]
            idem = f"csv:item:bc:{barcode}".lower() if barcode else f"csv:item:{sku}".lower()
            data["idempotency_key"] = idem

            records.append({
                "entity_id": f"item:{uuid.uuid4()}",
                "event_type": "item.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })

        try:
            _CHUNK = 500
            merged: dict = {"created": 0, "skipped": 0, "updated": 0, "errors": [], "batch_id": None}
            for i in range(0, max(len(records), 1), _CHUNK):
                chunk = records[i : i + _CHUNK]
                if not chunk:
                    break
                r = await api.batch_import(token, "/items/import/batch", chunk, upsert=upsert)
                merged["created"] += r.get("created", 0)
                merged["skipped"] += r.get("skipped", 0)
                merged["updated"] += r.get("updated", 0)
                merged["errors"].extend(r.get("errors") or [])
                if r.get("batch_id"):
                    merged["batch_id"] = r["batch_id"]
            result = merged
        except APIError as e:
            if e.status == 401:
                return import_abort_panel(
                    message=t("error.session_expired"),
                    import_more_href="/login",
                    back_href="/inventory",
                    has_mapping=True,
                )
            return import_abort_panel(
                message=f"Import failed: {e.detail}",
                import_more_href="/inventory/import",
                back_href="/inventory",
                has_mapping=True,
            )

        # Auto-merge discovered attribute keys into category schemas
        schema_info = ""
        if records:
            try:
                cat_attr_values = _collect_category_attributes(rows)
                inferred = _infer_category_schemas(cat_attr_values)
                if inferred:
                    await api.merge_category_schemas(token, inferred)
                    total_new = sum(len(fs) for fs in inferred.values())
                    cat_names = ", ".join(sorted(inferred.keys()))
                    schema_info = Div(
                        P(
                            f"{total_new} new attribute field(s) added to: {cat_names}. ",
                            A(t("inv.review"), href="/settings/inventory?tab=category-library"),
                            cls="flash flash--info",
                        ),
                    )
            except Exception:
                pass  # schema merge is best-effort; import already succeeded

        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        errors = list(result.get("errors", []) or [])

        return import_result_panel(
            created=created,
            skipped=skipped,
            updated=updated,
            errors=errors,
            entity_label="inventory",
            back_href="/inventory",
            import_more_href="/inventory/import",
            has_mapping=True,
            extra=schema_info,
        )

    # ── Blank-create: /inventory/create-blank ──────────────────────────────────
    # MUST be registered BEFORE /inventory/{entity_id} (static before variable)

    @app.post("/inventory/create-blank")
    async def inventory_create_blank(request: Request):
        """Create a minimal item and redirect to its detail page."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.create_item(token, {"name": "New Item", "quantity": 0, "sell_by": "piece"})
            item_id = result.get("id", result.get("entity_id", ""))
        except APIError as e:
            if e.status == 401:
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            logger.warning("Blank-create item failed: %s", e.detail)
            return Response("", status_code=500)
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{item_id}"})

    # /inventory/new: redirect for any bookmarked links
    @app.get("/inventory/new")
    async def inventory_new_redirect(request: Request):
        return RedirectResponse("/inventory", status_code=302)

    @app.get("/inventory/{entity_id}")
    async def item_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if not entity_id or entity_id.strip() == "":
            return RedirectResponse("/inventory", status_code=302)
        try:
            schema, item, company, cat_schemas, price_lists, units_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_company(token),
                api.get_all_category_schemas(token),
                api.get_price_lists(token),
                api.get_units(token),
            )
            ledger = (await api.list_ledger(token, {"entity_id": entity_id, "limit": 10})).get("items", [])
            locations = (await api.get_locations(token)).get("items", [])
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            schema, item, ledger, locations, company, cat_schemas, price_lists, units_resp = [], {}, [], [], {}, {}, [], {}

        currency = company.get("currency")
        # Inject category options into the schema's category field
        cat_names = sorted(cat_schemas.keys())
        loc_names = [loc.get("name", "") for loc in locations if loc.get("name")]
        schema = [
            {**f, "type": "select", "options": cat_names} if f.get("key") == "category"
            else {**f, "type": "select", "options": loc_names, "editable": True} if f.get("key") == "location_name"
            else f
            for f in schema
        ]
        # Merge category-specific fields for this item's category
        item_cat = item.get("category", "")
        if item_cat and item_cat in cat_schemas:
            global_keys = {f["key"] for f in schema}
            extra = [f for f in cat_schemas[item_cat] if f["key"] not in global_keys]
            schema = schema + extra

        # Build pricing_keys dynamically from company price lists
        pl_names = {pl.get("name", "") for pl in price_lists}
        # Include conventional key patterns (e.g. "retail_price" for "Retail")
        pl_conventional = {f"{n.lower()}_price" for n in pl_names}
        pricing_keys = pl_names | pl_conventional | {"total_cost", "total_wholesale", "total_retail"}
        detail_fields = [f for f in schema if f.get("key") not in pricing_keys and f.get("key") not in _PAIRED_SECONDARY_KEYS]
        pricing_fields = [f for f in schema if f.get("key") in pricing_keys]

        active_tab = request.query_params.get("tab", "details")

        units_list = units_resp if isinstance(units_resp, list) else []
        unit_names = [u["name"] for u in units_list]
        units_map = {u["name"]: u for u in units_list}
        detail_renderers = _inventory_cell_renderers(schema, unit_names, units_map)

        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Inventory", "/inventory"), (item.get("name") or item.get("sku") or entity_id, None)]),
            page_header(
                item.get("name") or item.get("sku") or entity_id,
                Div(
                    _print_label_dropdown(entity_id),
                    A(t("inv.back_to_inventory"), href="/inventory", cls="btn btn--secondary"),
                    cls="header-actions",
                ),
            ),
            _item_detail_tabs(entity_id, item, detail_fields, pricing_fields, ledger, currency, active_tab, price_lists=price_lists, cell_renderers=detail_renderers),
            title="Inventory Item - Celerp",
            nav_active="inventory",
            request=request,
        )

    @app.get("/api/items/{entity_id}/label-templates")
    async def item_label_templates(request: Request, entity_id: str):
        """Return label template dropdown options for the print button."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{_api_base}/api/labels/templates",
                    headers={"Authorization": f"Bearer {token}"},
                )
                templates = r.json().get("items", []) if r.status_code == 200 else []
        except Exception:
            templates = []
        if not templates:
            return Div(
                P(t("inv.no_label_templates"), cls="dropdown-empty"),
                A(t("inv.create_template"), href="/settings/labels", cls="dropdown-link"),
            )
        print_js = """
function celerpPrintLabel(entityId, templateId) {
    // UI-layer GET route: no API auth needed, auto-triggers window.print()
    window.open('/labels/print/' + encodeURIComponent(entityId) + '?template_id=' + encodeURIComponent(templateId));
}
"""
        items = [
            A(
                tpl.get("name", "Template"),
                href="#",
                onclick=f"celerpPrintLabel('{entity_id}','{tpl['id']}');this.closest('.print-label-dropdown').classList.remove('open');return false;",
                cls="dropdown-item",
            )
            for tpl in templates
        ]
        return Div(*items, Script(print_js))

    @app.get("/api/items/{entity_id}/field/{field}/edit")
    async def field_edit_cell(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            schema, item, cat_schemas, locs = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
            )
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        locations = locs.get("items", [])
        f_def, cell_type, options, allow_custom = _resolve_field_def(field, schema, cat_schemas, item, locations)
        from ui.components.table import editable_cell
        # Apply unit-field override (sell_by, purchase_unit → searchable select)
        if field in ("sell_by", "purchase_unit"):
            try:
                units_resp = await api.get_units(token)
                unit_names = [u["name"] for u in units_resp if u.get("name")]
            except Exception:
                unit_names = []
            cell_type, options, allow_custom = _apply_unit_field_override(field, cell_type, options, allow_custom, unit_names)
        label_map: dict | None = None
        if field == "category":
            try:
                label_map = await api.get_category_display_names(token)
            except Exception:
                label_map = None
        return editable_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                             cell_type=cell_type, options=options, allow_custom=allow_custom,
                             label_map=label_map)

    @app.get("/api/items/{entity_id}/field/{field}/display")
    async def field_display_cell(request: Request, entity_id: str, field: str):
        """Restore a cell to display (read-only) state — used by Escape key handler."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            schema, item, cat_schemas, locs = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
            )
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        locations = locs.get("items", [])
        f_def, cell_type, options, _ = _resolve_field_def(field, schema, cat_schemas, item, locations)
        from ui.components.table import display_cell
        label_map: dict | None = None
        if field == "category":
            try:
                label_map = await api.get_category_display_names(token)
            except Exception:
                label_map = None
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True,
                            label_map=label_map)

    _PAIRED_FIELDS: dict[str, str] = {"quantity": "sell_by", "sell_by": "quantity",
                                      "weight": "weight_unit", "weight_unit": "weight",
                                      "purchase_unit": "purchase_conversion_factor",
                                      "purchase_conversion_factor": "purchase_unit"}

    @app.patch("/api/items/{entity_id}/field/{field}")
    async def field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value: str | float | bool = str(form.get("value", ""))

        # Convert bool fields from string to proper bool
        if field == "allow_splitting":
            value = value.lower() in ("true", "1", "yes")
        # Convert numeric fields from string to float
        elif field == "quantity" or field.endswith("_price"):
            try:
                value = float(value)
            except (ValueError, TypeError):
                return P(t("error.invalid_number"), cls="cell-error")

        try:
            if field == "location_name":
                # Transfer requires location_id; resolve name → id from locations list
                locs = (await api.get_locations(token)).get("items", [])
                loc = next((l for l in locs if l.get("name") == value), None)
                if loc is None:
                    return P(f"Unknown location: {value}", cls="cell-error")
                await api.transfer_item(token, entity_id, loc.get("location_id") or loc.get("id", ""))
            else:
                await api.patch_item(token, entity_id, {field: value})
            schema, item, cat_schemas, locs_data = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
            )
        except APIError as e:
            return P(e.detail, cls="cell-error")

        locations = locs_data.get("items", [])
        f_def, cell_type, options, _ = _resolve_field_def(field, schema, cat_schemas, item, locations)
        # Category change: context-aware response
        if field == "category":
            safe_id = entity_id.replace(":", "-")
            current_url = request.headers.get("hx-current-url", "")
            if "/inventory/item:" in current_url:
                # Detail page: return display cell + OOB reload of attributes section
                try:
                    label_map = await api.get_category_display_names(token)
                except Exception:
                    label_map = {}
                f_def2, cell_type2, options2, _ = _resolve_field_def(field, schema, cat_schemas, item, locations)
                from ui.components.table import display_cell
                cat_cell = display_cell(
                    entity_id=entity_id, field=field, value=item.get(field, ""),
                    cell_type=cell_type2, options=options2,
                    editable=f_def2.get("editable", True) if f_def2 else True,
                    label_map=label_map,
                )
                oob_reload = Div(
                    hx_get=f"/api/items/{entity_id}/attributes-section",
                    hx_trigger="load",
                    hx_swap="outerHTML",
                    hx_swap_oob="true",
                    id="item-attributes-section",
                )
                return cat_cell, oob_reload
            else:
                # List page: row reload
                return Div(
                    hx_get=f"/api/items/{entity_id}/row",
                    hx_trigger="load",
                    hx_target=f"#row-{safe_id}",
                    hx_swap="outerHTML",
                    style="display:none",
                )
        # Paired fields: return the combined paired cell after save
        if field in _PAIRED_FIELDS:
            try:
                return await _paired_display(token, entity_id, field)
            except Exception:
                pass  # fall through to single display_cell on error
        from ui.components.table import display_cell
        try:
            label_map = await api.get_category_display_names(token) if field == "category" else None
        except Exception:
            label_map = None
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True,
                            label_map=label_map)

    # ── Paired-cell endpoints (quantity+sell_by, weight+weight_unit, purchase_unit+purchase_conversion_factor) ─────────

    @app.get("/api/items/{entity_id}/attributes-section")
    async def item_attributes_section(request: Request, entity_id: str):
        """Return the attributes detail-card for an item. Used by detail page after category change."""
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            schema, item, cat_schemas, price_lists = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_price_lists(token),
            )
        except APIError as e:
            return Response(str(e.detail), status_code=500)
        # Build category options
        cat_names = sorted(cat_schemas.keys())
        schema = [
            {**f, "type": "select", "options": cat_names} if f.get("key") == "category" else f
            for f in schema
        ]
        # Merge category-specific fields
        item_cat = item.get("category", "")
        if item_cat and item_cat in cat_schemas:
            global_keys = {f["key"] for f in schema}
            extra = [f for f in cat_schemas[item_cat] if f["key"] not in global_keys]
            schema = schema + extra
        # Build pricing_keys to exclude from detail_fields
        pl_names = {pl.get("name", "") for pl in price_lists}
        pl_conventional = {f"{n.lower()}_price" for n in pl_names}
        pricing_keys = pl_names | pl_conventional | {"total_cost", "total_wholesale", "total_retail"}
        detail_fields = [f for f in schema if f.get("key") not in pricing_keys and f.get("key") not in _PAIRED_SECONDARY_KEYS]
        right = [f for f in detail_fields if f.get("key") not in _ITEM_CORE_KEYS]
        currency = None
        try:
            company = await api.get_company(token)
            currency = company.get("currency")
        except Exception:
            pass
        return Div(
            _detail_table(entity_id, item, right, title="Attributes", currency=currency),
            id="item-attributes-section",
        )

    @app.get("/api/items/{entity_id}/row")
    async def item_row(request: Request, entity_id: str):
        """Return the full <tr> for one item. Used after category change to reload attribute columns."""
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            schema, item, cat_schemas, loc_resp, units_resp = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
                api.get_units(token),
            )
        except APIError as e:
            return Response(str(e.detail), status_code=500)
        unit_names = [u["name"] for u in units_resp if u.get("name")]
        units_map = {u["name"]: u for u in units_resp if u.get("name")}
        try:
            category_label_map = await api.get_category_display_names(token)
        except Exception:
            category_label_map = {}
        active_cat = item.get("category", "")
        eff_schema = _effective_schema(schema, cat_schemas, active_cat)
        col_prefs: dict = {}
        try:
            col_prefs = await api.get_column_prefs(token)
        except Exception:
            pass
        visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, [])
        cell_renderers = _inventory_cell_renderers(eff_schema, unit_names, units_map, category_label_map)
        from ui.components.table import display_cell, EMPTY
        safe_id = entity_id.replace(":", "-")
        flat = _flatten_item_attrs(item)
        visible = [f for f in eff_schema if f.get("key") in set(visible_cols)] if visible_cols else eff_schema
        cells = [
            cell_renderers[f["key"]](entity_id, flat) if f["key"] in cell_renderers
            else display_cell(
                entity_id=entity_id,
                field=f["key"],
                value=flat.get(f["key"], ""),
                cell_type=f.get("type", "text"),
                options=f.get("options"),
                editable=f.get("editable", True),
            )
            for f in visible
        ]
        return Tr(*cells, id=f"row-{safe_id}", cls="data-row")

    async def _paired_display(token: str, entity_id: str, field: str):
        """Return a display cell TD for the pair/triple containing `field`."""
        schema, item, cat_schemas, locs = await asyncio.gather(
            api.get_item_schema(token), api.get_item(token, entity_id),
            api.get_all_category_schemas(token), api.get_locations(token),
        )
        # Purchase triple: purchase_unit + purchase_conversion_factor + sell_by (read-only)
        if field in ("purchase_unit", "purchase_conversion_factor"):
            from ui.components.table import purchase_display_cell
            return purchase_display_cell(
                entity_id=entity_id,
                pu_val=item.get("purchase_unit", ""),
                cf_val=item.get("purchase_conversion_factor", ""),
                sb_val=item.get("sell_by", ""),
            )
        from ui.components.table import paired_display_cell
        from celerp.services.units import format_qty
        locations = locs.get("items", [])
        peer = _PAIRED_FIELDS[field]
        # Determine which is primary (qty/weight) vs secondary (unit)
        primary, secondary = (field, peer) if field not in ("sell_by", "weight_unit") else (peer, field)
        pri_def, pri_type, pri_opts, _ = _resolve_field_def(primary, schema, cat_schemas, item, locations)
        sec_def, sec_type, sec_opts, _ = _resolve_field_def(secondary, schema, cat_schemas, item, locations)
        # Build unit map for format_qty; secondary is the unit field (sell_by / weight_unit)
        units_resp = await api.get_units(token)
        umap = {u["name"]: u for u in units_resp if u.get("name")}
        unit_name = item.get(secondary, "")
        fmt_fn = (lambda v, _u=unit_name, _m=umap: format_qty(v, _u, _m)) if pri_type == "number" else None
        return paired_display_cell(
            entity_id=entity_id,
            primary_field=primary, primary_value=item.get(primary, ""),
            secondary_field=secondary, secondary_value=item.get(secondary, ""),
            primary_type=pri_type, secondary_type=sec_type,
            primary_options=pri_opts, secondary_options=sec_opts,
            format_fn=fmt_fn,
        )

    @app.get("/api/items/{entity_id}/field/{field}/paired-edit")
    async def field_paired_edit_cell(request: Request, entity_id: str, field: str):
        """Return editable_cell for `field` with restore_url → paired-display."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if field not in _PAIRED_FIELDS:
            return P("Not a paired field", cls="cell-error")
        try:
            schema, item, cat_schemas, locs = await asyncio.gather(
                api.get_item_schema(token), api.get_item(token, entity_id),
                api.get_all_category_schemas(token), api.get_locations(token),
            )
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        locations = locs.get("items", [])
        f_def, cell_type, options, allow_custom = _resolve_field_def(field, schema, cat_schemas, item, locations)
        # Field-specific overrides
        if field in ("sell_by", "purchase_unit"):
            try:
                units_resp = await api.get_units(token)
                unit_names = [u["name"] for u in units_resp if u.get("name")]
            except Exception:
                unit_names = []
            cell_type, options, allow_custom = _apply_unit_field_override(field, cell_type, options, allow_custom, unit_names)
        elif field == "purchase_conversion_factor":
            # Plain number input
            cell_type = "number"
        elif field in ("weight", "pieces"):
            # Derived fields: block editing when sell_by qualifies
            try:
                units_resp = await api.get_units(token)
                _umap = {u["name"]: u for u in units_resp if u.get("name")}
            except Exception:
                _umap = {}
            sell_by = item.get("sell_by") or ""
            derived = (field == "weight" and is_weight_unit(sell_by, _umap)) or \
                      (field == "pieces" and is_pieces_unit(sell_by, _umap))
            if derived:
                return Td(
                    Span("Derived from Qty column", cls="cell-derived"),
                    cls="cell",
                    data_col=field,
                )
        from ui.components.table import editable_cell
        restore_url = f"/api/items/{entity_id}/field/{field}/paired-display"
        return editable_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                             cell_type=cell_type, options=options, allow_custom=allow_custom,
                             restore_url=restore_url)

    @app.get("/api/items/{entity_id}/field/{field}/paired-display")
    async def field_paired_display_cell(request: Request, entity_id: str, field: str):
        """Restore paired cell to display state (ESC or after save)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if field not in _PAIRED_FIELDS:
            return P("Not a paired field", cls="cell-error")
        try:
            return await _paired_display(token, entity_id, field)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")

    # ── Bulk actions (list-level) ─────────────────────────────────────────────

    def _bulk_destructive_success(message: str, redirect_qs: str = "") -> Response:
        """Return a bulk-action success response that clears the client-side selection.

        Sends HX-Trigger: celerpSelectionClear so the JS handler resets CelerpSelection
        and the toolbar before the table reloads.  Used for all destructive bulk actions
        (merge, delete, archive, expire) where source items leave the visible table.
        """
        from starlette.responses import HTMLResponse
        content = Div(
            P(message, cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get=f"/inventory/content{redirect_qs}",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            **({"hx_push_url": f"/inventory{redirect_qs}"} if redirect_qs else {}),
        )
        return HTMLResponse(to_xml(content), headers={"HX-Trigger": "celerpSelectionClear"})

    @app.post("/api/items/bulk/status")
    async def bulk_item_status(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        status = str(form.get("bulk_status", "")).strip()
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        if not status:
            return Div(P(t("flash.no_status_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            result = await api.bulk_set_status(token, entity_ids, status)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        updated = result.get("updated", len(entity_ids))
        return _bulk_destructive_success(f"{updated} item(s) updated to '{status}'.")

    @app.post("/api/items/bulk/transfer")
    async def bulk_item_transfer(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        location_id = str(form.get("bulk_location_id", "")).strip()
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        if not location_id:
            return Div(P(t("flash.no_location_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            result = await api.bulk_transfer(token, entity_ids, location_id)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        updated = result.get("updated", len(entity_ids))
        return _bulk_destructive_success(f"{updated} item(s) transferred.")

    @app.post("/api/items/bulk/delete")
    async def bulk_item_delete(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            result = await api.bulk_delete(token, entity_ids)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        deleted = result.get("deleted", len(entity_ids))
        return _bulk_destructive_success(f"{deleted} item(s) deleted.")

    # ── Bulk expire ──────────────────────────────────────────────────────

    @app.post("/api/items/bulk/expire")
    async def bulk_item_expire(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            result = await api.bulk_expire(token, entity_ids)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        expired = result.get("expired", len(entity_ids))
        return _bulk_destructive_success(f"{expired} item(s) expired.")

    # ── Bulk merge (direct — no preview modal) ───────────────────────────

    @app.post("/api/items/bulk/merge")
    async def bulk_item_merge(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_sku_from = str(form.get("target_sku_from", "")).strip()
        if len(entity_ids) < 2:
            return Div(P(t("inv.select_at_least_2_items_to_merge"), cls="flash flash--warning"), id="bulk-action-result")
        if not target_sku_from:
            return Div(P(t("inv.target_item_selection_is_required"), cls="flash flash--warning"), id="bulk-action-result")
        # Fetch items to compute totals and resolve attribute conflicts
        items = []
        for eid in entity_ids:
            try:
                items.append(await api.get_item(token, eid))
            except APIError as e:
                return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        total_qty = sum(float(it.get("quantity", 0) or 0) for it in items)
        _CORE_KEYS = _CORE_ITEM_COLS | {"id", "is_available", "is_expired", "children",
                                         "child_skus", "merged_into", "reserved_quantity",
                                         "tax_codes", "unit", "expires_at", "total_cost",
                                         "entity_id"}
        def _extract_attrs(it: dict) -> dict:
            return {k: v for k, v in it.items() if k not in _CORE_KEYS and not k.endswith("_price") and v is not None}
        item_attrs = [_extract_attrs(it) for it in items]
        all_attr_keys: set[str] = set()
        for attrs in item_attrs:
            all_attr_keys.update(attrs.keys())
        resolved_attrs: dict = {}
        for key in all_attr_keys:
            vals = [str(attrs[key]) for attrs in item_attrs if key in attrs]
            unique = set(vals)
            resolved_attrs[key] = vals[0] if len(unique) == 1 else "mixed"
        try:
            result = await api.merge_items(
                token,
                source_entity_ids=entity_ids,
                target_sku_from=target_sku_from,
                resulting_quantity=total_qty,
                resolved_attributes=resolved_attrs or None,
            )
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        # Find the target item's SKU for the post-merge filter
        target_item = next((it for it in items if it.get("entity_id") == target_sku_from or it.get("id") == target_sku_from), None)
        target_sku = target_item.get("sku", "") if target_item else ""
        redirect_qs = f"?q={target_sku}" if target_sku else ""
        return _bulk_destructive_success(t("inv.items_merged_successfully"), redirect_qs)

    # ── Bulk split (simplified single-qty) ───────────────────────────────

    async def _next_split_sku(token: str, parent_sku: str, exclude: set[str] | None = None) -> str:
        """Find next available child SKU suffix for splitting.

        DEMO-RGH-001 -> DEMO-RGH-001.1, DEMO-RGH-001.2, ...
        DEMO-RGH-001.1 -> DEMO-RGH-001.1.1, DEMO-RGH-001.1.2, ...
        exclude: set of SKUs already allocated in this batch (not yet committed to DB).
        """
        prefix = f"{parent_sku}."
        try:
            resp = await api.list_items(token, {"q": parent_sku, "limit": 200, "status": "all"})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
        except Exception:
            items = []
        taken: set[int] = set()
        for it in items:
            sku = str(it.get("sku", ""))
            if sku.startswith(prefix):
                suffix_part = sku[len(prefix):]
                if "." not in suffix_part:
                    try:
                        taken.add(int(suffix_part))
                    except ValueError:
                        pass
        # Also exclude in-batch allocations
        if exclude:
            for s in exclude:
                if s.startswith(prefix):
                    suffix_part = s[len(prefix):]
                    if "." not in suffix_part:
                        try:
                            taken.add(int(suffix_part))
                        except ValueError:
                            pass
        n = 1
        while n in taken:
            n += 1
        return f"{prefix}{n}"

    @app.get("/api/items/bulk/split-preview")
    async def bulk_split_preview(request: Request):
        """HTMX fragment: preview for bulk split (inline in toolbar, no modal)."""
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        entity_id = request.query_params.get("entity_id", "").strip()
        child_sku_param = request.query_params.get("child_sku", "").strip() or None
        if not entity_id:
            return Div()
        try:
            preview = await api.split_preview(token, entity_id, child_sku_param)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"))

        if preview.get("cannot_split"):
            return Div(P("Cannot split - only 1 piece", cls="flash flash--warning"))

        sell_by_label = preview.get("sell_by_label", preview.get("sell_by", ""))
        decimals = preview.get("unit_decimals", 0)
        weight_decimals = preview.get("weight_decimals", 2)
        fmt = f"{{:.{decimals}f}}"
        wfmt = f"{{:.{weight_decimals}f}}"

        # Show weight/pieces columns only when the parent actually has those values.
        show_weight = preview.get("has_weight", False)
        show_pieces = preview.get("has_pieces", False)

        headers = [Th(""), Th("SKU", cls="sp-th"), Th(f"QTY ({sell_by_label})", cls="sp-th")]
        if show_weight:
            headers.append(Th("Weight", cls="sp-th"))
        if show_pieces:
            headers.append(Th("Pieces", cls="sp-th"))

        def _static_td(val: str) -> FT:
            return Td(val, cls="sp-td")

        def _editable_td(name: str, val: str, oninput: str | None = None, onblur: str | None = None, max: str | None = None, min: str | None = None) -> FT:
            kwargs = dict(type="number", name=name, value=val, step="any", cls="form-input form-input--xs sp-input")
            if oninput:
                kwargs["oninput"] = oninput
            if onblur:
                kwargs["onblur"] = onblur
            if max is not None:
                kwargs["max"] = max
            if min is not None:
                kwargs["min"] = min
            return Td(Input(**kwargs), cls="sp-td")

        _child_weight_oninput = "splitRecalcMotherWeight(this)"
        _child_weight_onblur = "splitClampWeight(this)"
        _child_pieces_oninput = "splitRecalcMotherPieces(this)"
        _child_pieces_onblur = "splitClampPieces(this)"

        def _parcel_row(label: str, sku_cell: FT, qty_cell: FT, weight_val, pieces_val,
                        weight_name: str | None, pieces_name: str | None, is_child: bool = False) -> FT:
            cells = [Td(label, cls="sp-row-label"), sku_cell, qty_cell]
            if show_weight:
                w = wfmt.format(weight_val) if weight_val is not None else wfmt.format(0)
                if weight_name and is_child:
                    cells.append(_editable_td(weight_name, w, oninput=_child_weight_oninput, onblur=_child_weight_onblur))
                else:
                    # Mother: static display, updated by JS
                    cells.append(Td(Span(w, cls="mother-weight-display"), cls="sp-td"))
            if show_pieces:
                p = str(int(pieces_val)) if pieces_val is not None else "0"
                if is_child and pieces_name:
                    pieces_max = str(int(preview["parent_pieces"]) - 1)
                    cells.append(_editable_td(pieces_name, p, oninput=_child_pieces_oninput, onblur=_child_pieces_onblur, max=pieces_max))
                else:
                    # Mother pieces: static display updated by JS
                    cells.append(Td(Span(p, cls="mother-pieces-display"), cls="sp-td"))
            return Tr(*cells)

        mother_row = _parcel_row(
            "Mother",
            _static_td(preview["parent_sku"]),
            Td(Span(fmt.format(preview["parent_qty"]), cls="mother-qty-display"), cls="sp-td"),
            preview.get("parent_weight"),
            preview.get("parent_pieces"),
            weight_name=None,
            pieces_name=None,
            is_child=False,
        )
        child_row = _parcel_row(
            "Child",
            Td(Input(type="text", name="child_sku", value=preview["child_sku"],
                     cls="form-input sp-sku-input",
                     oninput="bulkSplitSkuChanged(this)"), cls="sp-td"),
            Td(Input(type="number", name="child_qty", value="0",
                     step=str(10 ** -decimals if decimals > 0 else 1), min="0",
                     max=fmt.format(preview["parent_qty"] - (10 ** -decimals if decimals > 0 else 1)),
                     cls="form-input form-input--xs sp-input",
                     onchange="bulkSplitChildQtyChanged(this)"), cls="sp-td"),
            None,
            0,
            weight_name="child_weight" if show_weight else None,
            pieces_name="child_pieces" if show_pieces else None,
            is_child=True,
        )

        form_data: dict = {
            "data_weight_decimals": str(weight_decimals),
            "data_unit_decimals": str(decimals),
            "data_parent_qty": str(preview["parent_qty"]),
        }
        if show_weight:
            form_data["data_parent_weight"] = str(preview["parent_weight"])
        if show_pieces:
            form_data["data_parent_pieces"] = str(int(preview["parent_pieces"]))

        return Form(
            Input(type="hidden", name="entity_id", value=entity_id),
            Table(
                Thead(Tr(*headers)),
                Tbody(mother_row, child_row),
                cls="split-preview-table",
            ),
            Button("Confirm", type="submit", cls="btn btn--primary btn--sm sp-confirm-btn"),
            hx_post="/api/items/bulk/split",
            hx_target="#bulk-action-result",
            hx_swap="outerHTML",
            onsubmit="bulkSplitSubmit(this)",
            id="bulk-split-preview-form",
            **form_data,
        )

    @app.post("/api/items/bulk/split")
    async def bulk_item_split(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()

        # Accept entity_id from form (preview form path) or from selected checkboxes
        eid = str(form.get("entity_id", "")).strip()
        if not eid:
            entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
            if len(entity_ids) != 1:
                return Div(P(t("inv.select_exactly_1_item_to_split"), cls="flash flash--warning"), id="bulk-action-result")
            eid = entity_ids[0]

        split_qty_raw = str(form.get("child_qty", "") or form.get("split_qty", "")).strip()
        try:
            split_qty = float(split_qty_raw)
        except (ValueError, TypeError):
            return Div(P(t("inv.invalid_split_quantity"), cls="flash flash--warning"), id="bulk-action-result")
        if split_qty <= 0:
            return Div(P(t("inv.split_quantity_must_be_greater_than_0"), cls="flash flash--warning"), id="bulk-action-result")

        try:
            item = await api.get_item(token, eid)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")

        current_qty = float(item.get("quantity", 0) or 0)
        if split_qty >= current_qty:
            return Div(P(f"Split quantity must be less than current quantity ({current_qty}).", cls="flash flash--warning"), id="bulk-action-result")

        orig_sku = str(item.get("sku", "") or "")

        # Child SKU: from preview form or auto-generate
        child_sku_input = str(form.get("child_sku", "")).strip()
        child_sku = child_sku_input if child_sku_input else await _next_split_sku(token, orig_sku)

        # Optional weight/pieces overrides from preview form
        def _opt_float(key: str) -> float | None:
            raw = str(form.get(key, "")).strip()
            try:
                return float(raw) if raw else None
            except ValueError:
                return None

        child_weight = _opt_float("child_weight")
        child_pieces = _opt_float("child_pieces")

        if child_pieces is not None:
            parent_pieces_raw = item.get("pieces") or (item.get("attributes") or {}).get("pieces")
            parent_pieces_val = float(parent_pieces_raw) if parent_pieces_raw is not None else None
            if parent_pieces_val is not None and child_pieces >= parent_pieces_val:
                return Div(P(f"Child pieces ({int(child_pieces)}) must be less than parent pieces ({int(parent_pieces_val)}).", cls="flash flash--warning"), id="bulk-action-result")

        child: dict = {"sku": child_sku, "quantity": split_qty}
        if child_weight is not None:
            child["weight"] = child_weight
        if child_pieces is not None:
            # pieces lives in attributes on the item
            child["attributes"] = {"pieces": child_pieces}

        try:
            await api.split_item(token, eid, [child])
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")

        from urllib.parse import quote
        remaining_qty = current_qty - split_qty
        # Filter to exactly the two parcels by their SKUs
        exact_skus = f"{quote(orig_sku)},{quote(child_sku)}"
        return _bulk_destructive_success(
            f"Split: {orig_sku} ({remaining_qty}) + {child_sku} ({split_qty}).",
            f"?skus={exact_skus}&status=all",
        )

    # ── Send-to search (HTMX dropdown) ───────────────────────────────────

    @app.get("/api/items/send-to/search")
    async def send_to_search(request: Request):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse([], status_code=401)
        doc_type = request.query_params.get("doc_type", "").strip()
        q = request.query_params.get("q", "").strip()
        try:
            if doc_type == "invoice":
                params: dict = {"doc_type": "invoice", "limit": 20}
                if q:
                    params["q"] = q
                else:
                    params["status"] = "draft"
                resp = await api.list_docs(token, params)
                items = resp.get("items", [])
                # filter by draft/awaiting_payment
                items = [d for d in items if d.get("status") in ("draft", "awaiting_payment")]
                return JSONResponse([
                    {"id": d.get("id") or d.get("entity_id", ""),
                     "label": d.get("ref_id") or d.get("doc_number") or d.get("id", ""),
                     "status": d.get("status", "")}
                    for d in items
                ])
            elif doc_type == "list":
                params = {"limit": 20}
                if q:
                    params["q"] = q
                else:
                    params["status"] = "draft"
                resp = await api.list_lists(token, params)
                items = resp.get("items", [])
                items = [d for d in items if d.get("status") in ("draft", "sent")]
                return JSONResponse([
                    {"id": d.get("id") or d.get("entity_id", ""),
                     "label": d.get("ref_id") or d.get("id", ""),
                     "status": d.get("status", "")}
                    for d in items
                ])
            elif doc_type == "memo":
                params = {"doc_type": "memo", "limit": 20}
                if q:
                    params["q"] = q
                else:
                    params["status"] = "draft"
                resp = await api.list_docs(token, params)
                items = resp.get("items", [])
                items = [d for d in items if d.get("status") in ("draft",)]
                return JSONResponse([
                    {"id": d.get("id") or d.get("entity_id", ""),
                     "label": d.get("ref_id") or d.get("doc_number") or d.get("id", ""),
                     "status": d.get("status", "")}
                    for d in items
                ])
        except APIError:
            pass
        return JSONResponse([])

    # ── Send-to action ────────────────────────────────────────────────────

    @app.post("/api/items/send-to")
    async def send_to_action(request: Request):
        from ui.routes.documents import _line_items_from_inventory
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        doc_type = str(form.get("send_to_doc_type", "")).strip()
        target_id = str(form.get("send_to_target", "")).strip()
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        if not doc_type:
            return Div(P(t("inv.no_document_type_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            if target_id and not target_id.startswith("__new__"):
                # Add to existing document
                if doc_type == "invoice":
                    new_lines = await _line_items_from_inventory(token, entity_ids)
                    doc = await api.get_doc(token, target_id)
                    combined = (doc.get("line_items") or []) + new_lines
                    subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
                    await api.patch_doc(token, target_id, {"line_items": combined, "subtotal": subtotal, "total": subtotal})
                    return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})
                elif doc_type == "list":
                    new_lines = await _line_items_from_inventory(token, entity_ids)
                    lst = await api.get_list(token, target_id)
                    combined = (lst.get("line_items") or []) + new_lines
                    subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
                    await api.patch_list(token, target_id, {"line_items": combined, "subtotal": subtotal, "total": subtotal})
                    return Response("", status_code=204, headers={"HX-Redirect": f"/lists/{target_id}"})
                elif doc_type == "memo":
                    new_lines = await _line_items_from_inventory(token, entity_ids)
                    doc = await api.get_doc(token, target_id)
                    combined = (doc.get("line_items") or []) + new_lines
                    subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
                    await api.patch_doc(token, target_id, {"line_items": combined, "subtotal": subtotal, "total": subtotal})
                    return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})
            else:
                # Create new document
                if doc_type == "invoice":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    from ui.routes.documents import _company_doc_taxes
                    doc_taxes = await _company_doc_taxes(token)
                    result = await api.create_doc(token, {"doc_type": "invoice", "status": "draft", "line_items": line_items, "doc_taxes": doc_taxes})
                    doc_id = result.get("entity_id") or result.get("id", "")
                    return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})
                elif doc_type == "list":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    result = await api.create_list(token, {"list_type": "quotation", "status": "draft", "line_items": line_items})
                    list_id = result.get("entity_id") or result.get("id", "")
                    return Response("", status_code=204, headers={"HX-Redirect": f"/lists/{list_id}"})
                elif doc_type == "memo":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    from ui.routes.documents import _company_doc_taxes
                    doc_taxes = await _company_doc_taxes(token)
                    result = await api.create_doc(token, {"doc_type": "memo", "status": "draft", "line_items": line_items, "doc_taxes": doc_taxes})
                    doc_id = result.get("entity_id") or result.get("id", "")
                    return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        return Div(P(t("inv.unknown_document_type"), cls="flash flash--warning"), id="bulk-action-result")

    # ── T3: Item action routes ───────────────────────────────────────────

    @app.post("/api/items/{entity_id}/adjust")
    async def item_adjust(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            new_qty = float(str(form.get("new_qty", "0")))
        except ValueError:
            new_qty = 0.0
        try:
            await api.adjust_item(token, entity_id, new_qty)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/transfer")
    async def item_transfer(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        location_id = str(form.get("location_id", "")).strip()
        try:
            await api.transfer_item(token, entity_id, location_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/reserve")
    async def item_reserve(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            qty = float(str(form.get("quantity", "0")))
        except ValueError:
            qty = 0.0
        reference = str(form.get("reference", "")).strip() or None
        try:
            await api.reserve_item(token, entity_id, qty, reference)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/unreserve")
    async def item_unreserve(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            qty = float(str(form.get("quantity", "0")))
        except ValueError:
            qty = 0.0
        try:
            await api.unreserve_item(token, entity_id, qty)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/price")
    async def item_price(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        # Read all price list names dynamically from company settings
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            price_lists = [{"name": "Retail"}, {"name": "Wholesale"}, {"name": "Cost"}]
        try:
            for pl in price_lists:
                pl_name = pl.get("name", "")
                conventional_key = f"{pl_name.lower()}_price"
                val = str(form.get(conventional_key, "")).strip()
                if val:
                    try:
                        price = float(val)
                    except ValueError:
                        continue
                    await api.set_item_price(token, entity_id, pl_name, price)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/status")
    async def item_status(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        status = str(form.get("status", "")).strip()
        try:
            await api.set_item_status(token, entity_id, status)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/expire")
    async def item_expire(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.expire_item(token, entity_id, reason)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/archive")
    async def item_archive(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.set_item_status(token, entity_id, "archived", reason=reason)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/restore")
    async def item_restore(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.set_item_status(token, entity_id, "available", reason=reason)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/return-to-vendor")
    async def item_return_to_vendor(request: Request, entity_id: str):
        """Return an item received from a bill/PO back to the vendor."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            qty = float(str(form.get("quantity", "0")))
        except ValueError:
            qty = 0.0
        reason = str(form.get("reason", "")).strip() or None
        if qty <= 0:
            return Div(Span(t("inv.quantity_must_be_greater_than_0"), cls="flash flash--error"), id="item-action-error")
        try:
            # Reduce item quantity
            item = await api.get_item(token, entity_id)
            current_qty = float(item.get("quantity", 0) or 0)
            new_qty = max(0.0, current_qty - qty)
            await api.adjust_item(token, entity_id, new_qty)
            # Log the return event on the item
            # Note: the source doc tracking for returns will be enhanced in future
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/bring-back-in")
    async def item_bring_back_in(request: Request, entity_id: str):
        """Return a consigned-out item back into inventory."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            qty = float(str(form.get("quantity", "0")))
        except ValueError:
            qty = 0.0
        if qty <= 0:
            return Div(Span(t("inv.quantity_must_be_greater_than_0"), cls="flash flash--error"), id="item-action-error")
        try:
            item = await api.get_item(token, entity_id)
            current_qty = float(item.get("quantity", 0) or 0)
            new_qty = current_qty + qty
            await api.adjust_item(token, entity_id, new_qty)
            # Update item status back to available
            await api.set_item_status(token, entity_id, "available")
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/split")
    async def item_split(request: Request, entity_id: str):
        import json as _json
        from urllib.parse import quote
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})

        # Get parent item for SKU
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        orig_sku = str(item.get("sku", "") or "")

        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                children = body.get("children", [])
            except Exception:
                return Div(Span(t("inv.invalid_json_body"), cls="flash flash--error"), id="item-action-error")
        else:
            form = await request.form()
            # Simple format: comma-separated quantities (auto-generate SKUs)
            parts_raw = str(form.get("parts", "")).strip()
            if parts_raw:
                try:
                    quantities = [float(p.strip()) for p in parts_raw.split(",") if p.strip()]
                except ValueError:
                    return Div(Span(t("inv.invalid_quantities_use_commaseparated_numbers"), cls="flash flash--error"), id="item-action-error")
                if len(quantities) < 1:
                    return Div(Span(t("inv.enter_at_least_one_split_quantity"), cls="flash flash--error"), id="item-action-error")
                # All quantities become new child items; the parent's quantity is reduced by the total.
                # Find current max suffix for auto-generating SKUs
                prefix = f"{orig_sku}."
                try:
                    resp = await api.list_items(token, {"q": orig_sku, "limit": 200, "status": "all"})
                    existing_items = resp.get("items", []) if isinstance(resp, dict) else resp
                except Exception:
                    existing_items = []
                max_suffix = 0
                for it in existing_items:
                    sku = str(it.get("sku", ""))
                    if sku.startswith(prefix) and "." not in sku[len(prefix):]:
                        try:
                            max_suffix = max(max_suffix, int(sku[len(prefix):]))
                        except ValueError:
                            pass
                children = []
                for i, qty in enumerate(quantities, start=1):
                    children.append({"sku": f"{prefix}{max_suffix + i}", "quantity": qty})
            else:
                return Div(Span(t("inv.enter_commaseparated_quantities_eg_321"), cls="flash flash--error"), id="item-action-error")
        if len(children) < 1:
            return Div(Span(t("inv.enter_at_least_one_split_quantity"), cls="flash flash--error"), id="item-action-error")
        try:
            await api.split_item(token, entity_id, children)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        redirect = f"/inventory?q={quote(orig_sku)}" if orig_sku else f"/inventory/{entity_id}"
        return Response("", status_code=204, headers={"HX-Redirect": redirect})

    @app.post("/api/items/{entity_id}/split-inline")
    async def item_split_inline(request: Request, entity_id: str):
        """Detail page split: one or more children, auto-SKU, redirect to exact filtered inventory."""
        from urllib.parse import quote as _quote
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        qty_raws = [v.strip() for v in form.getlist("split_qty") if v.strip()]
        if not qty_raws:
            return Span(t("inv.invalid_split_quantity"), cls="flash flash--error", id="item-action-error")
        children_qtys: list[float] = []
        for raw in qty_raws:
            try:
                q = float(raw)
            except (ValueError, TypeError):
                return Span(t("inv.invalid_split_quantity"), cls="flash flash--error", id="item-action-error")
            if q <= 0:
                return Span(t("inv.split_quantity_must_be_greater_than_0"), cls="flash flash--error", id="item-action-error")
            children_qtys.append(q)
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error", id="item-action-error")
        orig_sku = str(item.get("sku", "") or "")
        # Auto-generate sequential SKUs for each child
        children: list[dict] = []
        used_skus: set[str] = set()
        for qty in children_qtys:
            child_sku = await _next_split_sku(token, orig_sku, exclude=used_skus)
            used_skus.add(child_sku)
            children.append({"sku": child_sku, "quantity": qty})
        try:
            await api.split_item(token, entity_id, children)
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error", id="item-action-error")
        child_skus = [c["sku"] for c in children]
        skus_param = ",".join(_quote(s) for s in [orig_sku] + child_skus)
        redirect = f"/inventory?skus={skus_param}&status=all" if orig_sku else "/inventory"
        return Response("", status_code=204, headers={"HX-Redirect": redirect})

    @app.post("/api/items/merge")
    async def item_merge(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        source_entity_ids = [v.strip() for v in form.getlist("source_entity_ids") if v.strip()]
        target_sku_from = str(form.get("target_sku_from", "")).strip()
        if not source_entity_ids or not target_sku_from:
            return Span(t("inv.source_items_and_target_selection_are_required"), cls="flash flash--error")
        raw_qty = str(form.get("resulting_quantity", "")).strip()
        raw_cost = str(form.get("resulting_cost_price", "")).strip()
        resulting_name = str(form.get("resulting_name", "")).strip() or None
        try:
            resulting_quantity = float(raw_qty) if raw_qty else None
        except ValueError:
            return Span(t("error.invalid_resulting_quantity"), cls="flash flash--error")
        try:
            resulting_cost_price = float(raw_cost) if raw_cost else None
        except ValueError:
            return Span(t("inv.invalid_resulting_cost_price"), cls="flash flash--error")
        # Collect resolved attributes for string conflicts.
        resolved_attributes: dict = {}
        for key, val in form.multi_items():
            if key.startswith("resolved_attr_"):
                attr_key = key[len("resolved_attr_"):]
                resolved_attributes[attr_key] = str(val)
            elif key.startswith("numeric_attr_"):
                attr_key = key[len("numeric_attr_"):]
                try:
                    resolved_attributes[attr_key] = str(float(val))
                except (TypeError, ValueError):
                    pass
        try:
            result = await api.merge_items(
                token,
                source_entity_ids=source_entity_ids,
                target_sku_from=target_sku_from,
                resulting_quantity=resulting_quantity,
                resulting_cost_price=resulting_cost_price,
                resulting_name=resulting_name,
                resolved_attributes=resolved_attributes or None,
            )
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error")
        new_id = result.get("id", "")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{new_id}"})

    @app.post("/api/items/{entity_id}/duplicate")
    async def item_duplicate(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        new_sku = str(form.get("new_sku", "")).strip()
        try:
            source = await api.get_item(token, entity_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        if not new_sku:
            orig = str(source.get("sku", "") or "")
            # Auto-generate: {sku}-copy, -copy2, -copy3 ...
            candidate = f"{orig}-copy"
            try:
                resp = await api.list_items(token, {"q": orig, "limit": 100, "status": "all"})
                existing_skus = {str(it.get("sku", "")) for it in (resp.get("items", []) if isinstance(resp, dict) else resp)}
            except Exception:
                existing_skus = set()
            n = 2
            while candidate in existing_skus:
                candidate = f"{orig}-copy{n}"
                n += 1
            new_sku = candidate

        # Build create payload from source — carry all fields except id, status, location_name
        _SKIP = {"id", "status", "location_name", "created_at", "updated_at"}
        _CORE = {"sku", "name", "quantity", "category", "location_id",
                 "description", "unit", "sell_by", "tax_codes"}
        payload: dict = {"sku": new_sku}
        attrs: dict = {}
        for k, v in source.items():
            if k in _SKIP or k == "sku" or v is None:
                continue
            if k in _CORE or k.endswith("_price"):
                payload[k] = v
            else:
                attrs[k] = v
        if attrs:
            payload["attributes"] = attrs
        try:
            result = await api.create_item(token, payload)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        new_id = result.get("id", "")
        return Response("", status_code=204, headers={"HX-Redirect": f"/inventory/{new_id}"})

    # ── Item file routes (unified file system) ───────────────────────────────

    @app.post("/items/{entity_id}/files")
    async def item_upload_file(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        file = form.get("file")
        if file is None:
            return P(t("msg.no_file_provided"), cls="cell-error")
        try:
            await api.upload_item_file(token, entity_id, file)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.post("/items/{entity_id}/files/{file_id}/tag")
    async def item_tag_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        form = await request.form()
        tag = str(form.get("document_tag", ""))
        try:
            await api.tag_item_file(token, entity_id, file_id, tag)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.patch("/items/{entity_id}/files/{file_id}/description")
    async def item_describe_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        form = await request.form()
        description = str(form.get("description", ""))
        try:
            await api.describe_item_file(token, entity_id, file_id, description)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.post("/items/{entity_id}/files/{file_id}/hero")
    async def item_set_file_hero(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            await api.set_item_file_hero(token, entity_id, file_id)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.delete("/items/{entity_id}/files/{file_id}")
    async def item_delete_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            await api.delete_item_file(token, entity_id, file_id)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.get("/items/{entity_id}/files/_section")
    async def item_files_section_partial(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _item_files_section(entity_id, item)

    @app.get("/items/{entity_id}/files/{file_id}/download")
    async def item_download_file(request: Request, entity_id: str, file_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            resp = await api.download_item_file(token, entity_id, file_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            from starlette.responses import Response as _R
            return _R(str(e.detail), status_code=e.status)
        content_type = resp.headers.get("content-type", "application/octet-stream")
        cd = resp.headers.get("content-disposition", "")
        filename = "download"
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"').strip("'")
        from starlette.responses import Response as _R
        return _R(
            content=resp.content,
            media_type=content_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    # ── Legacy attachment upload (redirects to new files endpoint) ────────────

    @app.post("/api/items/{entity_id}/attachments")
    async def item_upload_attachment_legacy(request: Request, entity_id: str):
        """Deprecated: use /api/items/{entity_id}/files instead."""
        return Response("", status_code=308, headers={"Location": f"/api/items/{entity_id}/files"})

    @app.delete("/api/items/{entity_id}")
    async def item_delete(request: Request, entity_id: str):
        """Delete a single item from the row-action menu.

        The data_table component renders the row menu with:
            hx_delete="/api/items/{entity_id}"
            hx_target="#row-{safe_id}"
            hx_swap="outerHTML"

        Returning an empty 200 causes htmx to replace the row element with
        nothing, removing it from the DOM immediately without a page reload.
        """
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.bulk_delete(token, [entity_id])
        except APIError as e:
            return Tr(
                Td(Span(str(e.detail), cls="flash flash--error"), colspan="100"),
                id=f"row-{entity_id.replace(':', '-')}",
            )
        return Response("", status_code=200)

    @app.delete("/api/items/{entity_id}/attachments/{att_id}")
    async def item_delete_attachment(request: Request, entity_id: str, att_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            await api.delete_attachment(token, entity_id, att_id)
        except APIError as e:
            logger.warning("API error on delete attachment %s/%s: %s", entity_id, att_id, e.detail)
        return Response("", status_code=204, headers={"HX-Refresh": "true"})


def _bulk_toolbar(locations: list[dict], p: dict | None = None, total_items: int = 0) -> FT:
    """Sticky toolbar: [N selected] [Clear] [Action ▾] [context-area].

    Single action dropdown drives everything. Context area swaps based on selection.
    """
    from celerp.modules.slots import get as get_slot

    _loc_opt, _loc_js = add_new_option("+ Add new location", "/settings/inventory?tab=locations")
    loc_opts = [Option(loc.get("name", ""), value=loc.get("location_id") or loc.get("id", "")) for loc in locations]

    # Send-to targets from modules (e.g. Invoice, List, Consignment Out)
    send_to_targets = get_slot("send_to_targets")
    send_to_opts = [
        Option(tgt.get("label", ""), value=tgt.get("doc_type", ""))
        for tgt in send_to_targets
    ]

    # Module bulk actions - each shows a confirm button in the context area.
    # action_type="navigate" → opens in new tab via native form submit.
    # action_type="htmx" (default) → HTMX POST into #bulk-action-result.
    module_action_opts = []
    for action in get_slot("bulk_action"):
        action_id = action["form_action"].replace("/", "_").strip("_")
        module_action_opts.append(
            Option(action.get("label", "Action"), value=f"mod:{action_id}")
        )

    # Build the main Action dropdown options
    action_options = [
        Option(t("inv.action"), value="", disabled=True, selected=True),
        Option(t("btn.transfer"), value="transfer"),
        Option(t("inv.split"), value="split"),
        Option(t("inv.merge"), value="merge"),
    ]
    if send_to_opts:
        action_options.append(Option(t("inv.send_to"), value="send_to"))
    action_options.extend(module_action_opts)
    action_options.append(Option(t("inv.archive"), value="archive"))
    action_options.append(Option(t("inv.expire"), value="expire"))
    # Restore and Delete only shown when viewing archived/expired items
    active_status = (p or {}).get("status", "")
    if active_status in ("archived", "expired"):
        action_options.append(Option(t("inv.restore"), value="restore"))
        action_options.append(Option(t("btn.delete"), value="delete"))

    return Div(
        Span(t("doc.0_selected"), id="bulk-count", cls="bulk-count"),
        Button(t("btn.clear"), id="bulk-clear-btn", cls="btn btn--ghost btn--sm",
               onclick="CelerpSelection.clear();CelerpSelection.syncCheckboxes();"
                       "document.getElementById('bulk-count').textContent='0 selected';"
                       "document.getElementById('bulk-toolbar').classList.remove('is-active');"
                       "this.style.display='none';"
                       "_resetBulkActions();",
               style="display:none"),
        # Action dropdown
        Select(
            *action_options,
            id="bulk-action-select",
            cls="form-input form-input--sm",
            onchange="bulkActionChanged(this.value)",
        ),
        # Context area - swapped by JS based on action selection
        Div(id="bulk-context", cls="bulk-context"),
        Div(id="bulk-action-result"),
        # Hidden templates for context area content
        _bulk_context_templates(loc_opts, _loc_opt, _loc_js, send_to_opts, get_slot("bulk_action"), p or {}, total_items),
        id="bulk-toolbar",
        cls="bulk-toolbar",
        **{"data-hidden": "true"},
    )


def _bulk_context_templates(
    loc_opts: list,
    loc_new_opt,
    loc_new_js: str,
    send_to_opts: list,
    module_actions: list,
    p: dict | None = None,
    total_items: int = 0,
) -> FT:
    """Hidden <template> elements for each action's context area. JS clones them into #bulk-context."""
    from fasthtml.common import Template

    # Transfer: location dropdown + apply button
    transfer_tpl = Template(
        Form(
            Select(
                Option(t("inv.select_location"), value="", disabled=True, selected=True),
                *loc_opts,
                loc_new_opt,
                name="bulk_location_id", cls="form-input form-input--sm",
                onchange=loc_new_js,
            ),
            Button(t("btn.apply"), type="submit", cls="btn btn--primary btn--sm"),
            hx_post="/api/items/bulk/transfer",
            hx_target="#bulk-action-result",
            hx_swap="outerHTML",
            onsubmit="submitBulkAction(this)",
            cls="display-contents",
        ),
        id="tpl-transfer",
    )

    # Split: auto-loads preview on action select; child QTY editable in preview table
    split_tpl = Template(
        Div(
            Div(id="bulk-split-preview"),
            id="bulk-split-form",
        ),
        id="tpl-split",
    )

    # Merge: target dropdown + confirm
    merge_tpl = Template(
        Div(
            Select(
                Option(t("inv.select_target_item"), value="", disabled=True, selected=True),
                id="merge-target-select",
                name="target_sku_from", cls="form-input form-input--sm",
            ),
            Div(id="merge-confirm", style="display:none"),
            id="merge-context",
        ),
        id="tpl-merge",
    )

    # Send-to: doc type dropdown + searchable doc dropdown + send button
    send_to_tpl = Template(
        Form(
            Select(
                Option(t("inv.document_type"), value="", disabled=True, selected=True),
                *send_to_opts,
                name="send_to_doc_type", cls="form-input form-input--sm",
                onchange="sendToTypeChanged(this.value)",
                id="send-to-type-select",
            ),
            Select(
                Option(t("inv.new"), value="__new__"),
                name="send_to_target", cls="form-input form-input--sm",
                id="send-to-target-select",
            ),
            Button(t("btn.send"), type="submit", cls="btn btn--primary btn--sm"),
            hx_post="/api/items/send-to",
            hx_target="#bulk-action-result",
            hx_swap="outerHTML",
            onsubmit="submitBulkAction(this)",
            cls="display-contents",
        ),
        id="tpl-send-to",
    )

    # Module action templates.
    # navigate type: native form submit opening a new tab (for full-page responses).
    # htmx type (default): HTMX POST into #bulk-action-result.
    # celerp-labels gets a special inline template selector to skip the intermediate page.
    module_tpls = []
    for action in module_actions:
        action_id = action["form_action"].replace("/", "_").strip("_")
        is_labels = action.get("_module") == "celerp-labels"
        is_navigate = action.get("action_type") == "navigate"

        if is_labels:
            form = Form(
                # Template selector loaded via HTMX on first use
                Select(
                    Option(t("label.loading_templates"), value="", disabled=True, selected=True),
                    name="template_id",
                    id="bulk-labels-template-select",
                    cls="form-input form-input--sm",
                    hx_get="/labels/template-options",
                    hx_trigger="load",
                    hx_swap="innerHTML",
                    hx_target="#bulk-labels-template-select",
                ),
                Button(action.get("label", t("btn._print_labels")), type="submit", cls="btn btn--primary btn--sm"),
                action="/labels/print-bulk/generate",
                method="post",
                target="_blank",
                onsubmit="submitBulkAction(this)",
                cls="display-contents",
            )
            module_tpls.append(Template(form, id=f"tpl-mod-{action_id}"))
        elif is_navigate:
            form = Form(
                Button(action.get("label", "Go"), type="submit", cls="btn btn--primary btn--sm"),
                action=action["form_action"],
                method="post",
                target="_blank",
                onsubmit="submitBulkAction(this)",
                cls="display-contents",
            )
            module_tpls.append(Template(form, id=f"tpl-mod-{action_id}"))
        else:
            form = Form(
                Button(action.get("label", "Go"), type="submit", cls="btn btn--primary btn--sm"),
                hx_post=action["form_action"],
                hx_target="#bulk-action-result",
                hx_swap="outerHTML",
                onsubmit="submitBulkAction(this)",
                cls="display-contents",
            )
            module_tpls.append(Template(form, id=f"tpl-mod-{action_id}"))

    return Div(
        transfer_tpl,
        split_tpl,
        merge_tpl,
        send_to_tpl,
        *module_tpls,
        style="display:none",
    )


# ---------------------------------------------------------------------------
# Vertical-specific status configuration
# ---------------------------------------------------------------------------
# Status filter tabs (value, label) shown in the tab bar per vertical.
# "memo" verticals (gems, watches, art, coins, wine) use memo_out + expired.
# "perishable" verticals (food, agricultural) use expired but not memo_out.
# Generic verticals show just available/reserved/sold.
_VERTICAL_STATUS_TABS: dict[str, list[tuple[str, str]]] = {
    "gemstones": [
        ("", "Available"), ("reserved", "Reserved"), ("memo_out", "On Memo"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "watches_accessories": [
        ("", "Available"), ("reserved", "Reserved"), ("memo_out", "On Memo"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "artwork": [
        ("", "Available"), ("reserved", "Reserved"), ("memo_out", "On Memo"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "coins_precious_metals": [
        ("", "Available"), ("reserved", "Reserved"), ("memo_out", "On Memo"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "wine_spirits": [
        ("", "Available"), ("reserved", "Reserved"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "food_beverage": [
        ("", "Available"), ("reserved", "Reserved"),
        ("sold", "Sold"), ("expired", "Expired"), ("archived", "Archived"), ("all", "All"),
    ],
    "agricultural": [
        ("", "Available"), ("reserved", "Reserved"),
        ("sold", "Sold"), ("expired", "Expired"), ("archived", "Archived"), ("all", "All"),
    ],
}
_DEFAULT_STATUS_TABS: list[tuple[str, str]] = [
    ("", "Available"), ("reserved", "Reserved"), ("sold", "Sold"),
    ("archived", "Archived"), ("all", "All"),
]

# Status card definitions (key, label, color) per vertical.
_VERTICAL_STATUS_CARDS: dict[str, list[tuple[str, str, str]]] = {
    "gemstones": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("memo_out", "On Memo", "yellow"),
    ],
    "wine_spirits": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
    ],
    "food_beverage": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("expired", "Expired", "red"),
    ],
    "agricultural": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("expired", "Expired", "red"),
    ],
    "watches_accessories": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("memo_out", "On Memo", "yellow"),
    ],
    "artwork": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("memo_out", "On Memo", "yellow"),
    ],
    "coins_precious_metals": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
        ("memo_out", "On Memo", "yellow"),
    ],
}
_DEFAULT_STATUS_CARDS: list[tuple[str, str, str]] = [
    ("available", "Available", "green"),
    ("reserved", "Reserved", "blue"),
]


def _vertical_status_filter_tabs(vertical: str) -> list[tuple[str, str]]:
    return _VERTICAL_STATUS_TABS.get(vertical, _DEFAULT_STATUS_TABS)


def _vertical_status_card_defs(vertical: str) -> list[tuple[str, str, str]]:
    return _VERTICAL_STATUS_CARDS.get(vertical, _DEFAULT_STATUS_CARDS)


def _inventory_status_cards(count_by_status: dict, active_status: str, vertical: str = "", p: dict | None = None, lang: str = "en") -> FT:
    """Status cards driven by backend count_by_status dict (scoped to active category/status filter).

    When a specific status filter is active (sold/archived/etc.), shows a single
    'All' card with the total count for that filtered view instead of the
    available/reserved breakdown (which would all be 0 and is meaningless).
    """
    base_state = {k: v for k, v in _base_state(p or {}).items() if k != "status"}
    base_url = "/inventory" + (f"?{urlencode(base_state)}" if base_state else "")

    # When viewing a specific hidden/archived status, the available/reserved card
    # defs are irrelevant. Show a single total card instead.
    _HIDDEN = {"sold", "archived", "merged", "expired"}
    if active_status and active_status not in ("", "all"):
        total = sum(count_by_status.values())
        cards = [{"label": t("chip.total", lang), "count": total, "status": active_status, "color": "gray"}]
        return status_cards(cards, base_url, active_status)

    _CARD_DEFS = _vertical_status_card_defs(vertical)
    cards = [
        {"label": label, "count": count_by_status.get(key, 0), "status": key, "color": color}
        for key, label, color in _CARD_DEFS
    ]
    return status_cards(cards, base_url, active_status or None)


def _inventory_empty_state(p: dict) -> FT:
    """Context-aware empty state: only show import CTA on unfiltered views."""
    active_status = p.get("status", "")
    active_q = p.get("q", "")
    if active_status:
        label = active_status.replace("_", " ").title()
        return Div(P(f"No {label.lower()} items.", cls="empty-state-msg"), cls="empty-state", id="data-table")
    if active_q:
        return Div(P(f"No results for '{active_q}'.", cls="search-empty--table"), cls="empty-state", id="data-table")
    return empty_state_cta("No items in inventory.", "Import from CSV", "/inventory/import")


def _category_tabs(category_counts: dict, p: dict, total_scoped: int | None = None) -> FT:
    if not category_counts and not total_scoped:
        return ""

    base = {k: v for k, v in _base_state(p).items() if k != "category"}

    def _tab(label: str, cat: str, active: bool) -> FT:
        state = {**base, "category": cat} if cat else base
        content_url = "/inventory/content" + (f"?{urlencode(state)}" if state else "")
        page_url = "/inventory" + (f"?{urlencode(state)}" if state else "")
        return A(
            label,
            href=page_url,
            hx_get=content_url,
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=page_url,
            cls=f"category-tab {'category-tab--active' if active else ''}",
        )

    total = total_scoped if total_scoped is not None else sum(category_counts.values())
    tabs = [_tab(f"All ({total})", "", not p.get("category"))]
    for cat, count in sorted(category_counts.items()):
        tabs.append(_tab(f"{cat} ({count})", cat, p.get("category") == cat))
    return Div(*tabs, cls="category-tabs", id="category-tabs")


def _valuation_bar(valuation: dict, currency: str | None = None, lang: str = "en", status: str = "") -> FT:
    from ui.components.table import fmt_money
    active_count = valuation.get('active_item_count', valuation.get('item_count', 0))
    # Label reflects the active status filter
    if status == "sold":
        count_label = t("chip.sold", lang)
    elif status == "archived":
        count_label = t("chip.archived", lang)
    elif status and status != "all":
        count_label = status.replace("_", " ").title()
    else:
        count_label = t("chip.available", lang)
    chips = [Span(f"{count_label}: {active_count:,}", cls="val-chip")]
    # Dynamic price totals from API
    price_totals = valuation.get("price_totals", {})
    if price_totals:
        for name, total in price_totals.items():
            chips.append(Span(f"{name}: {fmt_money(total, currency)}", cls="val-chip"))
    else:
        # Backward-compatible fallback
        chips.append(Span(f"{t('th.cost', lang)}: {fmt_money(valuation.get('cost_total', 0.0), currency)}", cls="val-chip"))
        chips.append(Span(f"{t('th.retail', lang)}: {fmt_money(valuation.get('retail_total', 0.0), currency)}", cls="val-chip"))
        chips.append(Span(f"{t('th.wholesale', lang)}: {fmt_money(valuation.get('wholesale_total', 0.0), currency)}", cls="val-chip"))
    return Div(*chips, cls="valuation-bar")


def _status_tabs(p: dict, vertical: str = "") -> FT:
    """Status filter tabs. Default view excludes sold/archived. Vertical controls which statuses appear."""
    _TABS = _vertical_status_filter_tabs(vertical)
    active_status = p.get("status", "")
    base = {k: v for k, v in _base_state(p).items() if k != "status"}

    def _tab(s: str, label: str) -> FT:
        state = {**base, "status": s} if s else base
        content_url = "/inventory/content" + (f"?{urlencode(state)}" if state else "")
        page_url = "/inventory" + (f"?{urlencode(state)}" if state else "")
        return A(
            label,
            href=page_url,
            cls=f"category-tab {'category-tab--active' if active_status == s else ''}",
            hx_get=content_url,
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=page_url,
        )

    tabs = [_tab(s, label) for s, label in _TABS]
    return Div(*tabs, cls="category-tabs status-tabs", id="status-tabs")


def _inventory_type_tabs(p: dict) -> FT:
    """Filter tabs for inventory_type (all / stocked / service / non_stocked)."""
    active = p.get("inventory_type", "")
    _TABS = [
        ("", "All Types"),
        ("stocked", "Stocked"),
        ("service", "Services"),
        ("non_stocked", "Non-Stocked"),
    ]
    base = {k: v for k, v in _base_state(p).items() if k != "inventory_type"}

    def _tab(it: str, label: str) -> FT:
        state = {**base, "inventory_type": it} if it else base
        content_url = "/inventory/content" + (f"?{urlencode(state)}" if state else "")
        page_url = "/inventory" + (f"?{urlencode(state)}" if state else "")
        return A(
            label,
            href=page_url,
            cls=f"category-tab {'category-tab--active' if active == it else ''}",
            hx_get=content_url,
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=page_url,
        )

    return Div(*[_tab(it, label) for it, label in _TABS], cls="category-tabs inventory-type-tabs", id="inventory-type-tabs")
_PAIRED_TABLE: dict[str, str] = {"quantity": "sell_by", "weight": "weight_unit", "purchase_unit": "purchase_conversion_factor"}
# Derived from _PAIRED_TABLE — secondary fields already rendered inside paired cells; exclude from standalone rows
_PAIRED_SECONDARY_KEYS: frozenset[str] = frozenset(_PAIRED_TABLE.values())
# Core item fields shown in the left (core details) panel on the detail page — single definition
_ITEM_CORE_KEYS: frozenset[str] = frozenset({
    "sku", "name", "status", "category", "quantity", "weight", "weight_unit",
    "sell_by", "allow_splitting", "barcode", "hs_code", "location_name",
    "short_description", "purchase_sku", "purchase_name", "purchase_unit",
    "purchase_conversion_factor",
})


def _inventory_cell_renderers(schema: list[dict], unit_names: list[str] | None = None, units_map: dict[str, dict] | None = None, category_label_map: dict | None = None) -> dict:
    """Build cell_renderers dict for paired/triple columns.

    Handles:
    - quantity+sell_by paired cell
    - weight+weight_unit paired cell
    - purchase_unit+purchase_conversion_factor+sell_by triple cell
    - weight derived from qty when sell_by is a weight unit
    - pieces derived from qty when sell_by is a pieces unit

    unit_names: company unit names used as sell_by/purchase_unit dropdown options.
    units_map: full unit dict keyed by name, used to check unit_type for derivation.
    """
    from ui.components.table import paired_display_cell, display_cell
    from celerp.services.units import format_qty
    schema_keys = {f["key"] for f in schema}
    renderers: dict = {}
    _umap = units_map or {}
    # sell_by options: company units (searchable select); fallback to text if no units
    sell_by_opts = unit_names or None
    paired_options: dict[str, list[str] | None] = {
        "sell_by": sell_by_opts,
        "weight_unit": _UNIVERSAL_FIELD_OPTIONS.get("weight_unit"),
    }
    for primary, secondary in _PAIRED_TABLE.items():
        if primary in schema_keys and secondary in schema_keys:
            pri_def = next((f for f in schema if f["key"] == primary), {})
            sec_def = next((f for f in schema if f["key"] == secondary), {})
            sec_opts = paired_options.get(secondary)
            sec_type = "select" if sec_opts else sec_def.get("type", "text")
            def _make(pri=primary, sec=secondary, pt=pri_def.get("type", "number"),
                      st=sec_type, po=pri_def.get("options"), so=sec_opts, _um=_umap):
                def renderer(entity_id: str, row: dict, _umap=_um):
                    # Format primary numeric value using its unit's decimal precision
                    raw_pri = row.get(pri, "")
                    unit_for_pri = row.get(sec, "") if pri in ("quantity", "weight") else None
                    fmt_pri = format_qty(raw_pri, unit_for_pri, _umap) if pt == "number" else raw_pri
                    return paired_display_cell(
                        entity_id=entity_id,
                        primary_field=pri, primary_value=fmt_pri,
                        secondary_field=sec, secondary_value=row.get(sec, ""),
                        primary_type=pt, secondary_type=st,
                        primary_options=po, secondary_options=so,
                    )
                return renderer
            renderers[primary] = _make()

    # purchase_unit triple renderer: overrides the generic paired renderer from _PAIRED_TABLE loop
    # Shows: purchase_unit → conversion_factor sell_by (sell_by read-only from item)
    if "purchase_unit" in schema_keys:
        def _purchase_renderer(entity_id: str, row: dict) -> FT:
            from ui.components.table import purchase_display_cell
            return purchase_display_cell(
                entity_id=entity_id,
                pu_val=row.get("purchase_unit", ""),
                cf_val=row.get("purchase_conversion_factor", ""),
                sb_val=row.get("sell_by", ""),
            )
        renderers["purchase_unit"] = _purchase_renderer

    # Derived cell renderers for weight and pieces
    # weight: derived from qty when sell_by is a weight unit; falls back to paired cell
    if "weight" in schema_keys:
        _weight_paired = renderers.get("weight")  # paired renderer built above (may be None)
        def _weight_renderer(entity_id: str, row: dict, _umap=_umap, _paired=_weight_paired) -> FT:
            sell_by = row.get("sell_by") or ""
            if is_weight_unit(sell_by, _umap):
                qty_val = row.get("quantity", "")
                fmt = format_qty(qty_val, sell_by, _umap)
                decimals = _umap.get(sell_by, {}).get("decimals", "")
                return Td(
                    Span(
                        f"{fmt} {sell_by}" if fmt not in ("", None) else EMPTY,
                        title="Derived from Qty column",
                        cls="cell-derived",
                    ),
                    cls="cell cell--number",
                    data_col="weight",
                    data_decimals=str(decimals),
                )
            # Not a weight unit - use paired cell (weight + weight_unit) or plain editable
            if _paired:
                return _paired(entity_id, row)
            return display_cell(entity_id=entity_id, field="weight", value=row.get("weight", ""), cell_type="number", editable=True)
        renderers["weight"] = _weight_renderer

    # pieces: derived from qty when sell_by is a pieces unit
    if "pieces" in schema_keys:
        def _pieces_renderer(entity_id: str, row: dict, _umap=_umap) -> FT:
            sell_by = row.get("sell_by") or ""
            if is_pieces_unit(sell_by, _umap):
                qty_val = row.get("quantity", "")
                fmt = format_qty(qty_val, sell_by, _umap)
                decimals = _umap.get(sell_by, {}).get("decimals", "")
                return Td(
                    Span(
                        fmt if fmt not in ("", None) else EMPTY,
                        title="Derived from Qty column",
                        cls="cell-derived",
                    ),
                    cls="cell cell--number",
                    data_col="pieces",
                    data_decimals=str(decimals),
                )
            return display_cell(entity_id=entity_id, field="pieces", value=row.get("pieces", ""), cell_type="number", editable=True)
        renderers["pieces"] = _pieces_renderer

    # Category renderer: shows display name instead of slug
    if category_label_map:
        _clm = category_label_map
        def _cat_renderer(entity_id: str, row: dict, _lm=_clm) -> FT:
            return display_cell(entity_id=entity_id, field="category", value=row.get("category", ""),
                                cell_type="select", editable=True, label_map=_lm)
        renderers["category"] = _cat_renderer

    return renderers


def _column_manager(schema: list[dict], p: dict, active_cat: str = "", visible_cols: list[str] | None = None, keep_open: bool = False) -> FT:
    """Column manager dropdown with immediate JS toggle + localStorage + drag-and-drop reorder.

    Server-side pref save is preserved for cross-device sync (background fetch).
    Client-side: checkboxes immediately show/hide columns and persist to localStorage.
    """
    import json as _json
    selected = set(visible_cols) if visible_cols else {f.get("key") for f in schema if f.get("show_in_table", True)}
    cat_pref = active_cat or "__all__"
    # JS data for all columns (key, label, visible)
    col_data = [{"key": f.get("key", ""), "label": f.get("label", f.get("key", ""))} for f in schema]
    col_data_js = _json.dumps(col_data)
    selected_js = _json.dumps(sorted(selected))
    # Hidden inputs for fallback server save (category, status, sort etc.)
    hidden_state = {k: v for k, v in _base_state(p).items() if k != "cols"}
    hidden_state["_cat_pref"] = cat_pref

    # Build checkbox list for initial render
    checkboxes = [
        Label(
            Input(
                type="checkbox",
                name="cols",
                value=f.get("key"),
                checked=f.get("key") in selected,
                id=f"col-chk-{f.get('key', '')}",
            ),
            Span(f.get("label", f.get("key"))),
            cls="column-option",
            draggable="true",
            data_col=f.get("key", ""),
        )
        for f in schema
    ]

    hidden_inputs = [Input(type="hidden", name=k, value=v) for k, v in hidden_state.items()]

    # JS: localStorage key matches data_table's PAGE_KEY for inventory
    paired_secondaries_js = _json.dumps(list(_PAIRED_TABLE.values()))

    col_mgr_js = f"""
(function() {{
  var LS_VIS_KEY = 'celerp_cols_inventory';
  var LS_ORDER_KEY = 'celerp_col_order_inventory';
  var CAT_PREF = '{cat_pref}';
  var ALL_COLS = {col_data_js};
  // Keys that are now merged into their primary column - strip from saved prefs
  var MERGED_SECONDARIES = {paired_secondaries_js};
  var btn = document.getElementById('col-mgr-btn');
  var menu = document.getElementById('col-mgr-menu');
  if (!btn || !menu) return;

  // Load visibility from localStorage
  function loadVis() {{
    try {{ return JSON.parse(localStorage.getItem(LS_VIS_KEY) || 'null'); }} catch(e) {{ return null; }}
  }}
  function saveVis(prefs) {{
    localStorage.setItem(LS_VIS_KEY, JSON.stringify(prefs));
  }}

  // Load order from localStorage
  function loadOrder() {{
    try {{ return JSON.parse(localStorage.getItem(LS_ORDER_KEY) || 'null'); }} catch(e) {{ return null; }}
  }}
  function saveOrder(order) {{
    localStorage.setItem(LS_ORDER_KEY, JSON.stringify(order));
  }}

  // Apply column visibility to the data table
  function applyVisToTable(prefs) {{
    var table = document.getElementById('data-table');
    if (!table) return;
    var ths = Array.from(table.querySelectorAll('thead th[data-key]'));
    var rows = Array.from(table.querySelectorAll('tbody tr.data-row'));
    ths.forEach(function(th) {{
      var key = th.dataset.key;
      var colIdx = Array.from(th.parentNode.children).indexOf(th);
      var show = prefs[key] !== false;
      th.style.display = show ? '' : 'none';
      rows.forEach(function(tr) {{
        var td = tr.querySelector('[data-col="' + key + '"]');
        if (td) td.style.display = show ? '' : 'none';
      }});
    }});
  }}

  // Sync checkboxes in menu to match localStorage
  function syncCheckboxes() {{
    var prefs = loadVis() || {{}};
    menu.querySelectorAll('input[type=checkbox]').forEach(function(cb) {{
      cb.checked = prefs[cb.value] !== false;
    }});
  }}

  // Apply column order to table (move TH and TD columns)
  function applyOrderToTable(order) {{
    if (!order || !order.length) return;
    var table = document.getElementById('data-table');
    if (!table) return;
    var thead_tr = table.querySelector('thead tr');
    if (!thead_tr) return;
    var actionsTh = thead_tr.querySelector('.col-actions');
    // Move TH elements into order (before actions column)
    order.forEach(function(key) {{
      var th = thead_tr.querySelector('th[data-key="' + key + '"]');
      if (th && actionsTh) thead_tr.insertBefore(th, actionsTh);
    }});
    // Re-order tbody cells to match header using data-col attribute
    var allThs = Array.from(thead_tr.querySelectorAll('th[data-key]'));
    table.querySelectorAll('tbody tr.data-row').forEach(function(tr) {{
      var cells = Array.from(tr.children);
      var checkboxTd = cells[0];
      var actionsTd = cells[cells.length - 1];
      var dataCells = allThs.map(function(th) {{
        return cells.find(function(td) {{ return td.dataset.col === th.dataset.key; }});
      }}).filter(Boolean);
      [checkboxTd].concat(dataCells).concat([actionsTd]).forEach(function(td) {{
        if (td) tr.appendChild(td);
      }});
    }});
  }}

  // Mirror the picker label order to match a given key array (picker is source of truth)
  function applyOrderToPicker(order) {{
    if (!order || !order.length) return;
    var labels = menu.querySelectorAll('label[data-col]');
    if (!labels.length) return;
    var parent = labels[0].parentNode;
    // Move labels into the declared order; unmentioned keys stay at end
    order.forEach(function(key) {{
      var lbl = menu.querySelector('label[data-col="' + key + '"]');
      if (lbl) parent.appendChild(lbl);
    }});
  }}

  // Get current picker order (label DOM order = source of truth)
  function pickerOrder() {{
    return Array.from(menu.querySelectorAll('label[data-col]')).map(function(l) {{ return l.dataset.col; }});
  }}

  // Save cols to server (background, no page reload)
  function saveToServer(visibleKeys) {{
    var form = new FormData();
    visibleKeys.forEach(function(k) {{ form.append('cols', k); }});
    Object.entries({_json.dumps(hidden_state)}).forEach(function(kv) {{
      form.append(kv[0], kv[1]);
    }});
    fetch('/inventory/columns', {{method:'POST', body:form}}).catch(function(){{}});
  }}

  // Toggle open/close
  btn.addEventListener('click', function(e) {{
    e.stopPropagation();
    var isOpen = menu.style.display !== 'none';
    menu.style.display = isOpen ? 'none' : '';
    if (!isOpen) syncCheckboxes();
  }});

  // Close on outside click
  document.addEventListener('click', function(e) {{
    if (!btn.contains(e.target) && !menu.contains(e.target)) {{
      menu.style.display = 'none';
    }}
  }});

  // Checkbox change: immediate column toggle + re-apply order so new column
  // appears at its picker position rather than at the DOM end of the table.
  menu.addEventListener('change', function(e) {{
    if (e.target.type !== 'checkbox') return;
    var key = e.target.value;
    var prefs = loadVis() || {{}};
    // Init prefs from current state if empty
    if (!Object.keys(prefs).length) {{
      ALL_COLS.forEach(function(c) {{ prefs[c.key] = {_json.dumps(sorted(selected))} .indexOf(c.key) !== -1; }});
    }}
    prefs[key] = e.target.checked;
    saveVis(prefs);
    applyVisToTable(prefs);
    // Re-apply picker order so the newly-visible column lands in the right slot
    applyOrderToTable(pickerOrder());
    // Save visible keys to server
    var visibleKeys = ALL_COLS.filter(function(c) {{ return prefs[c.key] !== false; }}).map(function(c){{return c.key;}});
    saveToServer(visibleKeys);
  }});

  // Drag-and-drop reordering within column manager menu
  var dragSrc = null;
  menu.querySelectorAll('label[draggable]').forEach(function(lbl) {{
    lbl.addEventListener('dragstart', function(e) {{
      dragSrc = lbl;
      e.dataTransfer.effectAllowed = 'move';
      lbl.style.opacity = '0.5';
    }});
    lbl.addEventListener('dragend', function() {{
      lbl.style.opacity = '';
      dragSrc = null;
    }});
    lbl.addEventListener('dragover', function(e) {{
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    }});
    lbl.addEventListener('drop', function(e) {{
      e.preventDefault();
      if (!dragSrc || dragSrc === lbl) return;
      // Swap in picker DOM
      var parent = lbl.parentNode;
      var srcNext = dragSrc.nextSibling;
      parent.insertBefore(dragSrc, lbl);
      if (srcNext) parent.insertBefore(lbl, srcNext); else parent.appendChild(lbl);
      dragSrc.style.opacity = '';
      // Persist new order and apply to table
      var newOrder = pickerOrder();
      saveOrder(newOrder);
      applyOrderToTable(newOrder);
    }});
  }});

  // Listen for table-header drag reorder (fired by data_table.py drag handler)
  document.addEventListener('celerp:col-reorder', function(e) {{
    if (!e.detail || !e.detail.order) return;
    applyOrderToPicker(e.detail.order);
  }});

  // Init: apply localStorage state on page load
  // Strip merged secondary keys from any saved prefs (migration for users who had them as separate columns)
  var storedVis = loadVis();
  if (storedVis) {{
    MERGED_SECONDARIES.forEach(function(k) {{ delete storedVis[k]; }});
    applyVisToTable(storedVis);
  }}
  var storedOrder = loadOrder();
  if (storedOrder) {{
    storedOrder = storedOrder.filter(function(k) {{ return MERGED_SECONDARIES.indexOf(k) === -1; }});
    applyOrderToPicker(storedOrder);
    applyOrderToTable(storedOrder);
  }}

  // Keep menu closed unless keep_open is set
  {'menu.style.display = "";' if keep_open else 'menu.style.display = "none";'}
}})();
"""

    return Div(
        Button(t("btn.manage_columns"), id="col-mgr-btn", cls="btn btn--secondary", type="button"),
        Div(
            *checkboxes,
            Button(
                t("btn.reset_columns"),
                id="col-mgr-reset",
                cls="btn btn--sm btn--ghost col-mgr-reset-btn",
                type="button",
                onclick=(
                    f"localStorage.removeItem('celerp_cols_inventory');"
                    f"localStorage.removeItem('celerp_col_order_inventory');"
                    f"localStorage.removeItem('celerp_col_widths_inventory');"
                    f"fetch('/inventory/columns',{{method:'POST',body:new FormData()}});"
                    f"location.reload();"
                ),
                title=t("btn.reset_columns_title"),
            ),
            Form(
                *hidden_inputs,
                id="col-mgr-form",
                style="display:none",
            ),
            cls="column-menu",
            id="col-mgr-menu",
            style="display:none" if not keep_open else "",
        ),
        Script(col_mgr_js),
        cls="column-manager",
        id="col-mgr-details",
    )



def _attachments_panel(entity_id: str, item: dict) -> FT:
    """Attachments panel with unified drag-drop + click-to-browse upload zone."""
    attachments: list[dict] = item.get("attachments") or []
    images = [a for a in attachments if a.get("type") == "image"]
    docs = [a for a in attachments if a.get("type") != "image"]

    def _img_card(a: dict) -> FT:
        return Div(
            A(Img(src=a["url"], cls="attachment-thumb", alt=a.get("filename", ""), loading="lazy"), href=a["url"], target="_blank"),
            Div(
                Span(a.get("filename", "image"), cls="attachment-name"),
                Button(t("btn.u00d7"),
                    cls="btn btn--icon btn--danger",
                    hx_delete=f"/api/items/{entity_id}/attachments/{a['id']}",
                    hx_confirm="Remove this image?",
                    hx_target="#attachments-panel",
                    hx_swap="outerHTML",
                ),
                cls="attachment-meta",
            ),
            cls="attachment-card attachment-card--image",
        )

    def _doc_card(a: dict) -> FT:
        label = a.get("label") or a.get("filename", "document")
        return Div(
            A(
                Span(t("doc.u0001f4c4"), cls="attachment-doc-icon"),
                Span(label, cls="attachment-name"),
                href=a["url"],
                target="_blank",
                cls="attachment-doc-link",
            ),
            Button(t("btn.u00d7"),
                cls="btn btn--icon btn--danger",
                hx_delete=f"/api/items/{entity_id}/attachments/{a['id']}",
                hx_confirm="Remove this document?",
                hx_target="#attachments-panel",
                hx_swap="outerHTML",
            ),
            cls="attachment-card attachment-card--doc",
        )

    drop_js = f"""
(function(){{
  var zone = document.getElementById('attachment-drop-zone');
  var input = document.getElementById('att-input-{entity_id}');
  if (!zone || !input) return;
  function uploadFile(file) {{
    var fd = new FormData();
    fd.append('file', file);
    var statusEl = zone.querySelector('.file-drop-text');
    if (statusEl) statusEl.textContent = 'Uploading...';
    fetch('/api/items/{entity_id}/attachments', {{
      method: 'POST',
      body: fd,
    }}).then(function(resp) {{
      if (!resp.ok) throw new Error('Upload failed');
      return resp.text();
    }}).then(function(html) {{
      var panel = document.getElementById('attachments-panel');
      if (panel) {{ panel.outerHTML = html; htmx.process(document.getElementById('attachments-panel')); }}
    }}).catch(function(err) {{
      alert('Upload failed: ' + err.message);
      if (statusEl) statusEl.textContent = 'Drop files here or click to browse';
    }});
  }}
  zone.addEventListener('click', function(e) {{
    if (e.target.tagName === 'BUTTON' || e.target.closest('button')) return;
    input.click();
  }});
  input.addEventListener('change', function() {{
    if (input.files.length) uploadFile(input.files[0]);
    input.value = '';
  }});
  zone.addEventListener('dragover', function(e) {{ e.preventDefault(); zone.classList.add('file-drop-zone--active'); }});
  zone.addEventListener('dragleave', function() {{ zone.classList.remove('file-drop-zone--active'); }});
  zone.addEventListener('drop', function(e) {{
    e.preventDefault();
    zone.classList.remove('file-drop-zone--active');
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  }});
}})();
"""

    upload_zone = Div(
        Div(
            Div(t("inv.u0001f4c1"), cls="file-drop-icon"),
            Div(t("label.drop_files_here_or_click_to_browse"), cls="file-drop-text"),
            Div(t("inv.images_pdfs_and_documents_up_to_10mb"), cls="file-drop-hint"),
            Input(type="file", name="file", id=f"att-input-{entity_id}",
                  accept="image/*,application/pdf,.doc,.docx,.txt",
                  style="display:none"),
            cls="file-drop-zone", id="attachment-drop-zone",
        ),
    )

    return Div(
        H3(t("page.attachments"), cls="section-title"),
        Div(*[_img_card(a) for a in images], cls="attachment-images") if images else "",
        Div(*[_doc_card(a) for a in docs], cls="attachment-docs") if docs else "",
        upload_zone,
        Script(drop_js),
        cls="detail-card",
        id="attachments-panel",
    )


_UNIVERSAL_FIELD_OPTIONS: dict[str, list[str]] = {
    "weight_unit": ["ct", "g", "kg", "oz", "lb", "t"],
    "inventory_type": ["stocked", "non_stocked", "service"],
}


def _apply_unit_field_override(
    field: str, cell_type: str, options, allow_custom: bool, unit_names: list[str]
) -> tuple[str, list | None, bool]:
    """Return (cell_type, options, allow_custom) with unit-field overrides applied.
    sell_by and purchase_unit become searchable selects populated from company units.
    """
    if field in ("sell_by", "purchase_unit"):
        return (
            "select",
            [*unit_names, ("__new__:/settings/inventory?tab=units", "+ Add new unit")],
            True,
        )
    return cell_type, options, allow_custom


def _resolve_field_def(
    field: str,
    schema: list[dict],
    cat_schemas: dict[str, list[dict]],
    item: dict,
    locations: list[dict] | None = None,
) -> tuple[dict | None, str, list | None, bool]:
    """Return (f_def, cell_type, options, allow_custom) for a field.

    allow_custom=True means the field is a select but also accepts free-text entries.
    Set by "add_new": true in category JSON field definitions.
    """
    # Universal constrained fields take priority
    if field in _UNIVERSAL_FIELD_OPTIONS:
        return None, "select", _UNIVERSAL_FIELD_OPTIONS[field], False
    # Category field: options = all known category names
    if field == "category":
        return {"key": "category", "editable": True}, "select", sorted(cat_schemas.keys()), True
    # Location field: options = all known location names
    if field == "location_name":
        loc_names = [l.get("name", "") for l in (locations or []) if l.get("name")]
        return {"key": "location_name", "editable": True}, "select", loc_names, True
    # Check global schema first
    f_def = next((f for f in schema if f["key"] == field), None)
    # Then check category-specific schema for this item's category
    if f_def is None:
        item_cat = item.get("category", "")
        if item_cat and item_cat in cat_schemas:
            f_def = next((f for f in cat_schemas[item_cat] if f["key"] == field), None)
    if f_def is None:
        return None, "text", None, False
    allow_custom = bool(f_def.get("add_new"))
    return f_def, f_def.get("type", "text"), f_def.get("options") or None, allow_custom




def _print_label_dropdown(entity_id: str) -> FT:
    """Print label icon button with HTMX-loaded template dropdown."""
    dropdown_id = f"print-label-dd-{entity_id.replace(':', '-')}"
    return Div(
        Button("🖨",
            cls="btn btn--secondary btn--icon",
            title=t("btn._print_labels"),
            onclick=f"var dd=document.getElementById('{dropdown_id}');dd.classList.toggle('open');",
        ),
        Div(
            Div(
                hx_get=f"/api/items/{entity_id}/label-templates",
                hx_trigger="load",
                hx_swap="innerHTML",
            ),
            cls="print-label-dropdown",
            id=dropdown_id,
        ),
        cls="print-label-wrapper",
    )


def _item_files_section(entity_id: str, item: dict) -> FT:
    """Render the shared files section for an item, with hero toggle enabled."""
    files = item.get("files") or []
    if not files and item.get("attachments"):
        # Display-side adapter: convert old attachment format for display until
        # the lazy migration fires on the first real file event.
        from celerp_inventory.projections import _ATTACHMENT_TYPE_TO_TAG, _is_image_mime
        existing_preview = item.get("preview_image_id")
        first_image_done = False
        for att in item["attachments"]:
            att_type = att.get("type", "image")
            tag = _ATTACHMENT_TYPE_TO_TAG.get(att_type, "product_images")
            att_id = att.get("id", "")
            is_hero = False
            if tag == "product_images" and _is_image_mime(att.get("mime", "")):
                if existing_preview:
                    is_hero = att_id == existing_preview
                elif not first_image_done:
                    is_hero = True
                    first_image_done = True
            files.append({
                "id": att_id,
                "filename": att.get("filename", ""),
                "mime": att.get("mime", ""),
                "size": att.get("size", 0),
                "url": att.get("url", ""),
                "document_tag": tag,
                "description": att.get("label") or None,
                "uploaded_at": None,
                "is_hero": is_hero,
            })
    return _shared_files_section("item", entity_id, files, can_set_hero=True, show_linked=False)


def _item_detail_tabs(
    entity_id: str,
    item: dict,
    detail_fields: list[dict],
    pricing_fields: list[dict],
    ledger: list[dict],
    currency: str | None,
    active_tab: str,
    price_lists: list[dict] | None = None,
    cell_renderers: dict | None = None,
) -> FT:
    """GemCloud-style tabbed item detail: Details | Pricing | Activity."""
    tabs = [("details", "Details"), ("pricing", "Pricing"), ("activity", "Activity")]
    tab_bar = Div(
        *[
            A(
                label,
                href=f"/inventory/{entity_id}?tab={key}",
                cls=f"category-tab{'  category-tab--active' if key == active_tab else ''}",
            )
            for key, label in tabs
        ],
        cls="category-tabs",
    )
    if active_tab == "pricing":
        if price_lists:
            panel = Div(
                _pricing_form(entity_id, item, price_lists, currency),
                cls="detail-grid detail-grid--single",
            )
        else:
            panel = Div(
                _detail_table(entity_id, item, pricing_fields, title="Pricing", currency=currency),
                cls="detail-grid detail-grid--single",
            )
    elif active_tab == "activity":
        panel = Div(
            _ledger_table(ledger),
            cls="detail-grid detail-grid--single",
        )
    else:
        # Details tab: two-column layout — core fields left, attributes right
        left = [f for f in detail_fields if f.get("key") in _ITEM_CORE_KEYS]
        right = [f for f in detail_fields if f.get("key") not in _ITEM_CORE_KEYS]
        panel = Div(
            _detail_table(entity_id, item, left, title="Core Details", currency=currency, cell_renderers=cell_renderers),
            Div(
                _detail_table(entity_id, item, right, title="Attributes", currency=currency),
                id="item-attributes-section",
            ) if right else Div(id="item-attributes-section"),
            cls="detail-grid",
        )
    return Div(
        tab_bar,
        panel,
        _item_files_section(entity_id, item),
        _advanced_panel(entity_id, item),
    )


def _pricing_form(entity_id: str, item: dict, price_lists: list[dict], currency: str | None) -> FT:
    """Render dynamic pricing form: unit price + linked total (back-calculates unit price from total)."""
    from ui.routes.documents import resolve_price as _resolve_price
    qty = float(item.get("quantity") or 0)
    sell_by = str(item.get("sell_by") or "unit")
    has_qty = qty > 0

    cur_sym = currency or ""
    rows = []
    for pl in price_lists:
        pl_name = pl.get("name", "")
        conventional_key = f"{pl_name.lower()}_price"
        price_val = _resolve_price(item, pl_name)
        unit_val = f"{price_val:.2f}" if price_val else ""
        total_val = f"{price_val * qty:.2f}" if price_val and has_qty else ""
        # JS IDs scoped per price list
        unit_id = f"unit_{conventional_key}"
        total_id = f"total_{conventional_key}"
        cur_prefix = Span(cur_sym, cls="input-prefix") if cur_sym else ""
        rows.append(Tr(
            Td(pl_name, cls="detail-label"),
            Td(
                Div(
                    cur_prefix,
                    Input(
                        type="number", name=conventional_key, id=unit_id,
                        value=unit_val, step="0.01", min="0", placeholder="—",
                        cls="form-input",
                        oninput=f"syncPricingTotal('{unit_id}','{total_id}',{qty})",
                    ),
                    cls="input-with-prefix",
                )
            ),
            Td(
                Div(
                    cur_prefix,
                    Input(
                        type="number", id=total_id,
                        value=total_val, step="0.01", min="0", placeholder="—",
                        cls="form-input",
                        disabled=not has_qty,
                        oninput=f"syncPricingUnit('{unit_id}','{total_id}',{qty})",
                    ),
                    cls="input-with-prefix",
                )
            ),
        ))

    unit_hdr = f"Unit price ({cur_sym} / {sell_by})" if cur_sym else f"Unit price / {sell_by}"
    total_hdr = f"Total ({qty:g} {sell_by})" if has_qty else f"Total (no stock)"

    return Div(
        H3(t("page.pricing"), cls="section-title"),
        Script("""
function syncPricingTotal(unitId, totalId, qty) {
  var u = parseFloat(document.getElementById(unitId).value);
  var tEl = document.getElementById(totalId);
  tEl.value = (isNaN(u) || !qty) ? '' : (u * qty).toFixed(2);
}
function syncPricingUnit(unitId, totalId, qty) {
  if (!qty) return;
  var total = parseFloat(document.getElementById(totalId).value);
  var uEl = document.getElementById(unitId);
  uEl.value = isNaN(total) ? '' : (total / qty).toFixed(2);
}
"""),
        Form(
            Table(
                Thead(Tr(Th(t("th.price_list")), Th(unit_hdr), Th(total_hdr))),
                Tbody(*rows),
                cls="detail-table",
            ),
            Button(t("btn.save_prices"), type="submit", cls="btn btn--primary mt-sm"),
            hx_post=f"/api/items/{entity_id}/price",
            hx_swap="none",
            hx_on__after_request="window.location.reload()",
        ),
        cls="detail-card",
    )


def _detail_table(entity_id: str, item: dict, fields: list[dict], title: str = "Details", currency: str | None = None, cell_renderers: dict | None = None) -> FT:
    if not fields:
        return ""
    from ui.components.table import display_cell
    def _row(f):
        key = f.get("key", "")
        if cell_renderers and key in cell_renderers:
            cell = cell_renderers[key](entity_id, item)
        else:
            cell = display_cell(
                entity_id=entity_id,
                field=key,
                value=item.get(key, ""),
                cell_type=f.get("type", "text"),
                options=f.get("options"),
                editable=f.get("editable", True),
                currency=currency,
            )
        return Tr(
            Td(f.get("label", key), (Span("?", cls="field-tooltip", title=t(f["tooltip_key"])) if f.get("tooltip_key") else ""), cls="detail-label"),
            cell,
        )
    return Div(
        H3(title, cls="section-title"),
        Table(
            Tbody(*[_row(f) for f in fields]),
            cls="detail-table",
        ),
        cls="detail-card",
    )


def _ledger_table(ledger: list[dict]) -> FT:
    from ui.components.activity import activity_table
    return activity_table(ledger, max_display=10)


# ---------------------------------------------------------------------------
# CSV import helpers
# ---------------------------------------------------------------------------

from ui.routes.csv_import import (
    CsvImportSpec,
    ValidateFn,
    _resolve_csv_text,
    _rows_to_csv,
    _stash_csv,
    apply_column_mapping,
    apply_fixes_to_rows as _apply_fixes,
    column_mapping_form,
    error_report_response,
    import_abort_panel,
    import_result_panel,
    read_csv_upload,
    upload_form as _csv_upload_form,
    validate_cell as _csv_validate_cell,
    validate_column_mapping,
    validation_result as _csv_validation_result,
)

def _union_category_attr_keys(cat_schemas: dict) -> list[str]:
    """Extract the deduplicated union of all attribute keys across all category schemas.

    Returns a stable-ordered list (insertion order, no duplicates).
    """
    seen: dict[str, None] = {}
    for fields in cat_schemas.values():
        if not isinstance(fields, list):
            continue
        for field in fields:
            key = field.get("key") or ""
            if key and key not in seen:
                seen[key] = None
    return list(seen)


# Base import columns (without price columns - those are added dynamically)
_IMPORT_BASE_COLS = ["sku", "name", "category", "quantity"]
_IMPORT_TAIL_COLS = ["weight", "weight_unit", "sell_by", "pieces", "barcode", "hs_code",
                     "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
                     "short_description", "description", "notes", "location_name"]

_IMPORT_SPEC = CsvImportSpec(
    cols=_IMPORT_BASE_COLS + ["retail_price", "wholesale_price", "cost_price"] + _IMPORT_TAIL_COLS,
    required={"name", "sell_by"},
    type_map={"quantity": float, "retail_price": float, "wholesale_price": float,
              "cost_price": float, "weight": float, "purchase_conversion_factor": float},
)


def _build_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build import spec with dynamic price columns from company price lists."""
    price_cols = [f"{pl.get('name', '').lower()}_price" for pl in price_lists if pl.get("name")]
    type_map = {"quantity": float, "weight": float, "pieces": float}
    for col in price_cols:
        type_map[col] = float
    return CsvImportSpec(
        cols=_IMPORT_BASE_COLS + price_cols + _IMPORT_TAIL_COLS,
        required={"name", "sell_by"},
        type_map=type_map,
    )


def _import_upload_form(error: str | None = None) -> FT:
    return _csv_upload_form(
        cols=_IMPORT_SPEC.cols,
        template_href="/inventory/import/template",
        preview_action="/inventory/import/preview",
        error=error,
        has_mapping=True,
    )


def _item_validate(col: str, value: str, row: dict | None = None) -> bool:
    return _csv_validate_cell(_IMPORT_SPEC, col, value)


async def _build_item_validator(token: str) -> tuple[ValidateFn, dict]:
    """Build a validator and import fix-table cell renderers for CSV import preview.

    Returns (validate_fn, cell_renderers) where cell_renderers maps column names
    to callables of signature (val: str, row_index: int, row: dict, is_bad: bool) -> FT.

    location_name is optional - blank or missing means "use default location"
    (resolved at confirm time). Validates sell_by against company units if present,
    and requires sell_by when the row's category has no default_sell_by fallback.
    """
    try:
        company_units = await api.get_units(token)
    except Exception:
        company_units = []

    try:
        vert_cats = await api.list_verticals_categories(token)
        cat_sell_by: dict[str, str] = {
            c["name"]: c["default_sell_by"]
            for c in vert_cats
            if c.get("default_sell_by")
        }
    except Exception:
        cat_sell_by = {}

    valid_unit_names: list[str] = [u["name"] for u in company_units]
    valid_unit_set: frozenset[str] = frozenset(valid_unit_names)

    def _validate(col: str, value: str, row: dict | None = None) -> bool:
        if col == "sell_by":
            v = value.strip()
            # sell_by is required unless the row's category provides a default
            if not v:
                category = str((row or {}).get("category", "")).strip()
                return bool(cat_sell_by.get(category))
            # If known units are available, validate membership
            return not valid_unit_set or v in valid_unit_set
        return _item_validate(col, value)

    # Build import fix-table cell renderers for constrained columns.
    # Renderer signature: (val: str, row_index: int, row: dict, is_bad: bool) -> FT
    cell_renderers: dict = {}
    if valid_unit_names:
        def _make_unit_renderer(col: str, _opts: list = valid_unit_names) -> "Callable":
            def _render(val: str, ri: int, row: dict, is_bad: bool) -> FT:
                err_cls = "cell-edit  input--error" if is_bad else "cell-edit"
                return Select(
                    Option("-- select unit --", value="", selected=(not val.strip())),
                    *[Option(u, value=u, selected=(val.strip() == u)) for u in _opts],
                    Option("+ Add new unit", value="__add_new__"),
                    data_col=col,
                    data_row=str(ri),
                    cls=err_cls,
                )
            return _render

        cell_renderers["sell_by"] = _make_unit_renderer("sell_by")
        cell_renderers["purchase_unit"] = _make_unit_renderer("purchase_unit")

    return _validate, cell_renderers


# Core item columns that map to top-level ItemCreate fields (not attributes).
# Price columns (any key ending in _price) are excluded from attributes separately.
_CORE_ITEM_COLS: frozenset[str] = frozenset({
    "sku", "name", "category", "quantity",
    "weight", "weight_ct", "weight_unit", "sell_by", "pieces", "status",
    "barcode", "hs_code", "short_description", "description", "notes", "location_name",
    "location_id", "created_at", "updated_at",
})

# Max distinct values before a column is treated as free-text instead of dropdown
_DROPDOWN_THRESHOLD = 15


def _collect_category_attributes(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Return {category: {col: [distinct_values]}} for all attribute columns."""
    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        cat = str(row.get("category", "") or "").strip() or "_uncategorized"
        if cat not in result:
            result[cat] = {}
        for k, v in row.items():
            if k in _CORE_ITEM_COLS or k.endswith("_price"):
                continue
            v_str = str(v).strip() if v is not None else ""
            if not v_str:
                continue
            if k not in result[cat]:
                result[cat][k] = []
            if v_str not in result[cat][k]:
                result[cat][k].append(v_str)
    return result


def _infer_category_schemas(cat_attr_values: dict[str, dict[str, list[str]]]) -> dict[str, list[dict]]:
    """Convert collected attribute values into schema field definitions."""
    schemas: dict[str, list[dict]] = {}
    for cat, cols in cat_attr_values.items():
        if cat == "_uncategorized":
            continue
        fields = []
        for key, distinct_vals in cols.items():
            if len(distinct_vals) <= _DROPDOWN_THRESHOLD:
                ftype = "dropdown"
                options = sorted(distinct_vals)
            else:
                ftype = "text"
                options = []
            fields.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": ftype,
                "options": options,
            })
        if fields:
            schemas[cat] = fields
    return schemas


def _effective_schema(
    global_schema: list[dict],
    cat_schemas: dict[str, list[dict]],
    active_cat: str,
) -> list[dict]:
    """Merge global schema with category-specific fields.

    For "All" view (active_cat=""): include union of all category fields,
    appended after global fields.
    For a specific category: include only that category's fields appended.
    Hidden-by-default attribute columns (show_in_table=False at category level)
    are still included in the schema so they appear in the column manager.
    """
    global_keys = {f["key"] for f in global_schema}

    if active_cat:
        extra = [f for f in (cat_schemas.get(active_cat) or []) if f["key"] not in global_keys]
    else:
        # Union of all category schemas, deduped by key
        seen: set[str] = set(global_keys)
        extra = []
        for fields in cat_schemas.values():
            for f in fields:
                if f["key"] not in seen:
                    extra.append(f)
                    seen.add(f["key"])

    # Category fields default show_in_table=True for their own category,
    # but False for "All" view (too noisy across mixed categories)
    if not active_cat:
        extra = [{**f, "show_in_table": False} for f in extra]

    return global_schema + extra


def _resolve_visible_cols(
    eff_schema: list[dict],
    col_prefs: dict,
    active_cat: str,
    url_cols: list[str],
) -> list[str]:
    """Determine visible column list for the current view.

    Priority: URL ?cols= override > saved pref for this view > schema defaults.
    """
    if url_cols:
        return url_cols
    pref_key = active_cat if active_cat else "__all__"
    saved = col_prefs.get(pref_key)
    if saved:
        return saved
    # Default: fields where show_in_table is True
    return [f["key"] for f in eff_schema if f.get("show_in_table", True)]


# ---------------------------------------------------------------------------
# T3: Advanced operations panel (non-inline-editable actions only)
# ---------------------------------------------------------------------------

def _advanced_panel(entity_id: str, item: dict) -> FT:
    """Compact item operations grid: Split, Duplicate, Expire, Dispose."""
    current_qty = float(item.get("quantity", 0) or 0)

    from celerp.modules.slots import get as get_slot
    module_item_actions = []
    for action in get_slot("item_action"):
        href = action.get("href_template", "").replace("{entity_id}", entity_id)
        module_item_actions.append(
            A(action.get("label", "Action"), href=href, cls="btn btn--secondary btn--sm")
        )

    # 2x2 compact action cards
    allow_splitting = item.get("allow_splitting", True)
    sell_by = item.get("sell_by") or "piece"
    if allow_splitting:
        sell_by_label = sell_by.capitalize()
        split_card = Div(
            Form(
                Strong(t("inv.u2702_split"), cls="action-card-title"),
                Div(
                    # Dynamic qty rows - JS adds more via addSplitRow()
                    Div(
                        Div(
                            Button("+", type="button", cls="btn btn--secondary btn--xs split-add-btn",
                                   onclick="addSplitRow(this)"),
                            Input(type="number", name="split_qty", placeholder=f"{sell_by_label} to split off",
                                  step="any", min="0.001", cls="form-input form-input--sm", required=True),
                            cls="split-qty-row",
                        ),
                        id="split-qty-rows",
                    ),
                    Button(t("btn.go"), type="submit", cls="btn btn--primary btn--xs"),
                    cls="action-card-row action-card-row--col",
                ),
                Script(f"""
function addSplitRow(btn) {{
  var container = document.getElementById('split-qty-rows');
  var row = document.createElement('div');
  row.className = 'split-qty-row';
  row.innerHTML = '<button type="button" class="btn btn--secondary btn--xs split-add-btn" onclick="addSplitRow(this)">+</button>'
    + '<input type="number" name="split_qty" placeholder="{sell_by_label} to split off" step="any" min="0.001" class="form-input form-input--sm" required>'
    + '<button type="button" class="btn btn--ghost btn--xs split-remove-btn" onclick="this.parentNode.remove()">✕</button>';
  container.appendChild(row);
  row.querySelector('input').focus();
}}
"""),
                hx_post=f"/api/items/{entity_id}/split-inline",
                hx_target="#item-action-error",
                hx_swap="outerHTML",
            ),
            cls="action-card",
        )
    else:
        split_card = Div(
            Strong(t("inv.u2702_split"), cls="action-card-title"),
            P(t("inv.splitting_disabled_hint"), cls="action-card-hint"),
            cls="action-card action-card--disabled",
        )

    duplicate_card = Div(
        Form(
            Strong(t("inv.u0001f4cb_duplicate"), cls="action-card-title"),
            Div(
                Input(type="text", name="new_sku", placeholder="New SKU (optional)", cls="form-input form-input--sm"),
                Button(t("btn.go"), type="submit", cls="btn btn--primary btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/duplicate",
            hx_target="#item-action-error",
            hx_swap="outerHTML",
        ),
        cls="action-card",
    )

    expire_card = Div(
        Form(
            Strong(t("inv.u23f3_expire"), cls="action-card-title"),
            Div(
                Input(type="text", name="reason", placeholder="Reason (optional)", cls="form-input form-input--sm"),
                Button(t("btn.go"), type="submit", cls="btn btn--danger btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/expire",
            hx_target="#item-action-error",
            hx_swap="outerHTML",
        ),
        cls="action-card",
    )

    archive_card = Div(
        Form(
            Strong(t("inv.u1f4e6_archive"), cls="action-card-title"),
            Div(
                Input(type="text", name="reason", placeholder="Reason (optional)", cls="form-input form-input--sm"),
                Button(t("btn.go"), type="submit", cls="btn btn--danger btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/archive",
            hx_target="#item-action-error",
            hx_swap="outerHTML",
        ),
        cls="action-card",
    )

    restore_card = Div(
        Form(
            Strong(t("inv.u21a9_restore"), cls="action-card-title"),
            Div(
                Input(type="text", name="reason", placeholder="Reason (optional)", cls="form-input form-input--sm"),
                Button(t("btn.go"), type="submit", cls="btn btn--primary btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/restore",
            hx_target="#item-action-error",
            hx_swap="outerHTML",
        ),
        cls="action-card",
    )

    # Items already in a terminal/hidden state: show restore instead of expire/archive
    item_status = item.get("status", "available")
    _RESTORABLE = {"archived", "expired"}
    if item_status in _RESTORABLE:
        lifecycle_cards = [restore_card]
    else:
        lifecycle_cards = [expire_card, archive_card]

    # Return to Vendor (conditional)
    rtv_card = ""
    if item.get("consignment_flag") == "in" or item.get("source_doc"):
        rtv_card = Div(
            Form(
                Strong(t("inv.u21a9_return_to_vendor"), cls="action-card-title"),
                Div(
                    Input(type="number", name="quantity", value=str(current_qty),
                          step="any", min="0", max=str(current_qty), cls="form-input form-input--sm"),
                    Button(t("btn.go"), type="submit", cls="btn btn--danger btn--xs"),
                    cls="action-card-row",
                ),
                hx_post=f"/api/items/{entity_id}/return-to-vendor",
                hx_target="#item-action-error",
                hx_swap="outerHTML",
            ),
            cls="action-card",
        )

    # Bring Back In (conditional)
    bbi_card = ""
    if item.get("consignment_flag") == "out" or item.get("status") == "consigned_out":
        bbi_card = Div(
            Form(
                Strong(t("inv.u21a9_bring_back_in"), cls="action-card-title"),
                Div(
                    Input(type="number", name="quantity", value=str(current_qty),
                          step="any", min="0", max=str(current_qty), cls="form-input form-input--sm"),
                    Button(t("btn.go"), type="submit", cls="btn btn--primary btn--xs"),
                    cls="action-card-row",
                ),
                hx_post=f"/api/items/{entity_id}/bring-back-in",
                hx_target="#item-action-error",
                hx_swap="outerHTML",
            ),
            cls="action-card",
        )

    return Div(
        H3(t("th.actions"), cls="section-title"),
        Span("", id="item-action-error"),
        Div(
            split_card,
            duplicate_card,
            *lifecycle_cards,
            rtv_card,
            bbi_card,
            cls="action-cards-grid",
        ),
        P(t("inv.to_merge_items_select_multiple_from_the_inventory"), cls="form-hint"),
        *([Div(*module_item_actions, cls="actions-group", style="margin-top:0.5rem")] if module_item_actions else []),
        cls="detail-card",
    )


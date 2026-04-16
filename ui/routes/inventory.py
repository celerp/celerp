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
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import data_table, search_bar, pagination, EMPTY, breadcrumbs, status_cards, empty_state_cta, add_new_option
from ui.config import get_token as _token
from ui.i18n import t, get_lang

_DEFAULT_PER_PAGE = 50




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
    cols = q.getlist("cols") or [c for c in q.get("cols", "").split(",") if c]
    return {
        "q": q.get("q", ""),
        "page": max(1, page),
        "status": q.get("status", ""),
        "category": q.get("category", ""),
        "sort": q.get("sort", ""),
        "dir": q.get("dir", "desc"),
        "per_page": max(1, per_page),
        "cols": cols,
    }


def _base_state(p: dict, include_page: bool = False) -> dict:
    state = {}
    for k in ("q", "status", "category", "sort", "dir"):
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
        if p["status"]:
            params["status"] = p["status"]
        if p["category"]:
            params["category"] = p["category"]
        if p["sort"]:
            params["sort"] = p["sort"]
            params["dir"] = p["dir"]
        items = (await api.list_items(token, params)).get("items", [])
    except APIError:
        valuation, items = {}, []

    currency = company.get("currency")
    vertical = company.get("settings", {}).get("vertical", "") if isinstance(company.get("settings"), dict) else ""

    category_counts = valuation.get("category_counts", {})
    count_by_status = valuation.get("count_by_status", {})
    active_cat = p.get("category", "")
    eff_schema = _effective_schema(schema, cat_schemas, active_cat)
    visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, p.get("cols") or [])
    extra_params = urlencode(_base_state(p))

    return Div(
        _category_tabs(category_counts, p),
        _valuation_bar(valuation, currency, lang),
        _inventory_status_cards(count_by_status, p.get("status", ""), vertical, p),
        _bulk_toolbar(locations),
        Div(_column_manager(eff_schema, p, active_cat, visible_cols, keep_open=col_manager_open), cls="column-manager-row"),
        data_table(
            eff_schema,
            items,
            entity_type="inventory",
            show_cols=visible_cols or None,
            sort_key=p["sort"],
            sort_dir=p["dir"],
            sort_url="/inventory/content",
            extra_params=_base_state(p),
            currency=currency,
            sort_target="#inventory-content",
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
            title="Inventory - Celerp",
            nav_active="inventory",
            lang=lang,
            request=request,
        )

    @app.get("/inventory/content")
    async def inventory_content(request: Request):
        """HTMX partial: returns #inventory-content fragment (tabs + cards + valuation + table).

        Used by category tabs, status tabs, search, sort, and pagination so all state stays consistent.
        """
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
        try:
            data = await api.export_items_csv(token, params)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            data = b"error\n" + e.detail.encode()
        from starlette.responses import Response
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
        from starlette.responses import Response
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
        validate = await _build_item_validator(token)

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
        validate = await _build_item_validator(token)
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
        validate = await _build_item_validator(token)
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

        # Build category → default_sell_by map for sell_by fallback
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

        for row in rows:
            sku = str(row.get("sku", "")).strip()
            name = str(row.get("name", "")).strip()
            loc_name = str(row.get("location_name", "")).strip()

            # Resolve location_id: explicit name → map; blank → default
            if loc_name:
                location_id = location_map.get(loc_name)
            else:
                location_id = default_location_id

            if not sku or not name:
                # Skip rows missing required fields (shouldn't reach confirm unless CSV tampered)
                continue

            if not location_id:
                return Div(
                    P(
                        "Import aborted: could not determine a location for one or more rows. "
                        "Your CSV has no location_name column and there are multiple locations configured. "
                        "Please add a location_name column or set a default location in Settings → Inventory.",
                        cls="flash flash--error",
                    ),
                    id="import-preview",
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
                "status": str(row.get("status", "")).strip() or None,
                "barcode": str(row.get("barcode", "")).strip() or None,
                "hs_code": str(row.get("hs_code", "")).strip() or None,
                "short_description": str(row.get("short_description", "")).strip() or None,
                "description": str(row.get("description", "")).strip() or None,
                "notes": str(row.get("notes", "")).strip() or None,
                "created_at": str(row.get("created_at", "")).strip() or None,
                "updated_at": str(row.get("updated_at", "")).strip() or None,
                "location_id": location_id,
                "attributes": attrs,
            }
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
            result = await api.batch_import(token, "/items/import/batch", records, upsert=upsert)
        except APIError as e:
            if e.status == 401:
                return Div(
                    P(t("error.session_expired"), cls="flash flash--error"),
                    A(t("inv.go_to_login"), href="/login", cls="btn btn--primary"),
                    id="import-preview",
                )
            return Div(
                P(f"Import failed: {e.detail}", cls="flash flash--error"),
                A(t("btn._try_again"), href="/inventory/import", cls="btn btn--secondary"),
                id="import-preview",
            )
        except Exception as e:
            return Div(
                P(f"Unexpected error: {e}", cls="flash flash--error"),
                A(t("btn._try_again"), href="/inventory/import", cls="btn btn--secondary"),
                id="import-preview",
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
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        import uuid as _uuid
        sku = f"ITEM-{_uuid.uuid4().hex[:8].upper()}"
        try:
            result = await api.create_item(token, {"sku": sku, "name": "New Item", "quantity": 0, "sell_by": "piece"})
            item_id = result.get("id", result.get("entity_id", ""))
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            logger.warning("Blank-create item failed: %s", e.detail)
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{item_id}"})

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
            schema, item, company, cat_schemas, price_lists = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_company(token),
                api.get_all_category_schemas(token),
                api.get_price_lists(token),
            )
            ledger = (await api.list_ledger(token, {"entity_id": entity_id, "limit": 10})).get("items", [])
            locations = (await api.get_locations(token)).get("items", [])
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            schema, item, ledger, locations, company, cat_schemas, price_lists = [], {}, [], [], {}, {}, []

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
        detail_fields = [f for f in schema if f.get("key") not in pricing_keys]
        pricing_fields = [f for f in schema if f.get("key") in pricing_keys]

        active_tab = request.query_params.get("tab", "details")

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
            _item_detail_tabs(entity_id, item, detail_fields, pricing_fields, ledger, currency, active_tab, price_lists=price_lists),
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
                    f"http://127.0.0.1:{request.url.port or 8080}/api/labels/templates",
                    headers={"Authorization": f"Bearer {token}"},
                )
                templates = r.json() if r.status_code == 200 else []
        except Exception:
            templates = []
        if not templates:
            return Div(
                P(t("inv.no_label_templates"), cls="dropdown-empty"),
                A(t("inv.create_template"), href="/settings/labels", cls="dropdown-link"),
            )
        print_js = """
function celerpPrintLabel(entityId, templateId) {
    var token = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('celerp_token='));
    token = token ? token.split('=')[1] : '';
    fetch('/api/labels/print/' + entityId + '?template_id=' + templateId, {
        method: 'POST',
        headers: {'Authorization': 'Bearer ' + token}
    })
    .then(r => r.blob())
    .then(blob => {
        var url = URL.createObjectURL(blob);
        var w = window.open(url);
        if (w) w.addEventListener('load', function() { w.print(); });
    });
}
"""
        items = [
            A(
                t.get("name", "Template"),
                href="#",
                onclick=f"celerpPrintLabel('{entity_id}','{t['id']}');this.closest('.print-label-dropdown').classList.remove('open');return false;",
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
        return editable_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                             cell_type=cell_type, options=options, allow_custom=allow_custom)

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
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True)

    @app.patch("/api/items/{entity_id}/field/{field}")
    async def field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value: str | bool = str(form.get("value", ""))

        # Convert bool fields from string to proper bool
        if field == "allow_splitting":
            value = value.lower() in ("true", "1", "yes")

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
        from ui.components.table import display_cell
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True)

    # ── Bulk actions (list-level) ─────────────────────────────────────────────

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
        return Div(
            P(f"{updated} item(s) updated to '{status}'.", cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get="/inventory/content",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
        )

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
        return Div(
            P(f"{updated} item(s) transferred.", cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get="/inventory/content",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
        )

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
        return Div(
            P(f"{deleted} item(s) deleted.", cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get="/inventory/content",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
        )

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
        return Div(
            P(f"{expired} item(s) expired.", cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get="/inventory/content",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
        )

    # ── Bulk merge (direct — no preview modal) ───────────────────────────

    @app.post("/api/items/bulk/merge")
    async def bulk_item_merge(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return Div(
            P(t("inv.items_merged_successfully"), cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get=f"/inventory/content{redirect_qs}",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=f"/inventory{redirect_qs}",
        )

    # ── Bulk split (simplified single-qty) ───────────────────────────────

    async def _next_split_sku(token: str, parent_sku: str) -> str:
        """Find next available child SKU suffix for splitting.

        DEMO-RGH-001 -> DEMO-RGH-001.1, DEMO-RGH-001.2, ...
        DEMO-RGH-001.1 -> DEMO-RGH-001.1.1, DEMO-RGH-001.1.2, ...
        """
        prefix = f"{parent_sku}."
        try:
            resp = await api.list_items(token, {"q": parent_sku, "limit": 200, "status": "all"})
            items = resp.get("items", []) if isinstance(resp, dict) else resp
        except Exception:
            items = []
        max_suffix = 0
        for it in items:
            sku = str(it.get("sku", ""))
            if sku.startswith(prefix):
                suffix_part = sku[len(prefix):]
                # Only count direct children (no dots in suffix)
                if "." not in suffix_part:
                    try:
                        max_suffix = max(max_suffix, int(suffix_part))
                    except ValueError:
                        pass
        return f"{prefix}{max_suffix + 1}"

    @app.post("/api/items/bulk/split")
    async def bulk_item_split(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if len(entity_ids) != 1:
            return Div(P(t("inv.select_exactly_1_item_to_split"), cls="flash flash--warning"), id="bulk-action-result")
        eid = entity_ids[0]
        split_qty_raw = str(form.get("split_qty", "")).strip()
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
        new_sku = await _next_split_sku(token, orig_sku)
        try:
            await api.split_item(token, eid, [
                {"sku": new_sku, "quantity": split_qty},
            ])
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        from urllib.parse import quote
        remaining_qty = current_qty - split_qty
        return Div(
            P(f"Split: {orig_sku} ({remaining_qty}) + {new_sku} ({split_qty}).", cls="flash flash--success"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get=f"/inventory/content?q={quote(orig_sku)}",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
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
                params = {"limit": 20}
                resp = await api.list_memos(token, params)
                items = resp.get("items", [])
                if q:
                    ql = q.lower()
                    items = [m for m in items if ql in str(m.get("memo_number", "")).lower()
                             or ql in str(m.get("contact_name", "")).lower()][:20]
                return JSONResponse([
                    {"id": d.get("id") or d.get("entity_id", ""),
                     "label": f"Memo {d.get('memo_number') or (d.get('id', '') or '')[:8]}",
                     "status": d.get("status", "")}
                    for d in items
                ])
        except APIError:
            pass
        return JSONResponse([])

    # ── Send-to action ────────────────────────────────────────────────────

    @app.post("/api/items/send-to")
    async def send_to_action(request: Request):
        from starlette.responses import Response as _R
        from ui.routes.documents import _line_items_from_inventory
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
                    return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{target_id}"})
                elif doc_type == "list":
                    new_lines = await _line_items_from_inventory(token, entity_ids)
                    lst = await api.get_list(token, target_id)
                    combined = (lst.get("line_items") or []) + new_lines
                    subtotal = sum(l.get("quantity", 0) * l.get("unit_price", 0) for l in combined)
                    await api.patch_list(token, target_id, {"line_items": combined, "subtotal": subtotal, "total": subtotal})
                    return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{target_id}"})
                elif doc_type == "memo":
                    for eid in entity_ids:
                        await api.add_memo_item(token, target_id, {"item_id": eid})
                    return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{target_id}"})
            else:
                # Create new document
                if doc_type == "invoice":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    from ui.routes.documents import _company_doc_taxes
                    doc_taxes = await _company_doc_taxes(token)
                    result = await api.create_doc(token, {"doc_type": "invoice", "status": "draft", "line_items": line_items, "doc_taxes": doc_taxes})
                    doc_id = result.get("entity_id") or result.get("id", "")
                    return _R("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})
                elif doc_type == "list":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    result = await api.create_list(token, {"list_type": "quotation", "status": "draft", "line_items": line_items})
                    list_id = result.get("entity_id") or result.get("id", "")
                    return _R("", status_code=204, headers={"HX-Redirect": f"/lists/{list_id}"})
                elif doc_type == "memo":
                    result = await api.create_memo(token)
                    memo_id = result.get("id", "")
                    for eid in entity_ids:
                        await api.add_memo_item(token, memo_id, {"item_id": eid})
                    return _R("", status_code=204, headers={"HX-Redirect": f"/crm/memos/{memo_id}"})
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        return Div(P(t("inv.unknown_document_type"), cls="flash flash--warning"), id="bulk-action-result")

    # ── T3: Item action routes ───────────────────────────────────────────

    @app.post("/api/items/{entity_id}/adjust")
    async def item_adjust(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            new_qty = float(str(form.get("new_qty", "0")))
        except ValueError:
            new_qty = 0.0
        try:
            await api.adjust_item(token, entity_id, new_qty)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/transfer")
    async def item_transfer(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        location_id = str(form.get("location_id", "")).strip()
        try:
            await api.transfer_item(token, entity_id, location_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/reserve")
    async def item_reserve(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/unreserve")
    async def item_unreserve(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        try:
            qty = float(str(form.get("quantity", "0")))
        except ValueError:
            qty = 0.0
        try:
            await api.unreserve_item(token, entity_id, qty)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/price")
    async def item_price(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/status")
    async def item_status(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        status = str(form.get("status", "")).strip()
        try:
            await api.set_item_status(token, entity_id, status)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/expire")
    async def item_expire(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.expire_item(token, entity_id, reason)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/dispose")
    async def item_dispose(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        notes = str(form.get("notes", "")).strip() or None
        try:
            await api.dispose_item(token, entity_id, reason, notes)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/return-to-vendor")
    async def item_return_to_vendor(request: Request, entity_id: str):
        """Return an item received from a bill/PO back to the vendor."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/bring-back-in")
    async def item_bring_back_in(request: Request, entity_id: str):
        """Return a consigned-out item back into inventory."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{entity_id}"})

    @app.post("/api/items/{entity_id}/split")
    async def item_split(request: Request, entity_id: str):
        import json as _json
        from starlette.responses import Response as _R
        from urllib.parse import quote
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})

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
                # Legacy format: child_sku_N / child_qty_N pairs (children only, not parent)
                children = []
                idx = 0
                while True:
                    sku = str(form.get(f"child_sku_{idx}", "")).strip()
                    qty_raw = str(form.get(f"child_qty_{idx}", "")).strip()
                    if not sku and not qty_raw:
                        break
                    if sku and qty_raw:
                        try:
                            children.append({"sku": sku, "quantity": float(qty_raw)})
                        except ValueError:
                            return Div(Span(f"Invalid quantity for child {idx+1}", cls="flash flash--error"), id="item-action-error")
                    idx += 1
                if not children:
                    return Div(Span(t("inv.enter_commaseparated_quantities_eg_321"), cls="flash flash--error"), id="item-action-error")
        if len(children) < 1:
            return Div(Span(t("inv.enter_at_least_one_split_quantity"), cls="flash flash--error"), id="item-action-error")
        try:
            await api.split_item(token, entity_id, children)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        redirect = f"/inventory?q={quote(orig_sku)}" if orig_sku else f"/inventory/{entity_id}"
        return _R("", status_code=204, headers={"HX-Redirect": redirect})

    @app.post("/api/items/merge")
    async def item_merge(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{new_id}"})

    @app.post("/api/items/{entity_id}/duplicate")
    async def item_duplicate(request: Request, entity_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        new_sku = str(form.get("new_sku", "")).strip()
        if not new_sku:
            return Div(Span(t("error.new_sku_required"), cls="flash flash--error"), id="item-action-error")
        try:
            source = await api.get_item(token, entity_id)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")

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
        return _R("", status_code=204, headers={"HX-Redirect": f"/inventory/{new_id}"})

    # ── Attachment upload (single file) ──────────────────────────────────────

    @app.post("/inventory/{entity_id}/attachments")
    async def item_upload_attachment(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        file = form.get("file")
        if file is None:
            return P(t("msg.no_file_provided"), cls="cell-error")
        try:
            await api.upload_attachment(token, entity_id, file)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _attachments_panel(entity_id, item)

    @app.delete("/inventory/{entity_id}/attachments/{att_id}")
    async def item_delete_attachment(request: Request, entity_id: str, att_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401)
        try:
            await api.delete_attachment(token, entity_id, att_id)
        except APIError as e:
            logger.warning("API error on delete attachment %s/%s: %s", entity_id, att_id, e.detail)
        return _R("", status_code=204, headers={"HX-Refresh": "true"})


def _bulk_toolbar(locations: list[dict]) -> FT:
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

    # Module immediate actions (e.g. Print Labels)
    module_action_opts = []
    for action in get_slot("bulk_action"):
        module_action_opts.append(
            Option(action.get("label", "Action"), value=f"module:{action['form_action']}")
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
    action_options.extend([
        Option(t("inv.archive"), value="archive"),
        Option(t("inv.expire"), value="expire"),
        Option(t("btn.delete"), value="delete"),
    ])

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
        _bulk_context_templates(loc_opts, _loc_opt, _loc_js, send_to_opts, get_slot("bulk_action")),
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

    # Split: qty input + split button
    split_tpl = Template(
        Form(
            Input(type="number", name="split_qty", placeholder="Quantity to split off",
                  step="any", min="0.001", cls="form-input form-input--sm", required=True),
            Button(t("inv.split"), type="submit", cls="btn btn--primary btn--sm"),
            hx_post="/api/items/bulk/split",
            hx_target="#bulk-action-result",
            hx_swap="outerHTML",
            onsubmit="submitBulkAction(this)",
            cls="display-contents",
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

    # Module action templates (immediate, e.g. Print Labels)
    module_tpls = []
    for action in module_actions:
        action_id = action["form_action"].replace("/", "_").strip("_")
        module_tpls.append(Template(
            Form(
                Button(action.get("label", "Go"), type="submit", cls="btn btn--primary btn--sm"),
                hx_post=action["form_action"],
                hx_target="#bulk-action-result",
                hx_swap="outerHTML",
                onsubmit="submitBulkAction(this)",
                cls="display-contents",
            ),
            id=f"tpl-module-{action_id}",
        ))

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
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
    ],
    "agricultural": [
        ("", "Available"), ("reserved", "Reserved"),
        ("sold", "Sold"), ("archived", "Archived"), ("all", "All"),
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
    ],
    "agricultural": [
        ("available", "Available", "green"),
        ("reserved", "Reserved", "blue"),
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


def _inventory_status_cards(count_by_status: dict, active_status: str, vertical: str = "", p: dict | None = None) -> FT:
    """Status cards driven by backend count_by_status dict (scoped to active category/status filter).

    Passes current non-status params (e.g. category, q) as base_url so clicking a card
    preserves the active category filter instead of resetting to All.
    """
    _CARD_DEFS = _vertical_status_card_defs(vertical)
    cards = [
        {"label": label, "count": count_by_status.get(key, 0), "status": key, "color": color}
        for key, label, color in _CARD_DEFS
    ]
    base_state = {k: v for k, v in _base_state(p or {}).items() if k != "status"}
    base_url = "/inventory" + (f"?{urlencode(base_state)}" if base_state else "")
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


def _category_tabs(category_counts: dict, p: dict) -> FT:
    if not category_counts:
        return ""

    def _url(category: str = "") -> str:
        state = _base_state(p)
        if category:
            state["category"] = category
        else:
            state.pop("category", None)
        return "/inventory" + (f"?{urlencode(state)}" if state else "")

    total = sum(category_counts.values())
    tabs = [
        A(
            f"All ({total})",
            href=_url(""),
            hx_get="/inventory/content" + (f"?{urlencode({k: v for k, v in _base_state(p).items() if k != 'category'})}" if _base_state(p) else ""),
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=_url(""),
            cls=f"category-tab {'category-tab--active' if not p.get('category') else ''}",
        ),
    ]
    for cat, count in sorted(category_counts.items()):
        state = _base_state(p)
        state["category"] = cat
        tabs.append(A(
            f"{cat} ({count})",
            href=_url(cat),
            hx_get=f"/inventory/content?{urlencode(state)}",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            hx_push_url=_url(cat),
            cls=f"category-tab {'category-tab--active' if p.get('category') == cat else ''}",
        ))
    return Div(*tabs, cls="category-tabs", id="category-tabs")


def _valuation_bar(valuation: dict, currency: str | None = None, lang: str = "en") -> FT:
    from ui.components.table import fmt_money
    active_count = valuation.get('active_item_count', valuation.get('item_count', 0))
    chips = [Span(f"{t('chip.available', lang)}: {active_count:,}", cls="val-chip")]
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

    def _url(s: str) -> str:
        state = _base_state(p)
        if s:
            state["status"] = s
        else:
            state.pop("status", None)
        return "/inventory" + (f"?{urlencode(state)}" if state else "")

    def _htmx_params(s: str) -> dict:
        state = _base_state(p)
        if s:
            state["status"] = s
        else:
            state.pop("status", None)
        return {
            "hx_get": "/inventory/content" + (f"?{urlencode(state)}" if state else ""),
            "hx_target": "#inventory-content",
            "hx_swap": "outerHTML",
            "hx_push_url": _url(s),
        }

    tabs = [
        A(
            label,
            href=_url(s),
            cls=f"category-tab {'category-tab--active' if active_status == s else ''}",
            **_htmx_params(s),
        )
        for s, label in _TABS
    ]
    return Div(*tabs, cls="category-tabs status-tabs", id="status-tabs")


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
    col_mgr_js = f"""
(function() {{
  var LS_VIS_KEY = 'celerp_cols_inventory';
  var LS_ORDER_KEY = 'celerp_col_order_inventory';
  var CAT_PREF = '{cat_pref}';
  var ALL_COLS = {col_data_js};
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
        var td = tr.cells[colIdx];
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

  // Checkbox change: immediate column toggle
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
      // Swap in DOM
      var parent = lbl.parentNode;
      var srcNext = dragSrc.nextSibling;
      parent.insertBefore(dragSrc, lbl);
      if (srcNext) parent.insertBefore(lbl, srcNext); else parent.appendChild(lbl);
      dragSrc.style.opacity = '';
      // Persist new order
      var newOrder = Array.from(menu.querySelectorAll('label[data-col]')).map(function(l){{return l.dataset.col;}});
      saveOrder(newOrder);
      applyOrderToTable(newOrder);
    }});
  }});

  // Init: apply localStorage state on page load
  var storedVis = loadVis();
  if (storedVis) applyVisToTable(storedVis);
  var storedOrder = loadOrder();
  if (storedOrder) applyOrderToTable(storedOrder);

  // Keep menu closed unless keep_open is set
  {'menu.style.display = "";' if keep_open else 'menu.style.display = "none";'}
}})();
"""

    return Div(
        Button(t("btn.manage_columns"), id="col-mgr-btn", cls="btn btn--secondary", type="button"),
        Div(
            *checkboxes,
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
                    hx_delete=f"/inventory/{entity_id}/attachments/{a['id']}",
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
                hx_delete=f"/inventory/{entity_id}/attachments/{a['id']}",
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
    fetch('/inventory/{entity_id}/attachments', {{
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
}


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
        Button(t("btn.u0001f5a8"),  # printer icon
            cls="btn btn--secondary btn--icon",
            title="Print label",
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


def _item_detail_tabs(
    entity_id: str,
    item: dict,
    detail_fields: list[dict],
    pricing_fields: list[dict],
    ledger: list[dict],
    currency: str | None,
    active_tab: str,
    price_lists: list[dict] | None = None,
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
        core_keys = {"sku", "name", "status", "category", "quantity", "weight", "weight_unit", "sell_by", "allow_splitting", "barcode", "hs_code", "location_name", "short_description", "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor"}
        left = [f for f in detail_fields if f.get("key") in core_keys]
        right = [f for f in detail_fields if f.get("key") not in core_keys]
        panel = Div(
            _detail_table(entity_id, item, left, title="Core Details", currency=currency),
            _detail_table(entity_id, item, right, title="Attributes", currency=currency) if right else "",
            cls="detail-grid",
        )
    return Div(
        tab_bar,
        panel,
        _attachments_panel(entity_id, item),
        _advanced_panel(entity_id, item),
    )


def _pricing_form(entity_id: str, item: dict, price_lists: list[dict], currency: str | None) -> FT:
    """Render dynamic pricing form with one input per price list."""
    from ui.routes.documents import resolve_price as _resolve_price
    rows = []
    for pl in price_lists:
        pl_name = pl.get("name", "")
        # Conventional key (e.g. "retail_price" for "Retail")
        conventional_key = f"{pl_name.lower()}_price"
        # Use the conventional key as input name so it matches item state
        price_val = _resolve_price(item, pl_name)
        rows.append(Tr(
            Td(pl_name, cls="detail-label"),
            Td(
                Input(
                    type="number",
                    name=conventional_key,
                    value=str(price_val) if price_val else "",
                    step="0.01",
                    min="0",
                    placeholder="—",
                    cls="form-input",
                )
            ),
        ))
    return Div(
        H3(t("page.pricing"), cls="section-title"),
        Form(
            Table(
                Thead(Tr(Th(t("th.price_list")), Th(t("th.price")))),
                Tbody(*rows),
                cls="detail-table",
            ),
            Button(t("btn.save_prices"), type="submit", cls="btn btn--primary mt-sm"),
            hx_post=f"/api/items/{entity_id}/price",
            hx_swap="none",
            hx_on__after_request=f"window.location.reload()",
        ),
        cls="detail-card",
    )


def _detail_table(entity_id: str, item: dict, fields: list[dict], title: str = "Details", currency: str | None = None) -> FT:
    if not fields:
        return ""
    from ui.components.table import display_cell
    return Div(
        H3(title, cls="section-title"),
        Table(
            Tbody(*[
                Tr(
                    Td(f.get("label", f.get("key")), cls="detail-label"),
                    display_cell(
                        entity_id=entity_id,
                        field=f.get("key", ""),
                        value=item.get(f.get("key", ""), ""),
                        cell_type=f.get("type", "text"),
                        options=f.get("options"),
                        editable=f.get("editable", True),
                        currency=currency,
                    ),
                )
                for f in fields
            ]),
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
_IMPORT_TAIL_COLS = ["weight", "weight_unit", "sell_by", "status", "barcode", "hs_code",
                     "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
                     "short_description", "description", "notes", "location_name",
                     "created_at", "updated_at"]

_IMPORT_SPEC = CsvImportSpec(
    cols=_IMPORT_BASE_COLS + ["retail_price", "wholesale_price", "cost_price"] + _IMPORT_TAIL_COLS,
    required={"sku", "name", "location_name"},
    type_map={"quantity": float, "retail_price": float, "wholesale_price": float,
              "cost_price": float, "weight": float, "purchase_conversion_factor": float},
)


def _build_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build import spec with dynamic price columns from company price lists."""
    price_cols = [f"{pl.get('name', '').lower()}_price" for pl in price_lists if pl.get("name")]
    type_map = {"quantity": float, "weight": float}
    for col in price_cols:
        type_map[col] = float
    return CsvImportSpec(
        cols=_IMPORT_BASE_COLS + price_cols + _IMPORT_TAIL_COLS,
        required={"sku", "name", "location_name"},
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


def _item_validate(col: str, value: str) -> bool:
    return _csv_validate_cell(_IMPORT_SPEC, col, value)


async def _build_item_validator(token: str) -> ValidateFn:
    """Build a validator for CSV import preview.

    location_name is optional - blank or missing means "use default location"
    (resolved at confirm time). Validates sell_by against company units if present.
    """
    # Fetch company units for sell_by validation; fall back to empty (no validation)
    try:
        company_units = await api.get_units(token)
    except Exception:
        company_units = []

    valid_unit_names: frozenset[str] = frozenset(u["name"] for u in company_units)

    def _validate(col: str, value: str) -> bool:
        if col == "sell_by" and value.strip():
            # If company units are known, validate against them
            return not valid_unit_names or value.strip() in valid_unit_names
        return _item_validate(col, value)

    return _validate


# Core item columns that map to top-level ItemCreate fields (not attributes).
# Price columns (any key ending in _price) are excluded from attributes separately.
_CORE_ITEM_COLS: frozenset[str] = frozenset({
    "sku", "name", "category", "quantity",
    "weight", "weight_ct", "weight_unit", "sell_by", "status",
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
    split_card = Div(
        Form(
            Strong(t("inv.u2702_split"), cls="action-card-title"),
            Div(
                Input(type="text", name="parts", placeholder="e.g. 3,2,1", cls="form-input form-input--sm",
                      title=f"Comma-separated quantities (current: {current_qty})"),
                Button(t("btn.go"), type="submit", cls="btn btn--primary btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/split",
            hx_target="#item-action-error",
            hx_swap="outerHTML",
        ),
        cls="action-card",
    )

    duplicate_card = Div(
        Form(
            Strong(t("inv.u0001f4cb_duplicate"), cls="action-card-title"),
            Div(
                Input(type="text", name="new_sku", placeholder="New SKU", cls="form-input form-input--sm", required=True),
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
            hx_swap="none",
        ),
        cls="action-card",
    )

    dispose_card = Div(
        Form(
            Strong(t("inv.u0001f5d1_dispose"), cls="action-card-title"),
            Div(
                Input(type="text", name="reason", placeholder="Reason", cls="form-input form-input--sm"),
                Button(t("btn.go"), type="submit", cls="btn btn--danger btn--xs"),
                cls="action-card-row",
            ),
            hx_post=f"/api/items/{entity_id}/dispose",
            hx_swap="none",
        ),
        cls="action-card",
    )

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
            expire_card,
            dispose_card,
            rtv_card,
            bbi_card,
            cls="action-cards-grid",
        ),
        P(t("inv.to_merge_items_select_multiple_from_the_inventory"), cls="form-hint"),
        *([Div(*module_item_actions, cls="actions-group", style="margin-top:0.5rem")] if module_item_actions else []),
        cls="detail-card",
    )


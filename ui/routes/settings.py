# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os
import zoneinfo as _zi
from typing import Any

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.attrs import hx_vals
from ui.components.shell import base_shell, page_header, flash, toast_header
from ui.components.table import EMPTY, unwrap_address
from ui.components.currency import CURRENCIES, CURRENCY_CODES, currency_label, currency_combobox_td
from ui.components.phone import phone_input_td as _phone_input_td, phone_head_items as _phone_head_items
from ui.config import PRIVACY_POLICY_URL
from ui.config import get_token as _token
from ui.config import get_role as _get_role
from celerp.services.pricing import ROUNDING_CHOICES
from ui.i18n import t, get_lang, tier_label
from ui.routes.documents import _action_error


async def _check_permission(request: Request, key: str) -> RedirectResponse | None:
    """Return None if the caller holds the named permission, else a redirect.

    Resolves overrides from the company settings, re-read per request so a
    permission change takes effect on the next page load. Degrades to registry
    defaults (empty settings) when the company row cannot be fetched."""
    from celerp.services.permissions import role_has_permission
    role = _get_role(request)
    token = _token(request)
    settings: dict = {}
    if token:
        try:
            settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            settings = {}
    if not role_has_permission(settings, role, key):
        return RedirectResponse("/dashboard", status_code=302)
    return None


def _category_row(key: str, display_name: str, field_count: int) -> FT:
    """Single category Tr for the Your Categories table."""
    from urllib.parse import quote as _q
    return Tr(
        Td(
            Span(display_name, cls="cat-name-display",
                 hx_get=f"/settings/categories/{_q(key, safe='')}/edit",
                 hx_target="closest tr",
                 hx_swap="outerHTML"),
            cls="cell",
        ),
        Td(str(field_count), cls="cell cell--center your-cats-fields"),
        Td(
            A(t("settings.edit"),
              href=f"/settings/inventory?tab=categories&cat={_q(key, safe='')}",
              cls="btn btn--secondary btn--xs"),
            Button("x", cls="btn btn--danger btn--xs",
                   hx_delete=f"/settings/categories/{_q(key, safe='')}",
                   hx_target="closest tr",
                   hx_swap="outerHTML"),
            cls="cell cell--action your-cats-action",
        ),
        cls="data-row",
    )


# ── Constrained field options ────────────────────────────────────────────

# IANA timezones - canonical names only (no deprecated aliases)
_TIMEZONES: list[str] = sorted(
    tz for tz in _zi.available_timezones()
    if "/" in tz and not tz.startswith("Etc/") and not tz.startswith("SystemV/")
) + ["UTC"]

def _tz_offset_str(tz_name: str) -> str:
    """Return 'UTC+7' / 'UTC-5:30' for a given IANA timezone name."""
    import datetime
    tz = _zi.ZoneInfo(tz_name)
    offset = datetime.datetime.now(tz).utcoffset()
    total_min = int(offset.total_seconds() / 60)
    h, m = divmod(abs(total_min), 60)
    sign = "+" if total_min >= 0 else "-"
    return f"UTC{sign}{h}" if m == 0 else f"UTC{sign}{h}:{m:02d}"

# Precompute search strings: "Asia/Bangkok UTC+7" - built once at startup
_TZ_SEARCH: dict[str, str] = {tz: f"{tz} {_tz_offset_str(tz)}" for tz in _TIMEZONES}

_FISCAL_MONTHS: list[tuple[str, str]] = [
    ("01-01", "January 1"),
    ("02-01", "February 1"),
    ("03-01", "March 1"),
    ("04-01", "April 1"),
    ("05-01", "May 1"),
    ("06-01", "June 1"),
    ("07-01", "July 1"),
    ("08-01", "August 1"),
    ("09-01", "September 1"),
    ("10-01", "October 1"),
    ("11-01", "November 1"),
    ("12-01", "December 1"),
]
_FISCAL_VALUES: frozenset[str] = frozenset(v for v, _ in _FISCAL_MONTHS)

# Fields that use constrained selects/comboboxes instead of free-text input
_COMPANY_SELECT_FIELDS = frozenset({"currency", "timezone", "fiscal_year_start"})




def _load_cat_schema_sorted(fields_raw: list[dict]) -> list[dict]:
    return sorted(fields_raw, key=lambda x: x.get("position", 0))


def _load_cat_schema_sorted(fields_raw: list[dict]) -> list[dict]:
    return sorted(fields_raw, key=lambda x: x.get("position", 0))


# ── DRY tax/terms CRUD registration helpers ──────────────────────────────

def _register_tax_crud(app, prefix: str, get_fn_name: str, patch_fn_name: str, redirect_url: str):
    """Register GET edit, PATCH, POST new, DELETE for tax CRUD at /settings/{prefix}/...

    get_fn_name/patch_fn_name are attribute names on the ``api`` module, resolved
    at call time so that ``unittest.mock.patch`` can intercept them.
    """

    def _make_edit(pfx, gname):
        async def tax_field_edit(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            try:
                taxes = await getattr(api, gname)(token)
            except APIError as e:
                return P(f"Error: {e.detail}", cls="cell-error")
            tax = taxes[idx] if idx < len(taxes) else {}
            val = str(tax.get(field, "") or "")
            if field == "tax_type":
                return Td(
                    Select(
                        *[Option(label, value=v, selected=(v == val))
                          for v, label in [("sales", "Sales"), ("purchase", "Purchase"), ("both", "Both")]],
                        name="value",
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                        hx_trigger="change",
                        cls="cell-input cell-input--select", autofocus=True,
                    ),
                    cls="cell cell--editing",
                )
            input_type = "number" if field == "rate" else "text"
            return Td(
                Input(
                    type=input_type, name="value", value=val,
                    hx_patch=f"/settings/{pfx}/{idx}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="blur delay:200ms",
                    cls="cell-input" + (" cell-input--number" if input_type == "number" else ""),
                    autofocus=True,
                    **({"step": "0.01"} if field == "rate" else {}),
                ),
                cls="cell cell--editing",
            )
        return tax_field_edit

    def _make_patch(pfx, gname, pname):
        async def tax_field_patch(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            form = await request.form()
            value = str(form.get("value", ""))
            if field == "tax_type" and value not in {"sales", "purchase", "both"}:
                return P(f"Invalid tax type: {value!r}", cls="cell-error")
            if field == "rate":
                try:
                    float(value)
                except (ValueError, TypeError):
                    return P(t("settings.rate_must_be_a_number"), cls="cell-error")
            try:
                taxes = await getattr(api, gname)(token)
                if idx < len(taxes):
                    if field == "rate":
                        taxes[idx][field] = float(value)
                    elif field == "is_default":
                        taxes[idx][field] = value.lower() in ("true", "yes", "1")
                    else:
                        taxes[idx][field] = value
                await getattr(api, pname)(token, taxes)
                taxes = await getattr(api, gname)(token)
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
            tax = taxes[idx] if idx < len(taxes) else {}
            return _tax_display_cell(idx, field, tax, prefix=pfx)
        return tax_field_patch

    def _make_new(gname, pname, redir):
        async def create_tax(request: Request):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                taxes = await getattr(api, gname)(token)
                taxes.append({"name": "New Tax", "rate": 0.0, "tax_type": "sales", "is_default": False, "description": ""})
                await getattr(api, pname)(token, taxes)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return create_tax

    def _make_delete(gname, pname, redir):
        async def delete_tax(request: Request, idx: int):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                taxes = await getattr(api, gname)(token)
                if 0 <= idx < len(taxes):
                    taxes.pop(idx)
                    await getattr(api, pname)(token, taxes)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return delete_tax

    app.get(f"/settings/{prefix}/{{idx}}/{{field}}/edit")(_make_edit(prefix, get_fn_name))
    app.patch(f"/settings/{prefix}/{{idx}}/{{field}}")(_make_patch(prefix, get_fn_name, patch_fn_name))
    app.post(f"/settings/{prefix}/new")(_make_new(get_fn_name, patch_fn_name, redirect_url))
    app.delete(f"/settings/{prefix}/{{idx}}")(_make_delete(get_fn_name, patch_fn_name, redirect_url))


def _register_terms_crud(app, prefix: str, get_fn_name: str, patch_fn_name: str, redirect_url: str):
    """Register GET edit, PATCH, POST new, DELETE for terms CRUD at /settings/{prefix}/..."""

    def _make_edit(pfx, gname):
        async def term_field_edit(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            try:
                terms = await getattr(api, gname)(token)
            except APIError as e:
                return P(f"Error: {e.detail}", cls="cell-error")
            term = terms[idx] if idx < len(terms) else {}
            val = str(term.get(field, "") or "")
            input_type = "number" if field == "days" else "text"
            return Td(
                Input(
                    type=input_type, name="value", value=val,
                    hx_patch=f"/settings/{pfx}/{idx}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="blur delay:200ms",
                    cls="cell-input" + (" cell-input--number" if input_type == "number" else ""),
                    autofocus=True,
                ),
                cls="cell cell--editing",
            )
        return term_field_edit

    def _make_patch(pfx, gname, pname):
        async def term_field_patch(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            form = await request.form()
            value = str(form.get("value", ""))
            if field == "days":
                try:
                    days_int = int(value)
                except (ValueError, TypeError):
                    return P(t("error.days_must_be_number"), cls="cell-error")
                if days_int < 0:
                    return P(t("error.days_negative"), cls="cell-error")
            try:
                terms = await getattr(api, gname)(token)
                if idx < len(terms):
                    terms[idx][field] = int(value) if field == "days" else value
                await getattr(api, pname)(token, terms)
                terms = await getattr(api, gname)(token)
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
            term = terms[idx] if idx < len(terms) else {}
            return _term_display_cell(idx, field, term, prefix=pfx)
        return term_field_patch

    def _make_new(gname, pname, redir):
        async def create_term(request: Request):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                terms = await getattr(api, gname)(token)
                terms.append({"name": "New Term", "days": 30, "description": ""})
                await getattr(api, pname)(token, terms)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return create_term

    def _make_delete(gname, pname, redir):
        async def delete_term(request: Request, idx: int):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                terms = await getattr(api, gname)(token)
                if 0 <= idx < len(terms):
                    terms.pop(idx)
                    await getattr(api, pname)(token, terms)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return delete_term

    app.get(f"/settings/{prefix}/{{idx}}/{{field}}/edit")(_make_edit(prefix, get_fn_name))
    app.patch(f"/settings/{prefix}/{{idx}}/{{field}}")(_make_patch(prefix, get_fn_name, patch_fn_name))
    app.post(f"/settings/{prefix}/new")(_make_new(get_fn_name, patch_fn_name, redirect_url))
    app.delete(f"/settings/{prefix}/{{idx}}")(_make_delete(get_fn_name, patch_fn_name, redirect_url))


def _toast_response(message: str, *fragments: FT):
    """Swap the given fragments and surface ``message`` as an auto-dismissing error toast,
    so no stale error text persists in the page."""
    from starlette.responses import HTMLResponse as _HR
    body = "".join(to_xml(f) for f in fragments)
    return _HR(body, headers=toast_header(message, "error"))


def _status_error(status_id: str, message: str, oob: FT | None = None):
    """Error for an auto-saving control: toast the message and reset the inline status
    span. ``oob`` optionally re-syncs a related element (e.g. a select showing a
    rejected choice)."""
    span = Span("", id=status_id, cls="settings-hint")
    return _toast_response(message, span, oob) if oob is not None else _toast_response(message, span)


def _pl_cell_error(idx: int, field: str, pl: dict, prefix: str, message: str):
    """Failed cell save: swap the saved display value back into the cell and toast the
    error. The cell stays click-to-edit for a retry."""
    return _toast_response(message, _price_list_display_cell(idx, field, pl, prefix=prefix))


def _register_price_lists_crud(app, prefix: str, get_fn_name: str, patch_fn_name: str, redirect_url: str):
    """Register GET edit, PATCH, POST new, DELETE for price list CRUD at /settings/{prefix}/..."""

    def _make_edit(pfx, gname):
        async def price_list_field_edit(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            try:
                price_lists = await getattr(api, gname)(token)
            except APIError as e:
                return P(f"Error: {e.detail}", cls="cell-error")
            pl = price_lists[idx] if idx < len(price_lists) else {}
            val = str(pl.get(field, "") or "")
            if field == "multiplier":
                return Td(
                    Input(
                        type="number", name="value", value=val, step="any", min="0",
                        placeholder=EMPTY,
                        title=t("settings.factor_tooltip"),
                        # Validate before sending: an unparseable entry reads as "" from a
                        # number input and would otherwise save as an unintended clear.
                        hx_validate="true",
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                        hx_trigger="blur delay:200ms, keyup[key=='Enter']",
                        cls="cell-input",
                        autofocus=True,
                    ),
                    cls="cell cell--editing",
                )
            if field == "rounding":
                def _selected(choice: str) -> bool:
                    try:
                        return val != "" and float(val) == float(choice)
                    except ValueError:
                        return False
                return Td(
                    Select(
                        Option(EMPTY, value="", selected=(val == "")),
                        *[Option(c, value=c, selected=_selected(c)) for c in ROUNDING_CHOICES],
                        name="value",
                        title=t("settings.rounding_tooltip"),
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                        hx_trigger="change",
                        cls="cell-input cell-input--select", autofocus=True,
                    ),
                    cls="cell cell--editing",
                )
            return Td(
                Input(
                    type="text", name="value", value=val,
                    hx_patch=f"/settings/{pfx}/{idx}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="blur delay:200ms, keyup[key=='Enter']",
                    cls="cell-input",
                    autofocus=True,
                ),
                cls="cell cell--editing",
            )
        return price_list_field_edit

    def _make_patch(pfx, gname, pname):
        async def price_list_field_patch(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            form = await request.form()
            value = str(form.get("value", ""))
            try:
                price_lists = await getattr(api, gname)(token)
                renamed_base: str | None = None
                renamed_default = False
                if idx < len(price_lists):
                    if field in ("multiplier", "rounding"):
                        if value.strip() == "":
                            price_lists[idx].pop(field, None)
                            if field == "multiplier":
                                # Rounding only applies to a derived list; clearing the factor clears it too.
                                price_lists[idx].pop("rounding", None)
                        else:
                            try:
                                price_lists[idx][field] = float(value)
                            except ValueError:
                                return _pl_cell_error(idx, field, price_lists[idx], pfx,
                                                      t("settings.enter_a_number_greater_than_0"))
                    else:
                        if field == "name":
                            # Base and default are stored by name; a rename must carry them along
                            # (the base atomically, so validation holds through the write).
                            old_name = price_lists[idx].get("name", "")
                            if old_name == await api.get_base_price_list(token):
                                renamed_base = value
                            renamed_default = old_name == await api.get_default_price_list(token)
                        price_lists[idx][field] = value
                await getattr(api, pname)(token, price_lists, base_price_list=renamed_base)
                if renamed_default:
                    await api.patch_default_price_list(token, value)
                price_lists = await getattr(api, gname)(token)
            except APIError as e:
                # Re-fetch so the cell reverts to the value the server actually holds.
                try:
                    price_lists = await getattr(api, gname)(token)
                    pl = price_lists[idx] if idx < len(price_lists) else {}
                except APIError:
                    pl = {}
                return _pl_cell_error(idx, field, pl, pfx, str(e.detail))
            pl = price_lists[idx] if idx < len(price_lists) else {}
            cell = _price_list_display_cell(idx, field, pl, prefix=pfx)
            if field == "name":
                # Re-render both selectors OOB so they reflect the new name. Returned as
                # sibling fragments, NOT wrapped in a Div: an HTML5 parser drops a <td>
                # nested inside a <div>, which would destroy the swapped name cell.
                try:
                    base_name = await api.get_base_price_list(token)
                    default_name = await api.get_default_price_list(token)
                except APIError:
                    return cell
                visible_names = [p.get("name", "") for p in price_lists if p.get("name") != "Cost"]
                all_names = [p.get("name", "") for p in price_lists]
                return (
                    cell,
                    _pl_select("default-price-list-select", "/settings/default-price-list",
                               "#default-price-list-status", visible_names, default_name, oob=True),
                    _pl_select("base-price-list-select", "/settings/base-price-list",
                               "#base-price-list-status", all_names, base_name, oob=True),
                )
            return cell
        return price_list_field_patch

    def _make_new(gname, pname, redir):
        async def create_price_list(request: Request):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                price_lists = await getattr(api, gname)(token)
                # Unique placeholder name: list names key derivation, default, and base.
                names = {pl.get("name", "") for pl in price_lists}
                name, n = "New Price List", 2
                while name in names:
                    name, n = f"New Price List {n}", n + 1
                price_lists.append({"name": name, "description": ""})
                await getattr(api, pname)(token, price_lists)
            except APIError as e:
                return _action_error(str(e.detail))
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return create_price_list

    def _make_delete(gname, pname, redir, get_default_fn_name: str):
        async def delete_price_list(request: Request, idx: int):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                price_lists = await getattr(api, gname)(token)
                default_name = await getattr(api, get_default_fn_name)(token)
                if 0 <= idx < len(price_lists):
                    name = price_lists[idx].get("name", "")
                    if name == "Retail":
                        return _action_error(t("settings.retail_price_list_cannot_be_deleted"))
                    if name in ("Cost", "Wholesale"):
                        return _action_error(t("settings.system_price_list_cannot_be_deleted").replace("{name}", name))
                    if name == default_name:
                        return _action_error(t("settings.default_price_list_cannot_be_deleted").replace("{name}", name))
                    base_name = await api.get_base_price_list(token)
                    if name == base_name:
                        return _action_error(t("settings.base_price_list_cannot_be_deleted").replace("{name}", name))
                    price_lists.pop(idx)
                    await getattr(api, pname)(token, price_lists)
            except APIError as e:
                return _action_error(str(e.detail))
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return delete_price_list

    app.get(f"/settings/{prefix}/{{idx}}/{{field}}/edit")(_make_edit(prefix, get_fn_name))
    app.patch(f"/settings/{prefix}/{{idx}}/{{field}}")(_make_patch(prefix, get_fn_name, patch_fn_name))
    app.post(f"/settings/{prefix}/new")(_make_new(get_fn_name, patch_fn_name, redirect_url))
    app.delete(f"/settings/{prefix}/{{idx}}")(_make_delete(get_fn_name, patch_fn_name, redirect_url, "get_default_price_list"))


def _factory_reset_card() -> FT:
    """Reset All Data card — lives inside the existing Danger Zone section."""
    modal_id = "factory-reset-modal"
    step1_id = "factory-reset-step1"
    step2_id = "factory-reset-step2"
    input_id = "factory-reset-confirm-input"
    btn_id   = "factory-reset-confirm-btn"
    close_js = f"document.getElementById('{modal_id}').close()"

    to_step2_js = (
        f"document.getElementById('{step1_id}').style.display='none';"
        f"document.getElementById('{step2_id}').style.display='block';"
        f"document.getElementById('{input_id}').focus();"
    )
    validate_js = (
        f"document.getElementById('{btn_id}').disabled="
        f"document.getElementById('{input_id}').value!=='RESET';"
    )
    success_js = (
        f"document.getElementById('{modal_id}').addEventListener('htmx:afterRequest',function(e){{"
        f"if(e.detail.xhr.status===200){{window.location.href='/setup';}}"
        f"}},{{once:true}});"
    )

    return Div(
        Div(id="reset-flash"),
        P("Permanently delete all company data and return to the initial setup wizard. "
          "Installed modules and server settings will be preserved.",
          cls="settings-help-text"),
        Button("Reset All Data",
               type="button",
               cls="btn btn--outline btn--danger",
               onclick=f"document.getElementById('{modal_id}').showModal()"),
        Dialog(
            # Step 1: warning
            Div(
                Div(
                    Div(
                        Span("⚠", cls="reset-modal__icon"),
                        H3("Reset all data?", cls="modal-dialog__title reset-modal__title--danger"),
                        cls="reset-modal__title-row",
                    ),
                    Button("✕", type="button", cls="modal-dialog__close", aria_label="Close",
                           onclick=close_js),
                    cls="modal-dialog__header",
                ),
                Div(
                    P("This will permanently delete all business data — contacts, items, "
                      "transactions, documents, and all other records."),
                    P(Strong("Your app settings and installed modules will be preserved. "
                             "This cannot be undone.")),
                    Div(
                        A("Download backup first",
                          href="/backup/export",
                          cls="btn btn--sm btn--ghost",
                          onclick=to_step2_js,
                          download=True),
                        Button("Skip — continue",
                               type="button",
                               cls="btn btn--sm btn--secondary",
                               onclick=to_step2_js),
                        cls="modal-dialog__actions",
                    ),
                    cls="reset-modal__body",
                ),
                id=step1_id,
            ),
            # Step 2: type-to-confirm
            Div(
                Div(
                    H3("Confirm deletion", cls="modal-dialog__title reset-modal__title--danger"),
                    Button("✕", type="button", cls="modal-dialog__close", aria_label="Close",
                           onclick=close_js),
                    cls="modal-dialog__header",
                ),
                Div(
                    P("Type ", Strong("RESET"), " to confirm. This cannot be undone."),
                    Input(type="text", id=input_id, placeholder="RESET",
                          autocomplete="off", cls="form-input",
                          oninput=validate_js),
                    Div(
                        Button("Delete everything",
                               type="submit",
                               id=btn_id,
                               cls="btn btn--danger",
                               disabled=True,
                               hx_post="/settings/factory-reset",
                               hx_target="#reset-flash",
                               hx_swap="innerHTML",
                               onclick=success_js),
                        Button("Cancel",
                               type="button",
                               cls="btn btn--ghost",
                               onclick=close_js),
                        cls="modal-dialog__actions",
                    ),
                    cls="reset-modal__body",
                ),
                id=step2_id,
                style="display:none",
            ),
            id=modal_id,
            cls="modal-dialog",
        ),
        cls="settings-card settings-card--danger",
    )


def setup_routes(app):

    @app.get("/settings")
    async def settings_redirect(request: Request):
        """Redirect legacy /settings to the new /settings/general."""
        # Preserve tab redirects for backward-compat deep links
        tab = request.query_params.get("tab", "")
        _SALES_TABS = {"taxes", "terms"}
        _INVENTORY_TABS = {"schema", "locations", "import-history", "bulk-attach", "verticals"}
        _ACCOUNTING_TABS = {"bank-accounts"}
        if tab in _SALES_TABS:
            return RedirectResponse(f"/settings/sales?tab={tab}", status_code=302)
        if tab in _ACCOUNTING_TABS:
            return RedirectResponse(f"/settings/accounting?tab={tab}", status_code=302)
        if tab in _INVENTORY_TABS:
            if tab == "schema":
                cat_tab = request.query_params.get("cat_tab", "")
                dest = f"/settings/inventory?tab=category-library"
                if cat_tab:
                    dest += f"&cat={cat_tab}"
                return RedirectResponse(dest, status_code=302)
            if tab == "verticals":
                return RedirectResponse("/settings/inventory?tab=category-library", status_code=302)
            return RedirectResponse(f"/settings/inventory?tab={tab}", status_code=302)
        # general tabs + default
        if tab == "cloud-relay":
            return RedirectResponse("/settings/cloud", status_code=302)
        if tab == "ai":
            return RedirectResponse("/ai", status_code=302)
        if tab in {"company", "users", "modules", "backup"}:
            return RedirectResponse(f"/settings/general?tab={tab}", status_code=302)
        setup_done = request.query_params.get("setup") == "done"
        dest = "/settings/general"
        if setup_done:
            dest += "?setup=done"
        return RedirectResponse(dest, status_code=302)

    # ── Preference endpoints ─────────────────────────────────────────
    @app.get("/settings/preferences/{key}/edit")
    async def preference_edit(request: Request, key: str):
        """HTMX: return editable select for a dashboard preference."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            company = await api.get_company(token)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        current = str(company.get(key, "") or "")
        lang = get_lang(request)
        if key == "docs_default_preset":
            options = [
                ("last_12m", t("filter.last_12m", lang)),
                ("this_year", "This calendar year"),
                ("all", t("filter.all_time", lang)),
            ]
            return Td(
                Select(
                    *[Option(label, value=val, selected=(val == current))
                      for val, label in options],
                    name="value",
                    hx_patch=f"/settings/preferences/{key}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        if key == "default_per_page":
            options = [("25", "25"), ("50", "50"), ("100", "100"), ("250", "250"), ("500", "500")]
            return Td(
                Select(
                    *[Option(label, value=val, selected=(val == current))
                      for val, label in options],
                    name="value",
                    hx_patch=f"/settings/preferences/{key}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        return P(t("msg.unknown_preference"), cls="cell-error")

    @app.patch("/settings/preferences/{key}")
    async def preference_patch(request: Request, key: str):
        """HTMX: save a dashboard preference, return display cell."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        raw = str(form.get("value", ""))
        # Coerce numeric preferences
        value: str | int = int(raw) if key == "default_per_page" and raw.isdigit() else raw
        try:
            await api.patch_company(token, {key: value})
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _preference_display_cell(key, value)

    # ── Password change (POST only - UI is in settings_general) ──────
    @app.post("/settings/password")
    async def settings_password_submit(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        current = str(form.get("current_password", ""))
        new_pw = str(form.get("new_password", ""))
        confirm = str(form.get("confirm_password", ""))
        lang = get_lang(request)

        if not current or not new_pw:
            return _password_form(error=t("settings.all_fields_required", lang), lang=lang)
        if new_pw != confirm:
            return _password_form(error=t("settings.passwords_do_not_match", lang), lang=lang)
        if len(new_pw) < 8:
            return _password_form(error=t("settings.password_min_length", lang), lang=lang)
        try:
            await api.change_password(token, current, new_pw)
        except APIError as e:
            return _password_form(error=e.detail, lang=lang)
        return _password_form(success=t("settings.password_changed", lang), lang=lang)

    @app.get("/settings/company/companies-list")
    async def company_settings_companies_list(request: Request):
        """HTMX fragment: list of all user's companies with switch links."""
        from fasthtml.common import to_xml
        token = _token(request)
        if not token:
            return Response(content="", media_type="text/html")
        lang = get_lang(request)
        try:
            resp = await api.my_companies(token)
            companies = resp.get("items", []) if isinstance(resp, dict) else resp
        except Exception:
            return Response(content="", media_type="text/html")
        if not companies:
            return Response(to_xml(P(t("msg.no_results", lang), cls="settings-hint")), media_type="text/html")
        if len(companies) == 1:
            name = companies[0].get("company_name", "")
            content = Span(name, cls="settings-hint")
        else:
            options = [
                Option(
                    c.get("company_name", ""),
                    value=c.get("company_id", ""),
                    selected=c.get("is_current", False),
                )
                for c in companies
            ]
            content = Select(
                *options,
                onchange="location='/switch-company/'+this.value",
                cls="cell-input cell-input--select",
                style="max-width:320px;",
            )
        return Response(to_xml(content), media_type="text/html")

    # ── Company PATCH endpoints ──────────────────────────────────────
    @app.get("/settings/company/{field}/edit")
    async def company_field_edit(request: Request, field: str):
        """HTMX: return editable input for a company field."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            company = await api.get_company(token)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        val = str(company.get(field, "") or "")

        if field == "phone":
            return _phone_input_td(
                value=val,
                patch_url=f"/settings/company/phone",
                cancel_url=f"/settings/company/phone/display",
                field_id="company-phone",
            )

        if field == "currency":
            return currency_combobox_td(
                value=val,
                hidden_id=f"company-{field}-input",
                patch_url=f"/settings/company/{field}",
                cancel_url=f"/settings/company/{field}/display",
            )

        if field == "fiscal_year_start":
            return Td(
                Select(
                    *[Option(label, value=v, selected=(v == val)) for v, label in _FISCAL_MONTHS],
                    name="value",
                    id=f"company-{field}-input",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                Button(t("btn.save"), type="button",
                       hx_patch=f"/settings/company/{field}",
                       hx_target="closest td", hx_swap="outerHTML",
                       hx_include=f"#company-{field}-input",
                       cls="btn btn--primary btn--xs ml-sm"),
                Button(t("btn.cancel"), type="button",
                       hx_get=f"/settings/company/{field}/display",
                       hx_target="closest td", hx_swap="outerHTML",
                       cls="btn btn--secondary btn--xs ml-xs"),
                cls="cell cell--editing",
            )

        if field == "timezone":
            # Searchable combobox - searches both IANA name and UTC offset
            return Td(
                Div(
                    Input(
                        type="text", value=val, placeholder="Search timezone or UTC offset…",
                        cls="cell-input combobox-input", autofocus=True,
                    ),
                    Input(type="hidden", name="value", value=val,
                          id=f"company-{field}-input"),
                    Div(
                        *[Div(
                            Span(tz, cls="tz-name"),
                            Span(_tz_offset_str(tz), cls="tz-offset"),
                            cls="combobox-option",
                            data_value=tz,
                            data_search=_TZ_SEARCH[tz],
                          ) for tz in _TIMEZONES],
                        cls="combobox-list",
                    ),
                    cls="combobox-wrap",
                ),
                Button(t("btn.save"), type="button",
                       hx_patch=f"/settings/company/{field}",
                       hx_target="closest td", hx_swap="outerHTML",
                       hx_include=f"#company-{field}-input",
                       cls="btn btn--primary btn--xs ml-sm"),
                Button(t("btn.cancel"), type="button",
                       hx_get=f"/settings/company/{field}/display",
                       hx_target="closest td", hx_swap="outerHTML",
                       cls="btn btn--secondary btn--xs ml-xs"),
                cls="cell cell--editing",
            )

        return Td(
            Input(
                type="text", name="value", value=val,
                id=f"company-{field}-input",
                cls="cell-input",
                autofocus=True,
            ) if field != "address" else Textarea(
                val,
                name="value",
                id=f"company-{field}-input",
                cls="cell-input",
                rows="3",
                autofocus=True,
            ),
            Button(t("btn.save"), type="button",
                   hx_patch=f"/settings/company/{field}",
                   hx_target="closest td", hx_swap="outerHTML",
                   hx_include=f"#company-{field}-input",
                   cls="btn btn--primary btn--xs ml-sm"),
            Button(t("btn.cancel"), type="button",
                   hx_get=f"/settings/company/{field}/display",
                   hx_target="closest td", hx_swap="outerHTML",
                   cls="btn btn--secondary btn--xs ml-xs"),
            cls="cell cell--editing",
        )

    @app.get("/settings/company/{field}/display")
    async def company_field_display(request: Request, field: str):
        """HTMX: return read-only display cell (used by Cancel button)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            company = await api.get_company(token)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        return _company_display_cell(field, company.get(field))

    @app.patch("/settings/company/{field}")
    async def company_field_patch(request: Request, field: str):
        """HTMX: save a company field, return display cell."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))

        # Server-side validation for constrained fields
        if field == "name" and not value.strip():
            return P(t("error.company_name_blank"), cls="cell-error")
        if field == "slug":
            import re as _re
            if not value.strip() or not _re.fullmatch(r"[a-z0-9][a-z0-9\-]*", value.strip()):
                return P(t("error.invalid_slug"), cls="cell-error")
        if field == "currency" and value not in CURRENCY_CODES:
            return P(f"Invalid currency: {value!r}", cls="cell-error")
        if field == "fiscal_year_start" and value not in _FISCAL_VALUES:
            return P(f"Invalid fiscal year start: {value!r}", cls="cell-error")
        if field == "timezone":
            try:
                _zi.ZoneInfo(value)
            except (_zi.ZoneInfoNotFoundError, KeyError):
                return P(f"Unknown timezone: {value!r}", cls="cell-error")

        try:
            await api.patch_company(token, {field: value})
            company = await api.get_company(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        flat = {**company, **(company.get("settings") or {})}
        return _company_display_cell(field, flat.get(field))

    # ── Users PATCH endpoints ────────────────────────────────────────
    @app.get("/settings/users/{user_id}/{field}/edit")
    async def user_field_edit(request: Request, user_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            users = (await api.get_users(token)).get("items", [])
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        user = next((u for u in users if u.get("id") == user_id), {})
        val = str(user.get(field, "") or "")
        if field == "role":
            return Td(
                Select(
                    *[Option(r.title(), value=r, selected=(r == val)) for r in ["owner", "admin", "manager", "operator", "viewer"]],
                    name="value",
                    hx_patch=f"/settings/users/{user_id}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        if field == "is_active":
            return Td(
                Select(
                    Option(t("th.active"), value="true", selected=val.lower() == "true"),
                    Option(t("settings.inactive"), value="false", selected=val.lower() != "true"),
                    name="value",
                    hx_patch=f"/settings/users/{user_id}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        return Td(
            Input(
                type="text", name="value", value=val,
                hx_patch=f"/settings/users/{user_id}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
            ),
            cls="cell cell--editing",
        )

    @app.patch("/settings/users/{user_id}/{field}")
    async def user_field_patch(request: Request, user_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        patch_data = {field: value}
        if field == "is_active":
            new_active = value.lower() == "true"
            # Block self-deactivation
            if not new_active:
                import base64, json as _json
                try:
                    payload_b64 = token.split(".")[1]
                    payload_b64 += "=" * (-len(payload_b64) % 4)
                    claims = _json.loads(base64.b64decode(payload_b64))
                    if str(claims.get("sub", "")) == str(user_id):
                        return P(t("error.cannot_deactivate_self"), cls="cell-error")
                except Exception:
                    pass
            patch_data[field] = new_active
        if field == "role" and value != "owner":
            # Block if this is the last owner
            try:
                users_now = (await api.get_users(token)).get("items", [])
                owner_count = sum(1 for u in users_now if u.get("role") == "owner" and u.get("is_active", True))
                is_currently_owner = any(u.get("id") == user_id and u.get("role") == "owner" for u in users_now)
                if is_currently_owner and owner_count <= 1:
                    return P(t("settings.cannot_demote_the_last_owner_assign_another_owner"), cls="cell-error")
            except APIError:
                pass
        try:
            await api.patch_user(token, user_id, patch_data)
            users = (await api.get_users(token)).get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        user = next((u for u in users if u.get("id") == user_id), {})
        return _user_display_cell(user_id, field, user.get(field))

    # ── Role permission matrix ───────────────────────────────────────
    @app.patch("/settings/roles/{perm_key}/{role_key}")
    async def role_permission_patch(request: Request, perm_key: str, role_key: str):
        from celerp.services.permissions import PERMISSIONS
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        granted = str(form.get("granted", "")).lower() == "true"
        perm = next((p for p in PERMISSIONS if p.key == perm_key), None)
        if perm is None:
            return P(f"Unknown permission '{perm_key}'", cls="cell-error")
        try:
            await api.patch_role_permission(token, perm_key, role_key, granted)
            company = await api.get_company(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _role_matrix_row(perm, company.get("settings"), is_owner=True)

    # ── Invite user ──────────────────────────────────────────────────
    @app.get("/settings/users/new")
    async def invite_user_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        return await base_shell(
            page_header(t("btn.create_user", lang), A(t("btn.back_to_settings", lang), href="/settings/general?tab=users", cls="btn btn--secondary")),
            Div(
                H3(t("settings.new_user", lang), cls="settings-section-title"),
                Form(
                    Table(
                        Tr(Td(t("label.name", lang), cls="detail-label"),
                           Td(Input(type="text", name="name", placeholder="Full name", cls="cell-input", required=True))),
                        Tr(Td(t("label.email", lang), cls="detail-label"),
                           Td(Input(type="email", name="email", placeholder="user@example.com", cls="cell-input", required=True))),
                        Tr(Td(t("label.password", lang), cls="detail-label"),
                           Td(Input(type="password", name="password", placeholder="Temporary password", cls="cell-input", required=True))),
                        Tr(Td(t("label.role", lang), cls="detail-label"),
                           Td(Select(
                               Option(t("settings.owner"), value="owner"),
                               Option(t("settings.admin"), value="admin"),
                               Option(t("settings.manager"), value="manager"),
                               Option(t("settings.operator"), value="operator"),
                               Option(t("settings.viewer"), value="viewer"),
                               name="role", cls="cell-input cell-input--select",
                           ))),
                        cls="detail-table",
                    ),
                    Div(
                        Button(t("btn.create_user", lang), type="submit", cls="btn btn--primary"),
                        A(t("btn.cancel", lang), href="/settings/general?tab=users", cls="btn btn--secondary ml-sm"),
                        cls="mt-md",
                    ),
                    Div(id="invite-error"),
                    hx_post="/settings/users/new",
                    hx_target="#invite-error",
                    hx_swap="innerHTML",
                ),
                cls="settings-card",
            ),
            title="Create User - Celerp",
            nav_active="settings",
            lang=lang,
            request=request,
        )

    @app.post("/settings/users/new")
    async def invite_user_submit(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="error-banner")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", "")).strip()
        role = str(form.get("role", "operator")).strip()
        if not name or not email or not password:
            return P(t("error.name_email_password_required"), cls="error-banner")
        try:
            await api.create_user(token, {"name": name, "email": email, "password": password, "role": role})
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        # Redirect to users tab on success
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/general?tab=users"})

    # ── Taxes PATCH endpoints ────────────────────────────────────────
    _register_tax_crud(app, "taxes", "get_taxes", "patch_taxes", "/settings/sales?tab=taxes")
    _register_tax_crud(app, "purchasing-taxes", "get_purchasing_taxes", "patch_purchasing_taxes", "/settings/purchasing?tab=taxes")

    # ── Payment Terms PATCH endpoints ────────────────────────────────
    _register_terms_crud(app, "terms", "get_payment_terms", "patch_payment_terms", "/settings/sales?tab=terms")
    _register_terms_crud(app, "purchasing-terms", "get_purchasing_payment_terms", "patch_purchasing_payment_terms", "/settings/purchasing?tab=terms")

    # ── Price Lists CRUD endpoints ───────────────────────────────────
    _register_price_lists_crud(app, "price-lists", "get_price_lists", "patch_price_lists", "/settings/contacts?tab=price-lists")

    @app.post("/settings/default-price-list")
    async def set_default_price_list(request: Request):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            return _status_error("default-price-list-status", t("settings.name_required"))
        try:
            await api.patch_default_price_list(token, name)
        except APIError as e:
            return _status_error("default-price-list-status", str(e.detail))
        return Span(t("settings._saved"), id="default-price-list-status", cls="flash flash--success")

    @app.post("/settings/base-price-list")
    async def set_base_price_list(request: Request):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            return _status_error("base-price-list-status", t("settings.name_required"))
        try:
            await api.patch_base_price_list(token, name)
        except APIError as e:
            # Re-sync the select out-of-band: without it the rejected choice would keep
            # displaying as if it had been saved.
            oob = None
            try:
                price_lists = await api.get_price_lists(token)
                base_name = await api.get_base_price_list(token)
                oob = _pl_select("base-price-list-select", "/settings/base-price-list",
                                 "#base-price-list-status",
                                 [pl.get("name", "") for pl in price_lists], base_name, oob=True)
            except APIError:
                pass
            return _status_error("base-price-list-status", str(e.detail), oob)
        return Span(t("settings._saved"), id="base-price-list-status", cls="flash flash--success")

    # ── Terms & Conditions CRUD endpoints ────────────────────────────
    _register_tc_crud(app, "terms-conditions", "get_terms_conditions", "patch_terms_conditions", "/settings/sales?tab=terms-conditions", scope_doc_types=_TC_DOC_TYPES_SALES)
    _register_tc_crud(app, "purchasing-terms-conditions", "get_terms_conditions", "patch_terms_conditions", "/settings/purchasing?tab=terms-conditions", scope_doc_types=_TC_DOC_TYPES_PURCHASING)

    # ── Schema PATCH endpoints ───────────────────────────────────────
    @app.get("/settings/schema/{idx}/{field}/edit")
    async def schema_field_edit(request: Request, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            schema = await api.get_item_schema(token)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        sorted_schema = sorted(schema, key=lambda x: x.get("position", 0))
        f = sorted_schema[idx] if idx < len(sorted_schema) else {}
        val = str(f.get(field, "") or "")
        if field in ("required", "editable"):
            return Td(
                Select(
                    Option(t("settings.yes"), value="true", selected=val.lower() == "true"),
                    Option(t("settings.no"), value="false", selected=val.lower() != "true"),
                    name="value",
                    hx_patch=f"/settings/schema/{idx}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        if field == "type":
            _SCHEMA_TYPES = ["text", "number", "money", "select", "date", "boolean", "weight", "status", "image"]
            return Td(
                Select(
                    *[Option(stype, value=stype, selected=(stype == val)) for stype in _SCHEMA_TYPES],
                    name="value",
                    hx_patch=f"/settings/schema/{idx}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )
        if field == "options":
            val = ", ".join(f.get("options", []))
        return Td(
            Input(
                type="number" if field == "position" else "text",
                name="value", value=val,
                hx_patch=f"/settings/schema/{idx}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
            ),
            cls="cell cell--editing",
        )

    @app.patch("/settings/schema/{idx}/{field}")
    async def schema_field_patch(request: Request, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        _SCHEMA_TYPES = frozenset({"text", "number", "money", "select", "date", "boolean", "weight", "status", "image"})
        if field == "type" and value not in _SCHEMA_TYPES:
            return P(f"Invalid field type: {value!r}", cls="cell-error")
        if field == "position":
            try:
                int(value)
            except (ValueError, TypeError):
                return P(t("settings.position_must_be_a_whole_number"), cls="cell-error")
        try:
            schema = await api.get_item_schema(token)
            sorted_schema = sorted(schema, key=lambda x: x.get("position", 0))
            if idx < len(sorted_schema):
                if field in ("required", "editable"):
                    sorted_schema[idx][field] = value.lower() in ("true", "yes", "1")
                elif field == "position":
                    sorted_schema[idx][field] = int(value)
                elif field == "options":
                    sorted_schema[idx][field] = [o.strip() for o in value.split(",") if o.strip()]
                else:
                    sorted_schema[idx][field] = value
            await api.patch_item_schema(token, sorted_schema)
            schema = await api.get_item_schema(token)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        sorted_schema = sorted(schema, key=lambda x: x.get("position", 0))
        f = sorted_schema[idx] if idx < len(sorted_schema) else {}
        return _schema_display_cell(idx, field, f)

    # ── Category schema PATCH/DELETE/POST endpoints ───────────────────

    @app.get("/settings/cat-schema/{category}/{idx}/{field}/display")
    async def cat_schema_field_display(request: Request, category: str, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            fields = await api.get_category_schema(token, category)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        sorted_fields = _load_cat_schema_sorted(fields)
        f = sorted_fields[idx] if idx < len(sorted_fields) else {}
        return _cat_schema_display_cell(category, idx, field, f)

    @app.get("/settings/cat-schema/{category}/{idx}/{field}/edit")
    async def cat_schema_field_edit(request: Request, category: str, idx: int, field: str):
        from urllib.parse import quote
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            fields = await api.get_category_schema(token, category)
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        sorted_fields = _load_cat_schema_sorted(fields)
        f = sorted_fields[idx] if idx < len(sorted_fields) else {}
        # Key is auto-managed - clicking the hidden key cell should never open an editor
        if field == "key":
            return _cat_schema_display_cell(category, idx, field, f)
        val = str(f.get(field, "") or "")
        enc = quote(category, safe="")
        patch_url = f"/settings/cat-schema/{enc}/{idx}/{field}"
        display_url = f"/settings/cat-schema/{enc}/{idx}/{field}/display"
        esc_js = f"if(event.key==='Escape'){{htmx.ajax('GET','{display_url}',{{target:this.closest('td'),swap:'outerHTML'}});}}"
        if field in ("required", "editable", "show_in_table"):
            return Td(
                Select(
                    Option(t("settings.yes"), value="true", selected=val.lower() == "true"),
                    Option(t("settings.no"), value="false", selected=val.lower() != "true"),
                    name="value",
                    hx_patch=patch_url,
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                    onkeydown=esc_js,
                ),
                cls="cell cell--editing",
            )
        if field == "type":
            _SCHEMA_TYPES = ["text", "number", "money", "select", "date", "boolean", "weight", "status", "image"]
            return Td(
                Select(
                    *[Option(stype, value=stype, selected=(stype == val)) for stype in _SCHEMA_TYPES],
                    name="value",
                    hx_patch=patch_url,
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                    onkeydown=esc_js,
                ),
                cls="cell cell--editing",
            )
        if field == "options":
            if f.get("type") not in _OPTIONS_TYPES:
                return _cat_schema_display_cell(category, idx, field, f)
            val = ", ".join(f.get("options", []))
        return Td(
            Input(
                type="number" if field == "position" else "text",
                name="value", value=val,
                hx_patch=patch_url,
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
                onkeydown=esc_js,
            ),
            cls="cell cell--editing",
        )

    @app.patch("/settings/cat-schema/{category}/{idx}/{field}")
    async def cat_schema_field_patch(request: Request, category: str, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        # Key is auto-managed - never directly editable
        if field == "key":
            try:
                fields = await api.get_category_schema(token, category)
                sorted_fields = _load_cat_schema_sorted(fields)
                f = sorted_fields[idx] if idx < len(sorted_fields) else {}
            except APIError:
                f = {}
            return _cat_schema_display_cell(category, idx, field, f)
        form = await request.form()
        value = str(form.get("value", ""))
        _SCHEMA_TYPES = frozenset({"text", "number", "money", "select", "date", "boolean", "weight", "status", "image"})
        if field == "type" and value not in _SCHEMA_TYPES:
            return P(f"Invalid field type: {value!r}", cls="cell-error")
        if field == "position":
            try:
                int(value)
            except (ValueError, TypeError):
                return P(t("settings.position_must_be_a_whole_number"), cls="cell-error")
        try:
            fields = await api.get_category_schema(token, category)
            sorted_fields = _load_cat_schema_sorted(fields)
            if idx < len(sorted_fields):
                if field in ("required", "editable", "show_in_table"):
                    sorted_fields[idx][field] = value.lower() in ("true", "yes", "1")
                elif field == "position":
                    sorted_fields[idx][field] = int(value)
                elif field == "options":
                    sorted_fields[idx][field] = [o.strip() for o in value.split(",") if o.strip()]
                else:
                    sorted_fields[idx][field] = value
                # When label changes, rederive key to match (excluding self from collision check)
                if field == "label":
                    other_keys = {f["key"] for j, f in enumerate(sorted_fields) if j != idx and f.get("key")}
                    sorted_fields[idx]["key"] = _derive_key(value, other_keys)
            await api.patch_category_schema(token, category, sorted_fields)
            fields = await api.get_category_schema(token, category)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        sorted_fields = _load_cat_schema_sorted(fields)
        f = sorted_fields[idx] if idx < len(sorted_fields) else {}
        return _cat_schema_display_cell(category, idx, field, f)

    @app.delete("/settings/cat-schema/{category}/{idx}")
    async def cat_schema_field_delete(request: Request, category: str, idx: int):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            fields = await api.get_category_schema(token, category)
            sorted_fields = _load_cat_schema_sorted(fields)
            if idx < len(sorted_fields):
                sorted_fields.pop(idx)
            await api.patch_category_schema(token, category, sorted_fields)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _R("", status_code=500)
        return _R("", status_code=204)

    @app.post("/settings/cat-schema/{category}/add")
    async def cat_schema_field_add(request: Request, category: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            fields = list(await api.get_category_schema(token, category))
            max_pos = max((f.get("position", 0) for f in fields), default=-1)
            existing_keys = {f["key"] for f in fields if f.get("key")}
            label = "New Field"
            key = _derive_key(label, existing_keys)
            new_field = {
                "key": key,
                "label": label,
                "type": "text",
                "required": False,
                "editable": True,
                "show_in_table": True,
                "options": [],
                "position": max_pos + 1,
            }
            fields.append(new_field)
            await api.patch_category_schema(token, category, fields)
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _R("", status_code=500)
        return _R("", status_code=204)

    # ── Module toggle endpoints ──────────────────────────────────────
    @app.post("/settings/locations/new")
    async def create_location_route(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.create_location(token, {"name": "New Location", "type": "warehouse", "address": None})
        except APIError as e:
            if e.status == 401:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            return _R("", status_code=500)
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/inventory?tab=locations"})

    @app.get("/settings/locations/{location_id}/{field}/edit")
    async def location_field_edit(request: Request, location_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            locations = (await api.get_locations(token)).get("items", [])
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        loc = next((l for l in locations if l.get("id") == location_id), {})
        val = str(loc.get(field, "") or "")

        if field == "type":
            return Td(
                Select(
                    *[Option(label, value=v, selected=(v == val)) for v, label in _LOC_TYPES],
                    name="value",
                    hx_patch=f"/settings/locations/{location_id}/{field}",
                    hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                    hx_trigger="change",
                    cls="cell-input cell-input--select", autofocus=True,
                ),
                cls="cell cell--editing",
            )

        return Td(
            Input(
                type="text", name="value", value=val,
                hx_patch=f"/settings/locations/{location_id}/{field}",
                hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                hx_trigger="blur delay:200ms",
                cls="cell-input", autofocus=True,
                onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur();}",
            ),
            cls="cell cell--editing",
        )

    @app.patch("/settings/locations/{location_id}/{field}")
    async def location_field_patch(request: Request, location_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value = str(form.get("value", ""))
        if field == "type" and value not in _LOC_TYPE_VALUES:
            return P(f"Invalid location type: {value!r}", cls="cell-error")
        patch_val: str | bool | dict = value
        if field == "is_default":
            patch_val = value.strip().lower() in {"true", "1", "yes"}
        elif field == "address":
            patch_val = {"text": value} if value.strip() else {}
        try:
            await api.patch_location(token, location_id, {field: patch_val})
            locations = (await api.get_locations(token)).get("items", [])
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        loc = next((l for l in locations if l.get("id") == location_id), {})
        return _location_display_cell(location_id, field, loc.get(field))

    @app.delete("/settings/locations/{location_id}")
    async def delete_location(request: Request, location_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.delete_location(token, location_id)
        except APIError as e:
            # Return error as plain text so HTMX can show it
            return _R(str(e.detail), status_code=e.status)
        return _R("", status_code=204, headers={"HX-Redirect": "/settings/inventory?tab=locations"})

    @app.post("/settings/company/language")
    async def company_language_post(request: Request):
        """HTMX: save company language setting and update celerp_lang cookie."""
        from starlette.responses import HTMLResponse
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        new_lang = str(form.get("language", "en")).strip()
        from pathlib import Path as _Path
        _VALID_LANGS = {p.stem for p in (_Path(__file__).parent.parent / "locales").glob("*.json")}
        if new_lang not in _VALID_LANGS:
            new_lang = "en"
        try:
            company = await api.get_company(token)
            settings_dict = dict(company.get("settings") or {})
            settings_dict["language"] = new_lang
            await api.patch_company(token, {"settings": settings_dict})
        except APIError:
            pass
        _LANGUAGES = sorted(
            [(p.stem, t(f"settings.language_{p.stem}", new_lang))
             for p in (_Path(__file__).parent.parent / "locales").glob("*.json")],
            key=lambda x: x[1],
        )
        from starlette.responses import HTMLResponse as _HR
        import fasthtml.common as _fh
        sel = Select(
            *[Option(label, value=code, selected=(code == new_lang)) for code, label in _LANGUAGES],
            name="language",
            hx_post="/settings/company/language",
            hx_target="this",
            hx_swap="outerHTML",
            hx_trigger="change",
            cls="cell-input cell-input--select",
        )
        from fasthtml.common import to_xml
        html = to_xml(sel)
        resp = _HR(content=html)
        resp.set_cookie("celerp_lang", new_lang, httponly=False, samesite="lax", max_age=86400 * 30)
        return resp

    @app.post("/settings/bulk-attach")
    async def settings_bulk_attach(request: Request):
        token = _token(request)
        if not token:
            return Div(P(t("error.unauthorized"), cls="error-banner"), id="bulk-attach-result")
        form = await request.form()
        file = form.get("file")
        if file is None:
            return Div(P(t("error.no_file"), cls="error-banner"), id="bulk-attach-result")
        try:
            override_hero = bool(form.get("override_hero"))
            result = await api.bulk_attach(token, file, override_hero=override_hero)
        except APIError as e:
            return Div(P(str(e.detail), cls="error-banner"), id="bulk-attach-result")

        report = result.get("report", [])
        n_matched = result.get("matched", 0)
        n_unmatched = result.get("unmatched", 0)
        n_errors = len(result.get("errors", []))

        def _row(r: dict) -> FT:
            status = r.get("status", "")
            cls = {"ok": "status-ok", "unmatched": "status-warn", "error": "status-error"}.get(status, "")
            hero_icon = "⭐" if r.get("is_hero") else ""
            # data-status lets the client-side filter (celerpBulkFilter) show/hide rows.
            return Tr(
                Td(r.get("sku", "")),
                Td(r.get("file", "")),
                Td(Span(status, cls=f"badge badge--{cls}") if cls else Span(status)),
                Td(r.get("tag", "") or r.get("detail", "")),
                Td(hero_icon, style="text-align:center;"),
                data_status=status or "unknown",
            )

        def _filter_btn(label: str, value: str, flash_cls: str, active: bool = False) -> FT:
            return Button(
                label,
                type="button",
                cls=f"flash {flash_cls} bulk-filter-btn{' is-active' if active else ''}",
                data_filter=value,
                onclick=f"celerpBulkFilter('{value}', this)",
            )

        return Div(
            Div(
                _filter_btn(f"All {len(report)}", "all", "flash--muted", active=True),
                _filter_btn(f"✓ {n_matched} matched", "ok", "flash--success"),
                _filter_btn(f"⚠ {n_unmatched} unmatched", "unmatched", "flash--warning") if n_unmatched else "",
                _filter_btn(f"✕ {n_errors} errors", "error", "flash--error") if n_errors else "",
                cls="bulk-result-summary",
            ),
            Table(
                Thead(Tr(Th("SKU"), Th(t("th.file")), Th(t("th.status")), Th(t("label.tag")), Th(t("th.hero"), style="text-align:center;"))),
                Tbody(*[_row(r) for r in report]),
                cls="data-table",
            ) if report else "",
            id="bulk-attach-result",
        )

    @app.post("/settings/import-history/{batch_id}/undo")
    async def undo_import_batch_route(request: Request, batch_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            result = await api.undo_import_batch(token, batch_id)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        removed = result.get("removed", 0)
        modified = result.get("modified_items", [])
        msg = f"Undone: {removed} item(s) removed."
        if modified:
            msg += f" Warning: {len(modified)} item(s) were modified since import and may need manual review."
        return _R("", status_code=204, headers={"HX-Redirect": f"/settings/inventory?tab=import-history&msg={removed}+undone"})

    # ── Cloud status HTMX fragment ───────────────────────────────────

    @app.get("/settings/cloud-connect")
    async def cloud_connect_fragment(request: Request):
        """HTMX fragment: return just the connect section (Back button target)."""
        token = _token(request)
        if not token:
            return Response(status_code=401)
        from celerp.config import ensure_instance_id
        iid = ensure_instance_id()
        return _cloud_relay_unconnected(iid)

    @app.get("/settings/cloud-status")
    async def cloud_status_fragment(request: Request):
        """HTMX fragment: render cloud connection status card."""
        import httpx
        from celerp.config import ensure_instance_id
        from ui.config import API_BASE
        token = _token(request)
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(base_url=API_BASE, timeout=3.0) as c:
                r = await c.get("/settings/cloud-status", headers=headers)
                data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        connected = data.get("connected", False)

        iid = ensure_instance_id()
        if connected:
            tier = data.get("tier") or "unknown"
            last_backup = data.get("last_backup")
            email_quota = data.get("email_quota", 0)
            email_used = data.get("email_used", 0)
            backup_text = f" - Last backup: {last_backup}" if last_backup else ""
            return Div(
                Span(t("settings._connected"), cls="text-connected"),
                Span(f" - {tier} plan{backup_text}", cls="settings-hint"),
                Br(),
                Span(f"Email: {email_used} / {email_quota} sent this period", cls="settings-hint"),
                Br(),
                A(t("settings.manage_subscription"), href="/settings/billing-portal", target="_blank", cls="auth-link"),
                cls="cloud-status-connected",
            )
        return _cloud_relay_unconnected(iid)

    @app.get("/settings/billing-portal")
    async def billing_portal_redirect(request: Request):
        """Open the Stripe Billing Portal for the Celerp subscription (cancel,
        change card, invoices). Linked from the Web Access connected-status card."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        redir = await _check_permission(request, "manage_integrations")
        if redir:
            return redir
        try:
            url = await api.get_billing_portal_url(token)
        except APIError as e:
            return await base_shell(flash(str(e.detail)), nav_active="web-access", request=request)
        return RedirectResponse(url, status_code=302)

    def _relay_base() -> str:
        from celerp.config import settings as _s
        if _s.gateway_http_url:
            return _s.gateway_http_url.rstrip("/")
        return _s.gateway_url.replace("wss://", "https://").replace("ws://", "http://").replace("/ws/connect", "")

    _RELAY_BASE = _relay_base()

    @app.post("/settings/cloud-activate")
    async def cloud_activate(request: Request):
        """HTMX: proxy to API process to call relay /auth/activate + start gateway."""
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        import ui.api_client as _api
        from celerp.config import ensure_instance_id

        ui_token = _token(request)
        # iid from UI process as fallback for error rendering only
        iid = ensure_instance_id()

        try:
            data = await _api.activate_relay(ui_token)
        except Exception as exc:
            return _cloud_relay_unconnected(iid, error=f"Could not reach API: {exc}")

        # Use instance_id from API response if present (canonical process)
        iid = data.get("instance_id") or iid

        if err := data.get("error"):
            return _cloud_relay_unconnected(iid, error=err)

        if data.get("reconnect"):
            return _cloud_reconnect_confirm(
                iid,
                data["gateway_token"],
                data.get("public_url"),
                data.get("tos_version"),
            )

        # Connected: the page chrome changes with the relay state (value-prop
        # landing vs connected tabs), so load the page fresh instead of
        # swapping only the panel.
        return Response(status_code=204, headers={"HX-Redirect": "/settings/cloud"})

    @app.get("/topbar-company-switcher")
    async def topbar_company_switcher(request: Request):
        """HTMX fragment: company switcher pill for the topbar. Renders empty when only 1 company."""
        from fasthtml.common import to_xml
        token = _token(request)
        if not token:
            return Response(content="", media_type="text/html")
        try:
            resp = await api.my_companies(token)
            companies = resp.get("items", []) if isinstance(resp, dict) else resp
        except Exception:
            return Response(content="", media_type="text/html")
        if len(companies) <= 1:
            return Response(content="", media_type="text/html")
        current = next((c.get("company_name", "") for c in companies if c.get("is_current")), companies[0].get("company_name", ""))
        options = [
            Option(c.get("company_name", ""), value=c.get("company_id", ""), selected=c.get("is_current", False))
            for c in companies
        ]
        widget = Select(
            *options,
            onchange="location='/switch-company/'+this.value",
            cls="company-switcher-select",
        )
        return Response(to_xml(widget), media_type="text/html")

    @app.get("/topbar-relay-status")
    async def topbar_relay_status(request: Request):
        """HTMX fragment: return the relay dot span for the topbar user menu."""
        import httpx
        from fasthtml.common import to_xml
        from ui.config import API_BASE
        from ui.i18n import get_lang
        token = _token(request)
        lang = get_lang(request)
        data: dict = {}
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(base_url=API_BASE, timeout=2.0) as c:
                r = await c.get("/settings/cloud-status", headers=headers)
                if r.status_code == 200:
                    data = r.json()
        except Exception:
            pass
        connected = bool(data.get("connected"))
        public_url = data.get("public_url") or ""
        dot_cls = "relay-dot relay-dot--on" if connected else "relay-dot relay-dot--off"
        if connected and public_url:
            dot_title = f"{t('msg.relay_connected', lang)}: {public_url}"
            dot_href = public_url
            dot_target = "_blank"
        else:
            dot_title = t("msg.relay_not_connected", lang)
            dot_href = "/settings/cloud"
            dot_target = "_self"
        dot = Span(
            A(Span(cls=dot_cls, title=dot_title), href=dot_href, target=dot_target,
              cls="relay-dot-link", onclick="event.stopPropagation()"),
            id="relay-dot-wrap",
        )
        return Response(to_xml(dot), media_type="text/html")

    def _cloud_reconnect_confirm(
        iid: str, token: str, public_url: str | None, tos_version: str | None
    ) -> FT:
        """Shown when activate returns reconnect=True.

        Lets the user confirm reconnecting or start the claim flow to pick a
        different account. The relay returns a public URL exactly when the
        tier is entitled to one, so no public URL means a free account: those
        get sign-back-in wording, never subscription wording.
        """
        if public_url:
            prompt = P(t("settings.this_instance_was_previously_connected_to"),
                       B(public_url),
                       t("settings.reconnect_to_same_subscription"),
                       cls="settings-hint", style="margin-bottom:12px;")
            confirm_label = t("btn.reconnect_to") + public_url
            other_label = t("btn.use_a_different_subscription")
        else:
            prompt = P(t("settings.previously_signed_in_free"),
                       cls="settings-hint", style="margin-bottom:12px;")
            confirm_label = t("btn.sign_back_in")
            other_label = t("btn.use_a_different_account")
        return Div(
            H3(t("settings.tab_cloud_relay"), cls="settings-section-title"),
            prompt,
            Div(
                Form(
                    Input(type="hidden", name="_reconnect_token", value=token),
                    Input(type="hidden", name="_reconnect_public_url", value=public_url or ""),
                    Input(type="hidden", name="_reconnect_tos_version", value=tos_version or ""),
                    Button(confirm_label, type="submit", cls="btn btn--primary"),
                    hx_post="/settings/cloud-reconnect-confirm",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                ),
                Button(other_label,
                    cls="btn btn--outline",
                    style="margin-left:8px;",
                    hx_post="/settings/cloud-disconnect",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                ),
                style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;",
            ),
            id="cloud-relay-tab",
            cls="settings-card",
        )

    @app.post("/settings/cloud-reconnect-confirm")
    async def cloud_reconnect_confirm(request: Request):
        """HTMX: apply a previously-retrieved gateway token via API (reconnect confirmation)."""
        import ui.api_client as _api
        from celerp.config import ensure_instance_id
        form = await request.form()
        gw_token = str(form.get("_reconnect_token", "")).strip()
        public_url = str(form.get("_reconnect_public_url", "")).strip() or None
        tos_version = str(form.get("_reconnect_tos_version", "")).strip() or None
        iid = ensure_instance_id()
        if not gw_token:
            return _cloud_relay_unconnected(iid, error="Reconnect token missing. Please try again.")
        ui_token = _token(request)
        try:
            data = await _api.apply_relay_token(ui_token, {"gateway_token": gw_token, "public_url": public_url, "tos_version": tos_version})
        except Exception as exc:
            return _cloud_relay_unconnected(iid, error=f"Could not reach API: {exc}")
        if err := data.get("error"):
            return _cloud_relay_unconnected(iid, error=err)
        # Same as cloud_activate: connecting changes the whole page, reload it.
        return Response(status_code=204, headers={"HX-Redirect": "/settings/cloud"})

    def _cloud_claim_selection(matches: list[dict], email: str, iid: str, otp_code: str | None = None) -> FT:
        """Render the subscription selection UI when multiple subs match an email.

        Replaces the entire #cloud-relay-tab (same swap target as cloud_claim).
        Each radio option shows the slug-based URL (if linked) or 'Not yet linked',
        plus tier and status. Confirm re-POSTs to /settings/cloud-claim with the
        chosen subscription_id and the original email.
        """
        def _match_row(m: dict, idx: int) -> FT:
            slug = m.get("slug")
            sub_id = m["subscription_id"]
            tier = tier_label(m.get("tier", ""))
            status = m.get("status", "")
            if slug:
                primary = f"{slug}.celerp.com"
                secondary = "Previously linked to an installation"
            else:
                primary = "Not yet linked"
                secondary = "No installation connected yet"
            return Label(
                Input(
                    type="radio",
                    name="subscription_id",
                    value=sub_id,
                    required=True,
                    checked=(idx == 0),
                    style="margin-right:10px;flex-shrink:0;",
                ),
                Div(
                    Div(
                        Span(primary, style="font-weight:500;"),
                        Span(f"{tier} · {status.title()}", cls="text-muted", style="margin-left:10px;font-size:0.85em;"),
                        style="display:flex;align-items:baseline;gap:4px;flex-wrap:wrap;",
                    ),
                    Span(secondary, cls="settings-hint", style="font-size:0.82em;"),
                    style="display:flex;flex-direction:column;gap:2px;",
                ),
                style=(
                    "display:flex;align-items:flex-start;gap:0;padding:10px 12px;"
                    "border:1px solid var(--c-border);border-radius:6px;cursor:pointer;"
                    + ("background:var(--c-bg-alt);" if idx == 0 else "")
                ),
            )

        radio_rows = [_match_row(m, i) for i, m in enumerate(matches)]

        form_content = Form(
            Input(type="hidden", name="claim_email", value=email),
            *([] if otp_code is None else [Input(type="hidden", name="otp_code", value=otp_code)]),
            P(t("settings.multiple_subscriptions_are_associated_with_that_em"),
                cls="settings-hint",
                style="margin:0 0 12px;",
            ),
            Div(*radio_rows, style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px;"),
            Div(
                Button(t("btn.connect_to_cloud"), type="submit", cls="btn btn--sm btn--primary"),
                Button(t("btn.back_to_settings"),
                    type="button",
                    cls="btn btn--sm btn--outline",
                    hx_get="/settings/cloud-connect",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                ),
                style="display:flex;gap:8px;align-items:center;",
            ),
            hx_post="/settings/cloud-claim",
            hx_target="#cloud-relay-tab",
            hx_swap="outerHTML",
        )

        return Div(
            H3(t("settings.tab_cloud_relay"), style="margin:0 0 16px;"),
            form_content,
            id="cloud-relay-tab",
            cls="settings-tab-content",
        )

    def _cloud_claim_otp_form(email: str, iid: str, error: str | None = None) -> FT:
        """Render the OTP entry step after sending a verification code."""
        children: list = [
            H3(t("page.check_your_email"), cls="settings-section-title"),
            P(
                f"We sent a 6-digit code to {email}. Enter it below:",
                cls="settings-hint",
                style="margin-bottom:12px;",
            ),
        ]
        if error:
            children.append(P(error, cls="text-error", style="margin:0 0 10px;"))

        children += [
            Form(
                Input(type="hidden", name="claim_email", value=email),
                Div(
                    Input(
                        type="text",
                        inputmode="numeric",
                        pattern="[0-9]{6}",
                        maxlength="6",
                        name="otp_code",
                        placeholder="000000",
                        required=True,
                        autofocus=True,
                        cls="input input--sm",
                        style="width:120px;letter-spacing:4px;font-size:1.1em;",
                    ),
                    Button(t("btn.verify_connect"), type="submit", cls="btn btn--sm btn--primary", style="margin-left:8px;"),
                    style="display:flex;align-items:center;",
                ),
                hx_post="/settings/cloud-claim",
                hx_target="#cloud-relay-tab",
                hx_swap="outerHTML",
                style="margin-bottom:12px;",
            ),
            Div(
                Button(t("btn.resend_code"),
                    type="button",
                    cls="btn btn--sm btn--outline",
                    hx_post="/settings/cloud-send-otp",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                    hx_vals=hx_vals({"claim_email": email}),
                ),
                Button(t("btn.back_to_settings"),
                    type="button",
                    cls="btn btn--sm btn--outline",
                    hx_get="/settings/cloud-connect",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                    style="margin-left:8px;",
                ),
                style="display:flex;align-items:center;",
            ),
        ]
        return Div(*children, id="cloud-relay-tab", cls="settings-card")

    @app.post("/settings/cloud-send-otp")
    async def cloud_send_otp(request: Request):
        """HTMX: send OTP via API process (uses canonical instance_id)."""
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        import ui.api_client as _api
        form = await request.form()
        email = str(form.get("claim_email", "")).strip()
        ui_token = _token(request)

        if not email:
            from celerp.config import ensure_instance_id
            return _cloud_relay_unconnected(ensure_instance_id(), error="Please enter an email address.")

        try:
            data = await _api.send_otp(ui_token, email)
        except Exception as exc:
            from celerp.config import ensure_instance_id
            return _cloud_relay_unconnected(ensure_instance_id(), error=f"Could not reach API: {exc}")

        iid = data.get("instance_id", "")
        if err := data.get("error"):
            return _cloud_relay_unconnected(iid, error=err)

        return _cloud_claim_otp_form(email, iid)

    @app.post("/settings/cloud-claim")
    async def cloud_claim(request: Request):
        """HTMX: verify OTP + link subscription + activate - all via API process.

        Using the API process ensures the same instance_id is used for both
        the /billing/claim relay call and the subsequent /auth/activate call.
        """
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        import ui.api_client as _api
        form = await request.form()
        email = str(form.get("claim_email", "")).strip()
        subscription_id = str(form.get("subscription_id", "")).strip() or None
        otp_code = str(form.get("otp_code", "")).strip() or None
        ui_token = _token(request)

        if not email:
            from celerp.config import ensure_instance_id
            return _cloud_relay_unconnected(ensure_instance_id(), error="Please enter an email address.")

        claim_payload: dict = {"email": email}
        if subscription_id:
            claim_payload["subscription_id"] = subscription_id
        if otp_code:
            claim_payload["otp_code"] = otp_code

        try:
            data = await _api.cloud_claim(ui_token, claim_payload)
        except Exception as exc:
            from celerp.config import ensure_instance_id
            return _cloud_relay_unconnected(ensure_instance_id(), error=f"Could not reach API: {exc}")

        iid = data.get("instance_id", "")

        if data.get("otp_error"):
            code = data.get("code", "otp_invalid")
            attempts_left = data.get("attempts_left", 0)
            if code == "otp_invalid":
                return _cloud_claim_otp_form(
                    email, iid,
                    error=f"Incorrect code. {attempts_left} attempt{'s' if attempts_left != 1 else ''} left.",
                )
            return _cloud_relay_unconnected(iid, error="Code expired or too many wrong attempts. Request a new code.")

        if data.get("otp_required"):
            return _cloud_claim_otp_form(email, iid)

        if data.get("requires_selection"):
            return _cloud_claim_selection(data["matches"], email, iid, otp_code=otp_code)

        if err := data.get("error"):
            return _cloud_relay_unconnected(iid, error=err)

        if data.get("connected"):
            # Same as cloud_activate: connecting changes the whole page, reload it.
            return Response(status_code=204, headers={"HX-Redirect": "/settings/cloud"})

        # Claim succeeded but activate pending (rare: relay linkage happened but WS not up yet)
        return _cloud_relay_unconnected(
            iid,
            error=None,
            info="Subscription linked. Click Connect to finish activating.",
            show_email_form=False,
        )

    @app.post("/settings/cloud-disconnect")
    async def cloud_disconnect(request: Request):
        """HTMX: stop gateway WS and clear credentials. Shows subscribe/claim UI."""
        import ui.api_client as _api
        from celerp.config import ensure_instance_id
        token = _token(request)
        try:
            await _api.disconnect_relay(token)
        except Exception:
            pass
        iid = ensure_instance_id()
        # Sticky disconnect: the credential is preserved (one-click reconnect via
        # the Connect button), but the fragment must not auto-fire Connect and
        # undo the disconnect the user just chose.
        return _cloud_relay_unconnected(iid, suppress_autoconnect=True)

    @app.post("/settings/cloud-accept-tos")
    async def cloud_accept_tos(request: Request):
        """HTMX: record TOS acceptance via API, reconnect gateway, re-render tab."""
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        import ui.api_client as _api
        from celerp.config import ensure_instance_id
        ui_token = _token(request)
        iid = ensure_instance_id()
        try:
            data = await _api.accept_relay_tos(ui_token)
        except Exception as exc:
            return _cloud_relay_unconnected(iid, error=f"Could not reach API: {exc}")
        return _cloud_relay_tab(
            relay_status=data.get("relay_status", "connecting"),
            public_url=data.get("public_url", ""),
        )

    @app.get("/settings/email-status")
    async def email_status_fragment(request: Request):
        """HTMX fragment: render email warning banner if not configured."""
        import httpx
        from ui.config import API_BASE
        token = _token(request)
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(base_url=API_BASE, timeout=3.0) as c:
                r = await c.get("/settings/email-status", headers=headers)
                data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        smtp_configured = data.get("smtp_configured", False)
        gateway_connected = data.get("gateway_connected", False)
        if not smtp_configured and not gateway_connected:
            return Div(t("settings._email_notifications_are_disabled"),
                A(t("settings.connect_to_celerp_cloud"), href="https://celerp.com/pricing", target="_blank"),
                " or configure SMTP to send invoices and alerts.",
                id="email-warning-banner",
                cls="flash flash--warning flex-row gap-xs",
            )
        return Div(id="email-warning-banner")  # empty - no banner needed

    # ── Verticals / Category Library endpoints ───────────────────────

    @app.post("/settings/verticals/apply-preset")
    async def verticals_apply_preset(request: Request):
        """HTMX: apply a vertical preset (seeds category schemas)."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        vertical = str(form.get("vertical", "")).strip()
        if not vertical:
            return P(t("settings.no_vertical_specified"), cls="error-banner")
        try:
            result = await api.apply_vertical_preset(token, vertical)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        n = result.get("categories", 0)
        return Div(
            Span(f"✓ Applied '{result.get('applied', vertical)}' - {n} categor{'y' if n == 1 else 'ies'} seeded",
                 cls="flash flash--success"),
            id="verticals-apply-result",
        )

    @app.post("/settings/verticals/apply-category")
    async def verticals_apply_category(request: Request):
        """HTMX: apply a single category schema."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            return P(t("settings.no_category_specified"), cls="error-banner")
        try:
            result = await api.apply_vertical_category(token, name)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return Div(
            Span(f"✓ '{result.get('display_name', name)}' added to your schema",
                 cls="flash flash--success"),
            id="verticals-apply-result",
        )

    # ── Category CRUD UI routes ─────────────────────────────────────────────

    async def _applied_panel_html(token: str) -> FT:
        """Re-render the applied-schemas panel for HTMX swap."""
        try:
            cat_schemas = await api.get_company_category_schemas(token)
        except Exception:
            cat_schemas = {}
        applied_names = sorted(cat_schemas.keys())
        return _verticals_applied_panel(applied_names)

    @app.post("/settings/categories")
    async def settings_category_create(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        name = str(form.get("new_category_name", "")).strip()
        if not name:
            return P(t("settings.new_category_name") + " is required", cls="error-banner")
        try:
            await api.create_category(token, name)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        return Response("", status_code=204, headers={"HX-Redirect": "/settings/inventory?tab=categories"})

    @app.delete("/settings/categories/{category_key}")
    async def settings_category_delete(request: Request, category_key: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.delete_category(token, category_key)
        except APIError as e:
            detail = e.detail
            if isinstance(detail, dict):
                item_count = detail.get("item_count", "?")
                msg = f"Can't delete - {item_count} item(s) use this category."
            else:
                msg = str(detail)
            return Tr(
                Td(category_key, cls="cell"),
                Td(
                    A(t("settings.edit"), href=f"/settings/inventory?tab=category-library&cat={category_key}", cls="auth-link"),
                    cls="cell",
                ),
                Td(
                    Button("✕", cls="btn btn--danger btn--xs",
                           hx_delete=f"/settings/categories/{category_key}",
                           hx_target="closest tr",
                           hx_swap="outerHTML"),
                    Span(msg, cls="error-inline"),
                    cls="cell",
                ),
                cls="data-row",
            )
        return Response("", status_code=200)

    @app.get("/settings/categories/{category_key}/edit")
    async def settings_category_rename_form(request: Request, category_key: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        return Tr(
            Td(
                Form(
                    Input(type="text", name="new_name", value=category_key,
                          cls="form-input form-input--sm",
                          autofocus=True),
                    Button(t("btn.save"), type="submit", cls="btn btn--primary btn--xs"),
                    Button(t("btn.cancel"), type="button", cls="btn btn--secondary btn--xs",
                           hx_get=f"/settings/categories/{category_key}/cancel",
                           hx_target="closest tr",
                           hx_swap="outerHTML"),
                    hx_patch=f"/settings/categories/{category_key}",
                    hx_target="closest tr",
                    hx_swap="outerHTML",
                ),
                cls="cell",
            ),
            Td("", cls="cell"),
            Td("", cls="cell"),
            cls="data-row",
        )

    @app.get("/settings/categories/{category_key}/cancel")
    async def settings_category_rename_cancel(request: Request, category_key: str):
        token = _token(request)
        display_name = category_key
        if token:
            try:
                dn_map = await api.get_category_display_names(token)
                display_name = dn_map.get(category_key, category_key)
            except Exception:
                pass
        schemas: dict = {}
        if token:
            try:
                schemas = await api.get_company_category_schemas(token)
            except Exception:
                pass
        return _category_row(category_key, display_name, len(schemas.get(category_key, [])))

    @app.patch("/settings/categories/{category_key}")
    async def settings_category_rename(request: Request, category_key: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        if not new_name:
            return P(t("settings.new_category_name") + " is required", cls="error-banner")
        try:
            await api.rename_category(token, category_key, new_name)
        except APIError as e:
            return P(str(e.detail), cls="error-banner")
        import re as _re
        new_key = _re.sub(r"[^a-z0-9]+", "_", new_name.lower()).strip("_")
        schemas: dict = {}
        try:
            schemas = await api.get_company_category_schemas(token)
        except Exception:
            pass
        return _category_row(new_key, new_name, len(schemas.get(new_key, [])))

    @app.post("/settings/factory-reset")
    async def factory_reset_ui(request: Request):
        """Proxy factory-reset to the API. Owner only."""
        import httpx
        role = _get_role(request)
        from celerp.services.permissions import role_has_permission
        if not role_has_permission({}, role, "manage_company_lifecycle"):
            return Div("Owner role required.", cls="flash flash--error")
        from ui.config import API_BASE, COOKIE_NAME, REFRESH_COOKIE_NAME
        token = _token(request)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as c:
                r = await c.post("/system/factory-reset", headers=headers)
            if r.status_code != 200:
                detail = r.json().get("detail", "Reset failed.") if r.headers.get("content-type", "").startswith("application/json") else "Reset failed."
                return Div(detail, cls="flash flash--error")
        except Exception as exc:
            return Div(f"Error: {exc}", cls="flash flash--error")
        from starlette.responses import Response as _Resp
        from celerp.config import settings as _celerp_settings
        _secure = getattr(_celerp_settings, "cookie_secure", False)
        resp = _Resp(status_code=200, content='{"ok":true}', media_type="application/json")
        resp.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", secure=_secure)
        resp.delete_cookie(REFRESH_COOKIE_NAME, httponly=True, samesite="lax", secure=_secure)
        resp.headers["HX-Redirect"] = "/setup"
        return resp

    @app.delete("/settings/company/deactivate")
    async def deactivate_company_ui(request: Request):
        """Deactivate the current company and redirect to login. Owner only."""
        import httpx
        role = _get_role(request)
        from celerp.services.permissions import role_has_permission
        if not role_has_permission({}, role, "manage_company_lifecycle"):
            return Div("Only the company owner can deactivate this company.", cls="flash flash--error")
        from ui.config import API_BASE
        token = _token(request)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as c:
                r = await c.delete("/companies/me", headers=headers)
            if r.status_code != 200:
                return Div(r.json().get("detail", "Deactivation failed."), cls="flash flash--error")
        except Exception as exc:
            return Div(f"Error: {exc}", cls="flash flash--error")
        from starlette.responses import RedirectResponse
        # Check if the user has other active companies
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as c:
                r2 = await c.get("/auth/my-companies", headers=headers)
            remaining = r2.json().get("total", 0) if r2.status_code == 200 else 0
        except Exception:
            remaining = 0
        if remaining == 0:
            # No companies left - keep token so they can create a new one
            return RedirectResponse(url="/setup/new-company?reason=deactivated", status_code=303)
        # Other companies exist - clear session, go to login to pick one
        resp = RedirectResponse(url="/login?deactivated=1", status_code=303)
        resp.delete_cookie("token")
        return resp

    # ── Backup HTMX handlers ──────────────────────────────────────────────
    # These forward to the API process via api_client (standard auth pattern).
    # The HTMX targets on the backup tab (/backup/list, /backup/trigger,
    # /backup/export, /backup/import) point here; we call the API with the
    # user's JWT just like every other settings route handler.
    #
    # Error handling rule: HTMX requests must ALWAYS return an HTML fragment
    # (never a redirect). Any 401 from the relay means "cloud not connected"
    # — show an error fragment. Only redirect to /login when the user's own
    # token is missing.

    def _backup_error(detail: str, lang: str = "en", flash_id: str | None = None) -> Response:
        from fasthtml.common import Div, to_xml
        kwargs = {"cls": "empty-state-msg"}
        if flash_id:
            kwargs["id"] = flash_id
        return Response(
            content=to_xml(Div(detail, **kwargs)),
            media_type="text/html",
        )

    @app.get("/backup/active")
    async def backup_active(request: Request):
        """Lightweight poll for the global 'backup in progress' banner (#161).

        Returns {"active": bool} — true while a snapshot is building (writes paused)."""
        from starlette.responses import JSONResponse
        import ui.api_client as _api
        token = _token(request)
        if not token:
            return JSONResponse({"active": False})
        try:
            status = await _api.get_backup_status(token)
            return JSONResponse({"active": bool(status.get("active"))})
        except Exception:
            return JSONResponse({"active": False})

    @app.get("/backup/list")
    async def backup_list(request: Request):
        """HTMX fragment: list cloud snapshots (database + files)."""
        import ui.api_client as _api
        from fasthtml.common import Div, to_xml
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            data = await _api.list_backups(token)
        except _api.APIError as exc:
            # Any error (including relay 401) renders as a fragment — never redirect.
            msg = exc.detail if exc.detail and exc.status_code != 401 else t("settings.cloud_not_connected", lang)
            return _backup_error(msg, lang)
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return Response(
                content=to_xml(Div(t("settings.no_backups_yet", lang), cls="empty-state-msg")),
                media_type="text/html",
            )
        from fasthtml.common import A, Button, Div, Table, Thead, Tbody, Tr, Th, Td
        def _fmt(iso: str | None) -> str:
            if not iso:
                return "-"
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
                return dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                return iso
        rows = []
        for item in items:
            sz = item.get("size_bytes") or 0
            mb = f"{sz / 1024**2:.1f} MB" if sz else "-"
            bid = item.get("id", "")
            rows.append(Tr(
                Td(_fmt(item.get("created_at")), cls="cell"),
                Td(mb, cls="cell cell--number"),
                Td(
                    A(t("btn.export"), href=f"/backup/export/{bid}",
                      cls="btn btn--xs btn--secondary"),
                    Button(t("btn.restore"),
                        hx_post=f"/backup/restore/{bid}",
                        hx_confirm="This restores the database AND files, replacing current data. Continue?",
                        hx_target="#backup-flash", hx_swap="outerHTML",
                        cls="btn btn--xs btn--outline btn--danger ml-sm"),
                    cls="cell",
                ),
            ))
        table = Table(
            Thead(Tr(Th(t("th.date")), Th(t("th.size")), Th(t("th.actions")))),
            Tbody(*rows),
            cls="data-table",
        )
        return Response(content=to_xml(table), media_type="text/html")

    @app.post("/backup/trigger")
    async def backup_trigger(request: Request):
        """Trigger an immediate cloud snapshot (database + files)."""
        import ui.api_client as _api
        from fasthtml.common import Div, to_xml
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            await _api.trigger_backup(token)
        except _api.APIError as exc:
            # 422 = backup ran but failed (pg_dump error, relay error, etc.) — show real message
            # Other codes = cloud not connected or auth failure
            msg = exc.detail if exc.status == 422 else t("settings.cloud_not_connected", lang)
            return _backup_error(msg, lang, flash_id="backup-flash")
        except Exception:
            return _backup_error(t("settings.cloud_not_connected", lang), lang, flash_id="backup-flash")
        msg = t("settings.backup_triggered", lang)
        resp = Response(
            content=to_xml(Div(msg, cls="flash flash--success", id="backup-flash")),
            media_type="text/html",
        )
        resp.headers["HX-Trigger"] = "backupDone"
        return resp

    @app.get("/backup/export")
    async def backup_export(request: Request):
        """Stream the full local backup archive (.celerp-backup) to the browser.

        Streamed (not buffered) so a multi-GB backup never sits in UI memory and isn't
        bound by the short default timeout; forwarding Content-Length gives the browser a
        native download progress bar."""
        import ui.api_client as _api
        from starlette.responses import StreamingResponse
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            stream, headers = await _api.export_backup(token)
        except _api.APIError as exc:
            from fasthtml.common import Div, to_xml
            return Response(
                content=to_xml(Div(str(exc.detail), cls="flash flash--error")),
                media_type="text/html",
            )
        return StreamingResponse(
            stream,
            media_type=headers.get("content-type", "application/octet-stream"),
            headers={k.title(): v for k, v in headers.items() if k != "content-type"},
        )

    @app.get("/backup/export/{backup_id}")
    async def backup_export_cloud(request: Request, backup_id: str):
        """Stream a cloud snapshot (rebuilt as .celerp-backup) to the browser.

        Streamed (not buffered) so a multi-GB snapshot never sits in UI memory; the
        browser's native download manager shows progress via the forwarded Content-Length."""
        import ui.api_client as _api
        from starlette.responses import StreamingResponse
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            stream, headers = await _api.export_backup(token, backup_id)
        except _api.APIError as exc:
            from fasthtml.common import Div, to_xml
            return Response(
                content=to_xml(Div(str(exc.detail), cls="flash flash--error")),
                media_type="text/html",
            )
        return StreamingResponse(
            stream,
            media_type=headers.get("content-type", "application/octet-stream"),
            headers={k.title(): v for k, v in headers.items() if k != "content-type"},
        )

    @app.post("/backup/restart-app")
    async def backup_restart_app(request: Request):
        """Restart the servers after a restore, then reload once they are back.

        Only ever rendered as the continuation of a just-completed restore flash;
        there is no standing restart control anywhere in the UI.
        """
        from starlette.responses import Response as _R
        from ui.components.shell import RESTART_POLL_JS
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            await api.restart_system(token)
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="backup-flash")
        return Div(
            Div(t("settings.restarting_the_application"), cls="flash flash--warning"),
            Script(RESTART_POLL_JS),
            id="backup-flash",
        )

    @app.post("/backup/import")
    async def backup_import(request: Request):
        """Import a .celerp-backup archive. Multipart upload forwarded to API."""

        import ui.api_client as _api
        import httpx
        from fasthtml.common import Div, to_xml
        from ui.config import API_BASE
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        file_field = form.get("file")
        if file_field is None:
            return Response(
                content=to_xml(Div(t("msg.no_file_selected", lang), cls="flash flash--error", id="backup-flash")),
                media_type="text/html",
            )
        # Stream the upload to a temp file so a multi-GB archive never sits in UI memory,
        # then forward it to the API as a streamed multipart file handle.
        import asyncio
        import shutil
        import tempfile
        from pathlib import Path as _Path
        tmp = tempfile.NamedTemporaryFile(suffix=".celerp-backup", delete=False)
        try:
            await asyncio.to_thread(shutil.copyfileobj, file_field.file, tmp)
            tmp.close()
            try:
                async with httpx.AsyncClient(base_url=API_BASE, headers={"Authorization": f"Bearer {token}"}, timeout=300) as c:
                    with open(tmp.name, "rb") as fh:
                        r = await c.post("/backup/import", files={"file": (file_field.filename, fh, file_field.content_type or "application/octet-stream")})
            finally:
                _Path(tmp.name).unlink(missing_ok=True)
            if r.status_code >= 400:
                detail = r.json().get("detail", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
                return Response(
                    content=to_xml(Div(detail, cls="flash flash--error", id="backup-flash")),
                    media_type="text/html",
                )
        except httpx.HTTPError as exc:
            return Response(
                content=to_xml(Div(str(exc), cls="flash flash--error", id="backup-flash")),
                media_type="text/html",
            )
        # The API returns a flash (success OR "Import failed: …") at HTTP 200 for the HTMX
        # swap. Relay it verbatim instead of assuming success — otherwise a failed restore
        # (e.g. a pg_restore version mismatch) is reported to the user as "imported".
        return Response(content=r.text, media_type="text/html")


# ── Display cell helpers (click-to-edit pattern) ─────────────────────────

def _preference_display_cell(key: str, value, lang: str = "en") -> FT:
    label_map = {
        "docs_default_preset": {
            "last_12m": t("filter.last_12m", lang),
            "this_year": "This calendar year",
            "all": t("filter.all_time", lang),
        },
        "default_per_page": {
            "25": "25 per page",
            "50": "50 per page",
            "100": "100 per page",
            "250": "250 per page",
            "500": "500 per page",
        },
    }.get(key, {})
    display = label_map.get(str(value or ""), str(value) if value else EMPTY)
    return Td(
        Span(display, cls="cell-text"),
        title="Click to change",
        hx_get=f"/settings/preferences/{key}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _company_display_cell(field: str, value) -> FT:
    raw = str(value) if value and str(value).strip() else ""
    if field == "currency" and raw:
        # Show "USD – US Dollar" if we know it, else just the code
        display = currency_label(raw)
    elif field == "fiscal_year_start" and raw:
        label_map = {v: label for v, label in _FISCAL_MONTHS}
        display = label_map.get(raw, raw)
    else:
        display = raw or EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/company/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _user_display_cell(user_id: str, field: str, value) -> FT:
    if field == "role":
        inner = Span(str(value or ""), cls=f"badge badge--{str(value or '').lower()}")
    elif field == "is_active":
        is_active = value if isinstance(value, bool) else str(value).lower() == "true"
        inner = Span("Active" if is_active else "Inactive",
                     cls="badge badge--active" if is_active else "badge badge--inactive")
    else:
        inner = Span(str(value) if value and str(value).strip() else EMPTY, cls="cell-text")
    return Td(
        inner,
        title="Click to edit",
        hx_get=f"/settings/users/{user_id}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _tax_display_cell(idx: int, field: str, tax: dict, prefix: str = "taxes") -> FT:
    val = tax.get(field, "")
    if field == "rate":
        display = f"{val}%" if val else EMPTY
    elif field == "is_default":
        display = "Yes" if val else "No"
    else:
        display = str(val) if val and str(val).strip() else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/{prefix}/{idx}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _term_display_cell(idx: int, field: str, term: dict, prefix: str = "terms") -> FT:
    val = term.get(field, "")
    display = str(val) if val is not None and str(val).strip() else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/{prefix}/{idx}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _price_list_display_cell(idx: int, field: str, pl: dict, prefix: str = "price-lists") -> FT:
    val = pl.get(field, "")
    display = str(val) if val and str(val).strip() else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/{prefix}/{idx}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


def _schema_display_cell(idx: int, field: str, f: dict) -> FT:
    if field == "options":
        val = ", ".join(f.get("options", []))
    elif field in ("required", "editable"):
        val = "Yes" if f.get(field) else "No"
    else:
        val = f.get(field, "")
    display = str(val) if val and str(val).strip() else EMPTY
    cls_extra = " cell--mono" if field == "key" else ""
    return Td(
        Span(display, cls=f"cell-text{cls_extra}"),
        title="Click to edit",
        hx_get=f"/settings/schema/{idx}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )


_OPTIONS_TYPES: frozenset[str] = frozenset({"select", "status"})
_BOOL_FIELDS: frozenset[str] = frozenset({"required", "editable", "show_in_table"})
_OPTIONS_PILL_LIMIT = 4


def _derive_key(label: str, existing_keys: set[str], exclude_idx: int | None = None) -> str:
    """Derive a unique snake_case key from a label.

    Lowercases, replaces spaces and hyphens with underscores, strips non-alphanumeric chars.
    Appends _2, _3 etc. to resolve collisions with existing_keys.
    exclude_idx is unused (collision check is against the passed set, caller excludes self).
    """
    import re
    base = re.sub(r"[^a-z0-9_]", "", label.lower().replace(" ", "_").replace("-", "_")).strip("_") or "field"
    key = base
    n = 2
    while key in existing_keys:
        key = f"{base}_{n}"
        n += 1
    return key


def _cat_schema_display_cell(category: str, idx: int, field: str, f: dict) -> FT:
    """Editable cell for a category attribute schema field."""
    from urllib.parse import quote
    enc = quote(category, safe="")

    # Boolean fields: ✓ / – icons
    if field in _BOOL_FIELDS:
        checked = bool(f.get(field))
        icon = Span("✓", cls="cell-bool-check") if checked else Span("–", cls="cell-bool-dash")
        return Td(
            icon,
            title="Click to edit",
            hx_get=f"/settings/cat-schema/{enc}/{idx}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            cls="cell cell--clickable",
            style="text-align:center",
        )

    # Label field: shown as "Name" to the user - key is fully internal
    if field == "label":
        label_val = str(f.get("label", "") or "")
        display = label_val if label_val.strip() else EMPTY
        return Td(
            Span(display, cls="cell-text"),
            title="Click to edit",
            hx_get=f"/settings/cat-schema/{enc}/{idx}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            cls="cell cell--clickable",
        )

    # Key field: hidden - merged into label cell
    if field == "key":
        return Td(hidden=True, style="display:none")

    # Options field: pill list with overflow
    if field == "options":
        opts: list[str] = f.get("options", []) or []
        if f.get("type") not in _OPTIONS_TYPES:
            return Td(
                Span("–", cls="cell-bool-dash"),
                title="Options only apply to select/status fields",
                cls="cell",
                style="text-align:center",
            )
        if not opts:
            display_content: Any = Span(EMPTY, cls="cell-text text-muted")
        else:
            visible = opts[:_OPTIONS_PILL_LIMIT]
            overflow = len(opts) - _OPTIONS_PILL_LIMIT
            pills: list[Any] = [Span(o, cls="attr-pill") for o in visible]
            if overflow > 0:
                pills.append(Span(f"+{overflow} more", cls="attr-pill attr-pill--more"))
            display_content = Span(*pills)
        return Td(
            display_content,
            title="Click to edit",
            hx_get=f"/settings/cat-schema/{enc}/{idx}/{field}/edit",
            hx_target="this", hx_swap="outerHTML", hx_trigger="click",
            cls="cell cell--clickable",
        )

    # Default: plain text
    val = f.get(field, "")
    display = str(val) if val and str(val).strip() else EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/cat-schema/{enc}/{idx}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )

# Location type options: single source of truth.
# "address" = company From-address (created via Settings > General > Addresses).
_LOC_TYPES: list[tuple[str, str]] = [
    ("warehouse", "Warehouse"),
    ("store", "Store"),
    ("office", "Office"),
    ("virtual", "Virtual"),
    ("address", "Company Address"),
]
_LOC_TYPE_VALUES: frozenset[str] = frozenset(v for v, _ in _LOC_TYPES)
_LOC_TYPE_LABELS: dict[str, str] = {v: lbl for v, lbl in _LOC_TYPES}


def _location_display_cell(location_id: str, field: str, value) -> FT:
    if field == "address":
        display = unwrap_address(value)
    elif field == "type":
        display = _LOC_TYPE_LABELS.get(str(value), str(value)) if value else EMPTY
    else:
        display = str(value) if value and str(value).strip() else EMPTY
    if not display:
        display = EMPTY
    return Td(
        Span(display, cls="cell-text"),
        title="Click to edit",
        hx_get=f"/settings/locations/{location_id}/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="click",
        cls="cell cell--clickable",
    )



def _password_form(error: str = "", success: str = "", lang: str = "en") -> FT:
    """Password change form accessible by all authenticated users."""
    notice = ""
    if error:
        notice = P(error, cls="text-danger", style="margin-bottom:12px;")
    elif success:
        notice = P(success, cls="text-success", style="margin-bottom:12px;")

    return Div(
        notice,
        Form(
            Div(
                Label(t("settings.current_password", lang), fr="current_password"),
                Input(type="password", name="current_password", id="current_password",
                      required=True, cls="input", autocomplete="current-password"),
                cls="form-group",
            ),
            Div(
                Label(t("settings.new_password", lang), fr="new_password"),
                Input(type="password", name="new_password", id="new_password",
                      required=True, minlength="8", cls="input", autocomplete="new-password"),
                cls="form-group",
            ),
            Div(
                Label(t("settings.confirm_password", lang), fr="confirm_password"),
                Input(type="password", name="confirm_password", id="confirm_password",
                      required=True, minlength="8", cls="input", autocomplete="new-password"),
                cls="form-group",
            ),
            Button(t("btn.change_password", lang), type="submit", cls="btn btn--primary mt-sm"),
            action="/settings/password",
            method="post",
            hx_post="/settings/password",
            hx_target="#password-form",
            hx_swap="outerHTML",
            style="max-width:400px;",
        ),
        id="password-form",
    )


def _company_settings_card(company: dict, lang: str = "en") -> FT:
    """The company's regional settings (Currency / Timezone / Fiscal Year Start), edited inline via the
    existing /settings/company/{field} routes. Sits to the right of the Contact Info card on Company
    Details, mirroring the customer/vendor settings card. Language is omitted - it is set from the header
    language switcher, so duplicating it here would be cruft."""
    fields = [
        ("currency", t("label.currency", lang)),
        ("timezone", t("label.timezone", lang)),
        ("fiscal_year_start", t("label.fiscal_year_start", lang)),
    ]
    flat = {**company, **(company.get("settings") or {})}
    return Div(
        H3(t("page.settings", lang), cls="section-title"),
        Table(
            *[Tr(Td(label, cls="detail-label"), _company_display_cell(key, flat.get(key)))
              for key, label in fields],
            cls="detail-table",
        ),
        cls="detail-card section-card",
    )


def _company_tab(company: dict, lang: str = "en", is_owner: bool = False) -> FT:
    prefs = [
        ("docs_default_preset", t("label.docs_default_preset", lang)),
        ("default_per_page", t("label.default_per_page", lang)),
    ]
    # Merge top-level company keys with settings dict; settings keys take precedence
    flat = {**company, **(company.get("settings") or {})}
    return Div(
        H3(t("settings.your_companies", lang), cls="settings-section-title"),
        Div(
            id="settings-companies-list",
            hx_get="/settings/company/companies-list",
            hx_trigger="load",
            hx_swap="innerHTML",
        ),
        A(
            "+ " + t("btn.new_company", lang),
            href="/setup/new-company",
            cls="btn btn--xs btn--secondary",
            style="margin-top:8px;display:inline-block;",
        ),
        # Company details (name + regional settings) + addresses live on Finance > Company Details now.
        H3(t("settings.preferences", lang), cls="settings-section-title"),
        P(t("msg.preferences_hint", lang), cls="settings-hint"),
        Table(
            *[Tr(
                Td(label, cls="detail-label"),
                _preference_display_cell(key, flat.get(key)),
            ) for key, label in prefs],
            cls="detail-table",
        ),
        *(
            [H3(t("page.danger_zone"), cls="settings-section-title settings-section-title--danger")]
            if is_owner else []
        ),
        *(
            [
                Div(
                    P(t("settings.deactivating_this_company_will_block_all_access_an"), cls="settings-help-text"),
                    Button(t("btn.deactivate_company"),
                        cls="btn btn--danger",
                        hx_delete="/settings/company/deactivate",
                        hx_confirm="Are you sure? All users will be logged out and the company will be deactivated.",
                        hx_target="body",
                        hx_push_url="true",
                    ),
                    cls="settings-card settings-card--danger",
                ),
                _factory_reset_card(),
                *(
                    [
                        Div(
                            Div(
                                Strong("Uninstall Celerp"),
                                P("Quit the app. Your data is preserved and can be re-imported later. Drag Celerp from Applications to Trash to complete removal.",
                                  cls="settings-help-text"),
                                cls="danger-zone-desc",
                            ),
                            Button("Uninstall (keep data)",
                                cls="btn btn--outline btn--danger",
                                onclick="window.celerp?.uninstallKeepData()"),
                            cls="settings-card settings-card--danger",
                        ),
                        Div(
                            Div(
                                Strong("Uninstall and Delete All Data"),
                                P("Permanently deletes all company data and removes Celerp. This cannot be undone.",
                                  cls="settings-help-text"),
                                cls="danger-zone-desc",
                            ),
                            Button("Uninstall and delete all data",
                                cls="btn btn--danger",
                                onclick="if(confirm('This will permanently delete all your data and cannot be undone. Are you sure?')){window.celerp?.uninstallDeleteData()}"),
                            cls="settings-card settings-card--danger",
                        ),
                    ]
                    if os.environ.get("CELERP_INSTALL_CHANNEL") == "electron" else []
                ),
            ]
            if is_owner else []
        ),
        cls="settings-card",
    )


def _users_tab(users: list[dict], settings: dict | None = None, lang: str = "en", is_owner: bool = False) -> FT:
    def _row(u: dict) -> FT:
        uid = u.get("id", "")
        return Tr(
            _user_display_cell(uid, "name", u.get("name")),
            _user_display_cell(uid, "email", u.get("email")),
            _user_display_cell(uid, "role", u.get("role")),
            _user_display_cell(uid, "is_active", u.get("is_active", True)),
            cls="data-row",
        )

    role_matrix = _role_permissions_matrix(settings, is_owner, lang)

    return Div(
        Table(
            Thead(Tr(Th(t("th.name", lang)), Th(t("th.email", lang)), Th(t("th.role", lang)), Th(t("th.active", lang)))),
            Tbody(*[_row(u) for u in users]),
            cls="data-table",
        ),
        A(t("btn.create_user", lang), href="/settings/users/new", cls="btn btn--primary mt-md"),
        role_matrix,
        cls="settings-card",
    )


# The fixed permissions carry no checkboxes; each states in one line why it cannot move.
_FIXED_ROW_REASON = {
    "manage_permissions": "Owner only, so no owner can remove their own access to this screen.",
    "manage_company_lifecycle": "Owner only.",
    "manage_billing": "Handled by the account owner through billing.",
}


def _role_matrix_row(perm, settings: dict | None, is_owner: bool) -> FT:
    """One permission row: a label plus a checkbox per registry role.

    A cell is interactive only for the owner, on a grantable permission, and never
    for the always-granted owner column; every other cell renders disabled. Toggling
    a cell saves through the per-toggle route and swaps this whole row, so the higher
    roles that inherit the change light up at once."""
    from celerp.services.permissions import ROLES, role_has_permission

    reason = _FIXED_ROW_REASON.get(perm.key) if not perm.grantable else None
    label = Td(
        perm.label,
        Span(reason, cls="role-ref-reason") if reason else "",
        cls="text-left",
    )
    cells = [label]
    for r in ROLES:
        checked = role_has_permission(settings, r.key, perm.key)
        interactive = is_owner and perm.grantable and r.key != "owner"
        attrs = {"type": "checkbox", "id": f"perm-{perm.key}-{r.key}", "checked": checked, "cls": "role-ref-check"}
        if interactive:
            # An unchecked checkbox is omitted from the request by the browser, so a
            # toggle-off simply sends no granted value; the route reads that as revoke.
            attrs.update({
                "name": "granted",
                "value": "true",
                "hx_patch": f"/settings/roles/{perm.key}/{r.key}",
                "hx_target": "closest tr",
                "hx_swap": "outerHTML",
                "hx_trigger": "change",
            })
        else:
            attrs["disabled"] = True
        cells.append(Td(Input(**attrs), cls="text-center"))
    return Tr(*cells, id=f"role-row-{perm.key}")


def _role_permissions_matrix(settings: dict | None, is_owner: bool, lang: str = "en") -> FT:
    """Owner-editable permission matrix built from the registry: a column per role,
    a row per permission, each cell the resolved grant for that pair."""
    from celerp.services.permissions import PERMISSIONS, ROLES

    return Details(
        Summary(t("settings.role_permissions_reference"), cls="role-ref-summary"),
        Table(
            Thead(Tr(
                Th(t("th.permission"), cls="text-left"),
                *[Th(t(r.label_key), cls="text-center") for r in ROLES],
            )),
            Tbody(*[_role_matrix_row(p, settings, is_owner) for p in PERMISSIONS]),
            cls="data-table role-ref-table",
        ),
        P(
            "Roles are hierarchical - each role inherits every permission from the roles below it. "
            "There must always be at least one Owner.",
            cls="role-ref-note",
        ),
        cls="role-ref-details mt-lg",
    )


def _taxes_tab(taxes: list[dict], lang: str = "en", prefix: str = "taxes", import_path: str | None = "/settings/import/taxes") -> FT:
    def _row(idx: int, tax: dict) -> FT:
        return Tr(
            _tax_display_cell(idx, "name", tax, prefix=prefix),
            _tax_display_cell(idx, "rate", tax, prefix=prefix),
            _tax_display_cell(idx, "tax_type", tax, prefix=prefix),
            _tax_display_cell(idx, "is_default", tax, prefix=prefix),
            _tax_display_cell(idx, "description", tax, prefix=prefix),
            Td(
                Button(t("btn.delete"), cls="btn btn--danger btn--xs",
                       hx_delete=f"/settings/{prefix}/{idx}",
                       hx_confirm=f"Delete tax '{tax.get('name', '')}'?",
                       hx_swap="none",
                       hx_on__after_request=f"window.location.reload()"),
                cls="cell",
            ),
            cls="data-row",
        )

    actions = [
        Button(t("btn.new_tax"), cls="btn btn--primary",
               hx_post=f"/settings/{prefix}/new", hx_swap="none",
               hx_on__after_request="window.location.reload()"),
    ]
    if import_path:
        actions.append(A(t("btn.import_taxes_csv"), href=import_path, cls="btn btn--secondary ml-sm"))

    return Div(
        Div(*actions, cls="page-actions mb-md"),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.rate")), Th(t("label.tax_type")), Th(t("th.default")), Th(t("th.description")), Th(""))),
            Tbody(*[_row(i, tax) for i, tax in enumerate(taxes)]),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _terms_tab(terms: list[dict], lang: str = "en", prefix: str = "terms", import_path: str | None = "/settings/import/payment-terms") -> FT:
    def _row(idx: int, term: dict) -> FT:
        return Tr(
            _term_display_cell(idx, "name", term, prefix=prefix),
            _term_display_cell(idx, "days", term, prefix=prefix),
            _term_display_cell(idx, "description", term, prefix=prefix),
            Td(
                Button(t("btn.delete"), cls="btn btn--danger btn--xs",
                       hx_delete=f"/settings/{prefix}/{idx}",
                       hx_confirm=f"Delete term '{term.get('name', '')}'?",
                       hx_swap="none",
                       hx_on__after_request="window.location.reload()"),
                cls="cell",
            ),
            cls="data-row",
        )

    actions = [
        Button(t("btn.new_term"), cls="btn btn--primary",
               hx_post=f"/settings/{prefix}/new", hx_swap="none",
               hx_on__after_request="window.location.reload()"),
    ]
    if import_path:
        actions.append(A(t("btn.import_payment_terms_csv"), href=import_path, cls="btn btn--secondary ml-sm"))

    return Div(
        Div(*actions, cls="page-actions mb-md"),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.days")), Th(t("th.description")), Th(""))),
            Tbody(*[_row(i, term) for i, term in enumerate(terms)]),
            cls="data-table",
        ),
        cls="settings-card",
    )


# All valid doc types for T&C association
_TC_DOC_TYPES_SALES = [
    ("invoice", "Invoice"), ("receipt", "Receipt"), ("credit_note", "Credit Note"),
    ("memo", "Consignment Out"),
]
_TC_DOC_TYPES_PURCHASING = [
    ("purchase_order", "Purchase Order"), ("bill", "Bill"),
    ("consignment_in", "Consignment In"),
]
_TC_DOC_TYPES_ALL = _TC_DOC_TYPES_SALES + _TC_DOC_TYPES_PURCHASING
_TC_DOC_TYPE_LABELS = dict(_TC_DOC_TYPES_ALL)


def _tc_display_cell(idx: int, field: str, template: dict, prefix: str = "terms-conditions") -> FT:
    """Display cell for a T&C template field."""
    if field == "doc_types":
        doc_types = template.get("doc_types") or []
        value = ", ".join(_TC_DOC_TYPE_LABELS.get(dt, dt) for dt in doc_types) or "--"
    elif field == "default_for":
        default_for = template.get("default_for") or []
        value = ", ".join(_TC_DOC_TYPE_LABELS.get(dt, dt) for dt in default_for) or "--"
    elif field == "text":
        raw = str(template.get("text") or "")
        value = (raw[:80] + "...") if len(raw) > 80 else raw or "--"
    else:
        value = str(template.get(field) or "") or "--"
    return Td(
        Div(
            value,
            hx_get=f"/settings/{prefix}/{idx}/{field}/edit",
            hx_target="closest td", hx_swap="outerHTML", hx_trigger="click",
            title="Click to edit", cls="editable-cell",
        ),
        cls="cell",
    )


def _terms_conditions_tab(templates: list[dict], prefix: str = "terms-conditions", scope: str = "all") -> FT:
    """Render T&C templates tab. scope='sales'|'purchasing'|'all' filters display."""
    if scope == "sales":
        scope_types = {dt for dt, _ in _TC_DOC_TYPES_SALES}
    elif scope == "purchasing":
        scope_types = {dt for dt, _ in _TC_DOC_TYPES_PURCHASING}
    else:
        scope_types = {dt for dt, _ in _TC_DOC_TYPES_ALL}
    # Filter: show templates that have at least one doc_type in scope (or no doc_types yet)
    filtered = [(i, tpl) for i, tpl in enumerate(templates)
                if not (tpl.get("doc_types") or []) or scope_types & set(tpl.get("doc_types") or [])]

    def _row(global_idx: int, tpl: dict) -> FT:
        return Tr(
            _tc_display_cell(global_idx, "name", tpl, prefix=prefix),
            _tc_display_cell(global_idx, "text", tpl, prefix=prefix),
            _tc_display_cell(global_idx, "doc_types", tpl, prefix=prefix),
            _tc_display_cell(global_idx, "default_for", tpl, prefix=prefix),
            Td(
                Button(t("btn.delete"), cls="btn btn--danger btn--xs",
                       hx_delete=f"/settings/{prefix}/{global_idx}",
                       hx_confirm=f"Delete T&C '{tpl.get('name', '')}'?",
                       hx_swap="none",
                       hx_on__after_request="window.location.reload()"),
                cls="cell",
            ),
            cls="data-row",
        )

    return Div(
        H3(t("page.terms_conditions_templates"), cls="settings-section-title"),
        P(t("settings.configure_reusable_terms_conditions_templates_assi"),
          cls="settings-hint"),
        Div(
            Button(t("btn.new_template"), cls="btn btn--primary",
                   hx_post=f"/settings/{prefix}/new", hx_swap="none",
                   hx_on__after_request="window.location.reload()"),
            cls="page-actions mb-md",
        ),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.text")), Th(t("th.document_types")), Th(t("th.default_for")), Th(""))),
            Tbody(*[_row(gi, tpl) for gi, tpl in filtered]),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _register_tc_crud(app, prefix: str, get_fn_name: str, patch_fn_name: str, redirect_url: str, scope_doc_types: list[tuple[str, str]] | None = None):
    """Register CRUD endpoints for Terms & Conditions templates.
    scope_doc_types: if provided, only these doc types are shown in checkboxes.
    """
    allowed_doc_types = scope_doc_types or _TC_DOC_TYPES_ALL

    def _make_edit(pfx, gname):
        async def tc_field_edit(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            try:
                templates = await getattr(api, gname)(token)
            except APIError as e:
                return P(f"Error: {e.detail}", cls="cell-error")
            tmpl = templates[idx] if idx < len(templates) else {}

            if field == "doc_types":
                current = set(tmpl.get("doc_types") or [])
                checkboxes = [
                    Div(
                        Input(type="checkbox", name="doc_types", value=dt, checked=(dt in current), id=f"dt-{idx}-{dt}"),
                        Label(label, For=f"dt-{idx}-{dt}"),
                        cls="checkbox-inline",
                    )
                    for dt, label in allowed_doc_types
                ]
                return Td(
                    Form(
                        *checkboxes,
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--xs btn--primary"),
                            Button(t("btn.cancel"), type="button", cls="btn btn--xs btn--secondary ml-xs",
                                   onclick="window.location.reload()"),
                            cls="mt-sm",
                        ),
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML",
                    ),
                    cls="cell cell--editing",
                )
            elif field == "default_for":
                # Only show doc types already assigned to this template
                my_doc_types = set(tmpl.get("doc_types") or [])
                current_defaults = set(tmpl.get("default_for") or [])
                available = [(dt, label) for dt, label in allowed_doc_types if dt in my_doc_types]
                if not available:
                    return Td(P(t("settings.assign_document_types_first"), cls="cell-hint"), cls="cell cell--editing")
                checkboxes = [
                    Div(
                        Input(type="checkbox", name="default_for", value=dt, checked=(dt in current_defaults), id=f"df-{idx}-{dt}"),
                        Label(label, For=f"df-{idx}-{dt}"),
                        cls="checkbox-inline",
                    )
                    for dt, label in available
                ]
                return Td(
                    Form(
                        *checkboxes,
                        Div(
                            Button(t("btn.save"), type="submit", cls="btn btn--xs btn--primary"),
                            Button(t("btn.cancel"), type="button", cls="btn btn--xs btn--secondary ml-xs",
                                   onclick="window.location.reload()"),
                            cls="mt-sm",
                        ),
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_swap="none",
                        hx_on__after_request="window.location.reload()",
                    ),
                    cls="cell cell--editing",
                )
            elif field == "text":
                return Td(
                    Textarea(
                        str(tmpl.get("text") or ""), name="value", rows="4",
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML",
                        hx_trigger="blur delay:200ms",
                        cls="cell-input", autofocus=True,
                    ),
                    cls="cell cell--editing",
                )
            else:
                val = str(tmpl.get(field, "") or "")
                return Td(
                    Input(
                        type="text", name="value", value=val,
                        hx_patch=f"/settings/{pfx}/{idx}/{field}",
                        hx_target="closest td", hx_swap="outerHTML", hx_include="this",
                        hx_trigger="blur delay:200ms",
                        cls="cell-input", autofocus=True,
                    ),
                    cls="cell cell--editing",
                )
        return tc_field_edit

    def _make_patch(pfx, gname, pname):
        async def tc_field_patch(request: Request, idx: int, field: str):
            token = _token(request)
            if not token:
                return P(t("error.unauthorized"), cls="cell-error")
            form = await request.form()
            try:
                templates = await getattr(api, gname)(token)
                if idx >= len(templates):
                    return P(t("settings.template_not_found"), cls="cell-error")
                if field == "doc_types":
                    new_doc_types = form.getlist("doc_types")
                    templates[idx]["doc_types"] = new_doc_types
                    # Prune default_for to only include types still in doc_types
                    old_defaults = set(templates[idx].get("default_for") or [])
                    templates[idx]["default_for"] = [dt for dt in new_doc_types if dt in old_defaults]
                elif field == "default_for":
                    new_defaults = set(form.getlist("default_for"))
                    templates[idx]["default_for"] = list(new_defaults)
                    # Remove these doc types from default_for on all other templates
                    for j, other in enumerate(templates):
                        if j != idx:
                            other["default_for"] = [dt for dt in (other.get("default_for") or []) if dt not in new_defaults]
                else:
                    templates[idx][field] = str(form.get("value", ""))
                await getattr(api, pname)(token, templates)
                templates = await getattr(api, gname)(token)
            except APIError as e:
                return P(str(e.detail), cls="cell-error")
            tmpl = templates[idx] if idx < len(templates) else {}
            return _tc_display_cell(idx, field, tmpl, prefix=pfx)
        return tc_field_patch

    def _make_new(gname, pname, redir):
        async def create_tc(request: Request):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                templates = await getattr(api, gname)(token)
                templates.append({"name": "New Template", "text": "", "doc_types": [], "default_for": []})
                await getattr(api, pname)(token, templates)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return create_tc

    def _make_delete(gname, pname, redir):
        async def delete_tc(request: Request, idx: int):
            from starlette.responses import Response as _R
            token = _token(request)
            if not token:
                return _R("", status_code=401, headers={"HX-Redirect": "/login"})
            try:
                templates = await getattr(api, gname)(token)
                if 0 <= idx < len(templates):
                    templates.pop(idx)
                    await getattr(api, pname)(token, templates)
            except APIError:
                return _R("", status_code=500)
            return _R("", status_code=204, headers={"HX-Redirect": redir})
        return delete_tc

    app.get(f"/settings/{prefix}/{{idx}}/{{field}}/edit")(_make_edit(prefix, get_fn_name))
    app.patch(f"/settings/{prefix}/{{idx}}/{{field}}")(_make_patch(prefix, get_fn_name, patch_fn_name))
    app.post(f"/settings/{prefix}/new")(_make_new(get_fn_name, patch_fn_name, redirect_url))
    app.delete(f"/settings/{prefix}/{{idx}}")(_make_delete(get_fn_name, patch_fn_name, redirect_url))


def _pl_select(select_id: str, post_url: str, status_id: str, names: list[str], selected: str, oob: bool = False) -> FT:
    """Auto-saving price-list <select>, used inline and for out-of-band refreshes after renames."""
    oob_kw = {"hx_swap_oob": "true"} if oob else {}
    return Select(
        *[Option(name, value=name, selected=(name == selected)) for name in names],
        name="name",
        id=select_id,
        cls="form-select",
        hx_post=post_url,
        hx_target=status_id,
        hx_swap="outerHTML",
        hx_trigger="change",
        **oob_kw,
    )


def _price_lists_tab(price_lists: list[dict], default_price_list: str, base_price_list: str, prefix: str = "price-lists") -> FT:
    def _row(idx: int, pl: dict) -> FT:
        name = pl.get("name", "")
        is_protected = name in ("Retail", "Wholesale")
        # Success navigates via HX-Redirect; failures surface as an auto-dismissing toast.
        delete_cell = Td(cls="cell") if is_protected else Td(
            Button(t("btn.delete"), cls="btn btn--danger btn--xs",
                   hx_delete=f"/settings/{prefix}/{idx}",
                   hx_confirm=f"Delete price list '{name}'?",
                   hx_swap="none"),
            cls="cell",
        )
        return Tr(
            _price_list_display_cell(idx, "name", pl, prefix=prefix),
            _price_list_display_cell(idx, "description", pl, prefix=prefix),
            _price_list_display_cell(idx, "multiplier", pl, prefix=prefix),
            _price_list_display_cell(idx, "rounding", pl, prefix=prefix),
            delete_cell,
            cls="data-row",
        )

    # Filter out Cost - it's a system field, not a user-facing price list
    visible_price_lists = [(i, pl) for i, pl in enumerate(price_lists) if pl.get("name") != "Cost"]
    pl_names = [pl.get("name", "") for _, pl in visible_price_lists]
    all_names = [pl.get("name", "") for pl in price_lists]

    return Div(
        H3(t("page.price_lists"), cls="settings-section-title"),
        P(t("settings.define_your_companys_price_tiers_these_names_are_u"),
          cls="settings-hint"),
        P(t("settings.derived_price_lists_hint"), cls="settings-hint"),
        Div(
            # Success navigates via HX-Redirect; failures surface as an auto-dismissing toast.
            Button(t("btn.add_price_list"), cls="btn btn--primary",
                   hx_post=f"/settings/{prefix}/new", hx_swap="none"),
            cls="page-actions mb-md",
        ),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.description")),
                     Th(t("th.factor"), title=t("settings.factor_tooltip")),
                     Th(t("th.rounding"), title=t("settings.rounding_tooltip")),
                     Th(""))),
            Tbody(*[_row(i, pl) for i, pl in visible_price_lists]),
            cls="data-table",
        ),
        H3(t("page.base_price_list"), cls="settings-section-title mt-lg"),
        P(t("settings.base_price_list_hint"), cls="settings-hint"),
        Form(
            _pl_select("base-price-list-select", "/settings/base-price-list",
                       "#base-price-list-status", all_names, base_price_list),
            Span("", id="base-price-list-status", cls="settings-hint"),
            cls="form-inline",
        ),
        H3(t("page.default_price_list"), cls="settings-section-title mt-lg"),
        P(t("settings.used_when_a_contact_has_no_price_list_assigned_and"),
          cls="settings-hint"),
        Form(
            _pl_select("default-price-list-select", "/settings/default-price-list",
                       "#default-price-list-status", pl_names, default_price_list),
            Span("", id="default-price-list-status", cls="settings-hint"),
            cls="form-inline",
        ),
        cls="settings-card",
    )


def _schema_tab(schema: list[dict], cat_schemas: dict, cat_tab: str = "") -> FT:
    """Item Schema tab with category selector.

    cat_tab="" → global schema (display only); cat_tab="CategoryName" → editable category schema.
    """
    from urllib.parse import quote
    categories = sorted(cat_schemas.keys())

    def _cat_tab_link(key: str, label: str) -> FT:
        href = f"/settings/inventory?tab=category-library&cat={key}" if key else "/settings/inventory?tab=category-library"
        active = (cat_tab == key)
        return A(label, href=href, cls=f"sub-tab {'sub-tab--active' if active else ''}")

    cat_selector = Div(
        _cat_tab_link("", "Global"),
        *[_cat_tab_link(c, c) for c in categories],
        cls="sub-tabs",
    ) if categories else ""

    is_cat = bool(cat_tab and cat_tab in cat_schemas)

    if is_cat:
        active_schema = cat_schemas[cat_tab]
        hint = f"Attribute columns for the '{cat_tab}' category. Click a cell to edit. Auto-populated from CSV imports."
        enc = quote(cat_tab, safe="")
        sorted_schema = sorted(active_schema, key=lambda x: x.get("position", 0))

        def _cat_row(idx: int, f: dict) -> FT:
            return Tr(
                _cat_schema_display_cell(cat_tab, idx, "position", f),
                _cat_schema_display_cell(cat_tab, idx, "key", f),
                _cat_schema_display_cell(cat_tab, idx, "label", f),
                _cat_schema_display_cell(cat_tab, idx, "type", f),
                _cat_schema_display_cell(cat_tab, idx, "required", f),
                _cat_schema_display_cell(cat_tab, idx, "editable", f),
                _cat_schema_display_cell(cat_tab, idx, "show_in_table", f),
                _cat_schema_display_cell(cat_tab, idx, "options", f),
                Td(
                    Button("✕", cls="btn btn--danger btn--xs",
                           hx_delete=f"/settings/cat-schema/{enc}/{idx}",
                           hx_confirm=f"Delete field '{f.get('key', idx)}'?",
                           hx_swap="none",
                           hx_on__after_request=f"window.location.href='/settings/inventory?tab=category-library&cat={cat_tab}'"),
                    cls="cell",
                ),
                cls="data-row",
            )

        add_row = Tr(
            Td(
                Button(t("btn.add_field"), cls="btn btn--secondary btn--xs",
                       hx_post=f"/settings/cat-schema/{enc}/add",
                       hx_swap="none",
                       hx_on__after_request=f"window.location.href='/settings/inventory?tab=category-library&cat={cat_tab}'"),
                colspan="9",
                cls="p-sm",
            ),
        )

        return Div(
            cat_selector,
            P(hint, cls="settings-hint"),
            Table(
                Thead(Tr(Th("Order", title="Display order - click to change"), Th("Name"), Th(t("th.doc_type")), Th("Req"), Th("Edit"), Th("In Table"), Th(t("th.options")), Th(""))),
                Tbody(*[_cat_row(i, f) for i, f in enumerate(sorted_schema)], add_row),
                cls="data-table",
            ),
            cls="settings-card",
        )

    # Global schema - display only (structural fields, not attribute-driven)
    active_schema = schema
    hint = "Global columns shown for all items. Order matches display order."
    sorted_schema = sorted(active_schema, key=lambda x: x.get("position", 0))

    def _row(idx: int, f: dict) -> FT:
        return Tr(
            _schema_display_cell(idx, "position", f),
            _schema_display_cell(idx, "key", f),
            _schema_display_cell(idx, "label", f),
            _schema_display_cell(idx, "type", f),
            _schema_display_cell(idx, "required", f),
            _schema_display_cell(idx, "editable", f),
            _schema_display_cell(idx, "show_in_table", f),
            _schema_display_cell(idx, "options", f),
            cls="data-row",
        )

    return Div(
        cat_selector,
        P(hint, cls="settings-hint"),
        Table(
            Thead(Tr(Th("#"), Th(t("th.key")), Th(t("th.label")), Th(t("th.doc_type")), Th(t("th.required")), Th(t("th.editable")), Th(t("th.show_in_table")), Th(t("th.options")))),
            Tbody(*[_row(i, f) for i, f in enumerate(sorted_schema)]),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _locations_tab(locations: list[dict], lang: str = "en") -> FT:
    def _row(loc: dict) -> FT:
        lid = loc.get("id", "")
        is_default = bool(loc.get("is_default"))
        return Tr(
            _location_display_cell(lid, "name", loc.get("name")),
            _location_display_cell(lid, "type", loc.get("type")),
            _location_display_cell(lid, "address", loc.get("address")),
            Td(
                Button(
                    "✓ Default" if is_default else "Set default",
                    cls=f"btn btn--xs {'btn--primary' if is_default else 'btn--secondary'}",
                    hx_patch=f"/settings/locations/{lid}/is_default",
                    hx_vals='{"value": "true"}',
                    hx_swap="none",
                    hx_on__after_request="window.location.href='/settings/inventory?tab=locations'",
                    disabled=is_default,
                ),
                cls="cell",
            ),
            Td(cls="cell") if is_default else Td(
                Button(t("btn.delete"), cls="btn btn--danger btn--xs",
                       hx_delete=f"/settings/locations/{lid}",
                       hx_confirm=f"Delete location '{loc.get('name', '')}'? Items must be unassigned first.",
                       hx_swap="none",
                       hx_on__after_request="if(event.detail.successful) window.location.href='/settings/inventory?tab=locations'"),
                cls="cell",
            ),
            cls="data-row",
        )

    return Div(
        Div(
            Button(t("btn.new_location"), cls="btn btn--primary",
                   hx_post="/settings/locations/new", hx_swap="none",
                   hx_on__after_request="window.location.href='/settings/inventory?tab=locations'"),
            A(t("settings.import_locations_csv"), href="/settings/import/locations", cls="btn btn--secondary ml-sm"),
            cls="page-actions mb-md",
        ),
        Table(
            Thead(Tr(Th(t("th.name")), Th(t("th.doc_type")), Th(t("th.address")), Th(t("th.default")), Th(""))),
            Tbody(*[_row(l) for l in locations]) if locations else Tbody(Tr(Td(t("settings.no_locations_yet"), colspan="5", cls="empty-state-msg"))),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _cloud_relay_unconnected(
    iid: str,
    error: str | None = None,
    info: str | None = None,
    show_email_form: bool = True,
    show_header: bool = True,
    suppress_autoconnect: bool = False,
) -> FT:
    """Render the unconnected state of the Celerp Connect tab (used by HTMX responses too).

    Args:
        show_header: When False, suppress the H3 title, description, and Subscribe button.
            Used when embedding inside the Web Access value-prop page which already
            has its own plan cards and CTAs.
        suppress_autoconnect: When True, omit the on-load auto-connect script. Set by
            the disconnect response so the fragment it swaps in cannot immediately
            re-fire Connect and undo the disconnect the user just performed; the
            Connect button stays and reconnects in one click from the preserved
            credential.
    """
    from celerp.gateway.state import build_subscribe_url
    subscribe_url = build_subscribe_url(iid)
    children: list = []
    if show_header:
        children += [
            H3(t("settings.tab_cloud_relay"), cls="settings-section-title"),
            P(t("settings.connect_to_celerp_cloud_to_get_a_stable_public_url"),
                cls="settings-hint",
            ),
        ]
    if error:
        children.append(P(error, cls="text-error", style="margin:8px 0;"))
    if info:
        children.append(P(info, cls="text-connected", style="margin:8px 0;"))

    children.append(
        Div(
            *(
                [A(t("settings.subscribe"), href=subscribe_url, target="_blank", cls="btn btn--primary")]
                if show_header else []
            ),
            Button(t("btn.connect_to_cloud"),
                cls="btn btn--outline",
                id="cloud-connect-btn",
                hx_post="/settings/cloud-activate",
                hx_target="#cloud-relay-tab",
                hx_swap="outerHTML",
                hx_indicator="#cloud-connecting",
                style="margin-left:8px;" if show_header else "",
            ),
            Span(t("settings.connecting"), id="cloud-connecting", cls="settings-hint htmx-indicator", style="margin-left:12px;display:none;"),
            style="display:flex;align-items:center;flex-wrap:wrap;gap:0;margin-top:12px;",
        )
    )
    # Auto-trigger on first load (silently tries to activate; shows result inline).
    # Withheld after a deliberate disconnect so the swapped-in fragment cannot
    # instantly reconnect and undo it.
    if not suppress_autoconnect:
        children.append(
            Script("""
(function(){
  if (sessionStorage.getItem('cloud_activate_tried')) return;
  sessionStorage.setItem('cloud_activate_tried', '1');
  var btn = document.getElementById('cloud-connect-btn');
  if (btn) htmx.trigger(btn, 'click');
})();
""")
        )

    if show_email_form:
        children += [
            P(
                "If you subscribed on the website and the payment isn't linking automatically, "
                "enter the email address you used at checkout:",
                cls="settings-hint",
                style="margin-top:16px;",
            ),
            Form(
                Input(
                    type="email",
                    name="claim_email",
                    placeholder="Email used at checkout",
                    required=True,
                    cls="input input--sm",
                    style="width:260px;",
                ),
                Button(t("btn.link_subscription"), type="submit", cls="btn btn--sm btn--outline", style="margin-left:8px;"),
                hx_post="/settings/cloud-send-otp",
                hx_target="#cloud-relay-tab",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;margin-top:8px;",
            ),
        ]

    return Div(*children, id="cloud-relay-tab", cls="settings-card")


def _tos_acceptance_card(required_version: str) -> FT:
    """Render the TOS acceptance card shown when the relay requires TOS acceptance."""
    return Div(
        H3(t("settings.tab_cloud_relay"), cls="settings-section-title"),
        P(t("settings.to_use_celerp_cloud_you_must_accept_the_terms_of_s"),
            cls="settings-hint",
            style="margin-bottom:16px;",
        ),
        Div(
            Input(
                type="checkbox",
                id="tos-agree-checkbox",
                oninput="document.getElementById('tos-accept-btn').disabled = !this.checked;",
                style="margin-right:8px;",
            ),
            Label(t("label.i_agree_to_the"),
                A(t("settings.terms_of_service"), href="https://relay.celerp.com/terms", target="_blank"),
                " and ",
                A(t("settings.privacy_policy"), href=PRIVACY_POLICY_URL, target="_blank"),
                **{"for": "tos-agree-checkbox"},
            ),
            style="display:flex;align-items:center;margin-bottom:16px;",
        ),
        Button(t("btn.accept_connect"),
            id="tos-accept-btn",
            cls="btn btn--primary",
            disabled=True,
            hx_post="/settings/cloud-accept-tos",
            hx_target="#cloud-relay-tab",
            hx_swap="outerHTML",
        ),
        id="cloud-relay-tab",
        cls="settings-card",
    )


# Billing tiers above free. Any tier NOT in this set (including "", None, and
# "free" itself) is treated as free - an unconfirmed tier must default to the
# more restrictive, upsell-showing state, never silently hide the free-tier
# note and sales funnel because a status round trip hasn't landed yet.
PAID_TIERS = frozenset({"cloud", "ai", "team"})


def _cloud_relay_tab(relay_status: str | None = None, public_url: str | None = None,
                      tier: str | None = None, token_bound: bool = False) -> FT:
    """Celerp Connect settings tab.

    relay_status: caller-supplied (cross-process split); falls back to local get_client().
    public_url: caller-supplied; falls back to local config.
    tier: caller-supplied billing tier ("free", "cloud", "ai", "team"); anything
    other than a known paid tier (including None/"" while unknown) is treated
    as free, so the free-tier note degrades to shown, never hidden.
    token_bound: the instance holds a gateway_token (it is signed in), which a
    free tier does WITHOUT a live tunnel - the WS client never starts for it, so
    relay_status stays "inactive". Treat that as connected so a signed-in free
    account still gets the account/disconnect view, not the subscribe/claim page.
    """
    from celerp.config import settings as _cfg, ensure_instance_id
    from celerp.gateway.client import get_client
    gw = get_client()

    if relay_status is None:
        relay_status = gw.relay_status if gw else "inactive"

    if public_url is None:
        public_url = getattr(_cfg, "celerp_public_url", "")

    required_tos = getattr(gw, "required_tos_version", "") if gw else ""
    if relay_status == "tos_required":
        return _tos_acceptance_card(required_tos)

    is_connected = token_bound or relay_status not in ("inactive",) or gw is not None
    if is_connected:
        badge_cls = {
            "active": "badge--active",
            "connecting": "badge--warning",
            "error": "badge--error",
            "inactive": "badge--inactive",
        }.get(relay_status, "badge--inactive")

        # Status explanation for non-active states
        status_hint = {
            "connecting": "Establishing connection...",
            "error": "Connection failed. Try disconnecting and reconnecting.",
            "inactive": "Initializing connection...",
        }.get(relay_status, "")

        rows = [
            Tr(Td(t("th.status"), cls="detail-label"), Td(
                Span(relay_status.capitalize(), cls=f"badge {badge_cls}"),
                Span(f" - {status_hint}", cls="settings-hint") if status_hint else "",
            )),
        ]
        # Show team URL (subdomain) if configured
        if public_url:
            rows.append(Tr(Td(t("settings.team_url"), cls="detail-label"), Td(
                A(public_url, href=public_url, target="_blank", cls="cell--mono"),
                P(t("settings.share_this_url_with_your_team_members_to_access_ce"), cls="settings-hint",
                  style="margin:4px 0 0;"),
            )))

        free_tier_note = Div(
            P(
                Span(t("cloud.free_tier_badge"), cls="badge badge--inactive", style="margin-right:6px;"),
                t("cloud.free_tier_note"),
                cls="settings-hint",
            ),
            Ul(
                Li(t("cloud.free_b1")),
                Li(t("cloud.free_b2")),
                Li(t("cloud.free_b3")),
                Li(t("cloud.free_b4")),
                style="margin:6px 0 0;padding-left:20px;",
            ),
            Button(t("btn.link_subscription"), type="button",
                   hx_get="/account/panel?intent=claim&panel=account-gate-panel&modal=1",
                   hx_target="#account-gate-host", hx_swap="outerHTML",
                   cls="btn btn--sm btn--outline", style="margin-top:8px;"),
            style="margin-top:12px;",
        ) if tier not in PAID_TIERS else ""

        return Div(
            H3(t("settings.tab_cloud_relay"), cls="settings-section-title"),
            Table(*rows, cls="detail-table"),
            free_tier_note,
            Div(
                Button(t("btn.disconnect"),
                    cls="btn btn--sm btn--outline btn--danger",
                    hx_post="/settings/cloud-disconnect",
                    hx_target="#cloud-relay-tab",
                    hx_swap="outerHTML",
                    hx_confirm="Disconnect web access? You can reconnect anytime.",
                ),
                style="margin-top:12px;",
            ),
            id="cloud-relay-tab",
            cls="settings-card",
        )

    # No token — render subscribe/claim UI
    iid = ensure_instance_id()
    return _cloud_relay_unconnected(iid)


def _backup_tab(lang: str = "en", backup_data: dict | None = None) -> FT:
    """Cloud Backup settings tab - full history UI with export/import."""
    from celerp.config import settings as _cfg
    from ui.components.backup import local_backup_buttons
    from ui.components.cloud_gate import upgrade_banner

    enc_ok = bool(backup_data and backup_data.get("enc_ok")) if backup_data is not None else bool(_cfg.backup_encryption_key)
    # gw_ok gates on public_url, not just a gateway_token: a free instance now holds
    # a gateway_token too (marketplace purchases), but backups are a paid-tier
    # feature and public_url is only granted to paid tiers (mirrors the lazy-tunnel
    # gate). Derived from the API response - reading get_client()/settings here
    # would always return the UI process's own state, not the API process's.
    gw_ok = bool(backup_data and backup_data.get("public_url"))

    if not gw_ok:
        return Div(
            H3(t("settings.tab_backup"), cls="settings-section-title"),
            upgrade_banner(
                t("cloud.backup_feature_name", lang),
                t("cloud.backup_desc", lang),
                price="USD $29/mo",
                plan="cloud",
                lang=lang,
            ),
            # Local export/import always available
            H3(t("page.local_backup"), cls="settings-section-title mt-lg"),
            P(t("settings.export_and_import_full_backups_locally_no_cloud_su"),
                cls="settings-hint",
            ),
            local_backup_buttons(),
            Div(id="backup-flash", cls="mt-sm"),
            cls="settings-card",
        )

    # ── Status summary ────────────────────────────────────────────────
    # backup_data is fetched from the API process by the caller to avoid
    # reading stale module-level state in the UI process.
    _bd = backup_data or {}
    _db = _bd.get("db") or {}
    scheduler_running = _bd.get("running", False)
    next_db_iso: str | None = _bd.get("next_db_utc")

    def _time_until(iso: str | None) -> str:
        if iso is None:
            return "not scheduled"
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        if hours > 0:
            return f"in {hours}h {mins}m"
        return f"in {mins}m"

    status_rows = [
        Tr(Td(t("settings.scheduler"), cls="detail-label"), Td(
            Span(t("settings.running"), cls="badge badge--active") if scheduler_running
            else Span(t("settings.stopped"), cls="badge badge--inactive"),
        )),
        Tr(Td(t("settings.next_db_backup"), cls="detail-label"), Td(_time_until(next_db_iso))),
    ]

    # Last snapshot result
    db_ok = _db.get("ok")
    if db_ok is not None:
        if db_ok:
            status_rows.append(Tr(Td(t("settings.last_db_backup"), cls="detail-label"), Td(
                Span("OK", cls="badge badge--active"),
                Span(f" - {(_db.get('size_bytes') or 0) / 1024**2:.1f} MB uploaded", cls="settings-hint"),
            )))
        else:
            status_rows.append(Tr(Td(t("settings.last_db_backup"), cls="detail-label"), Td(
                Span(t("settings.failed"), cls="badge badge--error"),
                Span(f" - {_db.get('error', '')}", cls="settings-hint"),
            )))

    status_section = Div(
        Table(*status_rows, cls="detail-table"),
        cls="mt-md",
    )

    # ── Encryption key ────────────────────────────────────────────────
    key_val = (backup_data or {}).get("enc_key") or ""
    key_section = Div(
        H4(t("page.encryption_key"), cls="settings-section-title"),
        P(
            "Your backups are encrypted with this key. Save it in a password manager - "
            "we cannot recover your backups without it.",
            cls="settings-hint",
        ),
        Div(
            Code(key_val, cls="cell--mono", id="backup-enc-key"),
            Button(t("btn.copy"),
                **{"data-copy": "#backup-enc-key"},
                cls="btn btn--xs btn--secondary ml-sm",
            ),
            cls="flex-row align-center gap-sm",
        ),
        cls="mt-lg",
    ) if enc_ok else ""

    # ── Action buttons ────────────────────────────────────────────────
    from ui.components.backup import cloud_backup_buttons
    actions = cloud_backup_buttons(enc_ok=enc_ok, gw_ok=gw_ok)

    flash_target = Div(id="backup-flash", cls="mt-sm")

    # ── Backup history (HTMX lazy-loaded from relay) ──────────────────
    history_section = Div(
        H4(t("page.cloud_backups"), cls="settings-section-title"),
        Div(
            id="backup-db-list",
            hx_get="/backup/list",
            hx_trigger="load, backupDone from:body",
            hx_swap="innerHTML",
        ),
        cls="mt-lg",
    )

    # ── Local export / import ─────────────────────────────────────────
    local_section = Div(
        H4(t("page.local_backup"), cls="settings-section-title"),
        P(t("settings.export_and_import_full_backups_locally_no_cloud_su"), cls="settings-hint"),
        local_backup_buttons(),
        Hr(cls="section-divider mt-lg"),
        cls="mb-md",
    )

    # ── How it works ──────────────────────────────────────────────────
    how_it_works = Div(
        H4(t("page.how_cloud_backup_works"), cls="settings-section-title"),
        P(t("settings.celerp_runs"),
            Code("pg_dump"),
            " locally and content-addresses every file, encrypts each piece with AES-256-GCM "
            "using your key (we never see it), then uploads only what changed to DigitalOcean "
            "Spaces. A daily snapshot captures the database and all files together; unchanged "
            "files are never re-uploaded. Oldest snapshots are auto-pruned per your plan. "
            "After cancellation, backups remain accessible for 30 days.",
            cls="settings-hint",
        ),
        cls="mb-lg",
    )

    return Div(
        H3(t("settings.tab_backup"), cls="settings-section-title"),
        local_section,
        how_it_works,
        status_section,
        key_section,
        actions,
        flash_target,
        history_section,
        cls="settings-card",
    )


def _import_history_tab(batches: list[dict]) -> FT:
    """Import History tab - lists all import batches with undo capability."""

    def _row(b: dict) -> FT:
        bid = b.get("id", "")
        status = b.get("status", "active")
        badge_cls = "badge--active" if status == "active" else "badge--inactive"
        imported_at = b.get("imported_at", "")[:19].replace("T", " ") if b.get("imported_at") else EMPTY
        undone_at = b.get("undone_at", "")
        undone_display = undone_at[:19].replace("T", " ") if undone_at else EMPTY
        return Tr(
            Td(b.get("entity_type", ""), cls="cell"),
            Td(b.get("filename") or EMPTY, cls="cell"),
            Td(str(b.get("row_count", 0)), cls="cell"),
            Td(imported_at, cls="cell"),
            Td(Span(status.capitalize(), cls=f"badge {badge_cls}"), cls="cell"),
            Td(
                Button(t("btn.undo"),
                    cls="btn btn--danger btn--xs",
                    hx_post=f"/settings/import-history/{bid}/undo",
                    hx_confirm=f"Undo this import? This will remove {b.get('row_count', 0)} item(s).",
                    hx_swap="none",
                ) if status == "active" else Span(undone_display, cls="settings-hint"),
                cls="cell",
            ),
            cls="data-row",
        )

    return Div(
        H3(t("settings.tab_import_history"), cls="settings-section-title"),
        P(
            "All item CSV imports are tracked here. "
            "You can undo an import to remove the items it created. "
            "Items modified since import will be flagged but not protected.",
            cls="settings-hint",
        ),
        Table(
            Thead(Tr(Th(t("th.doc_type")), Th(t("th.filename")), Th(t("th.rows")), Th(t("th.imported_at_utc")), Th(t("th.status")), Th(""))),
            Tbody(*[_row(b) for b in batches]) if batches else Tbody(
                Tr(Td(t("settings.no_imports_yet"), colspan="6", cls="empty-state-msg"))
            ),
            cls="data-table",
        ),
        cls="settings-card",
    )


def _bulk_attach_tab() -> FT:
    """Bulk Attachments tab - upload a ZIP of files named by SKU."""
    return Div(
        # Client-side filter for the result report: the summary pills (rendered by the
        # /settings/bulk-attach response) call this to show only matched/unmatched/error rows.
        Style(
            ".bulk-filter-btn{cursor:pointer;border:none;}"
            ".bulk-filter-btn.is-active{outline:2px solid currentColor;outline-offset:1px;}"
        ),
        Script(
            "function celerpBulkFilter(status, btn){"
            "var root=document.getElementById('bulk-attach-result');if(!root)return;"
            "var rows=root.querySelectorAll('tbody tr');"
            "for(var i=0;i<rows.length;i++){var s=rows[i].getAttribute('data-status');"
            "rows[i].style.display=(status==='all'||s===status)?'':'none';}"
            "var btns=root.querySelectorAll('.bulk-filter-btn');"
            "for(var j=0;j<btns.length;j++){btns[j].classList.remove('is-active');}"
            "if(btn){btn.classList.add('is-active');}}"
        ),
        H3(t("page.bulk_attach_images_documents"), cls="section-title"),
        Div(
            H4(t("page.file_naming_convention")),
            Table(
                Thead(Tr(Th(t("th.filename_pattern")), Th(t("th.result")))),
                Tbody(
                    Tr(Td(Code("SKU1234.jpg / .png / .webp")), Td(t("inv.hero_product_image"))),
                    Tr(Td(Code("SKU1234-img-2.jpg")), Td(t("inv.additional_product_image"))),
                    Tr(Td(Code("SKU1234-cert-grs.pdf")), Td(t("file_tag.certificates"))),
                    Tr(Td(Code("SKU1234-doc-warranty.pdf")), Td(t("file_tag.certificate_alias"))),
                    Tr(Td(Code("SKU1234-spec-datasheet.pdf")), Td(t("file_tag.spec_sheets"))),
                    Tr(Td(Code("SKU1234-safety-msds.pdf")), Td(t("file_tag.safety_docs"))),
                    Tr(Td(Code("SKU1234-360-exterior.mp4")), Td(t("file_tag.view_360"))),
                ),
                cls="data-table",
            ),
            cls="settings-card mb-md",
        ),
        Form(
            Div(
                Label(t("label.zip_archive"), cls="field-label"),
                Input(
                    type="file",
                    name="file",
                    accept=".zip,application/zip",
                    required=True,
                    cls="field-input",
                ),
                cls="field-group",
            ),
            Div(
                Label(
                    Input(type="checkbox", name="override_hero", value="1"),
                    f" {t('inv.override_hero_images')}",
                    cls="field-label",
                    style="display:flex;align-items:center;gap:6px;font-weight:normal;",
                ),
                cls="field-group",
                style="margin-bottom:8px;",
            ),
            Button(t("btn.upload_attach"), cls="btn btn--primary", type="submit"),
            hx_post="/settings/bulk-attach",
            hx_encoding="multipart/form-data",
            hx_target="#bulk-attach-result",
            hx_swap="outerHTML",
            hx_indicator="#bulk-attach-spinner",
        ),
        Span(t("settings.processing"), id="bulk-attach-spinner", cls="htmx-indicator"),
        Div(id="bulk-attach-result"),
        cls="settings-card",
    )


def _verticals_applied_panel(applied_names: list[str]) -> FT:
    """Render the applied-schemas panel (reused by HTMX category CRUD routes)."""
    if applied_names:
        applied_rows = [
            Tr(
                Td(
                    Span(name, cls="cat-name-display",
                         hx_get=f"/settings/categories/{name}/edit",
                         hx_target="closest tr",
                         hx_swap="outerHTML"),
                    cls="cell",
                ),
                Td(
                    A(t("settings.edit"), href=f"/settings/inventory?tab=category-library&cat={name}", cls="auth-link"),
                    cls="cell",
                ),
                Td(
                    Button("✕", cls="btn btn--danger btn--xs",
                           hx_delete=f"/settings/categories/{name}",
                           hx_target="closest tr",
                           hx_swap="outerHTML"),
                    cls="cell",
                ),
                cls="data-row",
            )
            for name in sorted(applied_names)
        ]
        add_row = Tr(
            Td(
                Form(
                    Input(type="text", name="new_category_name", placeholder=t("settings.new_category_name"),
                          cls="form-input form-input--sm cat-add-input"),
                    Button("Add Category", type="submit", cls="btn btn--secondary btn--sm"),
                    hx_post="/settings/categories",
                    hx_target="#vert-applied-panel",
                    hx_swap="outerHTML",
                    cls="cat-add-form",
                ),
                colspan="3", cls="cell",
            ),
        )
        applied_content = Table(
            Thead(Tr(Th(t("th.schema")), Th(""), Th(""))),
            Tbody(*applied_rows, add_row),
            cls="data-table",
        )
    else:
        applied_content = Div(
            P(t("settings.no_category_schemas_applied_yet"), cls="settings-hint"),
            Form(
                Input(type="text", name="new_category_name", placeholder=t("settings.new_category_name"),
                      cls="form-input form-input--sm cat-add-input"),
                Button("Add Category", type="submit", cls="btn btn--secondary btn--sm"),
                hx_post="/settings/categories",
                hx_target="#vert-applied-panel",
                hx_swap="outerHTML",
                cls="cat-add-form",
            ),
        )
    return Div(
        H3(t("page.applied_schemas"), cls="settings-section-title"),
        P(t("settings.these_schemas_are_active_on_your_inventory"), cls="settings-hint"),
        applied_content,
        cls="vert-applied-panel mt-xl",
        id="vert-applied-panel",
    )

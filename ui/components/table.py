# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import re

from fasthtml.common import *
from ui.i18n import t, get_lang

# Canonical empty-value placeholder (rule k)
EMPTY = "--"

# Statuses that dim a row to indicate it is not actively available for sale/use.
# Allowlist: adding a new status requires an explicit decision (mirrors fulfillment guard pattern).
INACTIVE_ITEM_STATUSES: frozenset[str] = frozenset({"archived", "expired", "sold", "memo_out"})

# Default column widths for fixed-layout tables.
# Keys are schema field keys; "_attr_default" applies to any column not listed here.
_DEFAULT_COL_WIDTHS: dict[str, str] = {
    "sku": "110px",
    "name": "200px",
    "barcode": "130px",
    "category": "130px",
    "quantity": "110px",
    "status": "90px",
    "weight": "100px",
    "weight_unit": "80px",
    "sell_by": "90px",
    "location_name": "140px",
    "pieces": "80px",
    "created_at": "120px",
    "updated_at": "120px",
    "_attr_default": "120px",
}


def format_value(v, fmt: str = "text", currency: str | None = None) -> str | FT:
    """Universal display formatter for table cells and detail pages.

    fmt: text | money | badge | date | weight
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return EMPTY
    if fmt == "money":
        try:
            return fmt_money(float(v), currency)
        except (ValueError, TypeError):
            return EMPTY
    if fmt == "badge":
        raw = str(v)
        key = raw.lower().replace(" ", "-").replace("_", "-")
        label = raw.replace("_", " ")
        return Span(label, cls=f"badge badge--{key}")
    if fmt == "date":
        s = str(v)[:10] if v else ""
        return s or EMPTY
    if fmt == "number":
        try:
            n = float(v)
            return str(int(n)) if n == int(n) else f"{n:g}"
        except (ValueError, TypeError):
            return str(v)
    if fmt == "weight":
        s = str(v).strip()
        return Span(f"{s} ct", cls="cell-weight") if s else Span(EMPTY)
    return str(v)

# Threshold above which a select must become searchable (UI/UX rule i)
_SEARCHABLE_THRESHOLD = 10

# Colors supported by status_cards (maps to CSS modifier class)
_STATUS_CARD_COLORS = {"green", "yellow", "red", "blue", "gray"}

# ISO 4217 → symbol map for money formatting
_CURRENCY_SYMBOLS: dict[str, str] = {
    "AED": "AED ", "AUD": "A$", "BDT": "৳", "BRL": "R$", "CAD": "C$",
    "CHF": "CHF ", "CLP": "$", "CNY": "¥", "COP": "$", "CZK": "Kč ",
    "DKK": "kr ", "EGP": "E£", "EUR": "€", "GBP": "£", "HKD": "HK$",
    "HUF": "Ft ", "IDR": "Rp ", "ILS": "₪", "INR": "₹", "JPY": "¥",
    "KRW": "₩", "KWD": "KD ", "MXN": "$", "MYR": "RM ", "NGN": "₦",
    "NOK": "kr ", "NZD": "NZ$", "PEN": "S/", "PHP": "₱", "PKR": "₨ ",
    "PLN": "zł ", "QAR": "QR ", "RON": "lei ", "RUB": "₽", "SAR": "SR ",
    "SEK": "kr ", "SGD": "S$", "THB": "฿", "TRY": "₺", "TWD": "NT$",
    "UAH": "₴", "USD": "$", "VND": "₫", "ZAR": "R ",
}


def currency_symbol(currency: str | None) -> str:
    """Return the display symbol for an ISO 4217 currency code. Falls back to code + space."""
    if not currency:
        return ""
    return _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")


def fmt_money(value: str | float, currency: str | None = None) -> str:
    """Format a numeric value as a currency string."""
    sym = currency_symbol(currency)
    try:
        return f"{sym}{float(value):,.2f}"
    except (ValueError, TypeError):
        return EMPTY


def status_cards(cards: list[dict], base_url: str, active_status: str | None = None, total_override: int | None = None, currency: str | None = None, show_all_card: bool = True, all_label: str = "All") -> FT:
    """Clickable status filter cards at top of list pages.

    cards: [{"label": "Paid", "count": 489, "total": 2990000.0, "status": "paid", "color": "green"}, ...]
    Clicking a card navigates to base_url?status=<status>.
    Cards may include a "_url" key to override the generated href entirely.
    Cards may include "_active_key" to override which value is compared against active_status.
    "All" card (status=None/"") is prepended unless show_all_card=False.
    total_override: if provided, the "All" card shows this count instead of summing cards.
    """
    def _card(label: str, count: int, total: float | None, status: str | None, color: str, href_override: str | None = None, active_key: str | None = None) -> FT:
        _cmp = active_key if active_key is not None else (status or "")
        is_active = (active_status or "") == _cmp
        color_cls = color if color in _STATUS_CARD_COLORS else "gray"
        cls = f"status-card status-card--{color_cls}" + (" status-card--active" if is_active else "")
        if href_override:
            href = href_override
        elif not status:
            href = base_url
        else:
            href = f"{base_url}{'&' if '?' in base_url else '?'}status={status}"
        inner = [
            Span(label, cls="status-card-label"),
            Span(str(count), cls="status-card-count"),
        ]
        if total is not None:
            inner.append(Span(fmt_money(total, currency), cls="status-card-total"))
        return A(*inner, href=href, cls=cls)

    # Ensure "All" card is first (optional)
    all_total = total_override if total_override is not None else sum(c.get("count", 0) for c in cards)
    els = [_card(all_label, all_total, None, None, "blue")] if show_all_card else []
    for c in cards:
        els.append(_card(
            c.get("label", ""),
            c.get("count", 0),
            c.get("total"),
            c.get("status"),
            c.get("color", "gray"),
            href_override=c.get("_url"),
            active_key=c.get("_active_key"),
        ))
    return Div(*els, cls="status-cards")


def empty_state_cta(
    message: str,
    action_label: str | None = None,
    action_url: str | None = None,
    hx_post: bool = False,
) -> FT:
    """Centered card with message + optional action button for empty pages."""
    inner: list[FT] = [P(message, cls="empty-state-cta-msg")]
    if action_label:
        if hx_post and action_url:
            inner.append(
                Button(action_label, hx_post=action_url, hx_swap="none", cls="empty-state-cta-btn")
            )
        elif action_url:
            inner.append(A(action_label, href=action_url, cls="empty-state-cta-btn"))
    return Div(*inner, cls="empty-state-cta")


def searchable_select(
    name: str,
    options: list[str | tuple[str, str]],
    value: str = "",
    placeholder: str = "Search or select...",
    cls_extra: str = "",
    allow_custom: bool = False,
    search_url: str = "",
    **htmx_attrs,
) -> FT:
    """
    Combobox-style searchable select for datasets with >10 options (rule i).
    Renders a visible text input + hidden value input + floating option list.
    Works with the initCombobox() JS in shell.py (no build step).

    options: list of strings OR list of (value, label) tuples.
    allow_custom: if True, user can type a value not in the list (saved as-is).
    search_url: if set, enables server-side search. The text input sends hx-get
        requests to this URL with ?q=<typed>, replacing the option list via HTMX.
        When q is empty the JS restores the original static options.
    htmx_attrs: HTMX attributes forwarded to the hidden input (hx_get, hx_target, etc.)
    """
    normalized = [
        (o, o) if isinstance(o, str) else (o[0], o[1])
        for o in options
    ]
    # Current label for display
    display_label = next((lbl for val, lbl in normalized if val == value), value)

    def _opt_cls(val: str) -> str:
        # "__"-prefixed values are action options (add-new, scope toggles): pinned so they
        # survive typing/filtering; "__new__" additionally gets the add-new styling.
        cls = "combobox-option"
        if val.startswith("__"):
            cls += " combobox-option--pinned"
        if val.startswith("__new__"):
            cls += " combobox-option--new"
        return cls

    opt_els = [Div(label, cls=_opt_cls(val), data_value=val) for val, label in normalized]
    opt_els.append(Div(t("msg.no_results"), cls="combobox-option combobox-option--empty", style="display:none"))

    wrap_attrs: dict = {"cls": "combobox-wrap"}
    if allow_custom:
        wrap_attrs["data_allow_custom"] = "true"
    if search_url:
        wrap_attrs["data_search_url"] = "1"
        wrap_attrs["hx_get"] = search_url
        wrap_attrs["hx_trigger"] = "input from:.combobox-input changed delay:300ms"
        wrap_attrs["hx_target"] = "find .combobox-list"
        wrap_attrs["hx_swap"] = "innerHTML"
        wrap_attrs["hx_include"] = "find .combobox-input"

    text_input_extra: dict = {}
    if search_url:
        text_input_extra["name"] = "q"

    return Div(
        Input(type="text", cls=f"combobox-input {cls_extra}".strip(),
              value=display_label, placeholder=placeholder, autocomplete="off", **text_input_extra),
        Input(type="hidden", name=name, data_name=name, value=value, **htmx_attrs),
        Div(*opt_els, cls="combobox-list"),
        **wrap_attrs,
    )


def paired_display_cell(
    entity_id: str,
    primary_field: str,
    primary_value,
    secondary_field: str,
    secondary_value,
    primary_type: str = "text",
    secondary_type: str = "text",
    primary_options: list[str] | None = None,
    secondary_options: list[str] | None = None,
    format_fn=None,
) -> FT:
    """Combined cell showing two separately dbl-click-editable values in one TD.

    Used for quantity+sell_by and weight+weight_unit so they share a column.
    Each span is independently double-click-to-edit via the paired-edit endpoint,
    which returns an editable_cell whose restore_url points back to paired-display.

    format_fn: optional callable(value) -> str for formatting the primary value.
    When provided, it is used instead of str(). Callers supply unit-aware formatters
    (e.g. format_qty) without coupling this generic component to inventory logic.
    """
    pri_edit = f"/api/items/{entity_id}/field/{primary_field}/paired-edit?peer={secondary_field}"
    sec_edit = f"/api/items/{entity_id}/field/{secondary_field}/paired-edit?peer={primary_field}"
    if primary_value not in (None, ""):
        pri_disp = format_fn(primary_value) if format_fn is not None else str(primary_value)
    else:
        pri_disp = EMPTY
    sec_disp = str(secondary_value) if secondary_value not in (None, "") else EMPTY
    return Td(
        Span(
            pri_disp,
            cls="paired-primary",
            title="Double-click to edit",
            hx_get=pri_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        ),
        Span(" ", cls="paired-sep"),
        Span(
            sec_disp,
            cls="paired-secondary",
            title="Double-click to edit",
            hx_get=sec_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        ),
        cls=f"cell cell--paired",
        data_col=primary_field,
    )


def purchase_display_cell(
    entity_id: str,
    pu_val,
    cf_val,
    sb_val,
) -> FT:
    """Cell showing purchase_unit → cf_val sell_by (tertiary read-only).

    purchase_unit and purchase_conversion_factor are dbl-click editable;
    sell_by is read-only in this column (edit via the Qty column).
    """
    pu_edit = f"/api/items/{entity_id}/field/purchase_unit/paired-edit"
    cf_edit = f"/api/items/{entity_id}/field/purchase_conversion_factor/paired-edit"
    pu_disp = str(pu_val) if pu_val not in (None, "") else EMPTY
    cf_disp = str(cf_val) if cf_val not in (None, "") else EMPTY
    sb_disp = str(sb_val) if sb_val not in (None, "") else EMPTY
    return Td(
        Span(
            pu_disp,
            cls="paired-primary",
            title="Double-click to edit",
            hx_get=pu_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        ),
        Span(" → ", cls="paired-sep", title="Purchasing unit conversion to stock unit"),
        Span(
            cf_disp,
            cls="paired-secondary",
            title="Double-click to edit",
            hx_get=cf_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        ),
        Span(" ", cls="paired-sep"),
        Span(sb_disp, cls="paired-tertiary cell-readonly"),
        cls="cell cell--paired",
        data_col="purchase_unit",
    )


def editable_cell(
    entity_id: str,
    field: str,
    value,
    cell_type: str = "text",
    options: list[str] | None = None,
    allow_custom: bool = False,
    restore_url: str | None = None,
    label_map: dict | None = None,
) -> FT:
    """Table cell in edit mode. Fires HTMX PATCH on blur/change, swaps itself back to display_cell.
    label_map: optional {slug: display_name} - if set, select renders option labels from map."""
    display_val = str(value) if value is not None else ""
    if cell_type == "number" and display_val:
        display_val = _normalize_number_str(display_val)
    patch_url = f"/api/items/{entity_id}/field/{field}"
    restore_url = restore_url or f"/api/items/{entity_id}/field/{field}/display"
    swap = dict(hx_patch=patch_url, hx_target="closest td", hx_swap="outerHTML", hx_include="this")
    # Apply label_map to options for selects
    if options is not None and label_map:
        options = [(o, label_map.get(o, o)) for o in options]
    # ESC cancel: prevent onblur from also firing by setting a flag before removing focus.
    # Enter: trigger blur to save.
    # ESC: capture scroll position synchronously at keydown (before browser may reset it),
    # then force-set _scrollSnap so the global htmx:afterSettle handler restores it.
    escape_js = (
        f"if(event.key==='Escape'){{"
        f"var _sw=document.querySelector('.table-scroll-wrap');"
        f"if(_sw&&window.__celerpScrollSnap!==undefined){{window.__celerpScrollSnap=_sw.scrollLeft;}}"
        f"this._escaping=true;"
        f"htmx.ajax('GET','{restore_url}',{{target:this.closest('td'),swap:'outerHTML'}});"
        f"event.preventDefault();}}"
        f"else if(event.key==='Enter'){{event.preventDefault();htmx.trigger(this,'blur');}}"
    )
    blur_restore_js = f"if(!this._escaping){{htmx.ajax('GET','{restore_url}',{{target:this.closest('td'),swap:'outerHTML'}})}}"
    # ESC handler for combobox wrapper (keydown bubbles up from the inner input)
    combobox_escape_js = (
        f"if(event.key==='Escape'){{"
        f"var _sw=document.querySelector('.table-scroll-wrap');"
        f"if(_sw&&window.__celerpScrollSnap!==undefined){{window.__celerpScrollSnap=_sw.scrollLeft;}}"
        f"htmx.ajax('GET','{restore_url}',{{target:this.closest('td'),swap:'outerHTML'}});"
        f"event.preventDefault();}}"
    )

    if cell_type in ("select", "status") and options is not None:
        if len(options) > _SEARCHABLE_THRESHOLD or allow_custom:
            # Searchable combobox for large option sets or allow-custom fields
            input_el = Div(
                searchable_select(
                    name="value",
                    options=options,
                    value=display_val,
                    allow_custom=allow_custom,
                    hx_patch=patch_url,
                    hx_target="closest td",
                    hx_swap="outerHTML",
                    hx_trigger="change",
                ),
                cls="cell-input-wrap",
                onkeydown=combobox_escape_js,
            )
        else:
            _opt_items = [(o, o) if isinstance(o, str) else o for o in options]
            input_el = Select(
                *([] if display_val else [Option("", value="", disabled=True, selected=True)]),
                *[Option(lbl, value=val, selected=(val == display_val)) for val, lbl in _opt_items],
                name="value",
                **swap,
                hx_trigger="change",
                cls=f"cell-input cell-input--{cell_type}",
                autofocus=True,
                onkeydown=escape_js,
                onblur=blur_restore_js,
            )
    elif cell_type in ("money", "weight"):
        step = "0.01" if cell_type == "money" else "0.001"
        input_el = Input(
            type="number", name="value", value=display_val, step=step,
            **swap,
            hx_trigger="blur delay:200ms",
            cls="cell-input cell-input--number",
            autofocus=True,
            onkeydown=escape_js,
        )
    elif cell_type == "bool":
        # Toggle: send "true"/"false" on change
        is_true = display_val.lower() in ("true", "1", "yes")
        input_el = Select(
            Option(t("settings.no"), value="false", selected=not is_true),
            Option(t("settings.yes"), value="true", selected=is_true),
            name="value",
            **swap,
            hx_trigger="change",
            cls="cell-input cell-input--select",
            autofocus=True,
            onkeydown=escape_js,
            onblur=blur_restore_js,
        )
    else:
        input_el = Input(
            type="text", name="value", value=display_val,
            **swap,
            hx_trigger="blur delay:200ms",
            cls="cell-input",
            autofocus=True,
            onkeydown=escape_js,
        )

    return Td(input_el, cls=f"cell cell--editing cell--{cell_type}")


def _normalize_number_str(s: str) -> str:
    """Format a numeric string for display: drop .0 for integers, use :g for floats."""
    try:
        n = float(s)
        return str(int(n)) if n == int(n) else f"{n:g}"
    except (ValueError, TypeError):
        return s


def _display_val(value, cell_type: str, currency: str | None = None) -> FT:
    """Format a value for display. Empty/null → EMPTY constant."""
    s = str(value).strip() if value is not None else ""
    if cell_type == "bool":
        is_true = str(value).strip().lower() in ("true", "1", "yes")
        return Span("Yes" if is_true else "No", cls="badge badge--yes" if is_true else "badge badge--no")
    if cell_type == "status":
        return Span(s or EMPTY, cls=f"badge badge--{s.lower().replace(' ', '-')}" if s else "")
    if cell_type == "money":
        try:
            return Span(fmt_money(s, currency), cls="cell-money") if s else Span(EMPTY)
        except ValueError:
            return Span(EMPTY)
    if cell_type == "number":
        if not s:
            return Span(EMPTY)
        return Span(_normalize_number_str(s), cls="cell-number")
    if cell_type == "weight":
        return Span(f"{s} ct", cls="cell-weight") if s else Span(EMPTY)
    if cell_type == "tags":
        tags = value if isinstance(value, list) else []
        return Span(*[Span(t, cls="tag-pill tag-pill--sm") for tag in tags]) if tags else Span(EMPTY)
    if cell_type == "image":
        if s:
            return Img(src=s, cls="cell-thumbnail", loading="lazy", alt="")
        return Span("＋", cls="cell-image-empty", title="Drop image here or click to upload")
    return Span(s or EMPTY, cls="cell-text")


def display_cell(
    entity_id: str,
    field: str,
    value,
    cell_type: str = "text",
    options: list[str] | None = None,
    editable: bool = True,
    currency: str | None = None,
    link_href: str | None = None,
    edit_url: str | None = None,
    label_map: dict | None = None,
) -> FT:
    """Read-only cell. Double-click-to-edit fires HTMX GET to fetch editable_cell.
    Image cells support drag-and-drop upload in addition to click.
    link_href: if set, renders cell value as a clickable hyperlink (e.g. SKU -> detail page).
    edit_url: custom HTMX GET URL for editing this cell. Overrides the default
              ``/api/items/{entity_id}/field/{field}/edit`` pattern.
    label_map: optional {slug: display_name} dict - if set, display_val shows the mapped name."""
    display_value = label_map.get(value, value) if label_map and value is not None else value
    inner = _display_val(display_value, cell_type, currency)
    _edit = edit_url or f"/api/items/{entity_id}/field/{field}/edit"
    _safe_id = entity_id.replace(":", "-")
    _safe_field = re.sub(r"[^A-Za-z0-9_-]", "_", field)
    _cell_id = f"cell-{_safe_id}-{_safe_field}"

    if not editable:
        # Only render hyperlink when there's actual content (not empty/placeholder)
        if link_href and value is not None and str(value).strip() and str(value).strip() != EMPTY:
            return Td(A(inner, href=link_href, cls="table-link"), id=_cell_id, cls=f"cell cell--{cell_type}", data_col=field)
        return Td(inner, id=_cell_id, cls=f"cell cell--{cell_type}", data_col=field)

    if cell_type == "image":
        # Drag-drop zone: dropping a file POSTs to the attachment endpoint.
        # A hidden file input allows click-to-upload as fallback.
        return Td(
            inner,
            Input(
                type="file",
                accept="image/*",
                cls="cell-image-input",
                hx_post=f"/api/items/{entity_id}/attachments",
                hx_encoding="multipart/form-data",
                hx_target=f"#img-cell-{entity_id.replace(':', '-')}",
                hx_swap="outerHTML",
                style="display:none",
                id=f"img-input-{entity_id.replace(':', '-')}",
            ),
            id=f"img-cell-{entity_id.replace(':', '-')}",
            cls="cell cell--image cell--droppable",
            data_entity_id=entity_id,
            data_col=field,
            title="Drag & drop image or click to upload",
        )

    if link_href and value is not None and str(value).strip() and str(value).strip() != EMPTY:
        return Td(
            A(inner, href=link_href, cls="table-link"),
            id=_cell_id,
            title="Double-click to edit",
            hx_get=_edit,
            hx_target="this",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
            cls=f"cell cell--{cell_type} cell--clickable",
            data_col=field,
        )

    if field == "sku":
        return Td(
            A(inner, href=f"/inventory/{entity_id}", cls="table-link"),
            id=_cell_id,
            title="Double-click to edit",
            hx_get=_edit,
            hx_target="this",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
            cls=f"cell cell--{cell_type} cell--clickable",
            data_col=field,
        )

    return Td(
        inner,
        id=_cell_id,
        title="Double-click to edit",
        hx_get=_edit,
        hx_target="this",
        hx_swap="outerHTML",
        hx_trigger="dblclick",
        cls=f"cell cell--{cell_type} cell--clickable",
        data_col=field,
    )


def data_table(
    schema: list[dict],
    rows: list[dict],
    entity_type: str = "item",
    show_cols: list[str] | None = None,
    sort_key: str = "",
    sort_dir: str = "desc",
    sort_url: str = "",
    extra_params: dict | None = None,
    currency: str | None = None,
    sort_target: str = "#data-table",
    q: str | None = None,
    show_row_menu: bool = True,
    show_checkboxes: bool = True,
    selection_key: str = "celerp_inv_selection",
    link_fn: dict[str, str] | None = None,
    auto_hide_empty: bool = True,
    edit_url_tpl: str | None = None,
    delete_url_tpl: str | None = None,
    cell_renderers: dict | None = None,
    hidden_fields: set | None = None,
) -> FT:
    """
    Dynamic spreadsheet table. Headers from schema (never hardcoded), rows from API.
    sort_key/sort_dir: current sort state for column header indicators.
    sort_target: HTMX swap target for sort header clicks. Use '#inventory-content' to
                 refresh tabs+cards+table together; use '#data-table' for table-only refreshes.
    sort_url: base URL for sort links (e.g. /inventory/search).
    q: active search query; when non-empty and rows is empty, shows a targeted "no results" message.
    show_checkboxes: render row-select checkboxes (default True). Set False for tables without bulk actions.
    link_fn: dict mapping field keys to URL templates with ``{id}`` placeholder
             (e.g. ``{"name": "/contacts/{id}"}``). Matched cells render as hyperlinks.
    auto_hide_empty: auto-hide columns where >80% of cells are empty (default True).
                     Set False when schema already defines the right visible set.
    edit_url_tpl: URL template for cell editing, with ``{id}`` and ``{field}`` placeholders
                  (e.g. ``"/contacts/{id}/field/{field}/edit"``). Overrides the default
                  ``/api/items/{id}/field/{field}/edit`` for all editable cells.
    delete_url_tpl: URL template for row-menu delete, with ``{entity_id}`` placeholder
                    (e.g. ``"/api/items/{entity_id}"``). Defaults to ``/api/items/{entity_id}``.
    """
    # Render ALL schema columns server-side.
    # show_cols only controls the INITIAL JS visibility state (not what HTML is rendered).
    # This ensures the column manager can show any column without a round-trip,
    # and page 2 / HTMX navigation retains all columns.
    visible = list(schema)
    # If show_cols provided, put those first (in declared order), extras follow
    if show_cols:
        ordered = [f for key in show_cols for f in schema if f["key"] == key]
        rest = [f for f in schema if f["key"] not in show_cols]
        visible = ordered + rest
    # Drop fields that are rendered inside another cell (paired secondaries etc.)
    if hidden_fields:
        visible = [f for f in visible if f["key"] not in hidden_fields]
    if not rows:
        if q and q.strip():
            return Div(
                P(f"No results for '{q.strip()}'", cls="search-empty--table"),
                cls="empty-state",
                id="data-table",
            )
        return Div(
            P(t("msg.no_items"), cls="empty-state-msg"),
            cls="empty-state",
            id="data-table",
        )

    def _th(f: dict) -> FT:
        key = f["key"]
        default_width = _DEFAULT_COL_WIDTHS.get(key, _DEFAULT_COL_WIDTHS["_attr_default"])
        th_style = f"width:{default_width}"
        if sort_url:
            params = {**(extra_params or {}), "sort": key}
            new_dir = "asc" if (sort_key == key and sort_dir == "desc") else "desc"
            params["dir"] = new_dir
            query = "&".join(f"{k}={v}" for k, v in params.items() if v not in ("", None))
            indicator = ""
            if sort_key == key:
                indicator = " ▲" if sort_dir == "asc" else " ▼"
            return Th(
                A(f["label"], indicator, href="#",
                  hx_get=f"{sort_url}?{query}" if query else sort_url,
                  hx_target=sort_target,
                  hx_swap="outerHTML",
                  hx_push_url="true",
                  cls="sort-link"),
                cls=f"col-{key}", data_key=key, draggable="true",
                title="Drag to reorder columns",
                style=th_style,
            )
        return Th(f["label"], cls=f"col-{key}", data_key=key, draggable="true",
                   title="Drag to reorder columns", style=th_style)

    checkbox_th = [Th(Input(type="checkbox", id="select-all-rows", title="Select all"), cls="col-checkbox")] if show_checkboxes else []
    header = Thead(Tr(
        *checkbox_th,
        *[_th(f) for f in visible],
        *([] if not show_row_menu else [Th("", cls="col-actions")]),
    ))

    def _row(row: dict) -> FT:
        entity_id = row.get("id") or row.get("entity_id", "")
        safe_id = entity_id.replace(":", "-")
        _delete_url = (delete_url_tpl or "/api/items/{entity_id}").format(entity_id=entity_id)
        action_cell = [] if not show_row_menu else [
            Td(
                Div(
                    Button("⋮", cls="row-menu-btn", onclick=f"toggleRowMenu('{safe_id}')"),
                    Div(
                        A(t("btn.edit"), href=f"/{entity_type}/{entity_id}", cls="row-menu-item"),
                        Button(t("btn.delete"), cls="row-menu-item row-menu-item--danger",
                               onclick=f"if(!confirm('Delete this item? This cannot be undone.'))return;"
                                       f"htmx.ajax('DELETE','{_delete_url}',"
                                       f"{{target:'#row-{safe_id}',swap:'outerHTML'}})"),
                        cls="row-menu-dropdown", id=f"menu-{safe_id}",
                    ),
                    cls="row-menu",
                ),
                cls="col-actions",
            )
        ]
        checkbox_td = [Td(Input(type="checkbox", cls="row-select", name="selected", value=entity_id,
                     data_entity_id=entity_id,
                     data_sku=row.get("sku", ""),
                     data_name=row.get("name", ""),
                     data_qty=str(row.get("quantity", 0)),
                     data_weight=str(row.get("weight", "") or ""),
                     data_weight_unit=row.get("weight_unit", ""),
                     data_sell_by=row.get("sell_by", ""),
               ), cls="col-checkbox")] if show_checkboxes else []
        status_val = str(row.get("status", "") or "").lower()
        row_cls = "data-row data-row--inactive" if status_val in INACTIVE_ITEM_STATUSES else "data-row"
        if str(row.get("inventory_type") or "") == "component":
            row_cls += " data-row--component"  # visual cue for component (raw-material) items
        return Tr(
            *checkbox_td,
            *[
                cell_renderers[f["key"]](entity_id, row) if cell_renderers and f["key"] in cell_renderers
                else display_cell(
                    entity_id=entity_id,
                    field=f["key"],
                    value=row.get(f["key"], ""),
                    cell_type=f.get("type", "text"),
                    options=f.get("options"),
                    editable=f.get("editable", True),
                    currency=currency,
                    link_href=(link_fn[f["key"]].format(id=entity_id) if link_fn and f["key"] in link_fn else None),
                    edit_url=(edit_url_tpl.format(id=entity_id, field=f["key"]) if edit_url_tpl else None),
                )
                for f in visible
            ],
            *action_cell,
            id=f"row-{safe_id}",
            cls=row_cls,
        )

    # JS: smart column defaults + localStorage persistence + drag-to-resize
    import json as _json
    page_key = f"celerp_cols_{entity_type}"
    # Default visibility: show_cols if provided (user/saved selection), else schema show_in_table.
    # Columns not in show_cols start hidden but are fully rendered in DOM so JS can toggle them.
    if show_cols:
        _schema_defaults = {f["key"]: (f["key"] in show_cols) for f in visible}
    else:
        _schema_defaults = {f["key"]: f.get("show_in_table", True) for f in visible}
    # Map primary_key → [virtual_key, ...] so drag and restore both move virtual cols with their primary.
    # Virtual total always follows its paired unit-price column (the primary).
    _virtual_followers: dict[str, list[str]] = {}
    for f in schema:
        if f.get("virtual") and f.get("paired_with"):
            paired = f["paired_with"]
            key = f["key"]
            _virtual_followers.setdefault(paired, []).append(key)
    _js = f"""
(function(){{
  var PAGE_KEY = '{page_key}';
  var ORDER_KEY = 'celerp_col_order_{entity_type}';
  var SCHEMA_DEFAULTS = {_json.dumps(_schema_defaults)};
  // Virtual columns that must move with their primary (e.g. cost_price_total follows cost_price)
  var VIRTUAL_FOLLOWERS = {_json.dumps(_virtual_followers)};
  var table = document.getElementById('data-table');
  if (!table) return;
  var ths = Array.from(table.querySelectorAll('thead th[data-key]'));
  var rows = Array.from(table.querySelectorAll('tbody tr.data-row'));

  var AUTO_HIDE = {'true' if auto_hide_empty else 'false'};

  // Detect empty columns (>80% null/dash cells in tbody)
  var colEmpty = {{}};
  if (AUTO_HIDE) {{
    ths.forEach(function(th, idx) {{
      var key = th.dataset.key;
      var total = rows.length;
      if (total === 0) return;
      var col_idx = Array.from(th.parentNode.children).indexOf(th);
      var empty = rows.filter(function(tr) {{
        var td = tr.cells[col_idx];
        if (!td) return true;
        var txt = td.textContent.trim();
        return !txt || txt === '--';
      }}).length;
      colEmpty[key] = total > 0 && (empty / total > 0.8);
    }});
  }}

  // Load persisted prefs or compute smart defaults
  var prefs;
  try {{ prefs = JSON.parse(localStorage.getItem(PAGE_KEY) || 'null'); }} catch(e) {{ prefs = null; }}
  if (!prefs) {{
    prefs = {{}};
    ths.forEach(function(th) {{
      prefs[th.dataset.key] = AUTO_HIDE ? !colEmpty[th.dataset.key] : (SCHEMA_DEFAULTS[th.dataset.key] !== false);
    }});
  }} else {{
    // Merge: columns not in stored prefs get their schema default
    ths.forEach(function(th) {{
      if (!(th.dataset.key in prefs)) {{
        prefs[th.dataset.key] = SCHEMA_DEFAULTS[th.dataset.key] !== false;
      }}
    }});
  }}

  // Apply visibility — accept optional live table so post-swap calls use the new DOM node
  function applyVis(liveTable) {{
    liveTable = liveTable || table;
    // Re-read prefs from localStorage so post-swap calls reflect changes made in
    // the column-manager dropdown (which writes to the same PAGE_KEY but doesn't
    // share the in-memory `prefs` variable from this IIFE closure).
    try {{ prefs = JSON.parse(localStorage.getItem(PAGE_KEY) || 'null') || prefs; }} catch(e) {{}}
    var liveRows = Array.from(liveTable.querySelectorAll('tbody tr.data-row'));
    var liveThs = Array.from(liveTable.querySelectorAll('thead th[data-key]'));
    liveThs.forEach(function(th) {{
      var key = th.dataset.key;
      var show = prefs[key] !== false;
      th.style.display = show ? '' : 'none';
      liveRows.forEach(function(tr) {{
        var td = tr.querySelector('[data-col="' + key + '"]');
        if (td) td.style.display = show ? '' : 'none';
      }});
    }});
    localStorage.setItem(PAGE_KEY, JSON.stringify(prefs));
  }}
  applyVis();
  // Re-apply after HTMX settles so rows added during the swap phase are also covered.
  // Guard with a global flag so repeated IIFE executions (after HTMX swaps) don't
  // accumulate duplicate listeners on document.body.
  var _VIS_SETTLE_KEY = '__celerpVisSettle_{entity_type}';
  if (!window[_VIS_SETTLE_KEY]) {{
    window[_VIS_SETTLE_KEY] = true;
    document.body.addEventListener('htmx:afterSettle', function(e) {{
      if (e.detail && e.detail.target && e.detail.target.id === 'inventory-content') {{
        // Re-query the live table after each settle - the old `table` ref may be detached
        var liveTable = document.getElementById('data-table');
        if (liveTable) applyVis(liveTable);
      }}
    }});
  }}

  // Drag-to-resize column headers — persist widths to localStorage
  var WIDTH_KEY = 'celerp_col_widths_{entity_type}';
  function loadWidths() {{
    try {{ return JSON.parse(localStorage.getItem(WIDTH_KEY) || 'null'); }} catch(e) {{ return null; }}
  }}
  function saveWidths() {{
    var w = {{}};
    ths.forEach(function(th) {{ if (th.style.width) w[th.dataset.key] = th.style.width; }});
    localStorage.setItem(WIDTH_KEY, JSON.stringify(w));
  }}
  // Restore persisted widths on load — defer to rAF so HTMX-swapped DOM has committed layout
  requestAnimationFrame(function() {{
    var saved = loadWidths();
    if (saved) ths.forEach(function(th) {{ if (saved[th.dataset.key]) th.style.width = saved[th.dataset.key]; }});
  }});
  ths.forEach(function(th) {{
    var handle = document.createElement('div');
    handle.className = 'col-resize-handle';
    th.style.position = 'relative';
    th.appendChild(handle);
    var startX, startW;
    handle.addEventListener('mousedown', function(e) {{
      startX = e.pageX;
      startW = th.offsetWidth;
      e.preventDefault();
      e.stopPropagation();
      var scrollWrap = table.closest('.table-scroll-wrap');
      var autoScrollRaf = null;
      function onMove(e2) {{
        e2.preventDefault();
        th.style.width = Math.max(40, startW + e2.pageX - startX) + 'px';
        // Auto-scroll the container when dragging near the right edge
        if (scrollWrap) {{
          var rect = scrollWrap.getBoundingClientRect();
          var ZONE = 48; // px from edge to trigger auto-scroll
          if (autoScrollRaf) cancelAnimationFrame(autoScrollRaf);
          if (e2.clientX > rect.right - ZONE) {{
            var speed = Math.round((e2.clientX - (rect.right - ZONE)) / ZONE * 12) + 2;
            (function scroll() {{
              scrollWrap.scrollLeft += speed;
              autoScrollRaf = requestAnimationFrame(scroll);
            }})();
          }}
        }}
      }}
      function onUp() {{
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        if (autoScrollRaf) cancelAnimationFrame(autoScrollRaf);
        saveWidths();
      }}
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    }});
  }});

  // Drag-and-drop column reorder via HTML5 drag on <th>
  var dragKey = null;
  ths.forEach(function(th) {{
    th.addEventListener('dragstart', function(e) {{
      dragKey = th.dataset.key;
      e.dataTransfer.effectAllowed = 'move';
      th.classList.add('col-dragging');
    }});
    th.addEventListener('dragend', function() {{
      th.classList.remove('col-dragging');
      dragKey = null;
      table.querySelectorAll('th[data-key]').forEach(function(t) {{ t.classList.remove('col-drag-over'); }});
    }});
    th.addEventListener('dragover', function(e) {{
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      table.querySelectorAll('th[data-key]').forEach(function(t) {{ t.classList.remove('col-drag-over'); }});
      th.classList.add('col-drag-over');
    }});
    th.addEventListener('drop', function(e) {{
      e.preventDefault();
      th.classList.remove('col-drag-over');
      if (!dragKey || dragKey === th.dataset.key) return;
      // Resolve the canonical group: if dragging a virtual, treat its primary as the drag source
      var effectiveDragKey = dragKey;
      for (var pk in VIRTUAL_FOLLOWERS) {{
        if (VIRTUAL_FOLLOWERS[pk].indexOf(dragKey) !== -1) {{ effectiveDragKey = pk; break; }}
      }}
      // Move TH (and any virtual followers) before the drop target
      var thead_tr = table.querySelector('thead tr');
      var srcTh = thead_tr.querySelector('th[data-key="' + effectiveDragKey + '"]');
      if (!srcTh) return;
      thead_tr.insertBefore(srcTh, th);
      // Place virtual followers immediately after the primary
      (VIRTUAL_FOLLOWERS[effectiveDragKey] || []).forEach(function(vk) {{
        var vth = thead_tr.querySelector('th[data-key="' + vk + '"]');
        if (vth) srcTh.insertAdjacentElement('afterend', vth);
      }});
      // Re-order body cells to match header
      var allThs = Array.from(thead_tr.children);
      table.querySelectorAll('tbody tr.data-row').forEach(function(tr) {{
        var cells = Array.from(tr.children);
        var newOrder = allThs.map(function(h) {{
          var k = h.dataset.key;
          if (!k) return null;
          return cells.find(function(td) {{ return td.dataset.col === k; }});
        }}).filter(Boolean);
        // Preserve fixed columns (checkbox, actions) by class
        var checkboxTd = tr.querySelector('.col-checkbox');
        var actionsTd = tr.querySelector('.col-actions');
        var dataTds = newOrder.filter(function(td) {{
          return td && td !== checkboxTd && td !== actionsTd;
        }});
        var ordered = [];
        if (checkboxTd) ordered.push(checkboxTd);
        ordered = ordered.concat(dataTds);
        if (actionsTd) ordered.push(actionsTd);
        ordered.forEach(function(td) {{ tr.appendChild(td); }});
      }});
      // Persist new order and notify picker
      var newOrder = Array.from(thead_tr.querySelectorAll('th[data-key]')).map(function(h){{return h.dataset.key;}});
      try {{ localStorage.setItem(ORDER_KEY, JSON.stringify(newOrder)); }} catch(e) {{}}
      document.dispatchEvent(new CustomEvent('celerp:col-reorder', {{detail: {{order: newOrder}}}}));
      dragKey = null;
    }});
  }});

  // Apply persisted column order on page load
  var storedOrder;
  try {{ storedOrder = JSON.parse(localStorage.getItem(ORDER_KEY) || 'null'); }} catch(e) {{ storedOrder = null; }}
  if (storedOrder && storedOrder.length) {{
    var thead_tr = table.querySelector('thead tr');
    var actionsTh = thead_tr.querySelector('.col-actions');
    storedOrder.forEach(function(key) {{
      var th2 = thead_tr.querySelector('th[data-key="' + key + '"]');
      if (th2 && actionsTh) thead_tr.insertBefore(th2, actionsTh);
      // Place virtual followers immediately after their primary
      (VIRTUAL_FOLLOWERS[key] || []).forEach(function(vk) {{
        var vth = thead_tr.querySelector('th[data-key="' + vk + '"]');
        if (vth && actionsTh) thead_tr.insertBefore(vth, actionsTh);
      }});
    }});
    // Re-order tbody to match new header order
    var allThs2 = Array.from(thead_tr.querySelectorAll('th[data-key]'));
    table.querySelectorAll('tbody tr.data-row').forEach(function(tr) {{
      var cells = Array.from(tr.children);
      var checkboxTd = tr.querySelector('.col-checkbox');
      var actionsTd = tr.querySelector('.col-actions');
      var dataCells = allThs2.map(function(th2) {{
        return cells.find(function(td) {{ return td.dataset.col === th2.dataset.key; }});
      }}).filter(Boolean);
      var ordered = [];
      if (checkboxTd) ordered.push(checkboxTd);
      ordered = ordered.concat(dataCells);
      if (actionsTd) ordered.push(actionsTd);
      ordered.forEach(function(td) {{ tr.appendChild(td); }});
    }});
  }}
}})();
"""
    _bulk_js = """
var CelerpSelection=(function(){
  var KEY='celerp_inv_selection',_map={};
  function _save(){try{sessionStorage.setItem(KEY,JSON.stringify(_map))}catch(e){}}
  function _load(){try{_map=JSON.parse(sessionStorage.getItem(KEY)||'{}')}catch(e){_map={}}}
  _load();
  return {
    add:function(id,meta){_map[id]=meta;_save()},
    remove:function(id){delete _map[id];_save()},
    has:function(id){return id in _map},
    clear:function(){_map={};_save()},
    count:function(){return Object.keys(_map).length},
    ids:function(){return Object.keys(_map)},
    all:function(){return Object.assign({},_map)},
    syncCheckboxes:function(){
      document.querySelectorAll('.row-select').forEach(function(cb){cb.checked=!!_map[cb.value]});
    }
  };
})();
function submitBulkAction(formEl){
  formEl.querySelectorAll('input[name="selected"]').forEach(function(el){el.remove()});
  CelerpSelection.ids().forEach(function(id){
    var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
    formEl.appendChild(inp);
  });
  return true;
}
function _clearBulkResult(){
  var r=document.getElementById('bulk-action-result');
  if(r) r.innerHTML='';
}
function _resetBulkActions(){
  var sel=document.getElementById('bulk-action-select');
  if(sel) sel.value='';
  var ctx=document.getElementById('bulk-context');
  if(ctx) ctx.innerHTML='';
  _clearBulkResult();
}
function bulkActionChanged(action){
  var ctx=document.getElementById('bulk-context');
  if(!ctx) return;
  ctx.innerHTML='';
  _clearBulkResult();
  var n=CelerpSelection.count();
  // Immediate actions (no context UI)
  if(action==='archive'){
    if(!confirm('Archive / Dispose selected items? They will be hidden from the default view.')) return;
    _bulkImmediate('/api/items/bulk/status','bulk_status','archived');return;
  }
  if(action==='restore'){
    if(!confirm('Restore selected items to the catalog? They will become available again.')) return;
    _bulkImmediate('/api/items/bulk/status','bulk_status','available');return;
  }
  if(action==='expire'){
    _bulkImmediate('/api/items/bulk/expire',null,null);return;
  }
  if(action==='delete'){
    if(!confirm('Delete selected items? This cannot be undone.')) return;
    _bulkImmediate('/api/items/bulk/delete',null,null);return;
  }
  // Context-driven actions - clone template (includes module actions via mod: prefix)
  // mod: actions use tpl-mod-{action_id} where action_id strips the mod: prefix.
  // The option value and template id must be derived the same way to stay in sync.
  var tplId=action.startsWith('mod:')?'tpl-mod-'+action.slice(4):'tpl-'+action;
  if(action==='send_to') tplId='tpl-send-to';
  var tpl=document.getElementById(tplId);
  if(!tpl) return;
  // Validate selection count constraints
  if(action==='split'&&n!==1){alert('Select exactly 1 item to split.');return;}
  if(action==='transform'&&n!==1){alert('Select exactly 1 item to transform.');return;}
  if(action==='merge'&&n<2){alert('Select at least 2 items to merge.');return;}
  var clone=tpl.content.cloneNode(true);
  ctx.appendChild(clone);
  // Split: auto-load preview immediately; no qty input needed
  if(action==='split'){
    if(window.htmx) htmx.process(ctx);
    if(typeof bulkSplitAutoLoad==='function') bulkSplitAutoLoad();
    return;
  }
  // Transform: auto-load preview immediately
  if(action==='transform'){
    var ids = CelerpSelection.ids();
    if(!ids.length) return;
    var entityId = ids[0];
    var url = '/api/items/bulk/transform-preview?entity_id=' + encodeURIComponent(entityId);
    htmx.ajax('GET', url, { target: '#bulk-transform-preview', swap: 'innerHTML' })
      .then(function() {
        if (window.htmx) htmx.process(document.getElementById('bulk-transform-preview'));
        if (typeof transformPreviewInit === 'function') transformPreviewInit('bulk-transform-preview-form');
      });
    return;
  }
  // Merge: populate target dropdown with selected items
  if(action==='merge') _populateMergeTargets();
  // Re-process htmx on new content
  if(window.htmx) htmx.process(ctx);
}
function _bulkImmediate(url,extraName,extraValue){
  var form=document.createElement('form');
  CelerpSelection.ids().forEach(function(id){
    var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
    form.appendChild(inp);
  });
  if(extraName){
    var ex=document.createElement('input');ex.type='hidden';ex.name=extraName;ex.value=extraValue;
    form.appendChild(ex);
  }
  document.body.appendChild(form);
  // Remove the form only after the request completes (see merge handler note) so HX-Trigger
  // events fired on the source element still reach the document-level listeners.
  htmx.ajax('POST',url,{source:form,target:'#bulk-action-result',swap:'outerHTML'})
    .then(function(){form.remove();},function(){form.remove();});
}
function _populateMergeTargets(){
  var sel=document.getElementById('merge-target-select');
  if(!sel) return;
  var all=CelerpSelection.all();
  Object.keys(all).forEach(function(id){
    var meta=all[id];
    var opt=document.createElement('option');
    opt.value=id;
    opt.textContent=(meta.sku||id)+' - '+(meta.name||'');
    sel.appendChild(opt);
  });
  sel.addEventListener('change',function(){
    var confirmDiv=document.getElementById('merge-confirm');
    if(!confirmDiv) return;
    var n=CelerpSelection.count();
    var targetText=sel.options[sel.selectedIndex].textContent;
    confirmDiv.innerHTML='';
    confirmDiv.style.display='flex';
    confirmDiv.style.flexDirection='column';
    confirmDiv.style.gap='0.5rem';
    confirmDiv.style.marginTop='0.5rem';
    var msg=document.createElement('span');
    msg.textContent='Merge '+n+' items into '+targetText+'?';
    msg.style.fontSize='0.85rem';
    var btnRow=document.createElement('div');
    btnRow.style.display='flex';
    btnRow.style.gap='0.5rem';
    var btn=document.createElement('button');
    btn.type='button';btn.className='btn btn--primary btn--sm';btn.textContent='Confirm';
    btn.addEventListener('click',function(){
      var form=document.createElement('form');
      CelerpSelection.ids().forEach(function(id){
        var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
        form.appendChild(inp);
      });
      var t=document.createElement('input');t.type='hidden';t.name='target_sku_from';t.value=sel.value;
      form.appendChild(t);
      document.body.appendChild(form);
      // Keep the form attached until the request finishes - removing it early detaches the htmx
      // event source so HX-Trigger toasts (e.g. a unit-mismatch error) never reach the listener.
      htmx.ajax('POST','/api/items/bulk/merge',{source:form,target:'#bulk-action-result',swap:'outerHTML'})
        .then(function(){form.remove();},function(){form.remove();});
    });
    var cancel=document.createElement('button');
    cancel.type='button';cancel.className='btn btn--ghost btn--sm';cancel.textContent='Cancel';
    cancel.addEventListener('click',function(){
      confirmDiv.style.display='none';
      sel.value='';
      _clearBulkResult();
    });
    confirmDiv.appendChild(msg);
    btnRow.appendChild(btn);
    btnRow.appendChild(cancel);
    confirmDiv.appendChild(btnRow);
  });
}
function sendToTypeChanged(docType){
  var targetSel=document.getElementById('send-to-target-select');
  if(!targetSel) return;
  // Reset to just "New"
  targetSel.innerHTML='';
  var newOpt=document.createElement('option');
  newOpt.value='__new__';newOpt.textContent='New '+docType;
  targetSel.appendChild(newOpt);
  if(!docType) return;
  // Fetch matching docs
  fetch('/api/items/send-to/search?doc_type='+encodeURIComponent(docType))
    .then(function(r){return r.json()})
    .then(function(docs){
      docs.forEach(function(d){
        var opt=document.createElement('option');
        opt.value=d.id||d.entity_id||'';
        opt.textContent=(d.label||d.doc_number||d.number||'')+(d.contact_name?' - '+d.contact_name:'');
        targetSel.appendChild(opt);
      });
    }).catch(function(){});
}
(function(){
  function _meta(cb){return {sku:cb.dataset.sku||'',name:cb.dataset.name||'',qty:cb.dataset.qty||'0',weight:cb.dataset.weight||'',weight_unit:cb.dataset.weightUnit||'',sell_by:cb.dataset.sellBy||''};}
  function updateBulkToolbar(){
    var n=CelerpSelection.count();
    var toolbar=document.getElementById('bulk-toolbar');
    var countEl=document.getElementById('bulk-count');
    var clearBtn=document.getElementById('bulk-clear-btn');
    if(countEl) countEl.textContent=n+' selected';
    if(toolbar){if(n>0){toolbar.classList.add('is-active')}else{toolbar.classList.remove('is-active')}}
    if(clearBtn){clearBtn.style.display=n>0?'':'none'}
  }
  var table=document.getElementById('data-table');
  if(!table) return;
  var selectAll=table.querySelector('thead .col-checkbox input');
  if(selectAll){
    selectAll.type='checkbox';
    selectAll.addEventListener('change',function(){
      table.querySelectorAll('tbody .row-select').forEach(function(cb){
        if(selectAll.checked){CelerpSelection.add(cb.value,_meta(cb));cb.checked=true}
        else{CelerpSelection.remove(cb.value);cb.checked=false}
      });
      _resetBulkActions();
      updateBulkToolbar();
    });
  }
  table.addEventListener('change',function(e){
    if(e.target&&e.target.classList.contains('row-select')){
      if(e.target.checked){CelerpSelection.add(e.target.value,_meta(e.target))}
      else{CelerpSelection.remove(e.target.value)}
      _resetBulkActions();
      updateBulkToolbar();
    }
  });
  document.body.addEventListener('celerpSelectionClear',function(){
    CelerpSelection.clear();
    document.querySelectorAll('.row-select').forEach(function(cb){cb.checked=false});
    var selectAll=document.querySelector('#data-table thead .col-checkbox input');
    if(selectAll) selectAll.checked=false;
    updateBulkToolbar();
    _resetBulkActions();
  });
  document.body.addEventListener('htmx:afterSwap',function(e){
    if(e.detail&&e.detail.target){
      var tid=e.detail.target.id;
      if(tid==='inventory-content'||tid==='data-table'){
        CelerpSelection.syncCheckboxes();updateBulkToolbar();
      }
    }
  });
  // Guard: register body-level htmx handlers only once per page load
  if(!window.__celerpHtmxHandlers){
    window.__celerpHtmxHandlers=true;
  // Preserve horizontal scroll position across any HTMX request that may replace
  // the table or its scroll container (cell edits, sort, search, pagination, etc.).
  // Save on htmx:beforeRequest AND eagerly exposed as window.__celerpScrollSnap so
  // inline ESC handlers can set it synchronously before the browser resets scroll.
  // Restore on htmx:afterSettle using requestAnimationFrame to run after browser reflow.
  window.__celerpScrollSnap=null;
  document.body.addEventListener('htmx:beforeRequest',function(e){
    var sw=document.querySelector('.table-scroll-wrap');
    if(sw){window.__celerpScrollSnap=sw.scrollLeft;}
  });
  document.body.addEventListener('htmx:afterSettle',function(e){
    if(window.__celerpScrollSnap!=null){
      var s=window.__celerpScrollSnap;window.__celerpScrollSnap=null;
      requestAnimationFrame(function(){
        var sw=document.querySelector('.table-scroll-wrap');
        if(sw)sw.scrollLeft=s;
      });
    }
  });
  // Sync derived cells (weight/pieces) after a quantity PATCH.
  // Use htmx:afterRequest (fires before swap) to get the requestConfig path reliably,
  // then re-query the live DOM after the swap completes via htmx:afterSettle.
  document.body.addEventListener('htmx:afterSettle',function(e){
    var path=(e.detail.requestConfig&&e.detail.requestConfig.path)||
             (e.detail.pathInfo&&e.detail.pathInfo.requestPath)||'';
    if(!path){return;}
    var m=path.match(/\/api\/items\/([^/]+)\/field\/quantity/);
    if(!m){return;}
    var eid=m[1];
    var safeId=eid.replace(/:/g,'-');
    var tr=document.getElementById('row-'+safeId);
    if(!tr){return;}
    var qtyTd=tr.querySelector('[data-col="quantity"]');
    if(!qtyTd){return;}
    var primSpan=qtyTd.querySelector('.paired-primary');
    var secSpan=qtyTd.querySelector('.paired-secondary');
    // Use the raw numeric value from the paired-primary span for precise formatting
    var rawQty=primSpan?primSpan.textContent.trim():qtyTd.textContent.trim();
    var newUnit=secSpan?secSpan.textContent.trim():'';
    function fmtNum(val, decimals) {
      var n=parseFloat(val);
      if(isNaN(n)){return val||'--';}
      if(decimals===''||decimals===null||decimals===undefined){return String(n);}
      return n.toFixed(parseInt(decimals,10));
    }
    tr.querySelectorAll('.cell-derived').forEach(function(span){
      var derivedTd=span.closest('td');
      if(!derivedTd){return;}
      var col=derivedTd.dataset.col;
      var decimals=derivedTd.dataset.decimals;
      var fmt=fmtNum(rawQty, decimals);
      if(col==='weight'){
        span.textContent=(rawQty&&rawQty!=='--'&&newUnit&&newUnit!=='--')
          ?fmt+'\u00a0'+newUnit:(fmt||'--');
      } else if(col==='pieces'){
        span.textContent=fmt||'--';
      }
    });
  });
  } // end if(!window.__celerpHtmxHandlers)
  CelerpSelection.syncCheckboxes();
  updateBulkToolbar();
})();
"""
    # Phantom top-scrollbar: a zero-height scrollable div that mirrors .table-scroll-wrap.
    # Only injected when the table has more than 10 rows, otherwise not needed.
    _top_scroll_js = Script("""
(function(){
  var wrap = document.querySelector('#data-table-wrap .table-scroll-wrap');
  var phantom = document.querySelector('#data-table-wrap .table-top-scroll');
  var inner = phantom && phantom.firstElementChild;
  if (!wrap || !phantom || !inner) return;
  // Size the inner spacer to match the scrollable content width
  function syncWidth() {
    inner.style.width = wrap.scrollWidth + 'px';
  }
  syncWidth();
  // Bidirectional scroll sync (guard flag prevents infinite loop)
  var syncing = false;
  phantom.addEventListener('scroll', function() {
    if (syncing) return; syncing = true;
    wrap.scrollLeft = phantom.scrollLeft;
    syncing = false;
  });
  wrap.addEventListener('scroll', function() {
    if (syncing) return; syncing = true;
    phantom.scrollLeft = wrap.scrollLeft;
    syncing = false;
  });
  // Re-sync width if columns change (e.g. column manager toggle)
  new MutationObserver(syncWidth).observe(wrap, {childList: true, subtree: true, attributes: true});
})();
""")
    top_scroll = Div(Div(style="height:1px"), cls="table-top-scroll") if len(rows) > 10 else None
    scripts = [Script(_js)]
    if show_checkboxes:
        scripts.append(Script(_bulk_js.replace("'celerp_inv_selection'", f"'{selection_key}'")))
    if top_scroll:
        scripts.append(_top_scroll_js)
    return Div(
        top_scroll,
        Div(
            Table(header, Tbody(*[_row(r) for r in rows]), cls="data-table", id="data-table"),
            cls="table-scroll-wrap",
        ),
        *scripts,
        id="data-table-wrap",
    )


def column_manager(schema: list[dict], entity_type: str, visible_cols: list[str] | None = None) -> FT:
    """Generic column manager dropdown. Toggles column visibility and order via localStorage.

    Uses the same localStorage key as ``data_table`` (``celerp_cols_{entity_type}``),
    so visibility state is shared between the manager UI and the table's own JS.

    Features:
    - Checkbox toggle per column with immediate visibility apply
    - Drag-and-drop reordering within the picker (source of truth for column order)
    - Listens for ``celerp:col-reorder`` events fired by data_table header drag, keeping
      the picker in sync when the user drags a table column header directly
    - Reset to default button (clears all localStorage keys and reloads)
    """
    import json as _json
    selected = set(visible_cols) if visible_cols else {f["key"] for f in schema if f.get("show_in_table", True)}
    col_data = [{"key": f["key"], "label": f.get("label", f["key"])} for f in schema]
    default_keys = _json.dumps([f["key"] for f in schema if f.get("show_in_table", True)])

    checkboxes = [
        Label(
            Input(type="checkbox", value=f["key"], checked=f["key"] in selected, id=f"col-chk-{f['key']}"),
            Span(f.get("label", f["key"])),
            cls="column-option",
            draggable="true",
            data_col=f["key"],
        )
        for f in schema
    ]

    _mgr_js = f"""
(function(){{
  var VIS_KEY='celerp_cols_{entity_type}',ORDER_KEY='celerp_col_order_{entity_type}',WIDTH_KEY='celerp_col_widths_{entity_type}';
  var ALL={_json.dumps(col_data)};
  var DEFAULTS={default_keys};
  var btn=document.getElementById('col-mgr-btn'),menu=document.getElementById('col-mgr-menu');
  if(!btn||!menu) return;
  function loadVis(){{try{{return JSON.parse(localStorage.getItem(VIS_KEY)||'null')}}catch(e){{return null}}}}
  function saveVis(p){{localStorage.setItem(VIS_KEY,JSON.stringify(p))}}
  function loadOrder(){{try{{return JSON.parse(localStorage.getItem(ORDER_KEY)||'null')}}catch(e){{return null}}}}
  function saveOrder(o){{localStorage.setItem(ORDER_KEY,JSON.stringify(o))}}
  function applyVis(prefs){{
    var t=document.getElementById('data-table');if(!t)return;
    var ths=Array.from(t.querySelectorAll('thead th[data-key]'));
    var rows=Array.from(t.querySelectorAll('tbody tr.data-row'));
    ths.forEach(function(th){{
      var k=th.dataset.key,show=prefs[k]!==false;
      th.style.display=show?'':'none';
      rows.forEach(function(tr){{var td=tr.querySelector('[data-col="'+k+'"]');if(td)td.style.display=show?'':'none';}});
    }});
  }}
  function applyOrderToTable(order){{
    if(!order||!order.length)return;
    var t=document.getElementById('data-table');if(!t)return;
    var htr=t.querySelector('thead tr');if(!htr)return;
    var actTh=htr.querySelector('.col-actions');
    order.forEach(function(k){{var th=htr.querySelector('th[data-key="'+k+'"]');if(th&&actTh)htr.insertBefore(th,actTh);else if(th)htr.appendChild(th);}});
    var allThs=Array.from(htr.querySelectorAll('th[data-key]'));
    t.querySelectorAll('tbody tr.data-row').forEach(function(tr){{
      var cbTd=tr.querySelector('.col-checkbox'),aTd=tr.querySelector('.col-actions');
      var data=allThs.map(function(h){{return tr.querySelector('[data-col="'+h.dataset.key+'"]');}}).filter(Boolean);
      var out=[];if(cbTd)out.push(cbTd);out=out.concat(data);if(aTd)out.push(aTd);
      out.forEach(function(td){{tr.appendChild(td);}});
    }});
  }}
  function applyOrderToPicker(order){{
    if(!order||!order.length)return;
    var labels=menu.querySelectorAll('label[data-col]');if(!labels.length)return;
    var parent=labels[0].parentNode;
    order.forEach(function(key){{var lbl=menu.querySelector('label[data-col="'+key+'"]');if(lbl)parent.appendChild(lbl);}});
  }}
  function pickerOrder(){{
    return Array.from(menu.querySelectorAll('label[data-col]')).map(function(l){{return l.dataset.col;}});
  }}
  function syncCB(){{var p=loadVis()||{{}};menu.querySelectorAll('input[type=checkbox]').forEach(function(c){{c.checked=p[c.value]!==false;}});}}
  btn.addEventListener('click',function(e){{e.stopPropagation();var o=menu.style.display!=='none';menu.style.display=o?'none':'';if(!o)syncCB();}});
  document.addEventListener('click',function(e){{if(!btn.contains(e.target)&&!menu.contains(e.target))menu.style.display='none';}});
  menu.addEventListener('change',function(e){{
    if(e.target.type!=='checkbox')return;
    var k=e.target.value,p=loadVis()||{{}};
    if(!Object.keys(p).length)ALL.forEach(function(c){{p[c.key]=DEFAULTS.indexOf(c.key)!==-1;}});
    p[k]=e.target.checked;saveVis(p);applyVis(p);
    applyOrderToTable(pickerOrder());
  }});
  var ds=null;
  menu.querySelectorAll('label[draggable]').forEach(function(l){{
    l.addEventListener('dragstart',function(e){{ds=l;e.dataTransfer.effectAllowed='move';l.style.opacity='0.5';}});
    l.addEventListener('dragend',function(){{l.style.opacity='';ds=null;}});
    l.addEventListener('dragover',function(e){{e.preventDefault();e.dataTransfer.dropEffect='move';}});
    l.addEventListener('drop',function(e){{
      e.preventDefault();if(!ds||ds===l)return;
      var par=l.parentNode,sn=ds.nextSibling;par.insertBefore(ds,l);if(sn)par.insertBefore(l,sn);else par.appendChild(l);
      ds.style.opacity='';
      var no=pickerOrder();saveOrder(no);applyOrderToTable(no);
    }});
  }});
  // Sync picker when table header is dragged (data_table fires this event)
  document.addEventListener('celerp:col-reorder',function(e){{
    if(!e.detail||!e.detail.order)return;
    applyOrderToPicker(e.detail.order);
    saveOrder(e.detail.order);
  }});
  var sv=loadVis();if(sv)applyVis(sv);
  var so=loadOrder();
  if(so){{applyOrderToPicker(so);applyOrderToTable(so);}}
  menu.style.display='none';
}})();
"""
    reset_onclick = (
        f"localStorage.removeItem('celerp_cols_{entity_type}');"
        f"localStorage.removeItem('celerp_col_order_{entity_type}');"
        f"localStorage.removeItem('celerp_col_widths_{entity_type}');"
        f"location.reload();"
    )
    return Div(
        Button(t("btn.manage_columns"), id="col-mgr-btn", cls="btn btn--secondary", type="button"),
        Div(
            *checkboxes,
            Button(
                t("btn.reset_columns"),
                id="col-mgr-reset",
                cls="btn btn--sm btn--ghost col-mgr-reset-btn",
                type="button",
                onclick=reset_onclick,
                title=t("btn.reset_columns_title"),
            ),
            cls="column-menu",
            id="col-mgr-menu",
            style="display:none",
        ),
        Script(_mgr_js),
        cls="column-manager",
    )


def pagination(page: int, total: int, per_page: int, base_url: str, extra_params: str = "") -> FT:
    total_pages = max(1, (total + per_page - 1) // per_page)
    sep = "&" if "?" in base_url or extra_params else "?"

    def _href(p: int) -> str:
        params = f"page={p}&per_page={per_page}"
        if extra_params:
            params += f"&{extra_params}"
        return f"{base_url}?{params}"

    pages = [
        A(str(p), href=_href(p),
          cls=f"page-btn {'page-btn--active' if p == page else ''}")
        for p in range(max(1, page - 2), min(total_pages, page + 3))
    ]
    return Div(
        *(([A("«", href=_href(page - 1), cls="page-btn")] if page > 1 else []) +
          pages +
          ([A("»", href=_href(page + 1), cls="page-btn")] if page < total_pages else [])),
        Span(f"{total:,} {'record' if total == 1 else 'records'}", cls="page-count"),
        _per_page_selector(per_page, base_url, extra_params),
        cls="pagination",
    )


def _per_page_selector(current: int, base_url: str, extra_params: str = "") -> FT:
    options = [25, 50, 100, 250, 500]
    # Swap only the content fragment (avoids double shell render).
    # base_url is e.g. "/inventory"; content endpoint is "/inventory/content".
    content_url = base_url.rstrip("/") + "/content"
    url_with_state = f"{content_url}?{extra_params}" if extra_params else content_url
    return Select(
        *[Option(f"{n} per page", value=str(n), selected=(n == current)) for n in options],
        name="per_page",
        hx_get=url_with_state,
        hx_trigger="change",
        hx_target="#inventory-content",
        hx_swap="outerHTML",
        hx_include="this",
        hx_push_url="true",
        cls="filter-select per-page-select",
    )


def search_bar(placeholder: str = "Search...", target: str = "#data-table", url: str = "") -> FT:
    # Enter key → insert comma (for barcode scanner multi-scan: each scan ends with Enter,
    # becoming a comma-separated OR query without submitting the form).
    enter_js = (
        "if(event.key==='Enter'){"
        "event.preventDefault();"
        "var v=this.value,end=this.selectionEnd;"
        "if(v.length&&v[v.length-1]!==','){"
        "this.value=v+',';"
        "} }"
    )
    return Input(
        type="search",
        name="q",
        placeholder=placeholder,
        hx_get=url,
        hx_trigger="input changed delay:300ms",
        hx_target=target,
        hx_swap="outerHTML",
        hx_push_url="true",
        hx_include="this",
        cls="search-input",
        id="search-input",
        onkeydown=enter_js,
        title="Use a comma (or Enter) for OR — e.g. scan multiple barcodes one after another",
    )


def breadcrumbs(crumbs: list[tuple[str, str | None]]) -> FT:
    """Breadcrumb navigation. Each crumb is (label, href). Last crumb has href=None (current page)."""
    parts: list[FT] = []
    for i, (label, href) in enumerate(crumbs):
        if i > 0:
            parts.append(Span("›", cls="breadcrumb-sep"))
        if href is not None:
            parts.append(A(label, href=href, cls="breadcrumb-link"))
        else:
            parts.append(Span(label, cls="breadcrumb-current"))
    return Div(*parts, cls="breadcrumbs")


def add_new_option(label: str = "+ Add new", redirect_url: str = "#") -> tuple:
    """Return (Option element, onchange JS snippet) for 'add new' in dynamic selects."""
    option = Option(label, value="__new__")
    js = f"if(this.value==='__new__')window.location='{redirect_url}'"
    return option, js


def simple_table(headers: list[str], rows: list[list], id: str = "", cls_extra: str = "") -> FT:
    """Simple table for pages that don't use the full data_table (reports, settings, etc.).
    Applies consistent styling: headers centered, text left, currency right.
    Empty values → EMPTY.
    """
    def _cell(val) -> FT:
        if val is None or (isinstance(val, str) and not val.strip()):
            return Td(EMPTY)
        # If it's already an FT element, pass through
        if hasattr(val, '__ft__') or isinstance(val, (tuple, list)):
            return Td(val)
        s = str(val)
        # Currency detection — check against all known symbols
        _MONEY_PREFIXES = tuple(_CURRENCY_SYMBOLS.values()) + ("฿", "$", "€", "£", "¥")
        if any(s.startswith(p) for p in _MONEY_PREFIXES):
            return Td(s, cls="cell--number")
        return Td(s)

    return Table(
        Thead(Tr(*[Th(h) for h in headers])),
        Tbody(*[Tr(*[_cell(c) for c in row], cls="data-row") for row in rows]),
        cls=f"data-table {cls_extra}".strip(),
        **({"id": id} if id else {}),
    )


def unwrap_address(raw) -> str:
    """Unwrap an address value that may be a dict (``{"text": "…"}``) or a plain string.

    Single source of truth for all UI address display - used by settings, documents, etc.
    """
    if not raw:
        return ""
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("line1") or ""
        for k in ("line2", "city", "state", "postal_code", "country"):
            v = raw.get(k) or ""
            if v:
                text = text + ("\n" if text else "") + v
        return text
    return str(raw)


def col_resize_script(table_selector: str, storage_key: str):
    """Reusable PROPORTIONAL drag-to-resize for a header's columns.

    The table is meant to fill its container (``width:100%; table-layout:fixed``). Every column
    width is held as a PERCENTAGE of the table, so the layout scales with the viewport and the
    rightmost column stays locked to the right edge at any screen size. Dragging a handle widens
    one column and narrows its right-hand neighbour by the same amount, so the columns always sum
    to 100% (no overflow, no horizontal scroll).

    On first load (no saved prefs) the columns' current rendered proportions are captured as the
    baseline; otherwise the saved percentages are restored. Widths persist to
    ``localStorage[storage_key]`` keyed by each th's first ``col-*`` class. The column set is
    fixed — this only resizes, never adds/removes/reorders. The last column has no handle (it is
    pinned to the right edge; resize it by dragging its left neighbour's handle).
    """
    import json as _json
    sel = _json.dumps(table_selector)
    key = _json.dumps(storage_key)
    js = (
        "(function(){"
        "var SEL=" + sel + ",KEY=" + key + ",MIN=3;"  # MIN = floor width per column, in %
        "function ck(h){var m=(h.className||'').match(/col-[a-z-]+/);return m?m[0]:'';}"
        "document.querySelectorAll(SEL).forEach(function(t){"
        "if(t.dataset.colResize)return;t.dataset.colResize='1';"
        "var ths=Array.from(t.querySelectorAll('thead th'));if(!ths.length)return;"
        "function tw(){return t.offsetWidth||1;}"
        "function pct(h){return h.offsetWidth/tw()*100;}"
        "function save(){var w={};ths.forEach(function(h){var k=ck(h);if(k)w[k]=parseFloat(h.style.width)||pct(h);});try{localStorage.setItem(KEY,JSON.stringify(w));}catch(e){}}"
        # Baseline: restore saved % per column, else capture the current rendered proportions;
        # then normalise so the set sums to exactly 100% and fills the table.
        "requestAnimationFrame(function(){"
        "var sv=null;try{sv=JSON.parse(localStorage.getItem(KEY)||'null');}catch(e){}"
        "var ws=ths.map(function(h){var k=ck(h);return(sv&&sv[k]!=null)?sv[k]:pct(h);});"
        "var s=ws.reduce(function(a,b){return a+b;},0)||1;"
        "ths.forEach(function(h,i){h.style.width=(ws[i]/s*100)+'%';});"
        "});"
        # Handles on every column except the last (which is pinned to the right edge).
        "ths.forEach(function(h,idx){"
        "if(idx>=ths.length-1)return;"
        "if(h.querySelector('.col-resize-handle'))return;"
        "var d=document.createElement('div');d.className='col-resize-handle';h.style.position='relative';h.appendChild(d);"
        "d.addEventListener('mousedown',function(e){"
        "var nx=ths[idx+1],W=tw(),sx=e.pageX,a0=h.offsetWidth/W*100,b0=nx.offsetWidth/W*100,sum=a0+b0;"
        "document.body.style.cursor='col-resize';"
        "function mv(ev){var dp=(ev.pageX-sx)/W*100,a=a0+dp,b=b0-dp;"
        "if(a<MIN){a=MIN;b=sum-MIN;}if(b<MIN){b=MIN;a=sum-MIN;}"
        "h.style.width=a+'%';nx.style.width=b+'%';}"
        "function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);document.body.style.cursor='';save();}"
        "document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);e.preventDefault();e.stopPropagation();"
        "});});});"
        "})();"
    )
    return Script(js)

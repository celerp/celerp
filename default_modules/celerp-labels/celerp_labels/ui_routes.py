# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-labels UI routes (FastHTML).

Registered into the FastHTML (UI) app by the module loader.

Pages (full-page, wrapped in base_shell)
----------------------------------------
GET /settings/labels               Template list + editor (create / edit / delete)
GET /settings/labels/{tmpl_id}     Load editor for a specific template

HTMX fragments (return partial HTML, no full-page render)
---------------------------------------------------------
POST   /settings/labels             Create template -> return refreshed root
DELETE /settings/labels/{id}        Delete template -> return refreshed root
PUT    /settings/labels/{id}        Save template -> return refreshed editor panel
"""
from __future__ import annotations

import logging
from ui.i18n import t, get_lang

try:
    from starlette.requests import Request
    from starlette.responses import RedirectResponse
except ImportError:  # pragma: no cover
    Request = None  # type: ignore[assignment,misc]
    RedirectResponse = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_COMMON_FIELDS = [
    ("barcode", "Barcode (bars)", "barcode"),
    ("qr", "QR Code", "qr"),
    ("name", "Name", "text"),
    ("sku", "SKU", "text"),
    ("category", "Category", "text"),
    ("status", "Status", "text"),
    ("location_name", "Location", "text"),
    ("weight", "Weight", "text"),
    ("unit", "Unit", "text"),
    ("cost_price", "Cost Price", "text"),
    ("sale_price", "Sale Price", "text"),
    ("description", "Description", "text"),
]

_FORMAT_OPTIONS = [
    ("24x24mm", "24 x 24"),
    ("29x29mm", "29 x 29"),
    ("34x34mm", "34 x 34"),
    ("40x30mm", "40 x 30"),
    ("62x29mm", "62 x 29"),
    ("100x50mm", "100 x 50"),
    ("A4", "A4"),
    ("letter", "Letter"),
    ("custom", "Custom..."),
]


_SAMPLE_DATA = {
    "name": "Sample Item",
    "sku": "SKU-001",
    "barcode": "123456789",
    "qr": "123456789",
    "category": "General",
    "status": "Available",
    "location_name": "Head Office",
    "weight": "2.5",
    "unit": "pcs",
    "cost_price": "150.00",
    "sale_price": "299.00",
    "retail_price": "299.00",
    "wholesale_price": "199.00",
    "description": "Sample description",
    "quantity": "10",
}

# Map field key -> render type for built-in fields
_FIELD_TYPE_MAP = {k: t for k, _label, t in _COMMON_FIELDS}

_PRESET_TEMPLATES = [
    # -- Small square barcode stickers --
    {
        "name": "Barcode Sticker (24x24)",
        "format": "24x24mm",
        "fields": [
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 1, "y": 1, "fontSize": 6},
            {"key": "name", "label": "Name", "type": "text", "x": 1, "y": 16, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 1, "y": 20, "fontSize": 5},
        ],
    },
    {
        "name": "Barcode Sticker (29x29)",
        "format": "29x29mm",
        "fields": [
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 1, "y": 1, "fontSize": 7},
            {"key": "name", "label": "Name", "type": "text", "x": 1, "y": 17, "fontSize": 6},
            {"key": "sku", "label": "SKU", "type": "text", "x": 1, "y": 22, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 1, "y": 26, "fontSize": 5},
        ],
    },
    {
        "name": "Barcode Sticker (34x34)",
        "format": "34x34mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 2, "y": 2, "fontSize": 7},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 8, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 2, "y": 24, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 2, "y": 28, "fontSize": 6},
        ],
    },
    # -- Rectangular labels --
    {
        "name": "Small Tag (40x30)",
        "format": "40x30mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 2, "y": 2, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 2, "y": 7, "fontSize": 5},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 12, "fontSize": 7},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 2, "y": 26, "fontSize": 7},
        ],
    },
    {
        "name": "QR Label (62x29)",
        "format": "62x29mm",
        "fields": [
            {"key": "qr", "label": "QR Code", "type": "qr", "x": 2, "y": 2, "fontSize": 7},
            {"key": "name", "label": "Name", "type": "text", "x": 14, "y": 2, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 14, "y": 8, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 14, "y": 13, "fontSize": 8},
            {"key": "category", "label": "Category", "type": "text", "x": 14, "y": 20, "fontSize": 5},
        ],
    },
    {
        "name": "Shelf Label (100x50)",
        "format": "100x50mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 3, "y": 3, "fontSize": 10},
            {"key": "sku", "label": "SKU", "type": "text", "x": 3, "y": 12, "fontSize": 6},
            {"key": "category", "label": "Category", "type": "text", "x": 3, "y": 17, "fontSize": 6},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 3, "y": 23, "fontSize": 8},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 60, "y": 3, "fontSize": 12},
            {"key": "location_name", "label": "Location", "type": "text", "x": 60, "y": 14, "fontSize": 6},
            {"key": "qr", "label": "QR Code", "type": "qr", "x": 60, "y": 23, "fontSize": 7},
        ],
    },
]

_LABELS_CSS = """
<style id="labels-css">
/* -- Settings page: template list (left) + editor (right) -- */
.label-settings-layout { display: flex; gap: 16px; align-items: flex-start; }
.label-settings-layout > .settings-card:first-child { width: 200px; flex-shrink: 0; }
.label-settings-layout > .settings-card:last-child { flex: 1; min-width: 0; }
.template-list { list-style: none; padding: 0; margin: 0 0 12px; }
.template-list-entry { display: flex; align-items: center; gap: 4px; margin-bottom: 4px; }
.template-list-item {
  flex: 1; display: flex; flex-direction: column; padding: 6px 10px;
  border-radius: var(--radius); border: 1px solid var(--c-border);
  background: var(--c-bg2); color: var(--c-text); text-decoration: none;
  font-size: 12px; min-width: 0;
}
.template-list-item:hover { border-color: var(--c-accent); text-decoration: none; }
.template-list-item--active { border-color: var(--c-accent); background: rgba(79,142,247,0.08); }
.template-item-text .text-muted { font-size: 11px; color: var(--c-text2); }
.template-create-form { display: flex; gap: 6px; align-items: center; margin-top: 8px; }
.template-create-form .form-input { flex: 1; min-width: 0; }

/* -- Editor: top config bar (stacked: name then format) -- */
.labels-top-bar {
  display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; max-width: 320px;
}
.labels-top-bar .labels-form-field { display: flex; flex-direction: column; }
.labels-form-field label { display: block; font-size: 11px; color: var(--c-text2); margin-bottom: 3px; }
.custom-dims { display: flex; gap: 8px; margin-bottom: 12px; }
.custom-dims .labels-form-field { flex: 1; }

/* -- Editor: stacked layout (preview on top, fields below) -- */
.label-editor-layout {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.label-canvas-panel { display: flex; flex-direction: column; gap: 6px; }
.label-canvas-panel h4 { font-size: 11px; color: var(--c-text2); margin: 0; }
.label-canvas {
  position: relative;
  border: 2px solid var(--c-accent);
  background: #fff;
  overflow: hidden;
  display: inline-block;
  cursor: default;
  min-width: 120px;
  min-height: 80px;
}
.label-field-block {
  position: absolute; font-size: 9px; line-height: 1.2;
  white-space: nowrap; overflow: hidden; color: #111;
  border: 1px dashed rgba(0,0,0,0.25); padding: 1px 3px;
  background: rgba(255,255,255,0.9); cursor: move;
  user-select: none; box-sizing: border-box;
}
.label-field-block:hover { border-color: var(--c-accent); }
/* Barcode preview image block */
.label-field-block--barcode {
  display: flex; flex-direction: column; align-items: flex-start;
  white-space: normal; padding: 1px 2px;
}
.label-field-block--barcode img { display: block; width: 100%; height: auto; }
.label-field-block--barcode .bc-text { font-size: 60%; color: #333; margin-top: 1px; }
/* QR preview image block */
.label-field-block--qr {
  padding: 1px; overflow: visible;
}
.label-field-block--qr img { display: block; width: 100%; height: 100%; }

/* -- Field list -- */
.label-fields-panel { max-width: 320px; }
.field-list { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.field-row-compact {
  display: grid;
  grid-template-columns: 1fr 1fr 24px;
  gap: 3px;
  align-items: center;
  background: var(--c-bg3);
  border: 1px solid var(--c-border);
  border-radius: var(--radius);
  padding: 3px 5px;
  font-size: 12px;
}
.fld-label-input {
  font-size: 11px; color: var(--c-text2); font-style: italic;
}
.fld-label-input:focus { color: var(--c-text); font-style: normal; }

/* -- Searchable select -- */
.searchable-select {
  position: relative; max-width: 320px; width: 100%; box-sizing: border-box;
}
.searchable-select__display {
  width: 100%; box-sizing: border-box; cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23888'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 8px center;
  padding-right: 24px;
}
.searchable-select__dropdown {
  display: none; position: absolute; z-index: 200; top: 100%; left: 0; right: 0;
  background: var(--c-bg2, #fff); border: 1px solid var(--c-accent, #4a90e2);
  border-radius: var(--radius, 4px); box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  max-height: 220px; overflow: hidden; display: none; flex-direction: column;
}
.searchable-select__dropdown.open { display: flex; }
.searchable-select__search {
  padding: 5px 8px; border: none; border-bottom: 1px solid var(--c-border, #ddd);
  font-size: 12px; background: var(--c-bg2, #fff); color: var(--c-text, #111);
  outline: none; width: 100%; box-sizing: border-box;
}
.searchable-select__list {
  overflow-y: auto; flex: 1;
}
.searchable-select__option {
  padding: 5px 10px; cursor: pointer; font-size: 12px; color: var(--c-text, #111);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.searchable-select__option:hover,
.searchable-select__option[aria-selected="true"] { background: rgba(79,142,247,0.12); }
.searchable-select__group-label {
  padding: 3px 8px 1px; font-size: 10px; color: var(--c-text2, #888);
  text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;
  border-top: 1px solid var(--c-border, #ddd); margin-top: 2px;
}
.searchable-select__group-label:first-child { border-top: none; margin-top: 0; }

/* -- Actions -- */
.form-actions { display: flex; gap: 8px; margin-top: 12px; }
.btn--danger { background: var(--c-red); color: #fff; border-color: var(--c-red); }
.btn--danger:hover { background: #dc2626; border-color: #dc2626; }
.btn--outline { background: transparent; color: var(--c-text); border: 1px solid var(--c-border); }
.btn--outline:hover { border-color: var(--c-accent); color: var(--c-accent); }
.btn--icon {
  padding: 1px 5px; font-size: 12px; line-height: 1; flex-shrink: 0;
  background: transparent; border: 1px solid var(--c-border); color: var(--c-text2);
}
.btn--icon:hover { border-color: var(--c-red); color: var(--c-red); }
.success-banner { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.25); color: var(--c-green); padding: 8px 12px; border-radius: var(--radius); margin-bottom: 10px; font-size: 12px; }
</style>
"""


def _ft():
    from fasthtml.common import (
        A, Button, Div, Form, H2, H3, H4, Input, Label, Li,
        NotStr, Option, P, Script, Select, Span, Strong,
        Table, Tbody, Td, Th, Thead, Tr, Ul,
    )
    return locals()


def _flash(msg: str, kind: str = "success") -> object:
    ft = _ft()
    cls = "success-banner" if kind == "success" else "error-banner"
    return ft["Div"](msg, cls=cls)


def _templates_list(templates: list[dict], active_id: str | None = None) -> object:
    ft = _ft()
    Div, H3, Ul, Li, A, Button, P, Span, Strong, Form, Input = (
        ft["Div"], ft["H3"], ft["Ul"], ft["Li"], ft["A"], ft["Button"],
        ft["P"], ft["Span"], ft["Strong"], ft["Form"], ft["Input"],
    )
    items = []
    for tpl in templates:
        tid = tpl["id"]
        is_active = tid == active_id
        items.append(Li(
            A(
                Div(
                    Strong(tpl["name"]),
                    Div(tpl.get("format", ""), cls="text-muted"),
                    cls="template-item-text",
                ),
                href=f"/settings/labels/{tid}",
                cls=f"template-list-item{'  template-list-item--active' if is_active else ''}",
            ),
            Button(
                "x",
                hx_delete=f"/settings/labels/{tid}",
                hx_target="#label-settings-root",
                hx_swap="outerHTML",
                hx_confirm=f"Delete template '{tpl['name']}'?",
                cls="btn btn--sm btn--icon btn--danger",
                title="Delete",
            ),
            cls="template-list-entry",
        ))
    return Div(
        H3(t("page.templates"), cls="section-title"),
        Ul(*items, cls="template-list") if items else P(t("label.no_templates_yet"), cls="text-muted"),
        Form(
            Input(name="name", placeholder="New template name", required=True, cls="form-input form-input--sm"),
            Button(t("btn._add"), type="submit", cls="btn btn--sm btn--primary"),
            hx_post="/settings/labels",
            hx_target="#label-settings-root",
            hx_swap="outerHTML",
            cls="template-create-form",
        ),
        id="template-list-panel",
        cls="settings-card",
    )


def _build_field_options_js(
    selected_key: str,
    global_extra: list[tuple[str, str]] | None,
    category_attrs: list[tuple[str, str]] | None,
) -> str:
    """Return a JS array literal of option-group objects for the searchable select."""
    import json

    groups = []
    # Built-in fields (no group label)
    builtin_opts = [{"k": k, "v": v} for k, v, _t in _COMMON_FIELDS]
    groups.append({"label": None, "options": builtin_opts})
    # Global item-schema attributes (custom fields added by company)
    if global_extra:
        groups.append({
            "label": "Attributes",
            "options": [{"k": k, "v": v} for k, v in global_extra],
        })
    # Category attributes
    if category_attrs:
        groups.append({
            "label": "Category Attributes",
            "options": [{"k": k, "v": v} for k, v in category_attrs],
        })
    return json.dumps(groups)


def _field_row_compact(
    idx: int,
    field: dict,
    global_extra: list[tuple[str, str]] | None = None,
    category_attrs: list[tuple[str, str]] | None = None,
) -> object:
    """Compact field row: searchable field select + delete button.

    The field key determines the render type (barcode/qr/text) via _FIELD_TYPE_MAP.
    """
    ft = _ft()
    Div, Button, NotStr = ft["Div"], ft["Button"], ft["NotStr"]

    key = field.get("key", "")
    label_val = str(field.get("label", "") or key).replace("&", "&amp;").replace('"', "&quot;")
    ftype = field.get("type", "text")
    x_val = field.get("x", "")
    y_val = field.get("y", "")
    font_size = field.get("fontSize", "")

    # Build option groups as JSON for the searchable-select component
    options_js = _build_field_options_js(key, global_extra, category_attrs)
    safe_key = key.replace('"', "&quot;")
    ss_id = f"ss-{idx}"

    return Div(
        NotStr(
            f'<div id="{ss_id}" class="searchable-select"'
            f' data-name="fields[{idx}][key]" data-value="{safe_key}"'
            f' data-options=\'{options_js}\'>'
            f'<input type="text" readonly class="form-input form-input--sm searchable-select__display"'
            f' placeholder="-- select --">'
            f'<div class="searchable-select__dropdown">'
            f'<input type="text" class="searchable-select__search" placeholder="Search fields...">'
            f'<div class="searchable-select__list"></div>'
            f'</div>'
            f'<input type="hidden" name="fields[{idx}][key]" value="{safe_key}" class="fld-key">'
            f'</div>'
        ),
        NotStr(
            f'<input type="text" name="fields[{idx}][label]" value="{label_val}" class="form-input form-input--sm fld-label fld-label-input"'
            f' placeholder="Display label" title="Label shown on preview" oninput="this._userEdited=true; labelEditorUpdatePreview()">'
        ),
        Button(t("btn.u00d7"),
            type="button",
            onclick="this.closest('.field-row-compact').remove(); labelEditorUpdatePreview()",
            cls="btn btn--icon btn--danger",
            title="Remove field",
        ),
        NotStr(
            f'<input type="hidden" name="fields[{idx}][type]" value="{ftype}" class="fld-type">'
            f'<input type="hidden" name="fields[{idx}][x]" value="{x_val}" class="fld-x">'
            f'<input type="hidden" name="fields[{idx}][y]" value="{y_val}" class="fld-y">'
            f'<input type="hidden" name="fields[{idx}][fontSize]" value="{font_size}" class="fld-fs">'
        ),
        cls="field-row-compact",
        data_idx=str(idx),
    )


def _editor_panel(
    tpl: dict,
    global_extra: list[tuple[str, str]] | None = None,
    category_attrs: list[tuple[str, str]] | None = None,
) -> object:
    """Label template editor panel.

    global_extra: additional (key, label) tuples from company item_schema.
    category_attrs: (key, "Category > label") tuples from category-schemas.
    """
    ft = _ft()
    Div, H3, H4, Form, Input, Label, Select, Option, Button, Script, NotStr, Span, P = (
        ft["Div"], ft["H3"], ft["H4"], ft["Form"], ft["Input"], ft["Label"],
        ft["Select"], ft["Option"], ft["Button"], ft["Script"], ft["NotStr"],
        ft["Span"], ft["P"],
    )

    tid = tpl["id"]
    raw_fields = tpl.get("fields") or []
    field_dicts = [
        {"key": f, "label": f, "type": "text"} if isinstance(f, str) else f
        for f in raw_fields
    ]
    field_rows = [_field_row_compact(i, fd, global_extra, category_attrs) for i, fd in enumerate(field_dicts)]

    cur_fmt = tpl.get("format", "40x30mm")
    format_opts = [
        Option(label, value=val, selected=(val == cur_fmt))
        for val, label in _FORMAT_OPTIONS
    ]

    custom_display = "block" if cur_fmt == "custom" else "none"
    w_mm = tpl.get("width_mm") or ""
    h_mm = tpl.get("height_mm") or ""

    # Build unified field options for JS (key, label, render type) - used by add-field button
    all_fields_js_parts = [f'{{k:"{k}",v:"{v}",t:"{rtype}"}}' for k, v, rtype in _COMMON_FIELDS]
    if global_extra:
        for fk, fl in global_extra:
            safe_k = fk.replace('"', '\\"')
            safe_v = fl.replace('"', '\\"')
            all_fields_js_parts.append(f'{{k:"{safe_k}",v:"{safe_v}",t:"text"}}')
    if category_attrs:
        for fk, fl in category_attrs:
            safe_k = fk.replace('"', '\\"')
            safe_v = fl.replace('"', '\\"')
            all_fields_js_parts.append(f'{{k:"{safe_k}",v:"{safe_v}",t:"text"}}')
    all_fields_js = "[" + ",".join(all_fields_js_parts) + "]"

    # Option groups JSON for add-field searchable-select
    import json as _json
    add_field_groups: list[dict] = [
        {"label": None, "options": [{"k": k, "v": v} for k, v, _ in _COMMON_FIELDS]},
    ]
    if global_extra:
        add_field_groups.append({"label": "Attributes", "options": [{"k": k, "v": v} for k, v in global_extra]})
    if category_attrs:
        add_field_groups.append({"label": "Category Attributes", "options": [{"k": k, "v": v} for k, v in category_attrs]})
    add_field_groups_js = _json.dumps(add_field_groups)

    # Type map: key -> render type (barcode/qr/text)
    type_map_js_parts = [f'"{k}":"{t}"' for k, _v, t in _COMMON_FIELDS]
    type_map_js = "{" + ",".join(type_map_js_parts) + "}"

    sample_data_js = "{" + ", ".join(f'"{k}":"{v}"' for k, v in _SAMPLE_DATA.items()) + "}"
    format_sizes_js = "{'24x24mm':[24,24],'29x29mm':[29,29],'34x34mm':[34,34],'40x30mm':[40,30],'62x29mm':[62,29],'100x50mm':[100,50],'A4':[210,297],'A5':[148,210],'letter':[216,279]}"

    init_js = NotStr(f"""
<script>
(function() {{
  var MAX_CANVAS_PX = 400;
  var sampleData = {sample_data_js};
  var allFields = {all_fields_js};
  var typeMap = {type_map_js};
  var formatSizes = {format_sizes_js};
  var addFieldGroups = {add_field_groups_js};

  function fieldType(key) {{ return typeMap[key] || 'text'; }}

  function getCanvasDims() {{
    var fmtEl = document.getElementById('fmt-sel');
    var fmt = fmtEl ? fmtEl.value : '40x30mm';
    var wMm, hMm;
    if (fmt === 'custom') {{
      wMm = parseFloat((document.getElementById('custom-w') || {{}}).value) || 50;
      hMm = parseFloat((document.getElementById('custom-h') || {{}}).value) || 30;
    }} else {{
      var s = formatSizes[fmt];
      if (s) {{ wMm = s[0]; hMm = s[1]; }} else {{
        var m = fmt.toLowerCase().replace('mm','').split('x');
        wMm = parseFloat(m[0]) || 40; hMm = parseFloat(m[1]) || 30;
      }}
    }}
    var scale = Math.min(MAX_CANVAS_PX / wMm, MAX_CANVAS_PX / hMm, 5);
    return {{ wMm: wMm, hMm: hMm, scale: scale,
              wPx: Math.round(wMm * scale), hPx: Math.round(hMm * scale) }};
  }}

  var fmtSel = document.getElementById('fmt-sel');
  var customDims = document.getElementById('custom-dims');
  if (fmtSel && customDims) {{
    fmtSel.addEventListener('change', function() {{
      customDims.style.display = this.value === 'custom' ? 'block' : 'none';
      labelEditorUpdatePreview();
    }});
  }}

  function reindexFields() {{
    var list = document.getElementById('field-list');
    if (!list) return;
    list.querySelectorAll('.field-row-compact').forEach(function(row, i) {{
      row.setAttribute('data-idx', i);
      row.querySelectorAll('[name]').forEach(function(el) {{
        el.name = el.name.replace(/fields\\[(\\d+)\\]/, 'fields[' + i + ']');
      }});
      // Update searchable-select data-name
      var ss = row.querySelector('.searchable-select');
      if (ss) ss.setAttribute('data-name', 'fields[' + i + '][key]');
    }});
  }}
  window.reindexFields = reindexFields;

  // ── Searchable select component ──────────────────────────────────────────
  // initSearchableSelect(wrapperEl, groups)
  // groups: [{{label: str|null, options: [{{k,v}}]}}]
  function initSearchableSelect(wrapper, groups) {{
    if (!wrapper || wrapper._ssInit) return;
    wrapper._ssInit = true;
    var display = wrapper.querySelector('.searchable-select__display');
    var dropdown = wrapper.querySelector('.searchable-select__dropdown');
    var searchIn = wrapper.querySelector('.searchable-select__search');
    var listEl = wrapper.querySelector('.searchable-select__list');
    var hiddenIn = wrapper.querySelector('input[type=hidden]');
    var currentVal = wrapper.getAttribute('data-value') || '';

    function renderOptions(filter) {{
      listEl.innerHTML = '';
      var f = filter ? filter.toLowerCase() : '';
      groups.forEach(function(grp) {{
        var visOpts = grp.options.filter(function(o) {{
          return !f || o.v.toLowerCase().includes(f) || o.k.toLowerCase().includes(f);
        }});
        if (!visOpts.length) return;
        if (grp.label) {{
          var gl = document.createElement('div');
          gl.className = 'searchable-select__group-label';
          gl.textContent = grp.label;
          listEl.appendChild(gl);
        }}
        visOpts.forEach(function(o) {{
          var opt = document.createElement('div');
          opt.className = 'searchable-select__option';
          opt.textContent = o.v;
          opt.setAttribute('data-key', o.k);
          if (o.k === currentVal) opt.setAttribute('aria-selected', 'true');
          opt.addEventListener('mousedown', function(e) {{
            e.preventDefault();
            selectOption(o.k, o.v);
          }});
          listEl.appendChild(opt);
        }});
      }});
    }}

    function selectOption(k, v) {{
      currentVal = k;
      display.value = v || '';
      if (hiddenIn) hiddenIn.value = k;
      // Sync fld-type and fld-label hidden inputs in the parent row
      var row = wrapper.closest('.field-row-compact');
      if (row) {{
        var typeIn = row.querySelector('.fld-type');
        if (typeIn) typeIn.value = fieldType(k);
        var labelIn = row.querySelector('.fld-label');
        if (labelIn && !labelIn._userEdited) labelIn.value = v || k;
      }}
      closeDropdown();
      labelEditorUpdatePreview();
    }}

    function openDropdown() {{
      renderOptions('');
      dropdown.classList.add('open');
      searchIn.value = '';
      searchIn.focus();
    }}
    function closeDropdown() {{
      dropdown.classList.remove('open');
    }}

    display.addEventListener('click', function(e) {{
      e.stopPropagation();
      if (dropdown.classList.contains('open')) {{ closeDropdown(); }} else {{ openDropdown(); }}
    }});
    searchIn.addEventListener('input', function() {{
      renderOptions(this.value);
    }});
    searchIn.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') closeDropdown();
    }});
    document.addEventListener('click', function(e) {{
      if (!wrapper.contains(e.target)) closeDropdown();
    }}, true);

    // Init display with current value label
    if (currentVal) {{
      var found = null;
      groups.forEach(function(grp) {{
        grp.options.forEach(function(o) {{ if (o.k === currentVal) found = o.v; }});
      }});
      display.value = found || currentVal;
    }}
  }}
  window.initSearchableSelect = initSearchableSelect;

  // Init all existing searchable-selects (options stored in data-options attribute)
  document.querySelectorAll('.field-row-compact .searchable-select').forEach(function(w) {{
    var opts = w.getAttribute('data-options');
    if (opts) {{
      try {{ initSearchableSelect(w, JSON.parse(opts)); }} catch(e) {{}}
    }}
  }});

  var addBtn = document.getElementById('add-field-btn');
  if (addBtn) {{
    addBtn.addEventListener('click', function() {{
      var list = document.getElementById('field-list');
      var idx = list.querySelectorAll('.field-row-compact').length;
      var row = document.createElement('div');
      row.className = 'field-row-compact';
      row.setAttribute('data-idx', idx);
      row.innerHTML =
        '<div class="searchable-select" data-name="fields[' + idx + '][key]" data-value="">' +
        '<input type="text" readonly class="form-input form-input--sm searchable-select__display" placeholder="-- select --">' +
        '<div class="searchable-select__dropdown">' +
        '<input type="text" class="searchable-select__search" placeholder="Search fields...">' +
        '<div class="searchable-select__list"></div>' +
        '</div>' +
        '<input type="hidden" name="fields[' + idx + '][key]" value="" class="fld-key">' +
        '</div>' +
        '<input type="text" name="fields[' + idx + '][label]" value="" class="form-input form-input--sm fld-label fld-label-input"' +
        ' placeholder="Display label" title="Label shown on preview" oninput="this._userEdited=true; labelEditorUpdatePreview()">' +
        '<button type="button" class="btn btn--icon btn--danger" title="Remove field"' +
        ' onclick="this.closest(\\'.field-row-compact\\').remove(); reindexFields(); labelEditorUpdatePreview()">×</button>' +
        '<input type="hidden" name="fields[' + idx + '][type]" value="text" class="fld-type">' +
        '<input type="hidden" name="fields[' + idx + '][x]" value="" class="fld-x">' +
        '<input type="hidden" name="fields[' + idx + '][y]" value="" class="fld-y">' +
        '<input type="hidden" name="fields[' + idx + '][fontSize]" value="" class="fld-fs">';
      list.appendChild(row);
      initSearchableSelect(row.querySelector('.searchable-select'), addFieldGroups);
      labelEditorUpdatePreview();
    }});
  }}

  // ── Drag to reposition ───────────────────────────────────────────────────
  var _drag = null;
  function setupBlockDrag(block, idx, scale) {{
    block.addEventListener('mousedown', function(e) {{
      e.preventDefault();
      var canvas = document.getElementById('preview-canvas');
      _drag = {{
        block: block, idx: idx, scale: scale,
        startX: e.clientX, startY: e.clientY,
        origLeft: parseInt(block.style.left) || 0,
        origTop: parseInt(block.style.top) || 0,
        canvasW: canvas ? canvas.offsetWidth : 400,
        canvasH: canvas ? canvas.offsetHeight : 300,
      }};
    }});
  }}
  document.addEventListener('mousemove', function(e) {{
    if (!_drag) return;
    var dx = e.clientX - _drag.startX;
    var dy = e.clientY - _drag.startY;
    var canvas = document.getElementById('preview-canvas');
    var maxLeft = (canvas ? canvas.offsetWidth : _drag.canvasW) - (_drag.block.offsetWidth || 30);
    var maxTop = (canvas ? canvas.offsetHeight : _drag.canvasH) - (_drag.block.offsetHeight || 12);
    _drag.block.style.left = Math.max(0, Math.min(_drag.origLeft + dx, maxLeft)) + 'px';
    _drag.block.style.top = Math.max(0, Math.min(_drag.origTop + dy, maxTop)) + 'px';
  }});
  document.addEventListener('mouseup', function() {{
    if (!_drag) return;
    var xMm = (parseInt(_drag.block.style.left) || 0) / _drag.scale;
    var yMm = (parseInt(_drag.block.style.top) || 0) / _drag.scale;
    var list = document.getElementById('field-list');
    if (list) {{
      var rows = list.querySelectorAll('.field-row-compact');
      if (_drag.idx < rows.length) {{
        var row = rows[_drag.idx];
        var xIn = row.querySelector('.fld-x');
        var yIn = row.querySelector('.fld-y');
        if (xIn) xIn.value = xMm.toFixed(2);
        if (yIn) yIn.value = yMm.toFixed(2);
      }}
    }}
    _drag = null;
  }});

  // ── Preview renderer (uses server-generated barcode/QR images) ─────────
  // Fixed QR size: always 10mm. Barcode: min 20mm wide, min 6mm tall.
  var QR_SIZE_MM = 10;
  var BC_MIN_W_MM = 20;
  var BC_MIN_H_MM = 6;

  window.labelEditorUpdatePreview = function() {{
    var canvas = document.getElementById('preview-canvas');
    if (!canvas) return;
    var dims = getCanvasDims();
    canvas.style.width = dims.wPx + 'px';
    canvas.style.height = dims.hPx + 'px';
    canvas.innerHTML = '';
    var list = document.getElementById('field-list');
    if (!list) return;
    var rows = list.querySelectorAll('.field-row-compact');
    var autoY = 2;
    rows.forEach(function(row, i) {{
      var hiddenKey = row.querySelector('.fld-key') || row.querySelector('[name*="[key]"]');
      var typeIn = row.querySelector('.fld-type');
      var labelIn = row.querySelector('.fld-label');
      var xEl = row.querySelector('.fld-x');
      var yEl = row.querySelector('.fld-y');
      var fsEl = row.querySelector('.fld-fs');
      var key = hiddenKey ? hiddenKey.value.trim() : '';
      if (!key) return;
      var ftype = typeIn ? typeIn.value : fieldType(key);
      var fieldLabel = labelIn ? (labelIn.value || '').trim() : '';
      var xMm = (xEl && xEl.value !== '') ? parseFloat(xEl.value) : null;
      var yMm = (yEl && yEl.value !== '') ? parseFloat(yEl.value) : null;
      var fs = (fsEl && fsEl.value !== '') ? parseFloat(fsEl.value) : 7;
      var lineHMm = fs * 0.35 * 1.4;
      var xPos, yPos;
      if (xMm !== null && !isNaN(xMm) && yMm !== null && !isNaN(yMm)) {{
        xPos = xMm; yPos = yMm;
      }} else {{
        xPos = 2; yPos = autoY;
        autoY += lineHMm + 1;
      }}

      var sample = sampleData[key] || key;
      var block = document.createElement('div');
      block.style.left = (xPos * dims.scale) + 'px';
      block.style.top = (yPos * dims.scale) + 'px';
      var scaledFs = Math.max(6, fs * dims.scale * 0.35);
      block.style.fontSize = scaledFs + 'px';

      if (ftype === 'barcode') {{
        block.className = 'label-field-block label-field-block--barcode';
        var bcWPx = Math.round(Math.max(BC_MIN_W_MM, Math.min(dims.wMm - xPos - 2, 30)) * dims.scale);
        var bcHPx = Math.round(Math.max(BC_MIN_H_MM, Math.min(8, dims.hMm / 4)) * dims.scale);
        block.style.width = bcWPx + 'px';
        var img = document.createElement('img');
        img.src = '/api/labels/preview/barcode?value=' + encodeURIComponent(sample);
        img.style.width = bcWPx + 'px';
        img.style.height = bcHPx + 'px';
        img.alt = 'barcode';
        var txt = document.createElement('span');
        txt.className = 'bc-text';
        txt.textContent = sample;
        block.appendChild(img);
        block.appendChild(txt);
      }} else if (ftype === 'qr') {{
        block.className = 'label-field-block label-field-block--qr';
        // QR is always exactly 10mm
        var qrPx = Math.round(QR_SIZE_MM * dims.scale);
        block.style.width = qrPx + 'px';
        block.style.height = qrPx + 'px';
        var img = document.createElement('img');
        img.src = '/api/labels/preview/qr?value=' + encodeURIComponent(sample);
        img.alt = 'qr';
        block.appendChild(img);
      }} else {{
        block.className = 'label-field-block';
        // Show "label: value" for text fields so fields are distinguishable
        block.textContent = fieldLabel ? (fieldLabel + ': ' + sample) : sample;
      }}

      setupBlockDrag(block, i, dims.scale);
      canvas.appendChild(block);
    }});
  }};

  // Render preview after DOM is fully settled.
  // On initial page load: immediate call is fine since DOM is complete.
  // After HTMX outerHTML swap: the inline script runs during swap processing,
  // but HTMX may clear/reset styles afterward. We listen for htmx:afterSettle
  // which fires once the swap is fully committed.
  if (typeof htmx !== 'undefined') {{
    // HTMX is loaded: might be a swap. Listen once for afterSettle.
    var settled = false;
    document.addEventListener('htmx:afterSettle', function onSettle() {{
      document.removeEventListener('htmx:afterSettle', onSettle);
      settled = true;
      labelEditorUpdatePreview();
    }});
    // Fallback: if afterSettle doesn't fire within 200ms (full page load),
    // render immediately.
    setTimeout(function() {{
      if (!settled) labelEditorUpdatePreview();
    }}, 200);
  }} else {{
    // No HTMX: plain page load.
    labelEditorUpdatePreview();
  }}
}})();
</script>
""")

    return Div(
        H3(f"Edit: {tpl['name']}", cls="section-title"),
        Form(
            # Top bar: name then format (stacked, half-width)
            Div(
                Div(
                    Label(t("label.template_name")),
                    Input(name="name", value=tpl["name"], cls="form-input form-input--sm", required=True),
                    cls="labels-form-field",
                ),
                Div(
                    Label(t("label.format_mm")),
                    Select(*format_opts, name="format", id="fmt-sel", cls="form-input form-input--sm"),
                    cls="labels-form-field",
                ),
                cls="labels-top-bar",
            ),
            # Custom dimensions (visible only when format = custom)
            Div(
                Div(
                    Label(t("label.width_mm")),
                    Input(name="width_mm", type="number", step="0.5", min="5",
                          id="custom-w", value=str(w_mm) if w_mm else "",
                          placeholder="e.g. 50", cls="form-input form-input--sm",
                          oninput="labelEditorUpdatePreview()"),
                    cls="labels-form-field",
                ),
                Div(
                    Label(t("label.height_mm")),
                    Input(name="height_mm", type="number", step="0.5", min="5",
                          id="custom-h", value=str(h_mm) if h_mm else "",
                          placeholder="e.g. 30", cls="form-input form-input--sm",
                          oninput="labelEditorUpdatePreview()"),
                    cls="labels-form-field",
                ),
                id="custom-dims",
                cls="custom-dims",
                style=f"display:{custom_display}",
            ),
            # Stacked layout: preview on top, fields below
            Div(
                Div(
                    H4(t("page.preview_drag_to_reposition")),
                    Div(id="preview-canvas", cls="label-canvas"),
                    cls="label-canvas-panel",
                ),
                Div(
                    H4(t("page.fields")),
                    Div(*field_rows, id="field-list", cls="field-list"),
                    Button(t("btn._add_field"), type="button", id="add-field-btn",
                           cls="btn btn--sm btn--outline"),
                    cls="label-fields-panel",
                ),
                cls="label-editor-layout",
            ),
            Div(
                Button(t("btn.save"), type="submit", cls="btn btn--primary"),
                cls="form-actions",
            ),
            hx_put=f"/settings/labels/{tid}",
            hx_target="#label-editor-panel",
            hx_swap="outerHTML",
        ),
        init_js,
        id="label-editor-panel",
        cls="settings-card",
    )


def _empty_editor() -> object:
    ft = _ft()
    return ft["Div"](
        ft["P"]("Select a template to edit, or create a new one.", cls="text-muted"),
        id="label-editor-panel",
        cls="settings-card",
    )


def _label_settings_root(
    templates: list[dict],
    active_id: str | None = None,
    editor: object | None = None,
    flash: str | None = None,
    flash_kind: str = "success",
) -> object:
    ft = _ft()
    Div, Script, NotStr = ft["Div"], ft["Script"], ft["NotStr"]
    from ui.components.table import breadcrumbs
    crumbs = breadcrumbs([("Inventory", "/inventory"), ("\u2699\ufe0f Inventory Settings", "/settings/inventory"), ("Labels", None)])
    return Div(
        NotStr(_LABELS_CSS),
        crumbs,
        _flash(flash, flash_kind) if flash else Div(),
        Div(
            _templates_list(templates, active_id),
            editor or _empty_editor(),
            cls="label-settings-layout",
        ),
        # SortableJS removed - drag-and-drop is on the canvas only
        id="label-settings-root",
    )


def _bulk_print_preview_page(entity_ids: list[str], templates: list[dict], api_base: str, token: str | None) -> object:
    """Show template picker + confirm Print button for bulk label printing."""
    ft = _ft()
    Div, H2, Form, Label, Select, Option, Button, P, Input = (
        ft["Div"], ft["H2"], ft["Form"], ft["Label"], ft["Select"],
        ft["Option"], ft["Button"], ft["P"], ft["Input"],
    )
    from ui.components.shell import base_shell

    hidden_ids = [Input(type="hidden", name="selected", value=eid) for eid in entity_ids]
    template_opts = [Option(tpl["name"], value=tpl["id"]) for tpl in templates]

    return base_shell(
        Div(
            H2(f"Print Labels — {len(entity_ids)} item(s)"),
            P(f"Selected: {', '.join(entity_ids[:5])}{'...' if len(entity_ids) > 5 else ''}"),
            Form(
                *hidden_ids,
                Div(
                    Label(t("label.label_template"), cls="form-label"),
                    Select(*template_opts, name="template_id", cls="form-input") if template_opts
                    else P(t("label.no_templates_found_create_one_in_settings_labels"), cls="flash flash--warning"),
                    cls="form-group",
                ),
                Button(t("btn._print_labels"), type="submit", cls="btn btn--primary"),
                action="/labels/print-bulk/generate",
                method="post",
            ) if template_opts else Div(
                P(t("label.no_label_templates_configured"), cls="flash flash--warning"),
                Button(t("btn.back_to_settings"), onclick="history.back()", cls="btn btn--secondary", type="button"),
            ),
            cls="settings-card",
            style="max-width:480px;margin:2rem auto;",
        ),
        title="Print Labels - Celerp",
        nav_active="inventory",
        request=request,
    )


def _printable_label_sheet(items: list[dict], template: dict | None) -> object:
    """Return a minimal printable HTML page that auto-triggers window.print()."""
    if not template:
        fields = [{"key": "name", "type": "text"}, {"key": "sku", "type": "text"}]
    else:
        fields = template.get("fields") or [{"key": "name", "type": "text"}, {"key": "sku", "type": "text"}]

    from starlette.responses import HTMLResponse

    label_rows = []
    for item in items:
        field_lines = []
        for f in fields:
            key = f.get("key", "")
            ftype = f.get("type", "text")
            field_label = str(f.get("label", "") or key).strip()
            val = str(item.get(key, "") or (item.get("attributes") or {}).get(key, "") or "")
            if not val:
                continue
            if ftype == "barcode":
                field_lines.append(f'<div class="label-field label-field--barcode">{val}</div>')
            elif ftype == "qr":
                field_lines.append(f'<div class="label-field label-field--qr">{val}</div>')
            else:
                display = f"{field_label}: {val}" if field_label else val
                field_lines.append(f'<div class="label-field">{display}</div>')
        label_rows.append(f'<div class="label-item">{"".join(field_lines)}</div>')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Print Labels</title>
<style>
body {{ font-family: sans-serif; margin: 0; padding: 1rem; }}
.label-sheet {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.label-item {{ border: 1px solid #999; padding: 6px 8px; min-width: 80px; font-size: 11px; break-inside: avoid; }}
.label-field {{ margin-bottom: 2px; }}
.label-field--barcode, .label-field--qr {{ font-family: monospace; font-size: 10px; }}
.no-print {{ margin-bottom: 1rem; }}
@media print {{ .no-print {{ display: none; }} }}
</style>
</head>
<body>
<div class="no-print">
  <button onclick="window.print()" style="padding:8px 16px;cursor:pointer;font-size:14px;">🖨 Print</button>
  <button onclick="history.back()" style="padding:8px 16px;cursor:pointer;font-size:14px;margin-left:8px;">← Back</button>
</div>
<div class="label-sheet">{"".join(label_rows)}</div>
<script>window.onload = function() {{ window.print(); }};</script>
</body>
</html>"""
    return HTMLResponse(content=html)


def setup_ui_routes(app) -> None:
    """Entry point called by the module loader."""
    import httpx
    from ui.components.shell import base_shell

    def _token(request: Request) -> str | None:
        from ui.config import COOKIE_NAME
        return request.cookies.get(COOKIE_NAME)

    def _api_base(request: Request) -> str:
        host = request.url.hostname or "localhost"
        return f"http://{host}:8000"

    async def _fetch_templates(request: Request) -> list[dict]:
        token = _token(request)
        if not token:
            return []
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{_api_base(request)}/api/labels/templates",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    return r.json().get("items", [])
        except Exception as exc:
            log.warning("Could not fetch label templates: %s", exc)
        return []

    async def _seed_presets_if_empty(request: Request) -> list[dict]:
        """Fetch templates; if empty, seed presets via API and return them."""
        templates = await _fetch_templates(request)
        if templates:
            return templates
        token = _token(request)
        if not token:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                for preset in _PRESET_TEMPLATES:
                    await c.post(
                        f"{_api_base(request)}/api/labels/templates",
                        json=preset,
                        headers={"Authorization": f"Bearer {token}"},
                    )
        except Exception as exc:
            log.warning("Could not seed preset label templates: %s", exc)
        return await _fetch_templates(request)

    async def _fetch_extra_fields(request: Request) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Fetch ALL available item fields for the label field picker.

        Combines three sources:
        1. item-schema: company-configured custom fields
        2. price-lists: dynamic price columns (e.g. retail_price, wholesale_price)
        3. category-schemas: per-category attribute fields
        4. item attribute keys: discovered from actual inventory items

        Returns:
            (global_extra, category_attrs) where:
            - global_extra: (key, label) from schema + prices + discovered attrs
            - category_attrs: (key, "Category > Label") from category-schemas
        """
        token = _token(request)
        if not token:
            return [], []
        builtin_keys = {k for k, _, _ in _COMMON_FIELDS}
        global_extra: list[tuple[str, str]] = []
        category_attrs: list[tuple[str, str]] = []
        seen_keys: set[str] = set(builtin_keys)
        try:
            base = _api_base(request)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=5) as c:
                # 1. Global item-schema
                r = await c.get(f"{base}/companies/me/item-schema", headers=headers)
                if r.status_code == 200:
                    schema = r.json() if isinstance(r.json(), list) else r.json().get("fields", [])
                    for f in schema:
                        fk = f.get("key", "")
                        if fk and fk not in seen_keys:
                            seen_keys.add(fk)
                            global_extra.append((fk, f.get("label", fk)))

                # 2. Price lists -> dynamic price columns
                r_pl = await c.get(f"{base}/companies/me/price-lists", headers=headers)
                if r_pl.status_code == 200:
                    for pl in (r_pl.json() if isinstance(r_pl.json(), list) else []):
                        name = pl.get("name", "")
                        if not name:
                            continue
                        pk = f"{name.lower()}_price"
                        if pk not in seen_keys:
                            seen_keys.add(pk)
                            global_extra.append((pk, f"{name} Price"))

                # 3. Category-schemas
                r2 = await c.get(f"{base}/companies/me/category-schemas", headers=headers)
                if r2.status_code == 200:
                    cat_data = r2.json()
                    if isinstance(cat_data, dict):
                        for cat_name, fields in cat_data.items():
                            if not isinstance(fields, list):
                                continue
                            for f in fields:
                                fk = f.get("key", "")
                                if not fk or fk in seen_keys:
                                    continue
                                seen_keys.add(fk)
                                fl = f.get("label", fk)
                                category_attrs.append((fk, f"{cat_name} \u203a {fl}"))

                # 4. Discover attribute keys from actual items (sample)
                r_items = await c.get(f"{base}/items?limit=20", headers=headers)
                if r_items.status_code == 200:
                    items_data = r_items.json()
                    item_list = items_data.get("items", []) if isinstance(items_data, dict) else items_data
                    for item in (item_list if isinstance(item_list, list) else []):
                        # Check top-level keys
                        for k in item:
                            if k not in seen_keys and not k.startswith("_") and k not in ("entity_id", "company_id", "id"):
                                seen_keys.add(k)
                                global_extra.append((k, k.replace("_", " ").title()))
                        # Check attributes sub-dict
                        attrs = item.get("attributes") or {}
                        if isinstance(attrs, dict):
                            for k in attrs:
                                if k not in seen_keys:
                                    seen_keys.add(k)
                                    global_extra.append((k, k.replace("_", " ").title()))
        except Exception as exc:
            log.debug("Could not fetch field schema for labels: %s", exc)
        return global_extra, category_attrs

    # ── Barcode/QR image preview endpoints (served from UI app) ─────────
    from starlette.responses import Response as StarletteResponse

    @app.get("/api/labels/preview/barcode")
    async def barcode_preview(request: Request):
        from celerp_labels.service import _make_barcode_image
        value = request.query_params.get("value", "0000000")
        buf = _make_barcode_image(value)
        if buf:
            return StarletteResponse(content=buf.read(), media_type="image/png",
                                     headers={"Cache-Control": "public, max-age=3600"})
        return StarletteResponse(content=b"", status_code=204)

    @app.get("/api/labels/preview/qr")
    async def qr_preview(request: Request):
        from celerp_labels.service import _make_qr_image
        value = request.query_params.get("value", "0000000")
        buf = _make_qr_image(value)
        if buf:
            return StarletteResponse(content=buf.read(), media_type="image/png",
                                     headers={"Cache-Control": "public, max-age=3600"})
        return StarletteResponse(content=b"", status_code=204)

    # /labels redirects to /settings/labels (single page, no redundancy)
    @app.get("/labels")
    async def labels_redirect(request: Request):
        return RedirectResponse("/settings/labels", status_code=302)

    @app.get("/settings/labels")
    async def label_settings(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        templates = await _seed_presets_if_empty(request)
        # Auto-redirect to first template if any exist
        if templates:
            return RedirectResponse(f"/settings/labels/{templates[0]['id']}", status_code=302)
        return base_shell(
            _label_settings_root(templates),
            title="Label Templates - Celerp",
            nav_active="labels",
            request=request,
        )

    @app.get("/settings/labels/{tmpl_id}")
    async def label_settings_edit(request: Request, tmpl_id: str):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        templates = await _seed_presets_if_empty(request)
        tpl = next((x for x in templates if x["id"] == tmpl_id), None)
        if not tpl:
            return base_shell(
                _label_settings_root(templates, flash="Template not found.", flash_kind="error"),
                title="Label Templates - Celerp",
                nav_active="labels",
                request=request,
            )
        global_extra, category_attrs = await _fetch_extra_fields(request)
        return base_shell(
            _label_settings_root(templates, active_id=tmpl_id, editor=_editor_panel(tpl, global_extra, category_attrs)),
            title=f"Edit: {tpl['name']} - Celerp",
            nav_active="labels",
            request=request,
        )

    @app.post("/settings/labels")
    async def label_settings_create(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            templates = await _fetch_templates(request)
            return _label_settings_root(templates, flash="Template name required.", flash_kind="error")
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(
                    f"{_api_base(request)}/api/labels/templates",
                    json={"name": name},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 201:
                    new_t = r.json()
                    templates = await _fetch_templates(request)
                    global_extra, category_attrs = await _fetch_extra_fields(request)
                    return _label_settings_root(
                        templates, active_id=new_t["id"],
                        editor=_editor_panel(new_t, global_extra, category_attrs),
                        flash=f"Created '{name}'.",
                    )
                error = r.json().get("detail", "Unknown error")
        except Exception as exc:
            error = str(exc)
        templates = await _fetch_templates(request)
        return _label_settings_root(templates, flash=f"Could not create: {error}", flash_kind="error")

    @app.delete("/settings/labels/{tmpl_id}")
    async def label_settings_delete(request: Request, tmpl_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.delete(
                    f"{_api_base(request)}/api/labels/templates/{tmpl_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as exc:
            log.warning("Delete template %s: %s", tmpl_id, exc)
        templates = await _fetch_templates(request)
        return _label_settings_root(templates, flash="Template deleted.")

    @app.put("/settings/labels/{tmpl_id}")
    async def label_settings_save(request: Request, tmpl_id: str):
        """Save template - returns refreshed editor panel only (HTMX target)."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        name = (form.get("name") or "").strip() or "Untitled"
        fmt = form.get("format") or "40x30mm"

        width_mm = _parse_float(form.get("width_mm"))
        height_mm = _parse_float(form.get("height_mm"))
        fields = _extract_fields_from_form(form)

        payload: dict = {"name": name, "format": fmt, "fields": fields}
        if width_mm is not None:
            payload["width_mm"] = width_mm
        if height_mm is not None:
            payload["height_mm"] = height_mm

        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.put(
                    f"{_api_base(request)}/api/labels/templates/{tmpl_id}",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    saved = r.json()
                    global_extra, category_attrs = await _fetch_extra_fields(request)
                    return _editor_panel(saved, global_extra, category_attrs)
        except Exception as exc:
            log.warning("Save template %s: %s", tmpl_id, exc)

        return _editor_panel({
            "id": tmpl_id, "name": name, "format": fmt,
            "fields": fields, "width_mm": width_mm, "height_mm": height_mm,
        })

    @app.post("/labels/print-bulk")
    async def labels_print_bulk(request: Request):
        """Bulk print preview: show template picker + print button for selected items."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return RedirectResponse("/inventory", status_code=302)
        templates = await _seed_presets_if_empty(request)
        return _bulk_print_preview_page(entity_ids, templates, _api_base(request), token)

    @app.post("/labels/print-bulk/generate")
    async def labels_print_bulk_generate(request: Request):
        """Generate printable HTML label sheet and trigger window.print()."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        template_id = str(form.get("template_id", "")).strip() or None
        if not entity_ids:
            return RedirectResponse("/inventory", status_code=302)
        # Fetch items and template
        items_data = []
        async with httpx.AsyncClient(timeout=10) as c:
            for eid in entity_ids:
                try:
                    r = await c.get(
                        f"{_api_base(request)}/items/{eid}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if r.status_code == 200:
                        items_data.append(r.json())
                except Exception:
                    pass
        template = None
        if template_id:
            templates = await _fetch_templates(request)
            template = next((tpl for tpl in templates if tpl["id"] == template_id), None)
        if not template:
            templates = await _seed_presets_if_empty(request)
            template = templates[0] if templates else None
        return _printable_label_sheet(items_data, template)

    log.info("celerp-labels: UI routes registered")


def _parse_float(val) -> float | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _extract_fields_from_form(form) -> list[dict]:
    """Parse fields[N][key/label/x/y/fontSize/bold/type] from multidict into ordered list."""
    import re
    buckets: dict[int, dict] = {}
    for key, val in form.multi_items():
        m = re.match(r"^fields\[(\d+)\]\[(\w+)\]$", key)
        if m:
            idx = int(m.group(1))
            attr = m.group(2)
            buckets.setdefault(idx, {})[attr] = val
    result = []
    for idx in sorted(buckets):
        row = buckets[idx]
        k = (row.get("key") or "").strip()
        if not k:
            continue
        field: dict = {
            "key": k,
            "label": (row.get("label") or k).strip(),
            "type": row.get("type", "text"),
        }
        for num_attr in ("x", "y", "fontSize"):
            v = _parse_float(row.get(num_attr))
            if v is not None:
                field[num_attr] = v
        if row.get("bold") in ("true", "1", "on"):
            field["bold"] = True
        result.append(field)
    return result

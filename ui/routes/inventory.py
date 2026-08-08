# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError, _flatten_item_attrs
from ui.components.files import files_section as _shared_files_section
from ui.components.shell import base_shell, page_header, toast_header
from ui.components.table import data_table, search_bar, pagination, EMPTY, breadcrumbs, status_cards, empty_state_cta, add_new_option, searchable_select, currency_symbol, INACTIVE_ITEM_STATUSES, SERVER_FILTER_JS, filter_th, sortable_th, table_pager, COLUMN_FILTER_JS, ENHANCED_TABLE_JS, date_range_filter
from ui.config import get_token as _token, get_role as _get_role
from celerp.services.permissions import role_has_permission
from celerp.services.field_schema import AMOUNT_ITEM_KEYS
from celerp.services.pricing import DEFAULT_PRICE_LIST_NAME, PRICE_LISTS_FALLBACK, is_cost_list_name, is_derived, price_key, resolve_price
from celerp.events.schemas import _WORKFLOW_TIME_UNITS
from ui.routes.documents import _ICON_PRINT as _ICON_PRINT_SVG
from ui.i18n import t, get_lang, is_rtl
from celerp.services.units import is_weight_unit, is_pieces_unit

_DEFAULT_PER_PAGE = 50


def _sp_static_td(val, num: bool = False) -> FT:
    """Static (non-editable) split-preview table cell; num right-aligns per HTML/CSS rule 4a."""
    return Td(val, cls="sp-td sp-td--num" if num else "sp-td")

# Shared live-delta helper, injected on the split surfaces that show a net gain/loss badge:
# the split preview table (used by both the bulk-split preview and the item-detail split
# card) and the transform preview badge. One definition, one term for the sum-and-subtract
# math: delta = parcel weight - sum of the elements marked data-delta-weight. Which elements
# carry that marker is a template decision per surface:
#   - split preview table: marks BOTH the mother's post-split element and each child's
#     outgoing element, so delta = original - (mother_post + children) = the net gain/loss,
#     non-zero only when the mother is edited independently or the children exceed it.
#   - transform preview: marks the child's outgoing element alone (trim-loss, parent_in -
#     child_out, not net gain/loss).
# Parcel weight comes from data-parent-weight (or data-parent-qty when the parcel is
# weight-sold, data-sell-by-weight="1"). The previews open with prefilled child weights and
# show a value immediately.
_SPLIT_DELTA_JS = """
function _updateSplitDelta(form) {
  var deltaEl = form.querySelector('.sp-delta-val');
  if (!deltaEl) return;
  var wDec = parseInt(form.dataset.weightDecimals || '2', 10);
  // Parcel weight: data-parent-weight, falling back to data-parent-qty for a weight-sold
  // parcel where quantity IS the weight (data-sell-by-weight="1").
  var parentWeight = parseFloat(form.dataset.parentWeight);
  if (isNaN(parentWeight) && form.dataset.sellByWeight === '1') {
    parentWeight = parseFloat(form.dataset.parentQty);
  }
  function setDelta(txt, negative) {
    deltaEl.textContent = txt;
    var badge = deltaEl.closest('.sp-delta');
    if (badge) badge.classList.toggle('cell--negative', !!negative);
  }
  // The template marks whichever elements carry the surface's split-weight terms with
  // data-delta-weight; this function only sums the marker, never a field name, so which
  // elements are marked (child only, or mother-post-split plus children) is a template
  // decision per surface, not a branch here.
  var sumW = 0;
  form.querySelectorAll('[data-delta-weight]').forEach(function(el) {
    var raw = (el.value !== undefined ? el.value : el.textContent);
    var v = parseFloat(raw);
    if (!isNaN(v)) sumW += v;
  });
  if (isNaN(parentWeight)) { setDelta('--', false); return; }
  var rounded = parseFloat((parentWeight - sumW).toFixed(wDec));  // own unclamped subtraction, never Math.max
  setDelta(rounded.toFixed(wDec), rounded < 0);
}
"""

_BULK_SPLIT_JS = """
function _setMotherQty(form, val, decimals) {
  // Mother qty is now an editable input; update its value reactively.
  // Only updates if the user has NOT manually edited the mother qty field.
  if (form.dataset.motherEdited === 'true') return;
  var inp = form.querySelector('.mother-qty-input');
  if (inp) inp.value = val.toFixed(decimals);
}
function _sumInputs(form, sel) {
  // Sum the numeric value/textContent of every element matching sel (N children).
  var total = 0;
  form.querySelectorAll(sel).forEach(function(el) {
    var raw = (el.value !== undefined ? el.value : el.textContent);
    var v = parseFloat(raw);
    if (!isNaN(v)) total += v;
  });
  return total;
}
function _updateSplitTotals(form) {
  // Sum QTY, Weight, Pieces across mother + all child rows and update tfoot cells.
  var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
  var weightDecimals = parseInt(form.dataset.weightDecimals || '2', 10);
  // QTY total
  var qtyTotal = form.querySelector('.sp-total-qty');
  if (qtyTotal) {
    var motherQtyEl = form.querySelector('.mother-qty-input');
    var motherQty = motherQtyEl ? (parseFloat(motherQtyEl.value) || 0) : 0;
    qtyTotal.textContent = (motherQty + _sumInputs(form, '[name="child_qty"]')).toFixed(decimals);
  }
  // Weight total: each child row carries either an editable input or a static display.
  var weightTotal = form.querySelector('.sp-total-weight');
  if (weightTotal) {
    var mwEl = form.querySelector('.mother-weight-display');
    var mw = mwEl ? (parseFloat(mwEl.textContent) || 0) : 0;
    var cw = _sumInputs(form, '[name="child_weight"]') + _sumInputs(form, '.child-weight-display');
    weightTotal.textContent = (mw + cw).toFixed(weightDecimals);
  }
  // Pieces total
  var piecesTotal = form.querySelector('.sp-total-pieces');
  if (piecesTotal) {
    var mpEl = form.querySelector('.mother-pieces-display');
    var mp = mpEl ? (parseInt(mpEl.textContent) || 0) : 0;
    var cp = _sumInputs(form, '[name="child_pieces"]') + _sumInputs(form, '.child-pieces-display');
    piecesTotal.textContent = String(mp + cp);
  }
  // Sync hidden inputs so form submission carries mother weight for server override.
  var mwHidden = form.querySelector('[name="mother_weight"]');
  if (mwHidden) {
    var mwDisp = form.querySelector('.mother-weight-display');
    if (mwDisp) mwHidden.value = mwDisp.textContent.trim();
  }
  _updateSplitDelta(form);
}
function splitRecalc(form) {
  // Shared recompute after a child changes, is added, or is removed: derive the mother
  // from parentQty minus the sum of all children (unless the mother is user-owned), keep
  // the mother weight/pieces displays in step, then refresh totals and delta.
  if (!form) return;
  if (form.dataset.motherEdited !== 'true') {
    var parentQty = parseFloat(form.dataset.parentQty || '0');
    var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
    var weightDecimals = parseInt(form.dataset.weightDecimals || '2', 10);
    var motherQty = Math.max(0, parentQty - _sumInputs(form, '[name="child_qty"]'));
    _setMotherQty(form, motherQty, decimals);
    var sellByType = form.dataset.sellByType;
    // The sell-by dimension IS the qty, so its mother display mirrors the derived mother qty.
    if (sellByType === 'weight') {
      var mwSell = form.querySelector('.mother-weight-display');
      if (mwSell) mwSell.textContent = motherQty.toFixed(weightDecimals);
    } else if (sellByType === 'pieces') {
      var mpSell = form.querySelector('.mother-pieces-display');
      if (mpSell) mpSell.textContent = String(Math.round(motherQty));
    }
    // Any dimension that is NOT the sell-by unit is independent: derive its mother display from
    // that dimension's own parent total minus the sum of its children so it stays live too.
    if (sellByType !== 'weight') {
      var parentWeight = parseFloat(form.dataset.parentWeight);
      if (!isNaN(parentWeight)) {
        var mwd = form.querySelector('.mother-weight-display');
        if (mwd) mwd.textContent = Math.max(0, parentWeight - _sumInputs(form, '[name="child_weight"]')).toFixed(weightDecimals);
      }
    }
    if (sellByType !== 'pieces') {
      var parentPieces = parseFloat(form.dataset.parentPieces);
      if (!isNaN(parentPieces)) {
        var mpd = form.querySelector('.mother-pieces-display');
        if (mpd) mpd.textContent = String(Math.round(Math.max(0, parentPieces - _sumInputs(form, '[name="child_pieces"]'))));
      }
    }
  }
  _updateSplitTotals(form);
}
function splitAddChild(btn) {
  // Clone the server-rendered <template> child row, append it, recompute, focus its qty.
  var form = btn.closest('form');
  if (!form) return;
  var tpl = form.querySelector('.split-child-template');
  var tbody = form.querySelector('.split-preview-table tbody');
  if (!tpl || !tbody) return;
  // The template wraps its child row in <table><tbody> so the server HTML parser keeps the
  // <tr> intact; extract that <tr> and append it, or it nests a whole <table> in one column.
  var frag = tpl.content.cloneNode(true);
  var tr = frag.querySelector('tr');
  if (!tr) return;
  tbody.appendChild(tr);
  splitRecalc(form);
  var rows = tbody.querySelectorAll('tr');
  var last = rows[rows.length - 1];
  var inp = last ? last.querySelector('[name="child_qty"]') : null;
  if (inp) inp.focus();
}
function splitRecalcMotherWeight(input) {
  // oninput on an editable child weight: recompute via the shared helper. Never write
  // input.value here, which would destroy a decimal being typed.
  var form = input.closest('form');
  if (!form) return;
  splitRecalc(form);
}
function splitClampWeight(input) {
  // onblur clamp for an editable child weight; a single child is upper-bounded at the parent
  // weight, over-split across N children is left for the server to reject (GDR 2e).
  var form = input.closest('form');
  if (!form) return;
  var parentWeight = parseFloat(form.dataset.parentWeight || '0');
  var decimals = parseInt(form.dataset.weightDecimals || '2', 10);
  var childVal = Math.min(Math.max(0, parseFloat(input.value) || 0), parentWeight);
  input.value = childVal.toFixed(decimals);
  splitRecalc(form);
}
function splitRecalcMotherPieces(input) {
  // oninput on an editable child pieces count: recompute via the shared helper. Never write
  // input.value here.
  var form = input.closest('form');
  if (!form) return;
  splitRecalc(form);
}
function splitClampPieces(input) {
  // onblur clamp for an editable child pieces count.
  var form = input.closest('form');
  if (!form) return;
  var parentPieces = parseFloat(form.dataset.parentPieces || '0');
  var motherEdited = form.dataset.motherEdited === 'true';
  var childP = motherEdited
    ? Math.max(0, Math.round(parseFloat(input.value) || 0))
    : Math.min(Math.max(0, Math.round(parseFloat(input.value) || 0)), Math.max(0, parentPieces - 1));
  input.value = String(childP);
  splitRecalc(form);
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
function bulkSplitMotherQtyChanged(input) {
  // Mother qty manually edited: mark form so child-change handlers never overwrite it.
  var form = input.closest('form');
  if (!form) return;
  form.dataset.motherEdited = 'true';
  var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
  var epsilon = decimals > 0 ? Math.pow(10, -decimals) : 1;
  var motherQty = Math.max(epsilon, parseFloat(input.value) || epsilon);
  input.value = motherQty.toFixed(decimals);
  // For weight-unit items qty IS weight: keep mother-weight-display in sync.
  if (form.dataset.sellByType === 'weight') {
    var weightDecimals = parseInt(form.dataset.weightDecimals || '2', 10);
    var mw = form.querySelector('.mother-weight-display');
    if (mw) mw.textContent = motherQty.toFixed(weightDecimals);
  }
  // For piece-unit items qty IS pieces: keep mother-pieces-display in sync.
  if (form.dataset.sellByType === 'pieces') {
    var mp = form.querySelector('.mother-pieces-display');
    if (mp) mp.textContent = String(Math.round(motherQty));
  }
  _updateSplitTotals(form);
}
function bulkSplitChildQtyChanged(input) {
  var form = input.closest('form');
  if (!form) return;
  var parentQty = parseFloat(form.dataset.parentQty || '0');
  var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
  var epsilon = decimals > 0 ? Math.pow(10, -decimals) : 1;
  var motherEdited = form.dataset.motherEdited === 'true';
  // Clamp a single child at the parent budget unless the mother is user-owned; the mother is
  // derived from the sum of ALL children in splitRecalc.
  var childQty = motherEdited
    ? Math.max(0, parseFloat(input.value) || 0)
    : Math.min(Math.max(0, parseFloat(input.value) || 0), parentQty - epsilon);
  input.value = childQty.toFixed(decimals);
  var row = input.closest('tr');
  // For piece-unit items qty and pieces are the same: mirror this row's pieces cell.
  if (form.dataset.sellByType === 'pieces' && row) {
    var piecesInput = row.querySelector('[name="child_pieces"]');
    if (piecesInput) { piecesInput.value = Math.round(childQty); }
    var childPiecesDisplay = row.querySelector('.child-pieces-display');
    if (childPiecesDisplay) { childPiecesDisplay.textContent = String(Math.round(childQty)); }
  }
  // For weight-unit items qty IS the weight: mirror this row's weight cell.
  if (form.dataset.sellByType === 'weight' && row) {
    var weightDecimals = parseInt(form.dataset.weightDecimals || '2', 10);
    var weightInput = row.querySelector('[name="child_weight"]');
    if (weightInput) { weightInput.value = childQty.toFixed(weightDecimals); }
    var childWeightDisplay = row.querySelector('.child-weight-display');
    if (childWeightDisplay) { childWeightDisplay.textContent = childQty.toFixed(weightDecimals); }
  }
  splitRecalc(form);
}
function bulkSplitChildPiecesChanged(input) {
  var form = input.closest('form');
  if (!form || form.dataset.sellBy !== 'piece') return;
  var decimals = parseInt(form.dataset.unitDecimals || '0', 10);
  var motherEdited = form.dataset.motherEdited === 'true';
  var parentPieces = parseFloat(form.dataset.parentPieces || form.dataset.parentQty || '0');
  var childPieces = motherEdited
    ? Math.max(0, Math.round(parseFloat(input.value) || 0))
    : Math.min(Math.max(0, Math.round(parseFloat(input.value) || 0)), Math.round(parentPieces) - 1);
  input.value = String(childPieces);
  var row = input.closest('tr');
  var qtyInput = row ? row.querySelector('[name="child_qty"]') : null;
  if (qtyInput) { qtyInput.value = childPieces.toFixed(decimals); }
  splitRecalc(form);
}
function bulkSplitChildWeightChanged(input) {
  var form = input.closest('form');
  if (!form) return;
  if (form.dataset.sellByType !== 'weight') return;
  var parentWeight = parseFloat(form.dataset.parentWeight || '0');
  var weightDecimals = parseInt(form.dataset.weightDecimals || '2', 10);
  var decimals = parseInt(form.dataset.unitDecimals || weightDecimals, 10);
  var motherEdited = form.dataset.motherEdited === 'true';
  var childWeight = motherEdited
    ? Math.max(0, parseFloat(input.value) || 0)
    : Math.min(Math.max(0, parseFloat(input.value) || 0), parentWeight);
  input.value = childWeight.toFixed(weightDecimals);
  var row = input.closest('tr');
  var qtyInput = row ? row.querySelector('[name="child_qty"]') : null;
  if (qtyInput) { qtyInput.value = childWeight.toFixed(decimals); }
  splitRecalc(form);
}
function bulkSplitSkuChanged(input) {}
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
  // Disable the confirm button for the duration of the request so a double-click cannot
  // double-post; htmx:afterRequest re-enables it (e.g. when a validation error swaps back).
  var confirmBtn = formEl.querySelector('.sp-confirm-btn');
  if (confirmBtn && !confirmBtn.disabled) {
    confirmBtn.disabled = true;
    formEl.addEventListener('htmx:afterRequest', function reenable() {
      confirmBtn.disabled = false;
      formEl.removeEventListener('htmx:afterRequest', reenable);
    });
  }
  return true;
}
"""

_BULK_TRANSFORM_JS = """
function transformCostManualEdit(input) {
  input.closest('form').dataset.costOverridden = 'true';
}
function transformUnitChanged(select) {
  var form = select.closest('form');
  var unit = select.value;
  var weightTd = form.querySelector('.tr-weight-td');
  var piecesTd = form.querySelector('.tr-pieces-td');
  var weightInput = weightTd ? weightTd.querySelector('input') : null;
  var piecesInput = piecesTd ? piecesTd.querySelector('input') : null;
  var weightUnits = (form.dataset.weightUnits || '').split(',').filter(Boolean);
  var qtyInput = form.querySelector('[name="child_qty"]');
  var qtyVal = qtyInput ? qtyInput.value : '0';
  if (weightUnits.indexOf(unit) !== -1) {
    if (weightInput) { weightInput.value = qtyVal; weightInput.disabled = true; weightInput.classList.add('tr-locked'); }
    if (piecesInput) { piecesInput.disabled = false; piecesInput.classList.remove('tr-locked'); }
  } else if (unit === 'piece') {
    if (piecesInput) { piecesInput.value = Math.round(parseFloat(qtyVal || '0')); piecesInput.disabled = true; piecesInput.classList.add('tr-locked'); }
    if (weightInput) { weightInput.disabled = false; weightInput.classList.remove('tr-locked'); }
  } else {
    if (weightInput) { weightInput.disabled = false; weightInput.classList.remove('tr-locked'); }
    if (piecesInput) { piecesInput.disabled = false; piecesInput.classList.remove('tr-locked'); }
  }
  _updateSplitDelta(form);
}
function transformQtyChanged(input) {
  var form = input.closest('form');
  var unitSelect = form.querySelector('[name="child_sell_by"]');
  if (unitSelect) transformUnitChanged(unitSelect);
}
function transformPreviewInit(formId) {
  var form = document.getElementById(formId);
  if (!form) return;
  var unitSelect = form.querySelector('[name="child_sell_by"]');
  if (unitSelect) transformUnitChanged(unitSelect);
}
"""




def _opt_float_field(form, key: str):
    """Parse an optional float form field: None on empty or non-numeric, so an absent or
    malformed override is omitted from the payload rather than forwarded as 0."""
    raw = str(form.get(key, "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _split_table_form(preview: dict, *, action: str, target: str, form_id: str,
                      allow_add_child: bool, child_sku_editable: bool,
                      entity_id: str | None = None) -> FT:
    """Shared split-table renderer used by both the bulk-split preview and the item-detail
    split card. Reads all unit/format context from the server preview dict and emits the
    Mother row (editable qty), one or more Child rows, a totals footer, and the live delta
    net-gain/loss badge next to Confirm. Both surfaces mark BOTH the mother's post-split
    element and each child's outgoing element with data-delta-weight, so the badge reads 0
    for a balanced carve and non-zero only when the mother is edited independently or the
    children exceed it. allow_add_child renders a + control plus a hidden <template> child
    row (cloned client-side for N children), each added row carrying a remove control;
    child_sku_editable renders the child SKU as an editable input (bulk) or a static cell
    keeping the parent SKU (item-detail)."""
    from fasthtml.common import Template

    sell_by_label = preview.get("sell_by_label", preview.get("sell_by", ""))
    decimals = preview.get("unit_decimals", 0)
    weight_decimals = preview.get("weight_decimals", 2)
    fmt = f"{{:.{decimals}f}}"
    wfmt = f"{{:.{weight_decimals}f}}"
    sell_by_type = preview.get("sell_by_type", "other")
    weight_unit_label = preview.get("weight_unit_label", "")

    def _unit_display_name(label: str) -> str:
        """Strip abbreviation in parens from a unit label: 'Carat (ct)' -> 'Carat'."""
        return re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()

    sell_by_display = _unit_display_name(sell_by_label)
    weight_display = _unit_display_name(weight_unit_label)

    # QTY is always shown. Weight/pieces columns follow one symmetric rule:
    #   - weight-type sell_by: qty IS weight, so the weight column is a read-only mirror of
    #     QTY, only worth showing alongside an editable pieces column.
    #   - pieces-type sell_by: qty IS pieces, the pieces mirror shows only alongside an
    #     editable weight column.
    #   - other sell_by: weight/pieces show when the item has them (editable).
    has_weight = preview.get("has_weight", False)
    has_pieces = preview.get("has_pieces", False)
    parent_pieces_val = preview.get("parent_pieces")
    if sell_by_type == "weight":
        show_weight = has_pieces
        show_pieces = has_pieces
    elif sell_by_type == "pieces":
        show_weight = has_weight
        show_pieces = has_weight
        if parent_pieces_val is None:
            parent_pieces_val = int(round(float(preview["parent_qty"])))
    else:
        show_weight = has_weight
        show_pieces = has_pieces

    weight_col_header = f"Weight ({weight_display})" if weight_display else "Weight"
    # Numeric/currency headers right-align over their figures (HTML/CSS rule 4a); SKU stays text.
    headers = [Th(""), Th("SKU", cls="sp-th"), Th(f"QTY {sell_by_display}", cls="sp-th sp-th--num")]
    if show_weight:
        headers.append(Th(weight_col_header, cls="sp-th sp-th--num"))
    if show_pieces:
        headers.append(Th("Pieces", cls="sp-th sp-th--num"))
    if allow_add_child:
        headers.append(Th("", cls="sp-th-remove"))

    # ESC-to-blur on every editable input the helper renders (GDR 2j); mirrors the
    # repeated-row convention in the journal-entry table (ui/routes/accounting.py:442).
    esc = "if(event.key==='Escape'){this.blur();event.preventDefault();}"

    def _editable_td(name: str, val: str, oninput: str | None = None, onblur: str | None = None, max: str | None = None, min: str | None = None, num: bool = False, dw: bool = False, width_cls: str | None = None) -> FT:
        base_cls = "form-input form-input--xs sp-input" + (f" {width_cls}" if width_cls else "")
        kwargs = dict(type="number", name=name, value=val, step="any", cls=base_cls, onkeydown=esc)
        if oninput:
            kwargs["oninput"] = oninput
        if onblur:
            kwargs["onblur"] = onblur
        if max is not None:
            kwargs["max"] = max
        if min is not None:
            kwargs["min"] = min
        if dw:
            kwargs["data_delta_weight"] = "1"
        return Td(Input(**kwargs), cls="sp-td sp-td--num" if num else "sp-td")

    _child_weight_oninput = "splitRecalcMotherWeight(this)"
    _child_weight_onblur = "splitClampWeight(this)"
    _child_pieces_oninput = "splitRecalcMotherPieces(this)"
    _child_pieces_onblur = "splitClampPieces(this)"

    def _parcel_row(label, sku_cell: FT, qty_cell: FT, weight_val, pieces_val,
                    weight_name: str | None, pieces_name: str | None, is_child: bool = False,
                    trailing: FT | None = None) -> FT:
        cells = [Td(label, cls="sp-row-label"), sku_cell, qty_cell]
        if show_weight:
            w = wfmt.format(weight_val) if weight_val is not None else wfmt.format(0)
            # Child weight is editable only when sell_by is NOT a weight unit.
            # When sell_by IS weight, qty = weight so child weight is derived from QTY (static).
            child_weight_editable = is_child and weight_name and sell_by_type != "weight"
            if child_weight_editable:
                # This editable weight IS the outgoing weight for the delta sum (marker).
                cells.append(_editable_td(weight_name, w, oninput=_child_weight_oninput, onblur=_child_weight_onblur, num=True, dw=True, width_cls="sp-input--weight"))
            elif is_child:
                cells.append(Td(Span(w, cls="child-weight-display sp-static-val"), cls="sp-td sp-td--num"))
            else:
                # Mother: static display (grey italic), updated by JS. Also carries the delta
                # marker (data-delta-weight) when sell_by is NOT weight-type, so the badge sums
                # mother_post + children (net gain/loss), not children alone. When sell_by IS
                # weight, this span may not render (no weight column for a pure weight-sold
                # parcel), so the marker lives on .mother-qty-input instead (mother_row below).
                _mother_span_dw = {"data_delta_weight": "1"} if sell_by_type != "weight" else {}
                cells.append(Td(Span(w, cls="mother-weight-display sp-static-val", **_mother_span_dw), cls="sp-td sp-td--num"))
        if show_pieces:
            p = str(int(pieces_val)) if pieces_val is not None else "0"
            # Child pieces is editable only when sell_by is NOT a pieces unit.
            child_pieces_editable = is_child and pieces_name and sell_by_type != "pieces"
            if child_pieces_editable:
                pieces_max = str(int(parent_pieces_val or 1) - 1)
                cells.append(_editable_td(pieces_name, p, oninput=_child_pieces_oninput, onblur=_child_pieces_onblur, max=pieces_max, num=True, width_cls="sp-input--pieces"))
            elif is_child:
                cells.append(Td(Span(p, cls="child-pieces-display sp-static-val"), cls="sp-td sp-td--num"))
            else:
                cells.append(Td(Span(p, cls="mother-pieces-display sp-static-val"), cls="sp-td sp-td--num"))
        # Trailing remove column, present on every row when add-child is enabled so the mother,
        # first child, and each cloned child keep an equal column count.
        if allow_add_child:
            cells.append(trailing or Td("", cls="sp-td sp-remove-td"))
        return Tr(*cells)

    # When sell_by is weight, qty IS the mother's post-split weight (no separate weight
    # column may render), so this input carries the delta marker instead of the span above.
    _mother_qty_dw = {"data_delta_weight": "1"} if sell_by_type == "weight" else {}
    mother_row = _parcel_row(
        "Mother",
        _sp_static_td(preview["parent_sku"]),
        Td(Input(type="number", name="mother_qty", value=fmt.format(preview["parent_qty"]),
                 step=str(10 ** -decimals if decimals > 0 else 1), min="0",
                 cls="form-input form-input--xs sp-input sp-input--qty mother-qty-input",
                 onchange="bulkSplitMotherQtyChanged(this)", onkeydown=esc, **_mother_qty_dw), cls="sp-td sp-td--num"),
        preview.get("parent_weight"),
        parent_pieces_val,
        weight_name=None,
        pieces_name=None,
        is_child=False,
    )

    # When sell_by is weight, child_qty IS the outgoing weight (no separate weight field),
    # so it carries the delta marker; oninput keeps the badge live as it is typed.
    _child_qty_dw = {"data_delta_weight": "1", "oninput": "_updateSplitDelta(this.form)"} if sell_by_type == "weight" else {}

    def _child_row(removable: bool) -> FT:
        if child_sku_editable:
            sku_cell = Td(Input(type="text", name="child_sku", value=preview["child_sku"],
                                cls="form-input sp-sku-input", onkeydown=esc,
                                oninput="bulkSplitSkuChanged(this)"), cls="sp-td")
        else:
            sku_cell = _sp_static_td(preview.get("child_sku") or "--")
        qty_cell = Td(Input(type="number", name="child_qty", value="0",
                            step=str(10 ** -decimals if decimals > 0 else 1), min="0",
                            max=fmt.format(preview["parent_qty"] - (10 ** -decimals if decimals > 0 else 1)),
                            cls="form-input form-input--xs sp-input sp-input--qty", onkeydown=esc,
                            onchange="bulkSplitChildQtyChanged(this)", **_child_qty_dw), cls="sp-td sp-td--num")
        # Removable children (cloned from the template) carry a trailing remove control; the
        # single permanent child carries the gutter "+" in its leading label cell. Both only
        # when add-child is enabled (item-detail card); bulk passes allow_add_child=False.
        if removable:
            label = "Child"
            trailing = Td(
                Button("✕", type="button", cls="btn btn--ghost btn--xs split-remove-child-btn",
                       title="Remove child",
                       onclick="var f=this.closest('form');var tr=this.closest('tr');if(tr)tr.remove();splitRecalc(f);"),
                cls="sp-td sp-remove-td",
            )
        elif allow_add_child:
            label = Span(
                Button("+", type="button", cls="btn btn--ghost btn--xs split-add-child-btn",
                       title="Add another child", onclick="splitAddChild(this)"),
                " Child",
            )
            trailing = None
        else:
            label = "Child"
            trailing = None
        return _parcel_row(
            label,
            sku_cell,
            qty_cell,
            None,
            0,
            weight_name="child_weight" if (show_weight and sell_by_type != "weight") else None,
            pieces_name="child_pieces" if (show_pieces and sell_by_type != "pieces") else None,
            is_child=True,
            trailing=trailing,
        )

    child_row = _child_row(removable=False)

    form_data: dict = {
        "data_weight_decimals": str(weight_decimals),
        "data_unit_decimals": str(decimals),
        "data_parent_qty": str(preview["parent_qty"]),
        "data_sell_by": preview["sell_by"],
        "data_sell_by_type": sell_by_type,
        "data_weight_units": ",".join(preview.get("weight_unit_names", [])),
    }
    if show_weight:
        form_data["data_parent_weight"] = str(preview["parent_weight"])
    if show_pieces:
        form_data["data_parent_pieces"] = str(int(parent_pieces_val))
    if sell_by_type == "weight":
        # Weight-sold: quantity IS the weight, so the delta reads parent weight from
        # data-parent-qty (data-parent-weight is absent for these parcels).
        form_data["data_sell_by_weight"] = "1"

    # Totals footer: mother qty (initial) + child qty (0) for QTY column; weight/pieces use
    # parent totals since the children start at 0.
    tfoot_cells: list = [Td("Total", cls="sp-row-label sp-total-label"), Td("")]
    tfoot_cells.append(Td(
        fmt.format(preview["parent_qty"]),
        cls="sp-td sp-total-val sp-total-qty",
    ))
    if show_weight:
        tfoot_cells.append(Td(
            wfmt.format(preview.get("parent_weight") or 0),
            cls="sp-td sp-total-val sp-total-weight",
        ))
    if show_pieces:
        tfoot_cells.append(Td(
            str(int(parent_pieces_val or 0)),
            cls="sp-td sp-total-val sp-total-pieces",
        ))
    if allow_add_child:
        tfoot_cells.append(Td("", cls="sp-total-val"))

    # Live delta badge next to Confirm: net gain/loss of the split, parcel weight minus the
    # sum of the elements marked data-delta-weight (the mother's post-split weight/qty plus
    # every child's outgoing weight/qty). Both surfaces mark both, so the badge reads 0 for a
    # balanced carve and non-zero only when the mother is edited independently or the children
    # exceed it. The JS reads the parcel weight from data-parent-weight (or data-parent-qty
    # when the parcel is weight-sold). Units with no weight carry no delta.
    delta_unit = f" {weight_display}" if weight_display else ""
    _show_delta_badge = (sell_by_type == "weight") or show_weight

    add_child_controls = []
    if allow_add_child:
        # The template wraps the child row in Table(Tbody(...)) so the server HTML parser keeps
        # the <tr> intact inside <template>; splitAddChild extracts and appends that <tr>. The
        # gutter "+" that adds a child lives in the first child's label cell (see _child_row).
        add_child_controls = [
            Template(Table(Tbody(_child_row(removable=True))), cls="split-child-template"),
        ]

    return Form(
        *([Input(type="hidden", name="entity_id", value=entity_id)] if entity_id is not None else []),
        # mother_weight hidden: tracks the mother-weight-display span value so JS can submit
        # it; _updateSplitTotals keeps it in sync after every change.
        *(
            [Input(type="hidden", name="mother_weight",
                   value=wfmt.format(preview.get("parent_weight") or 0))]
            if show_weight else []
        ),
        Table(
            Thead(Tr(*headers)),
            Tbody(mother_row, child_row),
            Tfoot(Tr(*tfoot_cells, cls="sp-totals-row")),
            cls="split-preview-table",
        ),
        *add_child_controls,
        Div(
            Button("Confirm", type="submit", cls="btn btn--primary btn--sm sp-confirm-btn"),
            *(
                [Span(
                    Span("Δ ", cls="sp-delta-label"),
                    Span("", cls="sp-delta-val"),
                    Span(delta_unit, cls="sp-delta-unit"),
                    cls="sp-delta sp-delta-badge",
                    title=t("inv.split_delta_tooltip"),
                )]
                if _show_delta_badge else []
            ),
            cls="sp-confirm-row",
        ),
        *(
            [Script(f"_updateSplitDelta(document.getElementById('{form_id}'));")]
            if _show_delta_badge else []
        ),
        hx_post=action,
        hx_target=target,
        hx_swap="outerHTML",
        onsubmit="bulkSplitSubmit(this)",
        id=form_id,
        **form_data,
    )


def _derive_import_qty(row: dict, sell_by: str, unit_map: dict[str, dict]) -> float:
    """Derive the stock quantity from a CSV row.

    Priority:
    1. If the row contains an explicit ``quantity`` or ``qty`` column, trust it
       unconditionally - the user knows what they're doing.
    2. Otherwise fall back to the semantic field for the unit type:
       - pieces-type (e.g. ``piece``) → ``pieces`` column
       - weight-type (e.g. ``carat``, ``gram``) → ``weight`` or ``weight_ct`` column
       - other (service, volume, length, unknown) → 0.0

    Returns a float; never raises.
    """
    def _to_float(val) -> float | None:
        s = str(val).strip() if val is not None else ""
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    explicit = _to_float(row.get("quantity")) if "quantity" in row else _to_float(row.get("qty"))
    if explicit is not None:
        return explicit
    if is_pieces_unit(sell_by, unit_map):
        return _to_float(row.get("pieces")) or 0.0
    if is_weight_unit(sell_by, unit_map):
        return _to_float(row.get("weight")) or _to_float(row.get("weight_ct")) or 0.0
    return 0.0


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
        "location_id": q.get("location_id", ""),  # column-filter funnel (csv of location ids)
        "source": q.get("source", ""),  # connector source filter (e.g. ?source=shopify)
        "filter": q.get("filter", ""),  # semantic filter (e.g. ?filter=low_stock)
        # Contact-scoped holdings: goods out on memo to a customer, or held on
        # consignment from a supplier. Backs the contact page's On Memo / Consignment cards.
        "on_memo_to": q.get("on_memo_to", ""),
        "consigned_from": q.get("consigned_from", ""),
        # Category-attribute column funnels: every ?attr.<key>=csv pair, keyed by <key>.
        "attr_filters": {k[len("attr."):]: v for k, v in q.items() if k.startswith("attr.") and v},
        "sort": q.get("sort", ""),
        "dir": q.get("dir", "desc"),
        "per_page": max(1, per_page),
        "cols": cols,
    }


def _base_state(p: dict, include_page: bool = False) -> dict:
    state = {}
    for k in ("q", "skus", "status", "category", "inventory_type", "location_id", "source", "filter",
              "on_memo_to", "consigned_from", "sort", "dir"):
        if p.get(k):
            state[k] = p[k]
    for akey, aval in (p.get("attr_filters") or {}).items():
        if aval:
            state[f"attr.{akey}"] = aval
    if p.get("per_page") and p["per_page"] != _DEFAULT_PER_PAGE:
        state["per_page"] = str(p["per_page"])
    if p.get("cols"):
        state["cols"] = ",".join(p["cols"])
    if include_page and p.get("page", 1) > 1:
        state["page"] = str(p["page"])
    return state


def _holdings_scope_banner(p: dict, holdings_total: float | None, currency: str | None) -> FT:
    """Banner for the contact-scoped holdings views, with a way back to all inventory.

    Under a scope the Value column is not the catalog price: for memo it is the price the
    customer was quoted, for consignment it is what the goods cost us. Say so, so the
    figure is never read as a list price.
    """
    from ui.components.table import fmt_money

    on_memo, consigned = p.get("on_memo_to", ""), p.get("consigned_from", "")
    if not (on_memo or consigned):
        return ""
    if on_memo:
        label, basis = "Out on memo to this customer", "Value shown is the price quoted to the customer."
    else:
        label, basis = "Held on consignment from this supplier", "Value shown is cost."
    total_el = (Span(fmt_money(holdings_total, currency), cls="holdings-scope-total")
                if holdings_total is not None else "")
    return Div(
        Div(Span(label, cls="holdings-scope-label"), total_el, cls="holdings-scope-heading"),
        Div(basis, cls="holdings-scope-basis"),
        A("Clear filter", href="/inventory", cls="btn btn--xs btn--secondary"),
        cls="holdings-scope-banner",
    )


def _inventory_column_filters(eff_schema: list[dict], global_schema: list[dict], locations: list[dict],
                              attribute_facets: dict, p: dict) -> dict | None:
    """Server-backed column funnels for the inventory table: Location plus every category-specific
    attribute that has distinct values. Each funnel reloads with ?param=csv (full-dataset filter)."""
    global_keys = {f["key"] for f in global_schema}
    attr_filters = p.get("attr_filters") or {}
    filters: dict = {}
    loc_opts = [(loc.get("id") or loc.get("location_id"), loc.get("name"))
                for loc in locations if (loc.get("id") or loc.get("location_id"))]
    if loc_opts:
        filters["location_name"] = {
            "param": "location_id", "options": loc_opts,
            "selected": [s for s in (p.get("location_id") or "").split(",") if s],
        }
    for f in eff_schema:
        key = f["key"]
        if key in global_keys:
            continue  # base fields keep their existing filters (status cards, category/type tabs)
        vals = attribute_facets.get(key)
        if not vals:
            continue
        filters[key] = {
            "param": f"attr.{key}", "options": [(v, v) for v in vals],
            "selected": [s for s in (attr_filters.get(key) or "").split(",") if s],
        }
    return filters or None


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
    role: str = "owner",
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
        if p.get("location_id"):
            params["location_id"] = p["location_id"]
        if p.get("source"):
            params["source"] = p["source"]
        if p.get("filter"):
            params["filter"] = p["filter"]
        if p.get("on_memo_to"):
            params["on_memo_to"] = p["on_memo_to"]
        if p.get("consigned_from"):
            params["consigned_from"] = p["consigned_from"]
        for akey, aval in (p.get("attr_filters") or {}).items():
            if aval:
                params[f"attr.{akey}"] = aval
        if p["sort"]:
            params["sort"] = p["sort"]
            params["dir"] = p["dir"]
        items_resp, units_resp = await asyncio.gather(
            api.list_items(token, params),
            api.get_units(token),
        )
        items = items_resp.get("items", [])
        # Pagination must reflect the SAME filters as the rows (incl. the search `q`), not the
        # valuation's unfiltered item_count — otherwise a search shows pages that don't exist
        # and clicking them lands on a blank page.
        list_total = items_resp.get("total", len(items))
        # Present only under a contact holdings scope: the value of the whole scoped set.
        holdings_total = items_resp.get("value_total")
        attribute_facets = items_resp.get("attribute_facets", {})
        unit_names: list[str] = [u["name"] for u in units_resp if u.get("name")]
        units_map: dict[str, dict] = {u["name"]: u for u in units_resp if u.get("name")}
        try:
            category_label_map: dict = await api.get_category_display_names(token)
        except Exception:
            category_label_map = {}
    except APIError:
        valuation, items, unit_names, units_map, category_label_map, attribute_facets = {}, [], [], {}, {}, {}
        list_total = 0
        holdings_total = None

    currency = company.get("currency")
    vertical = company.get("settings", {}).get("vertical", "") if isinstance(company.get("settings"), dict) else ""

    category_counts = valuation.get("category_counts", {})
    total_scoped = valuation.get("total_scoped_count", sum(category_counts.values()))
    count_by_status = valuation.get("count_by_status", {})
    active_cat = p.get("category", "")
    eff_schema = _effective_schema(schema, cat_schemas, active_cat)
    # Patch location_name in eff_schema to be editable with resolved options
    loc_names = [loc.get("name", "") for loc in locations if loc.get("name")]
    eff_schema = [
        {**f, "type": "select", "options": loc_names, "editable": True} if f.get("key") == "location_name"
        else f
        for f in eff_schema
    ]
    eff_schema = _apply_amount_edit_permission(eff_schema, role, company.get("settings") or {})
    # Under a contact holdings scope the meaningful per-row value is the scope value the
    # total is summed from (quoted memo price / consignment cost), not the catalog price.
    # Surface it as a read-only column so the rows visibly add up to the banner figure.
    scope_value_label = ""
    if p.get("on_memo_to"):
        scope_value_label = "Quoted"
    elif p.get("consigned_from"):
        scope_value_label = "Cost"
    if scope_value_label and any(i.get("holding_value") is not None for i in items):
        eff_schema = eff_schema + [{
            "key": "holding_value", "label": scope_value_label, "type": "money",
            "editable": False, "required": False, "options": [], "visible_to_roles": [],
            "position": 99, "show_in_table": True,
        }]
    else:
        scope_value_label = ""

    visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, p.get("cols") or [])
    if scope_value_label and "holding_value" not in visible_cols:
        # Saved column prefs predate this column, so make sure the scope value is shown.
        visible_cols = visible_cols + ["holding_value"]
    # Inject resolved cols into URL state so sort links and pagination always carry
    # the exact column set being rendered, even when it came from col_prefs not URL params.
    p_with_cols = {**p, "cols": visible_cols}
    extra_params = urlencode(_base_state(p_with_cols))
    total_items = valuation.get("item_count", 0)

    return Div(
        _holdings_scope_banner(p, holdings_total, currency),
        _category_tabs(category_counts, p, total_scoped=total_scoped, label_map=category_label_map),
        _inventory_type_tabs(p),
        _valuation_bar(valuation, currency, lang, status=p.get("status", "")),
        _inventory_status_cards(count_by_status, p.get("status", ""), vertical, p, lang=lang),
        _bulk_toolbar(locations, p, total_items, settings=company.get("settings") or {}, role=role),
        Div(
            _column_manager(eff_schema, p, active_cat, visible_cols, keep_open=col_manager_open),
            cls="column-manager-row",
        ),
        data_table(
            _label_price_cols(eff_schema),
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
            cell_renderers=_inventory_cell_renderers(eff_schema, unit_names, units_map, category_label_map, currency=currency),
            hidden_fields=set(_PAIRED_TABLE.values()),
            column_filters=_inventory_column_filters(eff_schema, schema, locations, attribute_facets, p),
        ) if items else _inventory_empty_state(p),
        pagination(p["page"], list_total, p["per_page"], "/inventory", extra_params),
        Script(SERVER_FILTER_JS),
        Div(id="modal-container"),
        id="inventory-content",
    )


async def _import_export_allowed(request: Request, token: str) -> bool:
    """True when the caller's role holds import_export_data over live company settings."""
    try:
        settings = (await api.get_company(token)).get("settings") or {}
    except APIError:
        settings = {}
    return role_has_permission(settings, _get_role(request), "import_export_data")


def _duplicate_payload(source: dict, new_sku: str) -> dict:
    """Build a create payload from an existing item, carrying every field except
    id, status, location_name, created_at, updated_at (status is reset by the create
    path). Core columns and any *_price stay top-level; everything else goes into
    attributes. Shared by the single-item and bulk duplicate paths."""
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
    return payload


async def _gen_copy_sku(token: str, orig: str, reserved: set[str] | None = None) -> str:
    """Generate a unique copy SKU for a source SKU: {orig}-copy, -copy2, -copy3 ...
    skipping any SKU that already exists (scanned via api.list_items) and any already
    in `reserved` (copies generated earlier in the same batch). When `reserved` is
    given, the chosen SKU is added to it so the next call in the batch skips it."""
    candidate = f"{orig}-copy"
    try:
        resp = await api.list_items(token, {"q": orig, "limit": 100, "status": "all"})
        existing_skus = {str(it.get("sku", "")) for it in (resp.get("items", []) if isinstance(resp, dict) else resp)}
    except Exception:
        existing_skus = set()
    taken = existing_skus | (reserved or set())
    n = 2
    while candidate in taken:
        candidate = f"{orig}-copy{n}"
        n += 1
    if reserved is not None:
        reserved.add(candidate)
    return candidate


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

        _settings = company.get("settings") or {}
        _role = _get_role(request)
        if not role_has_permission(_settings, _role, "view_inventory"):
            return RedirectResponse("/dashboard", status_code=302)

        currency = company.get("currency")
        lang = get_lang(request)
        vertical = company.get("settings", {}).get("vertical", "") if isinstance(company.get("settings"), dict) else ""
        active_cat = p.get("category", "")
        eff_schema = _effective_schema(schema, cat_schemas, active_cat)
        visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, p.get("cols") or [])

        content = await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=lang, role=_role)

        # Search must carry the active filters (status/category/type/location), so searching inside
        # e.g. Sold inventory stays scoped to sold instead of falling back to the default active set.
        _search_filters = {k: v for k, v in (
            ("status", p.get("status")), ("category", p.get("category")),
            ("inventory_type", p.get("inventory_type")), ("location_id", p.get("location_id")),
        ) if v}
        _search_url = "/inventory/content" + (f"?{urlencode(_search_filters)}" if _search_filters else "")

        _can_import_export = role_has_permission(_settings, _role, "import_export_data")
        _can_edit_inventory = role_has_permission(_settings, _role, "edit_inventory")
        return await base_shell(
            page_header(
                t("page.inventory", lang),
                search_bar(
                    placeholder=t("msg.search_inventory_placeholder", lang),
                    target="#inventory-content",
                    url=_search_url,
                ),
                A(t("btn.import", lang), href="/inventory/import", cls="btn btn--secondary") if _can_import_export else "",
                Button(t("btn.add_item", lang), hx_post="/inventory/create-blank", hx_swap="none", cls="btn btn--primary") if _can_edit_inventory else "",
                A(t("btn.export_csv", lang), href="/inventory/export/csv", cls="btn btn--secondary") if _can_import_export else "",
                A(t("inv.customize_fields"), href="/settings/inventory?tab=category-library", cls="btn btn--ghost btn--sm") if _can_import_export else "",
            ),
            content,
            Script(_SPLIT_DELTA_JS),
            Script(_BULK_SPLIT_JS),
            Script(_BULK_TRANSFORM_JS),
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
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=get_lang(request), role=_get_role(request))

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
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, col_manager_open=True, lang=get_lang(request), role=_get_role(request))

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
        return await _inventory_content(token, p, schema, cat_schemas, col_prefs, company, locations, lang=get_lang(request), role=_get_role(request))

    @app.get("/inventory/export/csv")
    async def inventory_export_csv(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
        lang = get_lang(request)
        return await base_shell(
            page_header(
                t("page.import_inventory", lang),
                A(t("btn.back", lang), href="/inventory", cls="btn btn--secondary"),
                A(t("btn.download_template", lang), href="/inventory/import/template", cls="btn btn--secondary"),
            ),
            _import_upload_form(),
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
        try:
            price_lists = await api.get_price_lists(token)
        except Exception:
            price_lists = PRICE_LISTS_FALLBACK
        spec = _build_import_spec(price_lists)
        try:
            cat_schemas = await api.get_all_category_schemas(token)
            cat_attrs = _union_category_attr_keys(cat_schemas)
        except Exception:
            cat_attrs = []
        all_cols = spec.cols + [a for a in cat_attrs if a not in spec.cols]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=all_cols)
        writer.writeheader()
        writer.writerow({c: "" for c in all_cols})
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return await base_shell(
                page_header(t("page.import_inventory", lang)),
                _import_upload_form(error=err),
                title="Import Inventory - Celerp",
                nav_active="inventory",
                lang=lang,
                request=request,
            )

        cols = list(rows[0].keys()) if rows else []
        if not cols:
            return await base_shell(
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
            price_lists = PRICE_LISTS_FALLBACK
        spec = _build_import_spec(price_lists)
        cat_schemas = await api.get_all_category_schemas(token)
        cat_attrs = _union_category_attr_keys(cat_schemas)

        return await base_shell(
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
                col_labels=_import_price_col_labels(price_lists),
                mutex_groups=_import_price_mutex_groups(price_lists),
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
        lang = get_lang(request)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return await base_shell(
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
            price_lists = PRICE_LISTS_FALLBACK
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
            return await base_shell(
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
                    col_labels=_import_price_col_labels(price_lists),
                    mutex_groups=_import_price_mutex_groups(price_lists),
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

        return await base_shell(
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)
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
        if not await _import_export_allowed(request, token):
            return RedirectResponse("/inventory", status_code=302)

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

        # Company currency for unit-price (rate) derivation; fetched once for the whole import.
        try:
            _imp_co = await api.get_company(token)
            _imp_currency = (_imp_co.get("currency") or "").strip() or "USD"
        except Exception:
            _imp_currency = "USD"

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

        # Build unit maps for sell_by normalization and qty derivation.
        # Both are derived from a single get_units call to stay DRY.
        _unit_canonical: dict[str, str] = {}
        _unit_map: dict[str, dict] = {}
        try:
            _units = await api.get_units(token)
            _unit_canonical = {u["name"].lower(): u["name"] for u in _units}
            _unit_map = {u["name"]: u for u in _units}
        except Exception:
            pass

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

            sell_by = (
                _unit_canonical.get(str(row.get("sell_by", "")).strip().lower())
                or str(row.get("sell_by", "")).strip()
                or _cat_sell_by.get(str(row.get("category", "")).strip())
                or ""
            )
            qty = _derive_import_qty(row, sell_by, _unit_map)

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
                if k not in _CORE_ITEM_COLS and not k.endswith("_price") and not k.endswith("_price_total") and v is not None:
                    v_str = str(v).strip()
                    if v_str:
                        attrs[k] = v_str

            data = {
                "sku": sku,
                "name": name,
                "quantity": qty,
                "category": str(row.get("category", "")).strip() or None,
                "weight": _flt("weight") or _flt("weight_ct"),
                "weight_unit": _unit_canonical.get(str(row.get("weight_unit", "")).strip().lower()) or str(row.get("weight_unit", "")).strip() or None,
                "gross_weight": _flt("gross_weight"),
                "gross_weight_unit": _unit_canonical.get(str(row.get("gross_weight_unit", "")).strip().lower()) or str(row.get("gross_weight_unit", "")).strip() or None,
                "pieces": _flt("pieces"),
                "sell_by": sell_by or None,
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
            # Extract price fields dynamically.
            # _price_total cols: back-calculate unit price = total / qty (Option B).
            # Only used when the corresponding _price col is not also present.
            # Requires qty > 0; rows failing this check are hard-errored via records sentinel.
            _price_errors: list[str] = []
            for col_key in row:
                if col_key.endswith("_price_total"):
                    unit_key = col_key[: -len("_total")]  # e.g. cost_price_total → cost_price
                    total_val = _flt(col_key)
                    if total_val is None:
                        continue
                    if _flt(unit_key) is not None:
                        # Unit price already mapped - total is redundant, skip silently
                        continue
                    if unit_key == "cost_price":
                        # cost_total is the primitive; store directly (no back-calculation)
                        data["cost_total"] = total_val
                        continue
                    # Derive the unit price (a rate) at the fewest decimals that reconcile the entered
                    # total to the cent - same helper as the interactive "set from total" edit (DRY).
                    # qty=0 or missing: treat as 1 (total = unit price for a single item).
                    from celerp.services.money import unit_price_from_total, to_stored_float
                    data[unit_key] = to_stored_float(unit_price_from_total(total_val, qty or 1, _imp_currency))
                elif col_key.endswith("_price") and _flt(col_key) is not None:
                    data[col_key] = _flt(col_key)
            if _price_errors:
                records.append({"_import_error": "; ".join(_price_errors), "name": name, "sku": sku})
                continue
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

    # /inventory/new: ?type=component creates a blank component item and opens it
    # (target of the "+ Add new component" picker option); otherwise redirect to the list.
    @app.get("/inventory/new")
    async def inventory_new_redirect(request: Request):
        if request.query_params.get("type") == "component":
            token = _token(request)
            if not token:
                return RedirectResponse("/login", status_code=302)
            try:
                result = await api.create_item(token, {"name": "New Component", "quantity": 0,
                                                       "sell_by": "piece", "inventory_type": "component"})
                item_id = result.get("id", result.get("entity_id", ""))
                return RedirectResponse(f"/inventory/{item_id}", status_code=302)
            except APIError as e:
                logger.warning("Create component item failed: %s", e.detail)
        return RedirectResponse("/inventory", status_code=302)

    @app.get("/inventory/{entity_id}")
    async def item_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if not entity_id or entity_id.strip() == "":
            return RedirectResponse("/inventory", status_code=302)
        from ui.routes.settings import _check_permission
        denied = await _check_permission(request, "view_inventory")
        if denied:
            return denied
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
        # Split preview for the item-detail split card; own try/except so a non-splittable item
        # (or any preview error) degrades to the disabled card rather than blanking the page.
        try:
            split_preview = await api.split_preview(token, entity_id)
        except APIError:
            split_preview = None
        # The base price list name (used by the pricing tab's derivation note) rides on the
        # company payload; the settings tab manages it through its dedicated endpoint.
        base_price_list = (company.get("settings") or {}).get("base_price_list") or DEFAULT_PRICE_LIST_NAME

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
        schema = _apply_amount_edit_permission(schema, _get_role(request), company.get("settings") or {})
        # Merge category-specific fields for this item's category
        item_cat = item.get("category", "")
        if item_cat and item_cat in cat_schemas:
            global_keys = {f["key"] for f in schema}
            extra = [f for f in cat_schemas[item_cat] if f["key"] not in global_keys]
            schema = schema + extra

        # Build pricing_keys dynamically from company price lists
        pl_names = {pl.get("name", "") for pl in price_lists}
        # Include conventional key patterns (e.g. "retail_price" for "Retail")
        pl_conventional = {price_key(n) for n in pl_names}
        pricing_keys = pl_names | pl_conventional | {"total_cost", "total_wholesale", "total_retail"}
        detail_fields = [f for f in schema if f.get("key") not in pricing_keys and f.get("key") not in _PAIRED_SECONDARY_KEYS and not f.get("virtual")]
        pricing_fields = [f for f in schema if f.get("key") in pricing_keys]

        active_tab = request.query_params.get("tab", "details")

        units_list = units_resp if isinstance(units_resp, list) else []
        unit_names = [u["name"] for u in units_list]
        units_map = {u["name"]: u for u in units_list}
        # Attach reorder velocity-suggestion hints (grey placeholder for empty reorder fields).
        await _inject_reorder_hints(token, item)
        detail_renderers = _inventory_cell_renderers(schema, unit_names, units_map, currency=currency)

        return await base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Inventory", "/inventory"), (item.get("name") or item.get("sku") or entity_id, None)]),
            page_header(
                item.get("name") or item.get("sku") or entity_id,
                Div(
                    A(NotStr(_ICON_PRINT_SVG), href=f"/inventory/{entity_id}/worksheet/print", target="_blank",
                      cls="btn btn--ghost btn--icon", title="Print production worksheet"),
                    _print_label_dropdown(entity_id),
                    A(t("inv.back_to_inventory"), href="/inventory", cls="btn btn--secondary"),
                    cls="header-actions",
                ),
            ),
            Script(_SPLIT_DELTA_JS),
            Script(_BULK_SPLIT_JS),
            _item_detail_tabs(entity_id, item, detail_fields, pricing_fields, ledger, currency, active_tab, price_lists=price_lists, cell_renderers=detail_renderers, base_price_list=base_price_list, split_preview=split_preview),
            title="Inventory Item - Celerp",
            nav_active="inventory",
            request=request,
        )

    @app.get("/inventory/{entity_id}/history")
    async def item_history_page(request: Request, entity_id: str):
        """Full, paginated activity history for one item (the Activity tab shows only the
        most recent; this is the 'see all' surface). Reuses activity_table + pagination."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1
        per_page = 50
        offset = (page - 1) * per_page
        try:
            item, company = await asyncio.gather(api.get_item(token, entity_id), api.get_company(token))
            resp = await api.list_ledger(token, {"entity_id": entity_id, "limit": per_page, "offset": offset})
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            item, company, resp = {}, {}, {"items": [], "total": 0}

        currency = company.get("currency")
        activities = resp.get("items", [])
        total = resp.get("total", 0)
        name = item.get("name") or item.get("sku") or entity_id

        from ui.components.activity import activity_table
        table = activity_table(activities, title="", section_cls="",
                               subject_entity_id=entity_id, currency=currency, resizable=True)
        pages = max(1, (total + per_page - 1) // per_page)
        pager = pagination(page, total, per_page, f"/inventory/{entity_id}/history") if pages > 1 else ""

        return await base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Inventory", "/inventory"),
                         (name, f"/inventory/{entity_id}"), ("History", None)]),
            page_header(
                f"History: {name}",
                A("Back to item", href=f"/inventory/{entity_id}", cls="btn btn--secondary"),
            ),
            Div(table, pager, cls="page-body"),
            title="Item History - Celerp",
            nav_active="inventory",
            request=request,
        )

    @app.get("/api/items/{entity_id}/label-templates")
    async def item_label_templates(request: Request, entity_id: str):
        """Return label template dropdown options for the print button."""
        from ui.config import API_BASE
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{API_BASE}/api/labels/templates",
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

    async def _recipe_section_response(token: str, entity_id: str, show_all: bool = False,
                                       flash_msg: str | None = None, flash_kind: str = "success"):
        """Fetch item + item list + currency and render the Manufacturing section."""
        item, company = await asyncio.gather(api.get_item(token, entity_id), api.get_company(token))
        items = (await api.list_items(token, {"limit": 1000})).get("items", [])
        return _recipe_section(entity_id, item, items, currency_symbol(company.get("currency") or ""),
                               show_all=show_all, flash_msg=flash_msg, flash_kind=flash_kind)

    def _recipe_saved_status(msg: str = "Saved ✓", kind: str = "saved"):
        return Div(msg, id="recipe-save-status", cls=f"recipe-save-status hint {kind}", hx_swap_oob="true")

    @app.get("/api/items/{entity_id}/recipe-section")
    async def recipe_section(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        # The self-refresh (recipeSaved) includes #recipe-form, so the picker scope survives.
        show_all = str(request.query_params.get("show_all_components", "")) in ("on", "true", "1")
        try:
            return await _recipe_section_response(token, entity_id, show_all=show_all)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return Div(P(e.detail, cls="cell-error"), id="recipe-section")

    async def _production_block_response(token: str, entity_id: str, flash_msg: str | None = None,
                                         flash_kind: str = "success"):
        item, company, hub = await asyncio.gather(
            api.get_item(token, entity_id), api.get_company(token),
            api.manufacturing_item_hub(token, entity_id),
        )
        cur = currency_symbol(company.get("currency") or (company.get("settings") or {}).get("currency") or "")
        return _production_block(entity_id, item, hub, cur, flash_msg=flash_msg, flash_kind=flash_kind)

    @app.get("/api/items/{entity_id}/production-block")
    async def production_block(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            return await _production_block_response(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return Div(P(e.detail, cls="cell-error"), id="production-block")

    _RUN_ACTIONS = {
        "start": api.start_mfg_order, "complete": lambda tok, rid: api.complete_mfg_order(tok, rid),
        "hold": lambda tok, rid: api.hold_mfg_order(tok, rid), "resume": api.resume_mfg_order,
        "cancel": lambda tok, rid: api.cancel_mfg_order(tok, rid),
    }

    @app.post("/api/items/{entity_id}/runs/{run_id}/act")
    async def run_action(request: Request, entity_id: str, run_id: str):
        """Advance a work order via its Action dropdown (action in the form body); refresh the block."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        fn = _RUN_ACTIONS.get(str(form.get("action") or ""))
        if fn is None:
            return await _production_block_response(token, entity_id)
        try:
            await fn(token, run_id)
            return await _production_block_response(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return await _production_block_response(token, entity_id, flash_msg=e.detail, flash_kind="error")

    @app.post("/api/items/{entity_id}/recipe-section")
    async def recipe_section_edit(request: Request, entity_id: str):
        """Structural recipe change (add/remove/clear row): persist immediately, re-render."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        show_all = str(form.get("show_all_components", "")) in ("on", "true", "1")
        action = request.query_params.get("action", "")
        try:
            index = int(request.query_params.get("index", "-1"))
        except ValueError:
            index = -1
        try:
            item = await api.get_item(token, entity_id)
            recipe = item.get("recipe") or {"output_qty": 1, "components": [], "labor": [], "overhead": []}
            changed = True
            if action == "add_component":
                new_id = str(form.get("comp_new", "")).strip()
                if new_id.startswith("__scope__"):
                    # In-dropdown scope toggle for this picker: no data change, just re-render.
                    show_all = new_id == "__scope__all"
                    changed = False
                else:
                    # A process recipe can use the same ingredient in more than one step
                    # (water added in two stages of dough), so the same component may appear
                    # more than once; each line carries its own quantity and cost.
                    changed = bool(new_id)
                    if changed:
                        recipe["components"].append({"item_id": new_id, "quantity": 1})
            elif action == "add_labor":
                # The whole line is filled in the add-row, so commit all fields at once.
                new_op = str(form.get("labor_new_op", "")).strip()
                changed = bool(new_op)
                if changed:
                    kind = str(form.get("labor_new_kind") or "hourly")
                    # Fixed uses the flat amount; rate-based lines derive cost from hours x rate,
                    # so amount is 0 (the add-row's Total there is just a read-only preview).
                    amount = _num_field(form.get("labor_new_amount")) if kind == "fixed" else 0.0
                    recipe["labor"].append({
                        "operation": new_op, "kind": kind,
                        "hours": _num_field(form.get("labor_new_hours")),
                        "rate": _num_field(form.get("labor_new_rate")),
                        "amount": amount,
                    })
            elif action == "add_overhead":
                new_desc = str(form.get("oh_new_desc", "")).strip()
                changed = bool(new_desc)
                if changed:
                    recipe["overhead"].append({"description": new_desc, "amount": _num_field(form.get("oh_new_amount"))})
            elif action == "remove_component" and 0 <= index < len(recipe.get("components", [])):
                recipe["components"].pop(index)
            elif action == "remove_labor" and 0 <= index < len(recipe.get("labor", [])):
                recipe["labor"].pop(index)
            elif action == "remove_overhead" and 0 <= index < len(recipe.get("overhead", [])):
                recipe["overhead"].pop(index)
            elif action == "clear":
                recipe = {"output_qty": 1, "components": [], "labor": [], "overhead": []}
            else:  # "noop" (Show-all toggle) or an out-of-range index: render only
                changed = False
            if changed:
                await api.set_item_recipe(token, entity_id, _recipe_api_payload(recipe))
            return await _recipe_section_response(token, entity_id, show_all=show_all)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            try:
                return await _recipe_section_response(token, entity_id, show_all=show_all,
                                                      flash_msg=e.detail, flash_kind="error")
            except APIError:
                return Div(P(e.detail, cls="cell-error"), id="recipe-section")

    @app.post("/api/items/{entity_id}/recipe-autosave")
    async def recipe_autosave(request: Request, entity_id: str):
        """Persist the output quantity on change, then fire recipeSaved to re-render the section."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        try:
            output_qty = float(str(form.get("output_qty", "1")) or 1)
        except ValueError:
            return _recipe_saved_status("Not saved: output quantity must be a number.", "error")
        try:
            item = await api.get_item(token, entity_id)
            recipe = item.get("recipe") or {"output_qty": 1, "components": [], "labor": [], "overhead": []}
            recipe["output_qty"] = output_qty
            await api.set_item_recipe(token, entity_id, _recipe_api_payload(recipe))
            # Body is ignored (the output_qty input swaps nothing); the trigger re-renders the section.
            return "", HttpHeader("HX-Trigger", "recipeSaved")
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return _recipe_saved_status(f"Not saved: {e.detail}", "error")

    @app.get("/api/items/{entity_id}/recipe-cell/{section}/{idx}/{field}/edit")
    async def recipe_cell_edit(request: Request, entity_id: str, section: str, idx: int, field: str):
        """Edit mode for one recipe value: the system-standard editable cell."""
        from ui.components.table import editable_cell
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        cell_type = _RECIPE_CELLS.get(section, {}).get(field)
        if cell_type is None:
            return P("Unknown recipe field", cls="cell-error")
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(e.detail, cls="cell-error")
        rows_ = (item.get("recipe") or {}).get(section, [])
        value = rows_[idx].get(field, "") if 0 <= idx < len(rows_) else ""
        return editable_cell(
            entity_id=entity_id, field=f"recipe__{section}__{idx}__{field}", value=value,
            cell_type=cell_type,
            options=list(_LABOR_KIND_LABELS) if field == "kind" else None,
            restore_url=f"/api/items/{entity_id}/recipe-cell/{section}/{idx}/{field}/display",
            label_map=_LABOR_KIND_LABELS if field == "kind" else None,
        )

    @app.get("/api/items/{entity_id}/recipe-cell/{section}/{idx}/{field}/display")
    async def recipe_cell_display(request: Request, entity_id: str, section: str, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if _RECIPE_CELLS.get(section, {}).get(field) is None:
            return P("Unknown recipe field", cls="cell-error")
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(e.detail, cls="cell-error")
        rows_ = (item.get("recipe") or {}).get(section, [])
        value = rows_[idx].get(field, "") if 0 <= idx < len(rows_) else ""
        return _recipe_cell(entity_id, section, idx, field, value)

    # ── Production workflow (ordered build steps) ────────────────────────────
    async def _workflow_section_response(token: str, entity_id: str):
        item = await api.get_item(token, entity_id)
        return _workflow_section(entity_id, item)

    @app.get("/api/items/{entity_id}/workflow-section")
    async def workflow_section(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            return await _workflow_section_response(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return Div(P(e.detail, cls="cell-error"), id="workflow-section")

    @app.post("/api/items/{entity_id}/workflow-section")
    async def workflow_section_edit(request: Request, entity_id: str):
        """Structural workflow change (add / remove / reorder / set or upload a step reference)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        action = request.query_params.get("action", "")
        form = await request.form()
        try:
            index = int(request.query_params.get("index", "-1"))
        except ValueError:
            index = -1
        try:
            item = await api.get_item(token, entity_id)
            steps = list((item.get("workflow") or {}).get("steps") or [])
            if action == "add":
                steps.append({"station": "", "instructions": "",
                              "time_value": 0, "time_unit": "min", "wait": False})
            elif action == "remove" and 0 <= index < len(steps):
                steps.pop(index)
            elif action == "reorder":
                order = [s for s in str(form.get("order", "")).split(",") if s]
                by_id = {s.get("id"): s for s in steps}
                seen = set(order)
                steps = [by_id[i] for i in order if i in by_id] + [s for s in steps if s.get("id") not in seen]
            elif action == "clear_ref" and 0 <= index < len(steps):
                steps[index]["ref_file_id"] = None
            elif action == "upload_ref" and 0 <= index < len(steps):
                up = form.get("file")
                if up is not None:
                    meta = await api.upload_item_file(token, entity_id, up)
                    steps[index]["ref_file_id"] = meta.get("file_id") or meta.get("id")
            await api.set_item_workflow(token, entity_id, {"steps": steps})
            return await _workflow_section_response(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return P(t("error.unauthorized"), cls="cell-error")
            return Div(P(e.detail, cls="cell-error"), id="workflow-section")

    @app.get("/api/items/{entity_id}/workflow-cell/{idx}/{field}/edit")
    async def workflow_cell_edit(request: Request, entity_id: str, idx: int, field: str):
        from ui.components.table import editable_cell
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        cell_type = _WORKFLOW_CELLS.get(field)
        if cell_type is None:
            return P("Unknown workflow field", cls="cell-error")
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(e.detail, cls="cell-error")
        steps = (item.get("workflow") or {}).get("steps") or []
        value = steps[idx].get(field, "") if 0 <= idx < len(steps) else ""
        options = list(_WORKFLOW_TIME_UNITS) if field == "time_unit" else None
        allow_custom = False
        if field == "station":
            # Dropdown of the company's work centers, with an add-new option (GDR §2i searchable).
            try:
                centers = (await api.list_work_centers(token)).get("items", [])
            except APIError:
                centers = []
            options = [c.get("name") for c in centers if c.get("name")] + [
                ("__new__:/settings/manufacturing#work-centers", "+ Add new work center")]
            allow_custom = True
        return editable_cell(
            entity_id=entity_id, field=f"workflow__{idx}__{field}", value=value, cell_type=cell_type,
            options=options, allow_custom=allow_custom,
            restore_url=f"/api/items/{entity_id}/workflow-cell/{idx}/{field}/display",
        )

    @app.get("/api/items/{entity_id}/workflow-cell/{idx}/{field}/display")
    async def workflow_cell_display(request: Request, entity_id: str, idx: int, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        if _WORKFLOW_CELLS.get(field) is None:
            return P("Unknown workflow field", cls="cell-error")
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(e.detail, cls="cell-error")
        steps = (item.get("workflow") or {}).get("steps") or []
        value = steps[idx].get(field, "") if 0 <= idx < len(steps) else ""
        return _workflow_cell(entity_id, idx, field, value)

    @app.get("/inventory/{entity_id}/worksheet/print")
    async def worksheet_print(request: Request, entity_id: str):
        """Standalone printable production worksheet (auto window.print(); user saves as PDF)."""
        from datetime import datetime, timezone

        from fasthtml.common import to_xml
        from starlette.responses import HTMLResponse
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return HTMLResponse(to_xml(_worksheet_print_view(entity_id, item, today)))

    @app.post("/api/items/{entity_id}/gallery-hero/{file_id}")
    async def gallery_set_hero(request: Request, entity_id: str, file_id: str):
        """Set a file as the hero image by clicking its gallery thumbnail; re-render the gallery."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            await api.set_item_file_hero(token, entity_id, file_id)
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        return _workflow_gallery(entity_id, item)

    @app.get("/api/items/{entity_id}/field/{field}/edit")
    async def field_edit_cell(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            schema, item, cat_schemas, locs, company = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
                api.get_company(token),
            )
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        if not role_has_permission(company.get("settings") or {}, _get_role(request), "edit_inventory"):
            # Without the edit permission, swap back a non-editable display cell
            # (a role granted the write in the matrix keeps its input).
            from ui.components.table import display_cell
            _f = next((x for x in schema if x.get("key") == field), {})
            return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                                cell_type=_f.get("type", "text"), editable=False)
        if field in AMOUNT_ITEM_KEYS and not role_has_permission(company.get("settings") or {}, _get_role(request), "edit_inventory_amounts"):
            # Amount fields (quantity/weight/pieces/gross_weight) are gated by
            # edit_inventory_amounts: this GET is the single edit-entry chokepoint, so no
            # amount cell can enter edit state without the permission, however it rendered.
            from ui.components.table import display_cell
            _f = next((x for x in schema if x.get("key") == field), {})
            return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                                cell_type=_f.get("type", "number"), editable=False)
        locations = locs.get("items", [])

        # Virtual total field (e.g. cost_price_total): show editable cell with computed value
        virtual_fields = {f["key"]: f for f in schema if f.get("virtual")}
        if field in virtual_fields:
            vf = virtual_fields[field]
            paired = vf.get("paired_with", "")
            try:
                if field == "cost_price_total" and item.get("cost_total") is not None:
                    # cost_total is the primitive; display it directly
                    total = float(item["cost_total"])
                else:
                    unit_price = float(item.get(paired) or 0)
                    qty = float(item.get("quantity") or 0)
                    total = unit_price * qty
                display_val = f"{total:.2f}" if total != 0 else ""
            except (ValueError, TypeError):
                display_val = ""
            from ui.components.table import editable_cell
            return editable_cell(entity_id=entity_id, field=field, value=display_val,
                                 cell_type="money", options=[], allow_custom=True)
        f_def, cell_type, options, allow_custom = _resolve_field_def(field, schema, cat_schemas, item, locations)
        from ui.components.table import editable_cell
        # Apply unit-field override (sell_by, purchase_unit, weight_unit → searchable select)
        if field in ("sell_by", "purchase_unit", "weight_unit", "gross_weight_unit"):
            try:
                units_resp = await api.get_units(token)
                unit_names = [u["name"] for u in units_resp if u.get("name")]
                weight_unit_names = [u["name"] for u in units_resp if u.get("unit_type") == "weight"]
            except Exception:
                unit_names = []
                weight_unit_names = []
            cell_type, options, allow_custom = _apply_unit_field_override(field, cell_type, options, allow_custom, unit_names, weight_unit_names)
        label_map: dict | None = None
        if field == "category":
            try:
                label_map = await api.get_category_display_names(token)
            except Exception:
                label_map = None
        elif field == "inventory_type":
            label_map = _INVENTORY_TYPE_LABELS
        # location_name: render a select cell with locations + "Add new" as last option
        if field == "location_name":
            loc_names = [loc.get("name", "") for loc in locations if loc.get("name")]
            current_val = str(item.get("location_name") or "")
            restore_url = f"/api/items/{entity_id}/field/{field}/display"
            patch_url = f"/api/items/{entity_id}/field/{field}"
            swap = dict(hx_patch=patch_url, hx_target="closest td", hx_swap="outerHTML", hx_include="this")
            escape_js = (
                f"if(event.key==='Escape'){{"
                f"var _sw=document.querySelector('.table-scroll-wrap');"
                f"if(_sw&&window.__celerpScrollSnap!==undefined){{window.__celerpScrollSnap=_sw.scrollLeft;}}"
                f"htmx.ajax('GET','{restore_url}',{{target:this.closest('td'),swap:'outerHTML'}});"
                f"event.preventDefault();}}"
            )
            add_new_url = "/settings/inventory?tab=locations"
            import json as _json_loc
            safe_val = _json_loc.dumps(current_val)  # JSON-encoded to prevent XSS
            onchange_js = (
                f"if(this.value==='__add_new__'){{"
                f"window.open('{add_new_url}','_blank');this.value={safe_val};}}"
            )
            return Td(
                Select(
                    *[Option(n, value=n, selected=(n == current_val)) for n in loc_names],
                    Option("+ Add new location", value="__add_new__"),
                    name="value", **swap, hx_trigger="change[this.value!='__add_new__']",
                    cls="cell-input cell-input--select", autofocus=True,
                    onkeydown=escape_js,
                    onchange=onchange_js,
                ),
                cls="cell cell--editing",
            )
        # Reorder fields: carry the velocity suggestion as a grey placeholder while editing
        # an empty field (a hint only - it is never submitted, so the stored value is unaffected).
        _placeholder = await _reorder_placeholder(token, entity_id, field, item.get(field))
        return editable_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                             cell_type=cell_type, options=options, allow_custom=allow_custom,
                             label_map=label_map, placeholder=_placeholder)

    @app.get("/api/items/{entity_id}/field/{field}/display")
    async def field_display_cell(request: Request, entity_id: str, field: str):
        """Restore a cell to display (read-only) state — used by Escape key handler."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        try:
            schema, item, cat_schemas, locs, company = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
                api.get_company(token),
            )
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")
        locations = locs.get("items", [])
        # ESC restore inherits the same amount read-only state as the static cell, so a
        # restored amount cell never re-offers click-to-edit without the permission.
        schema = _apply_amount_edit_permission(schema, _get_role(request), company.get("settings") or {})
        f_def, cell_type, options, _ = _resolve_field_def(field, schema, cat_schemas, item, locations)
        from ui.components.table import display_cell
        label_map: dict | None = None
        if field == "category":
            try:
                label_map = await api.get_category_display_names(token)
            except Exception:
                label_map = None
        elif field == "inventory_type":
            label_map = _INVENTORY_TYPE_LABELS
        # Virtual total fields store no value in item state; derive from primitives
        virtual_fields = {f["key"]: f for f in schema if f.get("virtual")}
        if field in virtual_fields:
            vf = virtual_fields[field]
            paired = vf.get("paired_with", "")
            try:
                if field == "cost_price_total" and item.get("cost_total") is not None:
                    unit_price = float(item["cost_total"])
                    qty = 1.0
                else:
                    unit_price = float(item.get(paired) or 0)
                    qty = float(item.get("quantity") or 0)
            except (ValueError, TypeError):
                unit_price, qty = 0.0, 0.0
            company = await api.get_company(token)
            currency = (company or {}).get("currency") or "USD"
            return _render_virtual_total_cell(entity_id, field, unit_price, qty, currency)
        # Reorder fields: keep the grey suggestion visible instantly on Escape (empty field).
        _placeholder = await _reorder_placeholder(token, entity_id, field, item.get(field))
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True,
                            label_map=label_map, placeholder=_placeholder)

    _PAIRED_FIELDS: dict[str, str] = {"quantity": "sell_by", "sell_by": "quantity",
                                      "weight": "weight_unit", "weight_unit": "weight",
                                      "gross_weight": "gross_weight_unit", "gross_weight_unit": "gross_weight",
                                      "purchase_unit": "purchase_conversion_factor",
                                      "purchase_conversion_factor": "purchase_unit"}

    @app.patch("/api/items/{entity_id}/field/{field}")
    async def field_patch(request: Request, entity_id: str, field: str):
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="cell-error")
        form = await request.form()
        value: str | float | bool = str(form.get("value", ""))

        # Workflow cells (field = workflow__{idx}__{name}): patch one value on the persisted
        # workflow and swap ONLY that cell back. No section reload — the running total is
        # recomputed client-side (WORKFLOW_JS) so editing a unit never reloads the page.
        if field.startswith("workflow__"):
            from ui.components.table import editable_cell
            try:
                _, idx_s, fname = field.split("__", 2)
                idx = int(idx_s)
            except ValueError:
                return P("Bad workflow field", cls="cell-error")
            restore_url = f"/api/items/{entity_id}/workflow-cell/{idx}/{fname}/display"
            cell_type = _WORKFLOW_CELLS.get(fname)
            if cell_type is None:
                return P("Unknown workflow field", cls="cell-error")
            if cell_type == "number":
                try:
                    value = float(str(value) or 0)
                except (ValueError, TypeError):
                    edit_td = editable_cell(entity_id=entity_id, field=field, value=str(form.get("value", "")),
                                            cell_type="number", restore_url=restore_url)
                    edit_td.attrs["class"] = (edit_td.attrs.get("class", "") + " cell--error").strip()
                    return edit_td
            try:
                item = await api.get_item(token, entity_id)
                steps = list((item.get("workflow") or {}).get("steps") or [])
                if not (0 <= idx < len(steps)):
                    return P("Step no longer exists. Reload the tab.", cls="cell-error")
                steps[idx][fname] = value
                await api.set_item_workflow(token, entity_id, {"steps": steps})
                item = await api.get_item(token, entity_id)
                fresh = (item.get("workflow") or {}).get("steps") or []
                new_val = fresh[idx].get(fname, "") if 0 <= idx < len(fresh) else ""
                return _workflow_cell(entity_id, idx, fname, new_val)
            except APIError as e:
                if e.status == 401:
                    return P(t("error.unauthorized"), cls="cell-error")
                edit_td = editable_cell(entity_id=entity_id, field=field, value=str(form.get("value", "")),
                                        cell_type=_WORKFLOW_CELLS.get(fname, "text"), restore_url=restore_url,
                                        options=list(_WORKFLOW_TIME_UNITS) if fname == "time_unit" else None)
                edit_td.attrs["class"] = (edit_td.attrs.get("class", "") + " cell--error").strip()
                edit_td.attrs["title"] = f"Not saved: {e.detail}"
                return edit_td

        # Recipe cells (field = recipe__{section}__{idx}__{name}): patch one value on the
        # persisted recipe, then return the display cell + cost card refresh (out-of-band).
        if field.startswith("recipe__"):
            from ui.components.table import editable_cell
            try:
                _, section, idx_s, fname = field.split("__", 3)
                idx = int(idx_s)
            except ValueError:
                return P("Bad recipe field", cls="cell-error")
            cell_type = _RECIPE_CELLS.get(section, {}).get(fname)
            if cell_type is None:
                return P("Unknown recipe field", cls="cell-error")
            restore_url = f"/api/items/{entity_id}/recipe-cell/{section}/{idx}/{fname}/display"
            if cell_type == "number":
                try:
                    value = float(str(value) or 0)
                except (ValueError, TypeError):
                    edit_td = editable_cell(entity_id=entity_id, field=field, value=str(form.get("value", "")),
                                            cell_type="number", restore_url=restore_url)
                    edit_td.attrs["class"] = (edit_td.attrs.get("class", "") + " cell--error").strip()
                    return edit_td
            try:
                item = await api.get_item(token, entity_id)
                recipe = item.get("recipe") or {}
                rows_ = recipe.get(section) or []
                if not (0 <= idx < len(rows_)):
                    return P("Row no longer exists. Reload the tab.", cls="cell-error")
                rows_[idx][fname] = value
                await api.set_item_recipe(token, entity_id, _recipe_api_payload(recipe))
                item = await api.get_item(token, entity_id)
                fresh = (item.get("recipe") or {}).get(section, [])
                new_val = fresh[idx].get(fname, "") if 0 <= idx < len(fresh) else ""
                # A <td> swap cannot carry OOB siblings (table fragment parsing mangles them);
                # fire recipeSaved instead, which the cost card + status listen for.
                return _recipe_cell(entity_id, section, idx, fname, new_val), HttpHeader("HX-Trigger", "recipeSaved")
            except APIError as e:
                if e.status == 401:
                    return P(t("error.unauthorized"), cls="cell-error")
                # Keep the user in the editor with the system-standard error styling.
                edit_td = editable_cell(entity_id=entity_id, field=field, value=str(form.get("value", "")),
                                        cell_type=cell_type, restore_url=restore_url,
                                        options=list(_LABOR_KIND_LABELS) if fname == "kind" else None,
                                        label_map=_LABOR_KIND_LABELS if fname == "kind" else None)
                edit_td.attrs["class"] = (edit_td.attrs.get("class", "") + " cell--error").strip()
                edit_td.attrs["title"] = f"Not saved: {e.detail}"
                return edit_td

        # Convert bool fields from string to proper bool
        if field == "allow_splitting":
            value = value.lower() in ("true", "1", "yes")
        # Convert numeric fields from string to float
        elif field == "quantity" or field.endswith("_price") or field.endswith("_price_total"):
            raw_str = str(form.get("value", ""))
            if raw_str.strip() == "" and field != "quantity":
                value = None  # blanking an optional price/cost cell = clear it (issue #202)
            else:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    # Return editable cell with error so user can correct without a page reload.
                    cell_type = "number" if field == "quantity" else "money"
                    patch_url = f"/api/items/{entity_id}/field/{field}"
                    restore_url = (
                        f"/api/items/{entity_id}/field/{field}/paired-display"
                        if field in _PAIRED_FIELDS
                        else f"/api/items/{entity_id}/field/{field}/display"
                    )
                    from ui.components.table import editable_cell
                    edit_td = editable_cell(
                        entity_id=entity_id, field=field, value=raw_str,
                        cell_type=cell_type, restore_url=restore_url,
                    )
                    edit_td.attrs["class"] = (edit_td.attrs.get("class", "") + " cell--error").strip()
                    return edit_td

        try:
            if value is None:
                # Clearing an optional price/cost field (issue #202): unset it. A "<x>_price_total"
                # cell is virtual — clear its underlying unit price (cost maps to cost_total, which is
                # the canonical primitive for cost).
                if field.endswith("_price_total"):
                    _unit = field[: -len("_total")]          # retail_price_total -> retail_price
                    clear_field = "cost_total" if _unit == "cost_price" else _unit
                else:
                    clear_field = "cost_total" if field == "cost_price" else field
                old_item = await api.get_item(token, entity_id)
                await api.patch_item(token, entity_id, {clear_field: {"old": old_item.get(clear_field), "new": None}})
            elif field == "location_name":
                # Transfer requires location_id; resolve name → id from locations list
                locs = (await api.get_locations(token)).get("items", [])
                loc = next((l for l in locs if l.get("name") == value), None)
                if loc is None:
                    return P(f"Unknown location: {value}", cls="cell-error")
                await api.transfer_item(token, entity_id, loc.get("location_id") or loc.get("id", ""))
            elif field.endswith("_price_total"):
                # Virtual total field: patch cost_total directly; back-calculate unit for other price lists.
                # Return: a placeholder td (keeps the cell in place) + OOB row reload (refreshes all cells
                # with correct formatting). Single-cell render can't work here because cost_total is not
                # a schema field, so field resolution produces no formatting.
                unit_price_field = field[: -len("_total")]  # e.g. "cost_price_total" → "cost_price"
                old_item = await api.get_item(token, entity_id)
                if unit_price_field == "cost_price":
                    # cost_total is the primitive; patch it directly
                    old_cost_total = old_item.get("cost_total")
                    await api.patch_item(token, entity_id, {"cost_total": {"old": old_cost_total, "new": float(value)}})
                else:
                    qty = float(old_item.get("quantity") or 0)
                    if qty == 0:
                        return P(t("error.cannot_divide_by_zero_qty", "Cannot compute unit price: quantity is zero"), cls="cell-error")
                    # Derive the unit price (a rate) at the fewest decimals that make rate*qty
                    # reconcile to the entered total - so the typed total is honoured exactly and the
                    # stored rate stays clean. Same helper as CSV import (DRY) -> both pages agree.
                    try:
                        _co = await api.get_company(token)
                        _cur = (_co.get("currency") or "").strip() or "USD"
                    except Exception:
                        _cur = "USD"
                    from celerp.services.money import unit_price_from_total, to_stored_float
                    unit_price = to_stored_float(unit_price_from_total(value, qty, _cur))
                    old_unit_price = old_item.get(unit_price_field)
                    await api.patch_item(token, entity_id, {unit_price_field: {"old": old_unit_price, "new": unit_price}})
                safe_id = entity_id.replace(":", "-")
                # Fetch updated item and currency for cell rendering
                updated_item = await api.get_item(token, entity_id)
                try:
                    company = await api.get_company(token)
                    currency = (company.get("currency") or "").strip() or None
                except Exception:
                    currency = None
                flat = _flatten_item_attrs(updated_item)
                qty = float(flat.get("quantity") or 0)
                new_cost_total = float(value)
                new_cost_price = new_cost_total / qty if qty else 0.0
                from ui.components.table import fmt_money, display_cell
                # Cell 1: cost_price_total (the edited cell, main swap target)
                total_formatted = fmt_money(new_cost_total, currency) if new_cost_total != 0 else "--"
                total_inner = Span(total_formatted, cls="cell-money") if total_formatted != "--" else Span("--")
                total_td = Td(
                    total_inner,
                    id=f"cell-{safe_id}-cost_price_total",
                    cls="cell cell--money cell--clickable",
                    data_col="cost_price_total",
                    hx_get=f"/api/items/{entity_id}/field/cost_price_total/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                )
                # Cell 2: cost_price (OOB swap into its existing td by id)
                unit_formatted = fmt_money(new_cost_price, currency) if new_cost_price != 0 else "--"
                unit_inner = Span(unit_formatted, cls="cell-money") if unit_formatted != "--" else Span("--")
                sell_by = (flat.get("sell_by") or "").strip()
                annotation = Span(f"/ {sell_by}", cls="cell-price-unit") if sell_by else ""
                unit_td = Td(
                    unit_inner, annotation,
                    id=f"cell-{safe_id}-cost_price",
                    hx_swap_oob="true",
                    cls="cell cell--money cell--clickable",
                    data_col="cost_price",
                    hx_get=f"/api/items/{entity_id}/field/cost_price/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                )
                return total_td, unit_td
            else:
                # Fetch old value before patching so activity log shows old → new.
                # get_item returns a flatten_item result: attribute fields are already
                # promoted to top level, so old_item.get(field) is always authoritative.
                old_item = await api.get_item(token, entity_id)
                if field == "cost_price":
                    # cost_total is the canonical primitive in state. Editing unit cost must
                    # derive and overwrite cost_total (unit × qty) so flatten_item's back-
                    # calculation doesn't silently revert the edit on next read.
                    qty = float(old_item.get("quantity") or 0)
                    new_unit = float(value)
                    new_total = round(new_unit * qty, 2)
                    old_total = old_item.get("cost_total")
                    await api.patch_item(token, entity_id, {"cost_total": {"old": old_total, "new": new_total}})
                else:
                    old_val = old_item.get(field)
                    await api.patch_item(token, entity_id, {field: {"old": old_val, "new": value}})
            schema, item, cat_schemas, locs_data = await asyncio.gather(
                api.get_item_schema(token),
                api.get_item(token, entity_id),
                api.get_all_category_schemas(token),
                api.get_locations(token),
            )
        except APIError as e:
            return P(e.detail, cls="cell-error")

        locations = locs_data.get("items", [])
        try:
            _fp_company = await api.get_company(token)
        except Exception:
            _fp_company = {}
        schema = _apply_amount_edit_permission(schema, _get_role(request), _fp_company.get("settings") or {})
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
        # Allow-splitting change on the detail page: return the toggled cell plus an OOB reload
        # of the actions card so the Split card reflects the new value without a page reload.
        # Mirrors the category -> attributes-section idiom above. On any non-detail URL the branch
        # is skipped and execution falls through to the generic display_cell return (no
        # #item-advanced-panel element exists there).
        if field == "allow_splitting":
            current_url = request.headers.get("hx-current-url", "")
            if "/inventory/item:" in current_url:
                from ui.components.table import display_cell
                split_cell = display_cell(
                    entity_id=entity_id, field=field, value=item.get(field, ""),
                    cell_type=cell_type, options=options,
                    editable=f_def.get("editable", True) if f_def else True,
                )
                oob_reload = Div(
                    hx_get=f"/api/items/{entity_id}/advanced-panel",
                    hx_trigger="load",
                    hx_swap="outerHTML",
                    hx_swap_oob="true",
                    id="item-advanced-panel",
                )
                return split_cell, oob_reload
        # Paired fields: return the combined paired cell after save
        if field in _PAIRED_FIELDS:
            from ui.components.table import fmt_money
            try:
                paired_td = await _paired_display(token, entity_id, field, _get_role(request), _fp_company.get("settings") or {})
            except Exception:
                paired_td = None
            if paired_td is not None:
                # If quantity changes, any virtual total columns (e.g. cost_price_total) must refresh
                has_virtual_totals = any(f2.get("virtual") and f2.get("type") == "money" for f2 in schema)
                if field == "quantity" and has_virtual_totals:
                    safe_id = entity_id.replace(":", "-")
                    # Re-fetch updated item for new virtual total values
                    updated_item = await api.get_item(token, entity_id)
                    flat = _flatten_item_attrs(updated_item)
                    try:
                        company = await api.get_company(token)
                        currency2 = (company.get("currency") or "").strip() or None
                    except Exception:
                        currency2 = None
                    # Build OOB cells for all virtual totals
                    oob_cells = []
                    for vf in schema:
                        if not (vf.get("virtual") and vf.get("type") == "money"):
                            continue
                        vkey = vf["key"]
                        cell_id = f"cell-{safe_id}-{vkey}"
                        if vkey == "cost_price_total":
                            cost_total = float(flat.get("cost_total") or 0)
                            formatted = fmt_money(cost_total, currency2) if cost_total != 0 else "--"
                        else:
                            unit_field = vf.get("paired_with", "")
                            unit_price = float(flat.get(unit_field) or 0)
                            new_qty = float(flat.get("quantity") or 0)
                            total = unit_price * new_qty
                            formatted = fmt_money(total, currency2) if total != 0 else "--"
                        inner = Span(formatted, cls="cell-money") if formatted != "--" else Span("--")
                        oob_cells.append(Td(
                            inner,
                            id=cell_id,
                            hx_swap_oob="true",
                            cls="cell cell--money cell--clickable",
                            data_col=vkey,
                            hx_get=f"/api/items/{entity_id}/field/{vkey}/edit",
                            hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                        ))
                    # Also re-render derived weight/pieces cells (qty value changed)
                    try:
                        units_resp2 = await api.get_units(token)
                        _umap2 = {u["name"]: u for u in units_resp2 if u.get("name")}
                        unit_names2 = [u["name"] for u in units_resp2 if u.get("name")]
                    except Exception:
                        _umap2, unit_names2 = {}, []
                    cell_renderers2 = _inventory_cell_renderers(schema, unit_names2, _umap2)
                    for derive_field in ("weight", "pieces"):
                        if derive_field not in cell_renderers2:
                            continue
                        rendered = cell_renderers2[derive_field](entity_id, flat)
                        children = list(rendered.children) if hasattr(rendered, "children") else []
                        attrs = {k: v for k, v in rendered.attrs.items() if k not in ("id",)}
                        oob_cells.append(Td(*children, id=f"cell-{safe_id}-{derive_field}",
                                            hx_swap_oob="true", **attrs))
                    return paired_td, *oob_cells
                # sell_by change: re-render weight and pieces cells via shared renderers so
                # they correctly switch between derived (read-only) and paired/editable display.
                if field == "sell_by":
                    safe_id = entity_id.replace(":", "-")
                    try:
                        units_resp2 = await api.get_units(token)
                        _umap2 = {u["name"]: u for u in units_resp2 if u.get("name")}
                        unit_names2 = [u["name"] for u in units_resp2 if u.get("name")]
                    except Exception:
                        _umap2, unit_names2 = {}, []
                    cell_renderers2 = _inventory_cell_renderers(schema, unit_names2, _umap2)
                    flat = _flatten_item_attrs(item)
                    oob_derived = []
                    for derive_field in ("weight", "pieces"):
                        if derive_field not in cell_renderers2:
                            continue
                        rendered = cell_renderers2[derive_field](entity_id, flat)
                        # Ensure the td carries id + hx-swap-oob for OOB targeting.
                        # display_cell already sets id=cell-{safe_id}-{field}; derived Td does not.
                        # Re-create with both attrs as constructor kwargs (underscore→hyphen by FastHTML).
                        children = list(rendered.children) if hasattr(rendered, "children") else []
                        attrs = {k: v for k, v in rendered.attrs.items() if k not in ("id",)}
                        oob_td = Td(*children, id=f"cell-{safe_id}-{derive_field}",
                                    hx_swap_oob="true", **attrs)
                        oob_derived.append(oob_td)
                    if oob_derived:
                        return paired_td, *oob_derived
                return paired_td
        # Price fields: re-render with currency symbol + / sell_unit annotation
        if cell_type == "money":
            from ui.components.table import fmt_money
            sell_by = (item.get("sell_by") or "").strip()
            val = item.get(field, "")
            try:
                company = await api.get_company(token)
                currency = (company.get("currency") or "").strip() or None
            except Exception:
                currency = None
            try:
                formatted = fmt_money(val, currency) if val not in (None, "", "--") else "--"
            except (ValueError, TypeError):
                formatted = "--"
            annotation = Span(f"/ {sell_by}", cls="cell-price-unit") if sell_by else ""
            inner = Span(formatted, cls="cell-money") if formatted != "--" else Span("--")
            safe_id = entity_id.replace(":", "-")
            price_td = Td(
                inner, annotation,
                id=f"cell-{safe_id}-{field}",
                cls="cell cell--money cell--clickable",
                data_col=field,
                hx_get=f"/api/items/{entity_id}/field/{field}/edit",
                hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
            )
            # If this price field has a virtual total counterpart, update it OOB by cell id
            virtual_total_field = f"{field}_total"
            vf_def = next((f2 for f2 in schema if f2.get("key") == virtual_total_field and f2.get("virtual")), None)
            if vf_def:
                qty = float(item.get("quantity") or 0)
                if virtual_total_field == "cost_price_total":
                    total_val = float(item.get("cost_total") or 0)
                else:
                    total_val = float(val) * qty if val not in (None, "", "--") else 0.0
                total_formatted = fmt_money(total_val, currency) if total_val != 0 else "--"
                total_inner = Span(total_formatted, cls="cell-money") if total_formatted != "--" else Span("--")
                total_td = Td(
                    total_inner,
                    id=f"cell-{safe_id}-{virtual_total_field}",
                    hx_swap_oob="true",
                    cls="cell cell--money cell--clickable",
                    data_col=virtual_total_field,
                    hx_get=f"/api/items/{entity_id}/field/{virtual_total_field}/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                )
                return price_td, total_td
            return price_td
        from ui.components.table import display_cell
        label_map = _INVENTORY_TYPE_LABELS if field == "inventory_type" else None
        if field == "category":
            try:
                label_map = await api.get_category_display_names(token)
            except Exception:
                label_map = None
        # Reorder fields: if saved empty, re-show the grey suggestion immediately.
        _placeholder = await _reorder_placeholder(token, entity_id, field, item.get(field))
        return display_cell(entity_id=entity_id, field=field, value=item.get(field, ""),
                            cell_type=cell_type, options=options,
                            editable=f_def.get("editable", True) if f_def else True,
                            label_map=label_map, placeholder=_placeholder)

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
        pl_conventional = {price_key(n) for n in pl_names}
        pricing_keys = pl_names | pl_conventional | {"total_cost", "total_wholesale", "total_retail"}
        detail_fields = [f for f in schema if f.get("key") not in pricing_keys and f.get("key") not in _PAIRED_SECONDARY_KEYS and not f.get("virtual")]
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

    @app.get("/api/items/{entity_id}/advanced-panel")
    async def item_advanced_panel(request: Request, entity_id: str):
        """Return the item operations (actions) card fragment. Used by the item-detail page to
        refresh the Split card in place after the allow_splitting toggle changes."""
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return Response(str(e.detail), status_code=500)
        # Fresh split preview in its own try/except so a non-splittable item (or any preview
        # error) degrades to the disabled card rather than blanking the fragment.
        try:
            split_preview = await api.split_preview(token, entity_id)
        except APIError:
            split_preview = None
        return _advanced_panel(entity_id, item, split_preview)

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
        try:
            company = await api.get_company(token)
            currency = (company.get("currency") or "").strip() or None
        except Exception:
            company, currency = {}, None
        active_cat = item.get("category", "")
        eff_schema = _effective_schema(schema, cat_schemas, active_cat)
        eff_schema = _apply_amount_edit_permission(eff_schema, _get_role(request), company.get("settings") or {})
        col_prefs: dict = {}
        try:
            col_prefs = await api.get_column_prefs(token)
        except Exception:
            pass
        visible_cols = _resolve_visible_cols(eff_schema, col_prefs, active_cat, [])
        visible_cols_set = set(visible_cols) if visible_cols else None
        cell_renderers = _inventory_cell_renderers(eff_schema, unit_names, units_map, category_label_map, currency=currency)
        from ui.components.table import display_cell
        safe_id = entity_id.replace(":", "-")
        flat = _flatten_item_attrs(item)
        # Render ALL schema columns (minus paired secondaries), matching data_table behaviour.
        # Columns not in visible_cols are still rendered but hidden via style="display:none" so
        # that the td count matches the header and JS column toggling works correctly.
        all_cols = [f for f in eff_schema if f["key"] not in _PAIRED_SECONDARY_KEYS]
        data_cells = []
        for f in all_cols:
            col_hidden = visible_cols_set is not None and f["key"] not in visible_cols_set
            if f["key"] in cell_renderers:
                td = cell_renderers[f["key"]](entity_id, flat)
            else:
                td = display_cell(
                    entity_id=entity_id,
                    field=f["key"],
                    value=flat.get(f["key"], ""),
                    cell_type=f.get("type", "text"),
                    options=f.get("options"),
                    editable=f.get("editable", True),
                    currency=currency,
                )
            if col_hidden:
                # Inject display:none to match what data_table JS would apply
                td.attrs["style"] = "display:none"
            data_cells.append(td)
        # Checkbox cell (matches data_table output)
        checkbox_td = Td(
            Input(type="checkbox", cls="row-select", name="selected", value=entity_id,
                  data_entity_id=entity_id,
                  data_sku=flat.get("sku", ""),
                  data_name=flat.get("name", ""),
                  data_qty=str(flat.get("quantity", 0)),
                  data_weight=str(flat.get("weight", "") or ""),
                  data_weight_unit=flat.get("weight_unit", ""),
                  data_sell_by=flat.get("sell_by", ""),
            ),
            cls="col-checkbox",
        )
        # Action cell (matches data_table output)
        action_td = Td(
            Div(
                Button("⋮", cls="row-menu-btn", onclick=f"toggleRowMenu('{safe_id}')"),
                Div(
                    A(t("btn.edit"), href=f"/inventory/{entity_id}", cls="row-menu-item"),
                    Button(t("btn.delete"), cls="row-menu-item row-menu-item--danger",
                           onclick=f"if(!confirm('Delete this item? This cannot be undone.'))return;"
                                   f"htmx.ajax('DELETE','/api/items/{entity_id}',"
                                   f"{{target:'#row-{safe_id}',swap:'outerHTML'}})"),
                    cls="row-menu-dropdown", id=f"menu-{safe_id}",
                ),
                cls="row-menu",
            ),
            cls="col-actions",
        )
        status_val = str(flat.get("status", "") or "").lower()
        row_cls = "data-row data-row--inactive" if status_val in INACTIVE_ITEM_STATUSES else "data-row"
        return Tr(checkbox_td, *data_cells, action_td, id=f"row-{safe_id}", cls=row_cls)

    async def _paired_display(token: str, entity_id: str, field: str, role: str = "owner", settings: dict | None = None):
        """Return a display cell TD for the pair/triple containing `field`."""
        schema, item, cat_schemas, locs = await asyncio.gather(
            api.get_item_schema(token), api.get_item(token, entity_id),
            api.get_all_category_schemas(token), api.get_locations(token),
        )
        schema = _apply_amount_edit_permission(schema, role, settings or {})
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
            primary_editable=(pri_def or {}).get("editable", True),
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
        if field in AMOUNT_ITEM_KEYS:
            # The paired cell is a second inline-edit entry point for amount fields
            # (quantity/weight/gross_weight); gate it exactly like field_edit_cell so no
            # amount can be hand-set without edit_inventory_amounts. Restore the read-only
            # paired cell rather than an editable input.
            try:
                _pe_company = await api.get_company(token)
            except Exception:
                _pe_company = {}
            _pe_settings = _pe_company.get("settings") or {}
            if not role_has_permission(_pe_settings, _get_role(request), "edit_inventory_amounts"):
                return await _paired_display(token, entity_id, field, _get_role(request), _pe_settings)
        f_def, cell_type, options, allow_custom = _resolve_field_def(field, schema, cat_schemas, item, locations)
        # Field-specific overrides
        if field in ("sell_by", "purchase_unit", "weight_unit", "gross_weight_unit"):
            try:
                units_resp = await api.get_units(token)
                unit_names = [u["name"] for u in units_resp if u.get("name")]
                weight_unit_names = [u["name"] for u in units_resp if u.get("unit_type") == "weight"]
            except Exception:
                unit_names = []
                weight_unit_names = []
            cell_type, options, allow_custom = _apply_unit_field_override(field, cell_type, options, allow_custom, unit_names, weight_unit_names)
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
            _pd_company = await api.get_company(token)
        except Exception:
            _pd_company = {}
        try:
            return await _paired_display(token, entity_id, field, _get_role(request), _pd_company.get("settings") or {})
        except APIError as e:
            return P(f"Error: {e.detail}", cls="cell-error")

    # ── Bulk actions (list-level) ─────────────────────────────────────────────

    def _bulk_destructive_success(message: str, redirect_qs: str = "", cls: str = "flash--success") -> Response:
        """Return a bulk-action result response that clears the client-side selection.

        Sends HX-Trigger: celerpSelectionClear so the JS handler resets CelerpSelection
        and the toolbar before the table reloads.  Used for bulk actions (merge, delete,
        archive, expire, duplicate) that reload the table; `cls` selects the flash
        variant (success, or warning for a partial-success count).
        """
        from starlette.responses import HTMLResponse
        content = Div(
            P(message, cls=f"flash {cls}"),
            id="bulk-action-result",
            hx_trigger="load delay:1s",
            hx_get=f"/inventory/content{redirect_qs}",
            hx_target="#inventory-content",
            hx_swap="outerHTML",
            **({"hx_push_url": f"/inventory{redirect_qs}"} if redirect_qs else {}),
        )
        return HTMLResponse(to_xml(content), headers={"HX-Trigger": "celerpSelectionClear"})

    def _bulk_toast_error(msg: str) -> Response:
        """Surface a bulk-action error as the standard lower-right toast (no inline swap), so
        validation failures like a weight-unit mismatch read as a clear popup."""
        from starlette.responses import HTMLResponse
        return HTMLResponse(
            "", status_code=200,
            headers={"HX-Reswap": "none", **toast_header(str(msg), "error")},
        )

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

    @app.post("/api/items/bulk/shopify-sync/{action}")
    async def bulk_item_shopify_sync(request: Request, action: str):
        """Bulk enable/disable outbound Shopify sync on the selected items."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        enable = action == "enable"
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            result = await api.bulk_shopify_sync(token, entity_ids, enable)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        updated = result.get("updated", len(entity_ids))
        verb = t("connectors.enable_shopify_sync") if enable else t("connectors.disable_shopify_sync")
        return _bulk_destructive_success(f"{verb}: {updated} item(s).")

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

    # ── Bulk duplicate (create a copy of each selected item) ─────────────

    @app.post("/api/items/bulk/duplicate")
    async def bulk_item_duplicate(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        # Dedupe as well as filter: each id creates a new persistent entity, so a
        # repeated id in a crafted POST must not multiply copies of one item.
        entity_ids = list(dict.fromkeys(v.strip() for v in form.getlist("selected") if v.strip()))
        if not entity_ids:
            return Div(P(t("flash.no_items_selected"), cls="flash flash--warning"), id="bulk-action-result")
        try:
            settings = (await api.get_company(token)).get("settings") or {}
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")
        if not role_has_permission(settings, _get_role(request), "edit_inventory"):
            return Div(P(t("error.unauthorized"), cls="flash flash--error"), id="bulk-action-result")
        reserved: set[str] = set()
        ok = 0
        failed = 0
        for eid in entity_ids:
            try:
                source = await api.get_item(token, eid)
                new_sku = await _gen_copy_sku(token, str(source.get("sku", "") or ""), reserved=reserved)
                await api.create_item(token, _duplicate_payload(source, new_sku))
                ok += 1
            except APIError:
                failed += 1
        if ok == 0:
            return Div(P(f"Failed to duplicate {failed} item(s).", cls="flash flash--error"), id="bulk-action-result")
        msg = f"{ok} item(s) duplicated, {failed} failed." if failed else f"{ok} item(s) duplicated."
        return _bulk_destructive_success(msg, cls="flash--warning" if failed else "flash--success")

    # ── Bulk merge (direct — no preview modal) ───────────────────────────

    @app.post("/api/items/bulk/merge")
    async def bulk_item_merge(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        entity_ids = [v.strip() for v in form.getlist("selected") if v.strip()]
        target_sku_from = str(form.get("target_sku_from", "")).strip()
        resulting_sku = str(form.get("resulting_sku", "")).strip() or None
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
        # Attribute conflict resolution lives entirely in the merge endpoint (schema-aware: dropdowns
        # and custom attributes collapse to the "Mixed" system value, numeric fields drop). The UI
        # must not duplicate that logic.
        try:
            result = await api.merge_items(
                token,
                source_entity_ids=entity_ids,
                target_sku_from=target_sku_from,
                resulting_quantity=total_qty,
                resulting_sku=resulting_sku,
            )
        except APIError as e:
            # Surface merge failures (e.g. a weight-unit mismatch) as the standard lower-right toast.
            return _bulk_toast_error(e.detail)
        # Post-merge filter: the merged item's actual SKU - the custom one when
        # typed, else the target item's.
        target_item = next((it for it in items if it.get("entity_id") == target_sku_from or it.get("id") == target_sku_from), None)
        target_sku = resulting_sku or (target_item.get("sku", "") if target_item else "")
        redirect_qs = f"?q={target_sku}" if target_sku else ""
        return _bulk_destructive_success(t("inv.items_merged_successfully"), redirect_qs)

    async def _next_transform_sku(token: str, parent_sku: str) -> str:
        """Suggest a fresh child SKU for a TRANSFORM (a new, distinct product derived from
        the parent) as the next available ``{parent_sku}.N``. User-editable in the form.

        DEMO-RGH-001 -> DEMO-RGH-001.1, DEMO-RGH-001.2, ...
        DEMO-RGH-001.1 -> DEMO-RGH-001.1.1, DEMO-RGH-001.1.2, ...

        (Splitting does NOT use this — a split child keeps the parent SKU; see split_item.)
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

        return _split_table_form(
            preview,
            action="/api/items/bulk/split",
            target="#bulk-action-result",
            form_id="bulk-split-preview-form",
            allow_add_child=False,
            child_sku_editable=True,
            entity_id=entity_id,
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

        child_qty_raw = str(form.get("child_qty", "")).strip()
        try:
            child_qty = float(child_qty_raw)
        except (ValueError, TypeError):
            return Div(P(t("inv.invalid_split_quantity"), cls="flash flash--warning"), id="bulk-action-result")
        if child_qty <= 0:
            return Div(P(t("inv.split_quantity_must_be_greater_than_0"), cls="flash flash--warning"), id="bulk-action-result")

        try:
            item = await api.get_item(token, eid)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")

        current_qty = float(item.get("quantity", 0) or 0)
        if child_qty >= current_qty:
            return Div(P(f"Split quantity must be less than current quantity ({current_qty}).", cls="flash flash--warning"), id="bulk-action-result")

        orig_sku = str(item.get("sku", "") or "")

        # Child SKU: honour an explicit override from the preview form; otherwise omit it
        # so split_item keeps the parent SKU (a split child is the same product).
        child_sku_input = str(form.get("child_sku", "")).strip()
        child_sku = child_sku_input or orig_sku   # display value for the message / filter below

        # Optional weight/pieces overrides from preview form
        child_weight = _opt_float_field(form, "child_weight")
        child_pieces = _opt_float_field(form, "child_pieces")
        # mother_qty/mother_weight: present when user manually edited the mother parcel in the preview.
        # Pass through to API so the server uses the user's value directly rather than computing
        # parent_qty - child_qty (which would ignore the user's explicit override).
        mother_qty_override = _opt_float_field(form, "mother_qty")
        mother_weight_override = _opt_float_field(form, "mother_weight")

        if child_pieces is not None:
            parent_pieces_raw = item.get("pieces") or (item.get("attributes") or {}).get("pieces")
            parent_pieces_val = float(parent_pieces_raw) if parent_pieces_raw is not None else None
            if parent_pieces_val is not None and child_pieces >= parent_pieces_val:
                return Div(P(f"Child pieces ({int(child_pieces)}) must be less than parent pieces ({int(parent_pieces_val)}).", cls="flash flash--warning"), id="bulk-action-result")

        child: dict = {"quantity": child_qty}
        if child_sku_input:
            child["sku"] = child_sku_input
        if child_weight is not None:
            child["weight"] = child_weight
        if child_pieces is not None:
            child["attributes"] = {"pieces": child_pieces}

        split_payload: dict = {"children": [child]}
        if mother_qty_override is not None:
            split_payload["mother_qty"] = mother_qty_override
        if mother_weight_override is not None:
            split_payload["mother_weight"] = mother_weight_override

        try:
            await api.split_item(token, eid, split_payload)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--error"), id="bulk-action-result")

        from urllib.parse import quote
        remaining_qty = mother_qty_override if mother_qty_override is not None else (current_qty - child_qty)
        exact_skus = f"{quote(orig_sku)},{quote(child_sku)}"
        return _bulk_destructive_success(
            f"Split: {orig_sku} ({remaining_qty}) + {child_sku} ({child_qty}).",
            f"?skus={exact_skus}&status=all",
        )

    @app.get("/api/items/bulk/transform-preview")
    async def bulk_transform_preview(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        entity_id = request.query_params.get("entity_id", "").strip()
        if not entity_id:
            return Div()
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"))
        try:
            company = await api.get_company(token)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"))
        # The endpoint is the trust boundary; cost is only shown to a role that holds
        # view_inventory_costs. On any settings error the guard above fails closed.
        can_see_cost = role_has_permission(company.get("settings") or {}, _get_role(request), "view_inventory_costs")

        parent_qty = float(item.get("quantity") or 0)
        parent_sell_by = item.get("sell_by") or "piece"
        parent_category = item.get("category") or ""
        parent_weight_unit = item.get("weight_unit") or parent_sell_by
        parent_pieces = item.get("pieces") or (item.get("attributes") or {}).get("pieces")
        parent_cost_total = float(item.get("cost_total") or 0) or round(float(item.get("cost_price") or 0) * parent_qty, 2)

        try:
            units = await api.get_units(token)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"))
        unit_map = {u["name"]: u for u in units}
        unit_names = [u["name"] for u in units]
        weight_unit_names = [u["name"] for u in units if u.get("unit_type") == "weight"]

        # For weight-sold items, quantity IS the weight; no separate weight field
        sell_by_is_weight = is_weight_unit(parent_sell_by, unit_map)
        if sell_by_is_weight:
            parent_weight = parent_qty
            parent_weight_unit = parent_sell_by
        else:
            parent_weight = item.get("weight")

        try:
            categories = await api.list_item_categories(token)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"))

        child_sku = await _next_transform_sku(token, item.get("sku", ""))

        fmt = lambda v, d=2: f"{float(v):.{d}f}" if v is not None else ""

        unit_select = Select(
            *[Option(u, value=u, selected=(u == parent_sell_by)) for u in unit_names],
            name="child_sell_by",
            cls="form-input form-input--xs",
            onchange="transformUnitChanged(this)",
        )
        cat_select = Select(
            *[Option(c, value=c, selected=(c == parent_category)) for c in categories],
            name="child_category",
            cls="form-input form-input--sm",
            style="min-width:160px",
        )

        # Weight and pieces columns always shown (may be empty if item has no value)
        parent_name = item.get("name") or ""
        mother_cells = [
            Td("Mother", cls="sp-row-label"),
            _sp_static_td(item.get("sku", "")),
            _sp_static_td(parent_name),
            _sp_static_td(parent_category),
            _sp_static_td(f"{fmt(parent_qty)} {parent_sell_by}", num=True),
            _sp_static_td(f"{fmt(parent_weight)} {parent_weight_unit}" if parent_weight is not None else "--", num=True),
            _sp_static_td(str(int(parent_pieces)) if parent_pieces is not None else "--", num=True),
        ]
        if can_see_cost:
            mother_cells.append(_sp_static_td(fmt(parent_cost_total), num=True))

        child_qty_input = Td(
            Input(type="number", name="child_qty", value=fmt(parent_qty), step="any", min="0",
                  cls="form-input form-input--xs sp-input",
                  onchange="transformQtyChanged(this)"),
            unit_select,
            cls="sp-td sp-td--num",
        )
        # child_weight IS the outgoing weight for the transform yield delta (marker).
        child_weight_td = Td(
            Input(type="number", name="child_weight",
                  value=fmt(parent_weight) if parent_weight is not None else "",
                  step="any", cls="form-input form-input--xs sp-input",
                  data_delta_weight="1",
                  oninput="_updateSplitDelta(this.form)"),
            cls="sp-td sp-td--num tr-weight-td",
        )
        child_pieces_td = Td(
            Input(type="number", name="child_pieces",
                  value=str(int(parent_pieces)) if parent_pieces is not None else "",
                  step="1", cls="form-input form-input--xs sp-input"),
            cls="sp-td sp-td--num tr-pieces-td",
        )

        child_cells = [
            Td("Child", cls="sp-row-label"),
            Td(Input(type="text", name="child_sku", value=child_sku, cls="form-input sp-sku-input"), cls="sp-td"),
            Td(Input(type="text", name="child_name", value=parent_name, cls="form-input sp-sku-input"), cls="sp-td"),
            Td(cat_select, cls="sp-td"),
            child_qty_input,
            child_weight_td,
            child_pieces_td,
        ]
        if can_see_cost:
            child_cells.append(
                Td(
                    Input(type="number", name="child_cost_total", value=fmt(parent_cost_total), step="0.01",
                          cls="form-input form-input--xs sp-input",
                          oninput="transformCostManualEdit(this)"),
                    cls="sp-td sp-td--num",
                )
            )

        # Numeric/currency headers right-align over their figures (HTML/CSS rule 4a); text stays left.
        headers = [
            Th(""), Th("SKU", cls="sp-th"), Th("Name", cls="sp-th"), Th("Category", cls="sp-th"),
            Th("Qty + Unit", cls="sp-th sp-th--num"), Th("Weight", cls="sp-th sp-th--num"),
            Th("Pieces", cls="sp-th sp-th--num"),
        ]
        if can_see_cost:
            headers.append(Th("Cost Total", cls="sp-th sp-th--num"))

        # Live delta badge next to Confirm: processing yield = mother weight - child weight.
        # A pieces-sold parent with no weight carries no reconciliation and shows no badge.
        _tr_delta = parent_weight is not None
        _tr_delta_unit = f" {parent_weight_unit}" if parent_weight_unit else ""

        form_attrs = {
            "data-parent-cost-total": str(parent_cost_total),
            "data-weight-units": ",".join(weight_unit_names),
            "data-parent-qty": str(parent_qty),
        }
        if _tr_delta:
            # parent_weight is always numeric when the badge shows, so the delta reads it
            # straight from data-parent-weight (no data-sell-by-weight fallback needed here).
            form_attrs["data-parent-weight"] = str(parent_weight)
            form_attrs["data-weight-decimals"] = "2"

        return Form(
            Input(type="hidden", name="entity_id", value=entity_id),
            Table(
                Thead(Tr(*headers)),
                Tbody(Tr(*mother_cells), Tr(*child_cells)),
                cls="split-preview-table",
            ),
            Div(
                Button("Confirm", type="submit", cls="btn btn--primary btn--sm sp-confirm-btn"),
                *(
                    [Span(
                        Span("Δ ", cls="sp-delta-label"),
                        Span("", cls="sp-delta-val"),
                        Span(_tr_delta_unit, cls="sp-delta-unit"),
                        cls="sp-delta sp-delta-badge",
                        title=t("inv.split_delta_tooltip"),
                    )]
                    if _tr_delta else []
                ),
                cls="sp-confirm-row",
            ),
            *(
                [Script("_updateSplitDelta(document.getElementById('bulk-transform-preview-form'));")]
                if _tr_delta else []
            ),
            hx_post="/api/items/bulk/transform",
            hx_target="#bulk-action-result",
            hx_swap="outerHTML",
            id="bulk-transform-preview-form",
            **form_attrs,
        )

    @app.post("/api/items/bulk/transform")
    async def bulk_item_transform(request: Request):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        form = await request.form()
        entity_id = str(form.get("entity_id", "")).strip()
        if not entity_id:
            return Div(P("No item selected.", cls="flash flash--warning"), id="bulk-action-result")

        cost_raw = form.get("child_cost_total")
        cost_present = cost_raw is not None and str(cost_raw).strip() != ""
        try:
            child_qty = float(str(form.get("child_qty", "0")).strip())
            child_cost_total = float(str(cost_raw).strip()) if cost_present else None
        except ValueError:
            return Div(P("Invalid numeric input.", cls="flash flash--warning"), id="bulk-action-result")

        child_weight_raw = str(form.get("child_weight", "")).strip()
        child_pieces_raw = str(form.get("child_pieces", "")).strip()

        payload = {
            "child_sku": str(form.get("child_sku", "")).strip(),
            "child_name": str(form.get("child_name", "")).strip() or None,
            "child_category": str(form.get("child_category", "")).strip(),
            "child_sell_by": str(form.get("child_sell_by", "")).strip(),
            "child_quantity": child_qty,
            "child_weight": float(child_weight_raw) if child_weight_raw else None,
            "child_pieces": int(child_pieces_raw) if child_pieces_raw else None,
            "child_cost_total": child_cost_total,
        }

        try:
            result = await api.transform_item(token, entity_id, payload)
        except APIError as e:
            return Div(P(str(e.detail), cls="flash flash--warning"), id="bulk-action-result")

        child_sku = result.get("child_sku", "")
        parent_sku = result.get("parent_sku", "")
        from urllib.parse import quote
        exact_skus = f"{quote(parent_sku)},{quote(child_sku)}"
        return _bulk_destructive_success(
            f"Transformed: {parent_sku} → {child_sku}",
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
            elif doc_type in ("list", "shipping_doc"):
                params = {"limit": 20}
                # A Shipping Document target only offers shipping-doc drafts; the generic
                # List/Quotation target keeps offering every list type, as before.
                if doc_type == "shipping_doc":
                    params["list_type"] = "shipping_doc"
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
            elif doc_type == "purchase_order":
                params = {"doc_type": "purchase_order", "limit": 20}
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
        from ui.routes.documents import _line_items_from_inventory, _reorder_lines_from_inventory
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
                elif doc_type in ("list", "shipping_doc"):
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
                elif doc_type == "purchase_order":
                    new_lines = await _reorder_lines_from_inventory(token, entity_ids)
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
                elif doc_type in ("list", "shipping_doc"):
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    _lt = "shipping_doc" if doc_type == "shipping_doc" else "quotation"
                    result = await api.create_list(token, {"list_type": _lt, "status": "draft", "line_items": line_items})
                    list_id = result.get("entity_id") or result.get("id", "")
                    return Response("", status_code=204, headers={"HX-Redirect": f"/lists/{list_id}"})
                elif doc_type == "memo":
                    line_items = await _line_items_from_inventory(token, entity_ids)
                    from ui.routes.documents import _company_doc_taxes
                    doc_taxes = await _company_doc_taxes(token)
                    result = await api.create_doc(token, {"doc_type": "memo", "status": "draft", "line_items": line_items, "doc_taxes": doc_taxes})
                    doc_id = result.get("entity_id") or result.get("id", "")
                    return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{doc_id}"})
                elif doc_type == "purchase_order":
                    # Purchase-side: qty = reorder_qty in purchase units (blank when unset),
                    # cost price, purchase unit, no sales taxes. Group the selection by each
                    # item's preferred supplier so a multi-supplier reorder becomes one draft
                    # PO per supplier (plus one for items with no supplier set).
                    from celerp.services.reorder import group_by_supplier
                    items = []
                    for eid in entity_ids:
                        try:
                            it = await api.get_item(token, eid)
                        except APIError:
                            it = {}
                        items.append({"entity_id": eid, "preferred_supplier": it.get("preferred_supplier")})
                    groups = group_by_supplier(items)
                    created: list[tuple[str, str, str]] = []
                    for sup, ids in groups.items():
                        line_items = await _reorder_lines_from_inventory(token, ids)
                        payload = {"doc_type": "purchase_order", "status": "draft",
                                   "purchase_kind": "inventory", "line_items": line_items}
                        if sup:
                            payload["contact_id"] = sup
                        result = await api.create_doc(token, payload)
                        did = result.get("entity_id") or result.get("id", "")
                        ref = result.get("ref_id") or result.get("doc_number") or did.split(":")[-1][:8]
                        created.append((sup, did, ref))
                    if len(created) == 1:
                        return Response("", status_code=204, headers={"HX-Redirect": f"/docs/{created[0][1]}"})
                    # More than one supplier: show each draft created (no silent multi-create),
                    # labelled by supplier and draft reference to match the document listing.
                    return Div(
                        P(t("inv.pos_created", n=len(created)), cls="flash flash--success"),
                        *[P(A(f"{sup or t('inv.no_supplier')}: {ref}",
                              href=f"/docs/{did}", cls="link")) for sup, did, ref in created],
                        id="bulk-action-result",
                    )
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
            price_lists = PRICE_LISTS_FALLBACK
        try:
            # Fetch item once to get qty (needed for cost_price → cost_total conversion)
            item_for_price = await api.get_item(token, entity_id)
            item_qty = float(item_for_price.get("quantity") or 0)
            cost_changed = False
            for pl in price_lists:
                pl_name = pl.get("name", "")
                conventional_key = price_key(pl_name)
                if conventional_key not in form:
                    continue  # field not submitted — leave it untouched
                val = str(form.get(conventional_key, "")).strip()
                if val == "":
                    # Cleared price → unset it (issue #202). Cost is canonically cost_total. Use
                    # patch_item with new=None so the field is removed, not stored as "" / 0.
                    if conventional_key == "cost_price":
                        await api.patch_item(token, entity_id, {"cost_total": {"old": item_for_price.get("cost_total"), "new": None}})
                        cost_changed = True
                    else:
                        await api.patch_item(token, entity_id, {conventional_key: {"old": item_for_price.get(conventional_key), "new": None}})
                    continue
                try:
                    price = float(val)
                except ValueError:
                    continue
                # cost_price is a derived field; always save as cost_total to avoid being
                # overwritten by flatten_item on the next read.
                # Use patch_item (not set_item_price) so price_type normalization doesn't mangle "cost_total".
                if conventional_key == "cost_price" and item_qty > 0:
                    old_cost_total = item_for_price.get("cost_total")
                    await api.patch_item(token, entity_id, {"cost_total": {"old": old_cost_total, "new": round(price * item_qty, 10)}})
                    cost_changed = True
                else:
                    await api.set_item_price(token, entity_id, pl_name, price)
                    if conventional_key == "cost_price":
                        cost_changed = True
            # A cost change ripples into every recipe that uses this item — recost them now so
            # the Manufacturing tab and margins stay current without any manual step.
            if cost_changed:
                try:
                    await api.recost_dependents(token, entity_id)
                except APIError:
                    pass  # best-effort propagation; the cost itself is already saved
        except APIError as e:
            # OOB so the message lands in the save-status (inputs use hx_swap="none").
            return Div(f"Not saved: {e.detail}", id="pricing-save-status",
                       cls="recipe-save-status hint error", hx_swap_oob="true")
        # Derived lists recompute from the base at read time; push their fresh values into
        # the open pricing form as out-of-band swaps (inputs save with hx_swap="none", so
        # the user's focus never moves and no reload is needed).
        oob_cells: list = []
        derived_lists = [pl for pl in price_lists if is_derived(pl)]
        if derived_lists:
            try:
                item_after = await api.get_item(token, entity_id)
                company_after = await api.get_company(token)
                from celerp.services.money import rate_dp as _rate_dp
                rdp = _rate_dp((company_after.get("currency") or "").strip() or "USD")
                qty_after = float(item_after.get("quantity") or 0)
                for pl in derived_lists:
                    name = pl.get("name", "")
                    unit, total = _readonly_price_cells(
                        price_key(name), resolve_price(item_after, name),
                        qty_after, qty_after > 0, rdp, oob=True,
                    )
                    oob_cells.extend([unit, total])
            except APIError:
                pass  # refresh is cosmetic; the save itself already succeeded
        # Autosave: a transient saved indicator, no page reload (consistent with the rest of the UI).
        return (Div("Saved ✓", id="pricing-save-status", cls="recipe-save-status hint saved", hx_swap_oob="true"),
                *oob_cells)

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
            # Simple format: comma-separated quantities (children keep the parent SKU)
            parts_raw = str(form.get("parts", "")).strip()
            if parts_raw:
                try:
                    quantities = [float(p.strip()) for p in parts_raw.split(",") if p.strip()]
                except ValueError:
                    return Div(Span(t("inv.invalid_quantities_use_commaseparated_numbers"), cls="flash flash--error"), id="item-action-error")
                if len(quantities) < 1:
                    return Div(Span(t("inv.enter_at_least_one_split_quantity"), cls="flash flash--error"), id="item-action-error")
                # All quantities become new child items; the parent's quantity is reduced by
                # the total. Split children keep the parent SKU (same product), so send only
                # the qty and let split_item default the SKU.
                children = [{"quantity": qty} for qty in quantities]
            else:
                return Div(Span(t("inv.enter_commaseparated_quantities_eg_321"), cls="flash flash--error"), id="item-action-error")
        if len(children) < 1:
            return Div(Span(t("inv.enter_at_least_one_split_quantity"), cls="flash flash--error"), id="item-action-error")
        try:
            await api.split_item(token, entity_id, {"children": children})
        except APIError as e:
            return Div(Span(str(e.detail), cls="flash flash--error"), id="item-action-error")
        redirect = f"/inventory?q={quote(orig_sku)}" if orig_sku else f"/inventory/{entity_id}"
        return Response("", status_code=204, headers={"HX-Redirect": redirect})

    @app.post("/api/items/{entity_id}/split-inline")
    async def item_split_inline(request: Request, entity_id: str):
        """Detail page split: one or more children (each keeps the parent SKU), redirect to exact filtered inventory."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        qty_raws = [v.strip() for v in form.getlist("child_qty") if v.strip()]
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
        sell_by = str(item.get("sell_by") or "piece")
        from celerp.services.units import DEFAULT_UNITS
        _default_umap = {u["name"]: u for u in DEFAULT_UNITS}
        # Optional complement fields (one value per row; may be empty)
        comp_weights  = form.getlist("child_weight")
        comp_pieces_l = form.getlist("child_pieces")
        # Split children keep the parent SKU (same product; a distinct lot by barcode /
        # entity_id) — omit the SKU and let split_item resolve it to the parent's.
        children: list[dict] = []
        for idx, qty in enumerate(children_qtys):
            child: dict = {"quantity": qty}
            # Attach complement if provided for this row
            if is_pieces_unit(sell_by, _default_umap) and idx < len(comp_weights):
                raw_w = (comp_weights[idx] or "").strip()
                if raw_w:
                    try:
                        _apply_complement(child, sell_by, float(raw_w), _default_umap)
                    except (ValueError, TypeError):
                        pass
            elif is_weight_unit(sell_by, _default_umap) and idx < len(comp_pieces_l):
                raw_p = (comp_pieces_l[idx] or "").strip()
                if raw_p:
                    try:
                        _apply_complement(child, sell_by, float(raw_p), _default_umap)
                    except (ValueError, TypeError):
                        pass
            children.append(child)
        # mother_qty/mother_weight: present when the user edited the mother parcel directly.
        # Forward so the server uses the explicit value rather than deriving parent - children.
        split_payload: dict = {"children": children}
        mother_qty_override = _opt_float_field(form, "mother_qty")
        mother_weight_override = _opt_float_field(form, "mother_weight")
        if mother_qty_override is not None:
            split_payload["mother_qty"] = mother_qty_override
        if mother_weight_override is not None:
            split_payload["mother_weight"] = mother_weight_override
        try:
            await api.split_item(token, entity_id, split_payload)
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error", id="item-action-error")
        return _split_redirect(orig_sku, [])   # children share the parent SKU

    def _split_redirect(orig_sku: str, child_skus: list[str]) -> Response:
        """Build the HX-Redirect response after a successful split."""
        from urllib.parse import quote as _quote
        skus_param = ",".join(_quote(s) for s in [orig_sku] + child_skus)
        redirect = f"/inventory?skus={skus_param}&status=all" if orig_sku else "/inventory"
        return Response("", status_code=204, headers={"HX-Redirect": redirect})

    def _apply_complement(child: dict, sell_by: str, complement: float, unit_map: dict) -> None:
        """Attach the complement field to a split child dict based on the item's sell_by unit.

        weight-unit items carry pieces as the complement; piece-unit items carry weight.
        Mutates child in place.
        """
        if is_weight_unit(sell_by, unit_map):
            child["pieces"] = int(round(complement))
        elif is_pieces_unit(sell_by, unit_map):
            child["weight"] = complement

    @app.post("/api/items/{entity_id}/batch-split")
    async def item_batch_split(request: Request, entity_id: str):
        """Batch split: N identical children of the same qty (each keeps the parent SKU), redirect to filtered inventory."""
        token = _token(request)
        if not token:
            return Response("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        raw_qty   = str(form.get("batch_qty",   "")).strip()
        raw_count = str(form.get("batch_count", "")).strip()
        try:
            batch_qty = float(raw_qty)
        except (ValueError, TypeError):
            return Span(t("inv.invalid_split_quantity"), cls="flash flash--error", id="item-action-error")
        try:
            batch_count = int(raw_count)
        except (ValueError, TypeError):
            return Span("Count must be a whole number.", cls="flash flash--error", id="item-action-error")
        if batch_qty <= 0:
            return Span(t("inv.split_quantity_must_be_greater_than_0"), cls="flash flash--error", id="item-action-error")
        if batch_count < 2:
            return Span("Count must be at least 2.", cls="flash flash--error", id="item-action-error")
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error", id="item-action-error")
        available = float(item.get("quantity") or 0)
        if batch_qty * batch_count > available:
            return Span(
                f"Total ({batch_qty * batch_count}) exceeds available ({available}).",
                cls="flash flash--error", id="item-action-error",
            )
        orig_sku = str(item.get("sku", "") or "")
        # Optional complement field (weight when sell_by is pieces, pieces when sell_by is weight)
        raw_comp = str(form.get("batch_complement", "")).strip()
        complement: float | None = None
        if raw_comp:
            try:
                complement = float(raw_comp)
            except (ValueError, TypeError):
                pass
        children: list[dict] = []
        sell_by = item.get("sell_by") or "piece"
        from celerp.services.units import DEFAULT_UNITS
        _default_umap = {u["name"]: u for u in DEFAULT_UNITS}
        for _ in range(batch_count):
            child: dict = {"quantity": batch_qty}   # keeps the parent SKU (split_item default)
            if complement is not None:
                _apply_complement(child, sell_by, complement, _default_umap)
            children.append(child)
        try:
            await api.split_item(token, entity_id, {"children": children})
        except APIError as e:
            return Span(str(e.detail), cls="flash flash--error", id="item-action-error")
        return _split_redirect(orig_sku, [])   # children share the parent SKU

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
        raw_cost = str(form.get("resulting_cost_total", "")).strip()
        resulting_name = str(form.get("resulting_name", "")).strip() or None
        resulting_sku = str(form.get("resulting_sku", "")).strip() or None
        try:
            resulting_quantity = float(raw_qty) if raw_qty else None
        except ValueError:
            return Span(t("error.invalid_resulting_quantity"), cls="flash flash--error")
        try:
            resulting_cost_total = float(raw_cost) if raw_cost else None
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
                resulting_cost_total=resulting_cost_total,
                resulting_name=resulting_name,
                resulting_sku=resulting_sku,
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
            new_sku = await _gen_copy_sku(token, str(source.get("sku", "") or ""))
        payload = _duplicate_payload(source, new_sku)
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
        return _item_files_section(entity_id, item, show_preview=_item_files_preview(request))

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
        return _item_files_section(entity_id, item, show_preview=_item_files_preview(request))

    @app.post("/items/{entity_id}/files/{file_id}/description")
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
        return _item_files_section(entity_id, item, show_preview=_item_files_preview(request))

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
        return _item_files_section(entity_id, item, show_preview=_item_files_preview(request))

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
        return _item_files_section(entity_id, item, show_preview=_item_files_preview(request))

    @app.get("/items/{entity_id}/files/_section")
    async def item_files_section_partial(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return Response("", status_code=401)
        try:
            item = await api.get_item(token, entity_id)
        except APIError as e:
            return P(str(e.detail), cls="cell-error")
        qp = request.query_params
        return _item_files_section(
            entity_id, item, show_preview=_item_files_preview(request),
            tag_filter=qp.get("tag_filter", ""), search=qp.get("search", ""),
            sort_dir=qp.get("sort_dir", "desc"), page=int(qp.get("page") or 1),
            date_from=qp.get("date_from", ""), date_to=qp.get("date_to", ""),
        )

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


def _bulk_toolbar(locations: list[dict], p: dict | None = None, total_items: int = 0,
                  settings: dict | None = None, role: str = "owner") -> FT:
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
    visible_bulk_actions = [
        action for action in get_slot("bulk_action")
        if not action.get("permission")
        or role_has_permission(settings or {}, role, action["permission"])
    ]
    module_action_opts = []
    for action in visible_bulk_actions:
        action_id = action["form_action"].replace("/", "_").strip("_")
        module_action_opts.append(
            Option(action.get("label", "Action"), value=f"mod:{action_id}")
        )

    # Build the main Action dropdown options
    action_options = [
        Option(t("inv.action"), value="", disabled=True, selected=True),
        Option(t("btn.transfer"), value="transfer"),
        Option(t("inv.split"), value="split"),
        Option(t("inv.transform"), value="transform"),
        Option(t("inv.merge"), value="merge"),
    ]
    if send_to_opts:
        action_options.append(Option(t("inv.send_to"), value="send_to"))
    action_options.extend(module_action_opts)
    action_options.append(Option(t("inv.archive"), value="archive"))
    action_options.append(Option(t("inv.expire"), value="expire"))
    if role_has_permission(settings or {}, role, "edit_inventory"):
        action_options.append(Option(t("inv.duplicate"), value="duplicate"))
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
        _bulk_context_templates(loc_opts, _loc_opt, _loc_js, send_to_opts, visible_bulk_actions, p or {}, total_items),
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

    # Transform: auto-loads preview on action select
    transform_tpl = Template(
        Div(
            Div(id="bulk-transform-preview"),
            id="bulk-transform-form",
        ),
        id="tpl-transform",
    )

    # Merge: the target dropdown lists the selected items plus a final "New SKU"
    # option. Picking an item merges into it (its SKU wins, nothing extra shown);
    # picking "New SKU" reveals [dropdown] -> [enter SKU] on the same line and the
    # merge bases on the first selected item. JS moves the option to the end after
    # populating the item choices.
    merge_tpl = Template(
        Div(
            Div(
                Select(
                    Option(t("inv.select_target_item"), value="", disabled=True, selected=True),
                    Option(t("inv.new_sku"), value="__new__", id="merge-new-sku-opt"),
                    id="merge-target-select",
                    name="target_sku_from", cls="form-input form-input--sm",
                ),
                Span("←" if is_rtl() else "→", id="merge-sku-arrow", style="display:none"),
                Input(
                    type="text", id="merge-resulting-sku", cls="form-input form-input--sm",
                    placeholder=t("inv.enter_sku"), title=t("inv.enter_sku"), style="display:none",
                ),
                style="display:flex;align-items:center;gap:0.5rem",
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
        transform_tpl,
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
    # Strip skus/q: clicking a status card is a catalog navigation action and should
    # clear any transient item-specific filters (post-split/merge result views etc.)
    base_state = {k: v for k, v in _base_state(p or {}).items() if k not in ("status", "skus", "q")}
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


def _category_tabs(category_counts: dict, p: dict, total_scoped: int | None = None,
                   label_map: dict | None = None) -> FT:
    if not category_counts and not total_scoped:
        return ""
    label_map = label_map or {}

    # Strip skus/q: clicking a category tab is a catalog navigation action and should
    # clear any transient item-specific filters (post-split/merge result views etc.)
    base = {k: v for k, v in _base_state(p).items() if k not in ("category", "skus", "q")}

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
        # cat is the schema key (a slug); show its human display label, keep the slug as the filter value.
        tabs.append(_tab(f"{label_map.get(cat, cat)} ({count})", cat, p.get("category") == cat))
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


# Human labels for the inventory_type slugs. "Inventory" is spelled out on stocked/non-stocked
# so they aren't confused with the Component type (added with the manufacturing module).
_INVENTORY_TYPE_LABELS: dict[str, str] = {
    "stocked": "Stocked Inventory",
    "component": "Component",
    "service": "Service",
    "non_stocked": "Non-Stocked Inventory",
    "freight": "Freight",
}


def _inventory_type_tabs(p: dict) -> FT:
    """Filter tabs for inventory_type (all / stocked / component / service / non_stocked)."""
    active = p.get("inventory_type", "")
    _TABS = [("", "All Types")] + [(k, _INVENTORY_TYPE_LABELS[k])
                                   for k in ("stocked", "component", "service", "non_stocked", "freight")]
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
_PAIRED_TABLE: dict[str, str] = {"quantity": "sell_by", "weight": "weight_unit", "gross_weight": "gross_weight_unit", "purchase_unit": "purchase_conversion_factor"}
# Derived from _PAIRED_TABLE — secondary fields already rendered inside paired cells; exclude from standalone rows
_PAIRED_SECONDARY_KEYS: frozenset[str] = frozenset(_PAIRED_TABLE.values())
# Core item fields shown in the left (core details) panel on the detail page — single definition
_ITEM_CORE_KEYS: frozenset[str] = frozenset({
    "sku", "name", "status", "category", "quantity", "pieces", "weight", "weight_unit",
    "gross_weight", "gross_weight_unit", "sell_by", "allow_splitting", "barcode",
    "batch_no", "hs_code", "location_name", "short_description", "reorder_point",
    "reorder_qty", "purchase_sku", "purchase_name",
    "purchase_unit", "purchase_conversion_factor",
})


def _label_price_cols(schema: list[dict]) -> list[dict]:
    """Return schema with '(Unit Price)' appended to price column labels at render time.
    Virtual columns (e.g. cost_price_total) already have their final label set.
    Does not mutate the input list."""
    result = []
    for f in schema:
        if f.get("type") == "money" and not f.get("virtual"):
            result.append({**f, "label": f"{f.get('label', f['key'])} (Unit Price)"})
        else:
            result.append(f)
    return result


def _render_virtual_total_cell(entity_id: str, field: str, unit_price: float | None, qty: float | None, currency: str | None) -> FT:
    """Render a display Td for a virtual total column (unit_price * qty)."""
    from ui.components.table import fmt_money
    try:
        total = float(unit_price or 0) * float(qty or 0)
        formatted = fmt_money(total, currency) if total != 0 else "--"
    except (ValueError, TypeError):
        formatted = "--"
    inner = Span(formatted, cls="cell-money") if formatted != "--" else Span("--")
    safe_eid = entity_id.replace(":", "-")
    return Td(
        inner,
        id=f"cell-{safe_eid}-{field}",
        cls="cell cell--money cell--clickable",
        data_col=field,
        hx_get=f"/api/items/{entity_id}/field/{field}/edit",
        hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
    )


def _inventory_cell_renderers(schema: list[dict], unit_names: list[str] | None = None, units_map: dict[str, dict] | None = None, category_label_map: dict | None = None, currency: str | None = None) -> dict:
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
    weight_unit_opts = [n for n, u in _umap.items() if u.get("unit_type") == "weight"] or None
    paired_options: dict[str, list[str] | None] = {
        "sell_by": sell_by_opts,
        "weight_unit": weight_unit_opts,
        "gross_weight_unit": weight_unit_opts,
    }
    for primary, secondary in _PAIRED_TABLE.items():
        if primary in schema_keys and secondary in schema_keys:
            pri_def = next((f for f in schema if f["key"] == primary), {})
            sec_def = next((f for f in schema if f["key"] == secondary), {})
            sec_opts = paired_options.get(secondary)
            sec_type = "select" if sec_opts else sec_def.get("type", "text")
            def _make(pri=primary, sec=secondary, pt=pri_def.get("type", "number"),
                      st=sec_type, po=pri_def.get("options"), so=sec_opts, _um=_umap,
                      ped=pri_def.get("editable", True)):
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
                        primary_editable=ped,
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
        _weight_editable = next((f.get("editable", True) for f in schema if f.get("key") == "weight"), True)
        def _weight_renderer(entity_id: str, row: dict, _umap=_umap, _paired=_weight_paired, _ed=_weight_editable) -> FT:
            sell_by = row.get("sell_by") or ""
            _safe_id = entity_id.replace(":", "-")
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
                    id=f"cell-{_safe_id}-weight",
                    cls="cell cell--number",
                    data_col="weight",
                    data_decimals=str(decimals),
                )
            # Not a weight unit - use paired cell (weight + weight_unit) or plain editable
            if _paired:
                td = _paired(entity_id, row)
                td.attrs["id"] = f"cell-{_safe_id}-weight"
                return td
            return display_cell(entity_id=entity_id, field="weight", value=row.get("weight", ""), cell_type="number", editable=_ed)
        renderers["weight"] = _weight_renderer

    # pieces: derived from qty when sell_by is a pieces unit
    if "pieces" in schema_keys:
        _pieces_editable = next((f.get("editable", True) for f in schema if f.get("key") == "pieces"), True)
        def _pieces_renderer(entity_id: str, row: dict, _umap=_umap, _ed=_pieces_editable) -> FT:
            sell_by = row.get("sell_by") or ""
            _safe_id = entity_id.replace(":", "-")
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
                    id=f"cell-{_safe_id}-pieces",
                    cls="cell cell--number",
                    data_col="pieces",
                    data_decimals=str(decimals),
                )
            return display_cell(entity_id=entity_id, field="pieces", value=row.get("pieces", ""), cell_type="number", editable=_ed)
        renderers["pieces"] = _pieces_renderer

    # Status renderer: a doc-driven status (sold, memo_out, consigned-in stock) carries
    # the causing document on the item state; the badge reads STATUS: DOC-NUMBER with
    # the number linked to the doc, so list rows are traceable and filterable by doc.
    if "status" in schema_keys:
        _status_def = next(f for f in schema if f["key"] == "status")
        def _status_renderer(entity_id: str, row: dict, _f=_status_def) -> FT:
            doc_id = str(row.get("status_doc_id") or "")
            doc_num = str(row.get("status_doc_number") or "") or doc_id.removeprefix("doc:")
            return display_cell(
                entity_id=entity_id, field="status", value=row.get("status", ""),
                cell_type=_f.get("type", "status"), options=_f.get("options"),
                editable=_f.get("editable", True),
                status_doc=(doc_id, doc_num) if doc_id else None,
            )
        renderers["status"] = _status_renderer

    # Category renderer: shows display name instead of slug
    if category_label_map:
        _clm = category_label_map
        def _cat_renderer(entity_id: str, row: dict, _lm=_clm) -> FT:
            return display_cell(entity_id=entity_id, field="category", value=row.get("category", ""),
                                cell_type="select", editable=True, label_map=_lm)
        renderers["category"] = _cat_renderer

    # Price column renderers: show currency symbol + "/ sell_unit" annotation
    from ui.components.table import fmt_money
    price_keys = [f["key"] for f in schema if f.get("type") == "money" and not f.get("virtual")]
    virtual_total_fields = {f["key"]: f for f in schema if f.get("virtual") and f.get("type") == "money"}
    _cur = currency
    for pk in price_keys:
        def _make_price_renderer(field=pk, _currency=_cur):
            def renderer(entity_id: str, row: dict) -> FT:
                sell_by = (row.get("sell_by") or "").strip()
                val = row.get(field, "")
                # Render formatted money value with currency symbol
                try:
                    formatted = fmt_money(val, _currency) if val not in (None, "", "--") else "--"
                except (ValueError, TypeError):
                    formatted = "--"
                annotation = Span(f"/ {sell_by}", cls="cell-price-unit") if sell_by else ""
                inner = Span(formatted, cls="cell-money") if formatted != "--" else Span("--")
                _safe_eid = entity_id.replace(":", "-")
                return Td(
                    inner, annotation,
                    id=f"cell-{_safe_eid}-{field}",
                    cls="cell cell--money cell--clickable",
                    data_col=field,
                    hx_get=f"/api/items/{entity_id}/field/{field}/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                )
            return renderer
        renderers[pk] = _make_price_renderer()

    # Virtual total column renderers
    for vk, vf in virtual_total_fields.items():
        paired = vf.get("paired_with", "")
        def _make_total_renderer(total_field=vk, unit_field=paired, _currency=_cur):
            def renderer(entity_id: str, row: dict) -> FT:
                try:
                    if total_field == "cost_price_total" and row.get("cost_total") is not None:
                        # cost_total is the primitive; display it directly
                        total = float(row["cost_total"])
                    else:
                        unit_price = float(row.get(unit_field) or 0)
                        qty = float(row.get("quantity") or 0)
                        total = unit_price * qty
                    formatted = fmt_money(total, _currency) if total != 0 else "--"
                except (ValueError, TypeError):
                    formatted = "--"
                inner = Span(formatted, cls="cell-money") if formatted != "--" else Span("--")
                _safe_eid = entity_id.replace(":", "-")
                return Td(
                    inner,
                    id=f"cell-{_safe_eid}-{total_field}",
                    cls="cell cell--money cell--clickable",
                    data_col=total_field,
                    hx_get=f"/api/items/{entity_id}/field/{total_field}/edit",
                    hx_target="this", hx_swap="outerHTML", hx_trigger="dblclick",
                )
            return renderer
        renderers[vk] = _make_total_renderer()

    # Reorder fields: show the velocity suggestion as a grey placeholder when empty.
    # The hint string is injected onto the row (`_<field>_hint`) only on the single-item
    # detail view; elsewhere (list/edit re-render) it is absent, so the cell renders as a
    # plain number cell - byte-identical to the default. The suggestion is display-only.
    for _rk in ("reorder_point", "reorder_qty"):
        if _rk in schema_keys:
            def _make_reorder(rk=_rk):
                def renderer(entity_id: str, row: dict) -> FT:
                    return display_cell(
                        entity_id=entity_id, field=rk, value=row.get(rk, ""),
                        cell_type="number", placeholder=row.get(f"_{rk}_hint"),
                    )
                return renderer
            renderers[_rk] = _make_reorder()

    return renderers


async def _inject_reorder_hints(token: str, item: dict) -> None:
    """Attach velocity-suggestion hint strings for EMPTY reorder fields, so the item
    detail can show them as a grey placeholder. Mutates `item` in place; best-effort
    (never raises). Only fetched when a field is actually unset (no wasted calls)."""
    need = [k for k in ("reorder_point", "reorder_qty")
            if str(item.get(k) or "").strip() in ("", "0")]
    if not need:
        return
    try:
        sugg = await api.get_reorder_suggestion(token, item.get("entity_id") or item.get("id") or "")
    except Exception:
        return
    for k in need:
        v = sugg.get(k)
        if v not in (None, ""):
            item[f"_{k}_hint"] = str(v)


async def _reorder_placeholder(token: str, entity_id: str, field: str, value) -> str | None:
    """Grey-placeholder string for a single reorder cell: the velocity suggestion when the
    field is empty/zero, else None. Used by the edit/display/patch re-render endpoints so the
    suggestion stays visible instantly after Escape or an empty save. Never raises."""
    if field not in ("reorder_point", "reorder_qty") or str(value or "").strip() not in ("", "0"):
        return None
    try:
        sv = (await api.get_reorder_suggestion(token, entity_id)).get(field)
    except Exception:
        return None
    return str(sv) if sv not in (None, "") else None


def _column_manager(schema: list[dict], p: dict, active_cat: str = "", visible_cols: list[str] | None = None, keep_open: bool = False) -> FT:
    """Column manager dropdown with immediate JS toggle + localStorage + drag-and-drop reorder.

    Server-side pref save is preserved for cross-device sync (background fetch).
    Client-side: checkboxes immediately show/hide columns and persist to localStorage.
    """
    import json as _json
    selected = set(visible_cols) if visible_cols else {f.get("key") for f in schema if f.get("show_in_table", True)}
    cat_pref = active_cat or "__all__"
    # JS data for all columns (key, label, visible) — exclude paired secondaries and virtual columns
    _cm_exclude = _PAIRED_SECONDARY_KEYS | {f.get("key") for f in schema if f.get("virtual")}
    col_data = [{"key": f.get("key", ""), "label": f.get("label", f.get("key", ""))} for f in schema if f.get("key") not in _cm_exclude]
    col_data_js = _json.dumps(col_data)
    # Map: primary_key → [virtual_key, ...] so applyOrderToTable can drag virtual cols alongside primary.
    # Virtual total always follows its paired unit-price column (the primary).
    _vf_map: dict[str, list[str]] = {}
    for f in schema:
        if f.get("virtual") and f.get("paired_with"):
            key, paired = f["key"], f["paired_with"]
            _vf_map.setdefault(paired, []).append(key)
    virtual_followers_js = _json.dumps(_vf_map)
    selected_js = _json.dumps(sorted(selected))
    # Hidden inputs for fallback server save (category, status, sort etc.)
    hidden_state = {k: v for k, v in _base_state(p).items() if k != "cols"}
    hidden_state["_cat_pref"] = cat_pref

    # Build checkbox list for initial render (exclude paired secondaries - they render inside primary cells)
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
        if f.get("key") not in _cm_exclude
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
  // Virtual columns that must move with their primary (e.g. cost_price_total follows cost_price)
  var VIRTUAL_FOLLOWERS = {virtual_followers_js};
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
    // Move TH elements into order (before actions column); each primary drags its virtual followers
    order.forEach(function(key) {{
      var th = thead_tr.querySelector('th[data-key="' + key + '"]');
      if (th && actionsTh) thead_tr.insertBefore(th, actionsTh);
      // Place virtual followers immediately after their primary
      (VIRTUAL_FOLLOWERS[key] || []).forEach(function(vk) {{
        var vth = thead_tr.querySelector('th[data-key="' + vk + '"]');
        if (vth && actionsTh) thead_tr.insertBefore(vth, actionsTh);
      }});
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
    // Always keep reset button and custom-col link at the bottom
    var resetBtn = menu.querySelector('#col-mgr-reset');
    if (resetBtn) parent.appendChild(resetBtn);
    var customLink = menu.querySelector('#col-mgr-custom-link');
    if (customLink) parent.appendChild(customLink);
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
                "Reset column settings",
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
            A(
                ("+ Add custom column" if active_cat else "+ Manage category columns"),
                href=(
                    f"/settings/inventory?tab=category-library&cat={active_cat}"
                    if active_cat
                    else "/settings/inventory?tab=category-library"
                ),
                id="col-mgr-custom-link",
                cls="col-mgr-custom-link",
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



_UNIVERSAL_FIELD_OPTIONS: dict[str, list[str]] = {
    "inventory_type": ["stocked", "component", "non_stocked", "service", "freight"],
}


def _apply_unit_field_override(
    field: str, cell_type: str, options, allow_custom: bool, unit_names: list[str],
    weight_unit_names: list[str] | None = None,
) -> tuple[str, list | None, bool]:
    """Return (cell_type, options, allow_custom) with unit-field overrides applied.
    sell_by and purchase_unit become searchable selects populated from company units.
    weight_unit becomes a searchable select filtered to weight-type units only.
    """
    if field in ("sell_by", "purchase_unit"):
        return (
            "select",
            [*unit_names, ("__new__:/settings/inventory?tab=units", "+ Add new unit")],
            True,
        )
    if field in ("weight_unit", "gross_weight_unit"):
        opts = weight_unit_names or unit_names
        return (
            "select",
            [*opts, ("__new__:/settings/inventory?tab=units", "+ Add new unit")],
            True,
        )
    return cell_type, options, allow_custom


def _apply_amount_edit_permission(schema: list[dict], role: str, settings: dict) -> list[dict]:
    """Return *schema* with amount fields marked read-only when *role* lacks the
    edit_inventory_amounts permission. One transform at the schema source,
    mirroring apply_field_visibility for costs (celerp.services.cost_visibility):
    every cell that reads a field's 'editable' flag - the data_table default cells
    and the weight/pieces renderers alike - inherits the restriction, so no amount
    cell renders as click-to-edit without the permission. The backend edit
    endpoints enforce the same gate regardless of what the UI drew."""
    if role_has_permission(settings, role, "edit_inventory_amounts"):
        return schema
    return [{**f, "editable": False} if f.get("key") in AMOUNT_ITEM_KEYS else f for f in schema]


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


def _item_files_preview(request: Request) -> bool:
    """Whether the item files table should show inline thumbnails.

    Thumbnails show on the Details tab (the default); they stay hidden on the
    Manufacturing tab, whose gallery already shows images large. HTMX file
    mutations echo the originating tab back via the HX-Current-URL header, so the
    re-rendered section keeps thumbnails on Details and omits them on Manufacturing.
    """
    return "tab=manufacturing" not in request.headers.get("HX-Current-URL", "")


def _item_files_section(entity_id: str, item: dict, *, title: str = "Production documents & images", show_preview: bool = False, tag_filter: str = "", search: str = "", sort_dir: str = "desc", page: int = 1, date_from: str = "", date_to: str = "") -> FT:
    """Render the shared files section for an item, with hero toggle enabled.

    Titled "Production documents & images" everywhere it appears (Details +
    Manufacturing tabs) so the heading stays consistent across every HTMX refresh
    (all the file mutation routes funnel through here) and describes what it is."""
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
    # On the Manufacturing tab this lives in a narrow column with no inline preview (the gallery
    # shows images large above), so callers default show_preview=False; the Details tab passes
    # show_preview=True to put thumbnails back next to filenames. No hero column either way (set
    # the hero by clicking a gallery thumbnail); compact filters keep the bar on one row.
    return _shared_files_section("item", entity_id, files, can_set_hero=False, show_linked=False,
                                 title=title, show_preview=show_preview, compact=True,
                                 tag_filter=tag_filter, search=search, sort_dir=sort_dir,
                                 page=page, date_from=date_from, date_to=date_to)


def _recipe_money(v, currency: str | None) -> str:
    """Format a derived cost; '--' (not blank) for unset, per GDR §2k."""
    if v in (None, ""):
        return EMPTY
    return f"{currency or ''}{float(v):,.2f}"


# Editable fields per recipe section, with their system-standard cell types.
_RECIPE_CELLS: dict[str, dict[str, str]] = {
    "components": {"quantity": "number"},
    "labor": {"operation": "text", "kind": "select", "hours": "number", "rate": "number", "amount": "number"},
    "overhead": {"description": "text", "amount": "number"},
}
# Hourly and Daily are both rate-based (qty x rate); Fixed is a flat total. Order = dropdown order.
_LABOR_KIND_LABELS = {"hourly": "Hourly", "daily": "Daily", "fixed": "Fixed"}
# Kinds whose cost is quantity x rate (vs a flat fixed total).
_LABOR_RATE_KINDS = frozenset({"hourly", "daily"})


def _num_field(v) -> float:
    """Parse a numeric add-row field; blank/invalid -> 0.0 (deterministic, no guessing)."""
    try:
        return float(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _recipe_api_payload(recipe: dict) -> dict:
    """The persisted recipe, shaped for the manufacturing API (derived costs recomputed there)."""
    def _num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    return {
        "output_qty": _num(recipe.get("output_qty")) or 1.0,
        "components": [
            {"item_id": c.get("item_id"), "sku": c.get("sku"), "quantity": _num(c.get("quantity")), "unit": c.get("unit")}
            for c in recipe.get("components", []) if c.get("item_id")
        ],
        "labor": [
            {"operation": l.get("operation", ""), "kind": l.get("kind") or "hourly", "hours": _num(l.get("hours")),
             "rate": _num(l.get("rate")), "amount": _num(l.get("amount")), "source": l.get("source") or "manual"}
            for l in recipe.get("labor", [])
        ],
        "overhead": [
            {"description": o.get("description", ""), "amount": _num(o.get("amount"))}
            for o in recipe.get("overhead", [])
        ],
    }


def _recipe_cell(entity_id: str, section: str, idx: int, field: str, value) -> FT:
    """A recipe value as the system-standard dblclick-to-edit cell (same as item/doc tables)."""
    from ui.components.table import display_cell
    return display_cell(
        entity_id=entity_id,
        field=f"recipe__{section}__{idx}__{field}",
        value=value,
        cell_type=_RECIPE_CELLS[section][field],
        edit_url=f"/api/items/{entity_id}/recipe-cell/{section}/{idx}/{field}/edit",
        label_map=_LABOR_KIND_LABELS if field == "kind" else None,
    )


def _recipe_cost_card(entity_id: str, item: dict, currency: str | None) -> FT:
    """The Cost Summary card: live materials / labor / overhead / unit-cost totals.

    The card always reflects the current recipe (the whole section re-renders on every save),
    and the rolled unit cost is applied to the item's cost price automatically, so there are
    no Recalculate / Apply buttons to press.
    """
    recipe = item.get("recipe") or {}
    has_recipe = bool(recipe.get("components"))

    def _row(label, key, total=False):
        return Tr(Td(label, cls="detail-label detail-label--total" if total else "detail-label"),
                  Td(_recipe_money(recipe.get(key), currency), cls="recipe-amount cell--total" if total else "recipe-amount"))

    hint = ("Rolled up from current component costs and applied to this item's cost price automatically."
            if has_recipe else
            "The standard cost will appear here as you add components below.")
    children = [
        H3("Cost summary", cls="section-title"),
        Table(Tbody(_row("Materials", "materials_cost"), _row("Labor", "labor_cost"),
                    _row("Overhead", "overhead_cost"), _row("Unit cost", "unit_cost", total=True)), cls="detail-table"),
        P(hint, cls="hint"),
    ]
    return Div(*children, id="recipe-cost-card", cls="detail-card recipe-cost-summary")


def _item_image_files(item: dict) -> list[dict]:
    """The item's image files, hero first, then product/360 tags, then the rest.

    Shared by the Manufacturing-tab gallery and the printable worksheet so both
    show the same images in the same order (DRY)."""
    images = [f for f in (item.get("files") or []) if (f.get("mime") or "").startswith("image/")]

    def _rank(f: dict):
        if f.get("is_hero"):
            return 0
        return 1 if (f.get("document_tag") or "") in ("product_images", "view_360") else 2

    return sorted(images, key=_rank)


def _workflow_gallery(entity_id: str, item: dict) -> FT:
    """Product image gallery for the Manufacturing tab: a large hero image plus a
    thumbnail strip, sized for shop-floor reference. Renders above the Cost summary.

    Images obey GDR §2l (one dimension fixed: width 100%, height auto, contained),
    and come from the item's uploaded files (managed in Production documents below).
    """
    images = _item_image_files(item)
    if not images:
        return Div(
            H3("Product images", cls="section-title"),
            P("No product images yet. Add them in Production documents below.", cls="hint"),
            cls="detail-card wf-gallery wf-gallery--empty",
            id="wf-gallery",
        )

    def _src(f: dict) -> str:
        return f"/items/{entity_id}/files/{f['id']}/download"

    hero = images[0]
    # Clicking a thumbnail makes that image the hero (the large one) — it does not open a new tab.
    thumbs = (
        Div(*[
            Button(Img(src=_src(f), alt=f.get("filename", ""), cls="wf-thumb-img", loading="lazy"),
                   type="button", title="Set as main image",
                   hx_post=f"/api/items/{entity_id}/gallery-hero/{f['id']}",
                   hx_target="#wf-gallery", hx_swap="outerHTML",
                   cls="wf-thumb" + (" wf-thumb--active" if f is hero else ""))
            for f in images
        ], cls="wf-thumbs")
        if len(images) > 1 else ""
    )
    return Div(
        H3("Product images", cls="section-title"),
        Img(src=_src(hero), alt=hero.get("filename", ""), cls="wf-hero-img", loading="lazy"),
        thumbs,
        P("Click a thumbnail to make it the main image.", cls="hint") if len(images) > 1 else "",
        cls="detail-card wf-gallery",
        id="wf-gallery",
    )


# Editable workflow-step fields and their system-standard cell types (double-click to edit).
# `station` is a select linked to the company's work centers (with an add-new option);
# `instructions` is a multi-line text box.
_WORKFLOW_CELLS: dict[str, str] = {
    "station": "select", "instructions": "textarea",
    "time_value": "number", "time_unit": "select",
}


def _fmt_minutes(mins) -> str:
    """Human elapsed time from canonical minutes: '90 min' -> '1 h 30 min', exact days/hours kept."""
    m = int(round(float(mins or 0)))
    if m <= 0:
        return "0 min"
    if m % 1440 == 0:
        return f"{m // 1440} d"
    if m % 60 == 0:
        return f"{m // 60} h"
    if m >= 60:
        h, mm = divmod(m, 60)
        return f"{h} h {mm} min"
    return f"{m} min"


def _workflow_cell(entity_id: str, idx: int, field: str, value) -> FT:
    """One double-click-to-edit workflow cell (mirrors _recipe_cell: same display_cell engine,
    so editing behaves identically to the rest of the system)."""
    from ui.components.table import display_cell
    return display_cell(
        entity_id=entity_id,
        field=f"workflow__{idx}__{field}",
        value=value,
        cell_type=_WORKFLOW_CELLS[field],
        options=list(_WORKFLOW_TIME_UNITS) if field == "time_unit" else None,
        edit_url=f"/api/items/{entity_id}/workflow-cell/{idx}/{field}/edit",
    )


WORKFLOW_JS = """
(function(){
 function init(){
  var sec=document.getElementById('workflow-section');
  if(!sec||sec._wfInit) return; sec._wfInit=true;
  var base=sec.getAttribute('data-base');
  // outerHTML swaps do not re-run inline scripts, so re-init the fresh section after a reload.
  function reload(html){ sec.outerHTML=html; var ns=document.getElementById('workflow-section'); if(ns&&window.htmx) htmx.process(ns); init(); }
  // Running total, recomputed client-side after each single-cell edit so editing a value or unit
  // never triggers a section reload.
  function fmtMin(m){ m=Math.round(m); if(m<=0)return '0 min'; if(m%1440===0)return (m/1440)+' d'; if(m%60===0)return (m/60)+' h'; if(m>=60){var h=Math.floor(m/60),mm=m%60;return h+' h '+mm+' min';} return m+' min'; }
  function recalc(){
    var total=0;
    sec.querySelectorAll('tr.wf-row').forEach(function(r){
      var tv=r.querySelector('[data-col$="__time_value"]'), tu=r.querySelector('[data-col$="__time_unit"]');
      if(!tv) return;
      var v=parseFloat((tv.textContent||'').replace(/[^0-9.\\-]/g,'')); if(isNaN(v)) v=0;
      var u=(tu?tu.textContent.trim():'min'), f=u==='day'?1440:(u==='hr'?60:1);
      total+=v*f;
    });
    var el=sec.querySelector('.wf-summary'); if(el) el.textContent='Total time '+fmtMin(total);
  }
  sec.addEventListener('htmx:afterSettle', recalc);
  var dragId=null;
  sec.querySelectorAll('.wf-drag').forEach(function(h){
    h.addEventListener('dragstart',function(e){ var tr=h.closest('tr'); dragId=tr.getAttribute('data-step-id'); tr.classList.add('wf-dragging'); e.dataTransfer.effectAllowed='move'; e.dataTransfer.setData('text/plain',dragId); });
    h.addEventListener('dragend',function(){ var tr=h.closest('tr'); if(tr) tr.classList.remove('wf-dragging'); });
  });
  sec.querySelectorAll('tr.wf-row').forEach(function(tr){
    tr.addEventListener('dragover',function(e){ if(dragId){ e.preventDefault(); tr.classList.add('wf-drag-over'); } });
    tr.addEventListener('dragleave',function(){ tr.classList.remove('wf-drag-over'); });
    tr.addEventListener('drop',function(e){
      tr.classList.remove('wf-drag-over');
      if(e.dataTransfer.files&&e.dataTransfer.files.length) return;  // file drops handled by the ref cell
      if(!dragId) return; e.preventDefault();
      var rows=Array.prototype.slice.call(sec.querySelectorAll('tr.wf-row'));
      var ids=rows.map(function(r){return r.getAttribute('data-step-id');});
      var from=ids.indexOf(dragId), to=ids.indexOf(tr.getAttribute('data-step-id'));
      if(from<0||to<0||from===to){ dragId=null; return; }
      ids.splice(to,0,ids.splice(from,1)[0]); dragId=null;
      var fd=new FormData(); fd.append('order',ids.join(','));
      fetch(base+'?action=reorder',{method:'POST',headers:{'HX-Request':'true'},body:fd}).then(function(r){return r.text();}).then(reload);
    });
  });
  sec.querySelectorAll('.wf-ref-drop').forEach(function(cell){
    cell.addEventListener('dragover',function(e){ if(e.dataTransfer.types&&Array.prototype.indexOf.call(e.dataTransfer.types,'Files')>=0){ e.preventDefault(); cell.classList.add('wf-ref-active'); } });
    cell.addEventListener('dragleave',function(){ cell.classList.remove('wf-ref-active'); });
    cell.addEventListener('drop',function(e){
      if(!(e.dataTransfer.files&&e.dataTransfer.files.length)) return;
      e.preventDefault(); e.stopPropagation(); cell.classList.remove('wf-ref-active');
      var idx=cell.getAttribute('data-idx');
      var fd=new FormData(); fd.append('file',e.dataTransfer.files[0]);
      fetch(base+'?action=upload_ref&index='+idx,{method:'POST',headers:{'HX-Request':'true'},body:fd}).then(function(r){return r.text();}).then(reload);
    });
  });
 }
 init();
})();
"""


def _workflow_section(entity_id: str, item: dict) -> FT:
    """The Production workflow: an ordered, drag-to-reorder list of build steps. Cells are the
    system-standard double-click-to-edit cells; the elapsed Time is canonical minutes (a number
    plus a unit select), Instructions is a multi-line text box, and a reference image can be
    dropped onto a step's Reference cell.
    """
    steps = list((item.get("workflow") or {}).get("steps") or [])
    files_by_id = {f.get("id"): f for f in (item.get("files") or [])}
    base = f"/api/items/{entity_id}/workflow-section"
    _struct = {"hx_post": base, "hx_target": "#workflow-section", "hx_swap": "outerHTML", "hx_disabled_elt": "this"}

    total = sum(float(s.get("time_minutes") or 0) for s in steps)
    summary = f"Total time {_fmt_minutes(total)}"

    def _ref_cell(idx: int, step: dict) -> FT:
        ref = files_by_id.get(step.get("ref_file_id")) if step.get("ref_file_id") else None
        if ref:
            src = f"/items/{entity_id}/files/{ref['id']}/download"
            inner = [
                A(Img(src=src, alt=ref.get("filename", ""), cls="wf-ref-img", loading="lazy"),
                  href=src, target="_blank", title=ref.get("filename", "")),
                Button("×", type="button", title="Remove reference", cls="btn btn--xs btn--ghost",
                       hx_post=f"{base}?action=clear_ref&index={idx}", **{k: v for k, v in _struct.items() if k != "hx_post"}),
            ]
        else:
            inner = [Span("Drop image", cls="wf-ref-hint")]
        return Td(*inner, cls="wf-ref-drop", **{"data-idx": str(idx)})

    def _row(idx: int, step: dict) -> FT:
        # Cell order must match the filter/sort column indices below (0-based, drag handle is 0).
        return Tr(
            Td(Span("⠿", cls="wf-drag", draggable="true", title="Drag to reorder"), cls="wf-col-drag"),
            _workflow_cell(entity_id, idx, "station", step.get("station")),
            _workflow_cell(entity_id, idx, "instructions", step.get("instructions")),
            _workflow_cell(entity_id, idx, "time_value", step.get("time_value")),
            _workflow_cell(entity_id, idx, "time_unit", step.get("time_unit") or "min"),
            _ref_cell(idx, step),
            Td(Button("×", type="button", title="Remove step", cls="btn btn--xs btn--ghost",
                      hx_post=f"{base}?action=remove&index={idx}", **{k: v for k, v in _struct.items() if k != "hx_post"}),
               cls="cell--actions"),
            cls="wf-row data-row", **{"data-step-id": step.get("id") or f"idx-{idx}"},
        )

    # Columns: 0 drag, 1 Station, 2 Instructions, 3 Time, 4 Unit, 5 Reference, 6 remove.
    # Station/Instructions/Unit are Excel-filterable + sortable; Time sorts. Filtering/sorting
    # is a client-side view (COLUMN_FILTER_JS/ENHANCED_TABLE_JS) over the manual drag order.
    head = Tr(
        Th("", cls="wf-col-drag"),
        filter_th("Station", 1, sortable=True),
        filter_th("Instructions", 2, sortable=True),
        sortable_th("Time", 3, right=True),
        filter_th("Unit", 4, sortable=True, center=True),
        Th("Reference"), Th("", cls="cell--actions"),
    )
    body = [_row(i, s) for i, s in enumerate(steps)]
    table = Table(Thead(head), Tbody(*body), cls="data-table js-table wf-table") if steps else ""
    empty = P("No steps yet. Add the build steps a worker follows; drag the handle to reorder.",
              cls="hint") if not steps else ""

    return Div(
        Div(
            H3("Production workflow", cls="section-title"),
            Span(summary, cls="wf-summary") if steps else "",
            cls="recipe-section-head",
        ),
        P("The ordered steps to build this item, by work center. Double-click a cell to edit; drag a "
          "row to reorder; sort or filter any column; drop an image on Reference to illustrate a step.",
          cls="hint"),
        table,
        empty,
        Button("+ Add step", type="button", cls="btn btn--sm btn--secondary",
               hx_post=f"{base}?action=add", **{k: v for k, v in _struct.items() if k != "hx_post"}),
        Script(COLUMN_FILTER_JS), Script(ENHANCED_TABLE_JS), Script(WORKFLOW_JS),
        id="workflow-section",
        cls="detail-card recipe-block",
        **{"data-base": base},
    )


_WORKSHEET_PRINT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Arial, sans-serif; font-size: 10pt; color: #111; background: #fff; padding: 16mm; }
.ws-header { display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #111; padding-bottom: 4mm; margin-bottom: 6mm; }
.ws-title { font-size: 17pt; font-weight: 700; }
.ws-sub { color: #555; font-size: 9pt; margin-top: 1mm; }
.ws-meta { text-align: right; font-size: 9pt; color: #333; }
.ws-meta strong { font-size: 11pt; }
.ws-section { margin-top: 6mm; page-break-inside: avoid; }
.ws-section h2 { font-size: 11pt; border-bottom: 1px solid #999; padding-bottom: 1mm; margin-bottom: 2mm; }
table.ws-tbl { width: 100%; border-collapse: collapse; font-size: 9pt; }
.ws-tbl th, .ws-tbl td { border: 1px solid #ccc; padding: 1.6mm 2.6mm; text-align: left; vertical-align: top; }
.ws-tbl th { background: #f2f2f2; }
.ws-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ws-pre { white-space: pre-wrap; }
.ws-images { display: flex; flex-wrap: wrap; gap: 4mm; }
.ws-images img { height: 38mm; width: auto; max-width: 62mm; object-fit: contain; border: 1px solid #ddd; }
.ws-step-img { height: 16mm; width: auto; border: 1px solid #ddd; }
.ws-muted { color: #777; }
.ws-foot { margin-top: 8mm; border-top: 1px solid #ccc; padding-top: 2mm; font-size: 8pt; color: #777; display: flex; justify-content: space-between; }
@page { size: A4 portrait; margin: 0; }
@media print { body { padding: 14mm; } }
"""


def _worksheet_unit_label(unit: str) -> str:
    return {"min": "min", "hr": "hr", "day": "day"}.get(unit, unit or "min")


def _worksheet_print_view(entity_id: str, item: dict, today: str) -> FT:
    """Standalone printable production worksheet: product info + images + materials + workflow.
    Costs never appear here — this is a shop-floor build sheet, not a costing document. Mirrors
    the document print view (auto window.print(); the browser saves it as one PDF)."""
    recipe = item.get("recipe") or {}
    EM = EMPTY

    # ── Product images ──
    images = _item_image_files(item)
    img_section = (
        Div(
            H2("Product images"),
            Div(*[Img(src=f"/items/{entity_id}/files/{f['id']}/download", alt=f.get("filename", ""))
                  for f in images[:6]], cls="ws-images"),
            cls="ws-section",
        ) if images else ""
    )

    # ── Materials to gather (no costs) ──
    comps = recipe.get("components", []) or []
    materials = (
        Div(
            H2("Materials"),
            Table(
                Thead(Tr(Th("Component"), Th("Qty", cls="ws-num"), Th("Unit"))),
                Tbody(*[Tr(
                    Td(f"{c.get('sku') or ''} {('- ' + c['name']) if c.get('name') else ''}".strip(" -") or EM),
                    Td(f"{float(c.get('quantity') or 0):g}", cls="ws-num"),
                    Td(c.get("unit") or EM),
                ) for c in comps]),
                cls="ws-tbl",
            ),
            cls="ws-section",
        ) if comps else ""
    )

    # ── Workflow steps ──
    steps = (item.get("workflow") or {}).get("steps") or []
    files_by_id = {f.get("id"): f for f in (item.get("files") or [])}

    def _time_cell(s: dict) -> FT:
        txt = f"{float(s.get('time_value') or 0):g} {_worksheet_unit_label(s.get('time_unit') or 'min')}"
        return Td(txt, cls="ws-num")

    def _step_ref(s: dict) -> FT:
        f = files_by_id.get(s.get("ref_file_id")) if s.get("ref_file_id") else None
        if f:
            return Td(Img(src=f"/items/{entity_id}/files/{f['id']}/download", alt="", cls="ws-step-img"))
        return Td(EM, cls="ws-muted")

    workflow = (
        Div(
            H2("Production workflow"),
            Table(
                Thead(Tr(Th("#", cls="ws-num"), Th("Station"), Th("Instructions"),
                         Th("Time", cls="ws-num"), Th("Ref"))),
                Tbody(*[Tr(
                    Td(str(i + 1), cls="ws-num"),
                    Td(s.get("station") or EM),
                    Td(s.get("instructions") or EM, cls="ws-pre"),
                    _time_cell(s),
                    _step_ref(s),
                ) for i, s in enumerate(steps)]),
                cls="ws-tbl",
            ),
            cls="ws-section",
        ) if steps else ""
    )

    sku = item.get("sku") or ""
    name = item.get("name") or sku or entity_id
    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(f"Worksheet - {name}"),
            Style(_WORKSHEET_PRINT_CSS),
        ),
        Body(
            Div(
                Div(
                    Div(name, cls="ws-title"),
                    Div(f"SKU {sku}" + (f"  ·  {item.get('category')}" if item.get("category") else ""), cls="ws-sub"),
                    cls="ws-headl",
                ),
                Div(
                    Div("Production Worksheet"),
                    Div(f"Output {float(recipe.get('output_qty') or 1):g} / batch"),
                    Div(today, cls="ws-muted"),
                    cls="ws-meta",
                ),
                cls="ws-header",
            ),
            img_section,
            materials,
            workflow,
            Div(Span(f"{sku} - {name}"), Span("Powered by celerp.com"), cls="ws-foot"),
            Script("window.onload = function() { window.print(); }"),
        ),
    )


def _recipe_section(entity_id: str, item: dict, items: list[dict], currency: str | None,
                    show_all: bool = False, flash_msg: str | None = None, flash_kind: str = "success") -> FT:
    """The Manufacturing tab: Materials / Labor / Overhead tables + Cost Summary.

    The persisted recipe is the single source of truth: every change saves immediately.
    Values use the system-standard double-click-to-edit cells (same as item and document
    tables) so the page behaves like the rest of celerp. All editing is on-page via HTMX.
    """
    recipe = item.get("recipe") or {}
    components = recipe.get("components", [])
    labor_rows = recipe.get("labor", [])
    overhead_rows = recipe.get("overhead", [])

    # list_items flattens the projection id onto "id" (see flatten_item); exclude the
    # item being edited so it can't be a component of itself.
    item_opts = []
    component_ids: set[str] = set()
    by_id: dict[str, dict] = {}
    for it in items:
        iid = it.get("id") or it.get("entity_id") or ""
        if not iid:
            continue
        by_id[iid] = it
        if iid != entity_id:
            item_opts.append((iid, f"{it.get('sku', '')} - {it.get('name', '')}".strip(" -")))
            if (it.get("inventory_type") or "stocked") == "component":
                component_ids.add(iid)

    sec = f"/api/items/{entity_id}/recipe-section"
    # hx_disabled_elt disables the clicked button while its request is in flight (no double-submits).
    _htmx = {"hx_include": "#recipe-form", "hx_target": "#recipe-section", "hx_swap": "outerHTML", "hx_disabled_elt": "this"}

    # The add-picker searches COMPONENTS only by default. Scope is controlled inside the
    # dropdown itself (the common footer-action pattern): a pinned "Search all items…"
    # option widens this picker only, and the last option creates a brand-new component.
    # Already-added components stay in the picker: a process recipe can use the same
    # ingredient in more than one step, so an added item must remain addable again.
    base_opts = [o for o in item_opts if show_all or o[0] in component_ids]
    scope_opt = ("__scope__components", "Show components only…") if show_all else ("__scope__all", "Search all items…")
    comp_opts = base_opts + [scope_opt, ("__new__:/inventory/new?type=component", "+ Add new component")]

    def _comp_row(i, c):
        cid = c.get("item_id", "")
        it = by_id.get(cid)
        orphan = it is None
        label = f"{c.get('sku') or cid} - {it.get('name', '')}".strip(" -") if it else f"{c.get('sku') or cid} (unavailable)"
        return Tr(
            Td(A(label, href=f"/inventory/{cid}", cls="table-link") if not orphan else Span(label)),
            _recipe_cell(entity_id, "components", i, "quantity", c.get("quantity")),
            Td(c.get("unit") or EMPTY, cls="comp-unit-cell"),
            # Unit cost + extended cost are read-only: they come from the component's catalog cost.
            Td(_recipe_money(c.get("unit_cost"), currency), cls="recipe-amount"),
            Td(_recipe_money(c.get("line_cost"), currency), cls="recipe-amount"),
            Td(Button("×", type="button", title="Remove component", cls="btn btn--xs btn--ghost",
                      hx_post=f"{sec}?action=remove_component&index={i}", **_htmx), cls="cell--actions"),
            cls="data-row recipe-row--orphan" if orphan else "data-row",
        )

    def _labor_row(i, l):
        # Qty + Rate apply to rate-based kinds (Hourly/Daily); the Total then shows qty x rate
        # (read-only). Fixed has no qty/rate, so its Total cell is the editable flat amount.
        # The Total column therefore always shows a value — never a hidden "--".
        kind = l.get("kind") or "hourly"
        rate_based = kind in _LABOR_RATE_KINDS
        _na = lambda: Td(EMPTY, cls="recipe-cell--na", title="Not used for this labor type")
        if rate_based:
            qty_cell = _recipe_cell(entity_id, "labor", i, "hours", l.get("hours"))
            rate_cell = _recipe_cell(entity_id, "labor", i, "rate", l.get("rate"))
            line_total = _num_field(l.get("hours")) * _num_field(l.get("rate"))
            total_cell = Td(_recipe_money(line_total, currency), cls="recipe-amount")
        else:
            qty_cell, rate_cell = _na(), _na()
            total_cell = _recipe_cell(entity_id, "labor", i, "amount", l.get("amount"))
        return Tr(
            _recipe_cell(entity_id, "labor", i, "operation", l.get("operation")),
            _recipe_cell(entity_id, "labor", i, "kind", kind),
            qty_cell, rate_cell, total_cell,
            Td(Button("×", type="button", title="Remove operation", cls="btn btn--xs btn--ghost",
                      hx_post=f"{sec}?action=remove_labor&index={i}", **_htmx), cls="cell--actions"),
            cls="data-row",
        )

    def _oh_row(i, o):
        return Tr(
            _recipe_cell(entity_id, "overhead", i, "description", o.get("description")),
            _recipe_cell(entity_id, "overhead", i, "amount", o.get("amount")),
            Td(Button("×", type="button", title="Remove cost", cls="btn btn--xs btn--ghost",
                      hx_post=f"{sec}?action=remove_overhead&index={i}", **_htmx), cls="cell--actions"),
            cls="data-row",
        )

    cur_label = f" ({currency})" if currency else ""

    # Permanent "add" row at the bottom of each table: committing on first input creates a real,
    # persisted row (then a fresh add-row appears). No orphan blank rows to lose on navigation.
    _add = {"hx_include": "#recipe-form", "hx_target": "#recipe-section", "hx_swap": "outerHTML",
            "hx_disabled_elt": "this"}
    # Enter anywhere in an add-row commits the whole line (clicks that row's + Add button).
    _add_enter = ("if(event.key==='Enter'){event.preventDefault();"
                  "this.closest('tr').querySelector('button.recipe-add-btn').click();}")

    # Materials: selecting a SKU adds the line in one action (the docs item-picker pattern);
    # quantity defaults to 1 and is edited in place. The scope toggle lives in the dropdown.
    comp_add_row = Tr(
        Td(searchable_select("comp_new", comp_opts, value="", placeholder="Search to add a component…",
                             hx_post=f"{sec}?action=add_component", hx_trigger="change",
                             hx_include="#recipe-form", hx_target="#recipe-section", hx_swap="outerHTML")),
        Td("", cls="cell--number"), Td(""), Td("", cls="recipe-amount"), Td("", cls="recipe-amount"),
        Td("", cls="cell--actions"),
        cls="recipe-add-row",
    )
    # Labor / Overhead: free-form lines — fill the whole row, then + Add commits it in one step.
    # Rate-based kinds (Hourly/Daily) use Qty+Rate and the Total shows Qty x Rate live (read-only);
    # Fixed uses the flat editable Total. Self-contained inline JS so it survives re-renders.
    # Live Total preview as Qty/Rate are typed (only while Total is the computed/read-only field).
    _live_total = ("var r=this.closest('tr'),"
                   "q=parseFloat(r.querySelector('[name=labor_new_hours]').value)||0,"
                   "rt=parseFloat(r.querySelector('[name=labor_new_rate]').value)||0,"
                   "t=r.querySelector('[name=labor_new_amount]');"
                   "if(t.readOnly){t.value=(q&&rt)?(q*rt).toFixed(2):'';}")
    # Switching Type flips which fields apply AND clears the now-inapplicable ones so no stale
    # greyed values linger (Qty/Rate for Fixed; the flat Total for rate-based).
    _kind_toggle = ("var r=this.closest('tr'),fx=this.value==='fixed',"
                    "q=r.querySelector('[name=labor_new_hours]'),"
                    "rt=r.querySelector('[name=labor_new_rate]'),"
                    "t=r.querySelector('[name=labor_new_amount]');"
                    "q.disabled=fx;rt.disabled=fx;t.readOnly=!fx;"
                    "if(fx){q.value='';rt.value='';t.value='';}"
                    "else{var a=(parseFloat(q.value)||0)*(parseFloat(rt.value)||0);t.value=a?a.toFixed(2):'';}")
    # Clicking + Add with the required text field empty flashes it red instead of failing silently.
    _add_validate = ("var q=this.closest('tr').querySelector('.recipe-req');"
                     "if(q&&!q.value.trim()){q.classList.add('field-flash-error');q.focus();"
                     "setTimeout(function(){q.classList.remove('field-flash-error');},1500);"
                     "event.stopImmediatePropagation();event.preventDefault();return false;}")
    labor_add_row = Tr(
        Td(Input(type="text", name="labor_new_op", value="", placeholder="Add an operation…", cls="recipe-req", onkeydown=_add_enter)),
        Td(Select(*[Option(lbl, value=val, selected=(val == "hourly")) for val, lbl in _LABOR_KIND_LABELS.items()],
                  name="labor_new_kind", cls="labor-kind", onchange=_kind_toggle)),
        Td(Input(type="number", name="labor_new_hours", value="", placeholder="0", step="any", min="0", cls="cell--number", oninput=_live_total, onkeydown=_add_enter), cls="cell--number"),
        Td(Input(type="number", name="labor_new_rate", value="", placeholder="0", step="any", min="0", cls="cell--number", oninput=_live_total, onkeydown=_add_enter), cls="cell--number"),
        # Total: read-only computed preview for rate-based (default Hourly), editable only for Fixed.
        Td(Input(type="number", name="labor_new_amount", value="", placeholder="0", step="any", min="0", cls="cell--number", readonly=True, onkeydown=_add_enter), cls="cell--number"),
        Td(Button("Save", type="button", cls="btn btn--xs btn--secondary recipe-add-btn",
                  hx_post=f"{sec}?action=add_labor", onclick=_add_validate, **_add), cls="cell--actions"),
        cls="recipe-add-row",
    )
    oh_add_row = Tr(
        Td(Input(type="text", name="oh_new_desc", value="", placeholder="Add a cost…", cls="recipe-req", onkeydown=_add_enter)),
        Td(Input(type="number", name="oh_new_amount", value="", placeholder="0", step="any", min="0", cls="cell--number", onkeydown=_add_enter), cls="cell--number"),
        Td(Button("Save", type="button", cls="btn btn--xs btn--secondary recipe-add-btn",
                  hx_post=f"{sec}?action=add_overhead", onclick=_add_validate, **_add), cls="cell--actions"),
        cls="recipe-add-row",
    )
    materials = Table(
        Thead(Tr(Th("Component SKU"), Th("Quantity", cls="cell--number"), Th("Unit"),
                 Th(f"Unit cost{cur_label}", cls="cell--number"), Th(f"Cost{cur_label}", cls="cell--number"),
                 Th("", cls="cell--actions"))),
        Tbody(*[_comp_row(i, c) for i, c in enumerate(components)], comp_add_row),
        cls="data-table",
    )
    labor = Table(
        Thead(Tr(Th("Operation"), Th("Type"), Th("Qty", cls="cell--number"),
                 Th(f"Rate{cur_label}", cls="cell--number"), Th(f"Total{cur_label}", cls="cell--number"),
                 Th("", cls="cell--actions"))),
        Tbody(*[_labor_row(i, l) for i, l in enumerate(labor_rows)], labor_add_row),
        cls="data-table",
    )
    overhead = Table(
        Thead(Tr(Th("Description"), Th(f"Amount{cur_label}", cls="cell--number"), Th("", cls="cell--actions"))),
        Tbody(*[_oh_row(i, o) for i, o in enumerate(overhead_rows)], oh_add_row),
        cls="data-table",
    )

    def _section_head(title: str, total_key: str) -> FT:
        """Section title with its running total on the right (GDR §4: currency right-aligned)."""
        return Div(
            H3(title, cls="section-title"),
            Span(_recipe_money(recipe.get(total_key), currency), cls="recipe-section-total"),
            cls="recipe-section-head",
        )

    has_recipe = bool((item.get("recipe") or {}).get("components"))

    # Mark-to-market is automatic: changing a component's cost recosts every item that uses it
    # (see the inventory pricing endpoint + manufacturing set_item_recipe), so there is no
    # manual "re-cost dependents" control here.
    # Right column, top to bottom: product images, then the drop-files area directly under them.
    # The Cost summary sits under the recipe (left column), right-aligned like a totals block.
    right_col = [
        _workflow_gallery(entity_id, item),
        _item_files_section(entity_id, item),
    ]

    has_rows = bool(components or labor_rows or overhead_rows)
    flash_el = Div(flash_msg, cls=f"flash flash--{flash_kind}", role="status") if flash_msg else ""

    # No Save button: every change persists automatically. Clear keeps an inline confirm.
    clear_action = ""
    if has_rows:
        clear_action = Div(
            Button("Clear recipe", type="button", cls="btn btn--ghost btn--danger",
                   onclick="this.style.display='none';this.nextElementSibling.style.display='inline-flex';"),
            Span(
                Span("Clear all rows?", cls="confirm-label"),
                Button("Yes, clear", type="button", cls="btn btn--danger btn--xs",
                       hx_post=f"{sec}?action=clear", **_htmx),
                Button("Cancel", type="button", cls="btn btn--ghost btn--xs",
                       onclick="var w=this.closest('.clear-confirm');w.style.display='none';w.previousElementSibling.style.display='';"),
                cls="clear-confirm", style="display:none;",
            ),
            cls="clear-recipe-wrap",
        )

    # Per-picker scope state rides in the form so it survives re-renders.
    scope_state = Input(type="hidden", name="show_all_components", value="on") if show_all else ""

    # Auto-save for the one standalone form field (output qty); rows save per-cell.
    _save = {"hx_post": f"/api/items/{entity_id}/recipe-autosave", "hx_trigger": "change",
             "hx_include": "#recipe-form", "hx_swap": "none"}

    return Div(
        Form(
            flash_el,
            scope_state,
            Div("All changes save automatically. Double-click a value to edit it.",
                id="recipe-save-status", cls="recipe-save-status hint"),
            Div(
                Label("Output quantity per batch", For="output_qty"),
                Input(type="number", id="output_qty", name="output_qty",
                      value=f"{recipe.get('output_qty', 1) or 1:g}", min="0.001", step="any", cls="cell--number", **_save),
                P("Unit cost = total recipe cost ÷ this quantity.", cls="hint"),
                cls="detail-card recipe-block",
            ),
            Div(_section_head("Materials", "materials_cost"), materials, cls="detail-card recipe-block"),
            Div(_section_head("Labor", "labor_cost"), labor, cls="detail-card recipe-block"),
            Div(_section_head("Overhead", "overhead_cost"), overhead, cls="detail-card recipe-block"),
            # Cost summary as a right-aligned totals block under the recipe (like a document's totals).
            Div(_recipe_cost_card(entity_id, item, currency), cls="recipe-totals-wrap"),
            clear_action,
            id="recipe-form",
        ),
        Div(*right_col, cls="recipe-right"),
        id="recipe-section",
        cls="recipe-grid",
        # Any per-cell or output-qty save fires "recipeSaved"; the whole section re-renders so
        # the per-row costs, the per-section totals and the cost summary all stay consistent.
        hx_get=f"/api/items/{entity_id}/recipe-section",
        hx_trigger="recipeSaved from:body",
        hx_include="#recipe-form",
        hx_swap="outerHTML",
    )


def _production_block(entity_id: str, item: dict, hub: dict, cur: str,
                      flash_msg: str | None = None, flash_kind: str = "success") -> FT:
    """The product Manufacturing-tab production hub: open demand for this product (with coverage) +
    its work orders. Both tables are client-sortable and Excel-filterable with from/to due-date
    filters, and paginate when long. Completed/cancelled work orders are hidden by default via the
    Status filter (re-check them in its funnel to show them) - no separate toggle."""
    from ui.routes.manufacturing import _status_badge as _coverage_badge
    demand = hub.get("demand", []) or []
    runs = hub.get("runs", []) or []
    flash_el = Div(flash_msg, cls=f"flash flash--{flash_kind}", role="status") if flash_msg else ""

    def _doc_url(d: dict) -> str:
        did = str(d.get("doc_id", ""))
        return f"/lists/{did}" if did.startswith("list:") else f"/docs/{did}"

    demand_rows = [
        Tr(
            Td(A(d.get("doc_number") or d.get("doc_id"), href=_doc_url(d), cls="table-link")),
            Td((d.get("doc_type") or "").replace("_", " ").title()),
            Td(d.get("contact_name") or EMPTY),
            Td(f"{float(d.get('quantity', 0)):g}", cls="cell--number"),
            Td(d.get("due") or EMPTY, cls="cell--center"),
            Td(_coverage_badge(d["coverage"]) if d.get("coverage") else EMPTY, cls="cell--center"),
            cls="data-row",
        )
        for d in demand
    ]
    demand_tbl = Div(
        date_range_filter("wo-demand-table", 4, "Due date") if demand_rows else "",
        Table(
            Thead(Tr(
                sortable_th("Document", 0), filter_th("Type", 1, sortable=True),
                filter_th("Customer", 2, sortable=True), sortable_th("Qty", 3, right=True),
                sortable_th("Due", 4, center=True), filter_th("Coverage", 5, sortable=True, center=True),
            )),
            Tbody(*demand_rows) if demand_rows else Tbody(Tr(Td("No open demand.", colspan="6", cls="empty-row"))),
            cls="data-table js-table", id="wo-demand-table", **{"data-page-size": "25"},
        ),
        table_pager("wo-demand-table"),
    )

    # Work orders are shown in a table that surfaces the order each one fulfils. The status is a
    # badge (with a tooltip); the lifecycle action is a concise per-row Action dropdown. Transitions
    # have side effects (Complete issues components, receives output and posts a JE).
    def _wo_status_badge(status: str) -> FT:
        from ui.routes.manufacturing import RUN_STATUS_HELP
        s = (status or "planned").lower()
        help_txt = RUN_STATUS_HELP.get(s, "")
        return Span(s.replace("_", " ").title(), cls=f"badge badge--{s.replace('_', '-')}",
                    **({"title": help_txt} if help_txt else {}))

    def _wo_action_select(rid: str, status: str) -> FT:
        opts = [Option("Action…", value="", disabled=True, selected=True)]
        if status == "planned":
            opts.append(Option("Start", value="start"))
        if status == "in_progress":
            opts += [Option("Complete", value="complete"), Option("Put on hold", value="hold")]
        if status == "on_hold":
            opts.append(Option("Resume", value="resume"))
        if status not in ("completed", "cancelled"):
            opts.append(Option("Cancel", value="cancel"))
        if len(opts) == 1:  # closed run - no further actions
            return Span(EMPTY)
        return Select(*opts, name="action", cls="wo-action-select", hx_trigger="change",
                      hx_post=f"/api/items/{entity_id}/runs/{rid}/act",
                      hx_target="#production-block", hx_swap="outerHTML", hx_disabled_elt="this")

    def _wo_source_cell(run: dict) -> FT:
        src_id, src_no = run.get("source_doc_id"), run.get("source_doc_number")
        if src_id and src_no:
            href = f"/lists/{src_id}" if str(src_id).startswith("list:") else f"/docs/{src_id}"
            return A(src_no, href=href, cls="table-link")
        return Span("To stock", cls="hint")  # no linked order = made to stock

    def _wo_row(run: dict) -> FT:
        rid, status = run.get("id"), run.get("status", "planned")
        qty = float((run.get("expected_outputs") or [{}])[0].get("quantity", 0))
        return Tr(
            Td("WO-" + str(rid).split(":")[-1][:8], cls="wo-ref"),
            Td(_wo_source_cell(run)),
            Td(run.get("source_contact_name") or EMPTY),
            Td(f"{qty:g}", cls="cell--number"),
            Td(run.get("source_due") or (run.get("created_at") or "")[:10] or EMPTY, cls="cell--center"),
            Td(_wo_status_badge(status)),
            Td(_wo_action_select(rid, status), cls="cell--actions"),
            cls="data-row" + (" data-row--inactive" if status == "cancelled" else ""),
        )

    runs_view = (Div(
        date_range_filter("wo-orders-table", 4, "Due date"),
        Table(
            Thead(Tr(
                sortable_th("Work order", 0), filter_th("Source order", 1, sortable=True),
                filter_th("Customer", 2, sortable=True), sortable_th("Qty", 3, right=True),
                sortable_th("Due", 4, center=True),
                filter_th("Status", 5, sortable=True, default_exclude=["Completed", "Cancelled"]),
                Th("", cls="cell--actions"),
            )),
            Tbody(*[_wo_row(r) for r in runs]),
            cls="data-table js-table", id="wo-orders-table", **{"data-page-size": "25"},
        ),
        table_pager("wo-orders-table"),
    ) if runs else P("No work orders to show. Create them from an order or on the Demand Planning page.", cls="hint"))

    return Div(
        flash_el,
        Div(H3("Open demand", cls="section-title"),
            P("Orders that have this item on order, with how well current supply covers each.", cls="hint"),
            demand_tbl, cls="detail-card recipe-block"),
        Div(
            H3("Work orders", cls="section-title"),
            P("Production tasks for this item, each shown with the order it fulfils. Completed and "
              "cancelled work orders are hidden by default - re-check them in the Status filter to "
              "show them. Advance one with its Action menu.", cls="hint"),
            runs_view, cls="detail-card recipe-block"),
        Script(COLUMN_FILTER_JS),
        Script(ENHANCED_TABLE_JS),
        # Re-seed the Status default-exclude (hide Completed/Cancelled) on every (re)render — runs
        # on the initial lazy load and after each Action-dropdown swap.
        Script("window.celerpRefreshFilters&&window.celerpRefreshFilters(document.getElementById('production-block'));"),
        id="production-block",
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
    cell_renderers: dict | None = None,
    base_price_list: str = "",
    split_preview: dict | None = None,
) -> FT:
    """Tabbed item detail: Details | Pricing | Manufacturing | Activity."""
    tabs = [("details", "Details"), ("pricing", "Pricing"), ("manufacturing", "Manufacturing"), ("activity", "Activity")]
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
            # _pricing_form owns its own Cost / Sell-prices grid; no single-column wrapper.
            panel = _pricing_form(entity_id, item, price_lists, currency, pricing_fields, base_price_list)
        else:
            panel = Div(
                _detail_table(entity_id, item, pricing_fields, title="Pricing", currency=currency),
                cls="detail-grid detail-grid--single",
            )
    elif active_tab == "manufacturing":
        # Lazy-loaded sections. recipe-section holds the recipe (left) plus the image gallery,
        # drop-files area, and cost summary (right column). The workflow renders full-width below
        # the cost summary, then the production hub (demand + runs).
        panel = Div(
            Div(
                P("Loading…", cls="hint"),
                hx_get=f"/api/items/{entity_id}/recipe-section",
                hx_trigger="load", hx_swap="outerHTML", id="recipe-section",
            ),
            Div(
                P("Loading…", cls="hint"),
                hx_get=f"/api/items/{entity_id}/workflow-section",
                hx_trigger="load", hx_swap="outerHTML", id="workflow-section",
            ),
            Div(
                hx_get=f"/api/items/{entity_id}/production-block",
                hx_trigger="load", hx_swap="outerHTML", id="production-block",
            ),
        )
    elif active_tab == "activity":
        panel = Div(
            _ledger_table(ledger, entity_id=entity_id, currency=currency),
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
    # Files + item operations belong with the item's core details — keep them off the
    # Pricing / Manufacturing / Activity tabs so each tab shows only its own concern.
    extras = (_item_files_section(entity_id, item, show_preview=True), _advanced_panel(entity_id, item, split_preview)) if active_tab == "details" else ()
    return Div(
        tab_bar,
        panel,
        *extras,
    )


def _fmt_rate(v: float, rdp: int) -> str:
    """Plain numeric string for a unit price: full saved rate precision, trailing
    zeros trimmed (15.285, not 15.28 and not 15.2850)."""
    s = f"{v:.{rdp}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _readonly_price_cells(conventional_key: str, price_val: float, qty: float,
                          has_qty: bool, rdp: int, oob: bool = False):
    """The unit/total value spans of a read-only pricing row.

    Shared by the pricing form render and the price-save response: after a base
    price saves, the same spans return as out-of-band swaps so derived rows update
    immediately without re-rendering the form or moving the user's focus."""
    unit_val = _fmt_rate(price_val, rdp) if price_val else ""
    total_val = f"{price_val * qty:.2f}" if price_val and has_qty else ""
    oob_kw = {"hx_swap_oob": "true"} if oob else {}
    unit = Span(unit_val or EMPTY, cls="form-input form-input--readonly",
                id=f"derived_unit_{conventional_key}", **oob_kw)
    total = Span(total_val or EMPTY, cls="form-input form-input--readonly",
                 id=f"derived_total_{conventional_key}", **oob_kw)
    return unit, total


def _pricing_form(entity_id: str, item: dict, price_lists: list[dict], currency: str | None,
                  pricing_fields: list[dict] | None = None, base_price_list: str = "") -> FT:
    """Pricing tab: Cost on the left, Sell prices on the right. Every field saves automatically.

    Each input autosaves on change (no Save button), consistent with the rest of the system.
    For a manufactured item the cost is rolled from the recipe and shown read-only here — it is
    edited on the Manufacturing tab — so the single source of truth stays the recipe.
    Derived price lists render read-only the same way: their values are computed from the base
    price list, and the schema (config-driven) marks their columns non-editable for every role.
    """
    qty = float(item.get("quantity") or 0)
    sell_by = str(item.get("sell_by") or "unit")
    has_qty = qty > 0
    cur_sym = currency or ""
    has_recipe = bool((item.get("recipe") or {}).get("components"))

    from celerp.services.money import currency_dp as _currency_dp, rate_dp as _rate_dp
    cdp, rdp = _currency_dp(currency or "USD"), _rate_dp(currency or "USD")

    unit_hdr = f"Unit ({cur_sym} / {sell_by})" if cur_sym else f"Unit / {sell_by}"
    total_hdr = f"Total ({qty:g} {sell_by})" if has_qty else "Total (no stock)"

    def _cur(v):
        return Div(Span(cur_sym, cls="input-prefix") if cur_sym else "", v, cls="input-with-prefix")

    # Schema editability is config-driven: derived lists' price columns are marked
    # non-editable by the effective field schema for every role.
    schema_editable = {f.get("key") for f in (pricing_fields or []) if f.get("editable")}

    def _row(pl_name: str, editable: bool) -> FT:
        conventional_key = price_key(pl_name)
        editable = editable and (not pricing_fields or conventional_key in schema_editable)
        price_val = resolve_price(item, pl_name)
        unit_val = _fmt_rate(price_val, rdp) if price_val else ""
        total_val = f"{price_val * qty:.2f}" if price_val and has_qty else ""
        if not editable:
            # Read-only display: recipe-managed cost (edited on the Manufacturing tab)
            # or a derived price list (computed from the base price list). The value spans
            # carry ids so a base-price save can refresh them out-of-band.
            unit_span, total_span = _readonly_price_cells(conventional_key, price_val, qty, has_qty, rdp)
            return Tr(
                Td(pl_name, cls="detail-label"),
                Td(_cur(unit_span)),
                Td(_cur(total_span) if has_qty else Span(EMPTY)),
            )
        unit_id, total_id = f"unit_{conventional_key}", f"total_{conventional_key}"
        # Enter commits by blurring (which fires `change` → the autosave below), matching the
        # rest of the system where Enter always commits an inline edit.
        enter_commits = "if(event.key==='Enter'){event.preventDefault();this.blur();}"
        # Autosave on change (blur/Enter); the linked total mirrors the unit live via oninput.
        _save = {"hx_post": f"/api/items/{entity_id}/price", "hx_trigger": "change",
                 "hx_include": "this", "hx_swap": "none"}
        unit_input = Input(type="number", name=conventional_key, id=unit_id, value=unit_val,
                           step="any", min="0", placeholder="--", cls="form-input",
                           oninput=f"syncPricingTotal('{unit_id}','{total_id}',{qty})",
                           onkeydown=enter_commits, **_save)
        total_input = Input(type="number", id=total_id, value=total_val, step="0.01", min="0",
                            placeholder="--", cls="form-input", disabled=not has_qty,
                            oninput=f"syncPricingUnit('{unit_id}','{total_id}',{qty},{cdp},{rdp})",
                            onkeydown=enter_commits,
                            # Editing the total back-fills the unit, then re-fires its autosave.
                            onchange=f"document.getElementById('{unit_id}').dispatchEvent(new Event('change'))")
        return Tr(Td(pl_name, cls="detail-label"), Td(_cur(unit_input)), Td(_cur(total_input)))

    def _card(title: str, lists: list[dict], editable: bool, note: str | None = None) -> FT:
        return Div(
            H3(title, cls="section-title"),
            Table(Thead(Tr(Th(t("th.price_list")), Th(unit_hdr), Th(total_hdr))),
                  Tbody(*[_row(pl.get("name", ""), editable) for pl in lists]),
                  cls="detail-table"),
            P(note, cls="hint") if note else "",
            cls="detail-card",
        )

    cost_lists = [pl for pl in price_lists if is_cost_list_name(pl.get("name", ""))]
    sell_lists = [pl for pl in price_lists if not is_cost_list_name(pl.get("name", ""))]

    cards = []
    if cost_lists:
        note = "Rolled up from the recipe and kept in sync automatically. Edit it on the Manufacturing tab." if has_recipe else None
        cards.append(_card("Cost", cost_lists, editable=not has_recipe, note=note))
    if sell_lists:
        derived_lists = [pl for pl in sell_lists if is_derived(pl)]
        if derived_lists:
            base_label = base_price_list or "the base price list"
            sentences = []
            for pl in derived_lists:
                s = f"{pl.get('name', '')} is {base_label} × {float(pl['multiplier']):g}"
                if pl.get("rounding") is not None:
                    s += f", rounded to the nearest {float(pl['rounding']):g}"
                sentences.append(s)
            note = ". ".join(sentences) + ". Derived prices update automatically when the base price changes. Factors are edited in Settings under Price Lists."
        elif pricing_fields and any(price_key(pl.get("name", "")) not in schema_editable for pl in sell_lists):
            # Factors are manager-only, but a read-only sell row still gets an explanation.
            note = "Computed automatically from the base price list."
        else:
            note = None
        cards.append(_card("Sell prices", sell_lists, editable=True, note=note))

    return Div(
        Script("""
function roundTo(x, d) { var f = Math.pow(10, d); return Math.round((x + Number.EPSILON) * f) / f; }
function syncPricingTotal(unitId, totalId, qty) {
  var u = parseFloat(document.getElementById(unitId).value);
  var tEl = document.getElementById(totalId);
  if (tEl) tEl.value = (isNaN(u) || !qty) ? '' : (u * qty).toFixed(2);
}
function syncPricingUnit(unitId, totalId, qty, cdp, rdp) {
  if (!qty) return;
  var total = parseFloat(document.getElementById(totalId).value);
  var uEl = document.getElementById(unitId);
  if (isNaN(total)) { uEl.value = ''; return; }
  // Fewest decimals (cdp..rdp) where unit*qty still rounds to the entered total - mirrors the
  // server's unit_price_from_total so the saved unit price actually matches the total entered.
  var target = roundTo(total, cdp), out = null;
  for (var d = cdp; d <= rdp; d++) {
    var cand = roundTo(total / qty, d);
    if (roundTo(cand * qty, cdp) === target) { out = cand.toFixed(d); break; }
  }
  if (out === null) out = (total / qty).toFixed(rdp);
  if (out.indexOf('.') >= 0) { out = out.replace(/0+$/, ''); if (out.charAt(out.length - 1) === '.') out = out.slice(0, -1); }
  uEl.value = out;
}
"""),
        Div("Prices save automatically.", id="pricing-save-status", cls="recipe-save-status hint"),
        Div(*cards, cls="pricing-grid"),
        cls="pricing-tab",
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
                label_map=_INVENTORY_TYPE_LABELS if key == "inventory_type" else None,
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


def _ledger_table(ledger: list[dict], entity_id: str | None = None, currency: str | None = None) -> FT:
    from ui.components.activity import activity_table
    history_url = f"/inventory/{entity_id}/history" if entity_id else None
    return activity_table(ledger, max_display=10, subject_entity_id=entity_id,
                          currency=currency, history_url=history_url, resizable=True)


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
_IMPORT_BASE_COLS = ["sku", "name", "sell_by", "category", "quantity"]
_IMPORT_TAIL_COLS = ["weight", "weight_unit", "gross_weight", "gross_weight_unit", "pieces", "barcode", "hs_code",
                     "purchase_sku", "purchase_name", "purchase_unit", "purchase_conversion_factor",
                     "short_description", "description", "notes", "location_name"]

_IMPORT_SPEC = CsvImportSpec(
    cols=_IMPORT_BASE_COLS + ["retail_price", "wholesale_price", "cost_price"] + _IMPORT_TAIL_COLS,
    required={"name", "sell_by"},
    type_map={"quantity": float, "retail_price": float, "wholesale_price": float,
              "cost_price": float, "weight": float, "purchase_conversion_factor": float},
)


def _importable_price_lists(price_lists: list[dict]) -> list[dict]:
    """Price lists whose values can be imported. Derived lists are computed from the base
    price list at read time, so the import mapper never offers their columns."""
    return [pl for pl in price_lists if pl.get("name") and not is_derived(pl)]


def _build_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build import spec with dynamic price columns from company price lists."""
    price_cols = [price_key(pl["name"]) for pl in _importable_price_lists(price_lists)]
    # Add virtual total cols (one per price col) - back-calculated at confirm time
    price_total_cols = [f"{col}_total" for col in price_cols]
    type_map = {"quantity": float, "weight": float, "pieces": float}
    for col in price_cols + price_total_cols:
        type_map[col] = float
    return CsvImportSpec(
        cols=_IMPORT_BASE_COLS + price_cols + price_total_cols + _IMPORT_TAIL_COLS,
        required={"name", "sell_by"},
        type_map=type_map,
    )


def _import_price_col_labels(price_lists: list[dict]) -> dict[str, str]:
    """Human-readable labels for price columns in the import mapping UI."""
    labels: dict[str, str] = {}
    for pl in _importable_price_lists(price_lists):
        name = pl.get("name", "")
        key = price_key(name)
        labels[key] = f"{name} (Unit Price)"
        labels[f"{key}_total"] = f"{name} (Total)"
    return labels


def _import_price_mutex_groups(price_lists: list[dict]) -> list[list[str]]:
    """Mutex groups: mapping unit price and total for the same price list is mutually exclusive."""
    groups = []
    for pl in _importable_price_lists(price_lists):
        key = price_key(pl["name"])
        groups.append([key, f"{key}_total"])
    return groups


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
    valid_unit_lower: dict[str, str] = {u.lower(): u for u in valid_unit_names}
    weight_unit_names: list[str] = [u["name"] for u in company_units if u.get("unit_type") == "weight"]
    weight_unit_lower: dict[str, str] = {u.lower(): u for u in weight_unit_names}

    def _validate(col: str, value: str, row: dict | None = None) -> bool:
        if col == "sell_by":
            v = value.strip()
            # sell_by is required unless the row's category provides a default
            if not v:
                category = str((row or {}).get("category", "")).strip()
                return bool(cat_sell_by.get(category))
            # If known units are available, validate membership (case-insensitive)
            return not valid_unit_set or v.lower() in valid_unit_lower
        if col == "weight_unit":
            v = value.strip()
            # weight_unit is optional; if provided it must be a known weight-type unit
            if not v:
                return True
            return not weight_unit_lower or v.lower() in weight_unit_lower
        if col == "gross_weight_unit":
            v = value.strip()
            if not v:
                return True
            return not weight_unit_lower or v.lower() in weight_unit_lower
        return _item_validate(col, value)

    # Build import fix-table cell renderers for constrained columns.
    # Renderer signature: (val: str, row_index: int, row: dict, is_bad: bool) -> FT
    cell_renderers: dict = {}
    if valid_unit_names:
        def _make_unit_renderer(col: str, _opts: list = valid_unit_names) -> "Callable":
            def _render(val: str, ri: int, row: dict, is_bad: bool) -> FT:
                err_cls = "cell-edit  input--error" if is_bad else "cell-edit"
                val_stripped = val.strip()
                val_lower = val_stripped.lower()
                matched = any(u.lower() == val_lower for u in _opts)
                # When value is unrecognised, inject it as a pre-selected invalid option
                # so the user can see what they had and choose a replacement.
                unknown_opt = (
                    Option(f"⚠ \"{val_stripped}\" (unknown)", value=val_stripped,
                           selected=True, cls="unit-unknown-option")
                    if val_stripped and not matched
                    else None
                )
                return Select(
                    Option("-- select unit --", value="", selected=(not val_stripped and not matched)),
                    *([unknown_opt] if unknown_opt else []),
                    *[Option(u, value=u, selected=(matched and u.lower() == val_lower)) for u in _opts],
                    Option("+ Add new unit", value="__add_new__"),
                    data_col=col,
                    data_row=str(ri),
                    cls=err_cls,
                )
            return _render

        cell_renderers["sell_by"] = _make_unit_renderer("sell_by")
        cell_renderers["purchase_unit"] = _make_unit_renderer("purchase_unit")
        if weight_unit_names:
            cell_renderers["weight_unit"] = _make_unit_renderer("weight_unit", weight_unit_names)
            cell_renderers["gross_weight_unit"] = _make_unit_renderer("gross_weight_unit", weight_unit_names)

    return _validate, cell_renderers


# Core item columns that map to top-level ItemCreate fields (not attributes).
# Price columns (any key ending in _price) are excluded from attributes separately.
_CORE_ITEM_COLS: frozenset[str] = frozenset({
    "sku", "name", "category", "quantity",
    "weight", "weight_ct", "weight_unit", "gross_weight", "gross_weight_unit",
    "sell_by", "pieces", "status",
    "barcode", "hs_code", "short_description", "description", "notes", "location_name",
    "location_id", "created_at", "updated_at",
})

# Max distinct values before a column is treated as free-text instead of dropdown
_DROPDOWN_THRESHOLD = 30


def _collect_category_attributes(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Return {category: {col: [distinct_values]}} for all attribute columns."""
    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        cat = str(row.get("category", "") or "").strip() or "_uncategorized"
        if cat not in result:
            result[cat] = {}
        for k, v in row.items():
            if k in _CORE_ITEM_COLS or k.endswith("_price") or k.endswith("_price_total"):
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
                ftype = "select"
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

    Virtual columns (e.g. cost_price_total) are always injected immediately after
    their ``paired_with`` column when that column is visible.
    """
    if url_cols:
        base = url_cols
    else:
        pref_key = active_cat if active_cat else "__all__"
        saved = col_prefs.get(pref_key)
        base = saved if saved else [f["key"] for f in eff_schema if f.get("show_in_table", True)]

    # Inject virtual columns immediately after their paired_with counterpart
    virtual_cols = {f["key"]: f for f in eff_schema if f.get("virtual")}
    result: list[str] = []
    seen: set[str] = set()
    for col in base:
        if col in seen:
            continue
        result.append(col)
        seen.add(col)
        # Insert virtual columns paired with this column
        for vk, vf in virtual_cols.items():
            if vf.get("paired_with") == col and vk not in seen:
                result.append(vk)
                seen.add(vk)
    # Also include any virtual columns whose paired_with wasn't in base (shouldn't happen, but safe)
    return result


# ---------------------------------------------------------------------------
# T3: Advanced operations panel (non-inline-editable actions only)
# ---------------------------------------------------------------------------

def _advanced_panel(entity_id: str, item: dict, split_preview: dict | None = None) -> FT:
    """Compact item operations grid: Split, Duplicate, Expire, Dispose."""
    current_qty = float(item.get("quantity", 0) or 0)

    from celerp.modules.slots import get as get_slot
    module_item_actions = []
    for action in get_slot("item_action"):
        href = action.get("href_template", "").replace("{entity_id}", entity_id)
        module_item_actions.append(
            A(action.get("label", "Action"), href=href, cls="btn btn--secondary btn--sm")
        )

    safe_id = re.sub(r"[^a-zA-Z0-9]", "_", entity_id)

    # 2x2 compact action cards
    allow_splitting = item.get("allow_splitting", True)
    sell_by = item.get("sell_by") or "piece"
    # Splittable only when the server preview resolved and the parcel can be carved.
    can_split = bool(allow_splitting and split_preview and not split_preview.get("cannot_split"))
    if can_split:
        sell_by_label = sell_by.capitalize()
        # Batch Split reuses the unit classification for its optional complement field.
        from celerp.services.units import DEFAULT_UNITS
        _default_umap = {u["name"]: u for u in DEFAULT_UNITS}
        _is_weight = is_weight_unit(sell_by, _default_umap)
        _is_pieces = is_pieces_unit(sell_by, _default_umap)
        if _is_weight:
            _comp_name   = "split_pieces"
            _comp_ph     = "Pieces (optional)"
            _comp_title  = t("inv.tooltip.split_pieces_opt")
            _comp_step   = "1"
        elif _is_pieces:
            _comp_name   = "split_weight"
            _comp_ph     = "Weight (optional)"
            _comp_title  = t("inv.tooltip.split_weight_opt")
            _comp_step   = "0.001"
        else:
            _comp_name   = None
            _comp_ph     = None
            _comp_title  = None
            _comp_step   = None

        _batch_qty_title   = t("inv.tooltip.batch_qty").replace("{unit}", sell_by_label)
        _batch_count_title = t("inv.tooltip.batch_count")

        # Combined card: shared split table (Mother + N children) above, Batch Split below.
        split_card = Div(
            _split_table_form(
                split_preview,
                action=f"/api/items/{entity_id}/split-inline",
                target="#item-action-error",
                form_id=f"split-form-{safe_id}",
                allow_add_child=True,
                child_sku_editable=False,
            ),
            Hr(cls="action-card-divider"),
            # Batch Split submits via JS; onsubmit guard blocks accidental Enter submission.
            Form(
                Strong(t("inv.batch_split"), cls="action-card-title", style="margin-bottom:4px"),
                Div(
                    Input(type="number", name="batch_qty",
                          placeholder=f"{sell_by_label} per child",
                          title=_batch_qty_title,
                          step="any", min="0.001",
                          cls="form-input form-input--sm split-qty-main",
                          oninput=f"batchSplitPreview_{safe_id}(this.form)"),
                    *([Input(type="number", name="batch_complement",
                             placeholder=_comp_ph, title=_comp_title,
                             step=_comp_step, min="0",
                             cls="form-input form-input--sm split-complement",
                             oninput=f"batchSplitPreview_{safe_id}(this.form)")]
                      if _comp_name else []),
                    Span("×", cls="batch-split-sep"),
                    Input(type="number", name="batch_count",
                          placeholder="Count",
                          title=_batch_count_title,
                          step="1", min="2",
                          cls="form-input form-input--sm batch-split-count",
                          oninput=f"batchSplitPreview_{safe_id}(this.form)"),
                    Button("Batch", type="button",
                           title=t("inv.tooltip.batch_submit"),
                           cls="btn btn--primary btn--xs btn--batch-submit",
                           onclick=f"batchSplitSubmit_{safe_id}(this.closest('form'))"),
                    cls="action-card-row",
                ),
                Div(id=f"batch-split-preview-{safe_id}", cls="batch-split-preview"),
                Script(f"""
function batchSplitPreview_{safe_id}(form) {{
  var qty   = parseFloat(form.querySelector('[name=batch_qty]').value)     || 0;
  var count = parseInt(form.querySelector('[name=batch_count]').value, 10) || 0;
  var avail = {current_qty};
  var unit  = '{sell_by_label}';
  var el    = document.getElementById('batch-split-preview-{safe_id}');
  if (!el) return;
  if (!qty || !count) {{ el.innerHTML = ''; return; }}
  var total = qty * count;
  var over  = total > avail;
  el.innerHTML = count + ' \u00d7 ' + qty + '\u00a0' + unit
    + ' = ' + total.toFixed(2) + '\u00a0' + unit
    + (over ? ' <span class="batch-split-over">exceeds available (' + avail + ')</span>' : '');
}}
function batchSplitSubmit_{safe_id}(form) {{
  var qty   = parseFloat(form.querySelector('[name=batch_qty]').value);
  var count = parseInt(form.querySelector('[name=batch_count]').value, 10);
  if (!qty || qty <= 0) {{ alert('Enter a valid quantity per child.'); return; }}
  if (!count || count < 2) {{ alert('Count must be at least 2.'); return; }}
  var btn = form.querySelector('.btn--batch-submit');
  if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}
  var compEl = form.querySelector('[name=batch_complement]');
  var vals = {{ batch_qty: qty, batch_count: count }};
  if (compEl && compEl.value) vals.batch_complement = compEl.value;
  htmx.ajax('POST', '/api/items/{entity_id}/batch-split',
    {{ target: '#item-action-error', swap: 'outerHTML', values: vals }});
}}
"""),
                onsubmit="return false;",
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
        id="item-advanced-panel",
        cls="detail-card",
    )


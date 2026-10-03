# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Reusable CSV import UI helpers.

Flow:
  upload (drag-and-drop zone) -> column mapping (optional) -> validate (server-side)
    -> errors: inline-fix panel with editable error cells, fill-down tools,
               error navigation, progress bar, and row numbers
    -> clean:  confirm panel with summary card, data preview table, and "Import All N Rows"

Step indicator tracks progress: Upload -> [Map Columns] -> Review -> Import.

Column mapping: after upload, the user sees every CSV header mapped to a target
field (dropdown).  Exact matches and common aliases are pre-filled.  Unmapped
columns default to "Import as attribute"; the user can also pick "Skip".
"""

from __future__ import annotations

import csv
import json
import logging
import uuid
from typing import Any, Callable

from fasthtml.common import *
from starlette.responses import StreamingResponse
import ui.api_client as api
from celerp.services import import_stage
from ui.i18n import t, get_lang
from ui.components.table import searchable_select

from celerp.importers import tabular
from celerp.importers.tabular import (  # re-exported for the existing CSV importers
    CsvImportSpec,
    MAPPING_ATTR_PREFIX,
    MAPPING_ATTRIBUTE,
    MAPPING_SKIP,
    ValidateFn,
    _IDENTIFIER_COLS,
    _row_errors,
    _rows_to_csv,
    apply_column_mapping,
    apply_fixes_to_rows,
    error_report_csv,
    form_mapping,
    suggest_mapping,
    validate_cell,
    validate_column_mapping,
)

logger = logging.getLogger(__name__)

async def _company_id(token: str) -> str:
    return str((await api.get_company(token)).get("id") or "")


async def stash_import_csv(token: str, csv_text: str) -> str:
    """Stage CSV text for the authenticated company; return its reference.

    The company is checked again once the stage is written: a company reset that
    committed meanwhile has already run its cleanup, so the stage removes itself
    rather than outlive the company. A reset committing after the check finds the
    stage on disk and removes it with the company's other files."""
    company_id = await _company_id(token)
    ref = import_stage.write_stage(company_id, csv_text)
    try:
        still = await _company_id(token)
    except BaseException:
        import_stage.delete_ref(ref)
        raise
    if still != company_id:
        import_stage.delete_ref(ref)
        raise api.APIError(401, "Session expired")
    return ref


async def stage_tabular_upload(token: str, form: Any) -> tuple[list[dict], str, str | None]:
    """Read the uploaded file and stage it for the authenticated company.

    Returns (rows, csv_ref, error). When the stage cannot be written the error
    is a generic message; where staged files live stays in the server log.
    """
    rows, err = await read_tabular_upload(form)
    if err:
        return rows, "", err
    cols = list(rows[0].keys()) if rows else []
    try:
        return rows, await stash_import_csv(token, _rows_to_csv(rows, cols)), None
    except OSError:
        logger.exception("Could not stage an uploaded import file")
        return [], "", t("import.err_stage_unavailable")


async def load_import_csv(token: str, ref: str) -> str | None:
    """Load a stage for the authenticated company. None if invalid, foreign, or expired."""
    if import_stage.stage_paths(ref) is None:
        return None
    return import_stage.read_stage(await _company_id(token), ref)


def import_result_errors(result: dict) -> list[str]:
    """The messages an import result page shows for rows that were not imported.

    Batch endpoints report incomplete success one of two ways: a list of error
    messages, or only a count of failed rows. The messages are shown whenever
    there are any; the count is shown only when it is all the endpoint said.
    """
    errors = [str(e) for e in result.get("errors") or []]
    if errors:
        return errors
    failed = int(result.get("failed", 0) or 0)
    return [t("settings_import.records_failed", n=failed)] if failed else []


async def discard_import_csv(token: str, form, result: dict) -> None:
    """Remove the caller's own stage once its import finished cleanly.

    ``result`` is the terminal import result. A result carrying errors (including
    an API or connection error, whose outcome on the server is unknown) or
    failed rows keeps the stage until it expires, so the user can go back and
    retry the same file.
    """
    if import_result_errors(result):
        return
    ref = str(form.get("csv_ref", "") or "")
    if await load_import_csv(token, ref) is not None:
        import_stage.delete_ref(ref)


async def resolve_import_csv(token: str, form) -> str:
    """CSV text from the staged reference (csv_ref). Empty if missing, invalid, expired, or foreign."""
    csv_ref = str(form.get("csv_ref", "") or "")
    if not csv_ref:
        return ""
    return await load_import_csv(token, csv_ref) or ""


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------


# ── Onboarding entry marker ──────────────────────────────────────────────────
# An import opened from the getting-started hub carries ``?from_onboarding=1``.
# The import page records that in a short-lived cookie (and clears it when opened
# any other way), so the result can offer "Back to setup". The destination is
# always /onboarding; nothing the user supplies becomes a redirect target.

ONBOARDING_MARKER = "from_onboarding"
_ONBOARDING_COOKIE = "celerp_import_from_onboarding"


def onboarding_entry_cookie(request) -> Any:
    """Set-Cookie header recording whether this import page was opened from onboarding."""
    if request.query_params.get(ONBOARDING_MARKER) == "1":
        return cookie(_ONBOARDING_COOKIE, "1", max_age=3600, httponly=True, samesite="lax", path="/")
    return cookie(_ONBOARDING_COOKIE, "", max_age=0, httponly=True, samesite="lax", path="/")


def entered_from_onboarding(request) -> bool:
    return request.cookies.get(_ONBOARDING_COOKIE) == "1"


def _mapping_js_labels() -> dict[str, str]:
    """Translated labels the mapping dropdown JS reads at render time.

    Resolved in Python and handed to the JS as a single ``json.dumps`` config
    object (``_MAP_I18N``); the JS never carries English literals of its own.
    """
    return {
        "skip_badge": t("import.js_skip_badge"),
        "skip_title": t("import.js_skip_title"),
        "custom_badge": t("import.js_custom_badge"),
        "custom_title": t("import.js_custom_title"),
        "matched_title": t("import.js_matched_title"),
        "search_placeholder": t("import.js_search_placeholder"),
        "show_matched": t("import.js_show_matched"),
        "hide_matched": t("import.js_hide_matched"),
        "category_fields": t("import.js_category_fields"),
        "core_fields": t("import.js_core_fields"),
    }


def column_mapping_form(
    *,
    csv_cols: list[str],
    target_cols: list[str],
    csv_ref: str,
    sample_rows: list[dict],
    confirm_action: str,
    back_href: str,
    required_targets: set[str] | None = None,
    category_attrs: list[str] | None = None,
    errors: list[str] | None = None,
    form_values: dict | None = None,
    col_labels: dict[str, str] | None = None,
    mutex_groups: list[list[str]] | None = None,
) -> FT:
    """Render a horizontal spreadsheet-style column mapping UI.

    Each CSV column stays as a visual column with a searchable mapping dropdown
    and 3-5 sample data rows below - matching the user's spreadsheet mental model.
    """
    attrs = category_attrs or []
    suggested = suggest_mapping(csv_cols, target_cols, category_attrs=attrs)
    req = required_targets or set()
    fv = form_values or {}
    preview = sample_rows[:5]
    _col_labels = col_labels or {}
    _mutex_groups = mutex_groups or []

    error_block = ""
    if errors:
        error_items = [Li(e) for e in errors]
        error_block = Div(Ul(*error_items, cls="error-list"), cls="flash flash--error")

    # Build option definitions for JS (shared across all columns)
    # Format: [{value, label, group, required}]
    import json as _json
    option_defs = []
    # Special options first (top of list)
    option_defs.append({"value": MAPPING_ATTRIBUTE, "label": t("import.opt_import_as_custom"), "group": "action"})
    option_defs.append({"value": MAPPING_SKIP, "label": t("import.opt_skip"), "group": "action"})
    # Core fields
    for tc in target_cols:
        label = _col_labels.get(tc) or tc.replace("_", " ").title()
        if tc in req:
            label += " *"
        option_defs.append({"value": tc, "label": label, "group": "core"})
    # Category attribute fields
    for attr_key in attrs:
        attr_val = f"{MAPPING_ATTR_PREFIX}{attr_key}"
        label = attr_key.replace("_", " ").title()
        option_defs.append({"value": attr_val, "label": label, "group": "category"})

    # Build per-column header cells
    mapping_cells = []
    for csv_col in csv_cols:
        target = str(fv.get(f"map__{csv_col}", "")) or suggested.get(csv_col, MAPPING_ATTRIBUTE)
        attr_name = str(fv.get(f"attr_name__{csv_col}", "")) or csv_col
        is_custom = target == MAPPING_ATTRIBUTE

        safe_col = csv_col.replace(" ", "_").replace(".", "_")
        hidden_id = f"map-hidden-{safe_col}"
        attr_input_id = f"attr-name-{safe_col}"
        dropdown_id = f"map-dd-{safe_col}"

        mapping_cells.append(Th(
            Div(
                # Hidden input carries the actual form value
                Input(type="hidden", name=f"map__{csv_col}", id=hidden_id, value=target),
                # Custom searchable dropdown container (built by JS)
                Div(id=dropdown_id, cls="mapping-dropdown",
                    data_col=csv_col, data_value=target),
                Input(
                    type="text",
                    name=f"attr_name__{csv_col}",
                    id=attr_input_id,
                    value=attr_name,
                    placeholder=t("import.custom_field_name_placeholder"),
                    cls="form-input form-input--sm mapping-attr-input",
                    style="" if is_custom else "display:none",
                ),
                # Badge placeholder (managed by JS)
                Span(cls="mapping-badge"),
                cls="mapping-col-header",
            ),
            cls="mapping-th",
        ))

    # CSV header row (original column names, muted)
    header_cells = [Td(Span(col, cls="text-muted"), cls="cell") for col in csv_cols]

    # Data preview rows
    data_rows = []
    for row in preview:
        cells = []
        for col in csv_cols:
            val = str(row.get(col, "")).strip()
            cells.append(Td(val[:60] if val else Span("--", cls="text-muted"), cls="cell"))
        data_rows.append(Tr(*cells, cls="data-row"))

    # Warning about unmapped required fields (strip attr prefix for comparison)
    mapped_targets = set()
    for c in csv_cols:
        v = str(fv.get(f"map__{c}", "")) or suggested.get(c, MAPPING_ATTRIBUTE)
        if v not in (MAPPING_ATTRIBUTE, MAPPING_SKIP):
            mapped_targets.add(v.removeprefix(MAPPING_ATTR_PREFIX) if v.startswith(MAPPING_ATTR_PREFIX) else v)
    missing_required = req - mapped_targets
    warning = ""
    if missing_required:
        names = ", ".join(sorted(missing_required))
        warning = P(
            t("import.required_not_mapped", names=names),
            cls="flash flash--warning",
        )

    row_count = len(sample_rows)
    showing_hint = (
        P(t("import.showing_rows", n=len(preview), total=row_count), cls="import-hint")
        if row_count > 5 else ""
    )

    return Div(
        _step_indicator(2, has_mapping=True),
        H3(t("page.map_columns"), cls="settings-section-title"),
        P(
            t("import.map_intro"),
            cls="import-hint",
        ),
        error_block,
        warning,
        Form(
            Input(type="hidden", name="csv_ref", value=csv_ref),
            Div(
                Table(
                    Thead(Tr(*mapping_cells)),
                    Tbody(
                        Tr(*header_cells, cls="mapping-original-header"),
                        *data_rows,
                    ),
                    cls="data-table column-mapping-table",
                ),
                cls="mapping-scroll-wrapper",
            ),
            showing_hint,
            Div(
                Button(t("btn.continue_to_preview"), type="button", cls="btn btn--primary",
                       data_busy=t("import.processing"),
                       onclick="this.disabled=true;this.textContent=this.dataset.busy;this.classList.add('btn--disabled');this.form.submit()"),
                A(t("btn.cancel"), href=back_href, cls="btn btn--secondary"),
                cls="flex-row gap-sm mt-md",
            ),
            method="post",
            action=confirm_action,
        ),
        Script(f"var _MAPPING_OPTIONS = {_json.dumps(option_defs)};"),
        Script(f"var _MUTEX_GROUPS = {_json.dumps(_mutex_groups)};"),
        Script(f"var _MAP_I18N = {_json.dumps(_mapping_js_labels())};"),
        Script(_MAPPING_JS),
        id="import-preview",
        cls="import-panel",
    )


_MAPPING_JS = """
(function() {
  var ATTR = '__attr__', SKIP = '__skip__';
  var allDropdowns = [];  // track all instances for cross-column awareness

  // Collect all currently selected values across columns (excluding self)
  function getUsedValues(excludeCol) {
    var used = {};
    allDropdowns.forEach(function(dd) {
      if (dd.col === excludeCol) return;
      var v = dd.hiddenInput.value;
      if (v && v !== ATTR && v !== SKIP) used[v] = dd.col;
    });
    return used;
  }

  // Update badge for a dropdown
  function updateBadge(dd) {
    var badge = dd.container.closest('.mapping-col-header').querySelector('.mapping-badge');
    if (!badge) return;
    var v = dd.hiddenInput.value;
    badge.className = 'mapping-badge';
    if (v === SKIP) {
      badge.textContent = _MAP_I18N.skip_badge; badge.classList.add('mapping-badge--skip'); badge.title = _MAP_I18N.skip_title;
    } else if (v === ATTR) {
      badge.textContent = _MAP_I18N.custom_badge; badge.classList.add('mapping-badge--attr'); badge.title = _MAP_I18N.custom_title;
    } else {
      badge.textContent = '\\u2713'; badge.classList.add('mapping-badge--matched'); badge.title = _MAP_I18N.matched_title;
    }
  }

  // Update column dim state
  function updateColumnDim(dd) {
    var th = dd.container.closest('.mapping-th');
    if (!th) return;
    var idx = Array.from(th.parentNode.children).indexOf(th);
    var tbody = th.closest('table').querySelector('tbody');
    if (!tbody) return;
    var isSkip = dd.hiddenInput.value === SKIP;
    tbody.querySelectorAll('tr').forEach(function(tr) {
      var td = tr.children[idx];
      if (td) td.classList.toggle('mapping-col--skipped', isSkip);
    });
  }

  // Update custom name input visibility
  function updateAttrInput(dd) {
    var safe = dd.col.replace(/ /g, '_').replace(/\\./g, '_');
    var inp = document.getElementById('attr-name-' + safe);
    if (inp) inp.style.display = (dd.hiddenInput.value === ATTR) ? '' : 'none';
  }

  // Get display label for a value
  function labelFor(val) {
    for (var i = 0; i < _MAPPING_OPTIONS.length; i++) {
      if (_MAPPING_OPTIONS[i].value === val) return _MAPPING_OPTIONS[i].label;
    }
    return val;
  }

  // Build one searchable dropdown
  function initDropdown(el) {
    var col = el.dataset.col;
    var currentVal = el.dataset.value;
    var safe = col.replace(/ /g, '_').replace(/\\./g, '_');
    var hiddenInput = document.getElementById('map-hidden-' + safe);

    var dd = { col: col, container: el, hiddenInput: hiddenInput, open: false, showMatched: false };
    allDropdowns.push(dd);

    // Trigger button (shows current selection)
    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'mapping-dd-trigger form-input form-input--sm';
    trigger.textContent = labelFor(currentVal);
    el.appendChild(trigger);

    // Dropdown panel
    var panel = document.createElement('div');
    panel.className = 'mapping-dd-panel';
    panel.style.display = 'none';
    el.appendChild(panel);

    // Search input
    var search = document.createElement('input');
    search.type = 'text';
    search.className = 'mapping-dd-search';
    search.placeholder = _MAP_I18N.search_placeholder;
    panel.appendChild(search);

    // Options list
    var optList = document.createElement('div');
    optList.className = 'mapping-dd-options';
    panel.appendChild(optList);

    // "Show already matched" toggle
    var toggleRow = document.createElement('div');
    toggleRow.className = 'mapping-dd-toggle-matched';
    var toggleLink = document.createElement('a');
    toggleLink.href = '#';
    toggleLink.textContent = _MAP_I18N.show_matched;
    toggleRow.appendChild(toggleLink);
    panel.appendChild(toggleRow);

    function renderOptions() {
      optList.innerHTML = '';
      var q = search.value.toLowerCase();
      var used = getUsedValues(col);
      var lastGroup = '';

      _MAPPING_OPTIONS.forEach(function(opt) {
        var isUsed = (opt.value !== ATTR && opt.value !== SKIP && used[opt.value]);
        // If used by another column and not showing matched, hide it
        if (isUsed && !dd.showMatched) return;
        // Search filter
        if (q && opt.label.toLowerCase().indexOf(q) === -1 && opt.value.toLowerCase().indexOf(q) === -1) return;

        // Group separator
        if (opt.group !== lastGroup && lastGroup !== '') {
          var sep = document.createElement('div');
          sep.className = 'mapping-dd-sep';
          if (opt.group === 'category') sep.textContent = _MAP_I18N.category_fields;
          else if (opt.group === 'core') sep.textContent = _MAP_I18N.core_fields;
          optList.appendChild(sep);
        }
        lastGroup = opt.group;

        var item = document.createElement('div');
        item.className = 'mapping-dd-item';
        if (opt.value === hiddenInput.value) item.classList.add('mapping-dd-item--selected');
        if (isUsed) item.classList.add('mapping-dd-item--used');

        var labelSpan = document.createElement('span');
        labelSpan.textContent = opt.label;
        item.appendChild(labelSpan);

        if (isUsed) {
          var usedBy = document.createElement('span');
          usedBy.className = 'mapping-dd-used-by';
          usedBy.textContent = '(' + used[opt.value] + ')';
          item.appendChild(usedBy);
        }

        item.addEventListener('click', function() {
          // If selecting an option used by another column, unmatch that column
          if (isUsed) {
            allDropdowns.forEach(function(other) {
              if (other.col === used[opt.value]) {
                other.hiddenInput.value = ATTR;
                other.container.querySelector('.mapping-dd-trigger').textContent = labelFor(ATTR);
                updateBadge(other);
                updateColumnDim(other);
                updateAttrInput(other);
              }
            });
          }
          hiddenInput.value = opt.value;
          trigger.textContent = opt.label;
          closePanel();
          updateBadge(dd);
          updateColumnDim(dd);
          updateAttrInput(dd);
          // Mutex groups: if selected value is in a group, set all other group members to Skip
          var SKIP = '__skip__';
          if (typeof _MUTEX_GROUPS !== 'undefined') {
            _MUTEX_GROUPS.forEach(function(group) {
              if (group.indexOf(opt.value) !== -1) {
                allDropdowns.forEach(function(other) {
                  if (other !== dd && group.indexOf(other.hiddenInput.value) !== -1) {
                    other.hiddenInput.value = SKIP;
                    other.container.querySelector('.mapping-dd-trigger').textContent = labelFor(SKIP);
                    updateBadge(other);
                    updateColumnDim(other);
                    updateAttrInput(other);
                  }
                });
              }
            });
          }
          // Re-render all other open panels to update used state
          allDropdowns.forEach(function(other) {
            if (other !== dd && other.open) other.render();
          });
        });

        optList.appendChild(item);
      });

      // Show/hide the toggle based on whether there are hidden matched items
      var hasHidden = false;
      if (!dd.showMatched) {
        _MAPPING_OPTIONS.forEach(function(opt) {
          if (opt.value !== ATTR && opt.value !== SKIP && used[opt.value]) hasHidden = true;
        });
      }
      toggleRow.style.display = hasHidden || dd.showMatched ? '' : 'none';
      toggleLink.textContent = dd.showMatched ? _MAP_I18N.hide_matched : _MAP_I18N.show_matched;
    }

    dd.render = renderOptions;

    function positionPanel() {
      var rect = trigger.getBoundingClientRect();
      panel.style.top = rect.bottom + 2 + 'px';
      panel.style.left = rect.left + 'px';
      panel.style.minWidth = Math.max(220, rect.width) + 'px';
      // If panel would overflow below viewport, open upward
      var panelH = panel.offsetHeight || 320;
      if (rect.bottom + panelH + 10 > window.innerHeight) {
        panel.style.top = Math.max(4, rect.top - panelH - 2) + 'px';
      }
    }

    function openPanel() {
      // Close all other panels first
      allDropdowns.forEach(function(other) {
        if (other !== dd && other.open) {
          other.container.querySelector('.mapping-dd-panel').style.display = 'none';
          other.open = false;
        }
      });
      panel.style.display = 'flex';
      dd.open = true;
      search.value = '';
      dd.showMatched = false;
      renderOptions();
      positionPanel();
      search.focus();
    }

    function closePanel() {
      panel.style.display = 'none';
      dd.open = false;
    }

    trigger.addEventListener('mousedown', function(e) { e.stopPropagation(); });
    trigger.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
      if (dd.open) closePanel(); else openPanel();
    });

    search.addEventListener('input', renderOptions);
    search.addEventListener('click', function(e) { e.stopPropagation(); });

    toggleLink.addEventListener('click', function(e) {
      e.preventDefault();
      e.stopPropagation();
      dd.showMatched = !dd.showMatched;
      renderOptions();
    });

    panel.addEventListener('click', function(e) { e.stopPropagation(); });
    panel.addEventListener('mousedown', function(e) { e.stopPropagation(); });

    // Init badge + dim
    updateBadge(dd);
    updateColumnDim(dd);
  }

  // Initialize all dropdowns
  document.querySelectorAll('.mapping-dropdown').forEach(initDropdown);

  // Close panels on outside click or scroll
  function closeAll() {
    allDropdowns.forEach(function(dd) {
      if (dd.open) {
        dd.container.querySelector('.mapping-dd-panel').style.display = 'none';
        dd.open = false;
      }
    });
  }
  document.addEventListener('mousedown', function(e) {
    allDropdowns.forEach(function(dd) {
      if (!dd.open) return;
      var panel = dd.container.querySelector('.mapping-dd-panel');
      var rect = panel.getBoundingClientRect();
      // Coordinate check: covers scrollbar clicks that don't register on the panel element
      if (e.clientX >= rect.left && e.clientX <= rect.right &&
          e.clientY >= rect.top && e.clientY <= rect.bottom) return;
      var trigger = dd.container.querySelector('.mapping-dd-trigger');
      if (trigger && trigger.contains(e.target)) return;
      panel.style.display = 'none';
      dd.open = false;
    });
  });
  document.querySelector('.mapping-scroll-wrapper')?.addEventListener('scroll', function(e) {
    // Only close if scrolling the wrapper itself, not inside a dropdown panel
    allDropdowns.forEach(function(dd) {
      if (!dd.open) return;
      var panel = dd.container.querySelector('.mapping-dd-panel');
      if (panel && panel.contains(e.target)) return;
      panel.style.display = 'none';
      dd.open = false;
    });
  });
  window.addEventListener('scroll', function(e) {
    // Don't close dropdowns when scrolling inside a dropdown panel
    allDropdowns.forEach(function(dd) {
      if (!dd.open) return;
      var panel = dd.container.querySelector('.mapping-dd-panel');
      if (panel && panel.contains(e.target)) return;
      panel.style.display = 'none';
      dd.open = false;
    });
  }, true);
})();
"""



def _step_indicator(current: int, has_mapping: bool = False, *, all_done: bool = False) -> FT:
    """Render the step indicator bar.

    ``current`` is 1-indexed.  Steps without mapping: Upload, Review, Import.
    Steps with mapping: Upload, Map Columns, Review, Import.
    ``all_done`` marks every step as completed (for result panels).
    """
    steps = (
        [t("import.step_upload"), t("import.step_map_columns"), t("inv.review"), t("btn.import")]
        if has_mapping
        else [t("import.step_upload"), t("inv.review"), t("btn.import")]
    )
    parts: list[Any] = []
    for i, label in enumerate(steps, 1):
        if all_done:
            cls = "import-step import-step--done"
        elif i < current:
            cls = "import-step import-step--done"
        elif i == current:
            cls = "import-step import-step--active"
        else:
            cls = "import-step"
        check = "✓" if (all_done or i < current) else str(i)
        parts.append(Span(
            Span(check, cls="import-step-num"),
            label,
            cls=cls,
        ))
        if i < len(steps):
            parts.append(Span("→", cls="import-step-arrow"))
    return Div(*parts, cls="import-steps")


_DROPZONE_JS = """
(function(){
  var dz=document.getElementById('import-dropzone');
  var fi=document.getElementById('csv_file');
  var info=document.getElementById('dropzone-file-info');
  var previewBtn=document.getElementById('csv-preview-btn');
  if(!dz||!fi) return;
  dz.addEventListener('click',function(){fi.click()});
  dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('import-dropzone--dragover')});
  dz.addEventListener('dragleave',function(){dz.classList.remove('import-dropzone--dragover')});
  dz.addEventListener('drop',function(e){
    e.preventDefault();dz.classList.remove('import-dropzone--dragover');
    if(e.dataTransfer.files.length){fi.files=e.dataTransfer.files;_showFile(fi.files[0])}
  });
  fi.addEventListener('change',function(){if(fi.files.length) _showFile(fi.files[0])});
  function _showFile(f){
    var kb=(f.size/1024).toFixed(1);
    info.textContent=f.name+' ('+kb+' KB)';
    info.style.display='inline-flex';
    if(previewBtn){
      previewBtn.disabled=false;
      previewBtn.classList.remove('btn--disabled');
      previewBtn.classList.add('btn--ready');
      previewBtn.title='';
    }
  }
})();
"""


def _sheet_picker(sheets: list[str] | None) -> FT | str:
    if not sheets:
        return ""
    return Label(
        t("import.sheet_label"),
        searchable_select("sheet", sheets, aria_label=t("import.sheet_label")),
        cls="form-label",
        style="display:block; margin-bottom: 12px;",
    )


def upload_form(
    *,
    cols: list[str] | None = None,
    template_href: str,
    preview_action: str,
    error: str | None = None,
    hint: str | None = None,
    has_mapping: bool = False,
) -> FT:
    return Div(
        _step_indicator(1, has_mapping=has_mapping),
        P(error, cls="flash flash--error") if error else "",
        P(hint, cls="form-hint", style="margin:0 0 8px") if hint else "",
        Form(
            Input(type="file", id="csv_file", name="csv_file", accept=".csv,.xlsx",
                  required=True, style="display:none"),
            _sheet_picker(getattr(error, "sheets", None)),
            Div(
                Div("📄", cls="import-dropzone-icon"),
                Div(t("msg.drag_your_file_here_or_click_to_browse"), cls="import-dropzone-text"),
                Div(t("msg.accepted_formats_import"), cls="import-dropzone-hint"),
                Span(id="dropzone-file-info", cls="import-dropzone-file", style="display:none"),
                Div(
                    A(t("btn.download_template"), href=template_href, cls="link",
                      onclick="event.stopPropagation()"),
                    cls="import-dropzone-template",
                ),
                id="import-dropzone",
                cls="import-dropzone",
            ),
            Button(t("btn.preview"), id="csv-preview-btn", cls="btn btn--primary btn--disabled", type="submit", disabled=True, title=t("import.upload_first_hint"), style="margin-top: 12px;"),
            method="post",
            action=preview_action,
            enctype="multipart/form-data",
        ),
        Script(_DROPZONE_JS),
        cls="import-panel",
    )


class UploadError(str):
    """An upload error message. ``sheets`` lists the workbook sheets to choose
    from when the file had more than one sheet with data."""

    sheets: list[str]

    def __new__(cls, message: str, sheets: list[str] | None = None) -> "UploadError":
        obj = super().__new__(cls, message)
        obj.sheets = list(sheets or [])
        return obj


async def read_tabular_upload(form: Any) -> tuple[list[dict], str | None]:
    """Return (rows, error) for an uploaded .csv or .xlsx file.

    Both formats go through ``tabular.read_table``, so a workbook and the same
    data saved as CSV yield identical rows. A workbook with several sheets that
    hold data is never read until the user picks one (``sheet`` form field); the
    error then carries the sheet names so the upload form can offer them.
    """
    file_obj = form.get("csv_file")
    if not file_obj or not hasattr(file_obj, "read"):
        return [], t("import.err_select_file")
    filename = getattr(file_obj, "filename", None) or "upload.csv"
    sheet = (form.get("sheet") or "").strip() or None
    try:
        content = await tabular.read_upload_bytes(file_obj)
        fieldnames, rows = tabular.read_table(content, filename, sheet=sheet)
    except UnicodeDecodeError:
        return [], t("import.err_decode")
    except csv.Error:
        return [], t("import.err_parse")
    except tabular.TabularError as exc:
        if exc.sheets:
            return [], UploadError(t("import.err_choose_sheet"), sheets=exc.sheets)
        if exc.code == "extra_columns":
            return [], t("import.err_extra_columns")
        if exc.code == "no_header":
            return [], t("import.err_no_header")
        return [], t("import.err_read_file", detail=str(exc))
    names = [str(f or "").strip() for f in fieldnames]
    if not names or "" in names:
        return [], t("import.err_no_header")
    if not rows:
        return [], t("import.err_empty")
    return rows, None


# ── Inline fix error panel ────────────────────────────────────────────────────

_INLINE_FIX_JS = """
(function() {
  window.csvFillColumn = function(col) {
    var input = document.getElementById('fill-' + col);
    if (!input) return;
    var val = input.value;
    if (!val) return;
    document.querySelectorAll('[data-col="' + col + '"]').forEach(function(el) {
      el.value = val;
      el.classList.remove('input--error');
    });
  };

  // Serialize all editable cells into fixes_json before htmx sends the form.
  // htmx does not fire the native 'submit' event; use htmx:configRequest instead.
  // This keeps the field count at 2 (csv_ref + fixes_json) regardless of
  // how many error cells exist, avoiding Starlette's max_fields=1000 limit.
  document.addEventListener('htmx:configRequest', function(e) {
    var form = e.detail.elt;
    if (!form || !form.querySelector('input[name="fixes_json"]')) return;
    var fixes = {};
    form.querySelectorAll('[data-row][data-col]').forEach(function(el) {
      fixes[el.dataset.row + '__' + el.dataset.col] = el.value;
    });
    e.detail.parameters['fixes_json'] = JSON.stringify(fixes);
  });

  // "Add new unit" option in unit dropdowns: open settings page and return.
  // Keep original value in the cell (do not reset to '') so fixes_json preserves
  // the bad value on revalidate. Auto-revalidate when tab regains visibility so
  // the freshly added unit appears as a valid option without a manual click.
  document.addEventListener('change', function(e) {
    if (e.target.matches('select.cell-edit') && e.target.value === '__add_new__') {
      var origVal = e.target.getAttribute('data-prev') || '';
      // Restore original value before opening new tab
      e.target.value = origVal;
      window.open('/settings/inventory?tab=units&from_import=1', '_blank');
      // When user returns to this tab, auto-revalidate so new units appear in dropdowns.
      var revalidateFired = false;
      document.addEventListener('visibilitychange', function _onVisible() {
        if (document.visibilityState !== 'visible' || revalidateFired) return;
        revalidateFired = true;
        document.removeEventListener('visibilitychange', _onVisible);
        var btn = document.querySelector('.csv-fix-actions button[type="submit"]');
        if (btn) btn.click();
      });
    }
  });

  // Track previous select values so bulk-sync and add-new can reference them.
  document.addEventListener('focus', function(e) {
    if (e.target.matches('select.cell-edit')) {
      e.target.setAttribute('data-prev', e.target.value);
    }
  }, true);

  // When a cell-edit select value changes, sync all other cells in the same
  // column that still hold the same old value (bulk-update identical bad values).
  document.addEventListener('change', function(e) {
    if (!e.target.matches('select.cell-edit') || e.target.value === '__add_new__') return;
    var col = e.target.getAttribute('data-col');
    var newVal = e.target.value;
    var oldVal = e.target.getAttribute('data-prev');
    if (!col || !oldVal || oldVal === newVal) return;
    document.querySelectorAll('select.cell-edit[data-col="' + col + '"]').forEach(function(el) {
      if (el !== e.target && el.value === oldVal) {
        el.value = newVal;
        el.classList.remove('input--error');
      }
    });
    // Update data-prev to new value after sync
    e.target.setAttribute('data-prev', newVal);
  });
})();
"""

_INLINE_FIX_CSS = """
<style>
.csv-fix-panel { margin-top: 12px; }
.csv-fix-summary { font-size: 13px; margin-bottom: 12px; }
.csv-fix-summary strong { color: var(--c-orange, #f59e0b); }
.csv-fill-bars { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px;
  padding: 10px 12px; background: var(--c-bg2); border-radius: var(--radius);
  border: 1px solid var(--c-border); }
.csv-fill-bar { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.csv-fill-bar label { min-width: 120px; font-weight: 500; }
.csv-fill-bar input { flex: 1; max-width: 200px; }
.csv-fix-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.csv-fix-table th { text-align: center; padding: 6px 8px; font-size: 11px;
  color: var(--c-text2); border-bottom: 1px solid var(--c-border); }
.csv-fix-table td { padding: 4px 6px; border-bottom: 1px solid var(--c-border); }
.csv-fix-table .cell-ro { color: var(--c-text2); font-size: 11px; }
.csv-fix-table input.cell-edit { width: 100%; box-sizing: border-box; padding: 3px 6px;
  font-size: 12px; border: 1px solid var(--c-border); border-radius: var(--radius); }
.csv-fix-table select.cell-edit { width: 100%; box-sizing: border-box; padding: 3px 6px;
  font-size: 12px; border: 1px solid var(--c-border); border-radius: var(--radius); }
.csv-fix-table input.input--error { border-color: var(--c-red, #ef4444);
  background: rgba(239,68,68,0.06); }
.csv-fix-table select.input--error { border-color: var(--c-red, #ef4444);
  background: rgba(239,68,68,0.06); }
.csv-ok-count { font-size: 12px; color: var(--c-text2); margin-top: 6px; }
.csv-fix-actions { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
</style>
"""


def _fix_errors_panel(
    rows: list[dict],
    cols: list[str],
    validate: ValidateFn,
    error_row_indices: list[int],
    error_cols: set[str],
    total_errors: int,
    csv_ref: str,
    revalidate_action: str,
    error_report_action: str,
    back_href: str,
    has_mapping: bool = False,
    cell_renderers: dict[str, "Callable[[str, int, dict, bool], FT]"] | None = None,
) -> FT:
    """Inline-fix error panel: editable error cells + fill-all bars."""
    ok_count = len(rows) - len(error_row_indices)
    visible_cols = [c for c in cols if c in _IDENTIFIER_COLS or c in error_cols]
    review_step = 3 if has_mapping else 2

    # Progress bar percentage
    pct = round(ok_count / len(rows) * 100) if rows else 0

    # Per-column error counts
    col_error_counts: dict[str, int] = {}
    for col in cols:
        if col not in error_cols:
            continue
        col_error_counts[col] = sum(
            1 for ri in error_row_indices
            if not validate(col, str(rows[ri].get(col, "")), rows[ri])
        )

    # Fill-all bars for error columns with many errors (>1 error row)
    fill_bars = []
    for col in cols:
        if col not in error_cols:
            continue
        col_error_count = col_error_counts.get(col, 0)
        if col_error_count > 1:
            label = col.replace("_", " ").title()
            renderer = (cell_renderers or {}).get(col)
            if renderer:
                # Reuse the per-cell renderer for the fill widget. ri=-1 marks it as
                # the fill source (not a target cell). Inject id so csvFillColumn can
                # read the selected value via document.getElementById('fill-{col}').
                fill_widget = renderer("", -1, {}, False)
                fill_widget.attrs["id"] = f"fill-{col}"
            else:
                fill_widget = Input(
                    type="text", id=f"fill-{col}",
                    placeholder=t("import.fill_placeholder", label=label),
                    cls="form-input form-input--sm",
                )
            fill_bars.append(Div(
                Label(f"{label} " + t("import.n_rows_paren", n=col_error_count), _for=f"fill-{col}"),
                fill_widget,
                Button(t("btn.apply"),
                    type="button",
                    onclick=f"csvFillColumn({json.dumps(col)})",
                    cls="btn btn--secondary btn--xs",
                ),
                cls="csv-fill-bar",
            ))

    fill_section = ""
    if fill_bars:
        fill_section = Details(
            Summary(t("msg.bulk_fix_tools")),
            Div(
                P(t("msg.fill_entire_columns_at_once"), cls="form-hint", style="margin:0 0 6px"),
                *fill_bars,
                cls="csv-fill-bars",
            ),
            cls="mt-sm",
        )

    # Table: identifier cols read-only, error cols editable
    # Row number + error badge headers
    _COL_TOOLTIPS: dict[str, str] = {
        "name": t("import.tooltip_name"),
        "sell_by": t("import.tooltip_sell_by"),
    }

    header_cells = [Th("#", cls="csv-th")]
    for col in visible_cols:
        label = col.replace("_", " ").title()
        is_err_col = col in error_cols
        badge = t("import.n_errors_paren", n=col_error_counts.get(col, 0)) if is_err_col else ""
        tooltip = _COL_TOOLTIPS.get(col)
        th_content: Any = (
            Span(
                f"{label}{badge}",
                title=tooltip,
                style="cursor:help;border-bottom:1px dotted currentColor",
            )
            if tooltip
            else f"{label}{badge}"
        )
        header_cells.append(Th(th_content, cls="csv-th--error" if is_err_col else ""))

    body_rows = []
    for err_idx, ri in enumerate(error_row_indices):
        row = rows[ri]
        bad = set(_row_errors(row, cols, validate))
        cells = [Td(str(ri + 1), cls="cell-ro")]  # 1-indexed row number
        for col in visible_cols:
            val = str(row.get(col, ""))
            if col in error_cols:
                is_bad = col in bad
                err_cls = f"cell-edit{'  input--error' if is_bad else ''}"
                renderer = (cell_renderers or {}).get(col)
                if renderer:
                    cells.append(Td(renderer(val, ri, row, is_bad)))
                else:
                    cells.append(Td(Input(
                        type="text",
                        value=val,
                        data_col=col,
                        data_row=str(ri),
                        cls=err_cls,
                    )))
            else:
                cells.append(Td(val, cls="cell-ro"))
        body_rows.append(Tr(*cells, cls="data-row"))

    # Error navigation JS. The "{pos} of {total}" counter text comes from the
    # label's data-fmt attribute (translated in Python), never spliced into JS.
    err_nav_js = """
(function(){
  var pos=0,rows=document.querySelectorAll('.csv-fix-table .data-row');
  var total=rows.length,lbl=document.getElementById('err-pos-label');
  var fmt=(lbl&&lbl.dataset.fmt)||'{pos} of {total}';
  function fmtLabel(p,n){return fmt.replace('{pos}',p).replace('{total}',n)}
  function go(d){if(!total)return;pos=((pos+d)%total+total)%total;rows[pos].scrollIntoView({block:'center'});if(lbl)lbl.textContent=fmtLabel(pos+1,total)}
  window.errPrev=function(){go(-1)};window.errNext=function(){go(1)};
  if(lbl) lbl.textContent=fmtLabel(1,total);
})();
"""

    return Div(
        NotStr(_INLINE_FIX_CSS),
        _step_indicator(review_step, has_mapping=has_mapping),
        Div(
            P(
                Strong(t("import.cells_count", n=total_errors)),
                t("import.need_fixing_across", rows=len(error_row_indices), total=len(rows)),
                cls="csv-fix-summary",
            ),
            Div(
                Div(style=f"width:{pct}%", cls="import-progress-fill"),
                cls="import-progress",
            ),
            P(t("import.rows_valid", ok=ok_count, total=len(rows)), cls="csv-ok-count"),
            Div(
                Span(id="err-pos-label", cls="import-err-pos", data_fmt=t("import.err_pos_fmt")),
                Button(t("btn.prev"), type="button", onclick="errPrev()", cls="btn btn--ghost btn--xs"),
                Button(t("btn.next"), type="button", onclick="errNext()", cls="btn btn--ghost btn--xs"),
                cls="import-err-nav",
            ) if error_row_indices else "",
            fill_section,
            Form(
                Input(type="hidden", name="csv_ref", value=csv_ref),
                Input(type="hidden", name="fixes_json", value=""),
                Table(
                    Thead(Tr(*header_cells)),
                    Tbody(*body_rows),
                    cls="csv-fix-table data-table",
                ),
                Div(
                    Button(t("btn.fix_import"),
                        type="submit",
                        cls="btn btn--primary",
                        hx_disabled_elt="this",
                    ),
                    A(t("msg.download_error_report"), href="#",
                      onclick="document.getElementById('csv-err-dl').submit(); return false",
                      cls="btn btn--secondary btn--sm"),
                    A(t("btn.cancel"), href=back_href, cls="btn btn--ghost btn--sm"),
                    cls="csv-fix-actions",
                ),
                hx_post=revalidate_action,
                hx_target="#import-preview",
                hx_swap="outerHTML",
            ),
            # Hidden form for error report download (separate from fix form)
            Form(
                Input(type="hidden", name="csv_ref", value=csv_ref),
                id="csv-err-dl",
                method="post",
                action=error_report_action,
                style="display:none",
            ),
            cls="csv-fix-panel",
        ),
        Script(_INLINE_FIX_JS),
        Script(NotStr(err_nav_js)),
        id="import-preview",
        cls="import-panel",
    )


def rows_have_errors(rows: list[dict], cols: list[str], validate: ValidateFn) -> bool:
    """True when any cell fails ``validate`` (the fix-errors panel would show)."""
    return any(_row_errors(row, cols, validate) for row in rows)


def validation_result(
    *,
    rows: list[dict],
    cols: list[str],
    validate: ValidateFn,
    csv_ref: str,
    confirm_action: str,
    error_report_action: str,
    back_href: str,
    revalidate_action: str = "",
    has_mapping: bool = False,
    upsert_label: str | None = None,
    cell_renderers: dict[str, "Callable[[str, int, dict, bool], FT]"] | None = None,
    notes: Any = "",
    ready: int | None = None,
) -> FT:
    """Return the post-upload panel: inline-fix error panel or clean confirm panel.

    If ``revalidate_action`` is provided and there are errors, users can fix
    cells on-screen and submit to that endpoint. Otherwise falls back to
    download-only error report.

    If ``upsert_label`` is provided, a checkbox is shown above the import
    button letting users opt-in to updating existing records. ``notes`` are shown
    on the confirm panel above the preview table; ``ready`` is how many rows the
    import will add, when the server's preview says fewer than every row.
    """
    error_pairs = [(i, _row_errors(row, cols, validate)) for i, row in enumerate(rows)]
    error_row_indices = [i for i, errs in error_pairs if errs]
    total_errors = sum(len(errs) for _, errs in error_pairs)
    error_cols: set[str] = {col for _, errs in error_pairs for col in errs}


    if error_row_indices:
        return _fix_errors_panel(
            rows=rows,
            cols=cols,
            validate=validate,
            error_row_indices=error_row_indices,
            error_cols=error_cols,
            total_errors=total_errors,
            csv_ref=csv_ref,
            revalidate_action=revalidate_action or error_report_action,
            error_report_action=error_report_action,
            back_href=back_href,
            has_mapping=has_mapping,
            cell_renderers=cell_renderers,
        )

    # Clean - confirm panel with preview table
    return _confirm_panel(
        rows=rows,
        cols=cols,
        hidden={"csv_ref": csv_ref},
        confirm_action=confirm_action,
        back_href=back_href,
        has_mapping=has_mapping,
        upsert_control=_upsert_control(upsert_label) if upsert_label else "",
        notes=notes,
        ready=ready,
    )


def _preview_table(rows: list[dict], cols: list[str]) -> FT:
    """First 5 rows, values truncated to 40 chars, with a 'showing n of total' note."""
    n = len(rows)
    preview_rows = rows[:5]
    if not preview_rows:
        return ""
    return Div(
        Table(
            Thead(Tr(*[Th(c.replace("_", " ").title()) for c in cols])),
            Tbody(*[Tr(*[Td(str(row.get(c, ""))[:40]) for c in cols]) for row in preview_rows]),
            cls="data-table import-preview-table",
        ),
        P(t("import.showing_rows", n=len(preview_rows), total=n), cls="import-hint") if n > 5 else "",
    )


def _upsert_control(upsert_label: str, *, checked: bool = False, review_action: str = "") -> FT:
    """The 'Update existing records' checkbox and its matching hint.

    With ``review_action`` a change re-runs the review for the new choice, so the
    confirmation always matches what will be imported.
    """
    review_attrs = (
        {"hx_post": review_action, "hx_trigger": "change", "hx_include": "closest form",
         "hx_target": "#import-preview", "hx_swap": "outerHTML"}
        if review_action else {}
    )
    return Div(
        Label(
            Input(type="checkbox", name="upsert", value="1", checked=checked, **review_attrs),
            " ",
            t("import.update_existing_records"),
            cls="flex-row gap-sm",
            style="align-items:center;cursor:pointer;",
        ),
        Span(
            t("import.upsert_hint", label=upsert_label),
            cls="import-hint",
            style="display:block;margin-top:4px;",
        ),
        cls="mt-sm mb-sm",
    )


def _confirm_panel(
    *,
    rows: list[dict],
    cols: list[str],
    hidden: dict[str, str],
    confirm_action: str,
    back_href: str,
    has_mapping: bool,
    upsert_control: Any = "",
    notes: Any = "",
    ready: int | None = None,
) -> FT:
    """Rows-ready summary, preview table, and the single import button."""
    review_step = 3 if has_mapping else 2
    n = len(rows) if ready is None else ready
    return Div(
        _step_indicator(review_step, has_mapping=has_mapping),
        Div(
            Div(
                Div(
                    Div(f"{n}", cls="import-card-value"),
                    Div(t("msg.rows_ready"), cls="import-card-label"),
                    cls="import-card import-card--success",
                ),
                cls="import-summary-cards",
            ),
            notes,
            _preview_table(rows, cols),
            Form(
                *[Input(type="hidden", name=k, value=v) for k, v in hidden.items()],
                upsert_control,
                Button(
                    t("import.import_all_rows", n=n),
                    cls="btn btn--primary",
                    type="submit",
                    hx_post=confirm_action,
                    hx_target="#import-preview",
                    hx_swap="outerHTML",
                    hx_disabled_elt="this",
                    hx_indicator="#import-spinner",
                ),
                Span(
                    Div(cls="spinner"),
                    " ",
                    t("import.importing"),
                    id="import-spinner",
                    cls="htmx-indicator import-spinner-label",
                ),
                A(t("btn.cancel"), href=back_href, cls="btn btn--secondary"),
                cls="flex-row gap-sm mt-md",
            ),
            cls="import-panel",
        ),
        id="import-preview",
    )


_REVIEW_ERROR_LIMIT = 50


def semantic_review_panel(
    *,
    rows: list[dict],
    cols: list[str],
    csv_ref: str,
    upsert: bool,
    upsert_label: str,
    errors: list[dict],
    locations_to_create: list[str],
    preview_hash: str,
    review_action: str,
    confirm_action: str,
    upload_href: str,
    back_href: str,
    notice: str = "",
) -> FT:
    """Final review of rows that passed the cell checks, from the server's preview.

    Row errors (``{"row", "field", "message"}``) block the import and are listed;
    a clean preview shows the import button carrying ``preview_hash``, so the
    server imports exactly what was reviewed. Changing 'Update existing records'
    re-runs the review.
    """
    upsert_control = _upsert_control(upsert_label, checked=upsert, review_action=review_action)
    notice_el = P(notice, cls="flash flash--warning") if notice else ""
    if not errors:
        hidden = {"csv_ref": csv_ref, "preview_hash": preview_hash}
        notes = Div(
            notice_el,
            P(t("import.locations_to_create", names=", ".join(locations_to_create)), cls="import-hint")
            if locations_to_create else "",
        )
        return _confirm_panel(
            rows=rows, cols=cols, hidden=hidden, confirm_action=confirm_action,
            back_href=back_href, has_mapping=True, upsert_control=upsert_control, notes=notes,
        )

    shown = errors[:_REVIEW_ERROR_LIMIT]
    return Div(
        _step_indicator(3, has_mapping=True),
        Div(
            notice_el,
            Div(
                Div(
                    Div(str(len({e.get("row") for e in errors})), cls="import-card-value"),
                    Div(t("import.rows_need_changes"), cls="import-card-label"),
                    cls="import-card import-card--error",
                ),
                cls="import-summary-cards",
            ),
            P(t("import.review_fix_in_file"), cls="import-hint"),
            Table(
                Thead(Tr(Th(t("import.col_row")), Th(t("import.col_field")), Th(t("import.col_problem")))),
                Tbody(*[
                    Tr(Td(str(e.get("row", ""))), Td(str(e.get("field", ""))), Td(str(e.get("message", ""))))
                    for e in shown
                ]),
                cls="data-table import-preview-table",
            ),
            P(t("import.showing_errors", n=len(shown), total=len(errors)), cls="import-hint")
            if len(errors) > len(shown) else "",
            Form(
                Input(type="hidden", name="csv_ref", value=csv_ref),
                upsert_control,
                Div(
                    A(t("import.upload_corrected_file"), href=upload_href, cls="btn btn--primary"),
                    A(t("btn.cancel"), href=back_href, cls="btn btn--secondary"),
                    cls="flex-row gap-sm mt-md",
                ),
            ),
            cls="import-panel",
        ),
        id="import-preview",
    )


def error_report_response(
    rows: list[dict],
    cols: list[str],
    validate: ValidateFn,
    filename: str = "import_errors.csv",
) -> StreamingResponse:
    """StreamingResponse that downloads the error report CSV."""
    content = error_report_csv(rows, cols, validate)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def import_abort_panel(
    *,
    message: str,
    import_more_href: str,
    back_href: str,
    has_mapping: bool = False,
) -> FT:
    """Step-aware error panel for hard aborts (e.g. missing sell_by, location conflicts).

    Keeps the journey map visible and gives the user a way forward.
    ``back_href`` links to the entity list so the user can return without importing more.
    """
    review_step = 3 if has_mapping else 2
    return Div(
        _step_indicator(review_step, has_mapping=has_mapping),
        P(message, cls="flash flash--error"),
        Div(
            A(t("msg.import_more"), href=import_more_href, cls="btn btn--secondary"),
            A(t("btn.cancel"), href=back_href, cls="btn btn--ghost"),
            cls="flex-row gap-sm mt-md",
        ),
        id="import-preview",
    )


async def import_numbered(token: str, resource: str, records: list[dict], number_field: str, prefix: str,
                          *, upsert: bool) -> dict:
    """Send ``records`` to the ``resource`` batch import, each under the id of the one existing
    record with exactly its number, or a new id when there is none, so a re-imported row updates
    the record it matched. A row whose number several records already share is reported as an
    error and not sent."""
    sendable: list[dict] = []
    errors: list[str] = []
    for rec in records:
        number = rec["data"][number_field]
        ids = await api.numbered_ids(token, resource, number, rec["data"].get("doc_type"))
        if len(ids) > 1:
            errors.append(t("msg.import_number_shared", number=number, count=len(ids)))
            continue
        rec["entity_id"] = ids[0] if ids else f"{prefix}:{uuid.uuid4()}"
        sendable.append(rec)
    result = (await api.batch_import(token, f"/{resource}/import/batch", sendable, upsert=upsert)
              if sendable else {"created": 0, "skipped": 0})
    return {**result, "errors": errors + list(result.get("errors") or [])}


def import_result_panel(
    *,
    created: int,
    skipped: int,
    errors: list[str],
    entity_label: str,
    back_href: str,
    import_more_href: str,
    error_details: list[str] | None = None,
    has_mapping: bool = False,
    extra: Any = "",
    updated: int = 0,
    from_onboarding: bool = False,
) -> FT:
    """Shared import result panel with summary cards.

    ``extra`` is an optional FT element inserted after the summary cards
    (e.g. schema-merge info for inventory).
    ``updated`` shows a blue "Updated" card when > 0 (upsert mode).
    ``from_onboarding`` adds "Back to setup" and keeps "Import more" in onboarding.
    """
    cards = [
        Div(
            Div(str(created), cls="import-card-value"),
            Div(t("msg.created"), cls="import-card-label"),
            cls="import-card import-card--success",
        ),
    ]
    if updated > 0:
        cards.append(Div(
            Div(str(updated), cls="import-card-value"),
            Div(t("msg.updated"), cls="import-card-label"),
            cls="import-card import-card--info",
        ))
    cards.append(Div(
        Div(str(skipped), cls="import-card-value"),
        Div(t("msg.skipped"), cls="import-card-label"),
        cls="import-card import-card--warning",
    ))
    if errors:
        cards.append(Div(
            Div(str(len(errors)), cls="import-card-value"),
            Div(t("msg.errors"), cls="import-card-label"),
            cls="import-card import-card--error",
        ))

    details = error_details or errors
    error_block: Any = ""
    if details:
        error_block = Details(
            Summary(t("import.error_details", n=len(details))),
            *(P(e) for e in details[:10]),
            cls="mt-sm",
        )

    label_title = entity_label.title()
    return Div(
        _step_indicator(1, has_mapping=has_mapping, all_done=True),
        Div(*cards, cls="import-summary-cards"),
        extra,
        error_block,
        Div(
            A(t("import.view_entity", label=label_title), href=back_href, cls="btn btn--primary"),
            A(t("import.back_to_setup"), href="/onboarding", cls="btn btn--secondary") if from_onboarding else "",
            A(t("msg.import_more"),
              href=f"{import_more_href}?{ONBOARDING_MARKER}=1" if from_onboarding else import_more_href,
              cls="btn btn--secondary"),
            cls="flex-row gap-sm mt-md",
        ),
        id="import-preview",
    )

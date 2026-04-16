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
import hashlib
import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fasthtml.common import *
from starlette.responses import StreamingResponse
from ui.i18n import t, get_lang


ValidateFn = Callable[[str, str], bool]

# Server-side CSV stash: store uploaded CSV data in temp files, keyed by hash.
# Avoids round-tripping large CSV data through hidden form fields (Starlette
# enforces a 1 MB multipart field limit that breaks large imports).
_CSV_STASH_DIR = Path(tempfile.gettempdir()) / "celerp_csv_stash"


def _stash_csv(csv_text: str) -> str:
    """Write CSV text to a temp file and return a short reference token."""
    _CSV_STASH_DIR.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(csv_text.encode()).hexdigest()[:16]
    (_CSV_STASH_DIR / token).write_text(csv_text, encoding="utf-8")
    return token


def _load_csv(token: str) -> str | None:
    """Load stashed CSV text by token. Returns None if expired/missing."""
    path = _CSV_STASH_DIR / token
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def parse_csv_from_stash_or_field(form: dict) -> tuple[list[dict], list[str]] | None:
    """Retrieve CSV rows+cols from a stash token or inline csv_data field.

    Returns (rows, cols) or None if neither source is available.
    """
    csv_text = _resolve_csv_text(form)
    if not csv_text:
        return None
    reader = csv.DictReader(io.StringIO(csv_text))
    cols = reader.fieldnames or []
    rows = list(reader)
    return rows, list(cols)


def _resolve_csv_text(form: dict) -> str:
    """Get CSV text from stash token (csv_ref) or inline field (csv_data)."""
    csv_ref = str(form.get("csv_ref", "") or "")
    if csv_ref:
        text = _load_csv(csv_ref)
        if text:
            return text
    return str(form.get("csv_data", "") or "")

# Columns always shown in the error table (identifiers), even if they have no errors.
_IDENTIFIER_COLS = {"sku", "name", "id", "email", "code"}


@dataclass(frozen=True)
class CsvImportSpec:
    cols: list[str]
    required: set[str]
    type_map: dict[str, Callable[[str], Any]]


def validate_cell(spec: CsvImportSpec, col: str, value: str) -> bool:
    if col in spec.required and not value.strip():
        return False
    cast = spec.type_map.get(col)
    if cast and value.strip():
        try:
            cast(value)
        except (ValueError, TypeError):
            return False
    return True


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

# Common aliases: CSV header (lowercase) -> Celerp target field.
# Used to pre-fill the mapping dropdown. Not auto-committed - user always sees
# and confirms the suggestion.
_COMMON_ALIASES: dict[str, str] = {
    "item_type": "category",
    "type": "category",
    "product_type": "category",
    "price": "retail_price",
    "selling_price": "retail_price",
    "sale_price": "retail_price",
    "cost": "cost_price",
    "unit_cost": "cost_price",
    "purchase_price": "cost_price",
    "wholesale": "wholesale_price",
    "weight_ct": "weight",
    "weight_g": "weight",
    "location": "location_name",
    "warehouse": "location_name",
    "upc": "barcode",
    "ean": "barcode",
    "isbn": "barcode",
    "code": "sku",
    "item_code": "sku",
    "product_code": "sku",
    "product_name": "name",
    "item_name": "name",
    "title": "name",
    "desc": "description",
    "qty": "quantity",
    "stock": "quantity",
    "on_hand": "quantity",
}

# Sentinel values for the mapping dropdown
MAPPING_ATTRIBUTE = "__attr__"
MAPPING_SKIP = "__skip__"
MAPPING_ATTR_PREFIX = "__catattr:"  # Category attribute: "__catattr:stone_type"


def suggest_mapping(
    csv_cols: list[str],
    target_cols: list[str],
    category_attrs: list[str] | None = None,
) -> dict[str, str]:
    """Return {csv_col: suggested_target} for each CSV column.

    Priority:
    1. Exact match (case-insensitive) to a core target column
    2. Known alias match to a core target column
    3. Exact match to a category attribute key (prefixed with 'attr:')
    4. Default to MAPPING_ATTRIBUTE (import as custom field)

    Each target field is claimed at most once (first match wins).
    Category attributes use 'attr:<key>' as the mapping value.
    """
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    target_lower = {item.lower(): item for item in target_cols}
    attrs = category_attrs or []
    attr_lower = {a.lower().replace(" ", "_"): a for a in attrs}

    # Pass 1: exact matches to core fields
    for csv_col in csv_cols:
        lc = csv_col.lower().strip()
        if lc in target_lower and target_lower[lc] not in claimed:
            mapping[csv_col] = target_lower[lc]
            claimed.add(target_lower[lc])

    # Pass 2: alias matches to core fields
    for csv_col in csv_cols:
        if csv_col in mapping:
            continue
        lc = csv_col.lower().strip()
        alias_target = _COMMON_ALIASES.get(lc)
        if alias_target and alias_target in target_lower.values() and alias_target not in claimed:
            mapping[csv_col] = alias_target
            claimed.add(alias_target)

    # Pass 3: match to category attribute keys
    claimed_attrs: set[str] = set()
    for csv_col in csv_cols:
        if csv_col in mapping:
            continue
        lc = csv_col.lower().strip().replace(" ", "_")
        if lc in attr_lower and attr_lower[lc] not in claimed_attrs:
            mapping[csv_col] = f"{MAPPING_ATTR_PREFIX}{attr_lower[lc]}"
            claimed_attrs.add(attr_lower[lc])

    # Pass 4: everything else defaults to custom
    for csv_col in csv_cols:
        if csv_col not in mapping:
            mapping[csv_col] = MAPPING_ATTRIBUTE

    return mapping


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

    error_block = ""
    if errors:
        error_items = [Li(e) for e in errors]
        error_block = Div(Ul(*error_items, cls="error-list"), cls="flash flash--error")

    # Build option definitions for JS (shared across all columns)
    # Format: [{value, label, group, required}]
    import json as _json
    option_defs = []
    # Special options first (top of list)
    option_defs.append({"value": MAPPING_ATTRIBUTE, "label": "Import as custom", "group": "action"})
    option_defs.append({"value": MAPPING_SKIP, "label": "Skip (don't import)", "group": "action"})
    # Core fields
    for tc in target_cols:
        label = tc.replace("_", " ").title()
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
                    placeholder="Custom field name",
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
            f"Required fields not yet mapped: {names}. "
            "Map them above or the import will fail validation.",
            cls="flash flash--warning",
        )

    row_count = len(sample_rows)
    showing_hint = (
        P(f"Showing {len(preview)} of {row_count} rows", cls="import-hint")
        if row_count > 5 else ""
    )

    return Div(
        _step_indicator(2, has_mapping=True),
        H3(t("page.map_columns"), cls="settings-section-title"),
        P(
            "Match your CSV columns to Celerp fields. "
            "Unrecognized columns can be mapped to category fields or imported as custom fields.",
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
                Button(t("btn.continue_to_preview"), type="submit", cls="btn btn--primary"),
                A(t("btn.cancel"), href=back_href, cls="btn btn--secondary"),
                cls="flex-row gap-sm mt-md",
            ),
            method="post",
            action=confirm_action,
        ),
        Script(f"var _MAPPING_OPTIONS = {_json.dumps(option_defs)};"),
        Script(_MAPPING_JS),
        id="import-preview",
        cls="import-panel",
    )


def validate_column_mapping(
    form: dict,
    csv_cols: list[str],
    core_fields: set[str] | None = None,
) -> list[str]:
    """Validate the user's column mapping choices. Returns list of error messages (empty = valid).

    Checks:
    1. Two CSV columns mapped to the same target field (duplicate targets).
    2. Attribute names that collide with core/built-in field names.
    3. Two attribute columns with the same custom name.
    """
    errors: list[str] = []
    core = core_fields or set()

    # Collect all mappings
    target_sources: dict[str, list[str]] = {}  # target -> [csv_col, ...]
    attr_names: dict[str, list[str]] = {}  # attr_name -> [csv_col, ...]

    for col in csv_cols:
        target = str(form.get(f"map__{col}", MAPPING_ATTRIBUTE) or MAPPING_ATTRIBUTE)
        if target == MAPPING_SKIP:
            continue

        if target == MAPPING_ATTRIBUTE:
            # Custom field name (from text input) or original col name
            attr_name = str(form.get(f"attr_name__{col}", "") or "").strip() or col
            attr_names.setdefault(attr_name, []).append(col)
            # Check collision with core field names
            if attr_name.lower() in {c.lower() for c in core}:
                errors.append(
                    f'Custom field name "{attr_name}" (from column "{col}") '
                    f"conflicts with a built-in field. Choose a different name."
                )
        elif target.startswith(MAPPING_ATTR_PREFIX):
            # Category attribute - use the attr key as the attribute name
            attr_key = target[len(MAPPING_ATTR_PREFIX):]
            attr_names.setdefault(attr_key, []).append(col)
        else:
            target_sources.setdefault(target, []).append(col)

    # Check duplicate target fields
    for target, sources in target_sources.items():
        if len(sources) > 1:
            names = " and ".join(f'"{s}"' for s in sources)
            errors.append(
                f"Columns {names} are both mapped to "
                f'"{target.replace("_", " ").title()}". Each target can only be used once.'
            )

    # Check duplicate attribute names
    for attr_name, sources in attr_names.items():
        if len(sources) > 1:
            names = " and ".join(f'"{s}"' for s in sources)
            errors.append(
                f"Columns {names} both have attribute name "
                f'"{attr_name}". Each attribute name must be unique.'
            )

    return errors


def apply_column_mapping(form: dict, csv_text: str) -> tuple[str, list[str]]:
    """Apply user's column mapping to CSV data.

    Reads map__<col>=<target> fields from the form.  Renames CSV headers
    according to the mapping.  Columns mapped to MAPPING_SKIP are dropped.
    Columns mapped to MAPPING_ATTRIBUTE use the custom name from attr_name__<col>
    (falling back to the original header).

    Returns (remapped_csv_text, remapped_cols).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    original_cols = list(reader.fieldnames or [])
    rows = list(reader)

    # Parse mapping from form
    mapping: dict[str, str] = {}
    for col in original_cols:
        target = str(form.get(f"map__{col}", MAPPING_ATTRIBUTE) or MAPPING_ATTRIBUTE)
        mapping[col] = target

    # Build new column list and rename map
    new_cols: list[str] = []
    rename: dict[str, str] = {}  # original -> new name
    for col in original_cols:
        target = mapping[col]
        if target == MAPPING_SKIP:
            continue
        elif target == MAPPING_ATTRIBUTE:
            attr_name = str(form.get(f"attr_name__{col}", "") or "").strip() or col
            new_cols.append(attr_name)
            rename[col] = attr_name
        elif target.startswith(MAPPING_ATTR_PREFIX):
            # Category attribute - use the key after the prefix as column name
            attr_key = target[len(MAPPING_ATTR_PREFIX):]
            new_cols.append(attr_key)
            rename[col] = attr_key
        else:
            new_cols.append(target)
            rename[col] = target

    # Write remapped CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=new_cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        new_row = {}
        for col in original_cols:
            if col not in rename:
                continue
            new_row[rename[col]] = row.get(col, "")
        writer.writerow(new_row)

    return output.getvalue(), new_cols


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
      badge.textContent = 'skip'; badge.classList.add('mapping-badge--skip'); badge.title = 'Skipped';
    } else if (v === ATTR) {
      badge.textContent = 'custom'; badge.classList.add('mapping-badge--attr'); badge.title = 'Custom field';
    } else {
      badge.textContent = '\\u2713'; badge.classList.add('mapping-badge--matched'); badge.title = 'Matched';
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
    search.placeholder = 'Search fields...';
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
    toggleLink.textContent = 'Show already matched';
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
          if (opt.group === 'category') sep.textContent = 'Category fields';
          else if (opt.group === 'core') sep.textContent = 'Core fields';
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
      toggleLink.textContent = dd.showMatched ? 'Hide already matched' : 'Show already matched';
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
        ["Upload", "Map Columns", "Review", "Import"]
        if has_mapping
        else ["Upload", "Review", "Import"]
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
  }
})();
"""


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
        Form(
            Input(type="file", id="csv_file", name="csv_file", accept=".csv",
                  required=True, style="display:none"),
            Div(
                Div("📄", cls="import-dropzone-icon"),
                Div(t("msg.drag_your_csv_here_or_click_to_browse"), cls="import-dropzone-text"),
                Div(t("msg.accepted_formats_csv_utf8"), cls="import-dropzone-hint"),
                Span(id="dropzone-file-info", cls="import-dropzone-file", style="display:none"),
                Div(
                    A(t("btn.download_template"), href=template_href, cls="link",
                      onclick="event.stopPropagation()"),
                    cls="import-dropzone-template",
                ),
                id="import-dropzone",
                cls="import-dropzone",
            ),
            Button(t("btn.preview"), cls="btn btn--primary", type="submit"),
            method="post",
            action=preview_action,
            enctype="multipart/form-data",
        ),
        Script(_DROPZONE_JS),
        cls="import-panel",
    )


async def read_csv_upload(form: Any) -> tuple[list[dict], str | None]:
    """Return (rows, error)."""
    file_obj = form.get("csv_file")
    if not file_obj or not hasattr(file_obj, "read"):
        return [], "Please select a CSV file."
    content = await file_obj.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        return [], "Could not decode file. Use UTF-8 encoding."
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [], "CSV file is empty or invalid."
    return rows, None


def _row_errors(row: dict, cols: list[str], validate: ValidateFn) -> list[str]:
    return [col for col in cols if not validate(col, str(row.get(col, "")))]


def error_report_csv(rows: list[dict], cols: list[str], validate: ValidateFn) -> str:
    """Return CSV with original columns + an `_errors` column, containing only invalid rows."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols + ["_errors"], extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        bad = _row_errors(row, cols, validate)
        if bad:
            writer.writerow({**{c: row.get(c, "") for c in cols}, "_errors": "; ".join(bad)})
    return output.getvalue()


# ── Inline fix error panel ────────────────────────────────────────────────────

_INLINE_FIX_JS = """
(function() {
  window.csvFillColumn = function(col) {
    var input = document.getElementById('fill-' + col);
    if (!input) return;
    var val = input.value;
    if (!val) return;
    document.querySelectorAll('input[data-col="' + col + '"]').forEach(function(el) {
      el.value = val;
      el.classList.remove('input--error');
    });
  };
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
.csv-fix-table input.input--error { border-color: var(--c-red, #ef4444);
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
            if not validate(col, str(rows[ri].get(col, "")))
        )

    # Fill-all bars for error columns with many errors (>1 error row)
    fill_bars = []
    for col in cols:
        if col not in error_cols:
            continue
        col_error_count = col_error_counts.get(col, 0)
        if col_error_count > 1:
            label = col.replace("_", " ").title()
            fill_bars.append(Div(
                Label(f"{label} ({col_error_count} rows):", _for=f"fill-{col}"),
                Input(
                    type="text", id=f"fill-{col}",
                    placeholder=f"Value for all {label} cells",
                    cls="form-input form-input--sm",
                ),
                Button(t("btn.apply"),
                    type="button",
                    onclick=f"csvFillColumn('{col}')",
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
    header_cells = [Th("#", cls="csv-th")]
    for col in visible_cols:
        label = col.replace("_", " ").title()
        is_err_col = col in error_cols
        badge = f" ({col_error_counts.get(col, 0)} errors)" if is_err_col else ""
        header_cells.append(Th(f"{label}{badge}", cls="csv-th--error" if is_err_col else ""))

    body_rows = []
    for err_idx, ri in enumerate(error_row_indices):
        row = rows[ri]
        bad = set(_row_errors(row, cols, validate))
        cells = [Td(str(ri + 1), cls="cell-ro")]  # 1-indexed row number
        for col in visible_cols:
            val = str(row.get(col, ""))
            if col in error_cols:
                is_bad = col in bad
                cells.append(Td(Input(
                    type="text",
                    name=f"fix__{ri}__{col}",
                    value=val,
                    data_col=col,
                    data_row=str(ri),
                    cls=f"cell-edit{'  input--error' if is_bad else ''}",
                )))
            else:
                cells.append(Td(val, cls="cell-ro"))
        body_rows.append(Tr(*cells, cls="data-row"))

    # Error navigation JS
    err_nav_js = """
(function(){
  var pos=0,rows=document.querySelectorAll('.csv-fix-table .data-row');
  var total=rows.length,lbl=document.getElementById('err-pos-label');
  function go(d){if(!total)return;pos=((pos+d)%total+total)%total;rows[pos].scrollIntoView({block:'center'});if(lbl)lbl.textContent=(pos+1)+' of '+total}
  window.errPrev=function(){go(-1)};window.errNext=function(){go(1)};
  if(lbl) lbl.textContent='1 of '+total;
})();
"""

    return Div(
        NotStr(_INLINE_FIX_CSS),
        _step_indicator(review_step, has_mapping=has_mapping),
        Div(
            P(
                Strong(f"{total_errors} cell(s)"),
                f" need fixing across {len(error_row_indices)} of {len(rows)} rows.",
                cls="csv-fix-summary",
            ),
            Div(
                Div(style=f"width:{pct}%", cls="import-progress-fill"),
                cls="import-progress",
            ),
            P(f"{ok_count} of {len(rows)} rows are valid", cls="csv-ok-count"),
            Div(
                Span(id="err-pos-label", cls="import-err-pos"),
                Button(t("btn.prev"), type="button", onclick="errPrev()", cls="btn btn--ghost btn--xs"),
                Button(t("btn.next"), type="button", onclick="errNext()", cls="btn btn--ghost btn--xs"),
                cls="import-err-nav",
            ) if error_row_indices else "",
            fill_section,
            Form(
                Input(type="hidden", name="csv_ref", value=csv_ref),
                Table(
                    Thead(Tr(*header_cells)),
                    Tbody(*body_rows),
                    cls="csv-fix-table data-table",
                ),
                Div(
                    Button(t("btn.fix_import"),
                        type="submit",
                        cls="btn btn--primary",
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


def apply_fixes_to_rows(
    form: dict,
    rows: list[dict],
    cols: list[str],
) -> list[dict]:
    """Apply inline-fix form values back into the row dicts.

    Form fields are named ``fix__{row_index}__{col_name}``.
    Returns the same list with values patched in-place.
    """
    for key, value in form.items():
        if not isinstance(key, str) or not key.startswith("fix__"):
            continue
        parts = key.split("__", 2)
        if len(parts) != 3:
            continue
        _, ri_str, col = parts
        try:
            ri = int(ri_str)
        except ValueError:
            continue
        if 0 <= ri < len(rows) and col in cols:
            rows[ri][col] = str(value)
    return rows


def validation_result(
    *,
    rows: list[dict],
    cols: list[str],
    validate: ValidateFn,
    confirm_action: str,
    error_report_action: str,
    back_href: str,
    revalidate_action: str = "",
    has_mapping: bool = False,
    upsert_label: str | None = None,
) -> FT:
    """Return the post-upload panel: inline-fix error panel or clean confirm panel.

    If ``revalidate_action`` is provided and there are errors, users can fix
    cells on-screen and submit to that endpoint. Otherwise falls back to
    download-only error report.

    If ``upsert_label`` is provided, a checkbox is shown above the import
    button letting users opt-in to updating existing records.
    """
    error_pairs = [(i, _row_errors(row, cols, validate)) for i, row in enumerate(rows)]
    error_row_indices = [i for i, errs in error_pairs if errs]
    total_errors = sum(len(errs) for _, errs in error_pairs)
    error_cols: set[str] = {col for _, errs in error_pairs for col in errs}

    csv_data = _rows_to_csv(rows, cols)
    csv_ref = _stash_csv(csv_data)

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
        )

    # Clean - confirm panel with preview table
    review_step = 3 if has_mapping else 2
    n = len(rows)

    # Preview table: first 5 rows, values truncated to 40 chars
    preview_rows = rows[:5]
    preview_header = [Th(c.replace("_", " ").title()) for c in cols]
    preview_body = []
    for row in preview_rows:
        cells = [Td(str(row.get(c, ""))[:40]) for c in cols]
        preview_body.append(Tr(*cells))

    # Optional upsert checkbox + info badge
    upsert_control: Any = ""
    if upsert_label:
        upsert_control = Div(
            Label(
                Input(type="checkbox", name="upsert", value="1"),
                " Update existing records",
                cls="flex-row gap-sm",
                style="align-items:center;cursor:pointer;",
            ),
            Span(
                f"Matched by: {upsert_label}. When checked, rows matching an existing "
                f"{upsert_label} will be updated instead of skipped.",
                cls="import-hint",
                style="display:block;margin-top:4px;",
            ),
            cls="mt-sm mb-sm",
        )

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
            Table(
                Thead(Tr(*preview_header)),
                Tbody(*preview_body),
                cls="data-table import-preview-table",
            ) if preview_rows else "",
            P(f"Showing {len(preview_rows)} of {n} rows", cls="import-hint") if n > 5 else "",
            Form(
                Input(type="hidden", name="csv_ref", value=csv_ref),
                upsert_control,
                Button(
                    f"Import All {n} Rows",
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
                    " Importing...",
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


def _rows_to_csv(rows: list[dict], cols: list[str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


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
) -> FT:
    """Shared import result panel with summary cards.

    ``extra`` is an optional FT element inserted after the summary cards
    (e.g. schema-merge info for inventory).
    ``updated`` shows a blue "Updated" card when > 0 (upsert mode).
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
            Summary(f"Error details ({len(details)})"),
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
            A(f"View {label_title}", href=back_href, cls="btn btn--primary"),
            A(t("msg.import_more"), href=import_more_href, cls="btn btn--secondary"),
            cls="flex-row gap-sm mt-md",
        ),
        id="import-preview",
    )

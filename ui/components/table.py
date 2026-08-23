# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import re

from fasthtml.common import *
from ui.i18n import t
from celerp.services.field_schema import MIXED_VALUE
# Canonical definitions live with the shared document renderer; re-exported
# here for the UI's many call sites. EMPTY is the canonical empty-value
# placeholder (rule k).
from celerp.output.doc_print import (  # noqa: F401
    CURRENCY_SYMBOLS, EMPTY, currency_symbol, fmt_money, fmt_rate, unwrap_address,
)

# Statuses that dim a row to indicate it is not actively available for sale/use.
# Allowlist: adding a new status requires an explicit decision (mirrors fulfillment guard pattern).
INACTIVE_ITEM_STATUSES: frozenset[str] = frozenset({"archived", "expired", "sold", "memo_out", "disposed"})

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
    if fmt == "rate":
        return fmt_rate(v, currency)
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

def status_cards(cards: list[dict], base_url: str, active_status: str | None = None, total_override: int | None = None, currency: str | None = None, show_all_card: bool = True, all_label: str = "All") -> FT:
    """Clickable status filter cards at top of list pages.

    cards: [{"label": "Paid", "count": 489, "total": 2990000.0, "status": "paid", "color": "green"}, ...]
    Clicking a card navigates to base_url?status=<status>.
    Cards may include a "_url" key to override the generated href entirely.
    Cards may include "_active_key" to override which value is compared against active_status.
    "All" card (status=None/"") is prepended unless show_all_card=False.
    total_override: if provided, the "All" card shows this count instead of summing cards.
    """
    def _card(label: str, count: int, total: float | None, status: str | None, color: str, href_override: str | None = None, active_key: str | None = None, title: str | None = None) -> FT:
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
        return A(*inner, href=href, cls=cls, **({"title": title} if title else {}))

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
            title=c.get("title"),
        ))
    return Div(*els, cls="status-cards")


# Shared bulk-action toolbar: [N selected] [Action ▾] [Clear]. Reused across list pages so bulk
# actions are consistent and extensible. Rows are `.bulk-select` checkboxes (name='selected',
# value=id) in the table; the header `.bulk-select-all` checkbox toggles visible rows; only
# VISIBLE (not .dp-row-hidden) rows count, so it composes with the column filters.
BULK_TOOLBAR_JS = """
(function(){
  if(window.__celerpBulkbar)return;window.__celerpBulkbar=true;
  function table(bar){return document.getElementById(bar.getAttribute('data-table'));}
  function visBoxes(t){if(!t)return [];return Array.prototype.slice.call(
    t.querySelectorAll('tbody tr.data-row:not(.dp-row-hidden) .bulk-select'));}
  function checkedIds(t){var seen={},out=[];visBoxes(t).forEach(function(c){
    if(c.checked&&!(c.value in seen)){seen[c.value]=1;out.push(c.value);}});return out;}
  function fieldsBox(bar){return bar.querySelector('.bulk-fields');}
  function closeFields(bar){var fb=fieldsBox(bar);if(fb)fb.classList.remove('bulk-fields--open');
    bar.removeAttribute('data-pending');var sel=bar.querySelector('.bulk-action-select');if(sel)sel.selectedIndex=0;}
  function refresh(bar){
    var t=table(bar);if(!t)return;var b=visBoxes(t),n=b.filter(function(c){return c.checked}).length;
    var cnt=bar.querySelector('.bulk-count');
    if(cnt)cnt.textContent=(bar.getAttribute('data-count-label')||'{n} selected').replace('{n}',n);
    bar.classList.toggle('is-active',n>0);
    var clr=bar.querySelector('.bulk-clear');if(clr)clr.style.visibility=n>0?'visible':'hidden';
    if(n===0)closeFields(bar);
    var all=t.querySelector('thead .bulk-select-all');if(all){all.checked=n>0&&n===b.length;all.indeterminate=n>0&&n<b.length;}
  }
  function refreshAll(){document.querySelectorAll('.bulkbar').forEach(refresh);}
  window.celerpBulkRefresh=refreshAll;
  function findAction(bar,val){
    var sel=bar.querySelector('.bulk-action-select');if(!sel)return null;
    var acts=JSON.parse(sel.getAttribute('data-actions')||'[]');
    for(var i=0;i<acts.length;i++){if(acts[i].value===val)return acts[i];}return null;}
  function runAction(bar,a){
    var t=table(bar);if(!t||!a)return;var ids=checkedIds(t);if(!ids.length)return;
    if(a.confirm&&!confirm(a.confirm.replace('{n}',ids.length)))return;
    if(a.method==='open'){window.open(a.url+(a.url.indexOf('?')>=0?'&':'?')+'ids='+encodeURIComponent(ids.join(',')),'_blank');return;}
    var form=document.createElement('form');
    ids.forEach(function(id){var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;form.appendChild(inp);});
    bar.querySelectorAll('.bulk-field[name]').forEach(function(f){
      var inp=document.createElement('input');inp.type='hidden';
      inp.name=f.getAttribute('name');inp.value=f.value;form.appendChild(inp);});
    document.body.appendChild(form);
    htmx.ajax('POST',a.url,{source:form,target:a.target||('#'+t.id),swap:a.swap||'outerHTML'})
      .then(function(){form.remove();},function(){form.remove();});
  }
  // Field-bearing actions (e.g. void, which takes an optional reason) reveal their
  // inputs below the bar and wait for the confirm button; field-less actions fire at once.
  function doAction(sel){
    var bar=sel.closest('.bulkbar');var t=table(bar);if(!t)return;
    var val=sel.value;if(!val)return;
    // The action list is always usable so its options can be read; if nothing is
    // ticked, say so and reset rather than silently swallowing the choice (GDR 2e).
    if(!checkedIds(t).length){sel.selectedIndex=0;
      if(window.celerpToast)celerpToast(bar.getAttribute('data-empty-msg')||'Select at least one row first.','error');
      return;}
    var a=findAction(bar,val);if(!a){sel.selectedIndex=0;return;}
    var fb=fieldsBox(bar);
    if(a.fields&&fb){fb.classList.add('bulk-fields--open');bar.setAttribute('data-pending',val);
      var f0=fb.querySelector('.bulk-field');if(f0)f0.focus();return;}
    sel.selectedIndex=0;runAction(bar,a);
  }
  document.addEventListener('change',function(e){
    var el=e.target;if(!el||!el.classList)return;
    if(el.classList.contains('bulk-select-all')){var t=el.closest('table');
      if(t)visBoxes(t).forEach(function(c){c.checked=el.checked;});refreshAll();}
    else if(el.classList.contains('bulk-select')){refreshAll();}
    else if(el.classList.contains('bulk-action-select')){doAction(el);}
  });
  document.addEventListener('click',function(e){
    var clr=e.target.closest&&e.target.closest('.bulk-clear');
    if(clr){var t=table(clr.closest('.bulkbar'));if(t)visBoxes(t).forEach(function(c){c.checked=false;});refreshAll();return;}
    var ap=e.target.closest&&e.target.closest('.bulk-apply');
    if(ap){var bar=ap.closest('.bulkbar');var val=bar.getAttribute('data-pending');
      var a=val?findAction(bar,val):null;closeFields(bar);if(a)runAction(bar,a);return;}
    var cx=e.target.closest&&e.target.closest('.bulk-cancel');
    if(cx){closeFields(cx.closest('.bulkbar'));return;}
  });
  document.addEventListener('keydown',function(e){
    if(e.key!=='Escape')return;var fb=e.target.closest&&e.target.closest('.bulk-fields');
    if(fb)closeFields(fb.closest('.bulkbar'));
  });
  document.addEventListener('htmx:afterSwap',function(){refreshAll();});
  refreshAll();
})();
"""


def bulk_toolbar(table_id: str, actions: list[dict], fields: list | None = None) -> FT:
    """Standard bulk-action toolbar: [N selected] [Action ▾] [Clear] [fields…].
    Hidden until at least one row is ticked (the JS toggles `is-active`), so the
    action list appears only when there is a selection for it to act on.
    actions: [{value, label, method('post'|'open'), url, confirm?, target?, swap?}].
    POST actions submit the selected ids (name='selected') via htmx; 'open' actions open
    url?ids=<csv> in a new tab. Pair with `.bulk-select` row checkboxes + a `.bulk-select-all`
    header checkbox in #table_id. `data-table` lives on the outer Div (the JS reads it there).

    `confirm` text may contain `{n}`, replaced with the number selected at click time.
    `fields` are extra inputs for an action that takes a value (a void reason). They
    sit hidden in a `.bulk-fields` group below the action list and only appear when an
    action marked `"fields": True` is chosen, revealed with its own Apply/Cancel rather
    than a popup (GDR 2f); any input carrying class `bulk-field` and a name is posted
    alongside the ids. A field-less action still fires the instant it is chosen."""
    import json as _json
    opts = [Option(t("inv.action"), value="", disabled=True, selected=True)]
    opts += [Option(a["label"], value=a["value"]) for a in actions]
    field_group = ()
    if fields:
        field_group = (Div(
            *fields,
            Button(t("btn.apply"), type="button", cls="btn btn--xs btn--primary bulk-apply"),
            Button(t("btn.cancel"), type="button", cls="btn btn--xs btn--ghost bulk-cancel"),
            cls="bulk-fields",
        ),)
    return Div(
        Span(t("label.n_selected", n=0), cls="bulk-count"),
        Select(*opts, cls="bulk-action-select",
               **{"data-actions": _json.dumps(actions)}),
        Button(t("btn.clear"), type="button", cls="btn btn--xs btn--ghost bulk-clear"),
        *field_group,
        Script(BULK_TOOLBAR_JS),
        cls="bulkbar bulk-action-bar", id=f"bulkbar-{table_id}",
        **{"data-table": table_id, "data-count-label": t("label.n_selected"),
           "data-empty-msg": t("label.select_rows_first")},
    )


def filter_th(label: str, col: int, *, center: bool = False, sortable: bool = False,
              right: bool = False, default_exclude: list[str] | None = None) -> FT:
    """A column header with an Excel-style filter funnel (client-side checkbox value list).
    `col` is the 0-based cell index the funnel filters on. Pair with COLUMN_FILTER_JS on the page.
    With sortable=True the label also sorts the column (needs ENHANCED_TABLE_JS); the sort arrow sits
    inner (right after the label) and the funnel arrow outer - matching the inventory list. right=True
    right-aligns the header to match numeric cells. The funnel is guarded so clicking it filters.
    default_exclude: values hidden by default (the funnel starts with them unchecked) - e.g. Status
    excludes Completed/Cancelled until the user re-checks them."""
    attrs = {"data-col": str(col), "aria-label": f"Filter by {label}"}
    if default_exclude:
        attrs["data-filter-exclude"] = "␟".join(default_exclude)  # unit-separator: values may contain commas
    inner = [Span(label)] + ([Span(cls="sort-ind")] if sortable else [])
    inner.append(Button("▾", type="button", cls="colfilter", title=f"Filter by {label}", **attrs))
    cls = ("colfilter-th" + (" sortable-th" if sortable else "")
           + (" cell--center" if center else "") + (" cell--number" if right else ""))
    return Th(*inner, cls=cls, **({"data-sort": str(col)} if sortable else {}))


def date_range_filter(table_id: str, col: int, label: str) -> FT:
    """A from/to date-range filter bound to a client table column (composes with the Excel funnels
    via COLUMN_FILTER_JS). `col` is the 0-based cell index holding an ISO date (YYYY-MM-DD)."""
    return Span(
        Span(f"{label}:", cls="daterange-label"),
        Input(type="date", cls="daterange-input", aria_label=f"{label} from", **{"data-bound": "from"}),
        Span("to", cls="daterange-sep"),
        Input(type="date", cls="daterange-input", aria_label=f"{label} to", **{"data-bound": "to"}),
        cls="daterange", **{"data-daterange-table": table_id, "data-daterange-col": str(col)},
    )


# Excel-style column filters: each `.colfilter` funnel (in a `filter_th`) opens a checkbox list of
# the distinct values in its column (data-col = cell index). Filtering is client-side and instant;
# multiple columns AND together; hidden rows get `.dp-row-hidden` and are de-selected. Per-table
# state (WeakMap) so it works on any table on the page; resets when a table is re-rendered via htmx.
COLUMN_FILTER_JS = """
(function(){
  if(window.__celerpColFilter)return;window.__celerpColFilter=true;
  var state=new WeakMap();
  function active(t){if(!state.has(t))state.set(t,{});return state.get(t);}
  function rows(t){return Array.prototype.slice.call(t.querySelectorAll('tbody tr.data-row'));}
  function cellText(r,col){var c=r.children[col];if(!c)return '';var dv=c.getAttribute('data-filter-value');return (dv!==null?dv:c.textContent).trim();}
  function distinct(t,col){var out=[],seen={};rows(t).forEach(function(r){var v=cellText(r,col);if(!(v in seen)){seen[v]=1;out.push(v);}});out.sort();return out;}
  function apply(t){
    var a=active(t);
    rows(t).forEach(function(r){
      var show=true;
      for(var key in a){
        var s=a[key];
        if(key.indexOf('_range_')===0){
          // Date-range filter: cell must be an ISO date within [from, to].
          var rc=+key.slice(7), cv=cellText(r,rc), ok=/^\\d{4}-\\d{2}-\\d{2}/.test(cv);
          if(!ok){show=false;break;}
          if(s.from&&cv<s.from){show=false;break;}
          if(s.to&&cv>s.to){show=false;break;}
        } else if(key==='_search_'){
          // Free-text search box (table_search): match the row's whole text.
          if(s&&r.textContent.toLowerCase().indexOf(s)<0){show=false;break;}
        } else if(s&&!s.has(cellText(r,+key))){show=false;break;}
      }
      r.classList.toggle('dp-row-hidden',!show);
      if(!show){var cb=r.querySelector('.bulk-select');if(cb&&cb.checked)cb.checked=false;}
    });
    t.querySelectorAll('thead .colfilter').forEach(function(b){b.classList.toggle('colfilter--active',!!a[b.getAttribute('data-col')]);});
    if(window.celerpBulkRefresh)window.celerpBulkRefresh();
    if(window.celerpTableEnhance)window.celerpTableEnhance(t);
  }
  // Seed a column's default-excluded values (e.g. Status hides Completed/Cancelled) once per table.
  function seedDefaults(t){
    var a=active(t);
    t.querySelectorAll('thead .colfilter[data-filter-exclude]').forEach(function(b){
      var col=b.getAttribute('data-col'); if(col in a) return;
      var ex=b.getAttribute('data-filter-exclude').split('\\u241f');
      var keep=distinct(t,+col).filter(function(v){return ex.indexOf(v)<0;});
      a[col]=new Set(keep);
    });
  }
  function refresh(t){ if(t){seedDefaults(t);apply(t);} }
  // Seed default filters for tables under `root` (called by sections after an htmx swap, since
  // htmx:afterSwap's target is unreliable for outerHTML swaps). A freshly swapped table is a new
  // element with empty state, so this seeds it without wiping user filters elsewhere.
  window.celerpRefreshFilters=function(root){(root||document).querySelectorAll('table.js-table,table.data-table').forEach(refresh);};
  function closeAll(){var p=document.querySelector('.colfilter-pop');if(p)p.remove();}
  function open(btn){
    var t=btn.closest('table');if(!t)return;
    var col=btn.getAttribute('data-col');
    var a=active(t),values=distinct(t,+col);
    var pop=document.createElement('div');pop.className='colfilter-pop';pop.setAttribute('data-col',col);
    var search=document.createElement('input');search.type='text';search.className='colfilter-search';search.placeholder='Search\\u2026';
    var selAll=document.createElement('label');selAll.className='colfilter-item colfilter-all';
    var selAllCb=document.createElement('input');selAllCb.type='checkbox';
    selAll.appendChild(selAllCb);selAll.appendChild(document.createTextNode(' (Select all)'));
    var list=document.createElement('div');list.className='colfilter-list';
    function commit(){
      var checked=[];list.querySelectorAll('input[type=checkbox]').forEach(function(c){if(c.checked)checked.push(c.value);});
      if(checked.length===values.length){delete a[col];}else{a[col]=new Set(checked);}
      selAllCb.checked=(checked.length===values.length);
      selAllCb.indeterminate=(checked.length>0&&checked.length<values.length);
      apply(t);
    }
    values.forEach(function(v){
      var lbl=document.createElement('label');lbl.className='colfilter-item';
      var cb=document.createElement('input');cb.type='checkbox';cb.value=v;
      cb.checked=!a[col]||a[col].has(v);
      cb.addEventListener('change',commit);
      lbl.appendChild(cb);lbl.appendChild(document.createTextNode(' '+(v||'(blank)')));
      list.appendChild(lbl);
    });
    selAllCb.checked=!a[col];selAllCb.indeterminate=!!a[col];
    selAllCb.addEventListener('change',function(){
      list.querySelectorAll('input[type=checkbox]').forEach(function(c){c.checked=selAllCb.checked;});commit();});
    search.addEventListener('input',function(){var q=search.value.toLowerCase();
      list.querySelectorAll('.colfilter-item').forEach(function(it){it.style.display=it.textContent.toLowerCase().indexOf(q)>=0?'':'none';});});
    var clear=document.createElement('button');clear.type='button';clear.className='colfilter-clear';clear.textContent='Clear filter';
    clear.addEventListener('click',function(){delete a[col];closeAll();apply(t);});
    pop.appendChild(search);pop.appendChild(selAll);pop.appendChild(list);pop.appendChild(clear);
    pop.style.position='fixed';
    document.body.appendChild(pop);
    var br=btn.getBoundingClientRect();
    pop.style.top=(br.bottom+2)+'px';
    pop.style.left=Math.max(8,Math.min(br.left,window.innerWidth-8-pop.offsetWidth))+'px';
    search.focus();
  }
  document.addEventListener('click',function(e){
    var btn=e.target.closest&&e.target.closest('.colfilter');
    if(btn){e.preventDefault();e.stopPropagation();
      var wasOpen=!!document.querySelector('.colfilter-pop[data-col="'+btn.getAttribute('data-col')+'"]');
      closeAll();if(!wasOpen)open(btn);return;}
    if(!(e.target.closest&&e.target.closest('.colfilter-pop')))closeAll();
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closeAll();});
  // Free-text search box (table_search): filters its table's rows in place, ANDing
  // with any active column funnels since both funnel state and search live in the
  // same per-table state object.
  document.addEventListener('input',function(e){
    var inp=e.target;
    if(!(inp.classList&&inp.classList.contains('js-table-search')))return;
    var t=document.getElementById(inp.getAttribute('data-search-for'));if(!t)return;
    var a=active(t),q=inp.value.trim().toLowerCase();
    if(q)a['_search_']=q;else delete a['_search_'];
    apply(t);
  });
  // Date-range inputs (date_range_filter): bound to a table column via .daterange wrapper.
  document.addEventListener('change',function(e){
    var inp=e.target;
    if(!(inp.classList&&inp.classList.contains('daterange-input')))return;
    var wrap=inp.closest('.daterange');if(!wrap)return;
    var t=document.getElementById(wrap.getAttribute('data-daterange-table'));if(!t)return;
    var col=wrap.getAttribute('data-daterange-col'),a=active(t);
    var from=(wrap.querySelector('[data-bound=from]')||{}).value||'';
    var to=(wrap.querySelector('[data-bound=to]')||{}).value||'';
    if(from||to)a['_range_'+col]={from:from,to:to};else delete a['_range_'+col];
    apply(t);
  });
  document.addEventListener('htmx:afterSwap',function(e){
    var tg=e.detail&&e.detail.target;if(!tg)return;
    var tables=tg.tagName==='TABLE'?[tg]:(tg.querySelectorAll?tg.querySelectorAll('table'):[]);
    if(tables.length){closeAll();Array.prototype.forEach.call(tables,function(t){state.delete(t);refresh(t);});}
  });
  if(document.readyState!=='loading')document.querySelectorAll('table.js-table,table.data-table').forEach(refresh);
  else document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('table.js-table,table.data-table').forEach(refresh);});
})();
"""


def _filter_funnel_btn(param: str, options: list, selected, label: str = "") -> FT:
    """The funnel button for a server-backed column filter. `options` is [(value, label), ...];
    `selected` is the active values (from the current query). SERVER_FILTER_JS handles the rest."""
    import json as _json
    sel = [str(s) for s in (selected or [])]
    return Button(
        "▾", type="button", cls="colfilter" + (" colfilter--active" if sel else ""),
        title=f"Filter by {label}" if label else "Filter",
        **{"data-param": param,
           "data-options": _json.dumps([[str(v), (lbl if lbl is not None else str(v))] for v, lbl in options]),
           "data-selected": _json.dumps(sel),
           "aria-label": f"Filter by {label}" if label else "Filter"},
    )


def server_filter_th(label: str, param: str, options: list, selected=None, *, center: bool = False) -> FT:
    """A column header with a SERVER-backed Excel filter funnel (for paginated lists). Checking
    values and applying reloads the page with `?<param>=<csv>` so the full dataset is filtered
    server-side. Pair with SERVER_FILTER_JS on the page."""
    return Th(
        Span(label), _filter_funnel_btn(param, options, selected, label),
        cls="colfilter-th" + (" cell--center" if center else ""),
    )


# Server-backed column filter: the funnel popup lists the column's value domain (provided
# server-side); Apply reloads with the chosen values as a `?<param>=csv` query so a PAGINATED list
# is filtered across its whole dataset, not just the visible page. Reuses the .colfilter-pop styling.
SERVER_FILTER_JS = """
(function(){
  if(window.__celerpSrvFilter)return;window.__celerpSrvFilter=true;
  function closeAll(){var p=document.querySelector('.colfilter-pop');if(p)p.remove();}
  function build(btn){
    var param=btn.getAttribute('data-param');
    var options=JSON.parse(btn.getAttribute('data-options')||'[]');
    var selected=new Set(JSON.parse(btn.getAttribute('data-selected')||'[]'));
    var noFilter=selected.size===0;
    var pop=document.createElement('div');pop.className='colfilter-pop';pop.setAttribute('data-param',param);
    var search=document.createElement('input');search.type='text';search.className='colfilter-search';search.placeholder='Search\\u2026';
    var selAll=document.createElement('label');selAll.className='colfilter-item colfilter-all';
    var selAllCb=document.createElement('input');selAllCb.type='checkbox';
    selAll.appendChild(selAllCb);selAll.appendChild(document.createTextNode(' (Select all)'));
    var list=document.createElement('div');list.className='colfilter-list';
    options.forEach(function(o){
      var v=String(o[0]),label=o[1]==null?v:o[1];
      var lbl=document.createElement('label');lbl.className='colfilter-item';
      var cb=document.createElement('input');cb.type='checkbox';cb.value=v;cb.checked=noFilter||selected.has(v);
      lbl.appendChild(cb);lbl.appendChild(document.createTextNode(' '+label));
      list.appendChild(lbl);
    });
    function checkedVals(){var out=[];list.querySelectorAll('input[type=checkbox]').forEach(function(c){if(c.checked)out.push(c.value);});return out;}
    function syncAll(){var n=checkedVals().length;selAllCb.checked=n===options.length;selAllCb.indeterminate=n>0&&n<options.length;}
    syncAll();list.addEventListener('change',syncAll);
    selAllCb.addEventListener('change',function(){list.querySelectorAll('input[type=checkbox]').forEach(function(c){c.checked=selAllCb.checked;});});
    search.addEventListener('input',function(){var q=search.value.toLowerCase();
      list.querySelectorAll('.colfilter-item').forEach(function(it){it.style.display=it.textContent.toLowerCase().indexOf(q)>=0?'':'none';});});
    function go(vals){var p=new URLSearchParams(window.location.search);
      if(!vals||vals.length===0||vals.length===options.length){p.delete(param);}else{p.set(param,vals.join(','));}
      p.set('page','1');window.location.search=p.toString();}
    var foot=document.createElement('div');foot.className='colfilter-foot';
    var apply=document.createElement('button');apply.type='button';apply.className='btn btn--xs btn--primary';apply.textContent='Apply';
    apply.addEventListener('click',function(){go(checkedVals());});
    var clear=document.createElement('button');clear.type='button';clear.className='colfilter-clear';clear.textContent='Clear';
    clear.addEventListener('click',function(){go([]);});
    foot.appendChild(apply);foot.appendChild(clear);
    pop.appendChild(search);pop.appendChild(selAll);pop.appendChild(list);pop.appendChild(foot);
    pop.style.position='fixed';document.body.appendChild(pop);
    var br=btn.getBoundingClientRect();
    pop.style.top=(br.bottom+2)+'px';pop.style.left=Math.max(8,Math.min(br.left,window.innerWidth-8-pop.offsetWidth))+'px';
    search.focus();
  }
  document.addEventListener('click',function(e){
    var btn=e.target.closest&&e.target.closest('.colfilter[data-param]');
    if(btn){e.preventDefault();e.stopPropagation();
      var open=!!document.querySelector('.colfilter-pop[data-param="'+btn.getAttribute('data-param')+'"]');
      closeAll();if(!open)build(btn);return;}
    if(!(e.target.closest&&e.target.closest('.colfilter-pop')))closeAll();
  });
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closeAll();});
})();
"""


def sortable_th(label, col: int, *, center: bool = False, right: bool = False) -> FT:
    """A clickable, client-sortable column header (asc/desc); the sort arrow sits inner (right after
    the label). Pair with ENHANCED_TABLE_JS and a `js-table` table. `col` is the 0-based cell index
    to sort on. right=True right-aligns the header to match numeric cells."""
    cls = "sortable-th" + (" cell--center" if center else "") + (" cell--number" if right else "")
    return Th(Span(label), Span(cls="sort-ind"), cls=cls, **{"data-sort": str(col)})


def table_pager(table_id: str) -> FT:
    """Client-side pager controls for a `js-table` (hidden until the table spans >1 page)."""
    return Div(
        Button("‹ Prev", type="button", cls="btn btn--xs btn--ghost",
               **{"data-page-nav": "prev", "data-page-for": table_id}),
        Span("", cls="enh-page-info"),
        Button("Next ›", type="button", cls="btn btn--xs btn--ghost",
               **{"data-page-nav": "next", "data-page-for": table_id}),
        cls="enh-pager", style="display:none", **{"data-pager-for": table_id},
    )


# Client-side table enhancer for bounded tables (`table.js-table`): clickable sort on
# `th[data-sort]` (asc/desc) and windowed pagination (`data-page-size`). Composes with the Excel
# column funnels - it only paginates VISIBLE (not .dp-row-hidden) rows and re-windows when a filter
# changes. Reusable across pages; no server round-trips.
ENHANCED_TABLE_JS = """
(function(){
  if(window.__celerpEnhTable)return;window.__celerpEnhTable=true;
  function dataRows(t){return Array.prototype.slice.call(t.querySelectorAll('tbody tr.data-row'));}
  function visible(t){return dataRows(t).filter(function(r){return !r.classList.contains('dp-row-hidden');});}
  function num(s){var n=parseFloat(String(s).replace(/[^0-9.\\-]/g,''));return (s!==''&&!isNaN(n))?n:null;}
  function cmp(a,b,col,dir){
    var av=a.children[col]?a.children[col].textContent.trim():'';
    var bv=b.children[col]?b.children[col].textContent.trim():'';
    var an=num(av),bn=num(bv),r;
    if(an!==null&&bn!==null)r=an-bn; else r=av.toLowerCase().localeCompare(bv.toLowerCase());
    return dir==='asc'?r:-r;
  }
  function paginate(t){
    var size=parseInt(t.getAttribute('data-page-size')||'0',10);
    var vis=visible(t),pager=document.querySelector('[data-pager-for="'+t.id+'"]');
    if(!size){return;}
    var pages=Math.max(1,Math.ceil(vis.length/size));
    var page=Math.min(pages,Math.max(1,parseInt(t.getAttribute('data-page')||'1',10)));
    t.setAttribute('data-page',page);
    vis.forEach(function(r,i){r.classList.toggle('enh-page-hidden',!(i>=(page-1)*size&&i<page*size));});
    if(pager){
      pager.style.display=pages>1?'':'none';
      var info=pager.querySelector('.enh-page-info');if(info)info.textContent=page+' / '+pages+' ('+vis.length+')';
      var prev=pager.querySelector('[data-page-nav=prev]'),next=pager.querySelector('[data-page-nav=next]');
      if(prev)prev.disabled=page<=1; if(next)next.disabled=page>=pages;
    }
  }
  function sortBy(t,col){
    var cur=t.getAttribute('data-sort-col');
    var dir=(cur===String(col)&&t.getAttribute('data-sort-dir')==='asc')?'desc':'asc';
    t.setAttribute('data-sort-col',col);t.setAttribute('data-sort-dir',dir);t.setAttribute('data-page','1');
    var tb=t.querySelector('tbody');
    dataRows(t).sort(function(a,b){return cmp(a,b,col,dir);}).forEach(function(r){tb.appendChild(r);});
    t.querySelectorAll('thead th[data-sort]').forEach(function(th){th.classList.remove('sort-asc','sort-desc');});
    var th=t.querySelector('thead th[data-sort="'+col+'"]');if(th)th.classList.add(dir==='asc'?'sort-asc':'sort-desc');
    paginate(t);
  }
  window.celerpTableEnhance=function(t){ if(t){paginate(t);return;} document.querySelectorAll('table.js-table').forEach(paginate); };
  document.addEventListener('click',function(e){
    if(e.target.closest&&e.target.closest('.colfilter'))return;  // funnel handles its own clicks
    var th=e.target.closest&&e.target.closest('th[data-sort]');
    if(th){var t=th.closest('table');if(t&&t.classList.contains('js-table'))sortBy(t,parseInt(th.getAttribute('data-sort'),10));return;}
    var nav=e.target.closest&&e.target.closest('[data-page-nav]');
    if(nav){var t=document.getElementById(nav.getAttribute('data-page-for'));if(!t)return;
      var p=parseInt(t.getAttribute('data-page')||'1',10);
      t.setAttribute('data-page',nav.getAttribute('data-page-nav')==='next'?p+1:p-1);paginate(t);}
  });
  document.addEventListener('htmx:afterSwap',function(){document.querySelectorAll('table.js-table').forEach(paginate);});
  document.querySelectorAll('table.js-table').forEach(paginate);
})();
"""


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


# A journal line's party picker: every contact, and no add-new. The forms that
# post to a control account by hand share one search URL and one option shape, so
# a party chosen on the reconciliation screen is the same party the manual entry
# form would have chosen. There is nowhere on either form to finish creating a
# contact, and a half-made one would be a party no statement could ever show.
PARTY_SEARCH_URL = "/contacts/search-options?contact_type=all&add_new=0"

# Parties drawn into a picker before anything is typed. Where the list opens, not
# the limit of what can be chosen: typing searches the whole list server-side.
PARTY_PRELOAD = 50


def party_options(contacts: list[dict]) -> list[tuple[str, str]]:
    """Contacts as picker options, labelled with the side of the books they sit on."""
    return [
        (c["id"], f"{c.get('name', '')} ({c.get('contact_type') or 'customer'})")
        for c in contacts if c.get("id")
    ]


def searchable_select(
    name: str,
    options: list[str | tuple[str, str]],
    value: str = "",
    placeholder: str = "Search or select...",
    cls_extra: str = "",
    allow_custom: bool = False,
    search_url: str = "",
    multiple: bool = False,
    values: list[str] | None = None,
    count_label: str = "",
    aria_label: str = "",
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
    multiple: if True, options toggle instead of replacing and every selection is
        submitted under the same name, so the server reads a repeated key. Pass the
        current selection as `values`. Callers that leave this off render exactly
        what they rendered before it existed.
    count_label: summary shown in the closed input when several are selected;
        "{n}" is replaced with the count. Defaults to the same `label.n_selected`
        the bulk toolbar counts with, so a caller passes one only to say something
        other than "N selected".
    aria_label: accessible name for the visible input, for a control whose only label
        would otherwise be a column header the screen reader does not read with it.
    htmx_attrs: HTMX attributes forwarded to the hidden input (hx_get, hx_target, etc.)
    """
    count_label = count_label or t("label.n_selected")
    normalized = [
        (o, o) if isinstance(o, str) else (o[0], o[1])
        for o in options
    ]
    selected = list(values or [])
    label_by_value = {val: lbl for val, lbl in normalized}
    # Current label for display
    if multiple:
        if len(selected) == 1:
            display_label = label_by_value.get(selected[0], selected[0])
        elif selected:
            display_label = count_label.replace("{n}", str(len(selected)))
        else:
            display_label = ""
    else:
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

    if multiple:
        chosen = set(selected)
        opt_els = [
            Div(label,
                cls=_opt_cls(val) + (" combobox-option--selected" if val in chosen else ""),
                data_value=val, role="option")
            for val, label in normalized
        ]
    else:
        opt_els = [Div(label, cls=_opt_cls(val), data_value=val, role="option") for val, label in normalized]
    opt_els.append(Div(t("msg.no_results"), cls="combobox-option combobox-option--empty", style="display:none"))

    wrap_attrs: dict = {"cls": "combobox-wrap"}
    if allow_custom:
        wrap_attrs["data_allow_custom"] = "true"
    if multiple:
        wrap_attrs["data_multiple"] = "true"
        wrap_attrs["data_count_label"] = count_label
        wrap_attrs["data_empty_label"] = placeholder
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
    # The visible input is the control a screen reader lands on, so the accessible
    # name belongs there rather than on the hidden value input.
    if aria_label:
        text_input_extra["aria_label"] = aria_label
    # ARIA combobox semantics (rule i): a screen reader announces this as a
    # combobox owning a popup listbox. The dynamic wiring - aria-controls to the
    # list id, aria-expanded on open/close, aria-activedescendant to the focused
    # option - is done by initCombobox() in shell.py, which owns the unique ids.
    text_input_extra["role"] = "combobox"
    text_input_extra["aria_expanded"] = "false"
    text_input_extra["aria_haspopup"] = "listbox"
    text_input_extra["aria_autocomplete"] = "list"

    if multiple:
        # The bag carries the submitted values; the sibling hidden input carries no
        # value of its own and exists only so the JS can read the field name.
        return Div(
            Input(type="text", cls=f"combobox-input {cls_extra}".strip(),
                  value=display_label, placeholder=placeholder, autocomplete="off", **text_input_extra),
            Input(type="hidden", data_name=name, value="", **htmx_attrs),
            Div(*[Input(type="hidden", name=name, value=v,
                        data_label=label_by_value.get(v, v)) for v in selected],
                cls="combobox-selected"),
            Div(*opt_els, cls="combobox-list", role="listbox"),
            **wrap_attrs,
        )

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
    primary_editable: bool = True,
    secondary_editable: bool = True,
) -> FT:
    """Combined cell showing two separately dbl-click-editable values in one TD.

    Used for quantity+sell_by and weight+weight_unit so they share a column.
    Each span is independently double-click-to-edit via the paired-edit endpoint,
    which returns an editable_cell whose restore_url points back to paired-display.

    format_fn: optional callable(value) -> str for formatting the primary value.
    When provided, it is used instead of str(). Callers supply unit-aware formatters
    (e.g. format_qty) without coupling this generic component to inventory logic.
    primary_editable / secondary_editable: when False the corresponding value renders
    as a plain, non-clickable span - no dblclick trigger, no edit affordance. The
    sell unit gates on the same edit_inventory_amounts permission as the amount it
    pairs with, because changing the unit rewrites the amount.
    """
    pri_edit = f"/api/items/{entity_id}/field/{primary_field}/paired-edit?peer={secondary_field}"
    sec_edit = f"/api/items/{entity_id}/field/{secondary_field}/paired-edit?peer={primary_field}"
    if primary_value not in (None, ""):
        pri_disp = format_fn(primary_value) if format_fn is not None else str(primary_value)
    else:
        pri_disp = EMPTY
    sec_disp = str(secondary_value) if secondary_value not in (None, "") else EMPTY
    pri_span = (
        Span(
            pri_disp,
            cls="paired-primary",
            title="Double-click to edit",
            hx_get=pri_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        )
        if primary_editable
        else Span(pri_disp, cls="paired-primary paired-primary--readonly")
    )
    sec_span = (
        Span(
            sec_disp,
            cls="paired-secondary",
            title="Double-click to edit",
            hx_get=sec_edit,
            hx_target="closest td",
            hx_swap="outerHTML",
            hx_trigger="dblclick",
        )
        if secondary_editable
        else Span(sec_disp, cls="paired-secondary paired-secondary--readonly")
    )
    return Td(
        pri_span,
        Span(" ", cls="paired-sep"),
        sec_span,
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


# MIXED_VALUE ("Mixed") is the reserved SYSTEM value for a field whose sources disagreed — e.g. a
# merge of items whose dropdown or custom attribute differed. It is never part of a field's configured
# options, but a select holding it must still render and count as a real value instead of matching no
# option and showing empty. These helpers make that work without polluting the configured option list.
def _is_mixed(value) -> bool:
    return str(value or "").strip().casefold() == MIXED_VALUE.casefold()


def _options_with_mixed(options: list, value) -> list:
    """Append a ('Mixed', 'Mixed') option when the cell value is the reserved 'Mixed' sentinel and the
    field's options don't already include it — so a merged select renders as Mixed, not empty."""
    if not _is_mixed(value):
        return options
    def _opt_val(o):
        return o[0] if isinstance(o, tuple) else o
    if any(_is_mixed(_opt_val(o)) for o in options):
        return options
    return list(options) + [(MIXED_VALUE, MIXED_VALUE)]


def editable_cell(
    entity_id: str,
    field: str,
    value,
    cell_type: str = "text",
    options: list[str] | None = None,
    allow_custom: bool = False,
    restore_url: str | None = None,
    label_map: dict | None = None,
    placeholder: str | None = None,
    patch_url: str | None = None,
    aria_label: str | None = None,
    cls_extra: str = "",
) -> FT:
    """Table cell in edit mode. Fires HTMX PATCH on blur/change, swaps itself back to display_cell.
    label_map: optional {slug: display_name} - if set, select renders option labels from map.
    placeholder: optional grey hint text for empty number/text inputs (e.g. a suggested value).
                 It is a hint only - never submitted - so the stored value is unaffected.
    patch_url: where the edit saves to. Defaults to the item field route, so a caller that
               owns its own REST surface (a module) points the cell at its own endpoint.
    aria_label: accessible name for the control. A cell editor replaces the cell it sits
               in, so the column header is no longer beside it once the editor opens."""
    # Grey hint for empty inputs (e.g. a reorder suggestion). Kept separate from `value`
    # so it can never be saved: HTML placeholder is shown only while the input is empty and
    # is never part of the submitted form data.
    _ph = {"placeholder": str(placeholder)} if placeholder not in (None, "") else {}
    _aria = {"aria_label": str(aria_label)} if aria_label not in (None, "") else {}
    display_val = str(value) if value is not None else ""
    if cell_type == "number" and display_val:
        display_val = _normalize_number_str(display_val)
    patch_url = patch_url or f"/api/items/{entity_id}/field/{field}"
    # ESC restores from the same field route it saves to, so pointing the cell at another
    # REST surface moves both halves together instead of leaving the restore on core's.
    restore_url = restore_url or f"{patch_url}/display"
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
        options = _options_with_mixed(options, display_val)
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
                    **_aria,
                ),
                cls="cell-input-wrap",
                onkeydown=combobox_escape_js,
            )
        else:
            _opt_items = [(o, o) if isinstance(o, str) else o for o in options]
            input_el = Select(
                # Offer a selectable blank "(clear)" option so a SET optional select can be unset
                # (issue #202); `status` stays required. When empty, show a disabled placeholder.
                *([Option("— clear —", value="")] if (display_val and cell_type == "select")
                  else [] if display_val
                  else [Option("", value="", disabled=True, selected=True)]),
                *[Option(lbl, value=val, selected=(val == display_val)) for val, lbl in _opt_items],
                name="value",
                **swap, **_aria,
                hx_trigger="change",
                cls=f"cell-input cell-input--{cell_type}",
                autofocus=True,
                onkeydown=escape_js,
                onblur=blur_restore_js,
            )
    elif cell_type in ("money", "weight", "rate"):
        # A rate (unit price) may carry more precision than a money amount, so don't constrain the
        # input step to whole cents - accept any precision and normalise on the server (GDR 2e).
        step = {"money": "0.01", "weight": "0.001"}.get(cell_type, "any")
        input_el = Input(
            type="number", name="value", value=display_val, step=step,
            **swap, **_ph, **_aria,
            hx_trigger="blur delay:200ms",
            cls="cell-input cell-input--number",
            autofocus=True,
            onkeydown=escape_js,
        )
    elif cell_type == "date":
        # Native date picker: saving on `change` is what a picker gives us (there is no
        # meaningful blur-to-save for a calendar popup), and blur restores the display cell
        # so an opened-but-untouched editor never sticks. Same pair the select branch uses.
        input_el = Input(
            type="date", name="value", value=display_val[:10],
            **swap, **_aria,
            hx_trigger="change",
            cls="cell-input",
            autofocus=True,
            onkeydown=escape_js,
            onblur=blur_restore_js,
        )
    elif cell_type == "textarea":
        # Multi-line editor: Enter inserts a newline (not save), Esc cancels, blur saves.
        textarea_escape_js = (
            f"if(event.key==='Escape'){{this._escaping=true;"
            f"htmx.ajax('GET','{restore_url}',{{target:this.closest('td'),swap:'outerHTML'}});"
            f"event.preventDefault();}}"
        )
        input_el = Textarea(
            display_val, name="value", **swap, **_aria,
            hx_trigger="blur delay:200ms",
            cls="cell-input cell-textarea-input", rows="5",
            autofocus=True,
            onkeydown=textarea_escape_js,
        )
    elif cell_type == "bool":
        # Toggle: send "true"/"false" on change
        is_true = display_val.lower() in ("true", "1", "yes")
        input_el = Select(
            Option(t("settings.no"), value="false", selected=not is_true),
            Option(t("settings.yes"), value="true", selected=is_true),
            name="value",
            **swap, **_aria,
            hx_trigger="change",
            cls="cell-input cell-input--select",
            autofocus=True,
            onkeydown=escape_js,
            onblur=blur_restore_js,
        )
    else:
        input_el = Input(
            type="text", name="value", value=display_val,
            **swap, **_ph, **_aria,
            hx_trigger="blur delay:200ms",
            cls="cell-input",
            autofocus=True,
            onkeydown=escape_js,
        )

    return Td(input_el, cls=f"cell cell--editing cell--{cell_type}{(' ' + cls_extra) if cls_extra else ''}")


def _normalize_number_str(s: str) -> str:
    """Format a numeric string for display: drop .0 for integers, use :g for floats."""
    try:
        n = float(s)
        return str(int(n)) if n == int(n) else f"{n:g}"
    except (ValueError, TypeError):
        return s


def _display_val(value, cell_type: str, currency: str | None = None,
                 status_doc: tuple[str, str] | None = None) -> FT:
    """Format a value for display. Empty/null → EMPTY constant.

    status_doc: for status cells only - (doc_entity_id, doc_number) of the document
    that caused the status. The badge then reads STATUS: DOC-NUMBER with the number
    linking to the document."""
    s = str(value).strip() if value is not None else ""
    if cell_type == "bool":
        is_true = str(value).strip().lower() in ("true", "1", "yes")
        return Span("Yes" if is_true else "No", cls="badge badge--yes" if is_true else "badge badge--no")
    if cell_type == "status":
        badge_cls = f"badge badge--{s.lower().replace(' ', '-')}" if s else ""
        if s and status_doc:
            doc_id, doc_number = status_doc
            return Span(s, ": ", A(doc_number, href=f"/docs/{doc_id}", cls="badge__doc-link"), cls=badge_cls)
        return Span(s or EMPTY, cls=badge_cls)
    if cell_type == "money":
        try:
            return Span(fmt_money(s, currency), cls="cell-money") if s else Span(EMPTY)
        except ValueError:
            return Span(EMPTY)
    if cell_type == "rate":
        return Span(fmt_rate(s, currency), cls="cell-money") if s else Span(EMPTY)
    if cell_type == "number":
        if not s:
            return Span(EMPTY)
        return Span(_normalize_number_str(s), cls="cell-number")
    if cell_type == "date":
        # Store may hold a full timestamp; the cell shows the day, matching _fmt("date").
        return Span(s[:10], cls="cell-text") if s else Span(EMPTY)
    if cell_type == "weight":
        return Span(f"{s} ct", cls="cell-weight") if s else Span(EMPTY)
    if cell_type == "tags":
        tags = value if isinstance(value, list) else []
        return Span(*[Span(t, cls="tag-pill tag-pill--sm") for tag in tags]) if tags else Span(EMPTY)
    if cell_type == "image":
        if s:
            return Img(src=s, cls="cell-thumbnail", loading="lazy", alt="")
        return Span("＋", cls="cell-image-empty", title="Drop image here or click to upload")
    if cell_type == "textarea":
        # Multi-line text: preserve line breaks on display (CSS white-space: pre-wrap).
        return Span(s, cls="cell-textarea") if s else Span(EMPTY)
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
    placeholder: str | None = None,
    status_doc: tuple[str, str] | None = None,
    cls_extra: str = "",
) -> FT:
    """Read-only cell. Double-click-to-edit fires HTMX GET to fetch editable_cell.
    Image cells support drag-and-drop upload in addition to click.
    link_href: if set, renders cell value as a clickable hyperlink (e.g. SKU -> detail page).
    edit_url: custom HTMX GET URL for editing this cell. Overrides the default
              ``/api/items/{entity_id}/field/{field}/edit`` pattern.
    label_map: optional {slug: display_name} dict - if set, display_val shows the mapped name.
    placeholder: optional grey hint shown ONLY when the cell is empty (e.g. a suggested
                 reorder value). It is display-only guidance, never a stored value; the cell
                 stays click-to-edit and saving still uses whatever the user types.
    status_doc: status cells only - (doc_entity_id, doc_number) of the causing document,
                rendered inside the badge as a link (see _display_val)."""
    _x = f" {cls_extra}" if cls_extra else ""
    display_value = label_map.get(value, value) if label_map and value is not None else value
    # Normalize the reserved conflict sentinel to the canonical "Mixed" for any cell that can carry it
    # (dropdowns AND custom/free attributes left after a merge), so legacy/any-case values read alike.
    if _is_mixed(value):
        display_value = MIXED_VALUE
    inner = _display_val(display_value, cell_type, currency, status_doc=status_doc)
    # Empty cell + a suggestion -> show it greyed (a hint, not a stored value). The
    # edit trigger below is unchanged, so the cell stays fully editable.
    if placeholder not in (None, "") and (value is None or str(value).strip() in ("", EMPTY)):
        inner = Span(str(placeholder), cls="cell-suggestion", style="color:#9aa0a6;",
                     title="Suggested value - click to set")
    _edit = edit_url or f"/api/items/{entity_id}/field/{field}/edit"
    _safe_id = entity_id.replace(":", "-")
    _safe_field = re.sub(r"[^A-Za-z0-9_-]", "_", field)
    _cell_id = f"cell-{_safe_id}-{_safe_field}"

    if not editable:
        # Only render hyperlink when there's actual content (not empty/placeholder)
        if link_href and value is not None and str(value).strip() and str(value).strip() != EMPTY:
            return Td(A(inner, href=link_href, cls="table-link"), id=_cell_id, cls=f"cell cell--{cell_type}{_x}", data_col=field)
        return Td(inner, id=_cell_id, cls=f"cell cell--{cell_type}{_x}", data_col=field)

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
            cls=f"cell cell--image cell--droppable{_x}",
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
            cls=f"cell cell--{cell_type} cell--clickable{_x}",
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
            cls=f"cell cell--{cell_type} cell--clickable{_x}",
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
        cls=f"cell cell--{cell_type} cell--clickable{_x}",
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
    column_filters: dict | None = None,
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
        spec = (column_filters or {}).get(key)
        funnel = _filter_funnel_btn(spec["param"], spec["options"], spec.get("selected"),
                                    f["label"]) if spec else ""
        th_cls = f"col-{key}" + (" colfilter-th" if spec else "")
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
                funnel,
                cls=th_cls, data_key=key, draggable="true",
                title="Drag to reorder columns",
                style=th_style,
            )
        return Th(f["label"], funnel, cls=th_cls, data_key=key, draggable="true",
                   title="Drag to reorder columns", style=th_style)

    checkbox_th = [Th(Input(type="checkbox", id="select-all-rows", title=t("label.select_all")), cls="col-checkbox")] if show_checkboxes else []
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
        status_val = str(row.get("status", "") or "").lower()
        checkbox_td = [Td(Input(type="checkbox", cls="row-select", name="selected", value=entity_id,
                     data_entity_id=entity_id,
                     data_sku=row.get("sku", ""),
                     data_name=row.get("name", ""),
                     data_qty=str(row.get("quantity", 0)),
                     data_weight=str(row.get("weight", "") or ""),
                     data_weight_unit=row.get("weight_unit", ""),
                     data_sell_by=row.get("sell_by", ""),
                     data_status=status_val,
               ), cls="col-checkbox")] if show_checkboxes else []
        row_cls = "data-row data-row--inactive" if status_val in INACTIVE_ITEM_STATUSES else "data-row"
        if str(row.get("inventory_type") or "") == "component":
            row_cls += " data-row--component"  # visual cue for component (raw-material) items
        # Per-row editability escape: a row may carry _row_editable_keys naming fields
        # that render click-to-edit even when the schema marked them read-only
        # (used for draft items, whose amount fields stay authorable until commit).
        row_editable = set(row.get("_row_editable_keys") or ())
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
                    editable=f.get("editable", True) or f["key"] in row_editable,
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
    try {{ localStorage.setItem(WIDTH_KEY, JSON.stringify(w)); }} catch(e) {{}}
  }}
  // Restore persisted widths synchronously. A saved width is an explicit px value that needs no
  // committed layout, and applying it before the sticky controller can pin the header is what keeps
  // the frozen header's captured geometry in step with the body for a custom column layout.
  (function() {{
    var saved = loadWidths();
    if (saved) ths.forEach(function(th) {{ if (saved[th.dataset.key]) th.style.width = saved[th.dataset.key]; }});
  }})();
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
      // Signal an in-progress resize so the sticky-header controller leaves the header alone while
      // dragging (its per-mutation re-pin would otherwise recapture the pre-drag width every pixel).
      document.body.classList.add('col-resizing');
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
        document.body.classList.remove('col-resizing');
        // Let the sticky controller recapture the new width once, now the drag is over, so a pinned
        // header re-aligns its body to the dragged column instead of staying at the old geometry.
        window.dispatchEvent(new Event('resize'));
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
    // Archiving retires the product but leaves any remaining stock on the books. When stock is
    // still held the user must choose (keep on books vs write off) - never a silent ledger move.
    if(_selectedStockedIds().length){_stockGuardChoice(function(){_bulkImmediate('/api/items/bulk/status','bulk_status','archived');});return;}
    if(!confirm('Archive selected items? They will be hidden from the default view.')) return;
    _bulkImmediate('/api/items/bulk/status','bulk_status','archived');return;
  }
  if(action==='restore'){
    if(!confirm('Restore selected items to the catalog? They will become available again.')) return;
    _bulkImmediate('/api/items/bulk/status','bulk_status','available');return;
  }
  if(action==='expire'){
    // Same guard as archive: an expired item that still holds stock needs the keep/write-off choice.
    if(_selectedStockedIds().length){_stockGuardChoice(function(){_bulkImmediate('/api/items/bulk/expire',null,null);});return;}
    _bulkImmediate('/api/items/bulk/expire',null,null);return;
  }
  if(action==='write_off'){
    _bulkWriteOff();return;
  }
  if(action==='make_available'){
    if(!confirm('Make selected draft items available? They will count as real stock.')) return;
    _bulkImmediate('/api/items/bulk/make-available',null,null);return;
  }
  if(action==='revert_to_draft'){
    if(!confirm('Revert selected items to draft? Only items with no circulation history can revert.')) return;
    _bulkImmediate('/api/items/bulk/revert-to-draft',null,null);return;
  }
  if(action==='delete'){
    if(!confirm('Delete selected items? This cannot be undone.')) return;
    _bulkImmediate('/api/items/bulk/delete',null,null);return;
  }
  if(action==='duplicate'){
    if(!confirm('Duplicate selected items? A copy of each will be created.')) return;
    _bulkImmediate('/api/items/bulk/duplicate',null,null);return;
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
function _selectedStockedIds(){
  // Selected items that still hold stock (quantity > 0). Prefer the live rendered qty - an inline
  // edit re-renders the row's checkbox - and fall back to the selection snapshot for rows not on
  // the current page, so the guard is never skipped on stale zero data.
  var all=CelerpSelection.all();
  return Object.keys(all).filter(function(id){
    var cb=document.querySelector('.row-select[value="'+id.replace(/"/g,'\\"')+'"]');
    var q=(cb&&cb.dataset.qty!=null&&cb.dataset.qty!=='')?cb.dataset.qty:(all[id].qty||'0');
    return parseFloat(q)>0;
  });
}
function _stockGuardChoice(keepFn){
  // The unmissable two-way choice for archiving/expiring still-stocked items (GDR 2d): keep the
  // stock on the books, or write it off. Cancel and Esc are the way back; no action fires until
  // the user picks, so nothing moves on the ledger automatically.
  var existing=document.getElementById('stock-guard-choice');
  if(existing){existing.close();existing.remove();}
  var dlg=document.createElement('dialog');
  dlg.className='modal-dialog';dlg.id='stock-guard-choice';
  var body=document.createElement('div');body.className='modal-body';
  var msg=document.createElement('p');
  msg.textContent='Some selected items still hold stock. Archiving retires the product but leaves its stock on the books. What should happen to the remaining stock?';
  body.appendChild(msg);
  var acts=document.createElement('div');acts.className='modal-actions';
  function mkBtn(label,cls,fn){var b=document.createElement('button');b.type='button';b.className='btn '+cls;b.textContent=label;b.addEventListener('click',function(){dlg.close();dlg.remove();if(fn)fn();});return b;}
  acts.appendChild(mkBtn('Keep stock on books','btn--secondary',keepFn));
  acts.appendChild(mkBtn('Write off remaining stock','btn--primary',_bulkWriteOff));
  acts.appendChild(mkBtn('Cancel','btn--ghost',null));
  body.appendChild(acts);dlg.appendChild(body);
  document.body.appendChild(dlg);
  dlg.addEventListener('cancel',function(){dlg.remove();});
  dlg.showModal();
}
function _bulkWriteOff(){
  // Seed a draft write-off list from the current selection and navigate to it (the server proxy
  // replies with HX-Redirect to the new list). Data entry happens on-page in that list.
  var form=document.createElement('form');
  CelerpSelection.ids().forEach(function(id){
    var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
    form.appendChild(inp);
  });
  document.body.appendChild(form);
  htmx.ajax('POST','/api/items/bulk/write-off',{source:form,target:'#bulk-action-result',swap:'outerHTML'})
    .then(function(){form.remove();},function(){form.remove();});
}
function _liveMergeMeta(id){
  // The SKU/name shown here must reflect the CURRENT value: an inline edit changes the
  // cell without re-selecting the row, so the meta snapshotted at select time (and kept
  // in sessionStorage) goes stale. Read the live row when it is on the page; fall back to
  // the stored snapshot only for selected rows not currently rendered (other pages).
  var cb=document.querySelector('.row-select[value="'+id.replace(/"/g,'\\"')+'"]');
  if(cb){
    var tr=cb.closest('tr');
    var skuCell=tr&&tr.querySelector('[data-col="sku"]');
    var nameCell=tr&&tr.querySelector('[data-col="name"]');
    return {sku:((skuCell?skuCell.textContent:cb.dataset.sku)||'').trim(),
            name:((nameCell?nameCell.textContent:cb.dataset.name)||'').trim()};
  }
  var m=CelerpSelection.all()[id]||{};
  return {sku:m.sku||'',name:m.name||''};
}
function _populateMergeTargets(){
  var sel=document.getElementById('merge-target-select');
  if(!sel) return;
  var all=CelerpSelection.all();
  Object.keys(all).forEach(function(id){
    var meta=_liveMergeMeta(id);
    var opt=document.createElement('option');
    opt.value=id;
    opt.textContent=(meta.sku||id)+' - '+(meta.name||'');
    sel.appendChild(opt);
  });
  // Keep "New SKU" as the last choice, below the item options.
  var newSkuOpt=document.getElementById('merge-new-sku-opt');
  if(newSkuOpt) sel.appendChild(newSkuOpt);
  sel.addEventListener('change',function(){
    var confirmDiv=document.getElementById('merge-confirm');
    if(!confirmDiv) return;
    var n=CelerpSelection.count();
    var isNewSku=sel.value==='__new__';
    var targetText=sel.options[sel.selectedIndex].textContent;
    confirmDiv.innerHTML='';
    confirmDiv.style.display='flex';
    confirmDiv.style.flexDirection='column';
    confirmDiv.style.gap='0.5rem';
    confirmDiv.style.marginTop='0.5rem';
    var msg=document.createElement('span');
    msg.textContent='Merge '+n+' items into '+(isNewSku?'a new SKU':targetText)+'?';
    msg.style.fontSize='0.85rem';
    // "New SKU" reveals [dropdown] -> [enter SKU]; picking an item hides it
    // (that item's SKU wins, so there is nothing extra to show).
    var skuInput=document.getElementById('merge-resulting-sku');
    var skuArrow=document.getElementById('merge-sku-arrow');
    if(skuInput){skuInput.style.display=isNewSku?'':'none';skuInput.value='';}
    if(skuArrow){skuArrow.style.display=isNewSku?'':'none';}
    if(isNewSku&&skuInput){skuInput.focus();}
    var btnRow=document.createElement('div');
    btnRow.style.display='flex';
    btnRow.style.gap='0.5rem';
    var btn=document.createElement('button');
    btn.type='button';btn.className='btn btn--primary btn--sm';btn.textContent='Confirm';
    btn.addEventListener('click',function(){
      var skuEl=document.getElementById('merge-resulting-sku');
      // "New SKU" mode needs a typed SKU; the merge bases on the first selected
      // item (deterministic - the pick only decides the SKU, not the survivor).
      if(isNewSku && (!skuEl || !skuEl.value.trim())){
        if(skuEl) skuEl.focus();
        return;
      }
      var form=document.createElement('form');
      CelerpSelection.ids().forEach(function(id){
        var inp=document.createElement('input');inp.type='hidden';inp.name='selected';inp.value=id;
        form.appendChild(inp);
      });
      var t=document.createElement('input');t.type='hidden';t.name='target_sku_from';
      t.value=isNewSku?CelerpSelection.ids()[0]:sel.value;
      form.appendChild(t);
      if(isNewSku){
        var sk=document.createElement('input');sk.type='hidden';sk.name='resulting_sku';sk.value=skuEl.value.trim();
        form.appendChild(sk);
      }
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
      if(skuInput){skuInput.style.display='none';skuInput.value='';}
      if(skuArrow){skuArrow.style.display='none';}
      _clearBulkResult();
    });
    confirmDiv.appendChild(msg);
    btnRow.appendChild(btn);
    btnRow.appendChild(cancel);
    confirmDiv.appendChild(btnRow);
  });
}
function sendToTypeChanged(docType, docLabel){
  var targetSel=document.getElementById('send-to-target-select');
  if(!targetSel) return;
  // Reset to just "New <type>", using the type's display label not its raw key
  targetSel.innerHTML='';
  var newOpt=document.createElement('option');
  newOpt.value='__new__';newOpt.textContent='New '+(docLabel||docType);
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
  function _meta(cb){return {sku:cb.dataset.sku||'',name:cb.dataset.name||'',qty:cb.dataset.qty||'0',weight:cb.dataset.weight||'',weight_unit:cb.dataset.weightUnit||'',sell_by:cb.dataset.sellBy||'',status:cb.dataset.status||''};}
  function updateBulkToolbar(){
    var n=CelerpSelection.count();
    var toolbar=document.getElementById('bulk-toolbar');
    var countEl=document.getElementById('bulk-count');
    var clearBtn=document.getElementById('bulk-clear-btn');
    if(countEl) countEl.textContent=n+' selected';
    if(toolbar){if(n>0){toolbar.classList.add('is-active')}else{toolbar.classList.remove('is-active')}}
    if(clearBtn){clearBtn.style.display=n>0?'':'none'}
    var all=CelerpSelection.all();
    var hasDraft=false,hasNonDraft=false;
    Object.keys(all).forEach(function(id){
      if((all[id].status||'')==='draft'){hasDraft=true}else{hasNonDraft=true}
    });
    var makeAvailOpt=document.querySelector('#bulk-action-select option[value="make_available"]');
    var revertOpt=document.querySelector('#bulk-action-select option[value="revert_to_draft"]');
    if(makeAvailOpt) makeAvailOpt.hidden=!hasDraft;
    if(revertOpt) revertOpt.hidden=!hasNonDraft;
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
    var m=path.match(/\\/api\\/items\\/([^/]+)\\/field\\/quantity/);
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
            Table(header, Tbody(*[_row(r) for r in rows]), cls="data-table sticky-head", id="data-table"),
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
    # Navigate full-page (consistent with the numbered page links above), resetting to page 1.
    # The previous version did an HTMX swap hardcoded to hx_target="#inventory-content", so the
    # dropdown was dead on every page that isn't /inventory (e.g. /docs — issue #171).
    suffix = f"&{extra_params}" if extra_params else ""
    return Select(
        *[Option(f"{n} per page", value=str(n), selected=(n == current)) for n in options],
        name="per_page",
        onchange=f"window.location='{base_url}?per_page='+this.value+'&page=1{suffix}'",
        cls="filter-select per-page-select",
    )


def search_bar(placeholder: str = "Search...", target: str = "#data-table", url: str = "",
               help: FT | None = None, label: str = "") -> FT:
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
    inp = Input(
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
        title="Use a comma (or Enter) for OR - e.g. scan multiple barcodes one after another",
    )
    # Wrap only when a help affordance is attached (e.g. search_help from
    # ui.components.shell) so pages without one keep their exact markup.
    inner = Div(inp, help, cls="page-search-wrap") if help is not None else inp
    if not label:
        return inner
    # Small centered scope label ("Search available inventory") so the on-page
    # box is instantly distinguishable from the global header search.
    return Div(Small(label, cls="search-scope-label"), inner, cls="search-scope")


def table_search(table_id: str, placeholder: str = "Search…") -> FT:
    """A client-side free-text filter for a bounded `js-table` (all rows already on
    the page). Typing hides non-matching rows in place and composes with the column
    funnels; ESC clears it. `table_id` is the id of the table it filters. Pair with
    COLUMN_FILTER_JS on the page (which owns the `.js-table-search` handler)."""
    return Input(
        type="search",
        placeholder=placeholder,
        aria_label=placeholder,
        cls="search-input js-table-search",
        onkeydown="if(event.key==='Escape'){this.value='';"
                  "this.dispatchEvent(new Event('input',{bubbles:true}));}",
        **{"data-search-for": table_id},
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


def bank_account_options(bank_accounts: list[dict] | None, default_code: str | None = None) -> list:
    """Option elements for every active bank account ("1110 - Bank name").

    DRY: doc payment forms, the bulk-pay modal, and the payments settings
    deposit selector. Key names match _bank_to_dict in celerp-accounting:
    chart_account_code and bank_name. default_code pre-selects its option.
    """
    return [
        Option(
            f"{ba.get('chart_account_code', '')} - {ba.get('bank_name', '')}",
            value=ba.get("chart_account_code", ""),
            selected=(ba.get("chart_account_code") == default_code),
        )
        for ba in (bank_accounts or [])
    ]


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
        _MONEY_PREFIXES = tuple(CURRENCY_SYMBOLS.values()) + ("฿", "$", "€", "£", "¥")
        if any(s.startswith(p) for p in _MONEY_PREFIXES):
            return Td(s, cls="cell--number")
        return Td(s)

    return Table(
        Thead(Tr(*[Th(h) for h in headers])),
        Tbody(*[Tr(*[_cell(c) for c in row], cls="data-row") for row in rows]),
        cls=f"data-table {cls_extra}".strip(),
        **({"id": id} if id else {}),
    )


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
        # Only VISIBLE columns participate: a display:none column (e.g. price/tax hidden on a no-money
        # audit/transfer list) must never be a resize neighbour, or dragging its visible left column
        # resizes nothing (the qty-can't-grow-into-on-hand bug).
        "var ths=Array.from(t.querySelectorAll('thead th')).filter(function(h){return getComputedStyle(h).display!=='none';});if(!ths.length)return;"
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

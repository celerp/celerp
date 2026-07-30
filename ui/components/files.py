# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared files section component.

Used by contacts, documents, and any other entity that supports file attachments.
Single source for document tag slugs via _DOCUMENT_TAGS.
"""
from __future__ import annotations

from fasthtml.common import *

from ui.components.attrs import hx_vals, esc_attr
from ui.i18n import t

# Single source of truth for tag slugs - drives both UI and i18n keys
_DOCUMENT_TAGS: tuple[str, ...] = (
    "bills",
    "contracts",
    "correspondence",
    "receipts",
    "registrations",
    "certificates",
    "photos",
    "shipping",
    "legal",
    "other",
    # inventory product tags (harmless in contacts/docs - unused in practice)
    "product_images",
    "spec_sheets",
    "safety_docs",
    "view_360",
)

# Tags shown for inventory items - product-focused, excludes business-doc tags
_ITEM_TAGS: tuple[str, ...] = (
    "product_images",
    "spec_sheets",
    "safety_docs",
    "view_360",
    "certificates",
    "photos",
    "other",
)

# Page size for file list pagination
FILES_PAGE_SIZE = 20


def _fmt_size(size: int) -> str:
    if size < 1_048_576:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1_048_576:.1f} MB"


def _tag_label(slug: str) -> str:
    return t(f"file_tag.{slug}") if slug else ""


def _safe_id(entity_id: str) -> str:
    """Return a colon-free DOM id fragment (colons are invalid in CSS selectors)."""
    return entity_id.replace(":", "-")


def _fmt_date(iso: str | None) -> str:
    """Format ISO datetime to YYYY-MM-DD for display; return '--' if absent."""
    if not iso:
        return "--"
    return iso[:10]


def files_section(
    entity_type: str,
    entity_id: str,
    files: list[dict],
    *,
    can_tag: bool = True,
    can_describe: bool = True,
    can_set_hero: bool = False,
    can_upload: bool = True,
    hide_product_images: bool = False,
    show_linked: bool = True,
    page: int = 1,
    sort_dir: str = "desc",
    tag_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    title: str | None = None,
    show_preview: bool = True,
    compact: bool = False,
    base_url: str | None = None,
) -> FT:
    """Render the files section for any entity.

    Args:
        entity_type:  e.g. "contact", "doc", "item"
        entity_id:    the entity's id
        files:        list of file dicts from the projection
        can_tag:      whether to show tag editing controls
        can_set_hero: whether to show hero star toggle (items only)
        can_describe: whether to show description editing controls
        show_linked:  whether to show the "Linked To" column (False for items)
        page:         current page (1-indexed)
        sort_dir:     "desc" (newest first) or "asc"
        tag_filter:   filter to this tag slug
        date_from:    filter uploaded_at >= this date (YYYY-MM-DD)
        date_to:      filter uploaded_at <= this date (YYYY-MM-DD)
        search:       filter by filename, description, or linked_ref (case-insensitive substring)
    """
    # Default derives the per-entity REST base; an aggregated/virtual view (e.g. all company files)
    # passes its own base_url so the sort + pagination links target a real _section route.
    base_url = base_url or f"/{entity_type}s/{entity_id}/files"
    sid = _safe_id(entity_id)  # safe DOM id fragment (no colons)
    _tags = _ITEM_TAGS if entity_type == "item" else _DOCUMENT_TAGS

    # Product images are excluded BEFORE sort/filter/pagination where they would
    # clutter business documents (the aggregated Company Files view) - never on
    # an item's own page, where the product images are the point. Excluding them
    # any later (e.g. a client-side column filter) leaves the pager counting
    # rows the table never shows.
    if hide_product_images:
        files = [f for f in files if (f.get("document_tag") or "") != "product_images"]

    # ── Sort ─────────────────────────────────────────────────────────────────
    def _uploaded_at_key(f: dict) -> str:
        return f.get("uploaded_at") or ""

    sorted_files = sorted(files, key=_uploaded_at_key, reverse=(sort_dir == "desc"))
    # Hero always at top regardless of date sort
    sorted_files = sorted(sorted_files, key=lambda f: 0 if f.get("is_hero") else 1)

    # ── Filter ───────────────────────────────────────────────────────────────
    if tag_filter:
        sorted_files = [f for f in sorted_files if f.get("document_tag", "") == tag_filter]
    if date_from:
        sorted_files = [f for f in sorted_files if (f.get("uploaded_at") or "") >= date_from]
    if date_to:
        # date_to is inclusive: compare against date_to + "T23:59:59Z"
        sorted_files = [f for f in sorted_files if (f.get("uploaded_at") or "")[:10] <= date_to]
    if search:
        q = search.lower()
        sorted_files = [
            f for f in sorted_files
            if q in (f.get("filename") or "").lower()
            or q in (f.get("description") or "").lower()
            or q in (f.get("linked_ref") or "").lower()
        ]

    # ── Pagination ────────────────────────────────────────────────────────────
    total = len(sorted_files)
    total_pages = max(1, (total + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    page_files = sorted_files[(page - 1) * FILES_PAGE_SIZE: page * FILES_PAGE_SIZE]

    # ── Filter bar ───────────────────────────────────────────────────────────
    tag_opts = [Option(t("label.all_tags"), value="")] + [
        Option(_tag_label(slug), value=slug, selected=(slug == tag_filter))
        for slug in _tags
    ]

    next_sort = "asc" if sort_dir == "desc" else "desc"
    sort_arrow = "▼" if sort_dir == "desc" else "▲"

    # Hidden fields carry all filter state so every individual control can
    # include the form and get a complete refresh URL.
    _hx = {"hx_get": f"{base_url}/_section", "hx_target": f"#files-section-{sid}",
           "hx_swap": "outerHTML", "hx_include": f"#files-filter-form-{sid}"}
    # Compact (narrow column, e.g. the Manufacturing tab): tag + search only, narrower; the date
    # range stays as hidden state so it survives refreshes but doesn't wrap the bar to two rows.
    if compact:
        date_controls = [Input(type="hidden", name="date_from", value=date_from),
                         Input(type="hidden", name="date_to", value=date_to)]
    else:
        date_controls = [
            Input(type="text", name="date_from", value=date_from, placeholder="YYYY-MM-DD",
                  cls="form-input form-input--sm", style="width:130px;", **_hx, hx_trigger="change"),
            Input(type="text", name="date_to", value=date_to, placeholder="YYYY-MM-DD",
                  cls="form-input form-input--sm", style="width:130px;", **_hx, hx_trigger="change"),
        ]
    filter_bar = Form(
        Select(*tag_opts, name="tag_filter", cls="form-input form-input--sm",
               style=f"width:{'120px' if compact else '160px'};", **_hx, hx_trigger="change"),
        *date_controls,
        Input(type="search", name="search", value=search, placeholder=t("label.search"),
              cls="form-input form-input--sm", style=f"width:{'130px' if compact else '180px'};",
              **_hx, hx_trigger="keyup changed delay:400ms, blur"),
        # Hidden state fields - keep current values across partial refreshes
        Input(type="hidden", name="sort_dir", value=sort_dir),
        Input(type="hidden", name="page", value=str(page)),
        id=f"files-filter-form-{sid}",
        cls="files-filter-bar",
        style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;",
    )

    # ── "Linked To" column ───────────────────────────────────────────────────
    has_linked = show_linked

    # ── Column resize JS ─────────────────────────────────────────────────────
    # Vanilla JS: drag resize handles between <th> elements.
    # Injected once per section render; keyed by sid so multiple sections
    # on the same page don't collide.
    resize_js = f"""
(function(){{
  var table=document.getElementById('files-table-{sid}');
  if(!table) return;
  var ths=table.querySelectorAll('thead th');
  ths.forEach(function(th,i){{
    // Skip last column (delete button - fixed narrow)
    if(i===ths.length-1) return;
    var handle=document.createElement('div');
    handle.className='col-resize-handle';
    handle.style='position:absolute;right:0;top:0;bottom:0;width:5px;cursor:col-resize;z-index:1;';
    th.style.position='relative';
    th.appendChild(handle);
    var startX,startW;
    handle.addEventListener('mousedown',function(e){{
      startX=e.pageX; startW=th.offsetWidth;
      document.addEventListener('mousemove',onMove);
      document.addEventListener('mouseup',onUp);
      e.preventDefault();
    }});
    function onMove(e){{ th.style.width=(startW+e.pageX-startX)+'px'; }}
    function onUp(){{ document.removeEventListener('mousemove',onMove); document.removeEventListener('mouseup',onUp); }}
  }});
}})();
"""

    # ── Table rows ────────────────────────────────────────────────────────────
    file_rows = []
    for f in page_files:
        fid = f.get("id", "")
        fname = f.get("filename", "file")
        size = f.get("size", 0) or 0
        doc_tag = f.get("document_tag") or ""
        desc = f.get("description") or ""
        uploaded_at = _fmt_date(f.get("uploaded_at"))
        linked_ref = f.get("linked_ref") or ""
        linked_url = f.get("linked_url") or ""
        # Files from a foreign entity carry their own URL overrides.
        file_download_url = f.get("_download_url") or f"{base_url}/{fid}/download"
        # Action base: tag/describe/hero/delete target (defaults to this entity's base_url)
        file_action_base = f.get("_action_base_url") or base_url
        no_delete = bool(f.get("_no_delete"))
        # When actions target a different entity, refresh via base_url/_section after mutation
        cross_entity = file_action_base != base_url

        def _refresh_section_js() -> str:
            """JS snippet: re-fetch the section after a cross-entity mutation."""
            return (
                f"fetch('{base_url}/_section')"
                f".then(function(r){{return r.text();}}).then(function(html){{"
                f"var sec=document.getElementById('files-section-{sid}');"
                f"if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{sid}');if(ns&&window.htmx)htmx.process(ns);}}else location.reload();"
                f"}});"
            )

        # The Excel funnel reads this explicit value (the tag's display label) rather than the cell's
        # textContent, which for the editable <select> would be every option concatenated.
        _tag_fv = _tag_label(doc_tag) if doc_tag else ""
        if can_tag:
            tag_opts_row = [Option(t("label.no_tag"), value="")] + [
                Option(_tag_label(slug), value=slug, selected=(slug == doc_tag))
                for slug in _tags
            ]
            if cross_entity:
                tag_cell = Td(
                    Select(
                        *tag_opts_row,
                        name="document_tag",
                        cls="form-input form-input--xs file-tag-select",
                        onchange=(
                            f"(function(sel){{var fd=new FormData();fd.append('document_tag',sel.value);"
                            f"fetch('{file_action_base}/{fid}/tag',{{method:'PATCH',body:fd}})"
                            f".then(function(){{" + _refresh_section_js() + f"}})}})this"
                        ),
                    ),
                    data_filter_value=_tag_fv,
                )
            else:
                tag_cell = Td(
                    Select(
                        *tag_opts_row,
                        name="document_tag",
                        cls="form-input form-input--xs file-tag-select",
                        hx_post=f"{file_action_base}/{fid}/tag",
                        hx_target=f"#files-section-{sid}",
                        hx_swap="outerHTML",
                        hx_trigger="change",
                    ),
                    data_filter_value=_tag_fv,
                )
        else:
            tag_cell = Td(Span(_tag_label(doc_tag), cls="badge badge--muted") if doc_tag else Span(),
                          data_filter_value=_tag_fv)

        if can_describe:
            _desc_fetch_then = (
                _refresh_section_js() if cross_entity else
                f".then(function(r){{return r.text();}}).then(function(html){{"
                f"var sec=document.getElementById('files-section-{sid}');"
                f"if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{sid}');if(ns&&window.htmx)htmx.process(ns);}}else location.reload();"
                f"}})"
            )
            _desc_fetch = (
                f"var fd=new FormData();fd.append('description',inp.value);"
                f"fetch('{file_action_base}/{fid}/description',{{method:'POST',body:fd}})"
                + (_refresh_section_js() if cross_entity else
                   f".then(function(r){{return r.text();}}).then(function(html){{"
                   f"var sec=document.getElementById('files-section-{sid}');"
                   f"if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{sid}');if(ns&&window.htmx)htmx.process(ns);}}else location.reload();"
                   f"}});")
            )
            desc_cell = Td(
                Span(
                    desc if desc else "--",
                    cls="file-desc muted",
                    title=t("label.dblclick_to_edit"),
                    **{
                        "ondblclick": (
                            f"(function(el){{var inp=document.createElement('input');"
                            f"inp.type='text';"
                            f"var cur=el.textContent.trim();inp.value=cur==='--'?'':cur;"
                            f"inp.className='form-input form-input--sm';inp.style='width:100%';"
                            f"var cancelled=false;"
                            f"inp.onblur=function(){{if(cancelled)return;"
                            + _desc_fetch +
                            f"}};"
                            f"inp.onkeydown=function(e){{if(e.key==='Enter')inp.blur();"
                            f"if(e.key==='Escape'){{cancelled=true;var orig=el.dataset.orig;el.textContent=orig;inp.replaceWith(el);}}}};"
                            f"el.dataset.orig=el.textContent;el.replaceWith(inp);inp.focus();"
                            f"}})(this)"
                        )
                    },
                ),
            )
        else:
            desc_cell = Td(Span(desc, cls="muted"))

        # "Linked to" cell - show only when column is visible
        if has_linked:
            if linked_ref:
                linked_content = (
                    A(linked_ref, href=linked_url, cls="doc-ref-link")
                    if linked_url else
                    Span(linked_ref, cls="badge badge--muted")
                )
            else:
                linked_content = Span("--", cls="muted")
            linked_cell = Td(linked_content)

        row_cells = [
            Td(Span(uploaded_at, cls="muted")),
            Td(
                Img(src=file_download_url, style="height:48px;width:auto;border-radius:3px;margin-right:6px;vertical-align:middle;object-fit:cover;")
                if (show_preview and f.get("mime", "").startswith("image/")) else "",
                A(fname, href=file_download_url, target="_blank", rel="noopener",
                  cls="file-link file-link--clip", title=fname),
                cls="files-name-cell",
            ),
            tag_cell,
            desc_cell,
        ]
        if has_linked:
            row_cells.append(linked_cell)
        if can_set_hero:
            is_hero = f.get("is_hero", False)
            is_image = (f.get("mime", "").startswith("image/"))
            if is_image:
                hero_cell = Td(
                    Button(
                        "⭐" if is_hero else "☆",
                        hx_post=f"{file_action_base}/{fid}/hero",
                        hx_target=f"#files-section-{sid}",
                        hx_swap="outerHTML",
                        cls=f"btn btn--ghost btn--xs{'  btn--hero-active' if is_hero else ''}",
                        title="Set as hero image" if not is_hero else "Hero image",
                    ),
                )
            else:
                hero_cell = Td()
            row_cells.append(hero_cell)
        row_cells += [
            Td(Span(_fmt_size(size), cls="muted")),
            Td(
                Button(
                    "×",
                    hx_delete=f"{file_action_base}/{fid}",
                    hx_target=f"#files-section-{sid}",
                    hx_swap="outerHTML",
                    hx_confirm=f"{t('action.delete_file')}: {esc_attr(fname)}?",
                    cls="btn btn--ghost btn--xs",
                    title=t("action.delete_file"),
                ) if not no_delete else Span(),
            ),
        ]
        # data-row is required for the Excel column funnels (COLUMN_FILTER_JS filters tr.data-row).
        row_cls = ("data-row files-row--hero" if f.get("is_hero") else "data-row")
        file_rows.append(Tr(*row_cells, cls=row_cls))

    # Date column header with sort arrow (clickable)
    col_count = 6 + (1 if has_linked else 0) + (1 if can_set_hero else 0)
    date_th = Th(
        A(
            f"{t('label.upload_date')} {sort_arrow}",
            href="#",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_vals=hx_vals({"sort_dir": next_sort, "page": "1", "tag_filter": tag_filter, "date_from": date_from, "date_to": date_to, "search": search}),
            cls="sort-link",
            style="white-space:nowrap;",
        ),
    )

    # Description column gets extra width via inline style (not in compact/narrow columns).
    desc_th = Th(t("label.file_description") if can_describe else "",
                 style=None if compact else "min-width:200px;")

    # Excel-style funnels: filter by Tag and, when present, by what each file is Linked To. Client-side,
    # additive over the server filter bar. (Product images are excluded server-side
    # above, so no default funnel exclusion is needed.)
    from ui.components.table import filter_th, COLUMN_FILTER_JS
    header_cells = [
        date_th,
        Th(t("th.filename")),
        filter_th(t("label.tag"), 2),
        desc_th,
    ]
    if has_linked:
        header_cells.append(filter_th(t("label.linked_to"), 4))
    if can_set_hero:
        header_cells.append(Th("Hero", style="text-align:center;width:48px;"))
    header_cells += [Th(t("th.size")), Th()]

    table = Table(
        Thead(Tr(*header_cells)),
        Tbody(*file_rows) if file_rows else Tbody(
            Tr(Td(P(t("label.no_files_yet"), cls="muted"), colspan=str(col_count)))
        ),
        id=f"files-table-{sid}",
        cls="data-table data-table--compact",
    )

    # ── Pagination ────────────────────────────────────────────────────────────
    pagination = None
    if total_pages > 1:
        pager_items = []
        for pg in range(1, total_pages + 1):
            pager_items.append(
                A(
                    str(pg),
                    href="#",
                    cls=f"page-link{'  page-link--active' if pg == page else ''}",
                    hx_get=f"{base_url}/_section",
                    hx_target=f"#files-section-{sid}",
                    hx_swap="outerHTML",
                    hx_vals=hx_vals({"page": str(pg), "sort_dir": sort_dir, "tag_filter": tag_filter, "date_from": date_from, "date_to": date_to, "search": search}),
                )
            )
        pagination = Div(*pager_items, cls="pagination", style="margin-top:8px;display:flex;gap:4px;")

    # ── Upload dropzone ───────────────────────────────────────────────────────
    # Named init function keyed by sid so it survives outerHTML swaps.
    # After each swap (htmx or plain fetch) we re-call it on the new zone.
    # The global _celerpDZ registry lets the new section call its own init
    # once inserted (htmx:afterSettle fires for hx-* swaps; for plain fetch
    # swaps we call it directly after setting outerHTML).
    drop_js = f"""
(function(){{
  if(!window._celerpDZ) window._celerpDZ={{}};
  function initDropZone(){{
    var zone=document.getElementById('file-drop-zone-{sid}');
    var inp=document.getElementById('file-drop-input-{sid}');
    if(!zone||!inp||zone._dzInited) return;
    zone._dzInited=true;
    function upload(file){{
      var fd=new FormData(); fd.append('file',file);
      var txt=zone.querySelector('.file-drop-text');
      if(txt) txt.textContent='{t("msg.uploading")}...';
      fetch('{base_url}',{{method:'POST',headers:{{'HX-Request':'true'}},body:fd}})
        .then(function(r){{if(!r.ok) throw new Error('Upload failed'); return r.text();}})
        .then(function(html){{
          var sec=document.getElementById('files-section-{sid}');
          if(sec){{
            sec.outerHTML=html;
            var ns=document.getElementById('files-section-{sid}');
            if(ns&&window.htmx) htmx.process(ns);
            if(window._celerpDZ['{sid}']) window._celerpDZ['{sid}']();
          }} else location.reload();
        }})
        .catch(function(e){{if(txt) txt.textContent='{t("msg.drop_files_here")}'; alert(e.message);}});
    }}
    zone.addEventListener('click',function(e){{
      if(e.target===zone||e.target.closest('.file-drop-text,.file-drop-icon,.file-drop-hint')) inp.click();
    }});
    inp.addEventListener('change',function(){{ if(inp.files.length) upload(inp.files[0]); }});
    zone.addEventListener('dragover',function(e){{e.preventDefault();zone.classList.add('file-drop-zone--active');}});
    zone.addEventListener('dragleave',function(){{zone.classList.remove('file-drop-zone--active');}});
    zone.addEventListener('drop',function(e){{
      e.preventDefault(); zone.classList.remove('file-drop-zone--active');
      if(e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
    }});
  }}
  window._celerpDZ['{sid}']=initDropZone;
  initDropZone();
  document.addEventListener('htmx:afterSettle',function(e){{
    if(e.detail&&e.detail.target&&e.detail.target.id==='files-section-{sid}') initDropZone();
  }});
}})();
"""

    upload_zone = Div(
        Div("📁", cls="file-drop-icon"),
        Div(t("msg.drop_files_here"), cls="file-drop-text"),
        Input(type="file", id=f"file-drop-input-{sid}", style="display:none"),
        Script(drop_js),
        cls="file-drop-zone",
        id=f"file-drop-zone-{sid}",
    )

    children = [
        H3(title or t("label.files"), cls="section-title"),
        filter_bar,
        table,
        Script(resize_js),
        Script(COLUMN_FILTER_JS),
        Script(f"window.celerpRefreshFilters&&window.celerpRefreshFilters(document.getElementById('files-section-{sid}'));"),
    ]
    if pagination:
        children.append(pagination)
    if can_upload:
        children.append(upload_zone)

    return Div(
        *children,
        id=f"files-section-{sid}",
    )

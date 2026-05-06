# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Shared files section component.

Used by contacts, documents, and any other entity that supports file attachments.
Single source for document tag slugs via _DOCUMENT_TAGS.
"""
from __future__ import annotations

from fasthtml.common import *

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


def _files_section(
    entity_type: str,
    entity_id: str,
    files: list[dict],
    *,
    can_tag: bool = True,
    can_describe: bool = True,
    page: int = 1,
    sort_dir: str = "desc",
    tag_filter: str = "",
    date_from: str = "",
    date_to: str = "",
) -> FT:
    """Render the files section for any entity.

    Args:
        entity_type: e.g. "contact", "doc"
        entity_id:   the entity's id
        files:       list of file dicts from the projection
        can_tag:     whether to show tag editing controls
        can_describe: whether to show description editing controls
        page:        current page (1-indexed)
        sort_dir:    "desc" (newest first) or "asc"
        tag_filter:  filter to this tag slug
        date_from:   filter uploaded_at >= this date (YYYY-MM-DD)
        date_to:     filter uploaded_at <= this date (YYYY-MM-DD)
    """
    base_url = f"/{entity_type}s/{entity_id}/files"
    sid = _safe_id(entity_id)  # safe DOM id fragment (no colons)

    # ── Sort ─────────────────────────────────────────────────────────────────
    def _uploaded_at_key(f: dict) -> str:
        return f.get("uploaded_at") or ""

    sorted_files = sorted(files, key=_uploaded_at_key, reverse=(sort_dir == "desc"))

    # ── Filter ───────────────────────────────────────────────────────────────
    if tag_filter:
        sorted_files = [f for f in sorted_files if f.get("document_tag", "") == tag_filter]
    if date_from:
        sorted_files = [f for f in sorted_files if (f.get("uploaded_at") or "") >= date_from]
    if date_to:
        # date_to is inclusive: compare against date_to + "T23:59:59Z"
        sorted_files = [f for f in sorted_files if (f.get("uploaded_at") or "")[:10] <= date_to]

    # ── Pagination ────────────────────────────────────────────────────────────
    total = len(sorted_files)
    total_pages = max(1, (total + FILES_PAGE_SIZE - 1) // FILES_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    page_files = sorted_files[(page - 1) * FILES_PAGE_SIZE: page * FILES_PAGE_SIZE]

    # ── Filter bar ───────────────────────────────────────────────────────────
    # Build refresh URL with all current params (HTMX GET to proxy /_section)
    def _filter_url(**overrides) -> str:
        params = {"page": page, "sort_dir": sort_dir, "tag_filter": tag_filter,
                  "date_from": date_from, "date_to": date_to}
        params.update(overrides)
        parts = "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, "", 1) or k == "page")
        return f"{base_url}/_section?{parts}" if parts else f"{base_url}/_section"

    tag_opts = [Option(t("label.all_tags"), value="")] + [
        Option(_tag_label(slug), value=slug, selected=(slug == tag_filter))
        for slug in _DOCUMENT_TAGS
    ]

    next_sort = "asc" if sort_dir == "desc" else "desc"
    sort_arrow = "▼" if sort_dir == "desc" else "▲"

    filter_bar = Form(
        Select(
            *tag_opts,
            name="tag_filter",
            cls="form-input form-input--sm",
            style="width:160px;",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_include=f"#files-filter-form-{sid}",
            hx_trigger="change",
        ),
        Input(
            type="date",
            name="date_from",
            value=date_from,
            cls="form-input form-input--sm",
            style="width:140px;",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_include=f"#files-filter-form-{sid}",
            hx_trigger="change",
        ),
        Input(
            type="date",
            name="date_to",
            value=date_to,
            cls="form-input form-input--sm",
            style="width:140px;",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_include=f"#files-filter-form-{sid}",
            hx_trigger="change",
        ),
        Input(
            type="search",
            name="search",
            placeholder=t("label.search"),
            cls="form-input form-input--sm",
            style="width:180px;",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_include=f"#files-filter-form-{sid}",
            hx_trigger="input changed delay:300ms",
        ),
        # Hidden state fields
        Input(type="hidden", name="sort_dir", value=sort_dir),
        Input(type="hidden", name="page", value=str(page)),
        id=f"files-filter-form-{sid}",
        cls="files-filter-bar",
        style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;",
    )

    # ── Table rows ────────────────────────────────────────────────────────────
    file_rows = []
    for f in page_files:
        fid = f.get("id", "")
        fname = f.get("filename", "file")
        size = f.get("size", 0) or 0
        doc_tag = f.get("document_tag") or ""
        desc = f.get("description") or ""
        uploaded_at = _fmt_date(f.get("uploaded_at"))

        if can_tag:
            tag_opts_row = [Option(t("label.no_tag"), value="")] + [
                Option(_tag_label(slug), value=slug, selected=(slug == doc_tag))
                for slug in _DOCUMENT_TAGS
            ]
            tag_cell = Td(
                Select(
                    *tag_opts_row,
                    name="document_tag",
                    cls="form-input form-input--xs file-tag-select",
                    hx_post=f"{base_url}/{fid}/tag",
                    hx_target=f"#files-section-{sid}",
                    hx_swap="outerHTML",
                    hx_trigger="change",
                ),
            )
        else:
            tag_cell = Td(Span(_tag_label(doc_tag), cls="badge badge--muted") if doc_tag else Span())

        if can_describe:
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
                            f"inp.onblur=function(){{var fd=new FormData();fd.append('description',inp.value);"
                            f"fetch('{base_url}/{fid}/description',{{method:'POST',body:fd}})"
                            f".then(function(r){{return r.text();}}).then(function(html){{"
                            f"var sec=document.getElementById('files-section-{sid}');"
                            f"if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{sid}');if(ns&&window.htmx)htmx.process(ns);}}else location.reload();"
                            f"}})}};"
                            f"inp.onkeydown=function(e){{if(e.key==='Enter')inp.blur();"
                            f"if(e.key==='Escape'){{el.textContent=inp.value=el.dataset.orig;inp.blur();}}}};"
                            f"el.dataset.orig=el.textContent;el.replaceWith(inp);inp.focus();"
                            f"}})(this)"
                        )
                    },
                ),
            )
        else:
            desc_cell = Td(Span(desc, cls="muted"))

        file_rows.append(Tr(
            Td(Span(uploaded_at, cls="muted")),
            Td(A(fname, href=f"{base_url}/{fid}/download", cls="file-link")),
            tag_cell,
            desc_cell,
            Td(Span(_fmt_size(size), cls="muted")),
            Td(
                Button(
                    "×",
                    hx_delete=f"{base_url}/{fid}",
                    hx_target=f"#files-section-{sid}",
                    hx_swap="outerHTML",
                    hx_confirm=f"{t('action.delete_file')}: {fname}?",
                    cls="btn btn--ghost btn--xs",
                    title=t("action.delete_file"),
                ),
            ),
        ))

    # Date column header with sort arrow (clickable)
    date_th = Th(
        A(
            f"{t('label.upload_date')} {sort_arrow}",
            href="#",
            hx_get=f"{base_url}/_section",
            hx_target=f"#files-section-{sid}",
            hx_swap="outerHTML",
            hx_vals=f'{{"sort_dir":"{next_sort}","page":"1","tag_filter":"{tag_filter}","date_from":"{date_from}","date_to":"{date_to}"}}',
            cls="sort-link",
            style="white-space:nowrap;",
        ),
    )

    table = Table(
        Thead(
            Tr(
                date_th,
                Th(t("th.filename")),
                Th(t("label.tag")) if can_tag else Th(),
                Th(t("label.file_description")) if can_describe else Th(),
                Th(t("th.size")),
                Th(),
            )
        ),
        Tbody(*file_rows) if file_rows else Tbody(
            Tr(Td(P(t("label.no_files_yet"), cls="muted"), colspan="6"))
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
                    hx_vals=f'{{"page":"{pg}","sort_dir":"{sort_dir}","tag_filter":"{tag_filter}","date_from":"{date_from}","date_to":"{date_to}"}}',
                )
            )
        pagination = Div(*pager_items, cls="pagination", style="margin-top:8px;display:flex;gap:4px;")

    # ── Upload dropzone ───────────────────────────────────────────────────────
    drop_js = f"""
(function(){{
  var zone=document.getElementById('file-drop-zone-{sid}');
  var inp=document.getElementById('file-drop-input-{sid}');
  if(!zone||!inp) return;
  function upload(file){{
    var fd=new FormData(); fd.append('file',file);
    var txt=zone.querySelector('.file-drop-text');
    if(txt) txt.textContent='{t("msg.uploading")}...';
    fetch('{base_url}',{{method:'POST',headers:{{'HX-Request':'true'}},body:fd}})
      .then(function(r){{if(!r.ok) throw new Error('Upload failed'); return r.text();}})
      .then(function(html){{
        var sec=document.getElementById('files-section-{sid}');
        if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{sid}');if(ns&&window.htmx)htmx.process(ns);}}
        else location.reload();
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
        H3(t("label.files"), cls="section-title"),
        filter_bar,
        table,
    ]
    if pagination:
        children.append(pagination)
    children.append(upload_zone)

    return Div(
        *children,
        id=f"files-section-{sid}",
    )

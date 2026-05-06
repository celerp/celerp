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
    "receipts",
    "certificates",
    "photos",
    "shipping",
    "legal",
    "other",
)


def _fmt_size(size: int) -> str:
    if size < 1_048_576:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1_048_576:.1f} MB"


def _tag_label(slug: str) -> str:
    return t(f"file_tag.{slug}") if slug else ""


def _files_section(
    entity_type: str,
    entity_id: str,
    files: list[dict],
    *,
    can_tag: bool = True,
    can_describe: bool = True,
) -> FT:
    """Render the files section for any entity.

    Args:
        entity_type: e.g. "contact", "doc"
        entity_id:   the entity's id
        files:       list of file dicts from the projection
        can_tag:     whether to show tag editing controls
        can_describe: whether to show description editing controls
    """
    base_url = f"/{entity_type}s/{entity_id}/files"

    file_rows = []
    for f in files:
        fid = f.get("id", "")
        fname = f.get("filename", "file")
        size = f.get("size", 0) or 0
        doc_tag = f.get("document_tag") or ""
        desc = f.get("description") or ""

        # Tag cell - DDE click-to-edit
        if can_tag:
            tag_opts = [Option(t("label.no_tag"), value="")] + [
                Option(_tag_label(slug), value=slug, selected=(slug == doc_tag))
                for slug in _DOCUMENT_TAGS
            ]
            tag_cell = Td(
                Select(
                    *tag_opts,
                    name="document_tag",
                    cls="form-input form-input--xs file-tag-select",
                    hx_post=f"{base_url}/{fid}/tag",
                    hx_target=f"#files-section-{entity_id}",
                    hx_swap="outerHTML",
                    hx_trigger="change",
                ),
            )
        else:
            tag_cell = Td(Span(_tag_label(doc_tag), cls="badge badge--muted") if doc_tag else Span())

        # Description cell - DDE dblclick-to-edit
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
                            f"var sec=document.getElementById('files-section-{entity_id}');"
                            f"if(sec){{sec.outerHTML=html;var ns=document.getElementById('files-section-{entity_id}');if(ns&&window.htmx)htmx.process(ns);}}else location.reload();"
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
            Td(A(fname, href=f"{base_url}/{fid}/download", cls="file-link")),
            tag_cell,
            desc_cell,
            Td(Span(_fmt_size(size), cls="muted")),
            Td(
                Button(
                    "×",
                    hx_delete=f"{base_url}/{fid}",
                    hx_target=f"#files-section-{entity_id}",
                    hx_swap="outerHTML",
                    hx_confirm=f"{t('action.delete_file')}: {fname}?",
                    cls="btn btn--ghost btn--xs",
                    title=t("action.delete_file"),
                ),
            ),
        ))

    # Search bar (client-side JS filter)
    search_bar = Div(
        Input(
            type="search",
            placeholder=t("label.search"),
            cls="form-input form-input--sm",
            style="width:200px;",
            oninput=(
                f"(function(q){{var rows=document.querySelectorAll('#files-table-{entity_id} tbody tr');"
                f"rows.forEach(function(r){{var text=r.textContent.toLowerCase();"
                f"r.style.display=text.includes(q.toLowerCase())?'':'none';}});"
                f"}})(this.value)"
            ),
        ),
        cls="files-search-bar",
        style="margin-bottom:8px;",
    )

    table = Table(
        Thead(
            Tr(
                Th(t("th.filename")),
                Th(t("label.tag")) if can_tag else Th(),
                Th(t("label.file_description")) if can_describe else Th(),
                Th(t("th.size")),
                Th(),
            )
        ),
        Tbody(*file_rows) if file_rows else Tbody(
            Tr(Td(P(t("label.no_files_yet"), cls="muted"), colspan="5"))
        ),
        id=f"files-table-{entity_id}",
        cls="data-table data-table--compact",
    )

    # Upload dropzone
    drop_js = f"""
(function(){{
  var zone=document.getElementById('file-drop-zone-{entity_id}');
  var inp=document.getElementById('file-drop-input-{entity_id}');
  if(!zone||!inp) return;
  function upload(file){{
    var fd=new FormData(); fd.append('file',file);
    var txt=zone.querySelector('.file-drop-text');
    if(txt) txt.textContent='{t("msg.uploading")}...';
    fetch('{base_url}',{{method:'POST',headers:{{'HX-Request':'true'}},body:fd}})
      .then(function(r){{if(!r.ok) throw new Error('Upload failed'); return r.text();}})
      .then(function(html){{
        var sec=document.getElementById('files-section-{entity_id}');
        if(sec) {{ sec.outerHTML=html; var ns=document.getElementById('files-section-{entity_id}'); if(ns&&window.htmx) htmx.process(ns); }}
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
        Input(type="file", id=f"file-drop-input-{entity_id}", style="display:none"),
        Script(drop_js),
        cls="file-drop-zone",
        id=f"file-drop-zone-{entity_id}",
    )

    return Div(
        H3(t("label.files"), cls="section-title"),
        search_bar,
        table,
        upload_zone,
        id=f"files-section-{entity_id}",
    )

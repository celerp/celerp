# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared notes tab component.

Used by both contacts (entity_type="contact") and documents/lists
(entity_type="doc" or "list").  All callers pass in pre-fetched notes so
this module stays free of I/O and is pure rendering.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fasthtml.common import *

from ui.i18n import t


def _safe_id(s: str) -> str:
    """Return a CSS-selector-safe HTML id (replaces ':' pseudo-class chars)."""
    return s.replace(":", "-")


def _fmt_ts(iso: str, zone: object) -> str:
    if not iso:
        return ""
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso[:16].replace("T", " ")


def notes_tab(
    *,
    entity_id: str,
    notes: list[dict],
    add_url: str,
    edit_url: str,       # format string with {note_id}
    delete_url: str,     # format string with {note_id}
    refresh_target: str, # htmx target selector for all swaps
    note_field: str = "note",   # key in note dict holding the text
    author_field: str = "author_name",
    tz: str = "UTC",
) -> FT:
    """Render a full-CRUD notes tab: add form + timeline with edit/delete.

    Parameters
    ----------
    entity_id:      the parent entity (doc/list/contact id) - used for CSS ids
    notes:          pre-fetched list of note dicts (newest first preferred)
    add_url:        POST target for new note form
    edit_url:       GET URL template for inline edit form; use {note_id}
    delete_url:     DELETE URL template; use {note_id}
    refresh_target: htmx target that receives the refreshed tab on every action
    note_field:     dict key holding the note text (contacts use "note"; docs same)
    author_field:   dict key holding the author display name
    tz:             IANA timezone string
    """
    try:
        zone: object = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        from datetime import timezone as _tz
        zone = _tz.utc

    note_form = Form(
        Div(
            Textarea(
                name="note",
                placeholder=t("label.add_note_placeholder"),
                rows="3",
                cls="form-input",
                style="width:100%",
            ),
            cls="form-group",
        ),
        Div(
            Button(t("btn.add_note"), type="submit", cls="btn btn--primary btn--sm"),
            cls="form-actions",
            style="margin-top:0.5rem",
        ),
        hx_post=add_url,
        hx_target=refresh_target,
        hx_swap="innerHTML",
        cls="section-card",
    )

    if not notes:
        return Div(note_form, P(t("label.no_notes_yet"), cls="empty-state-msg"))

    timeline = []
    for n in notes:
        note_id = n.get("note_id") or n.get("id") or ""
        text = n.get(note_field) or ""
        author = n.get(author_field) or ""
        created = n.get("created_at") or ""
        updated = n.get("updated_at")
        ts_display = _fmt_ts(created, zone)
        if updated:
            ts_display += f" (edited {_fmt_ts(updated, zone)})"
        initials = "".join(w[0].upper() for w in author.split()[:2]) if author else "?"
        safe_note_id = _safe_id(note_id)
        timeline.append(Div(
            Div(
                Span(initials, cls="note-author-badge", title=author),
                Div(
                    Span(author, cls="note-author-name") if author else "",
                    Small(ts_display, cls="note-timestamp"),
                    cls="note-meta",
                ),
                cls="note-header",
            ),
            P(text, cls="note-text"),
            Div(
                Button(
                    t("btn.edit"),
                    hx_get=edit_url.format(note_id=note_id),
                    hx_target=f"#note-{safe_note_id}",
                    hx_swap="outerHTML",
                    cls="btn btn--ghost btn--xs",
                ),
                Button(
                    t("btn.delete"),
                    hx_delete=delete_url.format(note_id=note_id),
                    hx_target=refresh_target,
                    hx_swap="innerHTML",
                    hx_confirm=t("confirm.delete_note") if hasattr(t, "__call__") else "Delete this note?",
                    cls="btn btn--ghost btn--xs btn--danger",
                ),
                cls="note-actions",
            ),
            cls="note-item",
            id=f"note-{safe_note_id}",
        ))

    return Div(note_form, *timeline)


def note_edit_form(
    *,
    note_id: str,
    current_text: str,
    save_url: str,     # PATCH target
    cancel_url: str,   # GET target to cancel (re-fetches notes tab)
    refresh_target: str,
    note_field: str = "note",
) -> FT:
    """Inline edit form for a single note (replaces the note-item div)."""
    safe_note_id = _safe_id(note_id)
    return Div(
        Form(
            Textarea(current_text, name="note", rows="3", cls="form-input", style="width:100%"),
            Div(
                Button(t("btn.save"), type="submit", cls="btn btn--primary btn--xs"),
                Button(
                    t("btn.cancel"),
                    type="button",
                    cls="btn btn--secondary btn--xs",
                    hx_get=cancel_url,
                    hx_target=refresh_target,
                    hx_swap="innerHTML",
                ),
                cls="form-row",
            ),
            hx_patch=save_url,
            hx_target=refresh_target,
            hx_swap="innerHTML",
        ),
        cls="note-item note-item--editing",
        id=f"note-{safe_note_id}",
    )

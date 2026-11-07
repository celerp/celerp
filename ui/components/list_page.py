# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The parts of a list page (GEN-012): a header with a summary line, the status widgets, one
filters panel with a label above every filter, then the table and pager. The panel is a plain
GET form, so every filter lives in the URL (GDR 2m): selects submit on change, the search stays
live, and Apply shows only while a custom date range is being typed. Reset is a link to the page.

Every rule these parts need sits under ``.list-page`` in app.css, so a page takes the layout
only when it wraps itself in ``Div(..., cls="list-page")``."""

from __future__ import annotations

from urllib.parse import parse_qsl

from fasthtml.common import A, Button, Div, Form, Input, Label, Option, Select, Span

from ui.components.shell import page_header
from ui.i18n import t

DATE_KEYS = ("preset", "from", "to")

# A preset submits at once and its from/to are dropped so they cannot override it; Custom only
# reveals the range (and the panel's Apply) for the dates to be typed.
_PERIOD_ON_CHANGE = (
    "var f=this.form,c=this.value==='custom';"
    "f.querySelectorAll('.period-custom').forEach(function(e){e.hidden=!c;});"
    "if(c){f.elements['from'].focus();return;}"
    "f.querySelectorAll('.date-input').forEach(function(i){i.disabled=true;});f.submit();"
)


def _presets(lang: str | None) -> list[tuple[str, str]]:
    # Imported here: the reports routes import the shell, which a component must not loop back into.
    from ui.routes.reports import _date_presets
    return _date_presets(lang)


def list_page_header(title: str, *actions, summary="") -> object:
    """The page title, its actions, and a muted summary line under the title (str or FT)."""
    return page_header(title, *actions, summary=summary)


def filter_field(label: str, control, *, field_id: str, wide: bool = False) -> object:
    """One filter: its label above its control, the label tied to the control by id."""
    if not control.attrs.get("id"):
        control.attrs["id"] = field_id
    return Div(
        Label(label, fr=field_id, cls="filter-field-label"),
        control,
        cls="filter-field filter-field--wide" if wide else "filter-field",
    )


def select_field(name: str, label: str, options: list[tuple[str, str]], value: str, *,
                 lang: str | None = None, locked: bool = False,
                 on_change: str = "this.form.submit()") -> object:
    """A labelled select that submits its form on change. options: [(value, label)], labels
    already translated. locked=True means the page fixes the value (a menu entry chose it), so
    only a hidden field carries it. on_change replaces the plain submit, e.g. to drop fields
    that the new value makes stale first."""
    if locked:
        return Input(type="hidden", name=name, value=value)
    field_id = f"filter-{name}"
    return filter_field(label, Select(
        *[Option(text, value=key, selected=key == value) for key, text in options],
        name=name, id=field_id, cls="form-input form-input--sm filter-select",
        onchange=on_change,
    ), field_id=field_id)


def period_fields(date_from: str, date_to: str, preset: str, *, lang: str | None = None) -> tuple:
    """(Period, From, To) for a filter panel. From and To show only for Custom."""
    is_custom = preset == "custom"
    period = filter_field(t("filter.period", lang), Select(
        *[Option(label, value=key, selected=key == preset) for key, label in _presets(lang)],
        name="preset", id="filter-preset", cls="form-input form-input--sm date-preset-select",
        onchange=_PERIOD_ON_CHANGE,
    ), field_id="filter-preset")
    date_from_field = filter_field(t("filter.from", lang), Input(
        type="date", name="from", id="filter-from", value=date_from, max=date_to or "",
        cls="form-input form-input--sm date-input",
    ), field_id="filter-from")
    date_to_field = filter_field(t("filter.to", lang), Input(
        type="date", name="to", id="filter-to", value=date_to, min=date_from or "",
        cls="form-input form-input--sm date-input",
    ), field_id="filter-to")
    for field in (date_from_field, date_to_field):
        field.attrs["class"] += " period-custom"
        field.attrs["hidden"] = not is_custom
    return period, date_from_field, date_to_field


def filter_panel(action: str, *fields, hidden: dict[str, str] | None = None, reset_href: str,
                 show_apply: bool = False, lang: str | None = None, panel_id: str | None = None) -> object:
    """The page's one filters form. hidden carries the state no field shows (sort, per page, a
    type fixed by the menu); empty values are dropped. Apply submits typed dates, so it shows
    with the custom range (show_apply, then toggled by the Period select)."""
    hidden_inputs = [Input(type="hidden", name=k, value=v) for k, v in (hidden or {}).items() if v]
    return Form(
        *hidden_inputs,
        *fields,
        Div(
            Button(t("btn.apply", lang), type="submit", cls="btn btn--secondary btn--sm period-custom",
                   hidden=not show_apply),
            A(t("btn.reset", lang), href=reset_href, cls="btn btn--ghost btn--sm filter-reset"),
            cls="filter-actions",
        ),
        action=action, method="get", cls="list-filters", **({"id": panel_id} if panel_id else {}),
    )


def date_range_select(base_url: str, date_from: str, date_to: str, preset: str,
                      extra_params: str = "", lang: str | None = None) -> object:
    """The one date control of a list page not yet on the panel: a preset dropdown that reveals
    a from/to range (with Apply) only when Custom is chosen. extra_params ("&k=v...") rides along
    as hidden fields so switching the dates keeps the search and every other filter."""
    hidden_params = [
        Input(type="hidden", name=k, value=v)
        for k, v in parse_qsl(extra_params.lstrip("&"), keep_blank_values=True)
        if k and k not in DATE_KEYS
    ]
    is_custom = preset == "custom"
    on_change = (
        "var f=this.form,r=f.querySelector('.date-range-custom'),c=this.value==='custom';"
        "r.hidden=!c;"
        "if(c){f.elements['from'].focus();return;}"
        "f.querySelectorAll('.date-input').forEach(function(i){i.disabled=true;});f.submit();"
    )
    return Form(
        *hidden_params,
        Select(
            *[Option(label, value=key, selected=key == preset) for key, label in _presets(lang)],
            name="preset", cls="form-input form-input--sm date-preset-select",
            aria_label=t("filter.date_range", lang), onchange=on_change,
        ),
        Span(
            Input(type="date", name="from", value=date_from, max=date_to or "", cls="date-input"),
            Span("--", cls="date-sep"),
            Input(type="date", name="to", value=date_to, min=date_from or "", cls="date-input"),
            Button(t("btn.apply", lang), type="submit", cls="btn btn--secondary btn--sm"),
            cls="date-range-custom", hidden=not is_custom,
        ),
        action=base_url, method="get", cls="date-range-form",
    )

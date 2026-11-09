# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The list page kit (GEN-012, ui/components/list_page.py): header with a summary line, labelled
filter fields, and one GET filters panel whose Reset is a link and whose Apply is a submit."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from bs4 import BeautifulSoup
from fasthtml.common import A, Input, to_xml

from ui.components.list_page import (
    date_range_select, filter_field, filter_panel, list_page_header, period_fields, select_field,
)
from ui.components.shell import page_header

ROOT = Path(__file__).resolve().parents[1]
NEW_KEYS = ("filter.period", "filter.from", "filter.to", "filter.records", "filter.invoices_in_period")


def _soup(ft) -> BeautifulSoup:
    return BeautifulSoup(to_xml(ft), "html.parser")


def _assert_labels_tied(soup: BeautifulSoup) -> None:
    labels = soup.find_all("label")
    assert labels
    for label in labels:
        control = soup.find(id=label["for"])
        assert control is not None and control.name in {"input", "select"}, label


# ── Header ───────────────────────────────────────────────────────────────────

def test_page_header_without_summary_is_unchanged():
    # The markup every page (and the report goldens) had before the summary keyword existed.
    expected = ('<div class="page-header">\n  <h1 class="page-title">Lists</h1>\n'
                '  <div class="page-actions">\n<a href="/new">New</a>  </div>\n</div>\n')
    assert to_xml(page_header("Lists", A("New", href="/new"))) == expected
    assert to_xml(page_header("Lists", A("New", href="/new"), summary="")) == expected
    assert to_xml(list_page_header("Lists", A("New", href="/new"))) == expected


def test_header_summary_sits_under_the_title_and_keeps_the_actions():
    soup = _soup(list_page_header("Invoices", A("New", href="/new"), summary="12 invoices in this period"))
    heading = soup.select_one(".page-header > .page-heading")
    assert heading.select_one("h1.page-title").get_text() == "Invoices"
    assert heading.select_one(".page-subtitle").get_text() == "12 invoices in this period"
    assert soup.select_one(".page-header > .page-actions a")["href"] == "/new"


def test_header_summary_accepts_markup():
    soup = _soup(list_page_header("Invoices", summary=A("AR", href="/ar", id="docs-summary")))
    assert soup.select_one(".page-subtitle a#docs-summary")["href"] == "/ar"


# ── Fields ───────────────────────────────────────────────────────────────────

def test_filter_field_label_is_tied_to_its_control():
    soup = _soup(filter_field("Search", Input(name="q"), field_id="filter-q", wide=True))
    assert soup.select_one("div.filter-field.filter-field--wide")
    assert soup.find("label")["for"] == "filter-q"
    assert soup.find("input")["id"] == "filter-q"


def test_filter_field_keeps_a_control_id_it_already_has():
    soup = _soup(filter_field("Search", Input(name="q", id="search-input"), field_id="search-input"))
    _assert_labels_tied(soup)


def test_select_field_submits_on_change_and_marks_the_value():
    soup = _soup(select_field("type", "Type", [("", "All"), ("quotation", "Quotation")], "quotation"))
    _assert_labels_tied(soup)
    select = soup.find("select")
    assert select["name"] == "type" and select["onchange"] == "this.form.submit()"
    assert select.find("option", selected=True)["value"] == "quotation"
    assert [o.get_text() for o in select.find_all("option")] == ["All", "Quotation"]


def test_locked_select_field_only_carries_the_value():
    soup = _soup(select_field("type", "Type", [("", "All"), ("quotation", "Quotation")], "quotation", locked=True))
    assert soup.find("select") is None and soup.find("label") is None
    hidden = soup.find("input")
    assert hidden["type"] == "hidden" and hidden["name"] == "type" and hidden["value"] == "quotation"


def test_period_fields_hide_from_and_to_unless_custom():
    period, date_from, date_to = period_fields("2026-01-01", "2026-03-31", "this_month", lang="en")
    for field in (period, date_from, date_to):
        _assert_labels_tied(_soup(field))
    assert _soup(period).find("select")["name"] == "preset"
    assert _soup(period).find("option", selected=True)["value"] == "this_month"
    assert [_soup(f).find("label").get_text() for f in (period, date_from, date_to)] == ["Period", "From", "To"]
    for field in (date_from, date_to):
        assert _soup(field).select_one("div.period-custom").has_attr("hidden")

    _, date_from, date_to = period_fields("2026-01-01", "2026-03-31", "custom", lang="en")
    for field in (date_from, date_to):
        assert not _soup(field).select_one("div.period-custom").has_attr("hidden")
    assert _soup(date_from).find("input")["max"] == "2026-03-31"
    assert _soup(date_to).find("input")["min"] == "2026-01-01"


# ── Panel ────────────────────────────────────────────────────────────────────

def _panel(preset: str = "this_month", **kw) -> BeautifulSoup:
    fields = (filter_field("Search", Input(name="q"), field_id="filter-q"),
              select_field("type", "Type", [("", "All"), ("audit", "Audit")], ""),
              *period_fields("", "", preset, lang="en"))
    return _soup(filter_panel("/lists", *fields, hidden={"sort": "date", "dir": "desc", "per_page": ""},
                              reset_href="/lists", show_apply=preset == "custom", lang="en", **kw))


def test_panel_is_one_get_form_that_carries_the_hidden_state():
    soup = _panel()
    forms = soup.find_all("form")
    assert len(forms) == 1
    form = forms[0]
    assert form["method"] == "get" and form["action"] == "/lists" and "list-filters" in form["class"]
    hidden = {i["name"]: i["value"] for i in form.find_all("input", type="hidden")}
    assert hidden == {"sort": "date", "dir": "desc"}, "an empty value is not carried"
    _assert_labels_tied(soup)


def test_panel_reset_is_a_link_and_apply_is_a_submit():
    soup = _panel()
    reset = soup.select_one(".filter-actions a.filter-reset")
    assert reset["href"] == "/lists" and reset.get_text() == "Reset"
    apply = soup.select_one(".filter-actions button")
    assert apply["type"] == "submit" and apply.get_text() == "Apply"


def test_panel_apply_shows_only_for_custom_dates():
    assert _panel("this_month").select_one(".filter-actions button").has_attr("hidden")
    assert not _panel("custom").select_one(".filter-actions button").has_attr("hidden")


def test_panel_id_is_optional():
    assert _panel().find("form").get("id") is None
    assert _panel(panel_id="lists-filters").find("form")["id"] == "lists-filters"


# ── The moved SAL-002 date control ──────────────────────────────────────────

def test_documents_keeps_the_date_range_select_alias():
    from ui.routes import documents
    assert documents._date_range_select is date_range_select


def test_date_range_select_carries_extra_params_but_not_dates():
    soup = _soup(date_range_select("/payments", "", "", "this_month", extra_params="&q=x&preset=all&from=1", lang="en"))
    hidden = {i["name"]: i["value"] for i in soup.find_all("input", type="hidden")}
    assert hidden == {"q": "x"}
    assert soup.select_one(".date-range-custom").has_attr("hidden")


# ── CSS and locales ──────────────────────────────────────────────────────────

def test_new_css_is_scoped_to_the_list_page():
    css = (ROOT / "ui" / "static" / "app.css").read_text()
    block = css[css.index("List page layout (GEN-012)"):]
    block = block[block.index("*/") + 2:block.index("/* ───", block.index("*/"))]
    selectors = [s.strip() for rule in re.findall(r"([^{}]+)\{", block) for s in rule.split(",")]
    assert selectors and all(s.startswith(".list-page ") for s in selectors), selectors
    assert "repeat(auto-fit, minmax(120px, 1fr))" in block


def test_new_keys_follow_date_range_in_every_locale():
    english = json.loads((ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    files = sorted((ROOT / "ui" / "locales").glob("*.json"))
    assert len(files) == 12
    for path in files:
        keys = list(json.loads(path.read_text(encoding="utf-8")))
        at = keys.index("filter.date_range")
        assert tuple(keys[at + 1:at + 1 + len(NEW_KEYS)]) == NEW_KEYS, path.name
        if path.stem != "en":
            strings = json.loads(path.read_text(encoding="utf-8"))
            assert all(strings[k] != english[k] for k in NEW_KEYS), path.name
            assert "{n}" in strings["filter.invoices_in_period"], path.name

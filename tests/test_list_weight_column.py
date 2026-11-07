"""The Lists table's Weight column: one shared-unit rule with the printed documents, a dash when the
lines do not share one unit, and no column at all when nothing on the page is weighed."""

from __future__ import annotations

import re

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.documents import _list_table
from celerp.services.line_measures import shared_unit_total


def _row(ref: str, **kw) -> dict:
    return {"entity_id": ref, "ref_id": ref, "list_type": "quotation", "item_count": 1, "total": 1, **kw}


def _headers(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th\b.*?</th>", html, re.S)]


def test_shared_unit_total_needs_every_line_in_one_named_unit():
    assert shared_unit_total([(2.5, "ct"), ("1.5", "ct")]) == (4.0, "ct")
    assert shared_unit_total([(2.5, "ct"), (1, "kg")]) is None
    assert shared_unit_total([(2.5, "ct"), (None, "ct")]) is None
    assert shared_unit_total([(2.5, "ct"), (1, None)]) is None
    assert shared_unit_total([]) is None


def test_weight_shows_its_unit_and_a_dash_when_unknown():
    i18n.set_lang("en")
    html = to_xml(_list_table([
        _row("Q-1", total_weight=4.0, weight_unit="kg", has_weight=True),
        _row("Q-2", total_weight=None, weight_unit=None, has_weight=True),
    ]))
    assert i18n.t("th.weight") in _headers(html)
    cells = re.findall(r'<td class="cell--number">(.*?)</td>', html, re.S)
    assert any(c.startswith("4 ") and "kg" in c for c in cells), cells
    q2 = re.search(r"Q-2.*?</tr>", html, re.S).group(0)
    assert "4 " not in q2


def test_weight_column_hidden_when_no_row_is_weighed():
    i18n.set_lang("en")
    html = to_xml(_list_table([_row("Q-1", total_weight=None, has_weight=False)]))
    headers = _headers(html)
    assert i18n.t("th.weight") not in headers
    body_row = re.search(r"<tbody>\s*<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
    assert len(re.findall(r"<td\b", body_row)) == len(headers)

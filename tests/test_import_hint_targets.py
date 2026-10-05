# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard card promises "Import is always at the top of the page", and
its links open the list pages with an arrow on that page's Import button. Every
list page with an Import button therefore carries exactly one data-import-hint, in
the page header's action bar. The UI runs against the real API in process (owner_ui).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

_EN = json.loads((Path(__file__).parent.parent / "ui" / "locales" / "en.json").read_text())

_LIST_PAGES = ["/inventory", "/contacts/customers", "/contacts/vendors", "/docs", "/lists",
               "/subscriptions?direction=sales", "/subscriptions?direction=purchasing"]


def _hint_count(html: str) -> int:
    """data-import-hint attributes in the markup (the shell's script names the selector)."""
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S).count("data-import-hint")


def _header_actions(html: str) -> str:
    """The page header's action bar, nested divs (the search bar) included."""
    start = html.find('<div class="page-actions">')
    if start < 0:
        return ""
    depth = 0
    for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start:start + tag.end()]
    return ""


@pytest.mark.parametrize("path", _LIST_PAGES)
async def test_every_list_page_has_exactly_one_import_hint(owner_ui, path):
    r = await owner_ui.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert _hint_count(r.text) == 1, path
    actions = _header_actions(r.text)
    hinted = re.search(r'<a[^>]*data-import-hint[^>]*>', actions)
    assert hinted, f"{path}: the hinted Import button sits in the header action bar"
    assert "/import" in hinted.group(0)
    # The arrow says "Click Import", so the button it points at says Import.
    inner = re.search(r'<a[^>]*data-import-hint[^>]*>(.*?)</a>', actions, re.S).group(1)
    label = re.sub(r"<[^>]+>", "", inner).strip()
    assert label == _EN["btn.import"], (path, label)


@pytest.mark.parametrize("path", ["/subscriptions?direction=sales", "/subscriptions?direction=purchasing"])
async def test_subscriptions_shell_follows_browser_language(owner_ui, path):
    """The shell around a subscriptions list is built for this request: German
    browser, German arrow text and nav, and the signed-in user's menu."""
    de = json.loads((Path(__file__).parent.parent / "ui" / "locales" / "de.json").read_text())
    r = await owner_ui.get(path, headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert r.status_code == 200
    assert json.dumps(de["shell.import_hint"])[1:-1] in r.text  # as the shell serializes it
    assert _EN["shell.import_hint"] not in r.text


async def test_inventory_empty_state_import_link_is_not_a_second_target(owner_ui):
    # A search with no match shows the empty state; the arrow still has one target.
    r = await owner_ui.get("/inventory?q=no-such-item-anywhere")
    assert r.status_code == 200
    assert _hint_count(r.text) == 1


@pytest.mark.parametrize("path, home", [
    ("/crm/import/contacts", "/contacts/customers"),
    ("/lists/import", "/lists"),
    ("/subscriptions/import", "/subscriptions"),
])
async def test_contacts_import_back_button_label(owner_ui, path, home):
    """An import page's back button goes to its own list and says Back, never
    "Back to settings" (the contacts page was fixed with the setup form; lists and
    subscriptions had the same mislabel)."""
    import html as _html
    r = await owner_ui.get(path)
    assert r.status_code == 200
    back = re.search(rf'<a[^>]*href="{re.escape(home)}"[^>]*>([^<]*)</a>', _header_actions(r.text))
    assert back, f"{path}: a back button to {home}"
    assert _html.unescape(back.group(1)).strip() == _EN["btn.back"]
    assert "Back to settings" not in r.text


# Each list page's search label is one complete keyed sentence per page and per
# filter, never a lower-cased label dropped into a template ("available-Bestand
# durchsuchen", "verkaufsabonnements suchen"). English reads as it always has.
_DE = json.loads((Path(__file__).parent.parent / "ui" / "locales" / "de.json").read_text())
_SEARCH_LABELS = [
    ("/inventory", "inventory.search_available", "Search available inventory"),
    ("/inventory?status=sold", "inventory.search_sold", "Search sold inventory"),
    ("/inventory?status=memo_out", "inventory.search_memo_out", "Search memo out inventory"),
    ("/inventory?status=nonsense", "inventory.search_any", "Search inventory"),
    ("/docs", "documents.search_all", "Search documents"),
    ("/docs?type=invoice", "documents.search_invoice", "Search invoices"),
    ("/docs?type=list", "documents.search_lists", "Search lists"),
    ("/contacts/customers", "contacts.search_customers", "Search customers"),
    ("/contacts/vendors", "contacts.search_vendors", "Search vendors"),
    ("/subscriptions?direction=sales", "subscriptions.search_sales", "Search sales subscriptions"),
    ("/subscriptions?direction=purchasing", "subscriptions.search_purchasing", "Search purchasing subscriptions"),
]


def _search_label(html: str) -> str:
    m = re.search(r'<small class="search-scope-label">(.*?)</small>', html, re.S)
    assert m, "the page has a search label"
    return m.group(1).strip()


@pytest.mark.parametrize("path,key,english", _SEARCH_LABELS)
async def test_search_label_is_one_keyed_sentence(owner_ui, path, key, english):
    r = await owner_ui.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert _search_label(r.text) == english == _EN.get(key), path
    r = await owner_ui.get(path, headers={"Accept-Language": "de"})
    assert r.status_code == 200, (path, r.status_code)
    assert key in _DE and _DE[key] != english, key
    assert _search_label(r.text) == _DE[key], path


@pytest.mark.parametrize("path,kind", [("/contacts/customers", "customer"), ("/contacts/vendors", "vendor")])
async def test_contacts_new_button_and_placeholder_are_keyed(owner_ui, path, kind):
    """"New {type}" fed with the plural label minus its last letter gave "Neues
    Lieferante" in German. The button and the search placeholder are keyed per type."""
    for lang, cat in (("en", _EN), ("de", _DE)):
        r = await owner_ui.get(path, headers={"Accept-Language": lang})
        assert r.status_code == 200
        assert f">{cat[f'contacts.new_{kind}']}</button>" in r.text, (lang, path)
        assert f'placeholder="{cat[f"contacts.search_{kind}s_placeholder"]}"' in r.text, (lang, path)
    assert _EN[f"contacts.new_{kind}"] == {"customer": "New Customer", "vendor": "New Vendor"}[kind]
    assert _DE[f"contacts.new_{kind}"] == {"customer": "Neuer Kunde", "vendor": "Neuer Lieferant"}[kind]


def test_import_hint_names_the_file_and_the_template():
    """The arrow says which file to bring: a spreadsheet saved as CSV or .xlsx, with a
    template on the import page."""
    hint = _EN["shell.import_hint"]
    assert "CSV" in hint and ".xlsx" in hint, hint
    assert "template" in hint, hint


@pytest.mark.parametrize("path", _LIST_PAGES)
async def test_every_import_target_takes_csv_or_xlsx_and_offers_a_template(owner_ui, path):
    """What the arrow says holds on every page its Import button opens."""
    r = await owner_ui.get(path)
    href = re.search(r'<a[^>]*data-import-hint[^>]*>', _header_actions(r.text)).group(0)
    target = re.search(r'href="([^"]+)"', href).group(1)
    page = (await owner_ui.get(target)).text
    assert 'accept=".csv,.xlsx"' in page, target
    assert _EN["btn.download_template"] in page, target

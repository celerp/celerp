# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Scanner resolution of GTIN / RFID-EPC and inbound duplicate allowance.

Three browser-verified behaviours:

1. Scanning a stored GTIN into the document scanner must resolve to the item that
   carries that GTIN and append its line. At merge base the scan-resolve path has
   no exact gtin stage and only a fuzzy text fallback, so a GTIN that also appears
   in another item's name resolves to that decoy instead of the item whose GTIN
   matches; the target line is never appended.

2. Scanning a stored RFID-EPC must likewise resolve by exact tag to the item that
   carries it. At merge base there is no rfid_epc stage, so the fuzzy fallback
   picks a decoy whose name contains the EPC string and the tagged item's line is
   never appended.

3. The client physical-duplicate guard must apply only to outbound customer-stock
   documents (invoice/memo), not to inbound documents. At merge base the guard
   runs on ALL doc types, so an inbound bill wrongly blocks a repeated scan of the
   same item and only one line appears where two are required.

4. A repeat scan of a splittable item raises its one line, and the line(s) the last
   scan run touched keep the scanned highlight until the next run, which moves it.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser


def _sku_row_count(page, sku: str) -> int:
    """Count line rows whose SKU input holds *sku* as its live VALUE PROPERTY.

    A scanned/filled row sets the input's `.value` property, never the HTML
    `value` attribute, so an attribute selector never matches a client-appended
    row. Read the property in the page instead."""
    return page.evaluate(
        """(sku) => Array.from(document.querySelectorAll('#line-body [data-name=\"sku\"]'))"""
        """.filter(e => e.value === sku).length""",
        sku,
    )


def _wait_for_sku_row(page, sku: str, timeout: int = 8000) -> None:
    page.wait_for_function(
        """(sku) => Array.from(document.querySelectorAll('#line-body [data-name=\"sku\"]'))"""
        """.some(e => e.value === sku)""",
        arg=sku, timeout=timeout,
    )


def _seed_item(api, tag: str, *, gtin=None, rfid_epc=None, barcode=None,
               allow_splitting=True) -> dict:
    """Seed one stock item and return its id/sku plus any identifiers set."""
    sku = f"SCAN-{tag}"
    payload = {
        "status": "available",
        "sku": sku,
        "name": f"Scan Widget {tag}",
        "sell_by": "piece",
        "quantity": 5,
        "allow_splitting": allow_splitting,
    }
    if gtin is not None:
        payload["gtin"] = gtin
    if rfid_epc is not None:
        payload["rfid_epc"] = rfid_epc
    if barcode is not None:
        payload["barcode"] = barcode
    r = api.post("/items", json=payload)
    assert r.status_code in {200, 201}, f"item create failed: {r.text}"
    item = r.json()
    return {"id": item.get("id") or item.get("entity_id"), "sku": sku,
            "gtin": gtin, "rfid_epc": rfid_epc, "barcode": barcode}


def _open_doc(page, ui_server, api, doc_type: str) -> None:
    doc_id = api.post("/docs", json={"doc_type": doc_type, "status": "draft"}).json()["id"]
    page.goto(f"{ui_server}/docs/{doc_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#scan-bar-input", timeout=8000)


def _scan(page, code: str) -> None:
    inp = page.locator("#scan-bar-input")
    inp.click()
    inp.fill(code)
    inp.press("Enter")
    page.locator("#scan-bar-add").click()
    page.wait_for_function("() => !document.getElementById('scan-bar-input').disabled", timeout=8000)


def _seed_name_decoy(api, tag: str, code: str) -> str:
    """Seed a decoy item whose NAME contains *code* but which does not carry it as an
    identifier, so only a fuzzy text search would match it. Returns its sku."""
    decoy_sku = f"DECOY-{tag}"
    r = api.post("/items", json={
        "status": "available", "sku": decoy_sku,
        "name": f"Spare Widget {code} unit {tag}",
        "sell_by": "piece", "quantity": 3,
    })
    assert r.status_code in {200, 201}, f"decoy create failed: {r.text}"
    return decoy_sku


def test_browser_gtin_scanner_input(page, ui_server, api):
    """Typing a stored GTIN into the document scanner must append the line of the
    item that carries that GTIN, not a text match.

    RED at merge base: there is no exact gtin stage, only a fuzzy text fallback, so
    the GTIN resolves to a decoy whose name contains the digits and the tagged
    item's line is never appended (its row count stays 0)."""
    tag = uuid.uuid4().hex[:6]
    gtin = "98765430"
    item = _seed_item(api, tag, gtin=gtin)
    decoy_sku = _seed_name_decoy(api, tag, gtin)

    _open_doc(page, ui_server, api, "invoice")
    _scan(page, gtin)

    _wait_for_sku_row(page, item["sku"], timeout=10000)
    assert _sku_row_count(page, item["sku"]) == 1, \
        "scanning the item's GTIN must append exactly the line of the item that carries it"
    assert _sku_row_count(page, decoy_sku) == 0, \
        "a name-only text match must not be picked over the exact GTIN owner"


def test_browser_rfid_epc_scanner_input(page, ui_server, api):
    """Typing a stored RFID-EPC into the document scanner must append the line of the
    item that carries that tag, by exact match, not a text match.

    RED at merge base: there is no rfid_epc stage, only a fuzzy text fallback, so
    the EPC resolves to a decoy whose name contains the EPC string and the tagged
    item's line is never appended (its row count stays 0)."""
    tag = uuid.uuid4().hex[:6]
    epc = f"EPC{tag.upper()}"
    item = _seed_item(api, tag, rfid_epc=epc)
    decoy_sku = _seed_name_decoy(api, tag, epc)

    _open_doc(page, ui_server, api, "invoice")
    _scan(page, epc)

    _wait_for_sku_row(page, item["sku"], timeout=10000)
    assert _sku_row_count(page, item["sku"]) == 1, \
        "scanning the item's RFID-EPC must append exactly the line of the item that carries it"
    assert _sku_row_count(page, decoy_sku) == 0, \
        "a name-only text match must not be picked over the exact RFID-EPC owner"


def test_invoice_duplicate_blocked_inbound_allowed(page, ui_server, api):
    """The physical-duplicate guard blocks a repeat on an outbound invoice but an
    inbound bill must allow the same item to be scanned twice.

    Part (a) is the control and passes at merge base. Part (b) is RED at merge
    base: the client guard runs on all doc types, so the bill wrongly blocks the
    second scan and only one line appears where two are required."""
    tag = uuid.uuid4().hex[:6]
    barcode = str(uuid.uuid4().int)[:12]
    item = _seed_item(api, tag, barcode=barcode, allow_splitting=False)

    # (a) Control: an outbound invoice blocks the duplicate scan (one row).
    _open_doc(page, ui_server, api, "invoice")
    _scan(page, item["barcode"])
    _wait_for_sku_row(page, item["sku"])
    assert _sku_row_count(page, item["sku"]) == 1

    _scan(page, item["barcode"])
    page.wait_for_timeout(1500)
    assert _sku_row_count(page, item["sku"]) == 1, \
        "the invoice must block a duplicate non-splittable scan"

    # (b) Inbound bill: repeats are allowed, so both scans append a line.
    _open_doc(page, ui_server, api, "bill")
    _scan(page, item["barcode"])
    _wait_for_sku_row(page, item["sku"])
    _scan(page, item["barcode"])
    page.wait_for_timeout(2000)
    assert _sku_row_count(page, item["sku"]) == 2, \
        "an inbound bill must allow the same item to be scanned twice"


def _scanned_skus(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#line-body tr.data-row--scanned'))"""
        """.map(r => (r.querySelector('[data-name=\"sku\"]') || {}).value)""")


def _scanned_border(page) -> list[str]:
    """The computed outline of each highlighted row, as "<width> <style>"."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#line-body tr.data-row--scanned')).map(r => {"""
        """ const c = getComputedStyle(r); return c.outlineWidth + ' ' + c.outlineStyle; })""")


def test_scan_highlights_the_lines_the_last_run_touched(page, ui_server, api):
    """Two scans of one splittable item are one row of quantity 2, highlighted; a scan of another
    item moves the highlight to that row only, and the rows keep their order. The highlighted row
    carries a 2px green border on screen and none in print, and no background tint: its cells are
    coloured exactly like the plain row's.

    RED at the base: every scan of a splittable lot is its own new line and no row carries the
    scanned highlight."""
    tag = uuid.uuid4().hex[:6]
    first = _seed_item(api, tag + "a", barcode=str(uuid.uuid4().int)[:12])
    second = _seed_item(api, tag + "b", barcode=str(uuid.uuid4().int)[:12])

    _open_doc(page, ui_server, api, "invoice")
    _scan(page, first["barcode"])
    _wait_for_sku_row(page, first["sku"])
    _scan(page, first["barcode"])
    page.wait_for_function(
        """(sku) => Array.from(document.querySelectorAll('#line-body tr')).some(r =>"""
        """ (r.querySelector('[data-name=\"sku\"]') || {}).value === sku"""
        """ && Number(r.querySelector('[data-name=\"quantity\"]').value) === 2)""",
        arg=first["sku"], timeout=8000)
    assert _sku_row_count(page, first["sku"]) == 1
    assert _scanned_skus(page) == [first["sku"]]
    assert _scanned_border(page) == ["2px solid"]
    page.emulate_media(media="print")
    assert [b.split()[1] for b in _scanned_border(page)] == ["none"]
    page.emulate_media(media="screen")

    _scan(page, second["barcode"])
    _wait_for_sku_row(page, second["sku"])
    page.wait_for_function("(sku) => document.querySelector('#line-body tr.data-row--scanned [data-name=\"sku\"]')"
                           "?.value === sku", arg=second["sku"], timeout=8000)
    assert _scanned_skus(page) == [second["sku"]]
    order = page.evaluate("""() => Array.from(document.querySelectorAll('#line-body [data-name=\"sku\"]'))"""
                          """.map(e => e.value).filter(Boolean)""")
    assert order == [first["sku"], second["sku"]]
    plain, scanned = page.evaluate(
        """() => Array.from(document.querySelectorAll('#line-body tr')).filter(r => r.querySelector('[data-name=\"sku\"]')?.value)"""
        """.map(r => Array.from(r.cells).map(c => getComputedStyle(c).backgroundColor))""")
    assert scanned == plain

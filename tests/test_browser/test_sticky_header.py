# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Playwright: the frozen (pinned) table header on excel-style data-table list pages.

The header row is inline until the top of the table reaches the top of the viewport, then
freezes at the top while the body scrolls underneath, and leaves with the table once the body
scrolls off screen. While frozen it stays fully functional (select-all, sort, column filter,
column reorder), tracks horizontal scroll, survives an htmx re-render, realigns on a resize,
and unpins for print. Exercised on the inventory list, which renders the shared data_table.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


# ── seed: a table taller than the viewport, across two named locations for the funnel ──────
@pytest.fixture(scope="module")
def sticky_inventory(api):
    """Thirty inventory rows split across two distinctly named locations. Names/SKUs are unique
    to this file so accumulated rows from other browser tests never collide with the funnel."""
    alpha = api.post("/companies/me/locations", json={"name": "Sticky Loc Alpha", "type": "warehouse"}).json()["id"]
    beta = api.post("/companies/me/locations", json={"name": "Sticky Loc Beta", "type": "warehouse"}).json()["id"]
    for i in range(15):
        api.post("/items", json={"sku": f"PINHDR-A-{i:03d}", "name": f"Pinned alpha {i}",
                                 "quantity": i + 1, "sell_by": "piece", "location_id": alpha})
        api.post("/items", json={"sku": f"PINHDR-B-{i:03d}", "name": f"Pinned beta {i}",
                                 "quantity": i + 1, "sell_by": "piece", "location_id": beta})
    return {"alpha": alpha, "beta": beta}


# ── helpers (viewport-relative geometry; controller-agnostic scroll) ───────────────────────
def _goto(page, ui_server):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{ui_server}/inventory", wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key]", timeout=10000)


# The controller marks the opted-in table with .hdr-pinned; the CSS rule
# .data-table.sticky-head.hdr-pinned > thead then fixes the header. The class lives
# on the table, so every pinned/unpinned check reads the table, not the thead.
_PINNED_JS = (
    "() => { var t=document.querySelector('#data-table');"
    " return !!(t && t.classList.contains('hdr-pinned')); }"
)
_UNPINNED_JS = (
    "() => { var t=document.querySelector('#data-table');"
    " return !!(t && !t.classList.contains('hdr-pinned')); }"
)


def _pinned(page) -> bool:
    return page.evaluate(_PINNED_JS)


def _thead_top(page):
    return page.evaluate(
        "() => { var th=document.querySelector('#data-table thead');"
        " return th ? th.getBoundingClientRect().top : null; }"
    )


def _phantom_h(page):
    # height of the phantom top-scrollbar element (16px whenever it exists), 0 if absent.
    return page.evaluate(
        "() => { var p=document.querySelector('#data-table-wrap > .table-top-scroll');"
        " return p ? p.getBoundingClientRect().height : 0; }"
    )


def _phantom_top(page):
    return page.evaluate(
        "() => { var p=document.querySelector('#data-table-wrap > .table-top-scroll');"
        " return p ? p.getBoundingClientRect().top : null; }"
    )


def _phantom_pinned(page) -> bool:
    return page.evaluate(
        "() => { var p=document.querySelector('#data-table-wrap > .table-top-scroll');"
        " return !!(p && p.classList.contains('top-scroll-pinned')); }"
    )


def _top_scroll_spacers(page) -> int:
    return page.evaluate(
        "() => document.querySelectorAll('#data-table-wrap > .top-scroll-spacer').length"
    )


def _data_keys(page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#data-table thead th[data-key]'))"
        ".map(function(th){return th.dataset.key;})"
    )


def _col_lefts(page, key):
    """[header th left, first body-cell left] for a column, viewport-relative, or None."""
    return page.evaluate(
        "(k) => { var th=document.querySelector('#data-table thead th[data-key=\"'+k+'\"]');"
        " var td=document.querySelector('#data-table tbody tr.data-row td[data-col=\"'+k+'\"]');"
        " if(!th||!td) return null;"
        " return [th.getBoundingClientRect().left, td.getBoundingClientRect().left]; }",
        key,
    )


def _col_values(page, key):
    return page.evaluate(
        "(k) => Array.from(document.querySelectorAll('#data-table tbody tr.data-row td[data-col=\"'+k+'\"]'))"
        ".map(function(td){return td.textContent.trim();})",
        key,
    )


def _scroll_delta(page, delta):
    """Scroll by `delta` px on whichever vertical scroller actually moves the table (window or
    the nearest scrollable ancestor). Returns the table's new viewport-relative top."""
    return page.evaluate(
        """(delta) => {
          var t=document.querySelector('#data-table');
          var y=window.scrollY; window.scrollBy(0, delta);
          if (window.scrollY === y) {
            var n=t.parentElement;
            while(n){
              var s=getComputedStyle(n);
              if(/(auto|scroll)/.test(s.overflowY) || /(auto|scroll)/.test(s.overflowX)){
                var b=n.scrollTop; n.scrollTop += delta; if(n.scrollTop !== b) break;
              }
              n=n.parentElement;
            }
          }
          return t.getBoundingClientRect().top;
        }""",
        delta,
    )


def _scroll_to_top(page):
    page.evaluate(
        """() => {
          window.scrollTo(0,0);
          var t=document.querySelector('#data-table'), n=t?t.parentElement:null;
          while(n){ try{ n.scrollTop=0; }catch(e){} n=n.parentElement; }
        }"""
    )


def _scroll_to_pin(page, margin=40):
    """Scroll until the table top passes `margin` px above the viewport top and the header pins."""
    for _ in range(30):
        top = _thead_top(page)
        cur = page.evaluate("() => { var t=document.querySelector('#data-table'); return t? t.getBoundingClientRect().top : null; }")
        if cur is None or cur <= -margin + 1:
            break
        _scroll_delta(page, cur + margin)
    page.wait_for_function(_PINNED_JS, timeout=6000)


def _add_scroll_room(page):
    page.evaluate(
        "() => { var host=document.querySelector('.main-content')||document.body;"
        " var d=document.createElement('div'); d.style.height='2000px'; d.setAttribute('data-scrollroom','1');"
        " host.appendChild(d); }"
    )


def _wait_swap(page, click_target):
    """Mark the current #data-table, click something that triggers an htmx outerHTML swap, and
    wait until the table node is replaced, then re-pinned."""
    page.evaluate("() => { var t=document.querySelector('#data-table'); if(t) t.setAttribute('data-swaptmp','1'); }")
    click_target.click()
    page.wait_for_function(
        "() => { var t=document.querySelector('#data-table'); return !!t && !t.hasAttribute('data-swaptmp'); }",
        timeout=8000,
    )
    page.wait_for_function(_PINNED_JS, timeout=6000)


def _drag_resize_handle(page, th_sel, dx):
    """Press-drag-release the resize handle inside th_sel by dx px, via real mouse events (not
    a direct style write) so the resizer's own mousedown/onMove/onUp - including its onUp ->
    saveWidths() persistence - actually runs, the same as a user dragging a column wider."""
    handle = page.locator(f"{th_sel} .col-resize-handle").first
    handle.scroll_into_view_if_needed()
    handle.hover()
    box = handle.bounding_box()
    assert box is not None, f"resize handle has no bounding box for {th_sel}"
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + dx, box["y"] + box["height"] / 2, steps=12)
    page.mouse.up()


# ── behaviors (a) + (b): inline before the top, frozen once the table top reaches it ───────
def test_header_freezes_at_viewport_top(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    # (a) at the top: inline, not frozen.
    assert not _pinned(page)
    assert page.evaluate("() => getComputedStyle(document.querySelector('#data-table thead')).position") == "static"
    # (a) after a short scroll that leaves the table top still below the viewport top: still inline.
    cur = page.evaluate("() => document.querySelector('#data-table').getBoundingClientRect().top")
    if cur > 160:
        _scroll_delta(page, cur - 80)
        still = page.evaluate("() => document.querySelector('#data-table').getBoundingClientRect().top")
        if still > 1:
            assert not _pinned(page), "header pinned before the table reached the viewport top"
    # (b) scroll the table top above the viewport top: frozen at top, columns aligned.
    _scroll_to_pin(page)
    assert _pinned(page)
    # this fixture has a phantom top-scrollbar, so the header freezes directly below it.
    assert abs(_thead_top(page) - _phantom_h(page)) <= 2
    key = _data_keys(page)[0]
    lefts = _col_lefts(page, key)
    assert lefts is not None and abs(lefts[0] - lefts[1]) <= 1.5


# ── behavior (c): the frozen header leaves with the table once the body scrolls off ────────
def test_header_unpins_past_table_end(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _add_scroll_room(page)
    _scroll_to_pin(page)
    assert _pinned(page) and abs(_thead_top(page) - _phantom_h(page)) <= 2
    # scroll until the table BOTTOM is above the viewport top.
    for _ in range(40):
        bottom = page.evaluate("() => document.querySelector('#data-table').getBoundingClientRect().bottom")
        if bottom <= -5:
            break
        _scroll_delta(page, bottom + 40)
    page.wait_for_function(_UNPINNED_JS, timeout=6000)
    assert not _pinned(page)
    assert _thead_top(page) < 0  # the header left with the table, no longer stuck at 0


# ── the frozen header stays functional: select-all, sort, column filter ────────────────────
def test_pinned_header_stays_interactive(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page)

    # (i) select-all from the pinned header checks every row checkbox.
    page.locator("#data-table thead #select-all-rows").check()
    page.wait_for_function(
        "() => { var b=Array.from(document.querySelectorAll('#data-table tbody tr.data-row input.row-select'));"
        " return b.length>0 && b.every(function(x){return x.checked;}); }",
        timeout=4000,
    )

    # (ii) sort from the pinned header reorders rows; the re-rendered header re-pins.
    key = _data_keys(page)[0]
    before = _col_values(page, key)
    _wait_swap(page, page.locator(f'#data-table thead th[data-key="{key}"] a.sort-link').first)
    after = _col_values(page, key)
    if after == before:  # already in this direction; toggle to the other
        _wait_swap(page, page.locator(f'#data-table thead th[data-key="{key}"] a.sort-link').first)
        after = _col_values(page, key)
    assert after != before, "sort from the pinned header did not reorder the rows"
    assert _pinned(page) and abs(_thead_top(page) - _phantom_h(page)) <= 2

    # (iii) column filter from the pinned header applies server-side (fresh load resets the URL).
    _goto(page, ui_server)
    _scroll_to_pin(page)
    page.locator("#data-table thead .colfilter[data-param='location_id']").first.click()
    page.wait_for_selector(".colfilter-pop", timeout=4000)
    page.locator(".colfilter-pop .colfilter-item", has_text="Sticky Loc Beta").locator("input").uncheck()
    page.locator(".colfilter-pop .colfilter-foot button:has-text('Apply')").click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#data-table", timeout=10000)
    assert "location_id=" in page.url
    body = page.locator("#data-table").inner_text()
    assert "PINHDR-A-000" in body and "PINHDR-B-000" not in body


# ── column reorder still works while frozen, and the header stays aligned after it ─────────
def test_pinned_header_column_reorder_works(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page)
    keys_before = _data_keys(page)
    # drag the 2nd data column ahead of the 1st (HTML5 drag; the handler reads a module-level key).
    page.evaluate(
        """([s,t]) => {
          var thead=document.querySelector('#data-table thead tr');
          var src=thead.querySelector('th[data-key=\"'+s+'\"]');
          var tgt=thead.querySelector('th[data-key=\"'+t+'\"]');
          var dt=new DataTransfer();
          function fire(el,type){ el.dispatchEvent(new DragEvent(type,{bubbles:true,cancelable:true,dataTransfer:dt})); }
          fire(src,'dragstart'); fire(tgt,'dragover'); fire(tgt,'drop'); fire(src,'dragend');
        }""",
        [keys_before[1], keys_before[0]],
    )
    keys_after = _data_keys(page)
    assert keys_after != keys_before, "column order did not change after the drag"
    first = keys_after[0]
    lefts = _col_lefts(page, first)
    assert lefts is not None and abs(lefts[0] - lefts[1]) <= 1.5
    assert _pinned(page)


# ── horizontal lockstep: the frozen header tracks the body sideways ────────────────────────
def test_pinned_header_tracks_horizontal_scroll(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    # widen the first column so the table overflows its wrap horizontally.
    page.evaluate(
        "() => { var th=document.querySelector('#data-table thead th[data-key]'); if(th) th.style.width='900px'; }"
    )
    _scroll_to_pin(page)
    page.evaluate(
        "() => { var w=document.querySelector('.table-scroll-wrap'); w.scrollLeft=300; w.dispatchEvent(new Event('scroll')); }"
    )
    page.wait_for_timeout(200)
    first = _data_keys(page)[0]
    lefts = _col_lefts(page, first)
    assert lefts is not None and abs(lefts[0] - lefts[1]) <= 2


# ── the frozen header stays clipped inside the table, never painting over the sidebar (#274) ──
def test_pinned_header_never_overlaps_sidebar_when_scrolled_right(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    # widen the first column so the table overflows its wrap and can be scrolled far right.
    page.evaluate(
        "() => { var th=document.querySelector('#data-table thead th[data-key]'); if(th) th.style.width='1200px'; }"
    )
    _scroll_to_pin(page)
    assert _pinned(page)
    # scroll the wrap to its far right so the table's left edge slides in under the sidebar.
    page.evaluate(
        "() => { var w=document.querySelector('.table-scroll-wrap');"
        " w.scrollLeft=w.scrollWidth; w.dispatchEvent(new Event('scroll')); }"
    )
    page.wait_for_timeout(200)
    probe = page.evaluate(
        """() => {
          var wrap=document.querySelector('.table-scroll-wrap');
          var thead=document.querySelector('#data-table thead');
          var wr=wrap.getBoundingClientRect(), tr=thead.getBoundingClientRect();
          var x=wr.left-5, y=tr.top+Math.min(tr.height,20)/2;
          var el=document.elementFromPoint(x, y);
          return {headerLeft: tr.left, wrapLeft: wr.left,
                  hitThead: !!(el && el.closest('#data-table thead'))};
        }"""
    )
    # precondition: the header box really does extend left of the probe point (in under the
    # sidebar), so the probe is a meaningful test of clipping and not a no-op.
    assert probe["headerLeft"] < probe["wrapLeft"] - 5, "setup: header not scrolled left of the wrap"
    # the frozen header must be clipped to the wrap; nothing of it may paint over the sidebar band.
    assert not probe["hitThead"], "frozen header paints over the sidebar when scrolled right"


# ── scrolling back to the top restores the inline header (no pin, no spacer, no inline style) ─
def test_scroll_back_up_restores_inline_header(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page)
    _scroll_to_top(page)
    page.wait_for_function(_UNPINNED_JS, timeout=6000)
    assert not _pinned(page)
    assert page.evaluate("() => getComputedStyle(document.querySelector('#data-table thead')).position") == "static"
    assert page.evaluate("() => document.querySelectorAll('#data-table .hdr-spacer').length") == 0


# ── the controller re-scans after an htmx swap; no stale pinned node is left behind ─────────
def test_pinned_header_survives_htmx_swap(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page)
    key = _data_keys(page)[0]
    _wait_swap(page, page.locator(f'#data-table thead th[data-key="{key}"] a.sort-link').first)
    assert _pinned(page) and abs(_thead_top(page) - _phantom_h(page)) <= 2
    counts = page.evaluate(
        "() => ({pinned: document.querySelectorAll('.hdr-pinned').length,"
        " spacer: document.querySelectorAll('.hdr-spacer').length})"
    )
    assert counts["pinned"] == 1, "exactly one header should be pinned after the swap"
    assert counts["spacer"] <= 1, "no orphaned spacer row from the old table"


# ── a column resized while pinned recomputes geometry so the header stays aligned ──────────
def test_pinned_header_resize_realigns(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    keys = _data_keys(page)
    page.evaluate(
        "(k) => { var th=document.querySelector('#data-table thead th[data-key=\"'+k+'\"]');"
        " th.style.width=(th.getBoundingClientRect().width+180)+'px'; }",
        keys[0],
    )
    page.wait_for_timeout(250)
    lefts = _col_lefts(page, keys[1])
    assert lefts is not None and abs(lefts[0] - lefts[1]) <= 2
    dims = page.evaluate(
        "() => [document.querySelector('#data-table thead').getBoundingClientRect().width,"
        " document.querySelector('#data-table').getBoundingClientRect().width]"
    )
    assert abs(dims[0] - dims[1]) <= 3, "pinned header width should track the body width"


# ── beforeprint actively clears the pin so no overlay/spacer/pixel-widths reach print ──────
def test_beforeprint_unpins_pinned_header(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page)
    assert page.evaluate("() => document.querySelectorAll('#data-table .hdr-spacer').length") >= 1
    page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
    page.wait_for_timeout(150)
    assert not _pinned(page)
    assert page.evaluate("() => document.querySelectorAll('#data-table .hdr-spacer').length") == 0
    inline = page.evaluate(
        "() => { var th=document.querySelector('#data-table thead');"
        " return {position: th.style.position, left: th.style.left, width: th.style.width}; }"
    )
    assert inline["position"] == "" and inline["left"] == "" and inline["width"] == ""




# ── the phantom top-scrollbar freezes in the top band, stacked directly above the header ────
def test_phantom_scrollbar_freezes_above_header(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    # this fixture renders > 10 rows, so the phantom top-scrollbar exists.
    assert _phantom_h(page) > 0, "fixture table should render the phantom scrollbar"
    _scroll_to_pin(page)
    assert _pinned(page)
    # phantom frozen in the top band...
    ptop = _phantom_top(page)
    assert ptop is not None and abs(ptop) <= 2, f"phantom not frozen at the top band: {ptop}"
    assert _phantom_pinned(page)
    # ...and the header sits directly beneath it, offset by exactly the phantom height.
    assert abs(_thead_top(page) - _phantom_h(page)) <= 2


# ── while frozen, the phantom stays on-screen and its scrollLeft tracks the body ───────────
def test_phantom_scrollbar_tracks_horizontal_scroll(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page) and _phantom_pinned(page)
    page.evaluate(
        "() => { var w=document.querySelector('#data-table-wrap .table-scroll-wrap');"
        " w.scrollLeft = 120; w.dispatchEvent(new Event('scroll')); }"
    )
    page.wait_for_timeout(100)
    ptop = _phantom_top(page)
    assert ptop is not None and abs(ptop) <= 2, "frozen phantom left the top band on horizontal scroll"
    synced = page.evaluate(
        "() => { var w=document.querySelector('#data-table-wrap .table-scroll-wrap');"
        " var p=document.querySelector('#data-table-wrap > .table-top-scroll');"
        " return Math.abs(w.scrollLeft - p.scrollLeft); }"
    )
    assert synced <= 2, "phantom scrollLeft did not track the wrap"


# ── unpinning restores the phantom to normal flow: no pin class, no spacer, no inline style ─
def test_phantom_scrollbar_restores_on_unpin(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page) and _phantom_pinned(page)
    assert _top_scroll_spacers(page) >= 1, "a spacer should hold the phantom's place while frozen"
    _scroll_to_top(page)
    page.wait_for_function(_UNPINNED_JS, timeout=6000)
    assert not _phantom_pinned(page)
    assert _top_scroll_spacers(page) == 0
    style = page.evaluate(
        "() => { var p=document.querySelector('#data-table-wrap > .table-top-scroll');"
        " return {left: p.style.left, width: p.style.width}; }"
    )
    assert style["left"] == "" and style["width"] == ""


# ── after an htmx re-render while pinned, exactly one phantom is frozen and no spacer piles up ─
def test_phantom_survives_htmx_swap(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page) and _phantom_pinned(page)
    key = _data_keys(page)[0]
    _wait_swap(page, page.locator(f'#data-table thead th[data-key="{key}"] a.sort-link').first)
    assert _pinned(page) and _phantom_pinned(page)
    counts = page.evaluate(
        "() => ({pinned: document.querySelectorAll('.table-top-scroll.top-scroll-pinned').length,"
        " spacer: document.querySelectorAll('#data-table-wrap > .top-scroll-spacer').length})"
    )
    assert counts["pinned"] == 1, "exactly one phantom should be frozen after the swap"
    assert counts["spacer"] <= 1, "no orphaned top-scroll spacer from the old table"


# ── beforeprint clears the frozen phantom and its spacer, same as the header ───────────────
def test_phantom_scrollbar_cleared_on_print(page, ui_server, sticky_inventory):
    _goto(page, ui_server)
    _scroll_to_pin(page)
    assert _pinned(page) and _phantom_pinned(page)
    assert _top_scroll_spacers(page) >= 1
    page.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
    page.wait_for_timeout(150)
    assert not _pinned(page) and not _phantom_pinned(page)
    assert _top_scroll_spacers(page) == 0


# ── a saved custom column layout must reach the pinned header's LOCKED geometry, not just ──
# ── the live thead: the header pin captures widths once (colgroup + cell styles) and a ─────
# ── later width restore can land after that capture without ever reaching the colgroup ─────
def test_pinned_header_aligns_with_body_under_custom_layout(page, ui_server, sticky_inventory):
    """With a saved custom column-width layout, the pinned header must line up with the body
    columns. The load-time width restore is deferred (requestAnimationFrame) and can commit
    after the pin has already captured and locked geometry into the colgroup - a real race that
    depends on unobservable browser scheduling. This intercepts requestAnimationFrame so the
    restore is queued but never auto-fires, scrolls to pin (capturing whatever geometry is live
    at that moment, before any restore has run), then flushes the queued restore and reads
    alignment in the same synchronous script - so no later async pass can paper over the exact
    state right after the restore lands post-pin."""
    # add_init_script runs a raw script at document-start; it must be a self-invoking
    # expression, not a bare arrow, or the body never executes and the override is a no-op.
    page.add_init_script(
        "(() => { window.__pendingRaf = [];"
        " window.requestAnimationFrame = function(cb) { window.__pendingRaf.push(cb); return window.__pendingRaf.length; };"
        " window.__flushRaf = function() { var cbs = window.__pendingRaf.splice(0); cbs.forEach(function(cb){ cb(0); }); }; })()"
    )
    _goto(page, ui_server)
    keys = _data_keys(page)
    custom = {keys[0]: "300px", keys[1]: "80px"}
    page.evaluate(
        "(a) => localStorage.setItem(a[0], JSON.stringify(a[1]))",
        ["celerp_col_widths_inventory", custom],
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key]", timeout=10000)
    # scroll to pin BEFORE the deferred width restore has run at all (it is only queued,
    # never auto-firing, under the requestAnimationFrame override above) - pin() captures
    # whatever geometry is live right now, which is the server-rendered default.
    _scroll_to_pin(page)
    assert _pinned(page)
    # now let the deferred restore land, after pin() already captured geometry - exactly the
    # race in the bug report - and read alignment in the SAME synchronous call so no later
    # MutationObserver-driven re-evaluate can run between the write and the read.
    result = page.evaluate(
        "(k) => { if (window.__flushRaf) window.__flushRaf();"
        " var th=document.querySelector('#data-table thead th[data-key=\"'+k+'\"]');"
        " var td=document.querySelector('#data-table tbody tr.data-row td[data-col=\"'+k+'\"]');"
        " return th && td ? [th.getBoundingClientRect().left, td.getBoundingClientRect().left] : null; }",
        keys[1],
    )
    assert result is not None, "missing header/body cell for the alignment check"
    assert abs(result[0] - result[1]) <= 1.5, (
        f"pinned header column '{keys[1]}' misaligned with its body column once the deferred "
        f"custom-width restore landed after pin: header left {result[0]:.1f}, body left {result[1]:.1f}"
    )


# ── dragging a resize handle while the header is pinned must keep the dragged width, not ───
# ── the pre-drag default the pin/unpin cycle recaptures when the drag's own style write ────
# ── is mistaken for an external mutation ────────────────────────────────────────────────────
def test_resize_while_pinned_persists(page, ui_server, sticky_inventory):
    """A column resized WHILE the header is pinned must keep the dragged width (and persist it
    to WIDTH_KEY), not silently revert to the pre-drag width. The resize handle's own onMove
    writes th.style.width, which the sticky controller's MutationObserver cannot distinguish
    from an external change: it clears the pinned header's captured widths and recaptures
    fresh geometry - if that recapture happens while the drag's style write has been cleared,
    the drag is lost even though the header stays visually aligned with the (also-reverted)
    body. This drags the actual handle (mousedown/move/up), the only path that also exercises
    the handle's onUp -> persistence, matching what a user does."""
    _goto(page, ui_server)
    page.evaluate("(k) => localStorage.removeItem(k)", "celerp_col_widths_inventory")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key] .col-resize-handle", timeout=10000)
    _scroll_to_pin(page)
    assert _pinned(page)

    key = _data_keys(page)[0]
    th_sel = f'#data-table thead th[data-key="{key}"]'
    before = page.evaluate("(s) => document.querySelector(s).getBoundingClientRect().width", th_sel)

    _drag_resize_handle(page, th_sel, 220)
    page.wait_for_timeout(250)  # let the pin/unpin churn the drag can trigger settle

    after = page.evaluate("(s) => document.querySelector(s).getBoundingClientRect().width", th_sel)
    assert after > before + 150, (
        f"column '{key}' did not keep the width dragged while pinned: {before:.0f}px -> {after:.0f}px"
    )
    stored = page.evaluate("(k) => JSON.parse(localStorage.getItem(k) || 'null')", "celerp_col_widths_inventory")
    assert stored and key in stored, f"resize while pinned was not persisted to localStorage: {stored}"
    stored_px = float(str(stored[key]).replace("px", ""))
    assert stored_px > before + 150, (
        f"persisted width for '{key}' lost the drag: stored {stored_px:.0f}px, pre-drag {before:.0f}px"
    )


# ── hidden columns: the pinned body must keep every visible column under its header ────────
def test_pinned_columns_align_with_hidden_column_after_sideways_scroll(page, ui_server, sticky_inventory):
    """Hide one column the way the column manager does (prefs in localStorage), scroll the
    table sideways FIRST, then down until the header pins. Every visible column's body cells
    must still sit under that column's header. The pin locks column geometry with a colgroup
    built from the header row's cells; a hidden column's cell generates no box, so a col built
    for it lands on the next visible column and every column after it drifts."""
    _goto(page, ui_server)
    keys = _data_keys(page)
    assert len(keys) >= 4, f"need several columns to observe drift, got {keys}"
    hidden = keys[1]
    page.evaluate(
        "(args) => { var p = {}; args.keys.forEach(function(k){ p[k] = k !== args.hidden; });"
        " localStorage.setItem('celerp_cols_inventory', JSON.stringify(p)); }",
        {"keys": keys, "hidden": hidden},
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#data-table thead th[data-key]", timeout=10000)
    page.wait_for_function(
        "(k) => { var th=document.querySelector('#data-table thead th[data-key=\"'+k+'\"]');"
        " return !!th && th.style.display === 'none'; }",
        arg=hidden, timeout=5000,
    )

    # Widen the first column so the table overflows its wrap, then scroll right BEFORE pinning.
    page.evaluate(
        "() => { var th=document.querySelector('#data-table thead th[data-key]'); if(th) th.style.width='900px'; }"
    )
    page.evaluate(
        "() => { var w=document.querySelector('.table-scroll-wrap'); w.scrollLeft=300; w.dispatchEvent(new Event('scroll')); }"
    )
    _scroll_to_pin(page)
    assert _pinned(page)

    visible = [k for k in keys if k != hidden]
    for key in visible:
        lefts = _col_lefts(page, key)
        if lefts is None:
            continue  # column has no body cell rendered (never true for the seed rows)
        assert abs(lefts[0] - lefts[1]) <= 2, (
            f"column '{key}' drifted from its header while pinned with '{hidden}' hidden: "
            f"header left {lefts[0]:.1f}px, body left {lefts[1]:.1f}px"
        )

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The figures a document page derives from the document's money, redrawn whole after a write.

Each element of the page that shows a figure worked out from the document's money (the totals
panel, the Outstanding beside the party) carries ATTRS and an id. A write that changes the
money answers with ``redraw_trigger()``; the page script (DOC_MONEY_JS) then fetches the
document page, drawn by the same renderer as on load, and puts each such element in place of
its own, so no older figure is left on screen beside a newer one.
"""
from __future__ import annotations

EVENT = "celerpDocMoney"
ATTRS = {"data-doc-money": "true"}


def redraw_trigger() -> dict:
    """The client event, for an HX-Trigger header, that redraws the page's money figures."""
    return {EVENT: True}


DOC_MONEY_JS = f"""
(function(){{
  if (window._celerpDocMoneyBound) return;
  window._celerpDocMoneyBound = true;
  document.addEventListener('{EVENT}', async function(){{
    const page = await fetch(window.location.pathname + window.location.search, {{credentials: 'same-origin'}});
    if (!page.ok) return;
    const fresh = new DOMParser().parseFromString(await page.text(), 'text/html');
    fresh.querySelectorAll('[data-doc-money][id]').forEach(function(el){{
      const shown = document.getElementById(el.id);
      if (shown) shown.outerHTML = el.outerHTML;
    }});
    document.querySelectorAll('[data-doc-money][id]').forEach(function(el){{
      if (!fresh.getElementById(el.id)) el.remove();
    }});
    if (window.htmx) document.querySelectorAll('[data-doc-money][id]').forEach(function(el){{ htmx.process(el); }});
    if (window._celerpMoneyOn) window._celerpStoredMoney = window._celerpMoneyOn(fresh);
  }});
}})();
"""

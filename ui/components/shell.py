# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlparse

from fasthtml.common import *

from celerp.services.update import CARD_REASONS, PIP_BLOCKERS, REASON_CODES, reason_key
from ui.config import COOKIE_NAME, get_role
from ui.i18n import t, get_lang, available_langs, lang_label, try_again_pattern
from ui.components.icons import icon
from ui.components.table import searchable_select

# Cache-bust static assets by hashing app.css content
def _css_version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "static", "app.css")
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "1"

_CSS_VER = _css_version()

# Idle auto-logout: after N minutes with no user interaction, send the browser to /logout. The
# effective minutes are decided per render by effective_idle_logout_minutes() so the timer is on only
# when the installation is exposed as a server (no token-TTL surgery); an active user's interactions
# keep resetting it, and 0 minutes renders a no-op. _idle_logout_js() fills the placeholder below.
_IDLE_LOGOUT_JS = ("""
(function(){
  var IDLE_MS = __IDLE_MIN__ * 60000;
  if (IDLE_MS <= 0) return;
  var t;
  function out(){ window.location.href = '/logout?reason=idle&next=' + encodeURIComponent(location.pathname + location.search); }
  function reset(){ clearTimeout(t); t = setTimeout(out, IDLE_MS); }
  // capture:true so scroll counts as activity: scroll events do not bubble, so a
  // document listener only sees them in the capture phase. Without it, scrolling an
  // inner overflow container (e.g. the reconciliation line panels) never resets the
  // timer and the session logs out mid-work despite the user actively scrolling.
  ['mousemove','mousedown','keydown','touchstart','scroll','wheel'].forEach(function(ev){
    document.addEventListener(ev, reset, {passive:true, capture:true});
  });
  document.addEventListener('htmx:afterRequest', reset);
  reset();
})();
""")


def _idle_logout_js() -> str:
    """Render the idle-logout script with the deployment's effective minutes.

    Interpolated per full shell render (not at import) so a Connect/disconnect or
    headless change is picked up on the next navigation without a process restart.
    """
    from celerp.config import effective_idle_logout_minutes
    return _IDLE_LOGOUT_JS.replace("__IDLE_MIN__", str(effective_idle_logout_minutes()))

# Minimal client-side JS: Esc to cancel edit, row menu toggle, close menus on outside click, searchable combobox
# Escape exits a plain form field, on every page including the sign-in and setup pages,
# which load no other script. Runs after the field's own handlers (this listener is on
# the document), leaves any Escape they consumed alone, defers to an open edit cell's
# own restore, and skips searchable selects, whose own Escape and blur handling
# restores the committed choice.
ESC_BLURS_FIELD = """
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape' || e.defaultPrevented || document.querySelector('.cell--editing')) return;
  var field = e.target;
  if (field && field.matches && field.matches('input, textarea, select') && !field.closest('.combobox-wrap')) {
    field.blur();
  }
});
"""

_CLIENT_JS = """
// A panel with fixed positioning (picker list, row menu, column funnel, tooltip) is placed
// against its control when it opens. A banner loading in above, a scroll or a resize then
// moves the control but not the panel, and a click aimed at an option lands beside it. So
// while the panel is shown, ``place`` runs again on any frame where the control has moved
// or the window changed size. Where the control stood is taken when the panel was placed, so
// a move before the next frame is followed too. Following ends once the panel is hidden or
// either is removed.
function followAnchor(popup, anchor, place) {
  function where() {
    var r = popup._anchor.getBoundingClientRect();
    return [r.top, r.left, r.width, r.height, window.innerWidth, window.innerHeight].join();
  }
  popup._anchor = anchor;
  popup._place = place;
  popup._placedAt = where();
  if (popup._following) return;
  popup._following = true;
  function tick() {
    if (!popup.isConnected || !popup._anchor.isConnected || !popup.getClientRects().length
        || getComputedStyle(popup).visibility === 'hidden') {
      popup._following = false;
      return;
    }
    var key = where();
    if (key !== popup._placedAt) { popup._place(); popup._placedAt = key; }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function _copiedFeedback(btn, restoreLabel) {
  btn.textContent = window.__shellI18n.copied;
  setTimeout(function() { btn.textContent = restoreLabel; }, 2000);
}
// A request to this app's own routes made as htmx makes one: it says so, with the page it came
// from, so an ended session is answered with the sign-in redirect, and that redirect is followed.
// A followed redirect never resolves, so no caller reads it as a saved or refused edit.
function celerpFetch(url, opts) {
  opts = Object.assign({}, opts);
  opts.headers = Object.assign({'HX-Request': 'true', 'HX-Current-URL': location.href}, opts.headers);
  return fetch(url, opts).then(function(r) {
    var to = r.headers.get('HX-Redirect');
    if (!to) return r;
    location.href = to;
    return new Promise(function() {});
  });
}
// Post one line action for the chosen rows (their .li-select checkboxes) as ONE request.
// Each row is named by its line id; a page whose rows do not all carry one names them by
// item instead, never a mix. ``fields`` are extra [name, value] pairs. The request carries
// the operation key the page was rendered with, so the same action sent again after a lost
// answer is recorded once. Resolves true on success, keeping the server's toast for the
// page the caller opens next; otherwise the reason is shown.
async function celerpLineAction(url, rows, fields, key, fallbackMsg) {
  var fd = new FormData();
  celerpLineSelection(rows).forEach(function(kv) { fd.append(kv[0], kv[1]); });
  (fields || []).forEach(function(kv) { fd.append(kv[0], kv[1]); });
  if (key) fd.append('idempotency_key', key);
  var msg = fallbackMsg;
  try {
    var resp = await celerpFetch(url, {method: 'POST', body: fd});
    var t = null;
    try { t = JSON.parse(resp.headers.get('HX-Trigger') || 'null'); } catch (e) {}
    if (resp.status === 204) {
      if (t && t.celerpToast) _celerpKeepToast(t.celerpToast);
      return true;
    }
    if (t && t.celerpToast && t.celerpToast.message) msg = t.celerpToast.message;
  } catch (e) {
  }
  celerpToast(msg, 'error');
  return false;
}
// One line action at a time: the second click of a double click, or a click while another
// action's confirm or request is open, does nothing. ``run`` resolves true when the page is
// reloading, which keeps the guard on until it has.
var _celerpLineActionBusy = false;
async function celerpLineActionOnce(e, run) {
  if ((e && e.detail > 1) || _celerpLineActionBusy) return;
  _celerpLineActionBusy = true;
  var leaving = false;
  try { leaving = await run(); } finally { if (!leaving) _celerpLineActionBusy = false; }
}
// The [name, value] pairs naming the chosen rows: line_id each when every row has one,
// otherwise the item each row binds.
function celerpLineSelection(rows) {
  var byLine = rows.every(function(cb) { return cb.getAttribute('data-line-id'); });
  var out = [];
  rows.forEach(function(cb) {
    var id = byLine ? cb.getAttribute('data-line-id') : cb.value;
    if (id) out.push([byLine ? 'line_id' : 'selected', id]);
  });
  return out;
}
// A count message in the form that reads right for n: forms is {one, many}, each with {n}.
function celerpCount(forms, n) {
  return forms[n === 1 ? 'one' : 'many'].replace('{n}', n);
}
// Try again: a notice whose message tells the user to select Try again gets the button that does
// it. Which messages those are comes from the catalog (try_again_script), never from the label, so a
// sentence that only means "do it again" gets none. The button is a plain link that opens the
// page with GET (a page a form posted opens the start of that form), so nothing is sent twice and
// nothing reloads on its own.
var _CELERP_NOTICE = '.cell-error, .flash, .error-banner, .error-inline, .toast__msg, #global-ui-error, #save-status, #scan-bar-status, .account-panel__notice';
function celerpTryAgainButton() {
  var button = document.createElement('a');
  button.className = 'btn btn--secondary btn--sm try-again';
  button.href = window.__shellI18n.tryAgainHref || location.href;
  button.textContent = window.__shellI18n.tryAgain;
  return button;
}
var _celerpTryAgainRe;
function celerpTryAgain(notice) {
  if (!window.__celerpTryAgainPattern || notice.querySelector(_CELERP_NOTICE + ', .try-again')) return;
  _celerpTryAgainRe = _celerpTryAgainRe || new RegExp(window.__celerpTryAgainPattern);
  if (_celerpTryAgainRe.test(notice.textContent)) notice.append(' ', celerpTryAgainButton());
}
// The page as it loads and every response swapped in (the target too, for a reply of bare text).
function _celerpTryAgainWithin(root) {
  if (!root || !root.querySelectorAll) return;
  if (root.matches(_CELERP_NOTICE)) celerpTryAgain(root);
  root.querySelectorAll(_CELERP_NOTICE).forEach(celerpTryAgain);
}
document.addEventListener('htmx:load', function(e) { _celerpTryAgainWithin(e.detail.elt); });
document.addEventListener('htmx:afterSwap', function(e) { _celerpTryAgainWithin(e.detail.target); });
function celerpToast(message, type, persist, action) {
  var container = document.getElementById('toast-container');
  if (!container) { alert(message); return; }
  var toast = document.createElement('div');
  toast.className = 'toast toast--' + (type || 'error') + (persist ? ' toast--persist' : '');
  var body = document.createElement('span');
  body.className = 'toast__msg';
  body.textContent = message;
  celerpTryAgain(body);
  toast.appendChild(body);
  // Optional action link (e.g. "Reconnect in Settings" pointing at /settings/cloud):
  // keeps a persistent error toast actionable instead of dead-ending the user.
  if (action && action.href && action.label) {
    var link = document.createElement('a');
    link.className = 'toast__action';
    link.href = action.href;
    link.textContent = action.label;
    toast.appendChild(link);
  }
  var close = document.createElement('button');
  close.className = 'toast__close';
  close.textContent = '×';
  close.onclick = function() { _dismissToast(toast); };
  toast.appendChild(close);
  container.appendChild(toast);
  requestAnimationFrame(function() { toast.classList.add('toast--visible'); });
  // Persistent toasts (e.g. an instruction the user must read/copy) stay until the ×
  // is clicked; the rest auto-dismiss after 6s.
  if (!persist) setTimeout(function() { _dismissToast(toast); }, 6000);
}
// Keep a toast for the next page load, which shows it (a redirect or reload follows).
function _celerpKeepToast(toast) {
  try { sessionStorage.setItem('celerp_pending_toast', JSON.stringify(toast)); } catch(ex) {}
}
// A toast reports the last action. The user's next action answers for itself, so the
// toasts that would otherwise linger beside its answer step aside; persistent ones stay.
document.addEventListener('htmx:beforeRequest', function(e) {
  var cause = e.detail.requestConfig && e.detail.requestConfig.triggeringEvent;
  if (!cause || (cause.type !== 'click' && cause.type !== 'submit')) return;
  document.querySelectorAll('#toast-container .toast:not(.toast--persist)').forEach(_dismissToast);
});
function _dismissToast(toast) {
  toast.classList.remove('toast--visible');
  setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 300);
}
document.addEventListener('htmx:afterRequest', function(e) {
  var hdr = e.detail.xhr && e.detail.xhr.getResponseHeader('HX-Trigger');
  if (!hdr) return;
  try {
    var obj = JSON.parse(hdr);
    // A toast sent with a redirect is shown on the page the redirect opens, not on this one.
    if (obj.celerpToast && e.detail.xhr.getResponseHeader('HX-Redirect')) _celerpKeepToast(obj.celerpToast);
    else if (obj.celerpToast) celerpToast(obj.celerpToast.message, obj.celerpToast.type || 'error', obj.celerpToast.persist, obj.celerpToast.action);
    if (obj.celerpRestoreCell) {
      // Close any open editable cell: trigger ESC on focused element, then blur
      var active = document.activeElement;
      if (active && active !== document.body) {
        active.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
        active.blur();
      }
      // Also close any open combobox lists
      document.querySelectorAll('.combobox-list.open').forEach(function(l) { l.classList.remove('open'); });
    }
  } catch(ex) {}
});

document.addEventListener('DOMContentLoaded', function() {
  var pending;
  try { pending = sessionStorage.getItem('celerp_pending_toast'); sessionStorage.removeItem('celerp_pending_toast'); } catch(ex) {}
  if (!pending) return;
  try { var t = JSON.parse(pending); celerpToast(t.message, t.type || 'error', t.persist, t.action); } catch(ex) {}
});

function showGlobalUiError(message) {
  var box = document.getElementById('global-ui-error');
  if (!box) {
    alert(message);
    return;
  }
  box.textContent = message;
  celerpTryAgain(box);
  box.style.display = 'block';
}

/* A click-to-edit cell is kept as it was shown just before its editor replaces it, so
   Escape can put it back in any editor, including those with no restore route of their
   own. The editor that consumed Escape itself (preventDefault) keeps its own restore.
   The editor is removed, so its pending blur save never fires. */
var _cellBeforeEdit = null;
document.addEventListener('htmx:beforeSwap', function(e) {
  var t = e.detail.target;
  _cellBeforeEdit = (t && t.matches && t.matches('td.cell:not(.cell--editing)')) ? t.outerHTML : null;
});
document.addEventListener('htmx:afterSettle', function() {
  if (_cellBeforeEdit === null) return;
  document.querySelectorAll('.cell--editing').forEach(function(c) {
    if (c._restoreHtml === undefined) c._restoreHtml = _cellBeforeEdit;
  });
  _cellBeforeEdit = null;
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var editing = document.querySelector('.cell--editing');
    if (editing) {
      var cell = e.target.closest && e.target.closest('.cell--editing');
      if (!e.defaultPrevented && cell && cell._restoreHtml !== undefined) {
        e.preventDefault();
        var tpl = document.createElement('template');
        tpl.innerHTML = cell._restoreHtml.trim();
        var shown = tpl.content.firstElementChild;
        cell.replaceWith(shown);
        htmx.process(shown);
      }
      return;
    }
    document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) { m.classList.remove('open'); });
    document.querySelectorAll('.combobox-list.open').forEach(function(l) { l.classList.remove('open'); });
    document.querySelectorAll('.popover-panel.open').forEach(function(p) {
      p.classList.remove('open');
      var b = p.parentElement.querySelector('.popover-toggle');
      if (b) b.setAttribute('aria-expanded', 'false');
    });
  }
});
/* Select-based edit cells revert on blur via their own onblur handler (blur_restore_js in
   ui/components/table.py), which restores the cell inline. A previous global fallback here
   reloaded the whole page 300ms after a select blurred if the cell was still in edit mode - but
   under load the change->PATCH->swap commit takes longer than 300ms, so that timer fired
   mid-commit and reloaded the page, dropping the just-made edit (and any other unsaved state).
   The per-cell handler is sufficient, so no global reload is needed. */
// Fixed to the window against its button so a table's overflow cannot clip it: anchored to
// the button's right edge, opening upward when too close to the bottom.
function placeRowMenu(menu, btn) {
  var r = btn.getBoundingClientRect();
  var menuH = 200; // estimated max height
  menu.style.position = 'fixed';
  menu.style.right = (window.innerWidth - r.right) + 'px';
  menu.style.left = 'auto';
  if (window.innerHeight - r.bottom < menuH) {
    menu.style.bottom = (window.innerHeight - r.top) + 'px';
    menu.style.top = 'auto';
  } else {
    menu.style.top = r.bottom + 'px';
    menu.style.bottom = 'auto';
  }
  menu.style.zIndex = '9999';
}
function toggleRowMenu(id) {
  var menu = document.getElementById('menu-' + id);
  if (!menu) return;
  var wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) {
    m.classList.remove('open');
    m.style.cssText = '';
  });
  if (!wasOpen) {
    var btn = menu.previousElementSibling;
    if (btn) placeRowMenu(menu, btn);
    menu.classList.add('open');
    if (btn) followAnchor(menu, btn, function() { placeRowMenu(menu, btn); });
  }
}
document.addEventListener('click', function(e) {
  // Popover toggles (search help "?", top bar Help): open the toggle's panel; a click
  // elsewhere closes any open one.
  var toggle = e.target.closest('.popover-toggle');
  if (toggle) {
    var panel = toggle.parentElement.querySelector('.popover-panel');
    if (panel) {
      var isOpen = panel.classList.toggle('open');
      toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    }
  }
  document.querySelectorAll('.popover-panel.open').forEach(function(p) {
    if (p.contains(e.target) || (toggle && p.parentElement === toggle.parentElement)) return;
    p.classList.remove('open');
    var b = p.parentElement.querySelector('.popover-toggle');
    if (b) b.setAttribute('aria-expanded', 'false');
  });
  // data-reveal button: show or mask the secret field it names
  var revealBtn = e.target.closest('[data-reveal]');
  if (revealBtn) {
    var secret = document.querySelector(revealBtn.getAttribute('data-reveal'));
    if (secret) {
      var masked = secret.type === 'password';
      secret.type = masked ? 'text' : 'password';
      revealBtn.textContent = revealBtn.getAttribute(masked ? 'data-hide' : 'data-show');
    }
  }
  // data-copy button: copy sibling/target text to clipboard with "Copied!" feedback
  var copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) {
    var targetSel = copyBtn.getAttribute('data-copy');
    var text = targetSel
      ? (document.querySelector(targetSel) || {}).textContent || ''
      : copyBtn.getAttribute('data-copy-text') || '';
    var original = copyBtn.textContent;
    function _markCopied() {
      _copiedFeedback(copyBtn, original);
    }
    function _fallbackCopy(str) {
      var ta = document.createElement('textarea');
      ta.value = str;
      ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      try { document.execCommand('copy'); } catch(err) {}
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(_markCopied).catch(function() {
        _fallbackCopy(text); _markCopied();
      });
    } else {
      _fallbackCopy(text); _markCopied();
    }
  }

  if (!e.target.closest('.row-menu')) {
    document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) {
      m.classList.remove('open');
      m.style.cssText = '';
    });
  }
  if (!e.target.closest('.combobox-wrap')) {
    document.querySelectorAll('.combobox-list.open').forEach(function(l) { l.classList.remove('open'); });
  }
});

/* Searchable combobox — works with .combobox-wrap > .combobox-input + .combobox-list */
function initCombobox(wrap) {
  var input = wrap.querySelector('.combobox-input');
  var list = wrap.querySelector('.combobox-list');
  var hidden = wrap.querySelector('input[type=hidden]');
  if (!input || !list) return;
  // Guard: don't double-init
  if (wrap._comboboxInit) return;
  wrap._comboboxInit = true;
  var allowCustom = wrap.dataset.allowCustom === 'true';
  // Server-search mode: HTMX swaps the list innerHTML; we save original for restore.
  var isServerSearch = wrap.dataset.searchUrl === '1';
  var originalListHTML = isServerSearch ? list.innerHTML : null;
  // Multi-select mode: values live in a bag of hidden inputs (one per selection)
  // rather than the single hidden input, because a repeated query key is what the
  // server reads and one input cannot carry two values.
  var isMulti = wrap.dataset.multiple === 'true';
  var bag = isMulti ? wrap.querySelector('.combobox-selected') : null;
  var fieldName = hidden ? hidden.getAttribute('data-name') : '';

  // ARIA wiring: give the list a unique id, point the input's aria-controls at
  // it, and keep aria-expanded/aria-activedescendant in step with the popup. IDs
  // are assigned here, not server-side, because one field name can appear on many
  // rows and each instance needs its own unique id. A MutationObserver mirrors
  // aria-expanded off the list's .open class so every open/close path stays in
  // sync, including the global outside-click closer that never calls this scope.
  var comboUid = (window.__comboUid = (window.__comboUid || 0) + 1);
  var listId = 'combobox-list-' + comboUid;
  list.id = listId;
  input.setAttribute('aria-controls', listId);
  function ensureOptId(opt, i) { if (!opt.id) opt.id = listId + '-opt-' + i; return opt.id; }
  new MutationObserver(function() {
    var open = list.classList.contains('open');
    input.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) input.removeAttribute('aria-activedescendant');
  }).observe(list, {attributes: true, attributeFilter: ['class']});

  // Committed selection for a plain single-select: typing only filters the list,
  // it never commits, so on blur without a pick we restore this rather than leave
  // a stale search query in the box. selectOpt() updates it on every real pick.
  var committedValue = hidden ? hidden.value : '';
  var committedLabel = input.value;

  // position:fixed on the list so overflow:hidden/auto ancestors can't clip it.
  // We set top/left/width inline on open so it tracks the input position.
  list.style.position = 'fixed';
  list.style.zIndex = '9999';

  // The list is position:fixed, so keep it inside the viewport: open above the input
  // when there is more room there, and never let it run past either side.
  function positionList() {
    var r = input.getBoundingClientRect();
    var gap = 8, vw = document.documentElement.clientWidth, vh = window.innerHeight;
    list.style.maxWidth = Math.min(400, vw - 2 * gap) + 'px';
    list.style.minWidth = Math.min(r.width, vw - 2 * gap) + 'px';
    list.style.width = 'auto';
    var below = vh - r.bottom - gap, above = r.top - gap;
    var up = below < Math.min(list.scrollHeight, 220) && above > below;
    list.style.maxHeight = Math.min(220, Math.max(up ? above : below, 0) - 2) + 'px';
    list.style.top = up ? 'auto' : (r.bottom + 2) + 'px';
    list.style.bottom = up ? (vh - r.top + 2) + 'px' : 'auto';
    list.style.left = Math.max(gap, Math.min(r.left, vw - gap - list.offsetWidth)) + 'px';
  }

  function showList() {
    positionList();
    list.classList.add('open');
    followAnchor(list, input, positionList);
  }

  // Lazy — always queries the live DOM so HTMX-swapped options are included.
  function currentOpts() {
    return Array.from(list.querySelectorAll('.combobox-option'));
  }

  function filterOpts(q) {
    var lower = q.toLowerCase();
    var visible = 0;
    currentOpts().forEach(function(opt) {
      // Pinned options (actions like "+ Add new" / scope toggles) always stay visible.
      if (opt.classList.contains('combobox-option--pinned')) { opt.style.display = ''; return; }
      // Search data-search if present (includes UTC offset aliases), else textContent
      var haystack = (opt.dataset.search || opt.textContent).toLowerCase();
      var match = haystack.includes(lower);
      opt.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    var empty = list.querySelector('.combobox-option--empty');
    if (empty) empty.style.display = visible === 0 ? '' : 'none';
  }
  function moveFocus(dir) {
    var opts = currentOpts().filter(function(o) {
      return o.style.display !== 'none' && !o.classList.contains('combobox-option--empty');
    });
    var cur = opts.findIndex(function(o) { return o.classList.contains('focused'); });
    currentOpts().forEach(function(o) { o.classList.remove('focused'); });
    var next = cur + dir;
    if (next < 0) next = opts.length - 1;
    if (next >= opts.length) next = 0;
    if (opts[next]) {
      opts[next].classList.add('focused');
      input.setAttribute('aria-activedescendant', ensureOptId(opts[next], next));
    }
  }
  function syncMultiLabel() {
    var n = bag.querySelectorAll('input').length;
    if (n === 0) { input.value = ''; input.placeholder = wrap.dataset.emptyLabel || ''; return; }
    if (n === 1) {
      var only = bag.querySelector('input');
      input.value = only.getAttribute('data-label') || only.value;
      return;
    }
    var tmpl = wrap.dataset.countLabel || '{n} selected';
    input.value = tmpl.replace('{n}', n);
  }

  // The bag is the selection; the tick on an option is only a view of it. A server
  // search swaps in options the server marked from the URL, and the restore path puts
  // the options the page loaded with back, so neither knows about anything picked
  // since. Both re-read the bag rather than trusting the markup that arrived.
  function markSelected() {
    if (!isMulti || !bag) return;
    var chosen = Array.from(bag.querySelectorAll('input')).map(function(el) { return el.value; });
    currentOpts().forEach(function(opt) {
      var val = opt.dataset.value !== undefined ? opt.dataset.value : opt.textContent.trim();
      opt.classList.toggle('combobox-option--selected', chosen.indexOf(val) !== -1);
    });
  }

  function toggleOpt(opt, val, label) {
    var existing = bag.querySelector('input[value="' + CSS.escape(val) + '"]');
    if (existing) {
      existing.remove();
      opt.classList.remove('combobox-option--selected');
    } else {
      var el = document.createElement('input');
      el.type = 'hidden';
      el.name = fieldName;
      el.value = val;
      el.setAttribute('data-label', label);
      bag.appendChild(el);
      opt.classList.add('combobox-option--selected');
    }
    syncMultiLabel();
    // Stay open: picking several accounts in a row is the whole point of the mode. The list
    // is not moved here: this runs on mousedown, and moving it would put the mouseup beside
    // the option. followAnchor places it again on the next frame if the field has moved.
  }

  function selectOpt(opt) {
    clearPickNote();
    var val = opt.dataset.value !== undefined ? opt.dataset.value : opt.textContent.trim();
    var label = opt.textContent.trim();
    // __new__:URL values trigger a redirect instead of a form submission
    if (val.indexOf('__new__:') === 0) {
      window.location = val.slice('__new__:'.length);
      return;
    }
    if (isMulti && bag) {
      toggleOpt(opt, val, label);
      return;
    }
    // Show human-readable label in the visible input; store actual value in hidden
    input.value = label;
    if (hidden) hidden.value = val;
    committedValue = val;
    committedLabel = label;
    list.classList.remove('open');
    currentOpts().forEach(function(o) { o.classList.remove('focused'); });
    fireChange();
  }

  function fireChange() {
    // Use htmx.trigger() — synthetic dispatchEvent is ignored by HTMX 2.x
    if (hidden && typeof htmx !== 'undefined') {
      htmx.trigger(hidden, 'change');
    } else if (hidden) {
      hidden.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  // allow-custom: on Enter, or on Tab or blur when no option contains it, typed text that is
  // not the committed label becomes the value (an emptied box clears it on Enter). Typing
  // already mirrors it into the hidden input, so the commit is the change event; text
  // equal to the committed label keeps the committed value.
  function commitCustom() {
    clearPickNote();
    if (input.value === committedLabel) { hidden.value = committedValue; return; }
    hidden.value = input.value;
    committedValue = input.value;
    committedLabel = input.value;
    fireChange();
  }

  // What the typed text names without arrow-key focus. exact: the option whose label,
  // or else whose value, it spells in any case. lone: the only option the query leaves
  // visible, which only Enter on a fixed-list box picks; an allow-custom box never turns
  // a partial match into that option, so "Gems" never becomes "Rough Gemstone". An empty
  // box is no query, so it never picks the lone option. Pinned actions (add-new, scope
  // toggles) stay visible whatever is typed, so they are never picked or counted.
  function typedMatch() {
    var opts = currentOpts().filter(function(o) {
      return !o.classList.contains('combobox-option--empty') && !o.classList.contains('combobox-option--pinned');
    });
    var vis = opts.filter(function(o) { return o.style.display !== 'none'; });
    var typed = input.value.trim().toLowerCase();
    var byLabel = opts.filter(function(o) { return typed && o.textContent.trim().toLowerCase() === typed; });
    var byValue = opts.filter(function(o) {
      return typed && o.dataset.value !== undefined && o.dataset.value.toLowerCase() === typed;
    });
    var exact = byLabel.length === 1 ? byLabel[0] : (!byLabel.length && byValue.length === 1 ? byValue[0] : null);
    return {exact: exact, lone: typed && vis.length === 1 ? vis[0] : null, matches: vis.length};
  }

  // Options still match on Tab or click-away: nothing is saved, the committed
  // value stays behind the typed text, and a note under the box says how to finish.
  function showPickNote() {
    hidden.value = committedValue;
    var note = wrap.querySelector('.combobox-note');
    if (!note) {
      note = document.createElement('div');
      note.className = 'cell-error combobox-note';
      note.setAttribute('role', 'status');
      wrap.appendChild(note);
    }
    // A box whose typed values follow a rule carries its own hint; the shared one offers Enter.
    note.textContent = wrap.dataset.customHint
      || window.__shellI18n.comboPickOrAdd.replace('{value}', input.value.trim());
  }
  function clearPickNote() {
    var note = wrap.querySelector('.combobox-note');
    if (note) note.remove();
  }

  // allow-custom, leaving the box by Tab or click-away: an exact match picks its option,
  // text no option contains is a new value, and text options still contain is an
  // unfinished search the user is told about. Text equal to the committed label
  // changes nothing, so a box Enter, Tab or a pick already committed is never saved twice.
  function settleTyped() {
    if (input.value === committedLabel) { hidden.value = committedValue; return; }
    var m = typedMatch();
    if (m.exact) {
      var val = m.exact.dataset.value !== undefined ? m.exact.dataset.value : m.exact.textContent.trim();
      if (val !== committedValue) selectOpt(m.exact);
      else { input.value = committedLabel; hidden.value = committedValue; }
    } else if (m.matches) showPickNote();
    else commitCustom();
  }

  function openList() {
    // Restore original static options so user sees full list on re-focus after a search
    if (isServerSearch && originalListHTML) {
      list.innerHTML = originalListHTML;
      markSelected();
    }
    // Clear the summary so typing searches rather than filtering against "3 selected".
    if (isMulti) input.value = '';
    filterOpts('');
    showList();
  }
  input.addEventListener('focus', openList);
  // A click on the field after Esc closed it opens it again; focus alone would not.
  input.addEventListener('click', function() { if (!list.classList.contains('open')) openList(); });
  input.addEventListener('input', function() {
    clearPickNote();
    if (isServerSearch) {
      if (!input.value) {
        // User cleared the search, so put the static options back and show them all
        list.innerHTML = originalListHTML;
        markSelected();
        filterOpts('');
      }
      // Non-empty: HTMX fires the server request and swaps results into the list
      showList();
      if (hidden && !isMulti) hidden.value = '';
    } else {
      filterOpts(input.value);
      showList();
      if (hidden && !isMulti) hidden.value = allowCustom ? input.value : '';
    }
  });
  input.addEventListener('blur', function() {
    // Allow mousedown on option to fire first
    setTimeout(function() {
      // Focus came straight back: the list the user just reopened stays open.
      if (document.activeElement === input) return;
      list.classList.remove('open');
      // Typing filters the list in multi mode; the selection bag is the state,
      // so restore the summary rather than leaving the search text behind.
      if (isMulti) { syncMultiLabel(); return; }
      if (allowCustom) { settleTyped(); return; }
      // Plain single-select over a fixed option set: typing only filters, it
      // never commits. Restore the committed label and value so blurring after a
      // search that was not confirmed never leaves a stale query in the box or an
      // emptied hidden value behind the visible text.
      input.value = committedLabel;
      if (hidden) hidden.value = committedValue;
    }, 150);
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') { moveFocus(1); e.preventDefault(); }
    if (e.key === 'ArrowUp') { moveFocus(-1); e.preventDefault(); }
    // Enter commits; on an allow-custom box Tab picks the same way and still moves on.
    var enter = e.key === 'Enter';
    if (enter || (e.key === 'Tab' && allowCustom)) {
      var focused = list.querySelector('.combobox-option.focused');
      if (focused) { selectOpt(focused); if (enter) e.preventDefault(); return; }
      if (!enter) { settleTyped(); return; }
      // Enter: an exact match picks its option, a fixed-list box also picks the lone
      // visible option, and an allow-custom box takes the typed text as a new value.
      var m = typedMatch();
      var pick = m.exact || (!allowCustom && m.lone);
      if (pick) { selectOpt(pick); e.preventDefault(); }
      else if (allowCustom) commitCustom();
    }
    if (e.key === 'Escape') {
      list.classList.remove('open');
      // Escape abandons the search: put the committed selection back now, so a
      // form submitted before blur never carries a half-typed query or an
      // emptied hidden value.
      if (isMulti) syncMultiLabel();
      else {
        input.value = committedLabel;
        if (hidden) hidden.value = committedValue;
      }
      // Do NOT preventDefault — let event bubble to parent for display restore
    }
  });

  // Event delegation for mousedown — handles both static and HTMX-swapped options
  list.addEventListener('mousedown', function(e) {
    var opt = e.target.closest('.combobox-option');
    if (opt && !opt.classList.contains('combobox-option--empty')) {
      e.preventDefault();
      selectOpt(opt);
    }
  });

  // Cancel HTMX search request when input is empty (JS restores static options instead)
  if (isServerSearch) {
    wrap.addEventListener('htmx:configRequest', function(e) {
      if (!input.value.trim()) {
        e.preventDefault();
      }
    });
    wrap.addEventListener('htmx:afterSwap', function(e) {
      if (e.target === list) markSelected();
    });
  }
}
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.combobox-wrap').forEach(initCombobox);
});
/* Re-init after HTMX swaps — search from document to handle outerHTML swaps */
document.addEventListener('htmx:afterSettle', function(e) {
  var root = (e.detail.elt && e.detail.elt.isConnected) ? e.detail.elt : document;
  root.querySelectorAll('.combobox-wrap').forEach(initCombobox);
});

/* Elements marked data-quiet-error surface their failure state themselves
   (e.g. a self-retrying panel) - a toast per failed poll would only add noise. */
function quietError(e) {
  var el = e.detail && e.detail.elt;
  return !!(el && el.closest && el.closest('[data-quiet-error]'));
}

/* A short plain-text or JSON {detail} reply already says what went wrong;
   anything else (an HTML page, a long body) gets the general message. */
function serverErrorText(xhr) {
  if (!xhr) return '';
  var type = xhr.getResponseHeader('Content-Type') || '';
  var body = xhr.responseText || '';
  var text = '';
  if (type.indexOf('text/plain') === 0) {
    text = body;
  } else if (type.indexOf('application/json') === 0) {
    try { var d = JSON.parse(body).detail; if (typeof d === 'string') text = d; } catch (_) {}
  }
  text = text.trim();
  return text.length && text.length <= 300 ? text : '';
}

document.addEventListener('htmx:responseError', function(e) {
  if (quietError(e)) return;
  showGlobalUiError(serverErrorText(e.detail && e.detail.xhr) || window.__shellI18n.requestFailed);
});

document.addEventListener('htmx:sendError', function(e) {
  if (quietError(e)) return;
  showGlobalUiError(window.__shellI18n.networkError);
});

/* ── Image cell drag-and-drop ─────────────────────────────────────────── */
/* Delegated at the document so cells rendered later by htmx swaps need no
   re-initialisation. Click opens the cell's hidden file input; a drop hands the
   dropped file to that same input, so both paths upload through its hx-post. */
function imageDropCell(e) {
  var cell = e.target.closest ? e.target.closest('.cell--droppable') : null;
  return cell;
}
function imageDropInput(cell) {
  return document.getElementById('img-input-' + cell.dataset.entityId.replace(/:/g, '-'));
}
document.addEventListener('click', function(e) {
  var cell = imageDropCell(e);
  if (!cell || e.target.tagName === 'INPUT') return;
  var input = imageDropInput(cell);
  if (input) input.click();
});
['dragover', 'dragenter'].forEach(function(ev) {
  document.addEventListener(ev, function(e) {
    var cell = imageDropCell(e);
    if (!cell) return;
    e.preventDefault();
    cell.classList.add('cell--drag-over');
  });
});
['dragleave', 'drop'].forEach(function(ev) {
  document.addEventListener(ev, function(e) {
    var cell = imageDropCell(e);
    if (!cell) return;
    e.preventDefault();
    cell.classList.remove('cell--drag-over');
    var input = imageDropInput(cell);
    if (ev === 'drop' && input && e.dataTransfer && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
});
// A tab strip wider than the screen scrolls sideways inside itself; bring its current
// tab into view whenever the strip is drawn, without moving the page.
function revealActiveTabs() {
  document.querySelectorAll('.category-tabs').forEach(function(strip) {
    var tab = strip.querySelector('.category-tab--active');
    if (!tab || strip.scrollWidth <= strip.clientWidth) return;
    var left = tab.offsetLeft - strip.offsetLeft;
    if (left < strip.scrollLeft || left + tab.offsetWidth > strip.scrollLeft + strip.clientWidth)
      strip.scrollLeft = left - (strip.clientWidth - tab.offsetWidth) / 2;
  });
}
document.addEventListener('DOMContentLoaded', revealActiveTabs);
document.addEventListener('htmx:afterSettle', revealActiveTabs);
"""


_INFO_TIP_JS = """
// One tooltip for every .info-tip icon (info_tip). It lives under <body> with fixed
// positioning, so no card's overflow can clip it: above the icon when there is room,
// else below, and always pulled inside the viewport edges. It follows its icon wherever
// the page moves it (followAnchor), and closes once the icon leaves the viewport. On touch screens a tap on the
// icon opens or closes it, and a tap anywhere else closes it.
(function() {
  var MARGIN = 8, GAP = 6, bubble = null, owner = null, tapWasOpen = null;
  function bubbleEl() {
    // A page swap can remove the bubble from the document; build a fresh one when it has.
    if (!bubble || !bubble.isConnected) {
      bubble = document.createElement('div');
      bubble.id = 'info-tip-bubble';
      bubble.className = 'info-tip-bubble';
      bubble.setAttribute('role', 'tooltip');
      document.body.appendChild(bubble);
    }
    return bubble;
  }
  function hide() {
    if (!owner) return;
    owner.removeAttribute('aria-describedby');
    owner = null;
    if (bubble) bubble.classList.remove('info-tip-bubble--open');
  }
  function place() {
    var r = owner.getBoundingClientRect(), b = bubble.getBoundingClientRect();
    var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    var top = r.top - GAP - b.height;
    if (top < MARGIN) top = Math.min(r.bottom + GAP, vh - MARGIN - b.height);
    var left = Math.min(r.left + r.width / 2 - b.width / 2, vw - MARGIN - b.width);
    bubble.style.top = Math.max(MARGIN, top) + 'px';
    bubble.style.left = Math.max(MARGIN, left) + 'px';
  }
  function show(tip) {
    var text = tip.getAttribute('data-tip');
    if (!text) return;
    if (owner && owner !== tip) owner.removeAttribute('aria-describedby');
    var el = bubbleEl();
    owner = tip;
    el.textContent = text;
    el.classList.add('info-tip-bubble--open');
    tip.setAttribute('aria-describedby', el.id);
    place();
    followAnchor(el, tip, follow);
  }
  function follow() {
    if (!owner) return;
    var r = owner.getBoundingClientRect();
    var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    if (!owner.isConnected || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) hide();
    else place();
  }
  function tipOf(e) { return e.target && e.target.closest ? e.target.closest('.info-tip') : null; }
  document.addEventListener('mouseover', function(e) { var t = tipOf(e); if (t && t !== owner) show(t); });
  document.addEventListener('mouseout', function(e) {
    var t = tipOf(e);
    if (t && t === owner && !t.contains(e.relatedTarget) && document.activeElement !== t) hide();
  });
  document.addEventListener('focusin', function(e) { var t = tipOf(e); if (t) show(t); });
  document.addEventListener('focusout', function(e) { if (tipOf(e) === owner) hide(); });
  document.addEventListener('keydown', function(e) { if (e.key === 'Escape') hide(); });
  document.addEventListener('pointerdown', function(e) {
    var t = tipOf(e);
    tapWasOpen = (t && e.pointerType === 'touch') ? (t === owner) : null;
    if (!t) hide();
  }, true);
  document.addEventListener('click', function(e) {
    var t = tipOf(e);
    if (!t) return;
    // An icon inside a <label> must not also toggle the label's checkbox.
    e.preventDefault();
    if (tapWasOpen === null) return;
    if (tapWasOpen) hide(); else show(t);
    tapWasOpen = null;
  });
  // A page swap that removes the icon ends following, so close the tip here.
  document.addEventListener('htmx:afterSettle', follow);
})();
"""


def info_tip(text: str, glyph: str = "ⓘ") -> FT:
    """An info icon whose explanation opens on hover or keyboard focus (``_INFO_TIP_JS``)."""
    return Span(glyph, cls="info-tip", tabindex="0", role="img", **{"aria-label": text, "data-tip": text})


_HEALTH_BANNER_JS = """
document.addEventListener('DOMContentLoaded', function() {
  fetch('/health/system')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.overall === 'ok') return;
      var banner = document.getElementById('sys-health-banner');
      if (!banner) return;
      var msg = '';
      var worst = data.overall;
      var components = ['ram', 'cpu', 'disk'];
      for (var i = 0; i < components.length; i++) {
        var c = data[components[i]];
        if (c && c.status === worst && c.message) { msg = c.message; break; }
      }
      if (!msg) return;
      banner.querySelector('.sys-health-banner__msg').textContent = msg;
      banner.style.backgroundColor = worst === 'critical' ? '#dc2626' : '#ca8a04';
      banner.style.color = '#fff';
      banner.style.display = 'flex';
    })
    .catch(function() {});
});
"""

_HEALTH_BANNER_HTML = Div(
    Span("", cls="sys-health-banner__msg"),
    Button(
        "x",
        cls="sys-health-banner__dismiss",
        type="button",
        onclick="this.parentElement.style.display='none'",
    ),
    id="sys-health-banner",
    cls="sys-health-banner",
    style="display:none;align-items:center;justify-content:space-between;padding:0.5rem 1rem;font-weight:500;",
)

_POLL_CONTROLLER_JS = """
// One shared single-flight adaptive poller for every raw-JS fetch-poll surface.
// A plain setInterval fires a new fetch every tick even while the previous one is
// still open, so a slow endpoint stacks overlapping requests and can exhaust the
// connection pool. celerpPoll collapses that to one in-flight request: a tick that
// arrives while a request is outstanding is skipped, never started as a second
// fetch. On success it polls at the normal interval; on repeated errors it backs
// off (doubling up to a cap) so a failing endpoint is not hammered. onData is
// called with the parsed body on success and with null on error, letting the caller
// hold its last known state on failure rather than clearing it.
window.celerpPoll = window.celerpPoll || function(name, url, onData, opts) {
  opts = opts || {};
  var interval = opts.interval || 8000;
  var maxErrorSkips = opts.maxErrorSkips || 8;
  // Bound every fetch: a socket that wedges and never settles would otherwise leave
  // inFlight true forever, and the single-flight guard would then skip every future
  // tick, silently killing the poller until a page reload. Abort a fetch that has not
  // settled within the deadline so it flows through the error/backoff branch and the
  // next tick can start a fresh request. Default to just under one interval.
  var fetchDeadline = opts.fetchDeadline || Math.max(interval - 1000, 4000);
  var inFlight = false;
  var errorCount = 0;   // consecutive errors, drives the backoff
  var skipTicks = 0;    // ticks to skip before the next attempt (backoff)

  function fire() {
    inFlight = true;
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var deadline = setTimeout(function() {
      if (controller) { try { controller.abort(); } catch (e) {} }
    }, fetchDeadline);
    fetch(url, { cache: 'no-store', signal: controller ? controller.signal : undefined })
      .then(function(r) {
        // A non-ok response (e.g. the server's 503 when its backend read fails) is an
        // error, not healthy data: reject so it flows through the same error/backoff
        // branch below and the counters are not reset.
        if (!r.ok) { throw new Error('poll response not ok: ' + r.status); }
        return r.json();
      })
      .then(function(d) {
        clearTimeout(deadline);
        inFlight = false;
        errorCount = 0;
        skipTicks = 0;
        try { onData(d); } catch (e) {}
      })
      .catch(function() {
        clearTimeout(deadline);
        inFlight = false;
        try { onData(null); } catch (e) {}
        // Adaptive backoff: after each error wait an extra tick, capped, so a
        // failing endpoint is polled less often rather than every tick.
        errorCount = errorCount + 1;
        skipTicks = Math.min(errorCount, maxErrorSkips);
      });
  }

  function tick() {
    // Single-flight: a tick arriving while a request is still open is skipped,
    // never started as a second overlapping fetch.
    if (inFlight) { return; }
    if (skipTicks > 0) { skipTicks = skipTicks - 1; return; }
    fire();
  }

  return {
    start: function() {
      fire();               // one immediate poll on start
      setInterval(tick, interval);
    }
  };
};
"""

_BACKUP_BANNER_JS = """
(function() {
  document.addEventListener('DOMContentLoaded', function() {
    var poller = window.celerpPoll('backup', '/backup/active', function(d) {
      var b = document.getElementById('backup-progress-banner');
      if (!b) return;
      // On an error/unknown state (or a request failure, d === null) hold the
      // banner's last known state and mark it as stale; never flip to inactive on
      // a failed poll, which would read as a false "no backup running".
      if (!d || d.state === 'error' || d.state === 'unknown') {
        b.setAttribute('data-poll-stale', '1');
        return;
      }
      b.removeAttribute('data-poll-stale');
      b.style.display = d.active ? 'flex' : 'none';
    }, { interval: 8000 });
    poller.start();
  });
})();
"""

def _backup_banner_html(lang: str = "en") -> FT:
    """Built at render time (never a module-level constant): the banner text must
    resolve in the current request's language, not get frozen at import (R1)."""
    return Div(
        Span(t("shell.backup_in_progress", lang)),
        id="backup-progress-banner",
        cls="sys-health-banner",
        style="display:none;align-items:center;justify-content:center;padding:0.5rem 1rem;font-weight:500;background-color:#2563eb;color:#fff;",
    )

# A move into Celerp that is still in progress, on every page, so its owner can always
# get back to it. Fills itself after the page loads; nothing shows when there is none.
_OPEN_MIGRATIONS_LOADER = Div(hx_get="/migrations/open-banner", hx_trigger="load", hx_swap="outerHTML",
                              **{"data-quiet-error": "1"})

_GLOBAL_UI_ERROR_HTML = Div(
    "",
    id="global-ui-error",
    cls="flash flash--error",
    style="display:none;margin:12px 0;",
)

_TOAST_CONTAINER_HTML = Div(id="toast-container", cls="toast-container")

_NOTIFICATION_JS = """
// The bell is an unread inbox: it lists only unread notifications, so a
// dismissed (read) one never reappears on refresh. Escape all server strings
// (recipient emails and doc numbers flow into titles/bodies).
function _notifEsc(s) {
  var d = document.createElement('div');
  d.textContent = (s == null ? '' : String(s));
  return d.innerHTML;
}

function _notifItemHtml(n) {
  var inner = '<div class="notif-item__title">' + _notifEsc(n.title) + '</div>'
            + '<div class="notif-item__body">' + _notifEsc(n.body) + '</div>';
  // A referenced document becomes a link; otherwise plain content.
  var content = n.action_url
    ? '<a class="notif-item__link" href="' + _notifEsc(n.action_url) + '">' + inner + '</a>'
    : '<div class="notif-item__link">' + inner + '</div>';
  return '<div class="notif-item' + (n.read ? '' : ' notif-item--unread')
    + (n.priority === 'high' ? ' notif-item--high' : '') + '" data-id="' + _notifEsc(n.id) + '">'
    + content
    + '<button class="notif-item__dismiss" type="button" title="' + window.__shellI18n.markAsRead + '" aria-label="' + window.__shellI18n.markAsRead + '">&times;</button>'
    + '</div>';
}

// A found app update is worth one bell count so the user notices it on the
// icon without opening the panel; the panel's update card carries the version
// and the restart/upgrade action, so the count needs no list item.
window._celerpUpdateLit = window._celerpUpdateLit || false;
window._lastNotifData = window._lastNotifData || { items: [], unread_count: 0 };

function _renderNotifs(data) {
  if (data) window._lastNotifData = data;
  data = window._lastNotifData;
  var badge = document.getElementById('notif-badge');
  var list = document.getElementById('notif-list');
  var count = (data.unread_count || 0) + (window._celerpUpdateLit ? 1 : 0);
  if (badge) {
    if (count > 0) {
      badge.textContent = count > 99 ? '99+' : count;
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
  }
  if (list) {
    if (!data.items || data.items.length === 0) {
      list.innerHTML = '<div class="notif-panel__empty">' + window.__shellI18n.noNotifications + '</div>';
    } else {
      list.innerHTML = data.items.slice(0, 10).map(_notifItemHtml).join('');
    }
  }
}

// Light or clear the bell's update count: lit while an update is found,
// downloading or downloaded (Electron) or available on PyPI (pip). Re-renders
// off the last fetched inbox so no network call is needed.
window.celerpSetUpdateBell = function(lit) {
  window._celerpUpdateLit = !!lit;
  _renderNotifs(null);
};

window._loadNotifs = function() {
  return fetch('/notifications?unread_only=true')
    .then(function(r) { return r.json(); })
    .then(_renderNotifs)
    .catch(function() {});
};

window.toggleNotifPanel = function() {
  var p = document.getElementById('notif-panel');
  if (!p) return;
  var visible = p.style.display !== 'none';
  p.style.display = visible ? 'none' : '';
  if (!visible) window._loadNotifs();
};

// Dismiss one notification (mark read) without navigating; it disappears and
// the badge updates, while notifications the user leaves alone stay put.
window.markNotifRead = function(id) {
  fetch('/notifications/' + encodeURIComponent(id) + '/read', { method: 'POST' })
    .then(function() { window._loadNotifs(); })
    .catch(function() {});
};

window.markAllNotifRead = function() {
  fetch('/notifications/read-all', { method: 'POST' })
    .then(function() { window._loadNotifs(); })
    .catch(function() {});
};

document.addEventListener('DOMContentLoaded', function() {
  var badge = document.getElementById('notif-badge');
  var panel = document.getElementById('notif-panel');
  var list = document.getElementById('notif-list');
  if (!badge || !panel) return;

  var NOTIF_FLOOR_MS = 60000;   // cap opportunistic refreshes to ~1/min
  var lastNotifFetch = 0;

  function loadNotifications() {
    lastNotifFetch = Date.now();
    window._loadNotifs();
  }

  // Per-item dismiss (delegated, so it survives list re-renders).
  if (list) {
    list.addEventListener('click', function(e) {
      var btn = e.target.closest('.notif-item__dismiss');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      var item = btn.closest('.notif-item');
      var id = item && item.getAttribute('data-id');
      if (id) window.markNotifRead(id);
    });
  }

  // Close panel on outside click
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.notif-wrap')) {
      panel.style.display = 'none';
    }
  });

  // SSE for real-time updates (muxed: notifications + session-watch)
  (function initEventStream() {
    if (typeof EventSource === 'undefined') return;
    var es = new EventSource('/events/stream');

    es.addEventListener('notification', function(e) {
      try {
        var data = JSON.parse(e.data);
        if (data.type === 'batch_progress') {
          document.dispatchEvent(new CustomEvent('celerp:batch-progress', { detail: data }));
          return;
        }
        loadNotifications();
        if (data.priority === 'high' && Notification.permission === 'granted') {
          // The listed notice is in the reader's language; the pushed one is as stored.
          fetch('/notifications?limit=20').then(function(r) { return r.json(); }).then(function(d) {
            var n = (d.items || []).find(function(i) { return i.id === data.id; });
            if (n) new Notification(n.title, { body: n.body });
          }).catch(function() {});
        }
      } catch(err) {}
    });

    es.addEventListener('evicted', function(e) {
      es.close();
      try {
        var data = JSON.parse(e.data || '{}');
        var by = data.by ? '&by=' + encodeURIComponent(data.by) : '';
        var nx = '&next=' + encodeURIComponent(location.pathname + location.search);
        window.location.href = '/login?reason=evicted' + by + nx;
      } catch(_) {
        window.location.href = '/login?reason=evicted&next=' + encodeURIComponent(location.pathname + location.search);
      }
    });

    es.addEventListener('drain', function(e) {
      es.close();
      window.location.href = '/login?reason=expired';
    });

    es.onerror = function() {
      // Auto-reconnect is built into EventSource
    };
  })();

  // Initial load
  loadNotifications();

  // Opportunistic badge refresh: in-page htmx actions nudge notifications so the
  // badge doesn't lag while the user stays on one page (the relay can't push)
  // The floor caps this at ~1 fetch/min regardless of activity and we
  // skip hidden tabs, so it adds strictly less load than the per-navigation
  // fetch and never becomes a poll. loadNotifications is a plain fetch, so this
  // listener can't re-trigger itself.
  document.addEventListener('htmx:afterSettle', function() {
    if (document.visibilityState === 'hidden') return;
    if (Date.now() - lastNotifFetch < NOTIF_FLOOR_MS) return;
    loadNotifications();
  });

  // Request browser notification permission
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    document.addEventListener('click', function requestPerm() {
      Notification.requestPermission();
      document.removeEventListener('click', requestPerm);
    }, { once: true });
  }

  /* ── Language switcher ────────────────────────────────────────────── */
  var langHidden = document.querySelector('.lang-switcher-wrap input[type="hidden"][name="lang"]');
  if (langHidden) {
    langHidden.addEventListener('change', function() {
      document.cookie = 'celerp_lang=' + langHidden.value + ';path=/;max-age=' + (86400 * 365) + ';samesite=lax';
      window.location.reload();
    });
  }

  /* ── Update status card ───────────────────────────────────────────── */
  function initUpdateCard() {
    var card = document.getElementById('update-status-card');
    if (!card) return;

    var versionEl = card.querySelector('.update-card__version');
    var stateEl = card.querySelector('.update-card__state');
    var checkBtn = card.querySelector('.update-card__check-btn');
    var restartBtn = card.querySelector('.update-card__restart-btn');
    var progressBar = card.querySelector('.update-card__progress-bar');
    var progressFill = card.querySelector('.update-card__progress-fill');
    var logEl = card.querySelector('.update-card__log');
    var installerLink = card.querySelector('.update-card__installer-link');
    var helpLink = card.querySelector('.update-card__help-link');

    function setState(text, showRestart) {
      if (stateEl) stateEl.textContent = text;
      if (restartBtn) restartBtn.style.display = showRestart ? '' : 'none';
    }

    // A failure offers the installer from the release page the state names. A
    // download that failed its security check names none: it offers help instead.
    function setInstallerLink(url) {
      if (!installerLink) return;
      if (url) installerLink.href = url;
      installerLink.style.display = url ? '' : 'none';
    }

    function setHelpLink(visible) {
      if (helpLink) helpLink.style.display = visible ? '' : 'none';
    }

    function setCheckBtn(visible) {
      if (checkBtn) checkBtn.style.display = visible ? '' : 'none';
    }

    // The log is the updater's own lines, kept in the state (capped there),
    // so a replay shows exactly what the live events showed.
    function setLog(lines) {
      if (!logEl) return;
      logEl.textContent = lines.join('\\n');
      logEl.style.display = lines.length ? '' : 'none';
      logEl.scrollTop = logEl.scrollHeight;
    }

    function setProgress(pct) {
      if (!progressBar || !progressFill) return;
      progressBar.style.display = pct >= 0 ? '' : 'none';
      progressFill.style.width = Math.min(100, Math.max(0, pct)) + '%';
    }

    function resetToIdle() {
      setCheckBtn(true);
      setProgress(-1);
    }

    if (window.celerp) {
      // ── Electron path ────────────────────────────────────────────────
      window.celerp.getVersion().then(function(v) {
        if (versionEl) versionEl.textContent = 'v' + v;
      }).catch(function() {});

      // One render for the main process's updater state, used for live events
      // and for the replay on every page load, so both always look the same.
      // The bell counts any update the user has been told about: found and
      // downloading, downloaded, or a download that then failed.
      // A state older than the one already shown (the page's replay answered
      // after a live event) is ignored; the revision outlives a Back restore.
      function renderUpdateState(s) {
        if (s.revision < (window._celerpUpdateRevision || 0)) return;
        window._celerpUpdateRevision = s.revision;
        var i18n = window.__shellI18n;
        if (s.status === 'downloading') {
          setState(s.percent > 0 ? i18n.downloadingPct.replace('{pct}', Math.round(s.percent))
                   : s.version ? i18n.downloadingVersion.replace('{version}', s.version)
                   : i18n.downloadingUpdate, false);
          setCheckBtn(false);
          setProgress(s.percent);
        } else if (s.status === 'downloaded') {
          setState(s.version ? i18n.versionReady.replace('{version}', s.version) : i18n.updateReady, true);
          setCheckBtn(false);
          setProgress(100);
        } else if (s.checking) {
          // A check has started and has no result yet.
          setState(i18n.checking, false);
          setCheckBtn(false);
          setProgress(-1);
        } else if (s.status === 'error') {
          setState(i18n.updateFailed[s.failure] || i18n.updateCheckFailed, false);
          resetToIdle();
        } else {
          setState(i18n.upToDate, false);
          resetToIdle();
        }
        setInstallerLink(s.status === 'error' ? s.releaseUrl : '');
        setHelpLink(s.status === 'error' && s.failure === 'verify');
        setLog(s.log);
        window.celerpSetUpdateBell(s.status === 'downloading' || s.status === 'downloaded'
                                   || (s.status === 'error' && s.downloadFailed));
      }

      // Updater events are subscribed once per page load and always reach the
      // card on screen now, which a Back restore from the htmx history cache
      // replaces with fresh elements.
      window._celerpUpdateCard = { render: renderUpdateState };
      if (!window._celerpUpdateSubscribed) {
        window._celerpUpdateSubscribed = true;
        var render = function(s) { window._celerpUpdateCard.render(s); };
        window.celerp.onUpdateLog(render);
        window.celerp.onUpdateAvailable(render);
        window.celerp.onDownloadProgress(render);
        window.celerp.onUpdateDownloaded(render);
        window.celerp.onUpdateNotAvailable(render);
        window.celerp.onUpdateError(render);
      }

      // Replay whatever the updater did before this card appeared. Until it
      // answers, the card stays as rendered (no status claimed).
      window.celerp.getUpdateState().then(renderUpdateState).catch(function() {});

      if (checkBtn) {
        checkBtn.addEventListener('click', function() {
          setCheckBtn(false);
          setState(window.__shellI18n.checking, false);
          window.celerp.checkForUpdates().catch(function() {
            setState(window.__shellI18n.updateCheckFailed, false);
            resetToIdle();
          });
        });
      }

      if (restartBtn) {
        restartBtn.addEventListener('click', function() {
          restartBtn.disabled = true;
          restartBtn.textContent = window.__shellI18n.restarting;
          window.celerp.installUpdate();
        });
      }
    } else {
      // ── pip path ──────────────────────────────────────────────────────
      // The API checks the package index and says whether this install can
      // update itself and whether this user may install; the card shows that.
      var i18n = window.__shellI18n;
      var releaseEl = card.querySelector('.update-card__release');
      var upgradeEl = card.querySelector('.update-card__upgrade-cmd');
      var autoEl = card.querySelector('.update-card__auto');
      var autoInput = card.querySelector('.update-card__auto-input');
      // Reasons the pip command is the way to update (see the markup).
      var pipReasons = upgradeEl ? upgradeEl.dataset.reasons.split(' ') : [];
      var lastStatus = null;
      if (progressBar) progressBar.style.display = 'none';

      function show(el, visible) { if (el) el.style.display = visible ? '' : 'none'; }
      function reasonText(code) { return i18n.updateBlocked[code] || ''; }

      function resultText(r) {
        if (r.ok) return i18n.updateResultOk.replace('{version}', r.to);
        var text = r.outcome === 'rollback_failed' ? i18n.updateResultRollbackFailed : i18n.updateResultFailed;
        return text.replace('{version}', r.to).replace('{reason}', r.detail || i18n.updateReasons[r.reason]);
      }

      function render(s) {
        lastStatus = s;
        if (versionEl) versionEl.textContent = 'v' + s.current;
        var notes = [];
        // The last attempt stays on the card while it is still the news: the
        // version it installed is running, or the version it failed on is
        // still the one on offer.
        var r = s.last_result;
        if (r && ((r.ok && r.to === s.current) || (!r.ok && r.to === s.latest))) notes.push(resultText(r));
        show(upgradeEl, false);
        show(autoEl, s.can_install);
        if (autoInput) autoInput.checked = !!s.auto;
        setCheckBtn(s.reason !== 'administrator' && !s.installing);
        if (s.installing) {
          setState(reasonText('in_progress'), false);
          show(autoEl, false);
        } else if (s.check_error) {
          setState(i18n.updateCheckFailed, false);
        } else if (!s.checked_at) {
          setState(i18n.updateNotChecked, false);
        } else if (s.latest) {
          setState(i18n.updateAvailablePrefix + s.latest, s.can_install);
          window.celerpSetUpdateBell(true);
          if (!s.can_install) {
            notes.push(reasonText(s.reason));
            show(upgradeEl, pipReasons.indexOf(s.reason) !== -1);
          }
        } else {
          setState(i18n.upToDate, false);
        }
        if (releaseEl) releaseEl.textContent = notes.join(' ');
      }

      function load(method, url) {
        return fetch(url, { method: method }).then(function(r) {
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        });
      }

      function refresh(check) {
        setCheckBtn(false);
        setState(i18n.checking, false);
        load(check ? 'POST' : 'GET', check ? '/system/update/check' : '/system/update')
          .then(render)
          .catch(function() {
            setState(i18n.updateCheckFailed, false);
            setCheckBtn(true);
          });
      }

      // Celerp restarts to install, so the UI itself is away for part of the
      // wait: a failed poll means "still restarting", not an error.
      function waitForUpdate(target, before) {
        var deadline = Date.now() + 5 * 60 * 1000;
        (function poll() {
          if (Date.now() > deadline) { setState(i18n.updateStillRestarting, false); return; }
          setTimeout(function() {
            load('GET', '/system/update').then(function(s) {
              var r = s.last_result;
              if (!s.installing && r && r.to === target && r.at !== before) { window.location.reload(); return; }
              poll();
            }).catch(poll);
          }, 3000);
        })();
      }

      if (checkBtn) {
        checkBtn.addEventListener('click', function() { refresh(true); });
      }

      if (restartBtn) {
        restartBtn.addEventListener('click', function() {
          if (!confirm(restartBtn.dataset.confirm)) return;
          var before = lastStatus && lastStatus.last_result ? lastStatus.last_result.at : null;
          restartBtn.disabled = true;
          fetch('/system/update', { method: 'POST' }).then(function(r) {
            return r.json().catch(function() { return {}; }).then(function(body) {
              restartBtn.disabled = false;
              if (!r.ok) {
                setState(reasonText(body.detail) || i18n.updateCheckFailed, false);
                return;
              }
              setState(i18n.updateInstalling.replace('{version}', body.installing), false);
              setCheckBtn(false);
              show(autoEl, false);
              waitForUpdate(body.installing, before);
            });
          }).catch(function() {
            restartBtn.disabled = false;
            setState(i18n.updateCheckFailed, false);
          });
        });
      }

      if (autoInput) {
        autoInput.addEventListener('change', function() {
          var wanted = autoInput.checked;
          fetch('/system/update/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ auto: wanted }),
          }).then(function(r) {
            if (!r.ok) throw new Error(String(r.status));
          }).catch(function() {
            autoInput.checked = !wanted;
            setState(i18n.requestFailed, false);
          });
        });
        autoInput.addEventListener('keydown', function(e) {
          if (e.key === 'Escape') autoInput.blur();
        });
      }

      var copyBtn = card.querySelector('.update-card__copy-btn');
      if (copyBtn) {
        copyBtn.addEventListener('click', function() {
          navigator.clipboard.writeText('pip install --upgrade celerp').catch(function() {});
          _copiedFeedback(copyBtn, i18n.copyLabel);
        });
      }

      refresh(false);
    }
  }
  initUpdateCard();
  // Back and Forward can restore a page from the htmx history cache instead of
  // loading it. The restored card is a saved copy with no listeners that shows
  // the status from when it was saved, so set it up again.
  document.addEventListener('htmx:historyRestore', initUpdateCard);
});
"""

_STAR_CTA_JS = """
// Decorative supporter chrome shares one request queue. Starting it after load keeps
// these best-effort calls out of the page's interactive relay fan-out, while the cache
// deduplicates /stars/badge when both the footer and supporter card need it.
window.celerpStarFetch = window.celerpStarFetch || (function(){
  var cache = {};
  var queue = new Promise(function(resolve){
    if (document.readyState === 'complete') resolve();
    else window.addEventListener('load', resolve, {once:true});
  });
  return function(url){
    if (cache[url]) return cache[url];
    var request = queue.catch(function(){}).then(function(){
      return fetch(url).then(function(r){
        if (!r.ok) throw new Error('star request failed: ' + r.status);
        return r.json();
      });
    });
    queue = request.catch(function(){});
    cache[url] = request;
    return request;
  };
})();
// Distinct local calendar days the dashboard was opened on, newest 30 kept. The star
// ask on the dashboard waits for the tenth. Storage that is blocked counts as day 0.
window.celerpUseDays = function(record){
  try {
    var key = 'celerp.dashboardDays';
    var days = JSON.parse(localStorage.getItem(key) || '[]');
    if (!Array.isArray(days)) days = [];
    if (record) {
      var d = new Date();
      var today = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
      if (days.indexOf(today) === -1) days.push(today);
      days = days.slice(-30);
      localStorage.setItem(key, JSON.stringify(days));
    }
    return days.length;
  } catch (e) { return 0; }
};
(function(){
  window.celerpStarFetch('/stars/cta?medium=footer').then(function(d){
    var el = document.getElementById('star-cta');
    if (!el || !d || !d.url) return;
    var label = '\\u2605 ' + window.__shellI18n.starOnGithub;
    if (d.show_count && d.count != null) label = '\\u2605 ' + d.count;  // proof/momentum: star count
    el.textContent = label;
    el.href = d.url;
    el.title = d.tooltip || window.__shellI18n.appreciateSupport;
    el.style.display = '';
  }).catch(function(){});
  window.celerpStarFetch('/stars/badge').then(function(d){
    var el = document.getElementById('supporter-badge');
    if (!el || !d || !d.badge) return;
    el.textContent = '\\u2605';            // just the gold star
    el.title = d.badge.label;              // tooltip: e.g. "First Stargazers (#1)"
    el.style.display = '';
  }).catch(function(){});
})();
"""

_LIST_HINT_JS = """
// A list page opened with ?hint=<name> points an arrow at one control: ?hint=import
// (the dashboard's import card) at the page's Import button, ?hint=demo (the
// dashboard's demo note) at the select-all box over the demo items. The parameter is
// dropped from the address straight away, so a refresh or a shared link never shows
// the arrow again. The arrow sits in the page flow, so it never covers another
// control, and goes on any click, its close button included, or on Esc.
(function(){
  var HINTS = {
    // Under the whole header row, pointing up at the button.
    import: {target: '[data-import-hint]', text: 'importHint',
             row: function(el){ return el.closest('.page-header') || el.parentElement; }},
    // Right above the table, pointing down at the box.
    demo: {target: '#select-all-rows', text: 'demoHint', above: true,
           row: function(el){ return el.closest('.table-scroll-wrap') || el.closest('table'); }}
  };
  var params = new URLSearchParams(location.search);
  var name = params.get('hint');
  if (!Object.prototype.hasOwnProperty.call(HINTS, name)) return;
  var hint = HINTS[name];
  params.delete('hint');
  var qs = params.toString();
  history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
  function start(){
    var btn = document.querySelector(hint.target);
    if (!btn) return;
    var row = hint.row(btn);
    var tip = document.createElement('div');
    tip.className = 'import-arrow' + (hint.above ? ' import-arrow--down' : '');
    tip.setAttribute('role', 'status');
    tip.appendChild(document.createTextNode(window.__shellI18n[hint.text]));
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'import-arrow-close';
    x.setAttribute('aria-label', window.__shellI18n.close);
    x.textContent = '\\u00d7';
    tip.appendChild(x);
    if (hint.above) row.before(tip); else row.after(tip);
    btn.classList.add('import-arrow-pulse');
    // Slide the arrow along its own row so its pointer sits on the control.
    function place(){
      var b = btn.getBoundingClientRect();
      var r = tip.parentElement.getBoundingClientRect();
      var w = tip.offsetWidth;
      var mid = b.left + b.width / 2 - r.left;
      var left = Math.max(0, Math.min(mid - w / 2, r.width - w));
      tip.style.marginLeft = left + 'px';
      tip.style.setProperty('--arrow-x', Math.max(12, Math.min(mid - left, w - 12)) + 'px');
    }
    function close(){
      tip.remove();
      btn.classList.remove('import-arrow-pulse');
      document.removeEventListener('click', close, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', place);
      window.removeEventListener('load', place);
    }
    function onKey(e){ if (e.key === 'Escape') close(); }
    document.addEventListener('click', close, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', place);
    // Late layout (fonts) can still move the control after load.
    window.addEventListener('load', place);
    place();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
"""


def star_supporter_card() -> FT:
    """The GitHub-star ask: a gold-bordered, dismissable card. The COPY (header + body
    + tooltip) is the relay's single source of truth, hydrated from /stars/cta; the card
    shows only in a non-neutral mode (relay reachable) and when not dismissed. Rendered
    on the dashboard (where setup lands)."""
    # Fetch the CTA copy + the user's badge together. If they've already claimed, show
    # the relay's thank-you copy ({badge} -> their label); otherwise show the ask. All
    # copy stays relay-sourced; the card is hidden in neutral (relay down) or dismissed.
    js = (
        # Deferred one tick: when htmx swaps this card in for the import card, the
        # import card is still in the page while the swap runs.
        "setTimeout(function(){"
        "if(!window.celerpStarFetch)return;"
        # The ask waits for 10 days of use and for the import card to be gone.
        "if(!window.celerpUseDays||window.celerpUseDays(false)<10"
        "||document.getElementById('getting-started-card'))return;"
        "Promise.all(["
        "window.celerpStarFetch('/stars/cta?medium=dashboard').catch(function(){return null}),"
        "window.celerpStarFetch('/stars/badge').catch(function(){return null})"
        "]).then(function(res){"
        "var d=res[0],bd=res[1];"
        "if(!d||d.dismissed||d.mode==='neutral')return;"
        "var card=document.getElementById('star-supporter-card');if(!card)return;"
        "var h=document.getElementById('star-card-headline');"
        "var b=document.getElementById('star-card-body');"
        "var b2=document.getElementById('star-card-body-2');"
        "var actions=document.getElementById('star-card-actions');"
        "var wall=document.getElementById('star-card-wall');"
        "var link=document.getElementById('star-card-star');"
        "if(link&&d.url)link.href=d.url;"
        "if(bd&&bd.badge){"
        "if(h)h.textContent=d.thanks_headline||card.dataset.fallbackThanksHeadline;"
        "if(b)b.textContent=(d.thanks_body||'').replace('{badge}',bd.badge.label);"
        "if(b2)b2.style.display='none';"
        "if(actions)actions.style.display='none';"
        "if(wall&&bd.wall_url){wall.href=bd.wall_url;wall.style.display='inline-block';}"
        "}else{"
        "if(h)h.textContent=d.headline||card.dataset.fallbackHeadline;"
        "var parts=(d.body||'').split('\\n\\n');"
        "if(b)b.textContent=parts[0]||'';"
        "if(b2)b2.textContent=parts.slice(1).join('\\n\\n');"
        "if(wall)wall.style.display='none';"
        "}"
        "card.style.display='';"
        "}).catch(function(){});"
        "var dz=document.getElementById('star-card-dismiss');"
        "if(dz)dz.addEventListener('click',function(){"
        "fetch('/stars/dismiss',{method:'POST'}).then(function(){"
        "var c=document.getElementById('star-supporter-card');if(c)c.style.display='none';});});"
        "},0);"
    )
    gold = "color:#d4af37"
    # Static fallbacks for when the relay does not supply its own copy (R2): translated
    # here in Python and handed to the client via data-* attributes the script above
    # reads, never spliced into the JS source directly.
    fallback_headline = t("shell.star_on_github")
    fallback_thanks_headline = t("shell.thank_you_support")
    return Div(
        Div(
            # Dismiss = X in the top-right corner.
            Button("×", id="star-card-dismiss", type="button", title=t("settings.dismiss"),
                   aria_label=t("settings.dismiss"),
                   style="position:absolute;top:10px;right:14px;background:none;border:none;"
                         "font-size:24px;line-height:1;cursor:pointer;color:#999;padding:0"),
            # Header IS the link to the repo (gold star + the relay-hydrated ask).
            H3(
                A(Span("★ ", style=gold), Span("", id="star-card-headline"),
                  id="star-card-star", href="#", target="_blank", rel="noopener",
                  style="text-decoration:underline;color:inherit;cursor:pointer"),
                style="margin:0 0 14px",
            ),
            P("", id="star-card-body", style="white-space:pre-line;margin:0"),
            # Smaller sub-paragraph for the founding-badge promise.
            P("", id="star-card-body-2", style="white-space:pre-line;margin:12px 0 0;font-size:13px;color:#555"),
            # Claiming a badge lists the founder on the public wall (that is what the
            # badge is). Hidden once claimed; the wall link below takes its place.
            Div(
                A(t("shell.claim_your_badge"), id="star-card-claim", href="/stars/claim", cls="btn btn--primary"),
                P(t("shell.claim_badge_note"), id="star-card-claim-note",
                  style="margin:8px 0 0;font-size:13px;color:#555"),
                id="star-card-actions",
                style="margin-top:22px",
            ),
            # Shown only once claimed: a quiet link to the public founders wall.
            A(Span("★ ", style=gold), t("shell.see_founders_wall"),
              id="star-card-wall", href="#", target="_blank", rel="noopener",
              style="display:none;margin-top:18px;font-size:13px;text-decoration:underline;color:inherit"),
            id="star-supporter-card",
            data_fallback_headline=fallback_headline,
            data_fallback_thanks_headline=fallback_thanks_headline,
            style="display:none;position:relative;margin:16px 0;padding:28px 32px;"
                  "border:1px solid #d4af37;border-radius:10px;text-align:center",
        ),
        Script(js),
    )


_STICKY_HEADER_JS = """
(function(){
  // Freeze the header row of the excel-style list tables while the table body is on screen.
  // Opt-in by class: only table.data-table.sticky-head is touched. The header cannot both live
  // inside the horizontal-scroll wrap and stick to the window via pure CSS (the overflow-x wrap
  // clips position:sticky), so this pins the LIVE thead with position:fixed (from CSS) and keeps
  // the whole table's column geometry aligned. See app.css .data-table.sticky-head.hdr-pinned.
  var SEL = 'table.data-table.sticky-head';
  var mqPrint = window.matchMedia ? window.matchMedia('print') : null;
  var mo = null;
  // Set the moment print is requested and cleared when it ends. beforeprint fires before some
  // browsers flip the print media query, and a mutation arriving in that gap would otherwise
  // re-pin the header we just cleared; this flag keeps the controller a no-op through print.
  var printing = false;

  function isPinned(t){ return t.classList.contains('hdr-pinned'); }

  // The phantom top-scrollbar (.table-top-scroll) is a sibling of the table's .table-scroll-wrap,
  // both under #data-table-wrap, and exists only when the table has enough rows to warrant it
  // (ui/components/table.py). When the header freezes, the phantom must freeze stacked directly
  // above it so a user scrolled mid-table can still scroll left/right; it is null when absent.
  function wrapOf(t){ return t.closest('.table-scroll-wrap'); }
  function phantomOf(t){
    var w = wrapOf(t); var c = w && w.parentElement;
    return c ? c.querySelector(':scope > .table-top-scroll') : null;
  }

  // Every DOM write the controller makes (colgroup, spacer row, thead styles, the pinned class)
  // is itself a mutation. MutationObserver callbacks are async microtasks, so a boolean "busy"
  // guard is already reset by the time the callback runs and cannot suppress them - the observer
  // would re-fire on its own writes forever. Disconnect it for the duration of every write pass
  // instead; genuine external mutations (rows added, an inline edit) still fire once we reconnect.
  function pauseObserver(fn){
    if(mo){ mo.disconnect(); }
    try { fn(); } finally { observe(); }
  }

  function reposition(t){
    var thead = t.tHead; if(!thead) return;
    var rect = t.getBoundingClientRect();
    // rect.left already reflects the wrap's horizontal scroll, so it is the body's content left.
    thead.style.left = rect.left + 'px';
    thead.style.width = rect.width + 'px';
    // The header is position:fixed, so the wrap's overflow does not clip it: scrolled sideways it
    // would paint over the sidebar on the left and past the table on the right. Clip it to the
    // wrap's visible box, the same geometry the phantom scrollbar below already tracks. clip-path
    // clips painting and hit-testing but leaves getBoundingClientRect intact, so columns stay aligned.
    var w = wrapOf(t);
    if(w){
      var wr = w.getBoundingClientRect();
      var clipL = Math.max(0, wr.left - rect.left);
      var clipR = Math.max(0, (rect.left + rect.width) - (wr.left + w.clientWidth));
      thead.style.clipPath = 'inset(0 ' + clipR + 'px 0 ' + clipL + 'px)';
      // Keep the frozen phantom aligned with the on-screen wrap so its track spans the visible body.
      // Its scrollLeft stays synced to the wrap by the existing bidirectional listener, unaffected
      // by fixing position (that sync is scrollLeft-based, not layout-based).
      var ph = phantomOf(t);
      if(ph && ph.classList.contains('top-scroll-pinned')){
        ph.style.left = wr.left + 'px';
        ph.style.width = w.clientWidth + 'px';
      }
    } else {
      thead.style.clipPath = '';
    }
  }

  // Remove every colgroup/spacer the controller has ever added to this table. querySelectorAll,
  // not querySelector: a re-render can leave the artifacts in place while stripping the pinned
  // class, so pin/unpin must clear ALL of them or a duplicate survives every later cycle.
  function clearPinArtifacts(t){
    var cgs = t.querySelectorAll(':scope > colgroup.hdr-cg');
    for(var i=0;i<cgs.length;i++) cgs[i].remove();
    var tb = t.tBodies[0];
    if(tb){ var sps = tb.querySelectorAll(':scope > tr.hdr-spacer'); for(var j=0;j<sps.length;j++) sps[j].remove(); }
    // Revert the frozen phantom scrollbar and drop every top-scroll spacer this table ever added.
    var ph = phantomOf(t);
    if(ph){ ph.classList.remove('top-scroll-pinned'); ph.style.left = ''; ph.style.width = ''; }
    t.classList.remove('has-top-phantom');
    var wrap = wrapOf(t); var cont = wrap && wrap.parentElement;
    if(cont){ var tss = cont.querySelectorAll(':scope > .top-scroll-spacer'); for(var k=0;k<tss.length;k++) tss[k].remove(); }
  }

  function pin(t){
    var thead = t.tHead; if(!thead || !thead.rows.length) return;
    // Idempotent: drop any stale artifacts before recapturing, so re-pinning never duplicates them.
    clearPinArtifacts(t);
    // Visible cells only: the column manager hides a column by display:none on its th and tds,
    // and hidden cells generate no boxes. colgroup cols map to RENDERED boxes positionally, so a
    // col emitted for a hidden column would land on the next visible column and shift every
    // column after it under the wrong header.
    var allCells = thead.rows[0].cells, cells = [], ci;
    for(ci=0;ci<allCells.length;ci++){ if(getComputedStyle(allCells[ci]).display !== 'none') cells.push(allCells[ci]); }
    if(!cells.length) return;
    var widths = [], i;
    for(i=0;i<cells.length;i++) widths.push(cells[i].getBoundingClientRect().width);
    var theadH = thead.getBoundingClientRect().height;
    var tableW = t.getBoundingClientRect().width;
    // Lock the whole table's columns so the body does not re-resolve once the thead leaves flow.
    var cg = document.createElement('colgroup'); cg.className = 'hdr-cg';
    for(i=0;i<widths.length;i++){ var col = document.createElement('col'); col.style.width = widths[i] + 'px'; cg.appendChild(col); }
    t.insertBefore(cg, t.firstChild);
    t.style.tableLayout = 'fixed';
    t.style.width = tableW + 'px';
    // Spacer row keeps the body from jumping up by the header's height while it is fixed.
    var tb = t.tBodies[0];
    if(tb){
      var sp = document.createElement('tr'); sp.className = 'hdr-spacer';
      var td = document.createElement('td'); td.colSpan = cells.length;
      td.style.padding = '0'; td.style.border = '0'; td.style.height = theadH + 'px';
      sp.appendChild(td); tb.insertBefore(sp, tb.firstChild);
    }
    // The fixed thead is its own table box so its cells lay out at the captured widths.
    thead.style.display = 'table';
    thead.style.tableLayout = 'fixed';
    for(i=0;i<cells.length;i++) cells[i].style.width = widths[i] + 'px';
    // Freeze the phantom top-scrollbar stacked directly above the header when one exists. The
    // has-top-phantom class shifts the fixed thead down by the phantom height (CSS) so they stack;
    // a spacer where the phantom sat keeps the content below from jumping up as it leaves flow.
    var ph = phantomOf(t);
    if(ph){
      t.classList.add('has-top-phantom');
      ph.classList.add('top-scroll-pinned');
      var cont = wrapOf(t).parentElement;
      var sp2 = document.createElement('div'); sp2.className = 'top-scroll-spacer';
      sp2.style.height = ph.getBoundingClientRect().height + 'px';
      cont.insertBefore(sp2, ph);
    }
    t.classList.add('hdr-pinned');
    reposition(t);
  }

  function unpin(t){
    t.classList.remove('hdr-pinned');
    var thead = t.tHead;
    if(thead){
      thead.style.display = ''; thead.style.tableLayout = ''; thead.style.width = ''; thead.style.left = ''; thead.style.clipPath = '';
      // Leave the cell widths in place: they carry the user's applied/resized column sizes, and a
      // re-pin recaptures from them. Blanking them here dropped custom widths on scroll-up and wiped
      // an in-progress resize before it could persist.
    }
    t.style.tableLayout = ''; t.style.width = '';
    clearPinArtifacts(t);
  }

  function unpinAll(){
    pauseObserver(function(){
      var tables = document.querySelectorAll(SEL);
      for(var i=0;i<tables.length;i++) if(isPinned(tables[i])) unpin(tables[i]);
    });
  }

  function evaluate(full){
    if(printing || (mqPrint && mqPrint.matches)){ unpinAll(); return; }
    // A column resize sets th widths pixel by pixel; leave the header alone until the drag ends,
    // or the per-mutation re-pin recaptures the pre-drag width every move and the drag never sticks.
    if(document.body && document.body.classList.contains('col-resizing')) return;
    pauseObserver(function(){
      var tables = document.querySelectorAll(SEL);
      for(var i=0;i<tables.length;i++){
        var t = tables[i];
        var thead = t.tHead; if(!thead){ continue; }
        // On a full re-evaluate (resize, re-render, reorder) drop stale geometry so widths recapture.
        if(full && isPinned(t)) unpin(t);
        var rect = t.getBoundingClientRect();
        var theadH = thead.getBoundingClientRect().height;
        var shouldPin = rect.top <= 0 && (rect.bottom - theadH) > 0;
        if(shouldPin){
          if(!isPinned(t)) pin(t); else reposition(t);
        } else if(isPinned(t)){
          unpin(t);
        }
      }
    });
  }

  function onScroll(){ evaluate(false); }
  function onFull(){ evaluate(true); }

  document.addEventListener('DOMContentLoaded', onFull);
  // Capture phase so whichever element scrolls vertically (window or an inner wrap) is caught,
  // and so the wrap's horizontal scroll repositions the frozen header in lockstep with the body.
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onFull);
  document.addEventListener('htmx:afterSwap', onFull);
  document.addEventListener('celerp:col-reorder', onFull);
  window.addEventListener('beforeprint', function(){ printing = true; unpinAll(); });
  window.addEventListener('afterprint', function(){ printing = false; onScroll(); });

  mo = new MutationObserver(onFull);
  // attributes:true catches a column-resize style write on a th; the controller's own writes
  // never re-fire it because pauseObserver disconnects the observer for the duration of each pass.
  function observe(){ if(mo && document.body) mo.observe(document.body, {childList:true, subtree:true, attributes:true}); }
  if(document.body) observe(); else document.addEventListener('DOMContentLoaded', observe);
})();

(function(){
  // Scroll cue: a data table's box that scrolls sideways shades the edge it can still scroll
  // toward (a box that does not scroll is never shaded) (app.css .scroll-more-left/right), so hidden columns read as there.
  function update(box){
    var max = /auto|scroll/.test(getComputedStyle(box).overflowX) ? box.scrollWidth - box.clientWidth : 0;
    box.classList.toggle('scroll-more-right', max > 1 && box.scrollLeft < max - 1);
    box.classList.toggle('scroll-more-left', max > 1 && box.scrollLeft > 1);
    box.style.setProperty('--scroll-x', box.scrollLeft + 'px');
  }
  // The box changes width with the window; the table changes width with its rows.
  var sizes = new ResizeObserver(function(entries){
    entries.forEach(function(e){ update(e.target.matches('table') ? e.target.parentElement : e.target); });
  });
  function watch(){
    document.querySelectorAll('table.data-table').forEach(function(t){
      var box = t.parentElement;
      if(!box || box.dataset.scrollCue) return;
      box.dataset.scrollCue = '1';
      box.addEventListener('scroll', function(){ update(box); }, {passive: true});
      sizes.observe(box); sizes.observe(t);
      update(box);
    });
  }
  document.addEventListener('DOMContentLoaded', watch);
  document.addEventListener('htmx:afterSwap', watch);
})();
"""


def page_title(label_key: str) -> str:
    """Compose a browser <title> from a translation key (R5): resolves at call
    time in the current request's language, so route handlers never hardcode
    an English "<Thing> - Celerp" string."""
    return t(label_key) + " - Celerp"


def _shell_js_i18n(lang: str = "en") -> dict:
    """Translated strings the static JS bundle needs (R2): resolved here at render
    time and handed to the client as a single config object, never spliced into
    the JS source. Read by _CLIENT_JS, _NOTIFICATION_JS, _STAR_CTA_JS and
    _LIST_HINT_JS; the config is injected by client_scripts, before any of them."""
    return {
        "copied": t("shell.copied", lang),
        "comboPickOrAdd": t("shell.combo_pick_or_add", lang),
        "copyLabel": t("btn.copy", lang),
        "requestFailed": t("shell.request_failed", lang),
        "networkError": t("shell.network_error", lang),
        "markAsRead": t("shell.mark_as_read", lang),
        "noNotifications": t("shell.no_notifications", lang),
        "upToDate": t("shell.up_to_date", lang),
        "downloadingVersion": t("shell.js_downloading_version", lang),
        "downloadingUpdate": t("shell.js_downloading_update", lang),
        "downloadingPct": t("shell.js_downloading_pct", lang),
        "versionReady": t("shell.js_version_ready", lang),
        "updateReady": t("shell.js_update_ready", lang),
        "updateCheckFailed": t("shell.update_check_failed", lang),
        "updateFailed": {kind: t(f"shell.update_{kind}_failed", lang)
                         for kind in ("check", "download", "save", "verify", "install")},
        "checking": t("shell.checking", lang),
        "restarting": t("shell.restarting", lang),
        "updateAvailablePrefix": t("shell.update_available_prefix", lang),
        "updateNotChecked": t("shell.update_not_checked", lang),
        "updateInstalling": t("shell.update_installing", lang),
        "updateStillRestarting": t("shell.update_still_restarting", lang),
        "updateResultOk": t("shell.update_result_ok", lang),
        "updateResultFailed": t("shell.update_result_failed", lang),
        "updateResultRollbackFailed": t("shell.update_result_rollback_failed", lang),
        "updateBlocked": {code: t(f"shell.update_blocked_{code}", lang) for code in CARD_REASONS},
        "updateReasons": {code: t(reason_key(code), lang) for code in REASON_CODES},
        "starOnGithub": t("shell.star_on_github", lang),
        "appreciateSupport": t("shell.appreciate_support", lang),
        "importHint": t("shell.import_hint", lang),
        "demoHint": t("shell.demo_hint", lang),
        "close": t("btn.close", lang),
        "tryAgain": t("btn.try_again", lang),
    }


_RELAY_CUSTOMER_HOST_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.celerp\.com(?::\d+)?$", re.IGNORECASE
)
_RELAY_RESERVED_HOSTS = frozenset({
    "relay", "www", "gateway", "api", "mail", "send", "pay", "share",
})


def _relay_info_from_request(request) -> dict | None:
    """Return known relay state when this page itself arrived through Connect.

    The cloud proxy stamps the original customer host and HTTPS scheme before it
    forwards the request through the gateway. A page that reached the UI through
    that path is already proof that the relay tunnel is up for this request, so the
    shell must not spend a second relay slot asking /topbar-relay-status to prove it.
    Direct/LAN requests have no such proof and keep the existing lazy local probe.
    """
    if request is None:
        return None
    host = (request.headers.get("x-forwarded-host") or "").strip().lower()
    proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    if proto != "https" or not _RELAY_CUSTOMER_HOST_RE.fullmatch(host):
        return None
    if host.split(".", 1)[0] in _RELAY_RESERVED_HOSTS:
        return None
    return {"connected": True, "public_url": f"https://{host}"}


def _has_get_route(routes, path: str) -> bool:
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in routes)


def try_again_address(request) -> str:
    """Where the Try again button goes when the page came from a form post: the nearest page
    at or above it that answers GET (the start of that form), so selecting it never sends the
    form again. Empty for a page that came from GET (or rendered outside the app): the button
    then opens the address the browser shows."""
    app = request.scope.get("app") if request is not None else None
    if app is None or request.method == "GET":
        return ""
    path = request.url.path
    while path not in ("", "/") and not _has_get_route(app.router.routes, path):
        path = path.rsplit("/", 1)[0]
    return "/" + path.lstrip("/")


def _js_value(value) -> str:
    """*value* as a JavaScript literal that is safe inside a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def try_again_script(lang: str) -> str:
    """The script behind _try_again_script_src: which messages get the Try again button, in *lang*."""
    return f"window.__celerpTryAgainPattern = {_js_value(try_again_pattern(lang))};"


def try_again_version(lang: str) -> str:
    """The version in the Try again script's address: it changes whenever the script does."""
    return hashlib.sha1(try_again_pattern(lang).encode()).hexdigest()[:12]


def _try_again_script_src(lang: str) -> str:
    return f"/static/try-again/{lang}.js?v={try_again_version(lang)}"


def client_scripts(lang: str = "en", request=None) -> list:
    """htmx, the translated strings the client bundle reads (R2) and the shared client
    bundle (searchable select, ESC handling), in that order: the config must come
    before any script that references window.__shellI18n. Every full-chrome page
    carries them; a page rendered in auth_shell adds them itself. The Try again
    pattern is a separate script the browser caches, so pages stay small."""
    config = {**_shell_js_i18n(lang), "tryAgainHref": try_again_address(request)}
    return [
        Script(src="/static/htmx.min.js"),
        Script(f"window.__shellI18n = {_js_value(config)};"),
        Script(src=_try_again_script_src(lang)),
        Script(_CLIENT_JS),
    ]


def _favicon_links() -> tuple[FT, ...]:
    """The Celerp shield icon set, made from the same master as the desktop app icon."""
    return (
        Link(rel="icon", type="image/x-icon", href="/static/favicon.ico"),
        Link(rel="icon", type="image/png", sizes="32x32", href="/static/favicon-32x32.png"),
        Link(rel="apple-touch-icon", sizes="180x180", href="/static/apple-touch-icon.png"),
    )


def _redirect_notice(request, lang: str) -> list:
    """Why the caller was sent here, on whichever page answers the redirect: the
    dashboard, or the page standing in for it where the company turned it off
    (ui.security.not_permitted_redirect), and, after setup by someone who cannot
    restart Celerp, that the business type's modules wait for that restart."""
    from ui.security import take_not_permitted
    notices = []
    if take_not_permitted(request):
        notices.append(flash(t("perm.redirected_no_access", lang)))
    if request is not None and request.query_params.get("modules") == "pending":
        notices.append(flash(t("setup.modules_pending", lang), kind="info"))
    return notices


def restore_outcome_parts(base_msg: str, warnings: list, safety_archive: str | None) -> list[str]:
    """The sentences that report a finished System Recovery: *base_msg*, each warning,
    then where the safety copy of the replaced installation was saved."""
    parts = [base_msg, *(str(w) for w in warnings)]
    if safety_archive:
        parts.append(t("system_recovery.safety_saved", path=safety_archive))
    return parts


async def _recovery_outcome_notice(request) -> list:
    """The outcome of a System Recovery, which ended every session, on the installation
    owner's first signed-in page (backup_import.take_recovery_failure and
    take_restore_notice). It names server paths, so nobody else sees it, and it stays
    for the owner until then."""
    from celerp.services import backup_import
    from ui.config import get_token
    token = get_token(request) if request is not None else None
    if not token or not (backup_import.recovery_failure_waiting() or backup_import.restore_notice_waiting()):
        return []
    import ui.api_client as _api
    if not await _api.installation_owner(token):
        return []
    notices = []
    if message := backup_import.take_recovery_failure():
        notices.append(flash(message))
    if (restored := backup_import.take_restore_notice()) is not None:
        warnings = restored.get("warnings") or []
        parts = restore_outcome_parts(t("system_recovery.imported"), warnings, restored.get("safety_archive"))
        notices.append(flash(" ".join(parts), kind="warning" if warnings else "success"))
    return notices


def _shell_document(*content, nav: FT, title: str = "Celerp", companies: list[dict] | None = None, extra_head: list | None = None, lang: str = "en", request=None, help_path: str | None = None) -> FT:
    """The outer HTML document shared by every full-chrome page: head assets, the
    supplied nav, top bar, banners, main content, and footer.

    The nav is passed in, never built here: `base_shell` supplies the
    permission-filtered sidebar, `minimal_shell` a nav-less one for pages rendered
    without an authorization context. `lang` is already resolved by the caller."""
    from ui.config import get_user_email
    user_email = get_user_email(request) if request is not None else None
    relay_info = _relay_info_from_request(request)
    head_items = [
        Meta(charset="utf-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        Title(title),
        *_favicon_links(),
        Link(rel="stylesheet", href=f"/static/app.css?v={_CSS_VER}"),
        *client_scripts(lang, request),
        Script(ESC_BLURS_FIELD),
        Script(_idle_logout_js()),
        Script(_HEALTH_BANNER_JS),
        Script(_POLL_CONTROLLER_JS),
        Script(_BACKUP_BANNER_JS),
        Script(_NOTIFICATION_JS),
        Script(_USER_MENU_JS),
        Script(_BUG_LINK_JS),
        Script(_STAR_CTA_JS),
        Script(_LIST_HINT_JS),
        Script(_STICKY_HEADER_JS),
        Script(_INFO_TIP_JS),
    ]
    if extra_head:
        head_items.extend(extra_head)
    return Html(
        Head(*head_items),
        Body(
            Div(
                nav,
                Div(
                    _topbar(companies or [], lang=lang, user_email=user_email, relay_info=relay_info, help_path=help_path),
                    _HEALTH_BANNER_HTML,
                    _backup_banner_html(lang),
                    _OPEN_MIGRATIONS_LOADER,
                    _GLOBAL_UI_ERROR_HTML,
                    _TOAST_CONTAINER_HTML,
                    Main(*_redirect_notice(request, lang), *content, id="main-content", cls="main-content"),
                    Div(id="account-gate-host"),
                    Footer(
                        A(t("msg.powered_by", lang), href="https://www.celerp.com", target="_blank",
                          cls="brand-footer-link"),
                        A("", id="star-cta", href="#", target="_blank", rel="noopener",
                          cls="brand-footer-link",
                          style="display:none;margin-left:16px;color:#d4af37;font-weight:600"),
                        cls="brand-footer",
                    ),
                    cls="content-area",
                ),
                cls="app-shell",
            ),
            cls="app-body",
        ),
    )


def _minimal_sidebar() -> FT:
    """Sidebar chrome with no navigation - the logo only.

    Used by `minimal_shell` for pages rendered with no authorization context, so no
    permission-dependent nav is ever computed or shown."""
    return Nav(
        Div(
            A(Img(src="/static/logo.png", alt="Celerp", cls="sidebar-logo"), href="/dashboard"),
            cls="sidebar-logo-wrap",
        ),
        cls="sidebar",
    )


async def base_shell(*content, title: str = "Celerp", nav_active: str = "", companies: list[dict] | None = None, extra_head: list | None = None, lang: str = "en", request=None, company_settings: dict | None = None, help_path: str | None = None) -> FT:
    """Outer chrome: permission-filtered sidebar nav + top header + content area.

    ``help_path`` is the screen the top bar Help panel looks up, for a page whose
    address does not say which screen it shows (a tab chosen by role); without it
    the panel uses the browser's address."""
    from ui.config import get_token
    role = get_role(request) if request is not None else "owner"
    if company_settings is None:
        company_settings = {}
        if request is not None and (_tok := get_token(request)):
            try:
                import ui.api_client as _api
                _co = await _api.get_company(_tok)
                company_settings = (_co.get("settings") if isinstance(_co, dict) else {}) or {}
            except Exception:
                company_settings = {}
    if request is not None:
        lang = get_lang(request)
    sidebar = _sidebar(nav_active, lang=lang, role=role, request=request, settings=company_settings)
    return _shell_document(
        *await _recovery_outcome_notice(request), *content, nav=sidebar, title=title, companies=companies,
        extra_head=extra_head, lang=lang, request=request, help_path=help_path,
    )


def minimal_shell(*content, title: str = "Celerp", companies: list[dict] | None = None, extra_head: list | None = None, lang: str = "en", request=None) -> FT:
    """Outer chrome with NO permission-dependent navigation.

    For pages rendered when the company authorization state is unavailable - e.g.
    the fresh company read that authorizes a page has itself failed. It shows the
    header/top-bar chrome and the content, never a sidebar built from registry
    DEFAULT grants (which would present a company-revoked capability as available)
    and never a second company read to try to rebuild one."""
    if request is not None:
        lang = get_lang(request)
    return _shell_document(
        *content, nav=_minimal_sidebar(), title=title, companies=companies,
        extra_head=extra_head, lang=lang, request=request,
    )


_BUG_REPORT_URL: str | None = None


def _bug_report_url() -> str:
    """GitHub 'new issue' link that opens the bug_report.yml issue form with the
    Celerp version and host OS pre-filled (the two fields users most often leave out).
    The current page is appended to the form's `page` field client-side on click (see
    the menu link's onclick), since the path is only known in the browser.

    Built once per process - version and OS don't change at runtime. Uses the form's
    field-prefill query params, so the form's own label (bug) still applies for
    every reporter (unlike inline body prefill, which would suppress the template).
    """
    global _BUG_REPORT_URL
    if _BUG_REPORT_URL is None:
        import platform
        try:
            from celerp import __version__ as ver
        except Exception:
            ver = "unknown"
        system = platform.system()
        if system == "Darwin":
            osname = f"macOS {platform.mac_ver()[0]}".strip()
        elif system == "Windows":
            osname = f"Windows {platform.release()}".strip()
        else:
            osname = f"{system} {platform.release()}".strip() or platform.platform()
        qs = urlencode({"template": "bug_report.yml", "version": ver, "os": osname})
        _BUG_REPORT_URL = f"https://github.com/celerp/celerp/issues/new?{qs}"
    return _BUG_REPORT_URL


def bug_report_url(**fields: str) -> str:
    """The bug report link (_bug_report_url) with more of the form's fields pre-filled."""
    return _bug_report_url() + (f"&{urlencode(fields)}" if fields else "")


def search_help(lang: str = "en", panel_id: str = "global-search-help-panel") -> FT:
    """Reusable (?) help affordance for a search box: a keyboard-focusable button
    that toggles a panel describing the query syntax with a worked example.
    Opening/closing (click plus ESC) is handled by the delegated listeners in
    _CLIENT_JS (class-based, so multiple instances coexist on one page);
    panel_id keeps each instance's aria wiring unique."""
    op = lambda s: Span(s, cls="global-search-help-op")
    return Div(
        Button(
            "?",
            type="button",
            cls="global-search-help popover-toggle",
            aria_label=t("msg.search_help", lang),
            aria_expanded="false",
            aria_controls=panel_id,
        ),
        Div(
            Div(t("msg.search_help", lang), cls="global-search-help-title"),
            Ul(
                Li(t("shell.search_help_plain_text", lang)),
                Li(op(","), " ", t("shell.search_help_or", lang), " ", Code("bolt, nut"),
                   " ", t("shell.search_help_or_result", lang)),
                Li(op("&"), " ", t("shell.search_help_and", lang), " ", Code("bolt & steel"),
                   " ", t("shell.search_help_and_result", lang)),
                Li(op("5-10"), " ", t("shell.search_help_range", lang)),
                Li(op("field:"), " ", t("shell.search_help_scoped", lang)),
                Li(op("field: 1-2"), " ", t("shell.search_help_scoped_range", lang)),
                Li(op("field: a, b"), " ", t("shell.search_help_carry", lang), " ",
                   Code("barcode: 1042, 1043"), " ", t("shell.search_help_carry_result", lang)),
                Li(op("barcode:"), " ", t("shell.search_help_exact", lang), " ",
                   Code("barcode: 1042"), " ", t("shell.search_help_exact_result", lang)),
                Li(op("all:"), " ", t("shell.search_help_all", lang), " ",
                   Code("barcode: 1042, all: ring"), " ", t("shell.search_help_all_result", lang)),
                Li(op("Enter"), " ", t("shell.search_help_scan", lang), " ",
                   Code("barcode: "), " ", t("shell.search_help_scan_result", lang)),
                Li(op(t("inventory.search_not_found", lang, codes="1099")), " ",
                   t("shell.search_help_not_found", lang), " ",
                   Code("barcode: 1042, 1099"), " ", t("shell.search_help_not_found_result", lang)),
            ),
            Div(t("shell.search_help_example_label", lang),
                Div(Code("bolt & 5-10"), " ", t("shell.search_help_example_result", lang)),
                Div(Code("demantoid & ct_each: 1-2 & grade: 3"), " ",
                    t("shell.search_help_example_scoped_result", lang)),
                Div(Code("barcode: 1042, 1043, 1044, 1045, 1046"), " ",
                    t("shell.search_help_example_scan_result", lang)),
                cls="global-search-help-example"),
            id=panel_id,
            cls="global-search-help-panel popover-panel",
            role="dialog",
        ),
        cls="global-search-help-wrap",
    )


def _topbar(companies: list[dict], lang: str = "en", user_email: str | None = None, relay_info: dict | None = None, help_path: str | None = None) -> FT:
    """Top bar with hamburger toggle, global search, and optional company switcher."""
    parts: list[FT] = [
        Button(icon("menu", 1.25), cls="sidebar-toggle", type="button", aria_label=t("settings_menu.title", lang)),
        Div(
            Input(
                type="search",
                name="q",
                placeholder=t("msg.search_placeholder", lang),
                hx_get="/search",
                hx_trigger="keyup changed delay:300ms",
                hx_target="#global-search-results",
                hx_swap="innerHTML",
                hx_sync="this:replace",
                cls="global-search-input",
                autocomplete="off",
            ),
            search_help(lang),
            Div(id="global-search-results", cls="global-search-results"),
            cls="global-search-wrap",
        ),
    ]
    # Help for this page: lazy-loaded like the company switcher, for the page it sits on.
    parts.append(
        Div(
            id="topbar-help",
            hx_get="/help/for-page",
            hx_vals=json.dumps({"path": help_path}) if help_path else "js:{path: location.pathname + location.search}",
            hx_trigger="load",
            hx_swap="outerHTML",
            data_quiet_error="1",
        ),
    )
    # Company switcher — lazy-loaded so it appears on every page without
    # per-route my_companies() calls. Renders nothing when only one company.
    parts.append(
        Div(
            id="topbar-company-switcher",
            hx_get="/topbar-company-switcher",
            hx_trigger="load",
            hx_swap="outerHTML",
            data_quiet_error="1",
        ),
    )
    # Language switcher: searchable combobox showing each locale's native name.
    parts.append(
        Div(
            Span(icon("globe"), cls="lang-switcher__globe"),
            searchable_select(
                "lang",
                [(code, lang_label(code)) for code in available_langs()],
                value=lang,
                aria_label=t("label.language", lang),
            ),
            cls="lang-switcher-wrap",
        ),
    )
    # Notification bell
    parts.append(
        Div(
            Button(
                icon("bell", 1.1),
                Span("", id="notif-badge", cls="notif-badge", style="display:none;"),
                cls="notif-bell-btn",
                type="button",
                aria_label=t("msg.notifications", lang),
                onclick="toggleNotifPanel()",
            ),
            Div(
                Div(t("msg.notifications"), cls="notif-panel__header"),
                Div(id="notif-list", cls="notif-panel__list"),
                Button(t("btn.mark_all_read"), cls="notif-panel__mark-all", type="button",
                       onclick="markAllNotifRead()"),
                Div(
                    Div(
                        Span("", cls="update-card__version"),
                        Span("", cls="update-card__state"),
                        cls="update-card__info",
                    ),
                    Div(
                        # Progress bar (hidden until download starts)
                        Div(
                            Div(cls="update-card__progress-fill"),
                            cls="update-card__progress-bar",
                            style="display:none;",
                        ),
                        # Scrolling log (hidden until first log line)
                        Pre(cls="update-card__log", style="display:none;"),
                        # Shown when an update fails, linking that version's release page.
                        A(t("btn.download_installer"), href="https://github.com/celerp/celerp/releases/latest",
                          target="_blank", rel="noopener noreferrer", cls="update-card__installer-link",
                          style="display:none;"),
                        # Shown instead when the download failed its security check.
                        A(t("shell.update_get_help"), href="https://github.com/celerp/celerp/issues",
                          target="_blank", rel="noopener noreferrer", cls="update-card__help-link",
                          style="display:none;"),
                        Button(t("btn.check_for_updates"), cls="update-card__check-btn", type="button"),
                        Span("", cls="update-card__release"),
                        Button(t("btn.restart_to_install"), cls="update-card__restart-btn", type="button",
                               data_confirm=t("shell.update_restart_confirm"), style="display:none;"),
                        Label(
                            Input(type="checkbox", cls="update-card__auto-input"),
                            Span(t("shell.update_auto_label")),
                            cls="update-card__auto",
                            style="display:none;",
                        ),
                        Div(
                            Code("pip install --upgrade celerp", cls="update-card__cmd"),
                            Button(t("btn.copy"), cls="update-card__copy-btn", type="button"),
                            cls="update-card__upgrade-cmd",
                            # The pip command is shown for these reasons only.
                            data_reasons=" ".join(("unsupervised", *PIP_BLOCKERS)),
                            style="display:none;",
                        ),
                        A(t("msg.releases"), href="https://github.com/celerp/celerp/releases",
                          target="_blank", rel="noopener noreferrer", cls="update-card__releases-link"),
                        cls="update-card__actions",
                    ),
                    id="update-status-card",
                    cls="update-status-card",
                ),
                id="notif-panel",
                cls="notif-panel",
                style="display:none;",
            ),
            cls="notif-wrap",
        ),
    )
    # User email + relay indicator + logout dropdown
    if user_email:
        relay_known = relay_info is not None
        _ri = relay_info or {}
        connected = bool(_ri.get("connected"))
        public_url = _ri.get("public_url") or ""
        dot_cls = "relay-dot relay-dot--on" if connected else "relay-dot relay-dot--off"
        if connected and public_url:
            dot_title = f"{t('msg.relay_connected', lang)}: {public_url}"
            dot_href = public_url
            dot_target = "_blank"
        else:
            dot_title = t("msg.relay_not_connected", lang)
            dot_href = "/settings/cloud"
            dot_target = "_self"
        if relay_known:
            relay_dot = Span(
                A(Span(cls=dot_cls, title=dot_title), href=dot_href, target=dot_target,
                  cls="relay-dot-link", onclick="event.stopPropagation()"),
                id="relay-dot-wrap",
            )
        else:
            relay_dot = Span(
                Span(cls="relay-dot relay-dot--off", title=""),
                id="relay-dot-wrap",
                hx_get="/topbar-relay-status",
                hx_trigger="load",
                hx_swap="outerHTML",
                data_quiet_error="1",
            )
        parts.append(
            Div(
                # Email pill = dropdown trigger
                Div(
                    Span("", id="supporter-badge", cls="supporter-badge", title="",
                         style="display:none;margin-right:8px;color:#d4af37;font-size:12px"),
                    Span(user_email, cls="user-menu__email-text"),
                    cls="user-menu__trigger",
                    onclick="toggleUserMenu()",
                ),
                # Dropdown
                Div(
                    A(
                        _menu_icon("bug"), t("nav.report_bug", lang, default="Report a bug"),
                        href=_bug_report_url(),
                        target="_blank",
                        rel="noopener",
                        cls="user-menu__item",
                        # Append the current page (and the matching Area) to the bug form at click
                        # time — the path is only known client-side; see _BUG_LINK_JS.
                        onclick="this.href=celerpBugUrl(this.dataset.base)",
                        **{"data-base": _bug_report_url()},
                    ),
                    A(
                        _menu_icon("lightbulb"), t("nav.suggest_feature", lang, default="Suggest a feature"),
                        href="https://github.com/celerp/celerp/discussions/new?category=ideas",
                        target="_blank",
                        rel="noopener",
                        cls="user-menu__item",
                    ),
                    A(
                        _menu_icon("key"), t("nav.change_password", lang, default="Change password"),
                        href="/settings/general?tab=password",
                        cls="user-menu__item",
                    ),
                    A(
                        "⎋ " + t("nav.logout", lang),
                        href="/logout",
                        cls="user-menu__item user-menu__item--logout",
                        onclick="event.preventDefault();fetch('/logout',{method:'POST',credentials:'same-origin'}).then(()=>window.location='/login')",
                    ),
                    id="user-menu-panel",
                    cls="user-menu__panel",
                    style="display:none;",
                ),
                # Relay dot — separate element to the right of the email pill
                relay_dot,
                cls="user-menu",
            )
        )
    return Div(*parts, cls="topbar")


# Kernel-level nav entries always present (no module required)
_KERNEL_NAV: list[dict] = []

_BUG_LINK_JS = """
(function(){
  // Build the "Report a bug" URL at click time: prefill the form's `page` field with the
  // current path, and preselect the `area` dropdown from the route the user is on (the path
  // is only known client-side). Area values must match the bug form's dropdown options exactly.
  var AREA_ROUTES = [
    [/^\\/(inventory|items|lists)/, "Inventory"],
    [/^\\/docs/, "Invoicing / documents"],
    [/^\\/manufacturing/, "Manufacturing"],
    [/^\\/(accounting|finance|payments)/, "Accounting"],
    [/^\\/(contacts|crm)/, "Contacts / CRM"],
    [/^\\/(reports|dashboard|history)/, "Reporting"],
    [/^\\/(settings|setup|subscriptions)/, "Setup / admin / permissions"]
  ];
  window.celerpBugUrl = function(base) {
    var path = location.pathname;
    var url = base + "&page=" + encodeURIComponent(path + location.search);
    for (var i = 0; i < AREA_ROUTES.length; i++) {
      if (AREA_ROUTES[i][0].test(path)) { url += "&area=" + encodeURIComponent(AREA_ROUTES[i][1]); break; }
    }
    return url;
  };
})();
"""

_USER_MENU_JS = """
(function(){
  window.toggleUserMenu = function() {
    var panel = document.getElementById('user-menu-panel');
    if (!panel) return;
    var visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : '';
  };
  document.addEventListener('click', function(e) {
    var panel = document.getElementById('user-menu-panel');
    if (panel && !e.target.closest('.user-menu')) {
      panel.style.display = 'none';
    }
  });
})();
"""

_SIDEBAR_JS = """
(function(){
  var KEY = 'celerp_sidebar_groups';
  function saved() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch(e) { return {}; } }
  function persist(state) { localStorage.setItem(KEY, JSON.stringify(state)); }
  document.querySelectorAll('.sidebar-group').forEach(function(g) {
    var key = g.dataset.group;
    var state = saved();
    // Default open unless user explicitly closed (state[key] === false)
    if (state[key] !== false) {
      g.classList.add('sidebar-group--open');
    }
    // Arrow toggles open/close; settings link navigates normally
    var arrow = g.querySelector('.sidebar-group-arrow');
    if (arrow) {
      arrow.addEventListener('click', function(e) {
        e.stopPropagation();
        g.classList.toggle('sidebar-group--open');
        var s = saved(); s[key] = g.classList.contains('sidebar-group--open'); persist(s);
      });
    }
  });
  /* Mobile hamburger */
  var btn = document.querySelector('.sidebar-toggle');
  var sb = document.querySelector('.sidebar');
  if (btn && sb) btn.addEventListener('click', function() { sb.classList.toggle('sidebar--open'); });
  /* Bring the active nav item into view when its group sits below the fold on a short viewport.
     Adjusts only the sidebar's own scroll (never the window), and only when off-screen. */
  var activeLink = sb && sb.querySelector('.nav-link--active');
  if (sb && activeLink) {
    var lr = activeLink.getBoundingClientRect(), sr = sb.getBoundingClientRect();
    if (lr.top < sr.top || lr.bottom > sr.bottom) {
      sb.scrollTop += (lr.top - sr.top) - (sr.height - lr.height) / 2;
    }
  }
})();
"""


def _resolve_active_nav_key(active: str, all_items: list[dict], request=None) -> str:
    """Resolve the exact active sidebar key from the current URL.

    Falls back to the route-provided `active` key when the current request does not
    match a more specific nav item. This keeps route code DRY while allowing query-
    and path-sensitive sidebar entries like sold/archived inventory and reconcile.
    """
    if request is None:
        return active

    current_path = request.url.path.rstrip("/") or "/"
    current_query = request.url.query
    current_params = parse_qs(current_query, keep_blank_values=True)

    def _matches(item: dict) -> bool:
        href = str(item.get("href") or "").strip()
        if not href.startswith("/"):
            return False
        parsed = urlparse(href)
        item_path = parsed.path.rstrip("/") or "/"
        if current_path != item_path:
            return False
        item_params = parse_qs(parsed.query, keep_blank_values=True)
        for key, values in item_params.items():
            if current_params.get(key) != values:
                return False
        return True

    best_match = None
    best_score = (-1, -1)
    for item in all_items:
        if not _matches(item):
            continue
        parsed = urlparse(str(item.get("href") or ""))
        score = (len(parse_qs(parsed.query, keep_blank_values=True)), len(parsed.path))
        if score > best_score:
            best_match = item
            best_score = score

    return str(best_match.get("key") or active) if best_match else active


def module_nav(settings: dict | None) -> list[dict]:
    """Nav entries of the modules running in this process that the company uses
    (uses_module on its settings), in display order. Anything that shows or links
    to a module's pages asks this, so a module that is off leaves no trace."""
    from celerp.modules.registry import uses_module
    try:
        from celerp.modules.slots import get as get_slot
        slot_items: list[dict] = get_slot("nav")
    except Exception:
        slot_items = []
    return [item for item in sorted(slot_items + _KERNEL_NAV, key=lambda x: x.get("order", 99))
            if uses_module(settings, item.get("_module"))]


def module_active(settings: dict | None, name: str) -> bool:
    """Whether module ``name`` is running here and the company uses it."""
    return any(item.get("_module") == name for item in module_nav(settings))


def visible_nav(settings: dict | None, role: str) -> list[dict]:
    """The nav entries this role sees for the company, in display order: what its permissions allow,
    less what the company hides, in the order the company chose."""
    from ui.module_slots import slot_permission_allows
    from ui.nav_overrides import apply_nav_overrides
    settings = settings or {}
    permitted = [item for item in module_nav(settings) if slot_permission_allows(item, settings, role)]
    # The company's choices come last: they hide and reorder what the role may see, never add to it.
    return apply_nav_overrides(permitted, settings)


def landing_path(settings: dict | None, role: str) -> str:
    """Where signing in lands: the first page in the caller's menu, which is the dashboard
    where the company runs it and the role may see it. With nothing in the menu, Settings,
    which every role can open."""
    items = visible_nav(settings, role)
    first = next((item for item in items if not item.get("group")), None) or next(iter(items), None)
    return (first or {}).get("href") or "/settings/general"


def _sidebar(active: str, lang: str = "en", role: str = "owner", request=None, settings: dict | None = None) -> FT:
    """Build sidebar entirely from module nav slots + kernel entries."""
    from collections import defaultdict
    from celerp.services.permissions import role_has_permission

    settings = settings or {}

    # Drop what this company cannot see before deduplicating, not after. Two modules
    # may offer the same section: accounting and reports both declare "reports",
    # because the financial reports stay reachable when the reports module is off.
    # Deduplicating first lets a disabled module's entry win the key and then be
    # filtered out, taking the enabled module's entry with it and leaving the
    # section missing from the nav altogether.
    visible = visible_nav(settings, role)

    # Deduplicate by key (first occurrence wins - kernel entries are last, so module wins)
    seen_keys: set[str] = set()
    all_items = []
    for item in visible:
        k = item.get("key", "")
        if k and k in seen_keys:
            continue
        if k:
            seen_keys.add(k)
        all_items.append(item)

    active = _resolve_active_nav_key(active, all_items, request=request)

    # Separate top-level (group=None) from grouped items
    top_level = [item for item in all_items if not item.get("group")]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in all_items:
        if item.get("group"):
            grouped[item["group"]].append(item)

    # Determine active group for auto-expand
    active_group = next(
        (grp for grp, items in grouped.items() if any(i.get("key") == active for i in items)),
        "",
    )

    def _link(item: dict) -> FT:
        key = item.get("key", "")
        label_key = item.get("label_key")
        label = t(label_key, lang) if label_key else item.get("label", "")
        href = item.get("href", "#")
        is_active = key == active or (not key and href.strip("/") == active)
        if item.get("probe_href"):
            # Shown only when its probe answers with the link (slots.py, nav probe_href).
            return Div(hx_get=f"{item['probe_href']}?active={int(is_active)}", hx_trigger="load",
                       hx_swap="outerHTML")
        return A(label, href=href, cls=f"nav-link {'nav-link--active' if is_active else ''}")

    sections: list[FT] = [_link(item) for item in top_level]

    for group, items in grouped.items():
        group_key = group.lower().replace(" ", "_")
        is_active_group = group == active_group
        # A module's group is named by nav.group.<key>; a group no catalog names shows as declared.
        label_key = f"nav.group.{group_key}"
        group_label = t(label_key, lang)
        if group_label == label_key:
            group_label = group
        # Check if any item in the group declares a settings_href
        settings_href = next((i["settings_href"] for i in items if i.get("settings_href")), None)
        if settings_href:
            header = Div(
                A(_menu_icon("settings"), group_label, href=settings_href, cls="sidebar-group-settings-link"),
                Span("›", cls="sidebar-group-arrow"),
                cls="sidebar-group-header",
            )
        else:
            header = Div(
                Span(group_label),
                Span("›", cls="sidebar-group-arrow"),
                cls="sidebar-group-header",
            )
        sections.append(
            Div(
                header,
                Div(*[_link(i) for i in items], cls="sidebar-group-items"),
                cls=f"sidebar-group {'sidebar-group--active sidebar-group--open' if is_active_group else ''}",
                data_group=group_key,
            )
        )

    # Blank install: only kernel entries visible — show a helpful prompt
    from celerp.modules.slots import get as get_slot
    has_module_nav = bool(get_slot("nav"))
    if not has_module_nav:
        empty_state: list[FT] = [Div(
            P(t("msg.no_modules_installed"), cls="sidebar-empty-title"),
            P(t("msg.complete_the_setup_wizard_to_get_started"), cls="sidebar-empty-hint"),
            cls="sidebar-empty",
        )]
    else:
        empty_state = []

    # Company Details sits just above Global Config / Web Access in the footer. It is not module nav (the
    # company is the tenant, not a Finance feature), so it lives with the kernel bottom links rather than
    # in a module group. (Company files are a tab inside it, not a separate entry.) Admin+ only.
    settings_link = []
    if role_has_permission(settings, role, "manage_company_settings"):
        settings_link.append(
            A(t("nav.company_details", lang), href="/finance/company-details",
              title=t("nav.company_details_tip", lang),
              cls=f"nav-link {'nav-link--active' if active == 'company-details' else ''}"),
        )
    settings_link.append(
        A(t("nav.settings", lang), href="/settings/general",
          title=t("nav.settings_tip", lang),
          cls=f"nav-link {'nav-link--active' if active == 'settings' else ''}"),
    )
    # Modules is a top-level surface, not a settings tab: it sits between
    # Global Config and Web Access.
    if role_has_permission(settings, role, "manage_company_settings"):
        settings_link.append(
            A(t("nav.modules", lang), href="/modules",
              title=t("nav.modules_tip", lang),
              cls=f"nav-link {'nav-link--active' if active == 'modules' else ''}"),
        )
        settings_link.append(
            A(t("nav.doctor", lang), href="/doctor",
              title=t("nav.doctor_tip", lang),
              cls=f"nav-link {'nav-link--active' if active == 'doctor' else ''}"),
        )
    if role_has_permission(settings, role, "manage_integrations"):
        settings_link.append(
            A(t("msg._web_access", lang), href="/settings/cloud",
              title=t("nav.web_access_tip", lang),
              cls=f"nav-link {'nav-link--active' if active == 'web-access' else ''}"),
        )
    return Nav(
        Div(
            A(Img(src="/static/logo.png", alt="Celerp", cls="sidebar-logo"), href="/dashboard"),
            cls="sidebar-logo-wrap",
        ),
        *sections,
        *empty_state,
        Div(
            *settings_link,
            cls="sidebar-footer",
        ),
        Script(_SIDEBAR_JS),
        cls="sidebar",
    )


def auth_shell(*content, title: str = "Celerp") -> FT:
    """Minimal shell for login/register/setup pages."""
    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(title),
            *_favicon_links(),
            Link(rel="stylesheet", href=f"/static/app.css?v={_CSS_VER}"),
            Script(ESC_BLURS_FIELD),
        ),
        Body(
            Div(*content, cls="auth-container"),
            cls="auth-body",
        ),
    )


def sign_in_fields(email: str = "", *, autofocus: bool = False) -> tuple[FT, ...]:
    """The email and password fields every sign-in form uses, and the way to recover a
    password. The browser checks nothing: a field it refuses blocks the submit with no
    message the desktop app shows, so every check is the server's, which answers it."""
    return (
        Div(Label(t("label.email"), For="email", cls="form-label"),
            Input(type="text", inputmode="email", autocomplete="email", id="email", name="email", value=email,
                  placeholder=t("account.email_placeholder"), autofocus=autofocus, cls="form-input"),
            cls="form-group"),
        Div(Label(t("label.password"), For="password", cls="form-label"),
            Input(type="password", id="password", name="password", autocomplete="current-password",
                  placeholder="••••••••", cls="form-input"),
            cls="form-group"),
        P(A(t("auth.forgot_password"), href="/forgot-password", cls="auth-link"), cls="auth-footer-text"),
    )


def _menu_icon(name: str) -> FT:
    """An icon placed before the text of a menu link, level with it."""
    return Span(icon(name), cls="menu-icon")


def flash(msg: str, kind: str = "error", raw: bool = False) -> FT:
    content = NotStr(msg) if raw else msg
    return Div(content, cls=f"flash flash--{kind}", id="flash")


def secret_input(name: str, value: str = "", lang: str = "en", cls: str = "form-input", **attrs) -> FT:
    """A pasted secret (a copy or pairing link): masked, kept on a refused check so the
    user can look at it, and shown only when they click Show."""
    return Div(
        Input(type="password", id=name, name=name, value=value, autocomplete="off", cls=cls, **attrs),
        Button(t("btn.show", lang), type="button", cls="btn btn--secondary btn--sm",
               data_reveal=f"#{name}", data_show=t("btn.show", lang), data_hide=t("btn.hide", lang)),
        cls="secret-input",
    )


def toast_header(message: str, kind: str = "success", *, persist: bool = False,
                 action: dict | None = None, **extra_triggers) -> dict:
    """HX-Trigger header for the shell's celerpToast listener. ``action`` ({href, label})
    adds a link to the toast. Extra keyword arguments become sibling client events fired
    from the same header."""
    toast: dict = {"message": message, "type": kind}
    if persist:
        toast["persist"] = True
    if action:
        toast["action"] = action
    return {"HX-Trigger": json.dumps({"celerpToast": toast, **extra_triggers})}


# Reload the page once the server is reachable again after a restart. The first poll
# waits out the graceful shutdown so a still-dying server cannot answer and trigger a
# premature reload. On the desktop app the shell reloads the window itself when the
# servers respawn (possibly on a new port); this poll then simply never fires.
RESTART_POLL_JS = """
(function () {
  setTimeout(function () {
    var timer = setInterval(function () {
      fetch("/login", { cache: "no-store" }).then(function (r) {
        if (r.ok) { clearInterval(timer); window.location.href = "/"; }
      }).catch(function () { /* still restarting */ });
    }, 1500);
  }, 4000);
})();
"""


def spinner() -> FT:
    return Div(cls="spinner", id="spinner")


def load_error(message: str, lang: str = "en", *, href: str | None = None,
               hx_get: str | None = None, target: str | None = None) -> FT:
    """Recoverable read failure with its retry. Empty-state copy is reserved for successful reads."""
    if href:
        retry: FT = A(t("btn.retry", lang), href=href, cls="btn btn--secondary btn--sm")
    else:
        retry = Button(
            t("btn.retry", lang), type="button", cls="btn btn--secondary btn--sm",
            hx_get=hx_get, hx_target=target, hx_swap="innerHTML",
        )
    return Div(P(message, cls="load-error__text"), retry, cls="load-error")


def page_header(title: str, *actions: FT) -> FT:
    return Div(
        H1(title, cls="page-title"),
        Div(*actions, cls="page-actions"),
        cls="page-header",
    )
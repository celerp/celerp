# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from fasthtml.common import *

from ui.config import COOKIE_NAME, get_role
from ui.i18n import t, get_lang
from celerp.services.auth import ROLE_LEVELS

# Cache-bust static assets by hashing app.css content
def _css_version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "static", "app.css")
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except Exception:
        return "1"

_CSS_VER = _css_version()

# Discover available locales at startup
_LOCALE_LABELS: dict[str, str] = {
    "en": "English", "th": "ไทย", "zh": "中文", "ja": "日本語",
    "ko": "한국어", "es": "Español", "fr": "Français", "de": "Deutsch",
    "pt": "Português", "it": "Italiano", "nl": "Nederlands", "ru": "Русский",
    "ar": "العربية", "hi": "हिन्दी", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu", "tr": "Türkçe", "pl": "Polski", "sv": "Svenska",
}

def _available_locales() -> list[tuple[str, str]]:
    """Return [(code, label)] for all locales with a .json file, sorted by label."""
    locales_dir = Path(__file__).parent.parent / "locales"
    codes = sorted(
        p.stem for p in locales_dir.glob("*.json")
    )
    return [(c, _LOCALE_LABELS.get(c, c.upper())) for c in codes]

_LOCALES = _available_locales()

# Minimal client-side JS: Esc to cancel edit, row menu toggle, close menus on outside click, searchable combobox
_CLIENT_JS = """
function celerpToast(message, type) {
  var container = document.getElementById('toast-container');
  if (!container) { alert(message); return; }
  var toast = document.createElement('div');
  toast.className = 'toast toast--' + (type || 'error');
  toast.textContent = message;
  var close = document.createElement('button');
  close.className = 'toast__close';
  close.textContent = '×';
  close.onclick = function() { _dismissToast(toast); };
  toast.appendChild(close);
  container.appendChild(toast);
  requestAnimationFrame(function() { toast.classList.add('toast--visible'); });
  setTimeout(function() { _dismissToast(toast); }, 6000);
}
function _dismissToast(toast) {
  toast.classList.remove('toast--visible');
  setTimeout(function() { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 300);
}
document.addEventListener('htmx:afterRequest', function(e) {
  var hdr = e.detail.xhr && e.detail.xhr.getResponseHeader('HX-Trigger');
  if (!hdr) return;
  try {
    var obj = JSON.parse(hdr);
    if (obj.celerpToast) celerpToast(obj.celerpToast.message, obj.celerpToast.type || 'error');
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

function showGlobalUiError(message) {
  var box = document.getElementById('global-ui-error');
  if (!box) {
    alert(message);
    return;
  }
  box.textContent = message;
  box.style.display = 'block';
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    /* If any edit cell is open, let per-cell onkeydown handle the restore */
    if (document.querySelector('.cell--editing')) {
      return;
    }
    document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) { m.classList.remove('open'); });
    document.querySelectorAll('.combobox-list.open').forEach(function(l) { l.classList.remove('open'); });
  }
});
/* Select-based edit cells revert on blur via their own onblur handler (blur_restore_js in
   ui/components/table.py), which restores the cell inline. A previous global fallback here
   reloaded the whole page 300ms after a select blurred if the cell was still in edit mode - but
   under load the change->PATCH->swap commit takes longer than 300ms, so that timer fired
   mid-commit and reloaded the page, dropping the just-made edit (and any other unsaved state).
   The per-cell handler is sufficient, so no global reload is needed. */
function toggleRowMenu(id) {
  var menu = document.getElementById('menu-' + id);
  if (!menu) return;
  var wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) {
    m.classList.remove('open');
    m.style.cssText = '';
  });
  if (!wasOpen) {
    // Position fixed relative to button so table overflow: hidden can't clip it
    var btn = menu.previousElementSibling;
    if (btn) {
      var r = btn.getBoundingClientRect();
      // Anchor to right edge of button; flip upward if too close to bottom
      var menuH = 200; // estimated max height
      var spaceBelow = window.innerHeight - r.bottom;
      menu.style.position = 'fixed';
      menu.style.right = (window.innerWidth - r.right) + 'px';
      menu.style.left = 'auto';
      if (spaceBelow < menuH) {
        menu.style.bottom = (window.innerHeight - r.top) + 'px';
        menu.style.top = 'auto';
      } else {
        menu.style.top = r.bottom + 'px';
        menu.style.bottom = 'auto';
      }
      menu.style.zIndex = '9999';
    }
    menu.classList.add('open');
  }
}
document.addEventListener('click', function(e) {
  // data-copy button: copy sibling/target text to clipboard with "Copied!" feedback
  var copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) {
    var targetSel = copyBtn.getAttribute('data-copy');
    var text = targetSel
      ? (document.querySelector(targetSel) || {}).textContent || ''
      : copyBtn.getAttribute('data-copy-text') || '';
    var original = copyBtn.textContent;
    function _markCopied() {
      copyBtn.textContent = 'Copied!';
      setTimeout(function() { copyBtn.textContent = original; }, 2000);
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

  // position:fixed on the list so overflow:hidden/auto ancestors can't clip it.
  // We set top/left/width inline on open so it tracks the input position.
  list.style.position = 'fixed';
  list.style.zIndex = '9999';

  function positionList() {
    var r = input.getBoundingClientRect();
    list.style.top = (r.bottom + 2) + 'px';
    list.style.left = r.left + 'px';
    list.style.minWidth = r.width + 'px';
    list.style.width = 'auto';
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
    if (opts[next]) opts[next].classList.add('focused');
  }
  function selectOpt(opt) {
    var val = opt.dataset.value !== undefined ? opt.dataset.value : opt.textContent.trim();
    var label = opt.textContent.trim();
    // __new__:URL values trigger a redirect instead of a form submission
    if (val.indexOf('__new__:') === 0) {
      window.location = val.slice('__new__:'.length);
      return;
    }
    // Show human-readable label in the visible input; store actual value in hidden
    input.value = label;
    if (hidden) hidden.value = val;
    list.classList.remove('open');
    currentOpts().forEach(function(o) { o.classList.remove('focused'); });
    // Use htmx.trigger() — synthetic dispatchEvent is ignored by HTMX 2.x
    if (hidden && typeof htmx !== 'undefined') {
      htmx.trigger(hidden, 'change');
    } else if (hidden) {
      hidden.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }

  input.addEventListener('focus', function() {
    // Restore original static options so user sees full list on re-focus after a search
    if (isServerSearch && originalListHTML) {
      list.innerHTML = originalListHTML;
    }
    filterOpts('');
    positionList();
    list.classList.add('open');
  });
  input.addEventListener('input', function() {
    if (isServerSearch) {
      if (!input.value) {
        // User cleared the search — restore static options and show all
        list.innerHTML = originalListHTML;
        filterOpts('');
      }
      // Non-empty: HTMX fires the server request and swaps results into the list
      positionList();
      list.classList.add('open');
      if (hidden) hidden.value = '';
    } else {
      filterOpts(input.value);
      positionList();
      list.classList.add('open');
      if (hidden) hidden.value = allowCustom ? input.value : '';
    }
  });
  input.addEventListener('blur', function() {
    // Allow mousedown on option to fire first
    setTimeout(function() {
      list.classList.remove('open');
      // If allow-custom and user typed something not in list, commit typed value
      if (allowCustom && input.value && !hidden.value) {
        hidden.value = input.value;
        if (typeof htmx !== 'undefined') htmx.trigger(hidden, 'change');
        else hidden.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }, 150);
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') { moveFocus(1); e.preventDefault(); }
    if (e.key === 'ArrowUp') { moveFocus(-1); e.preventDefault(); }
    if (e.key === 'Enter') {
      var focused = list.querySelector('.combobox-option.focused');
      if (focused) { selectOpt(focused); e.preventDefault(); }
    }
    if (e.key === 'Escape') {
      list.classList.remove('open');
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

document.addEventListener('htmx:responseError', function(e) {
  var path = (e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath) || 'unknown request';
  var status = (e.detail && e.detail.xhr && e.detail.xhr.status) || 'error';
  showGlobalUiError('Request failed (' + status + '): ' + path);
});

document.addEventListener('htmx:sendError', function(e) {
  var path = (e.detail && e.detail.pathInfo && e.detail.pathInfo.requestPath) || 'unknown request';
  showGlobalUiError('Network error while loading: ' + path);
});

/* ── Image cell drag-and-drop ─────────────────────────────────────────── */
function initImageDropZones(root) {
  root.querySelectorAll('.cell--droppable').forEach(function(cell) {
    cell.addEventListener('click', function() {
      var input = document.getElementById('img-input-' + cell.dataset.entityId.replace(/:/g, '-'));
      if (input) input.click();
    });
    ['dragover', 'dragenter'].forEach(function(ev) {
      cell.addEventListener(ev, function(e) { e.preventDefault(); cell.classList.add('cell--drag-over'); });
    });
    ['dragleave', 'drop'].forEach(function(ev) {
      cell.addEventListener(ev, function(e) {
        e.preventDefault();
        cell.classList.remove('cell--drag-over');
        if (ev === 'drop' && e.dataTransfer.files.length) {
          var entityId = cell.dataset.entityId;
          var safeId = entityId.replace(/:/g, '-');
          var fd = new FormData();
          fd.append('file', e.dataTransfer.files[0]);
          htmx.ajax('POST', '/api/items/' + entityId + '/attachments', {
            target: '#img-cell-' + safeId,
            swap: 'outerHTML',
            values: fd,
          });
        }
      });
    });
  });
}
initImageDropZones(document);
document.addEventListener('htmx:afterSwap', function(e) {
  initImageDropZones(e.detail.elt);
});
"""


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

_GLOBAL_UI_ERROR_HTML = Div(
    "",
    id="global-ui-error",
    cls="flash flash--error",
    style="display:none;margin:12px 0;",
)

_TOAST_CONTAINER_HTML = Div(id="toast-container", cls="toast-container")

_NOTIFICATION_JS = """
window.toggleNotifPanel = function() {
  var p = document.getElementById('notif-panel');
  if (!p) return;
  var visible = p.style.display !== 'none';
  p.style.display = visible ? 'none' : '';
  if (!visible) {
    fetch('/notifications')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        var badge = document.getElementById('notif-badge');
        var list = document.getElementById('notif-list');
        if (badge) {
          if (data.unread_count > 0) {
            badge.textContent = data.unread_count > 99 ? '99+' : data.unread_count;
            badge.style.display = '';
          } else {
            badge.style.display = 'none';
          }
        }
        if (list) {
          if (data.items.length === 0) {
            list.innerHTML = '<div class="notif-panel__empty">No notifications</div>';
          } else {
            list.innerHTML = data.items.slice(0, 10).map(function(n) {
              return '<div class="notif-item' + (n.read ? '' : ' notif-item--unread') + '">'
                + '<div class="notif-item__title">' + n.title + '</div>'
                + '<div class="notif-item__body">' + n.body + '</div>'
                + '</div>';
            }).join('');
          }
        }
      })
      .catch(function() {});
  }
};

window.markAllNotifRead = function() {
  fetch('/notifications/read-all', { method: 'POST' })
    .then(function() {
      var p = document.getElementById('notif-panel');
      if (p && p.style.display !== 'none') {
        fetch('/notifications')
          .then(function(r) { return r.json(); })
          .then(function(data) {
            var badge = document.getElementById('notif-badge');
            var list = document.getElementById('notif-list');
            if (badge) badge.style.display = 'none';
            if (list) list.innerHTML = '<div class="notif-panel__empty">No notifications</div>';
          })
          .catch(function() {});
      }
    })
    .catch(function() {});
};

document.addEventListener('DOMContentLoaded', function() {
  var badge = document.getElementById('notif-badge');
  var panel = document.getElementById('notif-panel');
  var list = document.getElementById('notif-list');
  if (!badge || !panel) return;

  function updateBadge(count) {
    if (count > 0) {
      badge.textContent = count > 99 ? '99+' : count;
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
  }

  function loadNotifications() {
    fetch('/notifications')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        updateBadge(data.unread_count);
        if (list) {
          if (data.items.length === 0) {
            list.innerHTML = '<div class="notif-panel__empty">No notifications</div>';
          } else {
            list.innerHTML = data.items.slice(0, 10).map(function(n) {
              return '<div class="notif-item' + (n.read ? '' : ' notif-item--unread') + '">'
                + '<div class="notif-item__title">' + n.title + '</div>'
                + '<div class="notif-item__body">' + n.body + '</div>'
                + '</div>';
            }).join('');
          }
        }
      })
      .catch(function() {});
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
        loadNotifications();
        if (data.priority === 'high' && Notification.permission === 'granted') {
          new Notification(data.title, { body: data.body });
        }
      } catch(err) {}
    });

    es.addEventListener('evicted', function(e) {
      es.close();
      try {
        var data = JSON.parse(e.data || '{}');
        var by = data.by ? '&by=' + encodeURIComponent(data.by) : '';
        window.location.href = '/login?reason=evicted' + by;
      } catch(_) {
        window.location.href = '/login?reason=evicted';
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

  // Request browser notification permission
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    document.addEventListener('click', function requestPerm() {
      Notification.requestPermission();
      document.removeEventListener('click', requestPerm);
    }, { once: true });
  }

  /* ── Language switcher ────────────────────────────────────────────── */
  var sel = document.getElementById('lang-switcher');
  if (sel) {
    sel.addEventListener('change', function() {
      document.cookie = 'celerp_lang=' + sel.value + ';path=/;max-age=' + (86400 * 365) + ';samesite=lax';
      window.location.reload();
    });
  }

  /* ── Update status card ───────────────────────────────────────────── */
  (function initUpdateCard() {
    var card = document.getElementById('update-status-card');
    if (!card) return;

    var versionEl = card.querySelector('.update-card__version');
    var stateEl = card.querySelector('.update-card__state');
    var checkBtn = card.querySelector('.update-card__check-btn');
    var restartBtn = card.querySelector('.update-card__restart-btn');
    var progressBar = card.querySelector('.update-card__progress-bar');
    var progressFill = card.querySelector('.update-card__progress-fill');
    var logEl = card.querySelector('.update-card__log');

    function setState(text, showRestart) {
      if (stateEl) stateEl.textContent = text;
      if (restartBtn) restartBtn.style.display = showRestart ? '' : 'none';
    }

    function setCheckBtn(visible) {
      if (checkBtn) checkBtn.style.display = visible ? '' : 'none';
    }

    function appendLog(msg) {
      if (!logEl) return;
      logEl.style.display = '';
      // Append via text node (avoids re-reading/rewriting the full textContent string).
      // Cap at 200 lines to prevent unbounded DOM growth during long downloads.
      var lines = logEl.querySelectorAll('.log-line');
      if (lines.length >= 200) lines[0].remove();
      var line = document.createElement('span');
      line.className = 'log-line';
      line.textContent = (logEl.childNodes.length ? '\\n' : '') + msg;
      logEl.appendChild(line);
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

      setState('Up to date', false);

      // Log lines: always show — no isManualCheck gate.
      window.celerp.onUpdateLog(function(msg) { appendLog(msg); });

      window.celerp.onUpdateAvailable(function(info) {
        setState('Downloading v' + (info && info.version ? info.version : 'update') + '...', false);
        setCheckBtn(false);
        setProgress(0);
      });

      window.celerp.onDownloadProgress(function(progress) {
        var pct = progress && typeof progress.percent === 'number' ? progress.percent : 0;
        setProgress(pct);
        // Also update state text so user sees live percentage
        setState('Downloading... ' + Math.round(pct) + '%', false);
      });

      window.celerp.onUpdateNotAvailable(function() {
        setState('Up to date', false);
        appendLog('Already on the latest version.');
        resetToIdle();
      });

      window.celerp.onUpdateDownloaded(function(info) {
        var v = info && info.version ? info.version : 'update';
        setState('v' + v + ' ready to install', false);
        setProgress(100);
        setCheckBtn(false);
        if (restartBtn) restartBtn.style.display = '';
        appendLog('v' + v + ' downloaded. Click "Restart to Install" when ready.');
      });

      // Errors are always visible — never silently swallowed.
      window.celerp.onUpdateError(function(info) {
        var msg = info && info.message ? info.message : 'Unknown error';
        setState('Update check failed', false);
        appendLog('Error: ' + msg);
        resetToIdle();
      });

      if (checkBtn) {
        checkBtn.addEventListener('click', function() {
          setCheckBtn(false);
          setState('Checking...', false);
          if (logEl) { logEl.textContent = ''; logEl.style.display = 'none'; }
          window.celerp.checkForUpdates().catch(function() {
            setState('Update check failed', false);
            resetToIdle();
          });
        });
      }

      if (restartBtn) {
        restartBtn.addEventListener('click', function() {
          restartBtn.disabled = true;
          restartBtn.textContent = 'Restarting...';
          window.celerp.installUpdate();
        });
      }
    } else {
      // ── PyPI / pip path ───────────────────────────────────────────────
      // Electron-only controls are hidden; everything else (version label,
      // state text, check button, pip upgrade command) works identically.
      if (restartBtn) restartBtn.style.display = 'none';
      if (progressBar) progressBar.style.display = 'none';

      function runPyPICheck() {
        setCheckBtn(false);
        setState('Checking...', false);
        fetch('/health').then(function(r) { return r.json(); }).then(function(health) {
          var current = health.version || '';
          var isDev = current.indexOf('.dev') !== -1 || current.indexOf('+dev') !== -1 || current === '0.0.0+dev';
          if (versionEl) versionEl.textContent = isDev ? 'Development build' : (current ? 'v' + current : 'Unknown');
          if (isDev) { setState('Running from source - no updates', false); resetToIdle(); return; }
          return fetch('https://pypi.org/pypi/celerp/json').then(function(r) { return r.json(); }).then(function(pypi) {
            var latest = pypi.info && pypi.info.version ? pypi.info.version : null;
            if (!latest) { setState('Up to date', false); resetToIdle(); return; }
            if (latest !== current) {
              setState('Update available: v' + latest, false);
              var upgrade = card.querySelector('.update-card__upgrade-cmd');
              if (upgrade) upgrade.style.display = '';
            } else {
              setState('Up to date', false);
            }
            resetToIdle();
          });
        }).catch(function() {
          setState('Check failed', false);
          resetToIdle();
        });
      }

      if (checkBtn) {
        checkBtn.addEventListener('click', function() { runPyPICheck(); });
      }

      var copyBtn = card.querySelector('.update-card__copy-btn');
      if (copyBtn) {
        copyBtn.addEventListener('click', function() {
          navigator.clipboard.writeText('pip install --upgrade celerp').catch(function() {});
          copyBtn.textContent = 'Copied!';
          setTimeout(function() { copyBtn.textContent = 'Copy'; }, 2000);
        });
      }

      // Auto-check on load so the card shows a version immediately.
      runPyPICheck();
    }
  })();
});
"""

_STAR_CTA_JS = """
(function(){
  fetch('/stars/cta?medium=footer').then(function(r){return r.json()}).then(function(d){
    var el = document.getElementById('star-cta');
    if (!el || !d || !d.url) return;
    var label = '\\u2605 Star on GitHub';
    if (d.show_count && d.count != null) label = '\\u2605 ' + d.count;  // proof/momentum: star count
    el.textContent = label;
    el.href = d.url;
    // Explain the ask on hover; the relay's body carries the founding pitch.
    el.title = d.body || 'Celerp is open source \\u2014 a GitHub star helps other teams find us.';
    el.style.display = '';
  }).catch(function(){});
  fetch('/stars/badge').then(function(r){return r.json()}).then(function(d){
    var el = document.getElementById('supporter-badge');
    if (!el || !d || !d.badge) return;
    el.textContent = '\\u2605 ' + d.badge.label;
    el.title = 'Thank you for being ' + d.badge.label;
    el.style.display = '';
  }).catch(function(){});
})();
"""


def star_supporter_card(medium: str = "dashboard") -> FT:
    """The founding-supporter ask: a gold-bordered, dismissable card. Hydrates from
    /stars/cta and shows only in a non-neutral mode (relay reachable) and when not
    dismissed. Rendered on the dashboard (where setup lands) and the onboarding page."""
    js = (
        "(function(){"
        "fetch('/stars/cta?medium=" + medium + "').then(function(r){return r.json()}).then(function(d){"
        "if(!d||!d.url||d.dismissed||d.mode==='neutral')return;"
        "var card=document.getElementById('star-supporter-card');if(!card)return;"
        "var h=document.getElementById('star-card-headline');if(h)h.textContent=d.headline||'Support Celerp';"
        "var b=document.getElementById('star-card-body');if(b)b.textContent=d.body||'';"
        "var s=document.getElementById('star-card-star');if(s)s.href=d.url;"
        "card.style.display='';"
        "}).catch(function(){});"
        "var dz=document.getElementById('star-card-dismiss');"
        "if(dz)dz.addEventListener('click',function(){"
        "fetch('/stars/dismiss',{method:'POST'}).then(function(){"
        "var c=document.getElementById('star-supporter-card');if(c)c.style.display='none';});});"
        "})();"
    )
    return Div(
        Div(
            H3("", id="star-card-headline"),
            P("", id="star-card-body", cls="auth-subtitle"),
            Div(
                A("Star on GitHub", id="star-card-star", href="#", target="_blank", rel="noopener", cls="btn btn--primary"),
                A("Claim your badge", href="/stars/claim", cls="btn btn--secondary"),
                Button("Maybe later", id="star-card-dismiss", type="button", cls="btn btn--ghost"),
                cls="mt-md",
            ),
            id="star-supporter-card",
            style="display:none;margin:16px 0;padding:20px;border:1px solid #d4af37;border-radius:10px;text-align:center",
        ),
        Script(js),
    )


def base_shell(*content, title: str = "Celerp", nav_active: str = "", companies: list[dict] | None = None, extra_head: list | None = None, lang: str = "en", request=None) -> FT:
    """Outer chrome: sidebar nav + top header + content area."""
    from ui.config import get_user_email, get_relay_info
    role = get_role(request) if request is not None else "owner"
    user_email = get_user_email(request) if request is not None else None
    relay_info: dict = {}
    if request is not None:
        lang = get_lang(request)
    head_items = [
        Meta(charset="utf-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        Title(title),
        Link(rel="icon", type="image/png", href="/static/icon.png"),
        Link(rel="stylesheet", href=f"/static/app.css?v={_CSS_VER}"),
        Script(src="/static/htmx.min.js"),
        Script(_CLIENT_JS),
        Script(_HEALTH_BANNER_JS),
        Script(_NOTIFICATION_JS),
        Script(_USER_MENU_JS),
        Script(_STAR_CTA_JS),
    ]
    if extra_head:
        head_items.extend(extra_head)
    return Html(
        Head(*head_items),
        Body(
            Div(
                _sidebar(nav_active, lang=lang, role=role, request=request),
                Div(
                    _topbar(companies or [], lang=lang, user_email=user_email, relay_info=relay_info),
                    _HEALTH_BANNER_HTML,
                    _GLOBAL_UI_ERROR_HTML,
                    _TOAST_CONTAINER_HTML,
                    Main(*content, id="main-content", cls="main-content"),
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


_BUG_REPORT_URL: str | None = None


def _bug_report_url() -> str:
    """GitHub 'new issue' link that opens the bug_report.yml issue form with the
    Celerp version and host OS pre-filled (the two fields users most often leave out).

    Built once per process - version and OS don't change at runtime. Uses the form's
    field-prefill query params, so the form's own labels (bug, triage) still apply for
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


def _topbar(companies: list[dict], lang: str = "en", user_email: str | None = None, relay_info: dict | None = None) -> FT:
    """Top bar with hamburger toggle, global search, and optional company switcher."""
    parts: list[FT] = [
        Button("☰", cls="sidebar-toggle", type="button"),
        Div(
            Input(
                type="search",
                name="q",
                placeholder=t("msg.search_placeholder", lang),
                hx_get="/search",
                hx_trigger="keyup changed delay:300ms",
                hx_target="#global-search-results",
                hx_swap="innerHTML",
                cls="global-search-input",
                autocomplete="off",
            ),
            Div(id="global-search-results", cls="global-search-results"),
            cls="global-search-wrap",
        ),
    ]
    # Company switcher — lazy-loaded so it appears on every page without
    # per-route my_companies() calls. Renders nothing when only one company.
    parts.append(
        Div(
            id="topbar-company-switcher",
            hx_get="/topbar-company-switcher",
            hx_trigger="load",
            hx_swap="outerHTML",
        ),
    )
    # Language switcher
    parts.append(
        Div(
            Span("🌐", cls="lang-switcher__globe"),
            Select(
                *[Option(code.upper(), value=code, selected=(code == lang)) for code, _label in _LOCALES],
                id="lang-switcher",
                cls="lang-switcher",
                title="Language",
            ),
            cls="lang-switcher-wrap",
        ),
    )
    # Notification bell
    parts.append(
        Div(
            Button(
                "🔔",
                Span("", id="notif-badge", cls="notif-badge", style="display:none;"),
                cls="notif-bell-btn",
                type="button",
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
                        Button(t("btn.check_for_updates"), cls="update-card__check-btn", type="button"),
                        Button(t("btn.restart_to_install"), cls="update-card__restart-btn", type="button",
                               style="display:none;"),
                        Div(
                            Code("pip install --upgrade celerp", cls="update-card__cmd"),
                            Button(t("btn.copy"), cls="update-card__copy-btn", type="button"),
                            cls="update-card__upgrade-cmd",
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
                        "🐞 " + t("nav.report_bug", lang, default="Report a bug"),
                        href=_bug_report_url(),
                        target="_blank",
                        rel="noopener",
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
                Span(
                    Span(cls="relay-dot relay-dot--off", title=""),
                    id="relay-dot-wrap",
                    hx_get="/topbar-relay-status",
                    hx_trigger="load",
                    hx_swap="outerHTML",
                ),
                cls="user-menu",
            )
        )
    return Div(*parts, cls="topbar")


# Kernel-level nav entries always present (no module required)
_KERNEL_NAV: list[dict] = []

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


def _sidebar(active: str, lang: str = "en", role: str = "owner", request=None) -> FT:
    """Build sidebar entirely from module nav slots + kernel entries."""
    from collections import defaultdict
    from ui.config import get_enabled_modules

    user_level = ROLE_LEVELS.get(role, ROLE_LEVELS["owner"])
    enabled_modules = get_enabled_modules(request) if request else set()

    def _allowed(item: dict) -> bool:
        min_role = item.get("min_role", "viewer")
        return user_level >= ROLE_LEVELS.get(min_role, 1)

    def _module_enabled(item: dict) -> bool:
        """Kernel entries (no _module key) always show. Module entries only show
        if their module is in the company's enabled set, or if enabled set is empty
        (old JWT without modules claim - show everything as safe fallback)."""
        mod = item.get("_module")
        if mod is None:
            return True
        if not enabled_modules:
            return True
        return mod in enabled_modules

    # Collect all nav items from loaded modules
    try:
        from celerp.modules.slots import get as get_slot
        slot_items: list[dict] = get_slot("nav")
    except Exception:
        slot_items = []

    all_items_raw = sorted(slot_items + _KERNEL_NAV, key=lambda x: x.get("order", 99))
    # Deduplicate by key (first occurrence wins - kernel entries are last, so module wins)
    seen_keys: set[str] = set()
    all_items = []
    for item in all_items_raw:
        k = item.get("key", "")
        if k and k in seen_keys:
            continue
        if k:
            seen_keys.add(k)
        all_items.append(item)

    # Filter by role and enabled modules
    all_items = [item for item in all_items if _allowed(item) and _module_enabled(item)]
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
        return A(label, href=href, cls=f"nav-link {'nav-link--active' if is_active else ''}")

    sections: list[FT] = [_link(item) for item in top_level]

    for group_label, items in grouped.items():
        group_key = group_label.lower().replace(" ", "_")
        is_active_group = group_label == active_group
        # Check if any item in the group declares a settings_href
        settings_href = next((i["settings_href"] for i in items if i.get("settings_href")), None)
        if settings_href:
            header = Div(
                A("⚙️ " + group_label, href=settings_href, cls="sidebar-group-settings-link"),
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
    has_module_nav = bool(slot_items)
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
    if user_level >= ROLE_LEVELS["admin"]:
        settings_link.append(
            A(t("nav.company_details", lang), href="/finance/company-details",
              cls=f"nav-link {'nav-link--active' if active == 'company-details' else ''}"),
        )
    settings_link.append(
        A(t("nav.settings", lang), href="/settings/general", cls=f"nav-link {'nav-link--active' if active == 'settings' else ''}"),
    )
    if user_level >= ROLE_LEVELS["manager"]:
        settings_link.append(
            A(t("msg._web_access"), href="/settings/cloud", cls=f"nav-link {'nav-link--active' if active == 'web-access' else ''}"),
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
    """Minimal shell for login/register/setup/onboarding pages."""
    return Html(
        Head(
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Title(title),
            Link(rel="icon", type="image/png", href="/static/icon.png"),
            Link(rel="stylesheet", href=f"/static/app.css?v={_CSS_VER}"),
        ),
        Body(
            Div(*content, cls="auth-container"),
            cls="auth-body",
        ),
    )


def flash(msg: str, kind: str = "error", raw: bool = False) -> FT:
    content = NotStr(msg) if raw else msg
    return Div(content, cls=f"flash flash--{kind}", id="flash")


def spinner() -> FT:
    return Div(cls="spinner", id="spinner")


def page_header(title: str, *actions: FT) -> FT:
    return Div(
        H1(title, cls="page-title"),
        Div(*actions, cls="page-actions"),
        cls="page-header",
    )

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import hashlib
import os
from pathlib import Path

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
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    /* If any edit cell is open, reload to cancel */
    if (document.querySelector('.cell--editing')) {
      e.preventDefault();
      window.location.reload();
      return;
    }
    document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) { m.classList.remove('open'); });
    document.querySelectorAll('.combobox-list.open').forEach(function(l) { l.classList.remove('open'); });
  }
});
/* Revert select-based edit cells on blur (click-away without changing) */
document.addEventListener('focusout', function(e) {
  var el = e.target;
  if (el && el.tagName === 'SELECT' && el.closest('.cell--editing')) {
    /* Small delay to let change event fire first if value changed */
    setTimeout(function() {
      if (el.closest('.cell--editing')) { window.location.reload(); }
    }, 300);
  }
});
function toggleRowMenu(id) {
  var menu = document.getElementById('menu-' + id);
  if (!menu) return;
  var wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) { m.classList.remove('open'); });
  if (!wasOpen) menu.classList.add('open');
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.row-menu')) {
    document.querySelectorAll('.row-menu-dropdown.open').forEach(function(m) { m.classList.remove('open'); });
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
  var opts = Array.from(list.querySelectorAll('.combobox-option'));
  var allowCustom = wrap.dataset.allowCustom === 'true';

  input.addEventListener('focus', function() {
    filterOpts('');
    list.classList.add('open');
  });
  input.addEventListener('input', function() {
    filterOpts(input.value);
    list.classList.add('open');
    if (hidden) hidden.value = allowCustom ? input.value : '';
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
  opts.forEach(function(opt) {
    opt.addEventListener('mousedown', function(e) {
      e.preventDefault();
      selectOpt(opt);
    });
  });

  function filterOpts(q) {
    var lower = q.toLowerCase();
    var visible = 0;
    opts.forEach(function(opt) {
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
    var visible = opts.filter(function(o) { return o.style.display !== 'none'; });
    var cur = visible.findIndex(function(o) { return o.classList.contains('focused'); });
    opts.forEach(function(o) { o.classList.remove('focused'); });
    var next = cur + dir;
    if (next < 0) next = visible.length - 1;
    if (next >= visible.length) next = 0;
    if (visible[next]) visible[next].classList.add('focused');
  }
  function selectOpt(opt) {
    var val = opt.dataset.value !== undefined ? opt.dataset.value : opt.textContent.trim();
    var label = opt.textContent.trim();
    // Show human-readable label in the visible input; store actual value in hidden
    input.value = label;
    if (hidden) hidden.value = val;
    list.classList.remove('open');
    opts.forEach(function(o) { o.classList.remove('focused'); });
    // Use htmx.trigger() — synthetic dispatchEvent is ignored by HTMX 2.x
    if (hidden && typeof htmx !== 'undefined') {
      htmx.trigger(hidden, 'change');
    } else if (hidden) {
      hidden.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
}
document.querySelectorAll('.combobox-wrap').forEach(initCombobox);
/* Re-init after HTMX swaps — search from document to handle outerHTML swaps */
document.addEventListener('htmx:afterSettle', function(e) {
  var root = (e.detail.elt && e.detail.elt.isConnected) ? e.detail.elt : document;
  root.querySelectorAll('.combobox-wrap').forEach(initCombobox);
});

/* ── Image cell drag-and-drop ─────────────────────────────────────────── */
function initImageDropZones(root) {
  root.querySelectorAll('.cell--droppable').forEach(function(cell) {
    cell.addEventListener('click', function() {
      var input = document.getElementById('img-input-' + cell.dataset.entityId);
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
          var fd = new FormData();
          fd.append('file', e.dataTransfer.files[0]);
          htmx.ajax('POST', '/inventory/' + entityId + '/attachments', {
            target: '#img-cell-' + entityId,
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

_NOTIFICATION_JS = """
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

  window.toggleNotifPanel = function() {
    var visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : '';
    if (!visible) loadNotifications();
  };

  window.markAllNotifRead = function() {
    fetch('/notifications/read-all', { method: 'POST' })
      .then(function() { loadNotifications(); })
      .catch(function() {});
  };

  // Close panel on outside click
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.notif-wrap')) {
      panel.style.display = 'none';
    }
  });

  // SSE for real-time updates
  if (typeof EventSource !== 'undefined') {
    var es = new EventSource('/notifications/stream');
    es.onmessage = function(e) {
      try {
        var data = JSON.parse(e.data);
        if (data.type === 'notification') {
          loadNotifications();
          // Browser notification for high priority
          if (data.priority === 'high' && Notification.permission === 'granted') {
            new Notification(data.title, { body: data.body });
          }
        }
      } catch(err) {}
    };
    es.onerror = function() {
      // Auto-reconnect is built into EventSource
    };
  }

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
});
"""

def base_shell(*content, title: str = "Celerp", nav_active: str = "", companies: list[dict] | None = None, extra_head: list | None = None, lang: str = "en", request=None) -> FT:
    """Outer chrome: sidebar nav + top header + content area."""
    role = get_role(request) if request is not None else "owner"
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
    ]
    if extra_head:
        head_items.extend(extra_head)
    return Html(
        Head(*head_items),
        Body(
            Div(
                _sidebar(nav_active, lang=lang, role=role),
                Div(
                    _topbar(companies or [], lang=lang),
                    _HEALTH_BANNER_HTML,
                    Main(*content, id="main-content", cls="main-content"),
                    Footer(
                        A(t("msg.powered_by", lang), href="https://www.celerp.com", target="_blank",
                          cls="brand-footer-link"),
                        cls="brand-footer",
                    ),
                    cls="content-area",
                ),
                cls="app-shell",
            ),
            cls="app-body",
        ),
    )


def _topbar(companies: list[dict], lang: str = "en") -> FT:
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
    if len(companies) > 1:
        current = companies[0].get("company_name", "") if companies else ""
        parts.append(
            Div(
                Button(
                    current,
                    " ▾",
                    hx_get="/switch-company",
                    hx_target="#company-picker-panel",
                    hx_swap="innerHTML",
                    hx_trigger="click",
                    cls="company-switcher-btn",
                ),
                Div(id="company-picker-panel", cls="company-picker-panel"),
                cls="company-switcher",
            ),
        )
    # Language switcher (always present; globe + 2-letter codes like website)
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
                id="notif-panel",
                cls="notif-panel",
                style="display:none;",
            ),
            cls="notif-wrap",
        ),
    )
    return Div(*parts, cls="topbar")


# Kernel-level nav entries always present (no module required)
_KERNEL_NAV: list[dict] = []

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
})();
"""


def _sidebar(active: str, lang: str = "en", role: str = "owner") -> FT:
    """Build sidebar entirely from module nav slots + kernel entries."""
    from collections import defaultdict

    user_level = ROLE_LEVELS.get(role, ROLE_LEVELS["owner"])

    def _allowed(item: dict) -> bool:
        min_role = item.get("min_role", "viewer")
        return user_level >= ROLE_LEVELS.get(min_role, 1)

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

    # Filter by role
    all_items = [item for item in all_items if _allowed(item)]

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

    settings_link = [
        A(t("nav.settings", lang), href="/settings/general", cls=f"nav-link {'nav-link--active' if active == 'settings' else ''}"),
    ]
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
            A(t("nav.logout", lang), href="/logout", cls="nav-link nav-link--logout",
              onclick="event.preventDefault();fetch('/logout',{method:'POST',credentials:'same-origin'}).then(()=>window.location='/login')"),
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


def flash(msg: str, kind: str = "error") -> FT:
    return Div(msg, cls=f"flash flash--{kind}", id="flash")


def spinner() -> FT:
    return Div(cls="spinner", id="spinner")


def page_header(title: str, *actions: FT) -> FT:
    return Div(
        H1(title, cls="page-title"),
        Div(*actions, cls="page-actions"),
        cls="page-header",
    )

# Copyright (c) 2026 Data Universal Limited
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Phone number input component using intl-tel-input.

Single source of truth for phone editing across the app:
  - Contact detail inline edit
  - Company settings inline edit

Usage:
    from ui.components.phone import phone_input_td, phone_head_items
    # In the page shell: extra_head=phone_head_items()
    # In the edit handler: return phone_input_td(...)
"""

from __future__ import annotations

from fasthtml.common import *


# Static asset paths (served by Starlette StaticFiles at /static/...)
_CSS = "/static/vendor/intl-tel-input/intlTelInput.min.css"
_JS  = "/static/vendor/intl-tel-input/intlTelInputWithUtils.min.js"


def phone_head_items() -> list:
    """Return [Link, Script] head elements required by phone_input_td.

    Inject into base_shell via extra_head=phone_head_items() on pages that
    may render a phone edit cell.
    """
    return [
        Link(rel="stylesheet", href=_CSS),
        Script(src=_JS),
    ]


def phone_input_td(
    *,
    value: str,
    patch_url: str,
    cancel_url: str,
    field_id: str = "phone-input",
    target: str = "closest td",
) -> FT:
    """Inline-edit Td containing an intl-tel-input phone picker.

    The hidden input (id=f"{field_id}-value") always holds the full E.164
    number (e.g. "+66812345678") which is what gets PATCHed.

    Args:
        value:      current stored phone string (E.164 or empty).
        patch_url:  HTMX PATCH URL for the save button.
        cancel_url: HTMX GET URL for the cancel button.
        field_id:   base id for DOM elements (must be CSS-selector-safe).
        target:     HTMX swap target for both buttons.
    """
    from ui.i18n import t

    hidden_id  = f"{field_id}-value"
    input_id   = f"{field_id}-visible"
    wrapper_id = f"{field_id}-wrap"

    # JS: initialise intl-tel-input and keep hidden input in sync.
    # nationalMode:false  → always shows international format with +
    # separateDialCode:false → dial code is part of the number field (stored in E.164)
    # autoPlaceholder:"aggressive" → shows e.g. "+66 XX XXXX XXXX" as placeholder
    init_js = f"""
(function() {{
  var inputEl  = document.getElementById({input_id!r});
  var hiddenEl = document.getElementById({hidden_id!r});
  if (!inputEl || !hiddenEl || !window.intlTelInput) return;
  var iti = window.intlTelInput(inputEl, {{
    nationalMode: false,
    separateDialCode: false,
    autoPlaceholder: 'aggressive',
    formatOnDisplay: true,
    initialCountry: 'auto',
    geoIpLookup: function(cb) {{
      // Cheap: parse existing stored value if present, else default to TH
      var v = hiddenEl.value || '';
      if (v.startsWith('+66')) {{ cb('th'); return; }}
      if (v.startsWith('+1'))  {{ cb('us'); return; }}
      cb('th');
    }},
  }});
  // Sync hidden value on every change
  inputEl.addEventListener('input', function() {{
    hiddenEl.value = iti.getNumber() || '';
  }});
  inputEl.addEventListener('countrychange', function() {{
    hiddenEl.value = iti.getNumber() || '';
  }});
}})();
"""

    return Td(
        Div(
            Input(
                type="tel",
                id=input_id,
                value=value,
                cls="cell-input iti-input",
                autofocus=True,
                autocomplete="tel",
            ),
            id=wrapper_id,
            cls="iti-wrap",
        ),
        Input(type="hidden", id=hidden_id, name="value", value=value),
        Script(init_js),
        Button(
            t("btn.save"), type="button",
            hx_patch=patch_url,
            hx_target=target, hx_swap="outerHTML",
            hx_include=f"#{hidden_id}",
            cls="btn btn--primary btn--xs ml-sm",
        ),
        Button(
            t("btn.cancel"), type="button",
            hx_get=cancel_url,
            hx_target=target, hx_swap="outerHTML",
            cls="btn btn--secondary btn--xs ml-xs",
        ),
        cls="cell cell--editing",
    )

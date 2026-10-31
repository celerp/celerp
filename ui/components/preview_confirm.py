# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A form that confirms a preview saves exactly what the preview on screen showed.

The form carries FORM_ATTRS; inside it, the element that asks for the preview and shows its
answer carries PREVIEW_ATTRS (a newer preview request replaces one still on the way, so an older
answer never lands last). A preview answer holds the server's binding (binding_input), the
confirm sends it back (confirmed_binding), and the server refuses a binding that differs from
what it would write now. The page script (PREVIEW_CONFIRM_JS) keeps the form's submit button
disabled from any change until the preview for the values now entered answers with a binding.
"""
from __future__ import annotations

from fasthtml.common import Input

from ui.components.operation_key import FIELD as OPERATION_KEY_FIELD

BINDING = "binding"
FORM_ATTRS = {"data-confirms-preview": "true"}
PREVIEW_ATTRS = {"data-preview": "true", "hx-sync": "this:replace"}


def binding_input(preview: dict) -> list:
    """The hidden binding a preview answer carries, or nothing when it has none."""
    return [Input(type="hidden", name=BINDING, value=preview[BINDING])] if preview.get(BINDING) else []


def confirmed_binding(form, wait: str) -> str:
    """The binding of the preview a submitted form confirms; ``wait`` is the refusal when it
    sent none (it was submitted before its preview answered)."""
    binding = str(form.get(BINDING) or "").strip()
    if not binding:
        raise ValueError(wait)
    return binding


PREVIEW_CONFIRM_JS = f"""
(function(){{
  function formOf(el){{ return el && el.closest ? el.closest('form[data-confirms-preview]') : null; }}
  // The values a preview is worked out for: the form without the binding and the operation key.
  function ask(form){{
    var f=new FormData(form); f.delete({BINDING!r}); f.delete({OPERATION_KEY_FIELD!r});
    return new URLSearchParams(f).toString();
  }}
  function allow(form,on){{ form.querySelectorAll('button[type=submit]').forEach(function(b){{ b.disabled=!on; }}); }}
  function hold(form){{
    if(ask(form)===form._celerpShown) return;
    form._celerpShown=null;
    allow(form,false);
    form.querySelectorAll('[data-preview] input[name={BINDING}]').forEach(function(el){{ el.remove(); }});
  }}
  function preview(e){{
    var el=e.detail.elt, form=formOf(el);
    return form && el.matches('[data-preview]') ? form : null;
  }}
  ['input','change'].forEach(function(type){{
    document.addEventListener(type,function(e){{ var form=formOf(e.target); if(form) hold(form); }});
  }});
  document.addEventListener('htmx:beforeRequest',function(e){{
    var form=preview(e); if(!form) return;
    e.detail.elt._celerpAsk=ask(form);
    hold(form);
  }});
  document.addEventListener('htmx:afterSwap',function(e){{
    var form=preview(e); if(!form) return;
    var el=e.detail.elt, current=el._celerpAsk===ask(form)&&!!el.querySelector('input[name={BINDING}]');
    form._celerpShown=current?el._celerpAsk:null;
    allow(form,current);
  }});
}})();
"""

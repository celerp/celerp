# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Share-flow fallback page. The live public document view renders through
celerp.output.doc_print (the same layout as the Print button); this module
only serves the dead-link page for revoked, expired, or missing documents."""
from __future__ import annotations

from fasthtml.common import A, Body, Div, H1, Head, Html, Meta, P, Style, Title

from celerp.output.branding import BRAND_TEXT, brand_url
from ui.components.encode import safe_url

_STYLE = """
    body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f9f9f9;padding:24px 16px;}
    .box{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);text-align:center;padding:40px 36px;max-width:420px;width:100%;}
    .box h1{font-size:20px;margin:0 0 8px;}
    .box p{color:#6b7280;font-size:15px;line-height:1.5;margin:0 0 28px;}
    .brand{border-top:1px solid #eee;padding-top:14px;font-size:12px;}
    .brand a{color:#9ca3af;text-decoration:none;}
    .brand a:hover{text-decoration:underline;}
"""


def _not_found_page(reason: str = "not-found"):
    """Dead-link page. The visitor came for a document that is gone; the only
    useful action is contacting the sender, so no sales button - just the
    same quiet brand line the live pages carry."""
    messages = {
        "link-expired": ("Link no longer active", "This share link has been revoked or has expired."),
        "doc-missing": ("Document unavailable", "The document this link pointed to no longer exists on the sender's side."),
        "not-found": ("Document not found", "This link may be incorrect, or the sender may be offline."),
    }
    title, body = messages.get(reason, messages["not-found"])
    return Html(
        Head(
            Meta(charset="UTF-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1.0"),
            Title(title),
            Style(_STYLE),
        ),
        Body(Div(
            H1(title),
            P(f"{body} Contact the sender for a current copy."),
            Div(A(BRAND_TEXT, href=safe_url(brand_url("share-page")), target="_blank", rel="noopener"),
                cls="brand"),
            cls="box",
        )),
        lang="en",
    )

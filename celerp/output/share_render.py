# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Share-flow fallback page. The live public document view renders through
celerp.output.doc_print (the same layout as the Print button); this module
only serves the dead-link page for revoked, expired, or missing documents."""
from __future__ import annotations

import html


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _not_found_page(reason: str = "not-found") -> str:
    """Dead-link page. The visitor came for a document that is gone; the only
    useful action is contacting the sender, so no sales button - just the
    same quiet brand line the live pages carry."""
    messages = {
        "link-expired": ("Link no longer active", "This share link has been revoked or has expired."),
        "doc-missing": ("Document unavailable", "The document this link pointed to no longer exists on the sender's side."),
        "not-found": ("Document not found", "This link may be incorrect, or the sender may be offline."),
    }
    title, body = messages.get(reason, messages["not-found"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(title)}</title>
  <style>
    body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f9f9f9;padding:24px 16px;}}
    .box{{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);text-align:center;padding:40px 36px;max-width:420px;width:100%;}}
    .box h1{{font-size:20px;margin:0 0 8px;}}
    .box p{{color:#6b7280;font-size:15px;line-height:1.5;margin:0 0 28px;}}
    .brand{{border-top:1px solid #eee;padding-top:14px;font-size:12px;}}
    .brand a{{color:#9ca3af;text-decoration:none;}}
    .brand a:hover{{text-decoration:underline;}}
  </style>
</head>
<body>
  <div class="box">
    <h1>{_esc(title)}</h1>
    <p>{_esc(body)} Contact the sender for a current copy.</p>
    <div class="brand"><a href="https://www.celerp.com" target="_blank" rel="noopener">Powered by Celerp.com - Downloadable ERP for Serious Businesses</a></div>
  </div>
</body>
</html>"""

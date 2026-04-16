#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Audit script: scan FastHTML source files for hardcoded English strings
that should be wrapped in t() calls.

Usage:
    python scripts/extract_i18n.py            # human-readable report
    python scripts/extract_i18n.py --json     # machine-readable JSON
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# FastHTML element names whose first positional arg is typically user-visible text
_ELEMENTS = {
    "Div", "H1", "H2", "H3", "H4", "P", "Li", "Span", "Label", "Button",
    "A", "Small", "Td", "Th", "Strong", "Em", "Option", "Input", "Title",
    "Legend", "Summary", "Details", "Figcaption", "Nav", "Section",
    "Footer", "Header",
}

# Strings to exclude: CSS classes, HTML attributes, URLs, short tokens, etc.
_EXCLUDE_PATTERNS = [
    re.compile(r"^https?://"),
    re.compile(r"^/[a-z]"),           # URL paths like /settings
    re.compile(r"^#"),                # anchors / colour codes
    re.compile(r"^\w+[-_]\w+$"),      # CSS class-like: "nav-link", "page_header"
    re.compile(r"^\s*$"),             # blank
    re.compile(r"^[A-Z_]{2,}$"),      # constants like "GET", "POST"
]

# Keywords that hint an argument is an HTML attribute rather than content
_ATTR_KWARG_NAMES = {
    "cls", "id", "type", "name", "method", "action", "href", "src",
    "hx_get", "hx_post", "hx_put", "hx_delete", "hx_target", "hx_swap",
    "hx_trigger", "hx_vals", "hx_confirm", "hx_include", "hx_push_url",
    "hx_indicator", "hx_encoding", "hx_ext",
    "data_group", "data_id", "data_value", "data_entity_id",
    "autocomplete", "placeholder", "style", "role", "aria_label",
    "value", "for_", "min", "max", "step", "pattern", "title",
    "target", "rel", "width", "height", "alt", "onclick",
    "onerror", "onchange", "onsubmit",
}

# Namespace inference from file path
def _infer_namespace(filepath: str) -> str:
    p = filepath.lower()
    if "setup" in p:
        return "setup"
    if "settings" in p or "config" in p:
        return "settings"
    if "auth" in p or "login" in p:
        return "auth"
    if "dashboard" in p:
        return "page"
    if "accounting" in p:
        return "acct"
    if "manufacturing" in p:
        return "mfg"
    if "inventory" in p:
        return "inv"
    if "report" in p:
        return "rpt"
    if "document" in p or "docs" in p:
        return "doc"
    if "contact" in p or "crm" in p:
        return "label"
    if "subscription" in p:
        return "label"
    if "component" in p:
        return "msg"
    return "msg"


def _to_snake(text: str) -> str:
    """Convert English text to a snake_case key fragment."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s[:40].rstrip("_")


def _is_translatable(s: str) -> bool:
    """Heuristic: string looks like user-visible English text."""
    if not s or len(s.strip()) < 2:
        return False
    for pat in _EXCLUDE_PATTERNS:
        if pat.match(s.strip()):
            return False
    # Must contain at least one letter
    if not re.search(r"[a-zA-Z]", s):
        return False
    # Skip if looks like a CSS class chain (multiple hyphen-words)
    if re.match(r"^[\w-]+\s+[\w-]+$", s.strip()) and " " in s and all(
        re.match(r"^[\w-]+$", w) for w in s.split()
    ):
        # Could be CSS classes like "kpi-grid kpi-grid--secondary"
        if "-" in s or "--" in s:
            return False
    return True


def _scan_file(filepath: Path) -> list[dict]:
    """Parse a Python file and find hardcoded strings in FastHTML elements."""
    try:
        source = filepath.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []

    results = []
    rel = str(filepath.relative_to(ROOT))
    ns = _infer_namespace(rel)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Get function name
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name not in _ELEMENTS:
            continue

        # Check first positional argument
        if not node.args:
            continue
        first_arg = node.args[0]

        # Skip t() calls
        if isinstance(first_arg, ast.Call):
            fn = first_arg.func
            if (isinstance(fn, ast.Name) and fn.id == "t") or \
               (isinstance(fn, ast.Attribute) and fn.attr == "t"):
                continue

        # Only handle string literals (Constant with str value)
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            text = first_arg.value
            if _is_translatable(text):
                key = f"{ns}.{_to_snake(text)}"
                results.append({
                    "file": rel,
                    "line": first_arg.lineno,
                    "text": text,
                    "suggested_key": key,
                    "element": func_name,
                })

        # Handle f-strings with mixed text+variables (JoinedStr)
        elif isinstance(first_arg, ast.JoinedStr):
            # Reconstruct template to check for English text
            parts = []
            has_text = False
            for v in first_arg.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    if v.value.strip():
                        has_text = True
                    parts.append(v.value)
                else:
                    parts.append("{...}")
            template = "".join(parts)
            if has_text and _is_translatable(template):
                key = f"{ns}.{_to_snake(template)}"
                results.append({
                    "file": rel,
                    "line": first_arg.lineno,
                    "text": template,
                    "suggested_key": key,
                    "element": func_name,
                    "is_fstring": True,
                })

    return results


def main():
    scan_paths = [
        ROOT / "ui" / "routes",
        ROOT / "ui" / "components",
    ]
    # Also scan default_modules
    for mod_dir in sorted((ROOT / "default_modules").glob("*/celerp_*")):
        scan_paths.append(mod_dir)

    all_results = []
    for scan_dir in scan_paths:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            all_results.extend(_scan_file(py_file))

    if "--json" in sys.argv:
        json.dump(all_results, sys.stdout, indent=2)
    else:
        print(f"Found {len(all_results)} hardcoded strings:\n")
        for r in all_results:
            fstr = " [f-string]" if r.get("is_fstring") else ""
            print(f"  {r['file']}:{r['line']}: {r['element']}(\"{r['text']}\"){fstr}")
            print(f"    → {r['suggested_key']}")
        print(f"\nTotal: {len(all_results)}")


if __name__ == "__main__":
    main()

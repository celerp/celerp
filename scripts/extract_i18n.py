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

# Attribute kwargs that DO carry user-visible text (tooltips, placeholders,
# confirm prompts, accessibility labels). These are checked for bare strings the
# same way element content is - the value must go through t() at render time.
_USER_FACING_ATTRS = {"placeholder", "title", "aria_label", "hx_confirm"}

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
    except (UnicodeDecodeError, OSError):
        return []
    return _scan_source(source, str(filepath.relative_to(ROOT)))


def _scan_source(source: str, rel: str) -> list[dict]:
    """Find hardcoded user-facing strings in one module's source text.

    Split out from _scan_file so the AST logic can be exercised on inline
    snippets without touching the filesystem.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results = []
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
        # Positional element content (Div/Button/Th/Option/...): first arg.
        if func_name in _ELEMENTS and node.args:
            hit = _string_hit(node.args[0])
            if hit:
                text, is_fstring = hit
                results.append({
                    "file": rel,
                    "line": node.args[0].lineno,
                    "text": text,
                    "suggested_key": f"{ns}.{_to_snake(text)}",
                    "element": func_name,
                    "attr": None,
                    **({"is_fstring": True} if is_fstring else {}),
                })

        # User-facing attribute kwargs (placeholder/title/aria_label/hx_confirm)
        # on any component-style call (FastHTML elements are Capitalized).
        if func_name and func_name[:1].isupper():
            for kw in node.keywords:
                if kw.arg not in _USER_FACING_ATTRS:
                    continue
                hit = _string_hit(kw.value)
                if hit:
                    text, is_fstring = hit
                    results.append({
                        "file": rel,
                        "line": kw.value.lineno,
                        "text": text,
                        "suggested_key": f"{ns}.{_to_snake(text)}",
                        "element": func_name,
                        "attr": kw.arg,
                        **({"is_fstring": True} if is_fstring else {}),
                    })

    return results


def _string_hit(arg: ast.expr) -> tuple[str, bool] | None:
    """Return (text, is_fstring) if this arg is a bare translatable string, else None.

    A ``t(...)`` / ``page_title(...)`` call is already routed through the
    translation layer, so it is never a hit; an f-string with static English
    text between its interpolations is (the static text needs translating).
    """
    # Already translated: t(...) or page_title(...) wrapping.
    if isinstance(arg, ast.Call):
        fn = arg.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if name in {"t", "page_title"}:
            return None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return (arg.value, False) if _is_translatable(arg.value) else None
    if isinstance(arg, ast.JoinedStr):
        parts, has_text = [], False
        for v in arg.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if v.value.strip():
                    has_text = True
                parts.append(v.value)
            else:
                parts.append("{...}")
        template = "".join(parts)
        if has_text and _is_translatable(template):
            return template, True
    return None


# The guardrail scope: the app's own rendered UI. default_modules ship their own
# copy and are audited separately, so the permanent regression gate covers ui/.
UI_SCAN_PATHS = [ROOT / "ui" / "routes", ROOT / "ui" / "components"]

# Known strings that are legitimately NOT translated (format hints, example
# values, credentials-shaped samples, brand names, unit abbreviations, magic
# confirm tokens). Frozen so no NEW hardcoded UI string can be added without
# either routing it through t() or making an explicit, reasoned exception here.
ALLOWLIST_PATH = ROOT / "scripts" / "i18n_allowlist.json"


def _load_allowlist() -> set[tuple[str, str]]:
    """Return the set of allowlisted (file, text) pairs; empty if none on disk."""
    if not ALLOWLIST_PATH.exists():
        return set()
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {(e["file"], e["text"]) for e in data["entries"]}


def scan(paths: list[Path] | None = None, apply_allowlist: bool = False) -> list[dict]:
    """Scan the given directories for hardcoded user-facing strings.

    paths defaults to the whole audit surface (ui/ + default_modules). Pass
    UI_SCAN_PATHS for the permanent guardrail scope. With apply_allowlist,
    (file, text) pairs recorded in i18n_allowlist.json are removed, leaving only
    NEW, un-triaged strings.
    """
    if paths is None:
        paths = list(UI_SCAN_PATHS)
        paths += sorted((ROOT / "default_modules").glob("*/celerp_*"))
    results: list[dict] = []
    for scan_dir in paths:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            results.extend(_scan_file(py_file))
    if apply_allowlist:
        allowed = _load_allowlist()
        results = [r for r in results if (r["file"], r["text"]) not in allowed]
    return results


def main():
    check = "--check" in sys.argv
    # --check gates only the guardrail scope; the plain report covers everything.
    all_results = scan(UI_SCAN_PATHS if check else None, apply_allowlist=check)

    if "--json" in sys.argv:
        json.dump(all_results, sys.stdout, indent=2)
        return
    if check:
        if all_results:
            print(f"{len(all_results)} NEW hardcoded UI string(s) not in the allowlist:\n")
            for r in all_results:
                attr = f" {r['attr']}=" if r.get("attr") else " "
                print(f"  {r['file']}:{r['line']}: {r['element']}({attr}\"{r['text']}\")")
                print(f"    route it through t(), or add a reasoned exception to {ALLOWLIST_PATH.name}")
            sys.exit(1)
        print("No new hardcoded UI strings: every user-facing string routes through t().")
        return
    print(f"Found {len(all_results)} hardcoded strings:\n")
    for r in all_results:
        fstr = " [f-string]" if r.get("is_fstring") else ""
        attr = f" {r['attr']}=" if r.get("attr") else " "
        print(f"  {r['file']}:{r['line']}: {r['element']}({attr}\"{r['text']}\"){fstr}")
        print(f"    → {r['suggested_key']}")
    print(f"\nTotal: {len(all_results)}")


if __name__ == "__main__":
    main()

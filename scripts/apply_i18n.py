#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Regex-based i18n string replacement for FastHTML source files.
Replaces Element("English text", ...) with Element(t("key", lang), ...)
Only replaces single-line string arguments to avoid breaking multi-line concatenations.

Usage:
    python scripts/apply_i18n.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_ELEMENTS = (
    "Div|H1|H2|H3|H4|P|Li|Span|Label|Button|A|Small|Td|Th|Strong|Em|"
    "Option|Title|Legend|Summary|Details|Figcaption"
)

_EXCLUDE_RE = [
    re.compile(r"^https?://"),
    re.compile(r"^/[a-z]"),
    re.compile(r"^#"),
    re.compile(r"^\s*$"),
    re.compile(r"^[A-Z_]{2,}$"),
]


def _is_translatable(s: str) -> bool:
    if not s or len(s.strip()) < 2:
        return False
    for pat in _EXCLUDE_RE:
        if pat.match(s.strip()):
            return False
    if not re.search(r"[a-zA-Z]", s):
        return False
    parts = s.strip().split()
    if len(parts) >= 2 and all(re.match(r"^[\w-]+$", p) for p in parts):
        if any("-" in p or "--" in p for p in parts):
            return False
    if len(parts) == 1 and re.match(r"^[\w]+-[\w-]+$", s.strip()):
        return False
    if re.match(r"^[a-z_]+$", s.strip()) and " " not in s:
        return False
    return True


def _infer_namespace(filepath: str) -> str:
    p = filepath.lower()
    if "setup" in p: return "setup"
    if "settings_accounting" in p: return "acct"
    if "settings_cloud" in p: return "settings"
    if "settings_contacts" in p: return "label"
    if "settings_general" in p: return "settings"
    if "settings_inventory" in p: return "inv"
    if "settings_purchasing" in p: return "settings"
    if "settings_sales" in p: return "settings"
    if "settings_import" in p: return "settings"
    if "settings" in p: return "settings"
    if "auth" in p: return "auth"
    if "dashboard" in p: return "page"
    if "accounting" in p or "reconcil" in p: return "acct"
    if "manufacturing" in p: return "mfg"
    if "inventory" in p or "scanning" in p: return "inv"
    if "report" in p: return "rpt"
    if "document" in p or "docs" in p or "_ui_doc" in p: return "doc"
    if "contact" in p or "crm" in p: return "label"
    if "subscription" in p: return "label"
    if "csv_import" in p or "import" in p: return "msg"
    if "label" in p: return "label"
    if "ai" in p: return "msg"
    if "backup" in p: return "settings"
    if "component" in p: return "msg"
    return "msg"


def _to_snake(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s[:50].rstrip("_")


_KNOWN_KEYS: dict[str, str] = {}


def _suggest_key(text: str, ns: str, element: str) -> str:
    t_str = text.strip()
    if t_str in _KNOWN_KEYS:
        return _KNOWN_KEYS[t_str]
    if element in ("H1", "H2", "H3", "H4"):
        prefix = "page"
    elif element == "Th":
        prefix = "th"
    elif element == "Button":
        prefix = "btn"
    elif element == "Label":
        prefix = "label"
    elif element == "Title":
        prefix = "page"
    else:
        prefix = ns
    if t_str.startswith("\u2190") or t_str.startswith("\u2190 "):
        prefix = "btn"
    snake = _to_snake(t_str)
    if not snake:
        snake = "unnamed"
    key = f"{prefix}.{snake}"
    _KNOWN_KEYS[t_str] = key
    return key


# Pattern: Element("string"[, ...]) or Element("string"[)...]
# The key insight: after the closing quote, there must be , or ) on the SAME segment
# This avoids matching multi-line implicit concatenation
_PATTERN = re.compile(
    r'\b(' + _ELEMENTS + r')\(\s*"((?:[^"\\]|\\.)*)"(?=\s*[,)])'
    r'|'
    r"\b(" + _ELEMENTS + r")\(\s*'((?:[^'\\]|\\.)*)'(?=\s*[,)])"
)


def _is_inside_t_call(source: str, match_start: int) -> bool:
    """Check if the match is inside an existing t() call."""
    # Look backwards for t( that hasn't been closed
    depth = 0
    i = match_start - 1
    while i >= 0:
        c = source[i]
        if c == ')':
            depth += 1
        elif c == '(':
            if depth > 0:
                depth -= 1
            else:
                # Unmatched ( - check if preceded by 't'
                if i > 0 and source[i-1] == 't':
                    # Check it's actually the function t(
                    if i < 2 or not source[i-2].isalnum():
                        return True
                break
        i -= 1
    return False


def process_file(filepath: Path, new_keys: dict, dry_run: bool = False) -> int:
    source = filepath.read_text()
    rel = str(filepath.relative_to(ROOT))
    ns = _infer_namespace(rel)

    count = 0

    def _replacer(m: re.Match) -> str:
        nonlocal count

        if m.group(1):
            elem, text = m.group(1), m.group(2)
        else:
            elem, text = m.group(3), m.group(4)

        actual_text = text.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n")

        if not _is_translatable(actual_text):
            return m.group(0)

        if _is_inside_t_call(source, m.start()):
            return m.group(0)

        key = _suggest_key(actual_text, ns, elem)
        new_keys[key] = actual_text
        count += 1
        return f'{elem}(t("{key}")'

    if dry_run:
        for m in _PATTERN.finditer(source):
            if m.group(1):
                text = m.group(2).replace('\\"', '"').replace("\\n", "\n")
                elem = m.group(1)
            else:
                text = m.group(4).replace("\\'", "'").replace("\\n", "\n")
                elem = m.group(3)
            if _is_translatable(text) and not _is_inside_t_call(source, m.start()):
                key = _suggest_key(text, ns, elem)
                new_keys[key] = text
                count += 1
        return count

    new_source = _PATTERN.sub(_replacer, source)
    if count > 0:
        filepath.write_text(new_source)
    return count


def ensure_lang_in_handlers(filepath: Path):
    """Insert lang = get_lang(request) in handlers that use lang but don't define it."""
    source = filepath.read_text()
    if "lang" not in source:
        return

    lines = source.split("\n")
    insertions = []

    i = 0
    while i < len(lines):
        line = lines[i]
        # Match function definitions with request parameter
        func_match = re.match(r"^(\s*)(async\s+)?def\s+\w+\(", line)
        if not func_match:
            i += 1
            continue

        base_indent = func_match.group(1)
        base_len = len(base_indent)

        # Collect entire function signature (may span multiple lines)
        sig_end = i
        paren_depth = line.count("(") - line.count(")")
        while paren_depth > 0 and sig_end < len(lines) - 1:
            sig_end += 1
            paren_depth += lines[sig_end].count("(") - lines[sig_end].count(")")

        sig = "\n".join(lines[i:sig_end + 1])
        if "request" not in sig:
            i = sig_end + 1
            continue

        # Find first real body line
        body_start = sig_end + 1
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
        if body_start >= len(lines):
            i = body_start
            continue

        # Determine body indent from first body line
        first_body = lines[body_start]
        body_indent_len = len(first_body) - len(first_body.lstrip())
        if body_indent_len <= base_len:
            i = body_start
            continue

        # Scan function body for lang usage and definition
        has_lang_def = False
        uses_lang = False
        j = body_start
        while j < len(lines):
            bline = lines[j]
            bstripped = bline.strip()
            if bstripped:
                cur_indent = len(bline) - len(bline.lstrip())
                # End of function if we're back to or before base indent
                if cur_indent <= base_len and j > body_start:
                    break
                if re.match(r"\s*lang\s*=\s*", bline) and "==" not in bline and "!=" not in bline:
                    has_lang_def = True
                if re.search(r'\blang\b', bline) and 'language' not in bline and 'get_lang' not in bline:
                    uses_lang = True
            j += 1

        if uses_lang and not has_lang_def:
            insertions.append((body_start, body_indent_len))

        i = sig_end + 1

    # Apply in reverse order
    for idx, indent in sorted(insertions, reverse=True):
        lines.insert(idx, " " * indent + "lang = get_lang(request)")

    filepath.write_text("\n".join(lines))


def ensure_import(filepath: Path):
    source = filepath.read_text()
    if "from ui.i18n import t, get_lang" in source:
        return
    if "from ui.i18n import t" in source:
        source = source.replace("from ui.i18n import t", "from ui.i18n import t, get_lang", 1)
        filepath.write_text(source)
        return
    if "get_lang" not in source and 't(' not in source:
        return
    lines = source.split("\n")
    # Find the last COMPLETE top-level import (not inside a multi-line import)
    last_import = 0
    in_multiline = False
    for i, line_text in enumerate(lines):
        if in_multiline:
            if ")" in line_text:
                in_multiline = False
                last_import = i
            continue
        if not line_text.startswith((" ", "\t")):  # top-level only
            stripped = line_text.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                if "(" in stripped and ")" not in stripped:
                    in_multiline = True
                last_import = i
    lines.insert(last_import + 1, "from ui.i18n import t, get_lang")
    filepath.write_text("\n".join(lines))


def main():
    dry_run = "--dry-run" in sys.argv

    en_path = ROOT / "ui" / "locales" / "en.json"
    existing = json.loads(en_path.read_text())
    new_keys = dict(existing)
    for k, v in existing.items():
        _KNOWN_KEYS[v] = k

    scan_dirs = [ROOT / "ui" / "routes", ROOT / "ui" / "components"]
    for mod_dir in sorted((ROOT / "default_modules").glob("*/celerp_*")):
        scan_dirs.append(mod_dir)

    total = 0
    files_touched = []
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            count = process_file(py_file, new_keys, dry_run)
            if count:
                print(f"  {py_file.relative_to(ROOT)}: {count} replacements")
                total += count
                files_touched.append(py_file)

    if not dry_run:
        # Ensure imports and lang definitions
        for f in files_touched:
            ensure_import(f)
            source = f.read_text()
            # Standardize old lang patterns to get_lang(request)
            source = source.replace(
                'lang = request.cookies.get("celerp_lang", "en")',
                "lang = get_lang(request)"
            )
            source = re.sub(
                r'lang = company\.get\("settings", \{\}\)\.get\("language", "en"\).*\n',
                "lang = get_lang(request)\n",
                source
            )
            source = re.sub(
                r'request\.cookies\.get\("celerp_lang",\s*"en"\)',
                "get_lang(request)",
                source
            )
            f.write_text(source)

        sorted_keys = dict(sorted(new_keys.items()))
        en_path.write_text(json.dumps(sorted_keys, indent=2, ensure_ascii=False) + "\n")
        print(f"\nTotal: {total} replacements")
        print(f"en.json: {len(sorted_keys)} keys ({len(sorted_keys) - len(existing)} new)")
    else:
        print(f"\n[DRY RUN] Would make {total} replacements, add {len(new_keys) - len(existing)} new keys")


if __name__ == "__main__":
    main()

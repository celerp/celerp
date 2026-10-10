"""Every button label in the catalog is read by the app.

Button keys are always written out in full where they are used (no key is built from parts),
so a btn.* key that no source file names is a label nothing shows. It is removed from every
catalog rather than left to be translated.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _source_names() -> set[str]:
    out = subprocess.run(
        ["git", "grep", "-h", "-I", "-o", "-E", r"btn\.[A-Za-z0-9_]+", "--",
         "*.py", "*.js", "*.html", ":!**/locales/**", ":!tests/**"],
        cwd=_ROOT, capture_output=True, text=True, check=True).stdout
    return set(out.split())


def test_every_button_key_is_read_somewhere():
    keys = [k for k in json.loads((_ROOT / "ui/locales/en.json").read_text(encoding="utf-8"))
            if k.startswith("btn.")]
    unread = sorted(set(keys) - _source_names())
    assert not unread, f"button labels nothing shows: {unread}"


def test_no_button_key_is_built_from_parts():
    out = subprocess.run(
        ["git", "grep", "-n", "-I", "-E", r"""f["']btn\.[^"']*\{|["']btn\.["']\s*\+""", "--",
         "*.py", "*.js", "*.html", ":!tests/**"],
        cwd=_ROOT, capture_output=True, text=True).stdout
    assert out == "", f"a built button key hides its readers from the check above:\n{out}"

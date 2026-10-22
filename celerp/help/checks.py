"""Mechanical checks over help page files.

Each ``check_<name>(pages)`` returns readable messages, one per defect, each
starting with the page's file name (the coverage check names the module and
the screen instead). tests/test_help_content.py runs them over every shipped
page; tests/test_help_checks.py proves each one flags its defect fixture.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from celerp.help.registry import CORE, CORE_HELP_DIR, page_files
from celerp.help.schema import load_file, module_prefix
from celerp.modules.loader import (
    first_party_names, installed_help_dirs, module_search_path, read_manifest,
    resolve_module_path,
)
from celerp.services.permissions import is_permission_key

# EXIF ImageDescription written by the capture test into every screenshot it saves.
CAPTURE_MARKER = "celerp help capture"
_EXIF_DESCRIPTION = 0x010E
MAX_IMAGE_BYTES = 60 * 1024
MAX_TOTAL_IMAGE_BYTES = 25 * 1024 * 1024

_UI_CATALOG = Path(__file__).resolve().parent.parent.parent / "ui" / "locales" / "en.json"
_PARAM = re.compile(r"\{[^}]+\}")


@dataclass(frozen=True)
class PageFile:
    path: Path
    data: object  # the parsed JSON, or None when the file is not readable JSON
    module: str
    help_dir: Path


def collect(module: str, help_dir: Path) -> list[PageFile]:
    out = []
    for path in page_files(help_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        out.append(PageFile(path=path, data=data, module=module, help_dir=help_dir))
    return out


def _first_party_modules() -> list[tuple[str, Path, dict]]:
    out = []
    for name in sorted(first_party_names()):
        path = resolve_module_path(name, module_search_path())
        if path is not None:
            out.append((name, path, read_manifest(path)))
    return out


def shipped_pages() -> list[PageFile]:
    """Core pages and the pages of every first-party module."""
    pages = collect(CORE, CORE_HELP_DIR)
    for source in installed_help_dirs():
        if source.first_party:
            pages += collect(source.name, source.help_dir)
    return pages


def _steps(page: PageFile) -> list[dict]:
    data = page.data
    steps = data.get("steps") if isinstance(data, dict) else None
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def check_schema(pages: list[PageFile]) -> list[str]:
    out = []
    for page in pages:
        _, errors = load_file(page.path, page.help_dir, module_prefix(page.module))
        out += [f"{page.path.name}: {e}" for e in errors]
    return out


def english_label_keys() -> set[str]:
    """Every key of the English UI catalog and of each first-party module's English catalog."""
    keys = set(json.loads(_UI_CATALOG.read_text(encoding="utf-8")))
    for _name, path, manifest in _first_party_modules():
        entry = (manifest.get("locales") or {}).get("en")
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            keys |= set(json.loads((path / entry["file"]).read_text(encoding="utf-8")))
    return keys


def check_label_keys(pages: list[PageFile], keys: set[str] | None = None) -> list[str]:
    keys = english_label_keys() if keys is None else keys
    out = []
    for page in pages:
        for step in _steps(page):
            labels = step.get("labels")
            for key in (labels.values() if isinstance(labels, dict) else []):
                if key not in keys:
                    out.append(f"{page.path.name}: label key {key!r} is not in the English catalogs")
    return out


@lru_cache(maxsize=1)
def ui_route_paths() -> tuple[str, ...]:
    """Every route of the UI app with every first-party module loaded. Read in a child
    process so loading the modules leaves this process's loader state untouched."""
    run = subprocess.run([sys.executable, "-m", "celerp.help.checks"], capture_output=True,
                         text=True, timeout=300, check=True,
                         env={**os.environ, "MODULE_DIR": "", "ENABLED_MODULES": ""})
    return tuple(json.loads(run.stdout.strip().splitlines()[-1]))


def _route_matcher(routes):
    from starlette.routing import compile_path
    patterns = [compile_path(r)[0] for r in routes]
    return lambda path: any(p.match(path) for p in patterns)


def check_routes(pages: list[PageFile], routes=None) -> list[str]:
    matches = _route_matcher(ui_route_paths() if routes is None else routes)
    out = []
    for page in pages:
        data = page.data if isinstance(page.data, dict) else {}
        declared = [r for r in data.get("routes") or [] if isinstance(r, str)]
        captured = [s["capture"].get("route") for s in _steps(page) if isinstance(s.get("capture"), dict)]
        for route in dict.fromkeys([*declared, *(r for r in captured if isinstance(r, str))]):
            if not matches(_PARAM.sub("sample", urlsplit(route).path)):
                out.append(f"{page.path.name}: {route} is not a screen of the app")
    return out


def check_permissions(pages: list[PageFile]) -> list[str]:
    out = []
    for page in pages:
        perm = page.data.get("permission") if isinstance(page.data, dict) else None
        if perm is not None and not is_permission_key(perm):
            out.append(f"{page.path.name}: permission {perm!r} is not a permission key")
    return out


def _has_marker(path: Path) -> bool:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return im.getexif().get(_EXIF_DESCRIPTION) == CAPTURE_MARKER
    except Exception:
        return False


def check_images(pages: list[PageFile]) -> list[str]:
    out = []
    total = 0
    for page in pages:
        for step in _steps(page):
            image = step.get("image")
            if not isinstance(image, str):
                continue
            path = page.help_dir / image
            if not path.is_file():
                out.append(f"{page.path.name}: image {image} does not exist")
            elif path.suffix != ".webp":
                out.append(f"{page.path.name}: image {image} is not a .webp file")
            elif path.stat().st_size > MAX_IMAGE_BYTES:
                out.append(f"{page.path.name}: image {image} is larger than {MAX_IMAGE_BYTES // 1024} KB")
            elif not _has_marker(path):
                out.append(f"{page.path.name}: image {image} was not written by the capture test")
            else:
                total += path.stat().st_size
    if total > MAX_TOTAL_IMAGE_BYTES:
        out.append(f"all help images: {total} bytes, over the {MAX_TOTAL_IMAGE_BYTES} byte total")
    return out


def nav_screens() -> dict[str, list[str]]:
    """Each first-party module's sidebar and settings links, read from its manifest."""
    out: dict[str, list[str]] = {}
    for name, _path, manifest in _first_party_modules():
        nav = (manifest.get("slots") or {}).get("nav") or []
        entries = [nav] if isinstance(nav, dict) else nav
        hrefs = [e.get(k) for e in entries if isinstance(e, dict) for k in ("href", "settings_href")]
        out[name] = [h for h in hrefs if isinstance(h, str) and h]
    return out


def check_module_coverage(pages: list[PageFile], modules: dict[str, list[str]] | None = None) -> list[str]:
    modules = nav_screens() if modules is None else modules
    covered: dict[str, set[str]] = {}
    for page in pages:
        routes = page.data.get("routes") if isinstance(page.data, dict) else None
        covered.setdefault(page.module, set()).update(
            urlsplit(r).path for r in routes or [] if isinstance(r, str))
    out = []
    for module, hrefs in sorted(modules.items()):
        for path in dict.fromkeys(urlsplit(h).path for h in hrefs):
            if path not in covered.get(module, set()):
                out.append(f"{module}: {path} has no help page")
    return out


def _dump_ui_routes() -> list[str]:
    import ui.app
    from celerp.modules import loader

    names = {n for n in first_party_names() if not loader.is_core_folded(n)}
    loaded = loader.load_all(module_search_path(), names)
    loader.register_ui_routes(ui.app.app, loaded)
    return sorted({r.path for r in ui.app.app.routes if isinstance(getattr(r, "path", None), str)})


if __name__ == "__main__":
    print(json.dumps(_dump_ui_routes()))

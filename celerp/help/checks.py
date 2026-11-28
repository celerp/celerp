"""Mechanical checks over help page files.

Each ``check_<name>(pages)`` returns readable messages, one per defect, each
starting with the page's file name (the screen help check names the module and
the screen, the screen coverage check the screen instead). tests/test_help_content.py runs them over every shipped
page; tests/test_help_checks.py proves each one flags its defect fixture.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from celerp.help.registry import CORE, CORE_HELP_DIR, page_files
from celerp.help.bundle import MAX_IMAGE_BYTES, MODULE_NAME, TOPIC_ID, bundle_problems
from celerp.help.ledger import LEDGER_FILE, SITE_LANGUAGES, ledger
from celerp.help.translations import catalog, catalog_languages, entry_state
from celerp.help.schema import load_file, module_prefix
from celerp.help.search import covers
from celerp.modules.loader import (
    NAV_SCREEN_HELP, first_party_names, installed_help_dirs, module_search_path, read_manifest,
    resolve_module_path,
)
from celerp.services.permissions import is_permission_key

# EXIF ImageDescription the capture test starts every screenshot's marker with (capture_marker).
CAPTURE_MARKER = "celerp help capture"
_EXIF_DESCRIPTION = 0x010E
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


def english_labels() -> dict[str, str]:
    """The English UI catalog and each first-party module's English catalog, key to text."""
    labels = json.loads(_UI_CATALOG.read_text(encoding="utf-8"))
    for _name, path, manifest in _first_party_modules():
        entry = (manifest.get("locales") or {}).get("en")
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            labels |= json.loads((path / entry["file"]).read_text(encoding="utf-8"))
    return labels


def check_label_keys(pages: list[PageFile], labels: dict[str, str] | None = None) -> list[str]:
    """Every label key is in the English catalogs and names a fixed label: a text with a
    blank to fill ("{n} item(s)") would reach the reader with the blank in it."""
    labels = english_labels() if labels is None else labels
    out = []
    for page in pages:
        for step in _steps(page):
            step_labels = step.get("labels")
            for key in (step_labels.values() if isinstance(step_labels, dict) else []):
                if key not in labels:
                    out.append(f"{page.path.name}: label key {key!r} is not in the English catalogs")
                elif "{" in str(labels[key]):
                    out.append(f"{page.path.name}: label key {key!r} has a blank to fill: {labels[key]!r}")
    return out


@lru_cache(maxsize=1)
def _app_facts() -> dict:
    """The UI app with every first-party module loaded: its route paths and its signed-in
    screens. Read in a child process so loading the modules leaves this process's loader
    state untouched."""
    run = subprocess.run([sys.executable, "-m", "celerp.help.checks"], capture_output=True,
                         text=True, timeout=300, check=True,
                         env={**os.environ, "MODULE_DIR": "", "ENABLED_MODULES": ""})
    return json.loads(run.stdout.strip().splitlines()[-1])


def ui_route_paths() -> tuple[str, ...]:
    """Every route of the UI app with every first-party module loaded."""
    return tuple(_app_facts()["routes"])


def screen_facts() -> dict[str, dict]:
    """Every signed-in screen of the UI app: its route path, the handlers serving it and the
    content hash of their source (see _screen_digest)."""
    return _app_facts()["screens"]


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


def capture_marker(page_id: str, n: int, capture: dict) -> str:
    """The marker the capture test writes into step ``n``'s picture and check_images expects:
    the page, the step and a hash of the recipe that took it (its capture spec with its evidence),
    so a picture moved to another step or left from an older recipe no longer passes."""
    recipe = hashlib.sha256(json.dumps(capture, sort_keys=True).encode()).hexdigest()[:16]
    return f"{CAPTURE_MARKER} {page_id}#{n} {recipe}"


def image_marker(path: Path) -> str | None:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return im.getexif().get(_EXIF_DESCRIPTION)
    except Exception:
        return None


def check_images(pages: list[PageFile]) -> list[str]:
    out = []
    total = 0
    for page in pages:
        for n, step in enumerate(_steps(page), 1):
            image = step.get("image")
            if not isinstance(image, str):
                out.append(f"{page.path.name}: step {n} has no image")
                continue
            path = page.help_dir / image
            if not path.is_file():
                out.append(f"{page.path.name}: image {image} does not exist")
            elif path.suffix != ".webp":
                out.append(f"{page.path.name}: image {image} is not a .webp file")
            elif path.stat().st_size > MAX_IMAGE_BYTES:
                out.append(f"{page.path.name}: image {image} is larger than {MAX_IMAGE_BYTES // 1024} KB")
            elif not str(marker := image_marker(path)).startswith(CAPTURE_MARKER):
                out.append(f"{page.path.name}: image {image} was not written by the capture test")
            elif marker != capture_marker(page.data.get("id"), n, step.get("capture")):
                out.append(f"{page.path.name}: image {image} was taken for another step or by an older "
                           f"recipe ({marker}): retake it")
            else:
                total += path.stat().st_size
    if total > MAX_TOTAL_IMAGE_BYTES:
        out.append(f"all help images: {total} bytes, over the {MAX_TOTAL_IMAGE_BYTES} byte total")
    return out


def nav_screens() -> dict[str, list[tuple[str, object]]]:
    """Each first-party module's sidebar and settings screens, read from its manifest,
    with the help page id each names (None when it names none)."""
    out: dict[str, list[tuple[str, object]]] = {}
    for name, _path, manifest in _first_party_modules():
        nav = (manifest.get("slots") or {}).get("nav") or []
        entries = [nav] if isinstance(nav, dict) else nav
        out[name] = [(entry[href], entry.get(help_key))
                     for entry in entries if isinstance(entry, dict)
                     for href, help_key in NAV_SCREEN_HELP.items()
                     if isinstance(entry.get(href), str) and entry[href]]
    return out


def check_screen_help(pages: list[PageFile],
                      modules: dict[str, list[tuple[str, object]]] | None = None) -> list[str]:
    """Each module screen names a help page that the module (or core) ships and whose
    routes cover the screen, so the screen's Help panel lists that page."""
    modules = nav_screens() if modules is None else modules
    by_id = {page.data["id"]: page for page in pages
             if isinstance(page.data, dict) and isinstance(page.data.get("id"), str)}
    out = []
    for module, screens in sorted(modules.items()):
        for path, page_id in dict.fromkeys(screens):
            page = by_id.get(page_id) if isinstance(page_id, str) else None
            if page_id is None:
                out.append(f"{module}: {path}: names no help page")
            elif page is None or page.module not in (module, CORE):
                out.append(f"{module}: {path}: names help page {page_id!r}, which {module} does not ship")
            elif not covers([r for r in page.data.get("routes") or [] if isinstance(r, str)], path):
                out.append(f"{module}: {path}: help page {page_id!r} does not list this screen in its routes")
    return out


def _page_paths(page_data) -> list[str]:
    """The paths of a page's routes. A screen is a route path of the app, so a page covers it
    when one of its routes serves that path, whatever tab or type the route's query names."""
    routes = page_data.get("routes") if isinstance(page_data, dict) else None
    return [urlsplit(r).path for r in routes if isinstance(r, str)] if isinstance(routes, list) else []


def _covered(page_data, screens: dict[str, dict]) -> list[str]:
    paths = _page_paths(page_data)
    return [screen for screen in sorted(screens) if covers(paths, screen)]


def check_screen_coverage(pages: list[PageFile], screens: dict[str, dict] | None = None) -> list[str]:
    """Every signed-in screen is covered by the routes of at least one page."""
    screens = screen_facts() if screens is None else screens
    routes = [_page_paths(page.data) for page in pages]
    return [f"{screen}: no help page covers this screen"
            for screen in sorted(screens) if not any(covers(r, screen) for r in routes)]


def check_covers_screen(pages: list[PageFile], screens: dict[str, dict] | None = None) -> list[str]:
    """Every page covers at least one signed-in screen."""
    screens = screen_facts() if screens is None else screens
    return [f"{page.path.name}: covers no screen of the app" for page in pages if not _covered(page.data, screens)]


def page_checked(page_data, screens: dict[str, dict]) -> str:
    """The ``checked`` value of a page: one hash over the digest of every screen it covers."""
    lines = sorted(f"{screen} {screens[screen]['digest']}" for screen in _covered(page_data, screens))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def check_checked(pages: list[PageFile], screens: dict[str, dict] | None = None) -> list[str]:
    """Each page's ``checked`` stamp equals the hash of the screens it covers, so a change to
    one of those screens re-opens the page until its steps are read again and it is stamped."""
    screens = screen_facts() if screens is None else screens
    out = []
    for page in pages:
        if not isinstance(page.data, dict):
            continue
        stamp = page.data.get("checked")
        if stamp == page_checked(page.data, screens):
            continue
        handlers = sorted({h for screen in _covered(page.data, screens) for h in screens[screen]["handlers"]})
        state = "missing" if stamp is None else "stale"
        out.append(f"{page.path.name}: checked is {state} (handlers: {', '.join(handlers)}); re-read its steps "
                   f"against those screens, then python -m celerp.help.checks stamp {page.data.get('id')}")
    return out


def _ledger_problems(data, pages: list[PageFile]) -> list[str]:
    if not (isinstance(data, dict) and set(data) == {"locales", "topics", "modules"}):
        return ["must be an object with exactly locales, topics and modules"]
    locales, topics, modules = data["locales"], data["topics"], data["modules"]
    out = []
    if not (isinstance(locales, list) and all(isinstance(c, str) for c in locales)):
        out.append("locales must be a list of language codes")
    if not (isinstance(topics, dict) and all(isinstance(k, str) and TOPIC_ID.match(k) for k in topics)):
        out.append("topics must be an object of topic ids")
    if not (isinstance(modules, dict) and all(isinstance(k, str) and MODULE_NAME.match(k) for k in modules)):
        out.append("modules must be an object of module names")
    if out:
        return out
    ids = {p.data.get("id") for p in pages if isinstance(p.data, dict)}
    live = {module_prefix(p.module) for p in pages}

    def target(where, value):
        if not (value == "" or value in ids or value in live):
            out.append(f"{where} must be a live topic id, a live module or \"\"")

    out += [f"shipped page {pid} has no topics entry" for pid in sorted(ids - set(topics))]
    for pid, value in sorted(topics.items()):
        if value is None and pid not in ids:
            out.append(f"page {pid} was deleted: set its ledger value to its replacement id, module or \"\"")
        elif value is not None and pid in ids:
            out.append(f"page {pid} is shipped: its ledger value must be null")
        elif value is not None:
            target(f"topics.{pid}", value)
    retired = {pid.split(".")[0] for pid in topics} - live
    out += [f"retired module {name} has no modules entry" for name in sorted(retired - set(modules))]
    for name, value in sorted(modules.items()):
        if name in live:
            out.append(f"module {name} is live and must have no modules entry")
        elif name not in retired:
            out.append(f"modules.{name} names no ledger topic")
        else:
            target(f"modules.{name}", value)
    out += [f"locales lacks {code}" + ("" if code == "en" else ", which has a catalog")
            for code in ["en", *catalog_languages()] if code not in locales]
    out += [f"locales holds {code}, which is not a site language" for code in locales if code not in SITE_LANGUAGES]
    return out


def check_ledger(pages: list[PageFile]) -> list[str]:
    """ledger.json names every shipped guide id, what replaced each deleted one, each retired
    module, and every guide language with a catalog."""
    try:
        data = ledger()
    except ValueError as e:
        return [str(e)]
    return [f"{LEDGER_FILE.name}: {p}" for p in _ledger_problems(data, pages)]


def check_translations(pages: list[PageFile]) -> list[str]:
    """Each catalog parses, names live pages only, and its fresh entries are valid with
    unique titles; the bundle built from them and the English passes bundle_problems,
    so text a deploy rule rejects fails here first."""
    from celerp.help.assemble import build_bundle  # assemble reads shipped_pages from here

    data = {p.data.get("id"): p.data for p in pages if isinstance(p.data, dict)}
    out = []
    for lang in catalog_languages():
        try:
            entries = catalog(lang)
        except ValueError as e:
            out.append(str(e))
            continue
        name, titles = f"{lang}.json", {}
        for pid, entry in entries.items():
            if pid not in data:
                out.append(f"{name}: {pid} is not a live page id")
                continue
            state, problems = entry_state(data[pid], entry)
            out += [f"{name}: {pid}: {p}" for p in problems]
            if state == "fresh" and isinstance(entry["title"], str):
                if entry["title"] in titles:
                    out.append(f"{name}: {pid}: its title is also the title of {titles[entry['title']]}")
                titles.setdefault(entry["title"], pid)
    if out:
        return out
    today = date.today().isoformat()
    try:
        bundle = build_bundle(lambda path: today)
    except ValueError as e:
        return [str(e)]
    return [f"help.json: {p}" for p in bundle_problems(bundle)]


_CHECKED_LINE = re.compile(r'"checked"\s*:\s*"[^"]*"')
_ORDER_LINE = re.compile(r'^([ \t]*)"order"\s*:.*$', re.MULTILINE)


def stamp_text(text: str, value: str) -> str:
    """The page file text with its ``checked`` value set: replaced in place, or written on a
    new line after ``order``."""
    if _CHECKED_LINE.search(text):
        return _CHECKED_LINE.sub(f'"checked": "{value}"', text, count=1)
    order = _ORDER_LINE.search(text)
    if order is None:
        raise ValueError("page has no order line to stamp after")
    return f'{text[:order.end()]}\n{order.group(1)}"checked": "{value}",{text[order.end():]}'


def stamp(page_ids: list[str]) -> list[str]:
    """Write the current ``checked`` value into each named shipped page; the file paths written."""
    screens = screen_facts()
    by_id = {page.data.get("id"): page for page in shipped_pages() if isinstance(page.data, dict)}
    unknown = [i for i in page_ids if i not in by_id]
    if unknown:
        raise SystemExit(f"no shipped help page with id {', '.join(unknown)}")
    written = []
    for page_id in page_ids:
        page = by_id[page_id]
        text = page.path.read_text(encoding="utf-8")
        page.path.write_text(stamp_text(text, page_checked(page.data, screens)), encoding="utf-8")
        written.append(str(page.path))
    return written


# Signed-in screens: GET routes whose handler reaches one of these page shells.
_SHELLS = frozenset({"base_shell", "wizard_page", "minimal_shell", "auth_shell"})
# A download answers with a file and shows a shell only on its error path: not a screen.
_NOT_SCREENS = ("/download", "/pack", "/import/template")


def _handler(endpoint):
    """FastHTML wraps each handler in a closure; the user function is the cell that is a function."""
    for cell in endpoint.__closure__ or ():
        value = cell.cell_contents
        if inspect.isfunction(value) and not value.__module__.startswith("fasthtml"):
            return inspect.unwrap(value)
    return inspect.unwrap(endpoint)


def _called_names(node) -> set[str]:
    return {n.func.id if isinstance(n.func, ast.Name) else n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))}


@lru_cache(maxsize=None)
def _module_tree(module: str) -> tuple[str, ast.Module]:
    source = inspect.getsource(sys.modules[module])
    return source, ast.parse(source)


@lru_cache(maxsize=None)
def _defs(module: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(_module_tree(module)[1]) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@lru_cache(maxsize=None)
def _call_graph(module: str) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for node in _defs(module):
        graph.setdefault(node.name, set()).update(_called_names(node))
    return graph


def _reaches_shell(fn) -> bool:
    """Whether ``fn`` calls a page shell, directly or through the functions it calls: a called
    name is followed to any function of that name defined in the same module (nested ones
    included), else to the function it names in the module's globals (another module)."""
    seen, todo = set(), [(fn.__module__, fn.__name__)]
    while todo:
        key = todo.pop()
        if key in seen or key[0] not in sys.modules or key[0].startswith(("fasthtml", "starlette")):
            continue
        seen.add(key)
        try:
            graph = _call_graph(key[0])
        except (OSError, TypeError, SyntaxError):
            continue
        called = graph.get(key[1], set())
        if called & _SHELLS:
            return True
        names = vars(sys.modules[key[0]])
        for name in called:
            if name in graph:
                todo.append((key[0], name))
            elif inspect.isfunction(names.get(name)):
                target = inspect.unwrap(names[name])
                todo.append((target.__module__, target.__name__))
    return False


def _handler_source(fn) -> str:
    """The handler's own source plus every function of its module it calls by name (one level,
    nested ones included), in line order, so a tab list helper counts as part of the screen."""
    lines, start = inspect.getsourcelines(fn)
    own = "".join(lines)
    end = start + len(lines) - 1
    called = _called_names(ast.parse(textwrap.dedent(own)))
    source = _module_tree(fn.__module__)[0]
    helpers = sorted((n for n in _defs(fn.__module__) if n.name in called and not start <= n.lineno <= end),
                     key=lambda n: n.lineno)
    return "\n".join([own, *(ast.get_source_segment(source, n) or "" for n in helpers)])


def _screens(app) -> dict[str, dict]:
    """Each signed-in screen of ``app``: the handlers serving it and the sha256 of their source."""
    import ui.app

    handlers: dict[str, list] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if ("GET" not in (getattr(route, "methods", None) or ()) or not getattr(route, "endpoint", None)
                or ui.app._is_public(path) or path.endswith(_NOT_SCREENS)):
            continue
        fn = _handler(route.endpoint)
        try:
            if _reaches_shell(fn):
                handlers.setdefault(path, []).append(fn)
        except (OSError, TypeError):
            continue
    out = {}
    for path, fns in sorted(handlers.items()):
        names = [f"{fn.__module__}.{fn.__name__}" for fn in fns]
        text = "\n".join(f"{name}\n{_handler_source(fn)}" for name, fn in zip(names, fns))
        out[path] = {"handlers": names, "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    return out


def _dump_app_facts() -> dict:
    import ui.app
    from celerp.modules import loader

    names = {n for n in first_party_names() if not loader.is_core_folded(n)}
    loaded = loader.load_all(module_search_path(), names)
    loader.register_ui_routes(ui.app.app, loaded)
    routes = sorted({r.path for r in ui.app.app.routes if isinstance(getattr(r, "path", None), str)})
    return {"routes": routes, "screens": _screens(ui.app.app)}


if __name__ == "__main__":
    if sys.argv[1:2] == ["stamp"]:
        for written in stamp(sys.argv[2:]):
            print(written)
    else:
        print(json.dumps(_dump_app_facts()))

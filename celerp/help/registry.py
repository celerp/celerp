"""The help index: core pages, then every installed module's help folder.

Built on first read and swapped in whole; ``reset`` (called by the module loader
on every load pass) drops it so the next read rebuilds from the modules now
installed. An invalid page is logged and skipped, never fatal: first-party pages
are held valid by the content tests instead.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from celerp.help.schema import HelpPage, load_file, module_prefix
from celerp.modules.loader import installed_help_dirs

log = logging.getLogger(__name__)

CORE_HELP_DIR = Path(__file__).resolve().parent / "pages"
CORE = "core"


@dataclass(frozen=True)
class Index:
    pages: dict[str, HelpPage]


_index: Index | None = None
_lock = threading.Lock()


def page_files(help_dir: Path) -> list[Path]:
    """The page files of one help folder, in name order."""
    return sorted(help_dir.glob("*.json")) if help_dir.is_dir() else []


def _sources() -> list[tuple[str, str, str, Path, dict]]:
    """(source, module, display name, help dir, module catalogs): core, first-party,
    then third-party."""
    found = installed_help_dirs()
    ordered = [s for s in found if s.first_party] + [s for s in found if not s.first_party]
    return [("core", CORE, "Celerp", CORE_HELP_DIR, {}), *(
        ("first_party" if s.first_party else "third_party", s.name, s.display_name, s.help_dir,
         s.catalogs)
        for s in ordered)]


def _build() -> Index:
    pages: dict[str, HelpPage] = {}
    for source, module, display, help_dir, catalogs in _sources():
        for path in page_files(help_dir):
            page, errors = load_file(path, help_dir, module_prefix(module))
            if page is None:
                log.warning("Help page %s from %s skipped: %s", path.name, module, "; ".join(errors))
                continue
            if page.id in pages:
                log.warning("Help page %s from %s skipped: id already provided by %s",
                            page.id, module, pages[page.id].module)
                continue
            pages[page.id] = dataclasses.replace(
                page, source=source, module=module, module_display=display, catalogs=catalogs)
    return Index(pages=pages)


def get_index() -> Index:
    global _index
    index = _index
    if index is None:
        with _lock:
            if _index is None:
                _index = _build()
            index = _index
    return index


def reset() -> None:
    global _index
    _index = None

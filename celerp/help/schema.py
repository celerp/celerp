"""Help page format: one JSON file per page, validated into a HelpPage.

The format is documented for module authors in SCHEMA.md beside this file.
Validation returns readable messages instead of raising, so a caller decides
whether a bad page is a test failure (first-party) or a logged skip (runtime).
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

PAGE_KEYS = frozenset({"id", "title", "summary", "keywords", "routes", "permission",
                       "order", "steps", "related"})
STEP_KEYS = frozenset({"text", "labels", "image", "capture"})
CAPTURE_KEYS = frozenset({"route", "selector", "actions"})
# One verb per action; "fill" also carries the text to type.
ACTION_VERBS = frozenset({"click", "fill", "press", "wait"})

MAX_TITLE = 120
MAX_STEP_TEXT = 500
MAX_STEPS = 20
MAX_FILE_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 60 * 1024

_ID = re.compile(r"^[a-z0-9-]+\.[a-z0-9-]+$")
PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")
_IMAGE = re.compile(r"^img/[A-Za-z0-9][A-Za-z0-9_-]*\.webp$")


@dataclass(frozen=True)
class HelpPage:
    id: str
    title: str
    summary: str
    keywords: list[str]
    routes: list[str]
    permission: str | None
    order: int
    steps: list[dict]
    related: list[str]
    help_dir: Path
    # Set by the registry: core, first_party or third_party, the owning module, and
    # that module's own UI catalogs by language (see render.label).
    source: str = ""
    module: str = ""
    module_display: str = ""
    catalogs: dict[str, dict] = field(default_factory=dict)


def module_prefix(module: str) -> str:
    """The page id prefix a module owns: its name without "celerp-" ("core" for core pages)."""
    return module.removeprefix("celerp-")


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _keys(where: str, data: dict, allowed: frozenset[str]) -> list[str]:
    errors = [f"{where}unknown key {k!r}" for k in sorted(set(data) - allowed)]
    errors += [f"{where}missing key {k!r}" for k in sorted(allowed - set(data))]
    return errors


def _capture_errors(where: str, capture) -> list[str]:
    if not isinstance(capture, dict):
        return [f"{where}capture must be an object"]
    errors = _keys(f"{where}capture ", capture, CAPTURE_KEYS)
    if errors:
        return errors
    if not isinstance(capture["route"], str) or not capture["route"].startswith("/"):
        errors.append(f"{where}capture route must be a path starting with /")
    if not isinstance(capture["selector"], str) or not capture["selector"].strip():
        errors.append(f"{where}capture selector must be a non-empty string")
    actions = capture["actions"]
    if not isinstance(actions, list):
        return [*errors, f"{where}capture actions must be a list"]
    for n, action in enumerate(actions, 1):
        verbs = set(action) & ACTION_VERBS if isinstance(action, dict) else set()
        extra = set(action) - ACTION_VERBS - {"value"} if isinstance(action, dict) else set()
        if (len(verbs) != 1 or extra or not all(isinstance(v, str) for v in action.values())
                or ("value" in action) != ("fill" in action)):
            errors.append(f"{where}capture action {n} must be one of "
                          '{"click": selector}, {"fill": selector, "value": text}, '
                          '{"press": key} or {"wait": selector}')
    return errors


def _step_errors(n: int, step) -> list[str]:
    where = f"step {n}: "
    if not isinstance(step, dict):
        return [f"{where}must be an object"]
    errors = _keys(where, step, STEP_KEYS)
    if errors:
        return errors
    text, labels, image = step["text"], step["labels"], step["image"]
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{where}text must be a non-empty string")
    elif len(text) > MAX_STEP_TEXT:
        errors.append(f"{where}text is longer than {MAX_STEP_TEXT} characters")
    if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in labels.items()):
        errors.append(f"{where}labels must map each placeholder name to a catalog key")
    elif isinstance(text, str):
        names = set(PLACEHOLDER.findall(text))
        errors += [f"{where}placeholder {{{k}}} has no label" for k in sorted(names - set(labels))]
        errors += [f"{where}label {k!r} is not used in the text" for k in sorted(set(labels) - names)]
        if "{" in PLACEHOLDER.sub("", text) or "}" in PLACEHOLDER.sub("", text):
            errors.append(f"{where}text has a brace that is not a {{name}} placeholder")
    if image is not None and not (isinstance(image, str) and _IMAGE.match(image)):
        errors.append(f"{where}image must be null or img/<file>.webp inside the help folder")
    return [*errors, *_capture_errors(where, step["capture"])]


def validate(data, help_dir: Path, prefix: str) -> tuple[HelpPage | None, list[str]]:
    """The page, or None and every reason it is not a valid page. ``prefix`` is the
    owning module's short name (see module_prefix); the page id must start with it."""
    if not isinstance(data, dict):
        return None, ["page must be a JSON object"]
    errors = _keys("", data, PAGE_KEYS)
    if errors:
        return None, errors
    page_id = data["id"]
    if not isinstance(page_id, str) or not _ID.match(page_id):
        errors.append("id must look like <module>.<page>, lower case letters, digits and -")
    elif page_id.split(".", 1)[0] != prefix:
        errors.append(f"id must start with {prefix!r}, the module that ships the page")
    title = data["title"]
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")
    elif len(title) > MAX_TITLE:
        errors.append(f"title is longer than {MAX_TITLE} characters")
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        errors.append("summary must be a non-empty string")
    for key in ("keywords", "related"):
        if not _str_list(data[key]):
            errors.append(f"{key} must be a list of strings")
    if not _str_list(data["routes"]) or not all(r.startswith("/") for r in data["routes"]):
        errors.append("routes must be a list of paths starting with /")
    if data["permission"] is not None and not isinstance(data["permission"], str):
        errors.append("permission must be a permission key or null")
    if not isinstance(data["order"], int) or isinstance(data["order"], bool):
        errors.append("order must be a whole number")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
    elif len(steps) > MAX_STEPS:
        errors.append(f"a page has at most {MAX_STEPS} steps")
    else:
        for n, step in enumerate(steps, 1):
            errors += _step_errors(n, step)
    if errors:
        return None, errors
    return HelpPage(
        id=page_id, title=title, summary=data["summary"], keywords=data["keywords"],
        routes=data["routes"], permission=data["permission"], order=data["order"],
        steps=steps, related=data["related"], help_dir=help_dir,
    ), []


def load_file(path: Path, help_dir: Path, prefix: str) -> tuple[HelpPage | None, list[str]]:
    """Read and validate one page file."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [f"file is larger than {MAX_FILE_BYTES // 1024} KB"]
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"not readable as JSON ({exc})"]
    return validate(data, help_dir, prefix)


def _is_webp(data: bytes) -> bool:
    """A whole WebP file: the RIFF header names WEBP and its size covers exactly the bytes."""
    return (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") + 8 == len(data))


def read_image(page: HelpPage, step: int) -> bytes | None:
    """The screenshot of ``page``'s step number ``step`` (from 1), or None when the step has
    none or its file is not a WebP image of at most MAX_IMAGE_BYTES stored as a regular
    file in the page's own img folder. Links are never followed, so a page can only show
    a file that is really inside its help folder."""
    if not 1 <= step <= len(page.steps):
        return None
    image = page.steps[step - 1]["image"]
    if not (isinstance(image, str) and _IMAGE.match(image)):
        return None
    path = page.help_dir / image
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        if path.parent.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            return None
        if path.resolve(strict=True).parent != page.help_dir.resolve(strict=True) / "img":
            return None
        with os.fdopen(os.open(path, flags), "rb") as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_IMAGE_BYTES:
                return None
            data = f.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_IMAGE_BYTES and _is_webp(data) else None

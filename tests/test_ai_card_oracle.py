# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every change the assistant can propose reads on its confirm card as a person reads it:
the action by its name, each field by its label and each choice by its label, in the
reader's language, a record by its name and never by its id, and nothing the model alone
needs (the preview fingerprint, the instruction to call the preview first).

The oracle builds arguments for every compiled write tool from the tool's own parameter
schema, the way a model fills them, and renders the card in English and German."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from test_helpers import real_agent_app

ROOT = Path(__file__).resolve().parents[1]
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UPLOAD = re.compile(r"ai_up_[0-9a-f]{32}")
# A key of the user's own in a free map (an attribute they made): it reads as they wrote it.
_OWN = "Colour"


def _catalog(lang: str) -> dict:
    out = json.loads((ROOT / "ui" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    for path in sorted(ROOT.glob(f"default_modules/*/*/locales/{lang}.json")):
        out.update(json.loads(path.read_text(encoding="utf-8")))
    return out


def _pending() -> set[str]:
    data = json.loads((ROOT / "tests" / "i18n_pending_translation.json").read_text(encoding="utf-8"))
    keys = set(data["keys"])
    for module in (data.get("modules") or {}).values():
        keys |= set(module)
    return keys


def _shapes(schema: dict) -> list[dict]:
    out = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        for alt in schema.get(key) or []:
            if isinstance(alt, dict):
                out.extend(_shapes(alt))
    return out


def _value(schema, key: str):
    """A value a model would send for ``key`` under ``schema``: a fresh id for an id
    field, an assistant upload for a file, the first choice of a choice."""
    if not isinstance(schema, dict):
        return "Sample"
    shapes = [s for s in _shapes(schema) if s.get("type") != "null"]
    enum = next((s["enum"] for s in shapes if s.get("enum")), None)
    if enum:
        return enum[0]
    props: dict = {}
    for shape in shapes:
        props.update(shape.get("properties") or {})
    if props:
        return {k: _value(v, k) for k, v in props.items()}
    types = {s.get("type") for s in shapes}
    items = next((s["items"] for s in shapes if isinstance(s.get("items"), dict)), None)
    if "array" in types or items is not None:
        return [_value(items or {}, key)]
    if "object" in types:
        return {_OWN: "Red"}
    if "boolean" in types:
        return True
    if "integer" in types:
        return 3
    if "number" in types:
        return 12.5
    if key.startswith("file") and key.endswith(("_id", "_ids")):
        return "ai_up_" + uuid.uuid4().hex
    if key.endswith(("_id", "_ids")) or any(s.get("format") == "uuid" for s in shapes):
        return str(uuid.uuid4())
    if any(s.get("format") == "date" for s in shapes):
        return "2026-01-31"
    return "Sample"


def _keys(value, out: set[str]) -> set[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def _enums(schema, key: str | None, out: set[tuple[str, str]]) -> set[tuple[str, str]]:
    if not isinstance(schema, dict):
        return out
    for k, v in (schema.get("properties") or {}).items():
        _enums(v, k, out)
    if isinstance(schema.get("items"), dict):
        _enums(schema["items"], key, out)
    for alt in ("anyOf", "oneOf", "allOf"):
        for a in schema.get(alt) or []:
            _enums(a, key, out)
    if key and schema.get("enum"):
        out |= {(key, e) for e in schema["enum"] if isinstance(e, str)}
    return out


@pytest.fixture(scope="module")
def write_tools():
    from celerp.ai import tools as ai_tools

    tools = {n: c for n, c in ai_tools.compile_agent_capabilities(real_agent_app(), {}).items()
             if c["requires_confirmation"]}
    assert len(tools) > 300
    return tools


def test_every_write_tool_card_reads_in_the_reader_language_and_never_shows_an_id(write_tools):
    from fasthtml.common import to_xml

    from celerp.ai.card import action_title, card_arguments
    from celerp.ai.service import _card_warnings
    from celerp_ai.ui_routes import _action_card, label_key
    from ui import i18n

    en, de, pending = _catalog("en"), _catalog("de"), _pending()
    problems: list[str] = []

    def readable(name: str, key: str | None, what: str):
        if key is None or key not in en:
            problems.append(f"{name}: {what} has no catalog label")
        elif key not in de and key not in pending:
            problems.append(f"{name}: {what} ({key}) is neither translated nor waiting for translation")

    for name, cap in sorted(write_tools.items()):
        params = cap["tool"]["function"]["parameters"]
        arguments = _value(params, "")
        token = (cap.get("preview") or {}).get("token")
        if token:
            arguments.setdefault("body", {})[token] = "ffffffffffffffff"
        shown = card_arguments(cap, arguments)
        fields = _keys(list(shown.values()), set()) - {_OWN}
        title = action_title(cap)
        readable(name, title["key"], "the title")
        if "Call " in title["text"] or name in title["text"]:
            problems.append(f"{name}: the title carries the model instruction")
        if token and token in fields:
            problems.append(f"{name}: the card shows the preview fingerprint {token}")
        for key in sorted(fields):
            readable(name, label_key(key), f"field {key}")
        for key, value in sorted(_enums(params, None, set())):
            readable(name, f"enum.{key}.{value}", f"choice {key}={value}")
        for warning in _card_warnings(cap, 20, {**arguments, "file_ids": ["ai_up_" + uuid.uuid4().hex]}):
            readable(name, warning["key"], f"warning {warning['text']!r}")
        action = {"id": "c1", "name": name, "title": title, "shown": shown, "names": {}, "status": "pending"}
        for lang in ("en", "de"):
            i18n.set_lang(lang)
            try:
                html = re.sub(r"<[^>]*>", " ", to_xml(_action_card("conv", "msg", action, lang)))
            finally:
                i18n.set_lang("en")
            if _UUID.search(html) or _UPLOAD.search(html):
                problems.append(f"{name} ({lang}): the card shows a record id")
            if name in html or "ffffffffffffffff" in html:
                problems.append(f"{name} ({lang}): the card shows what only the model needs")
    assert problems == [], f"{len(problems)} problems, first: " + "\n".join(problems[:40])


_STRAY = "zz_not_read"
_EXTRA = "zz_custom_price"


def _free(shapes: list[dict]) -> bool:
    return any(s.get("additionalProperties") not in (None, False)
               or (s.get("type") == "object" and "properties" not in s) for s in shapes)


def _with_field(schema, value, key: str, *, free: bool):
    """``value`` with ``key`` added to every record whose shape takes undeclared fields
    (``free``) or does not (not ``free``), at every depth and through every alternative."""
    if not isinstance(schema, dict):
        return value
    shapes = _shapes(schema)
    props: dict = {}
    for shape in shapes:
        props.update(shape.get("properties") or {})
    if isinstance(value, dict) and props:
        out = {k: _with_field(props.get(k), v, key, free=free) for k, v in value.items()}
        return {**out, key: "12"} if _free(shapes) == free else out
    items = next((s["items"] for s in shapes if isinstance(s.get("items"), dict)), None)
    if isinstance(value, list) and items is not None:
        return [_with_field(items, v, key, free=free) for v in value]
    return value


def _without_unshown(schema, value):
    """``value`` without the fields its schema marks ``x-celerp-unshown`` (a version the route
    checks), at any depth: they mean nothing to a person, so the card leaves them out."""
    if not isinstance(schema, dict):
        return value
    shapes = _shapes(schema)
    props: dict = {}
    for shape in shapes:
        props.update(shape.get("properties") or {})
    if isinstance(value, dict) and props:
        return {k: _without_unshown(props.get(k), v) for k, v in value.items()
                if (props.get(k) or {}).get("x-celerp-unshown") is not True}
    items = next((s["items"] for s in shapes if isinstance(s.get("items"), dict)), None)
    if isinstance(value, list) and items is not None:
        return [_without_unshown(items, v) for v in value]
    return value


def test_every_write_tool_card_shows_exactly_the_fields_its_route_reads(write_tools):
    """Each declared field the model fills shows, at every depth and through every
    alternative shape, and so does a field of the user's own on a record that takes them (an
    item's custom price column); a field the route does not declare (the route never reads
    it), a version the route marks unshown and the preview fingerprint do not."""
    from celerp.ai.card import card_arguments

    problems: list[str] = []
    strays = extras = unshown = 0
    for name, cap in sorted(write_tools.items()):
        params = cap["tool"]["function"]["parameters"]
        parts = params.get("properties") or {}
        arguments = _with_field(params, _value(params, ""), _EXTRA, free=True)
        extras += json.dumps(arguments).count(_EXTRA)
        token = (cap.get("preview") or {}).get("token")
        kept = {p: _without_unshown(parts.get(p), v) for p, v in arguments.items()}
        unshown += kept != arguments
        expected = {p: {k: v for k, v in kept[p].items() if not (p == "body" and k == token)}
                    for p in ("body", "path", "query") if isinstance(kept.get(p), dict) and p in parts}
        sent = _with_field(params, arguments, _STRAY, free=False)
        strays += json.dumps(sent).count(_STRAY)
        if token:
            sent.setdefault("body", {})[token] = "ffffffffffffffff"
        shown = card_arguments(cap, sent)
        if shown != expected:
            problems.append(f"{name}: shows {json.dumps(shown)[:200]} instead of {json.dumps(expected)[:200]}")
    assert strays > len(write_tools), "the oracle must send undeclared fields to most tools"
    assert extras, "the oracle must send a field of the user's own to a record that takes them (new item)"
    assert unshown, "the oracle must send a version the route marks unshown (bank resolve accept)"
    assert problems == [], f"{len(problems)} problems, first: " + "\n".join(problems[:20])

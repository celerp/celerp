# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a proposed change's confirm card shows: the arguments as the route reads them.

The model may send fields a route does not declare (the route ignores them), the
preview token a change sends back and a field the route marks ``x-celerp-unshown`` (a
version fingerprint it checks): neither means anything to a person. The card lists only
the other fields the tool's own parameter schema declares, at any depth; a field that is
a free map (attributes keyed by the user's own names) keeps every key.
"""
from __future__ import annotations

from typing import Any

_PARTS = ("body", "path", "query")


def _alternatives(schema: dict) -> list[dict]:
    """``schema`` and every object shape it may take (anyOf, oneOf, allOf)."""
    out = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        for alt in schema.get(key) or []:
            if isinstance(alt, dict):
                out.extend(_alternatives(alt))
    return out


def unshown(schema: Any) -> bool:
    """Whether a declared field is a check value the card leaves out."""
    return isinstance(schema, dict) and schema.get("x-celerp-unshown") is True


def declared(schema: Any, value: Any) -> Any:
    """``value`` with every field ``schema`` does not declare left out, at any depth."""
    if not isinstance(schema, dict):
        return value
    shapes = _alternatives(schema)
    if isinstance(value, dict):
        properties: dict = {}
        free = False
        for shape in shapes:
            if isinstance(shape.get("properties"), dict):
                properties.update(shape["properties"])
            if shape.get("additionalProperties") not in (None, False) or (
                    shape.get("type") == "object" and "properties" not in shape):
                free = True
        if not properties:
            return value
        return {k: declared(properties.get(k), v) for k, v in value.items()
                if not unshown(properties.get(k)) and (k in properties or free)}
    if isinstance(value, list):
        items = next((s["items"] for s in shapes if isinstance(s.get("items"), dict)), None)
        return [declared(items, v) for v in value]
    return value


def card_arguments(capability: dict, arguments: dict) -> dict:
    """The groups of ``arguments`` (body, path, query) a card for ``capability`` shows."""
    schema = capability["tool"]["function"]["parameters"].get("properties") or {}
    token = (capability.get("preview") or {}).get("token")
    shown = {}
    for part in _PARTS:
        value = arguments.get(part)
        if isinstance(value, dict) and part in schema:
            group = declared(schema[part], value)
            shown[part] = {k: v for k, v in group.items() if not (part == "body" and k == token)}
    return shown


def action_title(capability: dict) -> dict:
    """The action a card for ``capability`` names, kept as ui.i18n.kept keeps a text: the
    catalog key ``tool.<name>``, else the route's own summary."""
    return {"key": f"tool.{capability['name']}", "params": {}, "text": capability["summary"]}

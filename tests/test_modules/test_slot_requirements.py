# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A slot entry carries what the page or service reading it needs, in the type
it reads it as, and a module whose entry does not is refused before any of its
code runs.

Each rule comes from the code that reads the slot: the sidebar sorts nav by
"order" and groups it by "group", the inventory page offers catalog channels by
"id" and send-to targets by "doc_type", the category defaults are read as a
list of field definitions, and so on.
"""
from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path

import pytest

from celerp.modules import loader, slots

_CODE = {
    "hooks.py": "async def hook(*a):\n    return None\n",
    "search.py": "async def find(*a):\n    return {'items': []}\n",
    "render.py": "def render(doc):\n    return None\n",
    "events.py": "def handle(state, event_type, data):\n    return state\n",
}

# One correct entry for every slot. "{inner}" names the module's own package.
_VALID = {
    "nav": [{"key": "acme", "label": "Acme", "href": "/acme", "order": 40.5, "group": None},
            {"key": "acme-more", "label_key": "nav.acme", "href": "/acme/more", "order": 41,
             "group": "Acme"}],
    "search_provider": {"handler": "{inner}.search:find", "result_key": "items",
                        "permission": "view_inventory"},
    "bulk_action": [{"label": "Go", "form_action": "/acme/go", "action_type": "navigate",
                     "requires_connector": ""}],
    "item_action": [{"label": "Open", "href_template": "/acme/{entity_id}"}],
    "doc_detail_actions": [{"render": "{inner}.render:render"}],
    "doc_detail_badges": [{"render": "{inner}.render:render"}],
    "category_schema": [{"category": "Rings", "fields": [
        {"key": "size", "label": "Size", "type": "select", "options": ["5", "6"]}]}],
    "on_company_created": [{"handler": "{inner}.hooks:hook"}],
    "on_modules_ready": [{"handler": "{inner}.hooks:hook"}],
    "send_to_targets": [{"label": "Quote", "doc_type": "quotation", "statuses": ["in_stock"]}],
    "catalog_channel": [{"id": "shop", "label": "Shop", "marker": "S", "can_create": True,
                         "write_permission": "edit_inventory", "requires_connector": "shop"}],
    "projection_handler": [{"prefix": "acme.", "handler": "{inner}.events:handle"}],
    "pricing_action": [{"label": "Set", "href_template": "/acme/{entity_id}/{price_list}",
                        "show_on": ["sell"]}],
    "doc_finalize_hook": [{"handler": "{inner}.hooks:hook"}],
    "on_doc_payment": [{"handler": "{inner}.hooks:hook"}],
}

# (slot, the entry's wrong key and value, the text the refusal names).
_WRONG = [
    ("nav", {"order": "1"}, "order"),
    ("nav", {"order": True}, "order"),
    ("nav", {"order": None}, "order"),
    ("nav", {"key": ["acme"]}, "key"),
    ("nav", {"key": 7}, "key"),
    ("nav", {"group": {"a": 1}}, "group"),
    ("nav", {"group": 3}, "group"),
    ("nav", {"label": 5}, "label"),
    ("nav", {"label_key": ["nav.acme"]}, "label_key"),
    ("bulk_action", {"action_type": "navgate"}, "action_type"),
    ("bulk_action", {"requires_connector": ["shop"]}, "requires_connector"),
    ("bulk_action", {"label": {"en": "Go"}}, "label"),
    ("send_to_targets", {"doc_type": None}, "doc_type"),
    ("send_to_targets", {"doc_type": ""}, "doc_type"),
    ("send_to_targets", {"doc_type": 3}, "doc_type"),
    ("catalog_channel", {"id": None}, "id"),
    ("catalog_channel", {"id": 7}, "id"),
    ("catalog_channel", {"marker": 1}, "marker"),
    ("catalog_channel", {"can_create": "yes"}, "can_create"),
    ("category_schema", {"category": None}, "category"),
    ("category_schema", {"category": ["Rings"]}, "category"),
    ("category_schema", {"fields": None}, "fields"),
    ("category_schema", {"fields": {"key": "size"}}, "fields"),
    ("category_schema", {"fields": ["size"]}, "fields"),
    ("category_schema", {"fields": [{"label": "Size"}]}, "fields"),
    ("category_schema", {"fields": [{"key": 3}]}, "fields"),
    ("category_schema", {"fields": [{"key": "size", "options": "5,6"}]}, "fields"),
    ("item_action", {"label": 3}, "label"),
    ("pricing_action", {"show_on": [{}]}, "show_on"),
    ("pricing_action", {"show_on": [["sell"]]}, "show_on"),
    ("projection_handler", {"prefix": 1}, "prefix"),
    ("doc_detail_actions", {"render": None}, "module.path:function"),
    ("on_doc_payment", {"handler": 5}, "module.path:function"),
]


@pytest.fixture
def module_dir(tmp_path, monkeypatch):
    base = tmp_path / "modules"
    base.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(base))
    before_path, before_mods = list(sys.path), dict(sys.modules)
    slots.clear()
    yield base
    slots.clear()
    loader._loaded.clear()
    loader._load_errors.clear()
    sys.path[:] = before_path
    for key in set(sys.modules) - set(before_mods):
        sys.modules.pop(key, None)


def _write(base: Path, slots_manifest: dict) -> str:
    name, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
    manifest = {"name": name, "version": "1.0.0",
                "slots": ast.literal_eval(repr(slots_manifest).replace("{inner}", inner))}
    pkg = base / name
    (pkg / inner).mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
    (pkg / inner / "__init__.py").write_text("")
    for rel, body in _CODE.items():
        (pkg / inner / rel).write_text(body)
    return name


def test_every_slot_has_its_entry_rules():
    assert set(loader._SLOT_ENTRY_KEYS) == slots.SLOT_NAMES
    assert set(_VALID) == slots.SLOT_NAMES


def test_module_filling_every_slot_correctly_loads(module_dir):
    name = _write(module_dir, _VALID)

    admission = loader.admit_modules(str(module_dir), {name})
    assert admission.refused == {}
    loader.load_all(str(module_dir), {name}, admission=admission)
    assert loader.is_running(name), loader.load_errors()


@pytest.mark.parametrize("slot,wrong,reason", _WRONG,
                         ids=[f"{s}-{next(iter(w))}={next(iter(w.values()))!r}" for s, w, _ in _WRONG])
def test_entry_a_slot_cannot_read_is_refused_before_module_code_runs(module_dir, slot, wrong, reason):
    contribution = _VALID[slot]
    if isinstance(contribution, list):
        contribution = [{**contribution[0], **wrong}]
    else:
        contribution = {**contribution, **wrong}
    name = _write(module_dir, {slot: contribution})

    admission = loader.admit_modules(str(module_dir), {name})

    assert admission.admitted == []
    assert reason in admission.refused[name], admission.refused

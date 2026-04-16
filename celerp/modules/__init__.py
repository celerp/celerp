# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Celerp module system — public package.

Three public surfaces:

  celerp.modules.loader    — scans DATA_DIR/modules/, imports, registers slots
  celerp.modules.slots     — slot registry (get/register)
  celerp.modules.registry  — enabled/disabled state (persisted to company settings)
  celerp.modules.api       — PUBLIC API for module authors (AI query, etc.)

Module authors: import ONLY from celerp.modules.api. Do NOT import from
celerp.ai.*, celerp.session_gate, or any other celerp.* internal. The loader
will reject your module if it does. See https://celerp.com/docs/modules/ai-api
"""
from celerp.modules.loader import load_all, register_api_routes, register_ui_routes
from celerp.modules.slots import get as get_slot, register as register_slot

__all__ = [
    "load_all",
    "register_api_routes",
    "register_ui_routes",
    "get_slot",
    "register_slot",
]

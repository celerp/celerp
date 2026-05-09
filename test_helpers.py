# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared test utilities importable from any test location.

These are pure functions (no pytest fixtures). Import directly:
    from tests.helpers import make_test_token, authed_cookies, REPO_ROOT
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

# Repo root: one level above this file (tests/helpers.py → repo root)
REPO_ROOT = Path(__file__).resolve().parent.parent

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

_crm_src = os.path.join(os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")
_crm_available = os.path.isfile(os.path.join(_crm_src, "celerp_sales_funnel", "__init__.py"))


def make_test_token(
    role: str = "owner",
    user_id: str = "00000000-0000-0000-0000-000000000001",
    company_id: str = "00000000-0000-0000-0000-000000000002",
) -> str:
    """Create a minimal JWT cookie value with a decodable payload for UI role checks.

    NOT cryptographically signed - only used so that get_role() can decode
    the role claim from the base64 payload. Signature verification never runs in tests.
    """
    payload = json.dumps({"sub": user_id, "company_id": company_id, "role": role, "exp": 9999999999})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"header.{payload_b64}.sig"


def authed_cookies(role: str = "owner") -> dict:
    """Return cookies dict with a properly-formed test token for the given role."""
    return {"celerp_token": make_test_token(role=role)}

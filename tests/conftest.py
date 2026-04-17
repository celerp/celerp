# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import os

# Must be set before celerp.config is imported (JWT guard fires at module load).
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from celerp.db import get_session
from celerp.main import app
from ui.app import app as _ui_app

import sys as _sys, os as _os

# Register inventory module routes onto the test app.
_inv_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-inventory")
if _os.path.abspath(_inv_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_inv_src))
from celerp_inventory.routes import setup_api_routes as _setup_inv
from celerp_inventory.ui_routes import setup_ui_routes as _setup_inv_ui
_setup_inv(app)
_setup_inv_ui(_ui_app)

# Register Contacts module routes onto the test app.
_contacts_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-contacts")
if _os.path.abspath(_contacts_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_contacts_src))
from celerp_contacts.routes import setup_api_routes as _setup_contacts
from celerp_contacts.ui_routes import setup_ui_routes as _setup_contacts_ui
_setup_contacts(app)
_setup_contacts_ui(_ui_app)

# Register sales-funnel (deals) module routes onto the test app (if available).
_crm_src = _os.path.join(_os.path.dirname(__file__), "..", "premium_modules", "celerp-sales-funnel")
_crm_available = _os.path.isfile(_os.path.join(_crm_src, "celerp_sales_funnel", "__init__.py"))
if _crm_available:
    if _os.path.abspath(_crm_src) not in [_os.path.abspath(p) for p in _sys.path]:
        _sys.path.insert(0, _os.path.abspath(_crm_src))
    from celerp_sales_funnel.routes import setup_api_routes as _setup_crm
    from celerp_sales_funnel.ui_routes import setup_ui_routes as _setup_crm_ui
    _setup_crm(app)
    _setup_crm_ui(_ui_app)

# Register manufacturing module routes + projection handler onto the test app.
_mfg_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-manufacturing")
if _os.path.abspath(_mfg_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_mfg_src))
from celerp_manufacturing.routes import setup_api_routes as _setup_mfg
from celerp_manufacturing.ui_routes import setup_ui_routes as _setup_mfg_ui
_setup_mfg(app)
_setup_mfg_ui(_ui_app)

# Register connectors module routes onto the test app.
_conn_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-connectors")
if _os.path.abspath(_conn_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_conn_src))
from celerp_connectors.routes import setup_api_routes as _setup_connectors
_setup_connectors(app)

# Register docs module routes onto the test app.
_docs_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-docs")
if _os.path.abspath(_docs_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_docs_src))
from celerp_docs.api_setup import setup_api_routes as _setup_docs
from celerp_docs.ui_routes import setup_ui_routes as _setup_docs_ui
_setup_docs(app)
_setup_docs_ui(_ui_app)

# Register accounting module routes onto the test app.
_acc_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-accounting")
if _os.path.abspath(_acc_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_acc_src))
from celerp_accounting.api_setup import setup_api_routes as _setup_accounting
from celerp_accounting.ui_routes import setup_ui_routes as _setup_accounting_ui
_setup_accounting(app)
_setup_accounting_ui(_ui_app)

# Register reconciliation UI routes onto the test app.
from ui.routes.reconciliation import setup_routes as _setup_recon_ui
_setup_recon_ui(_ui_app)

# Register subscriptions module routes onto the test app.
_subs_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-subscriptions")
if _os.path.abspath(_subs_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_subs_src))
from celerp_subscriptions.routes import setup_api_routes as _setup_subs
from celerp_subscriptions.ui_routes import setup_ui_routes as _setup_subs_ui
from celerp_subscriptions.ui_routes_import import setup_ui_routes as _setup_subs_import_ui
_setup_subs(app)
_setup_subs_ui(_ui_app)
_setup_subs_import_ui(_ui_app)

# Register reports module routes onto the test app.
_rep_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-reports")
if _os.path.abspath(_rep_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_rep_src))
from celerp_reports.api_setup import setup_api_routes as _setup_reports
from celerp_reports.ui_routes import setup_ui_routes as _setup_reports_ui
_setup_reports(app)
_setup_reports_ui(_ui_app)

# Register verticals module routes onto the test app.
_vert_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-verticals")
if _os.path.abspath(_vert_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_vert_src))
from celerp_verticals.routes import setup_api_routes as _setup_verticals
_setup_verticals(app)

# Register dashboard module routes onto the test app.
_dash_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-dashboard")
if _os.path.abspath(_dash_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_dash_src))
from celerp_dashboard.setup import setup_api_routes as _setup_dashboard
_setup_dashboard(app)

# Register AI module routes onto the test app.
_ai_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-ai")
if _os.path.abspath(_ai_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_ai_src))
from celerp_ai.setup import setup_api_routes as _setup_ai
_setup_ai(app)

# Register backup module routes onto the test app.
_backup_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-backup")
if _os.path.abspath(_backup_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_backup_src))
from celerp_backup.setup import setup_api_routes as _setup_backup
_setup_backup(app)

# Register admin module routes onto the test app.
_admin_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-admin")
if _os.path.abspath(_admin_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_admin_src))
from celerp_admin.setup import setup_api_routes as _setup_admin
_setup_admin(app)

# Register labels module routes onto the test app.
_labels_src = _os.path.join(_os.path.dirname(__file__), "..", "default_modules", "celerp-labels")
if _os.path.abspath(_labels_src) not in [_os.path.abspath(p) for p in _sys.path]:
    _sys.path.insert(0, _os.path.abspath(_labels_src))
from celerp_labels.routes import setup_api_routes as _setup_labels
_setup_labels(app)

# Register warehousing module path so its UI components can be imported in tests.
_wh_src = _os.path.join(_os.path.dirname(__file__), "..", "premium_modules", "celerp-warehousing")
_wh_available = _os.path.isfile(_os.path.join(_wh_src, "celerp_warehousing", "__init__.py"))
if _wh_available:
    if _os.path.abspath(_wh_src) not in [_os.path.abspath(p) for p in _sys.path]:
        _sys.path.insert(0, _os.path.abspath(_wh_src))
    # Register warehousing API routes onto the test app.
    from celerp_warehousing.routes import router as _wh_router
    app.include_router(_wh_router, prefix="/warehousing", tags=["warehousing"])

_SLOT_CONTRIBUTIONS = [
    # --- nav slots (mirrors what modules register at load time) ---
    {"slot": "nav", "contrib": {"group": None, "key": "dashboard", "href": "/dashboard", "label": "Dashboard", "order": 1, "_module": "celerp-dashboard"}},
    {"slot": "nav", "contrib": {"group": "Sales Documents", "key": "docs", "href": "/docs", "label": "Documents", "order": 20, "_module": "celerp-docs"}},
    {"slot": "nav", "contrib": {"group": "Sales Documents", "key": "lists", "href": "/lists", "label": "Lists", "order": 21, "_module": "celerp-docs"}},
    {"slot": "nav", "contrib": {"group": "Sales", "key": "subscriptions", "href": "/subscriptions", "label": "Subscriptions", "order": 25, "_module": "celerp-subscriptions"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "inventory", "href": "/inventory", "label": "Inventory", "order": 30, "settings_href": "/settings/inventory", "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "scanning", "href": "/scanning", "label": "Scanning", "order": 31, "_module": "celerp-inventory"}},
    {"slot": "nav", "contrib": {"group": "Finance", "key": "accounting", "href": "/accounting", "label": "Accounting", "order": 50, "settings_href": "/settings/accounting", "_module": "celerp-accounting"}},
    {"slot": "nav", "contrib": {"group": "Finance", "key": "reports", "href": "/reports", "label": "Reports", "order": 51, "_module": "celerp-reports"}},
    {"slot": "nav", "contrib": {"group": "Inventory", "key": "manufacturing", "href": "/manufacturing", "label": "Manufacturing", "order": 40, "_module": "celerp-manufacturing"}},
    # --- projection_handler slots ---
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "doc.",
            "handler": "celerp_docs.doc_projections:apply_documents_event",
            "_module": "celerp-docs",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "list.",
            "handler": "celerp_docs.doc_projections:apply_documents_event",
            "_module": "celerp-docs",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "item.",
            "handler": "celerp_inventory.projections:apply_item_event",
            "_module": "celerp-inventory",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "crm.contact.",
            "handler": "celerp_contacts.projections:apply_contact_event",
            "_module": "celerp-contacts",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "crm.memo.",
            "handler": "celerp_contacts.projections:apply_contact_event",
            "_module": "celerp-contacts",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "acc.",
            "handler": "celerp_accounting.projections:apply_accounting_event",
            "_module": "celerp-accounting",
        },
    },
    {
        "slot": "on_company_created",
        "contrib": {
            "handler": "celerp_accounting.routes:seed_chart_of_accounts_hook",
            "_module": "celerp-accounting",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "sub.",
            "handler": "celerp_subscriptions.projection_handler:apply_subscription_event",
            "_module": "celerp-subscriptions",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "mfg.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        },
    },
    {
        "slot": "projection_handler",
        "contrib": {
            "prefix": "bom.",
            "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event",
            "_module": "celerp-manufacturing",
        },
    },
]

# If celerp-sales-funnel is installed, add the deal projection handler.
if _crm_available:
    _SLOT_CONTRIBUTIONS.append({
        "slot": "projection_handler",
        "contrib": {
            "prefix": "crm.deal.",
            "handler": "celerp_sales_funnel.projections:apply_deal_event",
            "_module": "celerp-sales-funnel",
        },
    })


def _ensure_slots() -> None:
    """Ensure all module projection handler slots are registered.

    Called before every test so that tests which call slots.clear() don't
    leave the projection engine unable to handle events.
    """
    from celerp.modules.slots import get, register
    for entry in _SLOT_CONTRIBUTIONS:
        slot = entry["slot"]
        contrib = entry["contrib"]
        registered = get(slot)
        dedup_key = contrib.get("prefix") or contrib.get("href")
        existing_keys = {c.get("prefix") or c.get("href") for c in registered}
        if dedup_key not in existing_keys:
            register(slot, contrib)


@pytest.fixture(autouse=True)
def _disable_rate_limits():
    from celerp.routers.auth import limiter as auth_limiter

    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    auth_limiter.enabled = False
    auth_limiter._storage.reset()
    yield
    app.state.limiter.enabled = False
    app.state.limiter._storage.reset()
    auth_limiter.enabled = False
    auth_limiter._storage.reset()


@pytest.fixture(autouse=True)
def _ensure_slot_registration():
    """Re-register module projection handler slots before each test."""
    _ensure_slots()
    yield
    _ensure_slots()


@pytest.fixture(autouse=True)
def _mock_get_modules_default():
    """Default get_modules mock — returns empty list so settings page always has a valid response."""
    from unittest.mock import patch, AsyncMock
    with patch("ui.api_client.get_modules", new=AsyncMock(return_value=[])):
        yield


from celerp.models.base import Base

DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def make_test_token(role: str = "owner", user_id: str = "00000000-0000-0000-0000-000000000001", company_id: str = "00000000-0000-0000-0000-000000000002") -> str:
    """Create a minimal JWT cookie value with a decodable payload for UI role checks.

    NOT cryptographically signed - only used so that `get_role()` can decode
    the role claim from the base64 payload. API calls in tests are mocked,
    so signature verification never runs on this token.
    """
    import base64
    import json
    payload = json.dumps({"sub": user_id, "company_id": company_id, "role": role, "exp": 9999999999})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    return f"header.{payload_b64}.sig"


def authed_cookies(role: str = "owner") -> dict:
    """Return cookies dict with a properly-formed test token for the given role."""
    return {"celerp_token": make_test_token(role=role)}


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def client(session: AsyncSession):
    from httpx import ASGITransport, AsyncClient
    from celerp.services.session_tracker import clear as _clear_tracker
    from celerp.gateway.state import set_session_token as _set_session_token
    from unittest.mock import patch, MagicMock

    _clear_tracker()
    _saved_token = None
    try:
        from celerp.gateway.state import get_session_token
        _saved_token = get_session_token()
    except Exception:
        pass
    _set_session_token("")  # ensure clean gateway state
    app.dependency_overrides[get_session] = lambda: session
    # Simulate a connected gateway so direct_connection_limit gate does not fire in tests.
    # We patch the underlying _client variable (not get_client) so that inner tests can
    # still override it to None when specifically testing the gate behavior.
    with patch("celerp.gateway.client._client", MagicMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()
    _clear_tracker()
    _set_session_token(_saved_token or "")

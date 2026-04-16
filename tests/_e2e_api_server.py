"""API server for E2E test. Run as: python _e2e_api_server.py"""
import os, sys

os.environ["ALLOW_INSECURE_JWT"] = "true"
DB_FILE = os.environ.get("E2E_DB_FILE", "/tmp/celerp_e2e_test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE}"
API_PORT = int(os.environ.get("E2E_API_PORT", "18950"))

CELERP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(CELERP_DIR)
sys.path.insert(0, CELERP_DIR)

for mod_dir in [
    "default_modules/celerp-inventory", "default_modules/celerp-contacts",
    "default_modules/celerp-manufacturing", "default_modules/celerp-connectors",
    "default_modules/celerp-docs", "default_modules/celerp-accounting",
    "default_modules/celerp-subscriptions", "default_modules/celerp-reports",
    "default_modules/celerp-verticals", "default_modules/celerp-dashboard",
    "default_modules/celerp-ai", "default_modules/celerp-backup",
    "default_modules/celerp-admin", "default_modules/celerp-labels",
]:
    p = os.path.join(CELERP_DIR, mod_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

# Import and configure app (must happen before create_tables so models are registered)
from celerp.main import app

from celerp_inventory.routes import setup_api_routes
from celerp_contacts.routes import setup_api_routes as s1b
from celerp_manufacturing.routes import setup_api_routes as s3
from celerp_connectors.routes import setup_api_routes as s4
from celerp_docs.api_setup import setup_api_routes as s5
from celerp_accounting.api_setup import setup_api_routes as s6
from celerp_subscriptions.routes import setup_api_routes as s7
from celerp_reports.api_setup import setup_api_routes as s8
from celerp_verticals.routes import setup_api_routes as s9
from celerp_dashboard.setup import setup_api_routes as s10
from celerp_ai.setup import setup_api_routes as s11
from celerp_backup.setup import setup_api_routes as s12
from celerp_admin.setup import setup_api_routes as s13
from celerp_labels.routes import setup_api_routes as s14

setup_api_routes(app); s1b(app); s3(app); s4(app); s5(app)
s6(app); s7(app); s8(app); s9(app); s10(app)
s11(app); s12(app); s13(app); s14(app)

app.state.limiter.enabled = False

# Create tables (after all imports so models are registered)
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from celerp.models.base import Base

engine = create_async_engine(os.environ["DATABASE_URL"])

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

asyncio.run(create_tables())

# Register projection handlers
from celerp.modules.slots import register
for c in [
    {"prefix": "item.", "handler": "celerp_inventory.projections:apply_item_event"},
    {"prefix": "doc.", "handler": "celerp_docs.doc_projections:apply_documents_event"},
    {"prefix": "crm.contact.", "handler": "celerp_contacts.projections:apply_contact_event"},
    {"prefix": "crm.memo.", "handler": "celerp_contacts.projections:apply_contact_event"},
    {"prefix": "acc.", "handler": "celerp_accounting.projections:apply_accounting_event"},
    {"prefix": "sub.", "handler": "celerp_subscriptions.projection_handler:apply_subscription_event"},
    {"prefix": "mfg.", "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event"},
    {"prefix": "bom.", "handler": "celerp_manufacturing.projection_handler:apply_manufacturing_event"},
]:
    register("projection_handler", c)

# Monkey-patch system restart to no-op
from celerp.routers import system as _sm
_sm._send_sigterm = lambda: None

# Ensure config dir exists
os.makedirs(os.path.expanduser("~/.config/celerp"), exist_ok=True)

import uvicorn
print(f"API server starting on :{API_PORT}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=API_PORT, log_level="warning")

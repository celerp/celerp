"""UI server for E2E test. Run as: python _e2e_ui_server.py"""
import os, sys

os.environ["ALLOW_INSECURE_JWT"] = "true"
DB_FILE = os.environ.get("E2E_DB_FILE", "/tmp/celerp_e2e_test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_FILE}"
API_PORT = int(os.environ.get("E2E_API_PORT", "18950"))
UI_PORT = int(os.environ.get("E2E_UI_PORT", "18951"))
os.environ["API_URL"] = f"http://127.0.0.1:{API_PORT}"
os.environ["CELERP_API_URL"] = f"http://127.0.0.1:{API_PORT}"

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

from ui.app import app

from celerp_inventory.ui_routes import setup_ui_routes
from celerp_contacts.ui_routes import setup_ui_routes as u1b
from celerp_docs.ui_routes import setup_ui_routes as u4
from celerp_accounting.ui_routes import setup_ui_routes as u5
from celerp_subscriptions.ui_routes import setup_ui_routes as u6
from celerp_reports.ui_routes import setup_ui_routes as u7
from celerp_ai.ui_routes import setup_ui_routes as u_ai
from ui.routes.reconciliation import setup_routes as u8

setup_ui_routes(app); u1b(app); u4(app); u5(app); u6(app); u7(app); u8(app); u_ai(app)

from celerp.modules.slots import register
for c in [
    {"group": None, "key": "dashboard", "href": "/dashboard", "label": "Dashboard", "order": 1},
    {"group": "Sales Documents", "key": "docs", "href": "/docs", "label": "Documents", "order": 20},
    {"group": "Inventory", "key": "inventory", "href": "/inventory", "label": "Inventory", "order": 30, "settings_href": "/settings/inventory"},
    {"group": "Finance", "key": "accounting", "href": "/accounting", "label": "Accounting", "order": 50},
    {"group": "AI", "key": "ai", "href": "/ai", "label": "AI Assistant", "order": 90, "min_role": "operator"},
]:
    register("nav", c)

import uvicorn
print(f"UI server starting on :{UI_PORT}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=UI_PORT, log_level="warning")

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An instance booted without Manufacturing, or without Accounting, starts cleanly and
shows nothing of the missing module, while inventory (and, without Manufacturing, the
books) work as before. Each case boots the real API and UI processes on a fresh database
with the module left out, the way a customer would run them."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg2
import pytest

from test_helpers import DATABASE_URL

REPO = Path(__file__).resolve().parents[1]
ALL = sorted(p.name for p in (REPO / "default_modules").iterdir() if (p / "__init__.py").exists())
# The bug-report form maps every area of the app, installed or not; it is not module UI.
_AREA_MAP = re.compile(r"AREA_ROUTES = \[.*?\];", re.S)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _database():
    parts = urlsplit(DATABASE_URL.replace("+asyncpg", ""))
    name = f"modoff_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                             password=parts.password, dbname=parts.path.lstrip("/"))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        try:
            yield DATABASE_URL.rsplit("/", 1)[0] + "/" + name
        finally:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


@contextmanager
def _booted(tmp_path, without: set[str]):
    """The API and UI, booted as separate processes with ``without`` left out."""
    enabled = ",".join(n for n in ALL if n not in without)
    with _database() as db:
        api_port, ui_port = _free_port(), _free_port()
        env = {**os.environ, "ALLOW_INSECURE_JWT": "true", "MODULE_DIR": "default_modules",
               "ENABLED_MODULES": enabled, "DATABASE_URL": db, "API_URL": f"http://127.0.0.1:{api_port}",
               "CELERP_API_URL": f"http://127.0.0.1:{api_port}", "TMPDIR": str(tmp_path)}
        logs = {k: open(tmp_path / f"{k}.log", "w+") for k in ("api", "ui")}
        procs = {k: subprocess.Popen([sys.executable, "-m", "uvicorn", app, "--port", str(port)], cwd=REPO,
                                     env=env, stdout=logs[k], stderr=subprocess.STDOUT)
                 for k, app, port in (("api", "celerp.main:app", api_port), ("ui", "ui.app:app", ui_port))}
        try:
            for k, url in (("api", f"http://127.0.0.1:{api_port}/auth/bootstrap-status"),
                           ("ui", f"http://127.0.0.1:{ui_port}/login")):
                for _ in range(120):
                    assert procs[k].poll() is None, (tmp_path / f"{k}.log").read_text()[-3000:]
                    try:
                        if httpx.get(url, timeout=2).status_code < 500:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    pytest.fail(f"{k} did not start: " + (tmp_path / f"{k}.log").read_text()[-3000:])
            r = httpx.post(f"http://127.0.0.1:{api_port}/auth/register", json={
                "company_name": "Shop", "email": "owner@shop.example", "name": "Owner", "password": "password123"})
            assert r.status_code == 200, r.text
            token = r.json()["access_token"]
            api = httpx.Client(base_url=f"http://127.0.0.1:{api_port}", headers={"Authorization": f"Bearer {token}"},
                               timeout=30)
            ui = httpx.Client(base_url=f"http://127.0.0.1:{ui_port}", cookies={"celerp_token": token}, timeout=30,
                             follow_redirects=True)
            yield api, ui, tmp_path
        finally:
            for p in procs.values():
                p.terminate()
            for p in procs.values():
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
            for f in logs.values():
                f.close()


def _item(api, sku: str, qty: float, cost: float = 0.0) -> str:
    r = api.post("/items", json={"sku": sku, "name": sku, "quantity": qty, "sell_by": "piece",
                                 "status": "available", "cost_total": cost})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _page(ui, path: str) -> str:
    r = ui.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[-2000:])
    return _AREA_MAP.sub("", r.text)


def _no_startup_errors(tmp_path) -> None:
    for k in ("api", "ui"):
        log = (tmp_path / f"{k}.log").read_text()
        assert "Traceback" not in log and " ERROR" not in log, log[-4000:]


@pytest.mark.timeout(240)
def test_without_manufacturing_the_app_shows_none_of_it_and_the_rest_works(tmp_path):
    with _booted(tmp_path, {"celerp-manufacturing"}) as (api, ui, logs):
        widget = _item(api, "WIDGET", 5, 50.0)
        # Nothing of Manufacturing is served, on the API or in the app.
        assert api.get("/manufacturing/runs").status_code == 404
        assert api.post(f"/manufacturing/items/{widget}/build", json={"quantity": 1}).status_code == 404
        assert ui.get("/manufacturing").status_code == 404
        for path in ("/dashboard", "/inventory", f"/inventory/{widget}", f"/inventory/{widget}?tab=manufacturing",
                     "/accounting", "/reports", "/settings"):
            html = _page(ui, path)
            assert "anufacturing" not in html.replace("anufacturer", ""), (path, re.findall(r".{80}anufacturing.{40}", html))
        # Inventory and the books work as before: stock moves, and the opening entry is booked.
        r = api.post(f"/items/{widget}/adjust", json={"new_qty": 3})
        assert r.status_code == 200, r.text
        assert api.get(f"/items/{widget}").json()["quantity"] == 3.0
        assert api.get("/accounting/trial-balance").status_code == 200
        assert "WIDGET" in _page(ui, "/inventory")
        _no_startup_errors(logs)


@pytest.mark.timeout(240)
def test_without_accounting_production_runs_every_step_and_takes_each_back(tmp_path):
    # Reports depends on Accounting, so it is left out with it.
    with _booted(tmp_path, {"celerp-accounting", "celerp-reports"}) as (api, ui, logs):
        assert api.get("/accounting/trial-balance").status_code == 404
        assert 'href="/accounting' not in _page(ui, "/inventory")
        gold = _item(api, "GOLD", 100, 1000.0)
        ring = _item(api, "RING", 0)
        r = api.put(f"/manufacturing/items/{ring}/recipe", json={
            "output_qty": 1, "components": [{"item_id": gold, "quantity": 10}], "labor": [], "overhead": []})
        assert r.status_code == 200, r.text
        run = api.post(f"/manufacturing/items/{ring}/build", json={"quantity": 2}).json()["id"]

        def act(step: str, **body):
            r = api.post(f"/manufacturing/{run}/{step}", json=body)
            assert r.status_code == 200, (step, r.text)
            return r.json()

        act("issue")
        assert api.get(f"/items/{gold}").json()["quantity"] == 80.0
        act("return", items=[{"item_id": gold, "quantity": 5}])
        assert api.get(f"/items/{gold}").json()["quantity"] == 85.0
        act("issue", items=[{"item_id": gold, "quantity": 5}])
        lot = act("receive", quantity=1)["lot_item_id"]
        act("undo-receipt", lot_item_id=lot)
        act("complete")
        act("reopen")
        lots = api.get(f"/manufacturing/{run}").json()["received_lots"]
        assert sum(api.get(f"/items/{lot}").json()["quantity"] for lot in lots) == 2.0
        for lot in lots:
            act("undo-receipt", lot_item_id=lot)
            assert api.get(f"/items/{lot}").json()["quantity"] == 0.0
        act("return")
        assert api.get(f"/items/{gold}").json()["quantity"] == 100.0
        act("cancel")
        assert api.get(f"/manufacturing/{run}").json()["status"] == "cancelled"
        # The product page still offers Manufacturing, and inventory works.
        assert "tab=manufacturing" in _page(ui, f"/inventory/{ring}")
        assert "GOLD" in _page(ui, "/inventory")
        _no_startup_errors(logs)

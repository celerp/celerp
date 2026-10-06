# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Business records for the packaged upgrade smoke, through the HTTP API of a
running copy. Standard library only, so it runs on a bare CI runner against
any release.

  python scripts/smoke_records.py seed http://127.0.0.1:PORT
      Registers the owner on fresh data, which writes the company's first ledger
      events, and creates a location. Exits 1 if either fails or the ledger is
      empty. Modules a fresh copy has not started yet are not needed.
  python scripts/smoke_records.py snapshot http://127.0.0.1:PORT
      Prints the version, every ledger event (id, type, entity), the locations,
      and the customers and items or the status their pages answer once
      Celerp has finished starting, as one JSON line. Two snapshots of the same data are equal when nothing was lost or
      changed, including which of those pages the company is served.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

EMAIL = "owner@example.com"
PASSWORD = "Smoke-Upgrade-Password-1"


def http(method: str, url: str, token: str | None = None, body: dict | None = None,
         timeout: float = 30) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def module_get(api: str, path: str, token: str, timeout: float = 120) -> tuple[int, dict]:
    """GET a module route. /health/ready answers once the database does, and
    module routes answer 503 "still starting" until the UI process has reported
    the modules it started; ask again, as that says. Raises RuntimeError when
    the route is still starting after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        status, body = http("GET", api + path, token)
        if not (status == 503 and isinstance(body, dict) and "still starting" in str(body.get("detail"))):
            return status, body
        if time.monotonic() > deadline:
            raise RuntimeError(f"GET {path} still answers that Celerp is starting after {timeout:.0f} s")
        time.sleep(1)


def fail(why: str) -> None:
    print(why, file=sys.stderr)
    sys.exit(1)


def login(api: str) -> str:
    status, body = http("POST", api + "/auth/login-force", body={"email": EMAIL, "password": PASSWORD})
    if status != 200:
        fail(f"login: {status} {body}")
    return body["access_token"]


def seed(api: str) -> None:
    status, body = http("POST", api + "/auth/register", body={
        "company_name": "Smoke Trading", "email": EMAIL, "name": "Owner", "password": PASSWORD})
    if status != 200:
        fail(f"register: {status} {body}")
    token = body["access_token"]
    status, body = http("POST", api + "/companies/me/locations", token, {"name": "Smoke Warehouse", "type": "warehouse"})
    if status != 200:
        fail(f"POST /companies/me/locations: {status} {body}")
    status, body = http("GET", api + "/ledger?limit=1", token)
    if status != 200 or not body.get("total"):
        fail(f"GET /ledger after registering: {status} {body}")
    print(f"registered the owner ({body['total']} ledger events) and created a location")


def snapshot(api: str) -> None:
    token = login(api)
    _, health = http("GET", api + "/health")
    status, ledger = http("GET", api + "/ledger?limit=1000", token)
    if status != 200:
        fail(f"GET /ledger: {status} {ledger}")
    served = {}
    for path, field in (("/companies/me/locations", "name"), ("/crm/contacts", "name"), ("/items", "sku")):
        try:
            status, body = module_get(api, path, token)
        except RuntimeError as exc:
            fail(str(exc))
        rows = body if isinstance(body, list) else body.get("items", [])
        served[path] = sorted(r.get(field) for r in rows) if status == 200 else status
    print(json.dumps({
        "version": health.get("version"),
        "ledger": [[e["id"], e["event_type"], e["entity_id"]] for e in ledger.get("items", [])],
        "served": served,
    }, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("seed", "snapshot"):
        fail(__doc__)
    {"seed": seed, "snapshot": snapshot}[sys.argv[1]](sys.argv[2].rstrip("/"))

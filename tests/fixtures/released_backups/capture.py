"""Capture one company backup per demo business type from a released Celerp.

A release seeds demo data only into the company its first registration makes, so each
business type gets its own empty database. Run this file from the release's own checkout
(``STAGE=list`` prints the business types it offers); for each type, boot the release on
an empty database and run ``API=http://127.0.0.1:8000 VERTICAL=<type> STAGE=1``, restart
the app so the type's modules load, then run the same with ``STAGE=2
OUT=<this repo>/tests/fixtures/released_backups/<version>``. Stage 1 registers and sets the
business type through the release's own API; stage 2 loads that type's demo data and saves
the company's backup as ``<type>.zip``. Demo data only."""
import os
import time

import httpx

from celerp.services.vertical_presets import list_presets

STAGE = os.environ["STAGE"]
if STAGE == "list":
    print(" ".join(sorted(p["name"] for p in list_presets())))
    raise SystemExit
API, VERTICAL = os.environ["API"], os.environ["VERTICAL"]
EMAIL, PASSWORD = "owner@example.com", "capture-pw-12"
c = httpx.Client(base_url=API, timeout=300)


def ok(r: httpx.Response) -> dict:
    for _ in range(60):
        if r.status_code != 503:
            break
        time.sleep(2)
        r = c.send(r.request)
    assert r.status_code < 300, (r.request.method, r.request.url.path, r.status_code, r.text[:400])
    return r.json() if r.content else {}


if STAGE == "1":
    ok(c.post("/auth/register", json={"company_name": f"Demo {VERTICAL}", "email": EMAIL, "name": "Owner",
                                      "password": PASSWORD}))
c.headers["Authorization"] = "Bearer " + ok(c.post("/auth/login-force", json={"email": EMAIL, "password": PASSWORD}))["access_token"]
if STAGE == "1":
    ok(c.post("/companies/me/business-type", json={"vertical": VERTICAL}))
else:
    seeded = ok(c.post("/companies/me/demo/reseed"))
    assert seeded.get("ok") and seeded.get("replaced"), seeded
    r = c.get("/company-backups/download")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    with open(os.path.join(os.environ["OUT"], f"{VERTICAL}.zip"), "wb") as fh:
        fh.write(r.content)
    print(VERTICAL, seeded, len(r.content))

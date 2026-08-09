# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Guards for the OpenAPI schema exposure boundary and the grouped API reference.

Production must not publish the API shape: with default settings the schema route
is unregistered and /openapi.json returns 404. The schema is served only when the
expose setting is on (the press-kit capture harness and CI), and when served it
carries a description and tag groups, including the events router, so the reference
renders grouped and labelled.
"""

from __future__ import annotations

import logging
import os

# The JWT guard fires from celerp.config at import; keep it quiet.
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import celerp.main
from celerp.routers.events import router as events_router


def test_openapi_hidden_by_default():
    # The app is constructed at import under production defaults (expose off), so
    # no schema route is registered and the path 404s for everyone.
    assert celerp.main.app.openapi_url is None
    client = TestClient(celerp.main.app)
    assert client.get("/openapi.json").status_code == 404


def test_openapi_grouped_when_exposed(monkeypatch, caplog):
    # The events router carries its tag so its endpoints group under "events".
    assert events_router.tags == ["events"]

    monkeypatch.setattr(celerp.main.settings, "expose_openapi_schema", True)
    with caplog.at_level(logging.WARNING, logger="celerp.main"):
        kwargs = celerp.main._openapi_settings()

    assert kwargs["openapi_url"] == "/openapi.json"

    app = FastAPI(title="Celerp", docs_url=None, redoc_url=None, **kwargs)
    client = TestClient(app)
    res = client.get("/openapi.json")
    assert res.status_code == 200

    schema = res.json()
    assert schema["info"].get("description"), "served schema has no info.description"
    tag_names = {t["name"] for t in schema.get("tags", [])}
    assert "events" in tag_names, f"events tag group missing from {sorted(tag_names)}"

    # Turning the schema on must not be silent in the server logs.
    assert any(
        "openapi" in r.message.lower() and r.levelno >= logging.WARNING
        for r in caplog.records
    ), "no warning emitted when the public OpenAPI schema is exposed"

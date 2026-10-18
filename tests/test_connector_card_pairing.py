"""The Celerp connector card: camera capture, the accountant link, and one pasted pairing link."""
from __future__ import annotations

import base64
import json
import re
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fasthtml.common import to_xml

from ui import api_client
from ui.routes import settings_connectors as routes

LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"
ADDRESS = "https://firm.example.com"
KEY = f"cp1.{uuid.uuid4()}.{uuid.uuid4()}.{secrets.token_urlsafe(32)}"


class FormData(dict):
    def getlist(self, name):
        return list(self.get(name, ["contacts"])) if name == "scopes" else []


class Request:
    def __init__(self, data=None):
        self.state = SimpleNamespace()
        self.cookies = {}
        self.headers = {}
        self._form = FormData(data or {})

    async def form(self):
        return self._form


def _endpoints():
    app = FastAPI()
    routes.setup_routes(app)
    return {(route.path, tuple(sorted(route.methods))): route.endpoint for route in app.routes}


def _endpoint(path, method="POST"):
    return next(fn for (p, methods), fn in _endpoints().items() if p == path and method in methods)


def _celerp_card(**kw):
    return to_xml(routes._connector_card(
        {"id": "celerp", "name": "Accountant Sync", "auth_type": "api_key",
         "category": "accounting", "entities": ["contacts"], **kw},
        None, "", "", lang="en"))


def test_doc_scan_offers_camera_capture():
    """A phone takes the receipt photo straight from the document files section."""
    from ui.components.files import files_section

    html = to_xml(files_section("doc", "doc:1", [], base_url="/docs/doc:1/files"))
    camera = re.search(r'<input[^>]*id="file-camera-input-[^"]+"[^>]*>', html)
    assert camera, "no camera input"
    assert 'accept="image/*"' in camera.group(0) and 'capture="environment"' in camera.group(0)
    assert "Take a photo" in html
    # The ordinary picker still offers every file type.
    picker = re.search(r'<input[^>]*id="file-drop-input-[^"]+"[^>]*>', html).group(0)
    assert "capture" not in picker
    assert "file-camera-input-" in html.split("<script>", 1)[1]


def test_send_page_to_accountant_link():
    """The Celerp connector card carries one link a client sends to the accountant."""
    card = _celerp_card()
    assert 'href="https://celerp.com/accountants"' in card
    assert "Send this page to your accountant" in card
    woo = to_xml(routes._connector_card(
        {"id": "woocommerce", "name": "WooCommerce", "auth_type": "api_key"}, None, "", "", lang="en"))
    assert "celerp.com/accountants" not in woo


def test_pairing_link_round_trip():
    from celerp.services.peer_access import pairing_link, parse_pairing_link

    link = pairing_link(ADDRESS, KEY)
    assert parse_pairing_link(f"  {link}\n") == (ADDRESS, KEY)
    plain = base64.urlsafe_b64encode(b"http://firm.example.com").decode().rstrip("=")
    for bad in ("", KEY, ADDRESS, link + "!", link.replace("cplink1", "cplink2"),
                f"cplink1.{plain}.{KEY}"):
        with pytest.raises(ValueError):
            parse_pairing_link(bad)


def test_connector_card_takes_one_pairing_link():
    """The client pastes one link; there is no typed address or key."""
    card = _celerp_card()
    assert 'name="pairing_link"' in card
    assert 'name="store_url"' not in card and 'name="consumer_key"' not in card
    assert "Receiving company key" not in card and "accountant.example.com" not in card


@pytest.mark.asyncio
async def test_connect_with_pairing_link_stores_address_and_key(monkeypatch):
    from celerp.services.peer_access import pairing_link

    stored = []

    async def store(token, platform, key, secret, url):
        stored.append((platform, key, secret, url))
        raise RuntimeError("stored")  # the handler shows it; the connected card is tested elsewhere

    monkeypatch.setattr(routes, "_token", lambda request: "t")
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    monkeypatch.setattr(routes, "_request_company_id", lambda request: str(uuid.uuid4()))
    monkeypatch.setattr(api_client, "store_connector_credentials", store)
    monkeypatch.setattr(api_client, "delete_connector_credentials", store)
    handler = _endpoint("/settings/connectors/{platform}/connect-apikey")

    async def permission(request, key):
        return None
    monkeypatch.setattr(routes, "_check_permission", permission)

    bad = await handler(Request({"pairing_link": "not a link"}), "celerp")
    assert "Check the pairing link" in to_xml(bad)
    assert stored == []
    shown = await handler(Request({"pairing_link": pairing_link(ADDRESS, KEY)}), "celerp")
    assert "stored" in to_xml(shown)
    assert stored[0] == ("celerp", KEY, "", ADDRESS)


@pytest.mark.asyncio
async def test_issue_result_shows_pairing_link(monkeypatch):
    from celerp.config import settings
    from celerp.services.peer_access import pairing_code, parse_pairing_link

    async def permission(request, key):
        request.state.auth_company = {"id": str(uuid.uuid4()), "current_role": "owner"}
    monkeypatch.setattr(routes, "_token", lambda request: "t")
    monkeypatch.setattr(routes, "_check_permission", permission)
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    monkeypatch.setattr(api_client, "role_from_company", lambda data: data["current_role"])

    async def create(token, body):
        return {"credential": KEY}

    async def grants(token):
        return {"items": []}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    monkeypatch.setattr(api_client, "accountant_peer_grants", grants)
    monkeypatch.setattr(settings, "celerp_public_url", ADDRESS)

    form = to_xml(routes._grant_form("en", "Firm Practice"))
    assert 'name="address"' in form and f'value="{ADDRESS}"' in form
    assert 'name="firm"' in form and 'value="Firm Practice"' in form

    issue = _endpoint("/settings/accountant-access/grants")
    code = pairing_code(uuid.uuid4(), "Client")
    response = await issue(Request({"pairing_code": code, "label": "", "address": ADDRESS, "firm": "Firm Practice"}))
    body = response.body.decode()
    link = re.search(r'id="issued-pairing-link"[^>]*>([^<]+)<', body).group(1)
    assert parse_pairing_link(link) == (ADDRESS, KEY)
    assert 'data-copy="#issued-pairing-link"' in body

    refused = await issue(Request({"pairing_code": code, "label": "", "address": "http://x"}))
    assert "https://" in to_xml(refused)


def test_connector_help_copy_localized():
    """Every string on the accountant surfaces is translated; none are typed English."""
    keys = ["accountant_access.connector_source_help", "accountant_access.source_help",
            "accountant_access.pairing_link", "accountant_access.pairing_link_invalid",
            "accountant_access.address", "accountant_access.address_invalid",
            "accountant_access.copy_link", "connectors.send_to_accountant", "msg.take_photo"]
    en = json.loads((LOCALES / "en.json").read_text())
    for path in LOCALES.glob("*.json"):
        data = json.loads(path.read_text())
        for key in keys:
            assert data.get(key), f"{path.name} lacks {key}"
            if path.name != "en.json":
                assert data[key] != en[key], f"{path.name} {key} is English"
    for key in ("accountant_access.connector_source_help", "accountant_access.source_help"):
        for word in ("installation", "receiving company", "sending company", "scoped"):
            assert word not in en[key].lower(), f"{key} uses '{word}'"
    source = (Path(routes.__file__)).read_text()
    assert "Receiving company key" not in source and "accountant.example.com" not in source

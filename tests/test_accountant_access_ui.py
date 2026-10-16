"""Receiving-company access is owner-managed and credentials are displayed once."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from ui import api_client
from ui.routes import settings_connectors as routes

KEY = f"cp1.{uuid.uuid4()}.{uuid.uuid4()}.{'A' * 43}"


class FormData(dict):
    def getlist(self, name):
        return list(self.get("scopes", ["contacts", "products"])) if name == "scopes" else []


class Request:
    def __init__(self, data=None):
        self.state = SimpleNamespace()
        self._form = FormData({"address": "https://firm.example.com", **(data or {})})
    async def form(self):
        return self._form


def _code(source):
    from celerp.services.peer_access import pairing_code
    return pairing_code(uuid.UUID(str(source)), "Client company")


def _endpoints():
    app = FastAPI()
    routes.setup_routes(app)
    return {route.path: route.endpoint for route in app.routes}


def _authenticated(monkeypatch, *, role="owner"):
    async def permission(request, key):
        assert key == "manage_integrations"
        request.state.auth_company = {"id": str(uuid.uuid4()), "current_role": role}
        return None
    monkeypatch.setattr(routes, "_token", lambda request: "test-token")
    monkeypatch.setattr(routes, "_check_permission", permission)
    monkeypatch.setattr(routes, "get_lang", lambda request: "en")
    monkeypatch.setattr(api_client, "role_from_company", lambda data: data["current_role"])


@pytest.mark.asyncio
async def test_owner_receives_noncacheable_single_display_and_no_secret_in_listing(monkeypatch):
    _authenticated(monkeypatch)
    source = str(uuid.uuid4())
    issued = []
    async def create(token, body):
        assert token == "test-token"
        issued.append(body)
        return {"credential": KEY}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    handler = _endpoints()["/settings/accountant-access/grants"]
    request = Request({"pairing_code": _code(source), "label": "Client"})
    response = await handler(request)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert KEY.encode() in response.body
    assert b"shown only once" in response.body
    assert issued == [{"source_company_id": source, "label": "Client",
                       "scopes": ["contacts", "products"]}]


@pytest.mark.asyncio
async def test_member_cannot_issue_or_revoke_company_keys(monkeypatch):
    _authenticated(monkeypatch, role="staff")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Must not reach API")
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", forbidden)
    monkeypatch.setattr(api_client, "revoke_accountant_peer_grant", forbidden)
    import ui.security
    monkeypatch.setattr(ui.security, "not_permitted_redirect",
                        lambda request: Response("denied", status_code=403))
    ends = _endpoints()
    request = Request({"pairing_code": _code(str(uuid.uuid4())),
                       "label": "Client"})
    assert (await ends["/settings/accountant-access/grants"](request)).status_code == 403
    assert (await ends["/settings/accountant-access/grants/{grant_id}/revoke"](
        request, str(uuid.uuid4()))).status_code == 403
    assert (await ends["/settings/accountant-access"](request)).status_code == 403


def _grants(now):
    from datetime import timedelta
    return [
        {"id": str(uuid.uuid4()), "label": "Soon Co", "scopes": ["contacts", "inventory"],
         "source_company_id": str(uuid.uuid4()), "created_at": (now - timedelta(days=25)).isoformat(),
         "expires_at": (now + timedelta(days=3)).isoformat(), "revoked": False},
        {"id": str(uuid.uuid4()), "label": "Late Co", "scopes": ["documents"],
         "source_company_id": str(uuid.uuid4()), "created_at": (now - timedelta(days=40)).isoformat(),
         "expires_at": (now - timedelta(days=2)).isoformat(), "revoked": False},
        {"id": str(uuid.uuid4()), "label": "Old Co", "scopes": ["products"],
         "source_company_id": str(uuid.uuid4()), "created_at": (now - timedelta(days=50)).isoformat(),
         "expires_at": (now + timedelta(days=200)).isoformat(), "revoked": True},
        {"id": str(uuid.uuid4()), "label": "Year Co", "scopes": ["products"],
         "source_company_id": str(uuid.uuid4()), "created_at": (now - timedelta(days=20)).isoformat(),
         "expires_at": (now + timedelta(days=20)).isoformat(), "revoked": False},
    ]


def test_accountant_access_table_labels_no_reload():
    """Scopes read as record names, dates as dates, IDs stay out, and actions swap the table."""
    from datetime import datetime, timezone
    from fasthtml.common import to_xml

    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    grants = _grants(now)
    html = to_xml(routes._grants_table(grants, "en", now=now))
    assert 'id="accountant-access-grants"' in html
    assert "Contacts, Stock counts" in html and "contacts, inventory" not in html
    assert grants[0]["source_company_id"] not in html and "T00:00" not in html
    assert "2026-10-15" in html and "Expires in 3 days" in html and "badge--soon" in html
    assert "Expired" in html and "badge--expired" in html
    # Warned from 14 days before expiry; a key further out is simply active.
    assert "Expires in 20 days" in html and html.count("badge--soon") == 1
    assert "Revoked" in html
    assert html.count(">Renew key<") == 3 and html.count(">Revoke<") == 3
    assert "Extend" not in html
    assert "HX-Refresh" not in html and 'hx-target="#accountant-access-grants"' in html
    assert to_xml(routes._grants_table([], "en", now=now)).count("No client access keys") == 1


@pytest.mark.asyncio
async def test_key_renewal_and_expiry_warning_on_the_access_page(monkeypatch):
    """Renew key and Revoke answer with the refreshed table instead of reloading the page."""
    from datetime import datetime, timezone
    from fasthtml.common import to_xml

    _authenticated(monkeypatch)
    now = datetime.now(timezone.utc)
    grants = _grants(now)
    calls = []

    async def listing(token):
        return {"items": grants}

    async def renew(token, grant_id):
        calls.append(("renew", grant_id))
        return {}

    async def revoke(token, grant_id):
        calls.append(("revoke", grant_id))
        return {"revoked": True}

    monkeypatch.setattr(api_client, "accountant_peer_grants", listing)
    monkeypatch.setattr(api_client, "renew_accountant_peer_grant", renew)
    monkeypatch.setattr(api_client, "revoke_accountant_peer_grant", revoke)
    ends = _endpoints()
    gid = grants[1]["id"]
    for action in ("renew", "revoke"):
        response = await ends[f"/settings/accountant-access/grants/{{grant_id}}/{action}"](Request(), gid)
        body = to_xml(response) if not hasattr(response, "body") else response.body.decode()
        assert 'id="accountant-access-grants"' in body
        assert "hx-refresh" not in {k.lower() for k in getattr(response, "headers", {})}
    assert calls == [("renew", gid), ("revoke", gid)]


@pytest.mark.asyncio
async def test_issued_key_has_copy_and_refreshes_the_list(monkeypatch):
    from datetime import datetime, timezone
    _authenticated(monkeypatch)
    now = datetime.now(timezone.utc)

    async def create(token, body):
        return {"credential": KEY}

    async def listing(token):
        return {"items": _grants(now)}

    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    monkeypatch.setattr(api_client, "accountant_peer_grants", listing)
    response = await _endpoints()["/settings/accountant-access/grants"](Request({
        "pairing_code": _code(uuid.uuid4()), "label": "Client"}))
    body = response.body.decode()
    assert 'data-copy="#issued-pairing-link"' in body and 'id="issued-pairing-link"' in body
    assert 'hx-swap-oob="true"' in body and 'id="accountant-access-grants"' in body


@pytest.mark.asyncio
async def test_documents_scope_can_be_granted_without_contact_permission(monkeypatch):
    _authenticated(monkeypatch)
    seen = []
    async def create(token, body):
        seen.append(body)
        return {"credential": KEY}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    issue = _endpoints()["/settings/accountant-access/grants"]
    source = str(uuid.uuid4())
    response = await issue(Request({
        "pairing_code": _code(source),
        "label": "Finance documents only",
        "scopes": ["documents"],
    }))
    assert response.status_code == 200
    assert seen == [{"source_company_id": source, "label": "Finance documents only",
                     "scopes": ["documents"]}]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_accountant_grant_rejects_unapproved_or_empty_scope(monkeypatch):
    _authenticated(monkeypatch)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid grant must not call the API")
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", forbidden)
    issue = _endpoints()["/settings/accountant-access/grants"]
    source = str(uuid.uuid4())
    for scopes in ([], ["attachments"], ["contacts", "manage_users"]):
        response = await issue(Request({
            "pairing_code": _code(source), "label": "Test",
            "scopes": scopes,
        }))
        from fasthtml.common import to_xml
        assert "Check the pairing code" in to_xml(response)


@pytest.mark.asyncio
async def test_pairing_code_carries_company_id(monkeypatch):
    """The sending company copies one code; the receiving owner pastes it instead of a typed ID."""
    from fasthtml.common import to_xml

    from celerp.services.peer_access import pairing_code, parse_pairing_code

    source = uuid.uuid4()
    code = pairing_code(source, "Acme Trading Co.")
    assert parse_pairing_code(f"  {code}\n") == (source, "Acme Trading Co.")
    for bad in ("", str(source), "cpair1." + str(source)[:-1] + ".QQ", "cp1." + code[7:], code + "!"):
        with pytest.raises(ValueError):
            parse_pairing_code(bad)

    card = to_xml(routes._pairing_card(str(source), "Acme Trading Co.", "en"))
    assert code in card and 'data-copy="#peer-pairing-code"' in card
    assert ">Copy<" in card

    _authenticated(monkeypatch)
    seen = []
    async def create(token, body):
        seen.append(body)
        return {"credential": KEY}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    issue = _endpoints()["/settings/accountant-access/grants"]
    response = await issue(Request({"pairing_code": code, "label": ""}))
    assert response.status_code == 200
    assert seen == [{"source_company_id": str(source), "label": "Acme Trading Co.",
                     "scopes": ["contacts", "products"]}]

    response = await issue(Request({"pairing_code": str(source), "label": "Client"}))
    assert "Check the pairing code" in to_xml(response)
    assert len(seen) == 1

    page = routes._grant_form("en")
    # Keys last one year; there is no expiry to choose.
    assert 'name="lifetime_days"' not in to_xml(page)
    html = to_xml(page)
    assert 'name="pairing_code"' in html and 'name="source_company_id"' not in html
    assert "required" not in html


@pytest.mark.asyncio
async def test_scopes_derived_from_entities(monkeypatch):
    """Every entity the connector sends, stock counts included, can be authorized."""
    import typing

    from fasthtml.common import to_xml

    from celerp.connectors.celerp import CelerpConnector
    from celerp.routers.company_peers import Scope
    from celerp.services.peer_access import ENTITY_SCOPE, SCOPES

    sent = {name[len("sync_"):-len("_out")] for name in vars(CelerpConnector)
            if name.startswith("sync_") and name.endswith("_out")}
    assert sent == set(ENTITY_SCOPE)
    assert set(typing.get_args(Scope)) == SCOPES == set(ENTITY_SCOPE.values())

    html = to_xml(routes._grant_form("en"))
    for scope in SCOPES:
        assert f'value="{scope}"' in html
    assert "Stock counts" in html

    _authenticated(monkeypatch)
    seen = []
    async def create(token, body):
        seen.append(body)
        return {"credential": KEY}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    response = await _endpoints()["/settings/accountant-access/grants"](Request({
        "pairing_code": _code(uuid.uuid4()), "label": "Stock",
        "scopes": ["inventory"]}))
    assert response.status_code == 200
    assert seen[0]["scopes"] == ["inventory"]

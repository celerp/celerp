"""Receiving-company access is owner-managed and credentials are displayed once."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from ui import api_client
from ui.routes import settings_connectors as routes


class FormData(dict):
    def getlist(self, name):
        return list(self.get("scopes", ["contacts", "products"])) if name == "scopes" else []


class Request:
    def __init__(self, data=None):
        self.state = SimpleNamespace()
        self._form = FormData(data or {})
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
        return {"credential": "cp1.ONE-TIME-TEST-KEY"}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    handler = _endpoints()["/settings/accountant-access/grants"]
    request = Request({"pairing_code": _code(source), "label": "Client",
                       "lifetime_days": "30"})
    response = await handler(request)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert b"cp1.ONE-TIME-TEST-KEY" in response.body
    assert b"shown only once" in response.body
    assert issued == [{"source_company_id": source, "label": "Client",
                       "scopes": ["contacts", "products"], "lifetime_days": 30}]


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
                       "label": "Client", "lifetime_days": "14"})
    assert (await ends["/settings/accountant-access/grants"](request)).status_code == 403
    assert (await ends["/settings/accountant-access/grants/{grant_id}/revoke"](
        request, str(uuid.uuid4()))).status_code == 403
    assert (await ends["/settings/accountant-access"](request)).status_code == 403


@pytest.mark.asyncio
async def test_revoke_refreshes_access_list(monkeypatch):
    _authenticated(monkeypatch)
    revoked = []
    async def revoke(token, grant_id):
        revoked.append((token, grant_id))
        return {"revoked": True}
    monkeypatch.setattr(api_client, "revoke_accountant_peer_grant", revoke)
    id_ = str(uuid.uuid4())
    response = await _endpoints()["/settings/accountant-access/grants/{grant_id}/revoke"](
        Request(), id_)
    assert response.status_code == 204
    assert response.headers["hx-refresh"] == "true"
    assert revoked == [("test-token", id_)]



@pytest.mark.asyncio
async def test_documents_scope_can_be_granted_without_contact_permission(monkeypatch):
    _authenticated(monkeypatch)
    seen = []
    async def create(token, body):
        seen.append(body)
        return {"credential": "cp1.ONE-TIME-TEST-KEY"}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    issue = _endpoints()["/settings/accountant-access/grants"]
    source = str(uuid.uuid4())
    response = await issue(Request({
        "pairing_code": _code(source),
        "label": "Finance documents only",
        "lifetime_days": "14",
        "scopes": ["documents"],
    }))
    assert response.status_code == 200
    assert seen == [{"source_company_id": source, "label": "Finance documents only",
                     "scopes": ["documents"], "lifetime_days": 14}]
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
            "pairing_code": _code(source), "label": "Test", "lifetime_days": "14",
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
        return {"credential": "cp1.ONE-TIME-TEST-KEY"}
    monkeypatch.setattr(api_client, "issue_accountant_peer_grant", create)
    issue = _endpoints()["/settings/accountant-access/grants"]
    response = await issue(Request({"pairing_code": code, "label": "", "lifetime_days": "30"}))
    assert response.status_code == 200
    assert seen == [{"source_company_id": str(source), "label": "Acme Trading Co.",
                     "scopes": ["contacts", "products"], "lifetime_days": 30}]

    response = await issue(Request({"pairing_code": str(source), "label": "Client",
                                    "lifetime_days": "30"}))
    assert "Check the pairing code" in to_xml(response)
    assert len(seen) == 1

    page = routes._grant_form("en")
    html = to_xml(page)
    assert 'name="pairing_code"' in html and 'name="source_company_id"' not in html
    assert "required" not in html

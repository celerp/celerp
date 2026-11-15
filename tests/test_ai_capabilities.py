# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Generic OpenAPI capability compiler/executor tests."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import re
import sys
import textwrap

import pytest
from fastapi import Depends, FastAPI, Form, Query, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from celerp.ai import tools as ai_tools
from celerp.services import cost_visibility, journal_accounts, permissions
from test_helpers import real_agent_app


_bearer = HTTPBearer()


class _WriteBody(BaseModel):
    name: str
    idempotency_key: str | None = None


class _UnsafeWriteBody(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/things/{item_id}", openapi_extra={"x-celerp-agent": True})
    async def get_thing(
        item_id: str,
        q: list[str] = Query(default=[]),
        auth: HTTPAuthorizationCredentials = Depends(_bearer),
    ):
        return {"item_id": item_id, "q": q, "token": auth.credentials}

    @app.post("/things", openapi_extra={
        "x-celerp-agent": True, "x-celerp-agent-idempotent": True, "x-celerp-agent-key": "idempotency_key",
    })
    async def create_thing(
        body: _WriteBody,
        auth: HTTPAuthorizationCredentials = Depends(_bearer),
    ):
        return {"name": body.name, "idempotency_key": body.idempotency_key, "token": auth.credentials}

    @app.post("/unsafe", openapi_extra={"x-celerp-agent": True})
    async def unsafe_write(body: _UnsafeWriteBody):
        return {"name": body.name}

    @app.post(
        "/things/{item_id}/touch",
        openapi_extra={"x-celerp-agent": True, "x-celerp-agent-idempotent": True},
    )
    async def touch_thing(item_id: str):
        return {"touched": item_id}

    @app.post(
        "/preview",
        openapi_extra={"x-celerp-agent": True, "x-celerp-agent-confirm": False},
    )
    async def preview_thing(body: _UnsafeWriteBody):
        return {"preview": body.name}

    @app.delete("/things/{item_id}", openapi_extra={"x-celerp-agent": True})
    async def delete_thing(item_id: str):
        return {"deleted": item_id}

    @app.get("/text", openapi_extra={"x-celerp-agent": True}, response_class=PlainTextResponse)
    async def text_response():
        return "not json"

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and getattr(endpoint, "__name__", "") in {
            "get_thing", "create_thing", "unsafe_write", "delete_thing", "text_response", "touch_thing", "preview_thing"
        }:
            endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    return app


def test_compile_uses_openapi_and_fails_closed_for_unsafe_shapes():
    app = _app()
    compiled = ai_tools.compile_agent_capabilities(app, {})

    names = set(compiled)
    get_name = next(name for name in names if name.startswith("get_thing_"))
    post_name = next(name for name in names if name.startswith("create_thing_"))

    assert all("unsafe_write" not in name for name in names)
    assert all("delete_thing" not in name for name in names)
    assert all("text_response" not in name for name in names)

    get_params = compiled[get_name]["tool"]["function"]["parameters"]
    assert set(get_params["properties"]) == {"path", "query"}
    assert get_params["properties"]["path"]["required"] == ["item_id"]
    assert get_params["properties"]["query"]["properties"]["q"]["type"] == "array"

    post_body = compiled[post_name]["tool"]["function"]["parameters"]["properties"]["body"]
    assert "idempotency_key" not in post_body["properties"]
    assert compiled[post_name]["key_field"] == "idempotency_key"
    assert compiled[post_name]["keyed"] is True


def test_declared_idempotent_mutation_compiles_without_a_key():
    """A route that dedupes by construction declares itself idempotent with no key
    field; a mutation with no retry marker never compiles."""
    compiled = ai_tools.compile_agent_capabilities(_app(), {})
    touch_name = next(name for name in compiled if name.startswith("touch_thing_"))
    touch = compiled[touch_name]
    assert touch["method"] == "POST"
    assert touch["key_field"] is None
    assert touch["keyed"] is True
    assert touch["expects_body"] is False
    assert set(touch["tool"]["function"]["parameters"]["properties"]) == {"path", "after_previous"}
    assert all("unsafe_write" not in name for name in compiled)


def test_nonconfirming_post_preview_compiles_without_idempotency():
    compiled = ai_tools.compile_agent_capabilities(_app(), {})
    preview = next(cap for name, cap in compiled.items() if name.startswith("preview_thing_"))
    assert preview["method"] == "POST"
    assert preview["requires_confirmation"] is False
    assert preview["key_field"] is None


def test_compile_filters_existing_route_permissions(monkeypatch):
    app = FastAPI()

    async def permission_guard():
        return None

    permission_guard.required_permission = "view_secret"

    @app.get(
        "/guarded",
        dependencies=[Depends(permission_guard)],
        openapi_extra={"x-celerp-agent": True},
    )
    async def guarded():
        return {"ok": True}

    route = next(r for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == "guarded")
    route.endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None

    monkeypatch.setattr(ai_tools, "role_has_permission", lambda settings, role, permission: role == "owner")
    assert ai_tools.compile_agent_capabilities(app, {}, role="owner")
    assert not ai_tools.compile_agent_capabilities(app, {}, role="viewer")


def test_compile_filters_disabled_and_third_party_modules(monkeypatch):
    app = FastAPI()

    @app.get("/module-read", openapi_extra={"x-celerp-agent": True})
    async def module_read():
        return {"ok": True}

    route = next(r for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == "module_read")
    route.endpoint.__module__ = "example_module.routes"
    app.openapi_schema = None

    monkeypatch.setattr(
        ai_tools,
        "loaded_modules",
        lambda: [{"name": "celerp-example", "api_routes": "example_module.api_setup", "first_party": True}],
    )
    assert ai_tools.compile_agent_capabilities(app, {})
    assert not ai_tools.compile_agent_capabilities(app, {"enabled_modules": []})

    monkeypatch.setattr(
        ai_tools,
        "loaded_modules",
        lambda: [{"name": "celerp-example", "api_routes": "example_module.api_setup", "first_party": False}],
    )
    assert not ai_tools.compile_agent_capabilities(app, {})


async def test_executor_reenters_fastapi_with_user_auth_and_server_idempotency():
    app = _app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    get_cap = next(cap for name, cap in compiled.items() if name.startswith("get_thing_"))
    post_cap = next(cap for name, cap in compiled.items() if name.startswith("create_thing_"))

    got = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        get_cap,
        {"path": {"item_id": "item:1"}, "query": {"q": ["red", "blue"]}},
        "call-read",
    )
    assert got == {
        "ok": True,
        "status": 200,
        "data": {"item_id": "item:1", "q": ["red", "blue"], "token": "user-token"},
    }

    created = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        post_cap,
        {"body": {"name": "A", "idempotency_key": "model-controlled"}},
        "call-write",
    )
    assert created["ok"] is True
    assert created["data"]["name"] == "A"
    assert created["data"]["token"] == "user-token"
    assert created["data"]["idempotency_key"].startswith("agent:")
    assert created["data"]["idempotency_key"] != "model-controlled"


async def test_executor_rejects_request_infrastructure_and_path_escape():
    app = _app()
    cap = next(
        cap for name, cap in ai_tools.compile_agent_capabilities(app, {}).items()
        if name.startswith("get_thing_")
    )

    bad_section = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        cap,
        {"path": {"item_id": "item:1"}, "headers": {"Authorization": "Bearer attacker"}},
        "call-1",
    )
    assert bad_section["error"]["code"] == "invalid_arguments"

    bad_path = await ai_tools.execute_agent_capability(
        app,
        "Bearer user-token",
        cap,
        {"path": {"item_id": "../secret"}},
        "call-2",
    )
    assert bad_path["error"]["code"] == "invalid_path_parameter"


async def test_executor_bounds_json_result():
    app = FastAPI()

    @app.get("/big", openapi_extra={"x-celerp-agent": True})
    async def big():
        return {"value": "x" * 200}

    route = next(r for r in app.routes if getattr(getattr(r, "endpoint", None), "__name__", "") == "big")
    route.endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    cap = next(iter(ai_tools.compile_agent_capabilities(app, {}).values()))

    result = await ai_tools.execute_agent_capability(
        app, "Bearer user-token", cap, {}, "call-big", result_max_bytes=32
    )
    assert result["error"]["code"] == "result_too_large"


# ---------------------------------------------------------------------------
# Every permission that shapes a capability's result is declared
# ---------------------------------------------------------------------------

def _resolve(expr, namespace: dict):
    if isinstance(expr, ast.Name):
        return namespace.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolve(expr.value, namespace)
        return getattr(base, expr.attr, None) if base is not None else None
    return None


def _namespace(fn, tree) -> dict:
    """The names ``fn`` sees: its module's, plus what it imports in its own body."""
    namespace = dict(fn.__globals__)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name("." * node.level + name, fn.__module__.rpartition(".")[0])
            module = sys.modules.get(name)
            for alias in node.names:
                # `is None`, not truthiness: an imported name may be an SQL column, which refuses bool().
                found = getattr(module, alias.name, None)
                namespace[alias.asname or alias.name] = (
                    found if found is not None else sys.modules.get(f"{name}.{alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name if alias.asname else alias.name.split(".")[0]
                namespace[alias.asname or name] = sys.modules.get(name)
    return namespace


def _consulted(fn, seen: set | None = None, granted: bool = False) -> tuple[set[str], bool, set[str]]:
    """What ``fn`` and every Celerp function it calls consult of the caller's authority:
    the permission keys named to a permission primitive, whether field visibility is
    applied (``restricted_field_keys``), and each permission check whose key is not
    written at the call (decided by data, so only every key covers it).

    The fund movement check (journal_accounts._authorize_movement) runs on every journal
    entry emit_event posts, yet consults a role only inside an open movement grant, which
    only a function entering ``fund_movement_grant`` opens. So it counts below such a
    function and nowhere else: a capability that never opens a grant has its movements
    refused before any role is read."""
    seen = set() if seen is None else seen
    fn = inspect.unwrap(fn)
    module = getattr(fn, "__module__", "") or ""
    if ((fn, granted) in seen or not inspect.isfunction(fn) or not module.startswith(("celerp.", "celerp_"))
            or (fn is journal_accounts._authorize_movement and not granted)):
        return set(), False, set()
    seen.add((fn, granted))
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    namespace = _namespace(fn, tree)
    calls = [(node, _resolve(node.func, namespace)) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    granted = granted or any(
        inspect.unwrap(target) is inspect.unwrap(journal_accounts.fund_movement_grant)
        for _, target in calls if target is not None)
    keys, visibility, opaque = set(), False, set()
    for node, target in calls:
        if target is None:
            continue
        if getattr(target, "__module__", None) == permissions.__name__:
            named = {
                const.value
                for arg in (*node.args, *(keyword.value for keyword in node.keywords))
                for const in ast.walk(arg)
                if isinstance(const, ast.Constant) and permissions.is_permission_key(const.value)
            }
            keys |= named
            # The primitives pass on the key they were given; their callers name it.
            if (not named and module != permissions.__name__
                    and target in (permissions.role_has_permission, permissions.assert_role_permission)):
                opaque.add(f"{module}.{fn.__name__}: {ast.unparse(node)}")
        visibility |= target is cost_visibility.restricted_field_keys
        found = _consulted(target, seen, granted)
        keys |= found[0]
        visibility |= found[1]
        opaque |= found[2]
    return keys, visibility, opaque


def _undeclared(app) -> list[str]:
    every_key = {p.key for p in permissions.PERMISSIONS}
    routes = {(method, path): route for path, methods, route in ai_tools._api_routes(app) for method in methods}
    found = []
    for name, capability in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        route = routes[(capability["method"], capability["path"])]
        keys, visibility, opaque = _consulted(route.endpoint)
        for dependency in route.dependant.dependencies:
            more = _consulted(dependency.call)
            keys, visibility, opaque = keys | more[0], visibility or more[1], opaque | more[2]
        declared = set(capability["result_permissions"])
        if declared == every_key:
            keys, opaque = every_key, set()
        consulted = keys | set(capability["required_permissions"])
        if declared != consulted or opaque:
            found.append(f"{name}: declared {sorted(declared)}, consults {sorted(consulted)}, by data {sorted(opaque)}")
        if visibility != capability["field_visibility"]:
            found.append(f"{name}: field visibility applied {visibility}, declared {capability['field_visibility']}")
    return found


def test_every_permission_a_capabilitys_result_depends_on_is_declared():
    """A kept read is replayed only while its asker still holds every permission its
    result depended on (celerp.ai.service._admit_reads). So a capability
    declares exactly the permissions its handler, or any Celerp function it calls,
    consults, and whether it applies field visibility. A check whose key comes from
    data (a registry) needs every key declared."""
    assert _undeclared(real_agent_app()) == []


def _undescribed(app) -> list[str]:
    """Agent tools without a real summary of their own. The description is the route's
    `summary=` only (a docstring carries internal notes and costs context on every turn):
    one sentence ending in a full stop, at least four words, no braces or backticks,
    under 300 characters, and never the title FastAPI makes from the function name."""
    endpoints = {(path, method): route for path, methods, route in ai_tools._api_routes(app) for method in methods}
    found = []
    for name, capability in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        route = endpoints[(capability["path"], capability["method"])]
        summary = (route.summary or "").strip()
        generated = route.name.replace("_", " ").title()
        real = (
            summary
            and summary == capability["tool"]["function"]["description"]
            and summary.endswith(".")
            and summary.count(". ") == 0
            and len(summary.split()) >= 4
            and len(summary) < 300
            and not set("{}`") & set(summary)
            and summary not in (name, generated)
        )
        if not real:
            found.append(name)
    return found


def test_every_agent_tool_says_what_it_does():
    """The AI reads a tool's description to pick it, so every tool the live surface
    compiles carries one sentence written for the user's task, never the operation id."""
    assert _undescribed(real_agent_app()) == []


def test_the_description_guard_finds_an_undocumented_tool():
    app = FastAPI()

    @app.get("/plain", operation_id="plain", openapi_extra={"x-celerp-agent": True})
    async def plain():
        return {"ok": True}

    @app.get("/told", operation_id="told", openapi_extra={"x-celerp-agent": True})
    async def told():
        """List what was told."""
        return {"ok": True}

    @app.get("/terse", operation_id="terse", summary="List.", openapi_extra={"x-celerp-agent": True})
    async def terse():
        return {"ok": True}

    @app.get("/coded", operation_id="coded", summary="List the `told` rows for one {id}.",
             openapi_extra={"x-celerp-agent": True})
    async def coded():
        return {"ok": True}

    @app.get("/said", operation_id="said", summary="List what each contact said last.",
             openapi_extra={"x-celerp-agent": True})
    async def said():
        return {"ok": True}

    names = ("plain", "told", "terse", "coded", "said")
    for route in app.routes:
        if getattr(getattr(route, "endpoint", None), "__name__", "") in names:
            route.endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    assert _undescribed(app) == ["coded", "plain", "terse", "told"]


def _costs_helper(settings, role):
    return permissions.role_has_permission(settings, role, "view_inventory_costs")


def _visibility_helper(role):
    return cost_visibility.restricted_field_keys(role, [])


def test_the_declaration_guard_finds_a_check_in_a_handler_or_a_helper_it_calls():
    app = FastAPI()

    @app.get("/direct", operation_id="direct", openapi_extra={"x-celerp-agent": True})
    async def direct():
        return {"ok": permissions.role_has_permission({}, "owner", "view_contacts")}

    @app.get("/helper", operation_id="helper", openapi_extra={"x-celerp-agent": True})
    async def helper():
        return {"ok": _costs_helper({}, "owner"), "hidden": sorted(_visibility_helper("owner"))}

    @app.get("/by-data", operation_id="by_data", openapi_extra={"x-celerp-agent": True})
    async def by_data(key: str = "view_contacts"):
        return {"ok": permissions.role_has_permission({}, "owner", key)}

    @app.get("/declared", operation_id="declared", openapi_extra={
        "x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_inventory_costs"],
        "x-celerp-agent-field-visibility": True})
    async def declared():
        return {"ok": _costs_helper({}, "owner"), "hidden": sorted(_visibility_helper("owner"))}

    @app.get("/emits", operation_id="emits", openapi_extra={"x-celerp-agent": True})
    async def emits():
        from celerp.events.engine import emit_event
        return await emit_event(None)

    @app.get("/moves", operation_id="moves", openapi_extra={"x-celerp-agent": True})
    async def moves():
        from celerp_accounting.funds import post_fund_movement
        return await post_fund_movement(None, None, ts=None, reason="", actor=None)

    for fn in (_costs_helper, _visibility_helper, direct, helper, by_data, declared, emits, moves):
        fn.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    found = "\n".join(_undeclared(app))
    # The movement check counts only under a function that opens a movement grant.
    assert "emits:" not in found
    assert "moves: declared [], consults [], by data ['celerp.services.journal_accounts._authorize_movement" in found
    assert "direct: declared [], consults ['view_contacts']" in found
    assert "helper: declared [], consults ['view_inventory_costs']" in found
    assert "helper: field visibility applied True, declared False" in found
    assert "by_data: declared [], consults [], by data" in found
    assert "declared:" not in found


def test_a_declared_result_permission_must_be_a_permission_key():
    app = FastAPI()

    @app.get("/typo", operation_id="typo", openapi_extra={"x-celerp-agent": True, "x-celerp-agent-result-permissions": ["view_costs"]})
    async def typo():
        return {"ok": True}

    typo.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    with pytest.raises(ValueError, match="permission keys"):
        ai_tools.compile_agent_capabilities(app, {})


# ---------------------------------------------------------------------------
# Every route is a tool or excluded with a reason; tool schemas and retry markers
# ---------------------------------------------------------------------------

_KEY_FIELD = re.compile(r"idempot|request_key|_token$")
_SECRET_FIELD = re.compile(
    r"^(password|new_password|current_password|passcode|otp|secret|client_secret|api_key|private_key"
    r"|credentials?|refresh_token|access_token|token|claim_token|service_key)$"
)
# Short-lived ids the server issued to the same user in the same company; not secrets.
_SERVER_HANDLES = {"scan_token", "draft_token", "upload_token", "share_token"}
# The share token inside a link the user pastes; it opens only that one shared document.
_HANDLE_ROUTES = {("POST", "/docs/import-link"): {"token"}}
_CHANGE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _nested_api_routes(routes) -> list[APIRoute]:
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        router = getattr(route, "original_router", route)
        found.extend(_nested_api_routes(getattr(router, "routes", None) or ()))
    return found


def _schema_fields(schema) -> list[str]:
    """Every property name in a JSON schema, nested objects and list items included."""
    if not isinstance(schema, dict):
        return []
    found = []
    for name, sub in (schema.get("properties") or {}).items():
        found.append(name)
        found.extend(_schema_fields(sub))
    for key in ("items", "additionalProperties"):
        found.extend(_schema_fields(schema.get(key)))
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or ():
            found.extend(_schema_fields(sub))
    return found


def _route_index(app) -> dict[tuple[str, str], APIRoute]:
    return {(method, path): route for path, methods, route in ai_tools._api_routes(app) for method in methods}


def test_unknown_exclusion_reason_is_refused():
    assert ai_tools.agent_excluded("session") == {"x-celerp-agent-excluded": "session"}
    assert set(ai_tools.AGENT_EXCLUSIONS) == {
        "session", "internal", "public", "machine", "secret", "company", "installation", "assistant",
    }
    assert all(reason.strip().endswith(".") for reason in ai_tools.AGENT_EXCLUSIONS.values())
    with pytest.raises(ValueError):
        ai_tools.agent_excluded("convenience")


def test_every_route_is_a_tool_or_excluded():
    """Every action a user can take is an assistant tool. A route stays off only when its
    own marker names why, with a reason from AGENT_EXCLUSIONS; a new route fails here
    until it is classified. Hidden routes are walked too."""
    app = real_agent_app()
    walked = ai_tools._api_routes(app)
    assert {route.unique_id for _, _, route in walked} == {route.unique_id for route in _nested_api_routes(app.routes)}
    compiled = {(cap["method"], cap["path"]) for cap in ai_tools.compile_agent_capabilities(app, {}).values()}
    problems = []
    for path, methods, route in walked:
        extra = route.openapi_extra or {}
        reason = extra.get("x-celerp-agent-excluded")
        for method in sorted(methods):
            tool = (method, path) in compiled
            if reason is not None and reason not in ai_tools.AGENT_EXCLUSIONS:
                problems.append(f"{method} {path}: unknown exclusion reason {reason!r}")
            elif tool and reason is not None:
                problems.append(f"{method} {path}: both a tool and excluded")
            elif not tool and reason is None:
                marked = extra.get("x-celerp-agent") is True
                problems.append(f"{method} {path}: {'marked but does not compile' if marked else 'neither a tool nor excluded'}")
    assert problems == []


def test_every_write_tool_is_classified():
    """A confirm-first change declares exactly one retry marker: idempotent (its key
    reaches a lookup, so a retry answers the first result) or a check screen (the
    signed-in screen the card links when the outcome is unknown)."""
    from celerp.help import checks
    from celerp.help.search import covers

    app = real_agent_app()
    routes = _route_index(app)
    screens = checks.ui_route_paths()
    problems = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        if not cap["requires_confirmation"]:
            continue
        route = routes[(cap["method"], cap["path"])]
        extra = route.openapi_extra or {}
        idempotent = extra.get("x-celerp-agent-idempotent") is True
        check = extra.get("x-celerp-agent-check")
        if idempotent == bool(check):
            problems.append(f"{name}: needs exactly one of idempotent or check")
            continue
        if cap["keyed"] is not idempotent:
            problems.append(f"{name}: keyed is {cap['keyed']}")
        if idempotent and not ai_tools.key_store(route):
            problems.append(f"{name}: idempotent, but no lookup reads its key")
        if idempotent and extra.get("x-celerp-agent-outside") and (cap["method"], cap["path"]) not in ai_tools.KEYED_BEFORE_OUTSIDE:
            problems.append(f"{name}: keyed with an outside step but no ordering entry")
        if check and not covers(screens, re.sub(r"\{[^}]+\}", "sample", check)):
            problems.append(f"{name}: check screen {check!r} is not a signed-in screen")
    assert problems == []


def test_no_tool_shows_a_key_field():
    """Keys are supplied by the system and bound to the card's tool call; the model never
    sees or writes one, so a retry of one card reuses its key."""
    shown = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(real_agent_app(), {}).items()):
        for field in _schema_fields(cap["tool"]["function"]["parameters"]):
            if _KEY_FIELD.search(field) and field not in _SERVER_HANDLES:
                shown.append(f"{name}: {field}")
    assert shown == []


def test_no_tool_takes_a_secret():
    """A secret typed into the chat would reach the AI provider and stay in the log. The
    one secret the system fills is removed at compile; every other such route stays off."""
    taken = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(real_agent_app(), {}).items()):
        allowed = _SERVER_HANDLES | _HANDLE_ROUTES.get((cap["method"], cap["path"]), set())
        for field in _schema_fields(cap["tool"]["function"]["parameters"]):
            if _SECRET_FIELD.match(field) and field not in allowed:
                taken.append(f"{name}: {field}")
    assert taken == []


def _binary(prop) -> bool:
    if not isinstance(prop, dict):
        return False
    if prop.get("format") == "binary" or prop.get("contentMediaType") == "application/octet-stream":
        return True
    return _binary(prop.get("items")) or any(_binary(sub) for sub in prop.get("anyOf") or ())


def test_user_facing_fields_stay_visible_to_the_model():
    """The model fills every value the user would fill on the screen. Compile removes only
    the closed set: the key field, upload parts and the generated secret; it adds only
    `after_previous` on a change and `file_ids` on an upload."""
    app = real_agent_app()
    openapi = app.openapi()
    schemas = (openapi.get("components") or {}).get("schemas") or {}
    routes = _route_index(app)
    problems = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        extra = routes[(cap["method"], cap["path"])].openapi_extra or {}
        operation = openapi["paths"][cap["path"]][cap["method"].lower()]
        content = (operation.get("requestBody") or {}).get("content") or {}
        raw = next(iter(content.values()), {}).get("schema") if content else None
        body = ai_tools._inline_local_refs(raw or {}, schemas)
        props = body.get("properties") or {}
        uploads = {field for field, prop in props.items() if _binary(prop)}
        key = extra.get("x-celerp-agent-key") or ""
        removed = uploads | {extra.get("x-celerp-agent-generated-secret")} | ({key} if "[]" not in key else set())
        params = cap["tool"]["function"]["parameters"]
        top = params["properties"]
        expected_top = {"path", "query", "body"} | ({"after_previous"} if cap["requires_confirmation"] else set())
        if uploads:
            expected_top.add("file_ids")
            if "file_ids" not in params.get("required", ()):
                problems.append(f"{name}: file_ids is not required")
        if not set(top) <= expected_top or cap["requires_confirmation"] != ("after_previous" in top):
            problems.append(f"{name}: top-level arguments {sorted(top)}")
        shown = top.get("body") or {}
        if set(shown.get("properties") or {}) != set(props) - removed:
            problems.append(f"{name}: body fields {sorted(set(shown.get('properties') or {}) ^ (set(props) - removed))} differ")
        if not (set(body.get("required") or ()) - removed) <= set(shown.get("required") or ()):
            problems.append(f"{name}: a required field became optional")
    assert problems == []


def test_every_change_tool_is_confirm_first():
    """Every change the assistant makes waits for the user's confirmation. Only a preview
    that writes nothing opts out, on its own route."""
    app = real_agent_app()
    routes = _route_index(app)
    unconfirmed = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        if cap["method"] in _CHANGE_METHODS and not cap["requires_confirmation"]:
            extra = routes[(cap["method"], cap["path"])].openapi_extra or {}
            if extra.get("x-celerp-agent-confirm") is not False or cap["method"] == "DELETE":
                unconfirmed.append(name)
    assert unconfirmed == []


def test_owner_only_tool_is_hidden_from_a_member():
    """A route gated outside require_permission (installation owner, company owner) is
    dropped at compile for a caller who fails the gate, so their model never sees it."""
    app = real_agent_app()

    def paths(role, gates):
        compiled = ai_tools.compile_agent_capabilities(app, {}, role=role, caller_gates=gates)
        return {(cap["method"], cap["path"]) for cap in compiled.values()}

    install_owner_route = ("POST", "/settings/cloud-disconnect")
    owner_route = ("GET", "/migrations/export-requests")
    member = paths("operator", frozenset())
    owner = paths("owner", frozenset({"owner", "install_owner"}))
    assert install_owner_route not in member and owner_route not in member
    assert install_owner_route in owner and owner_route in owner


# ---------------------------------------------------------------------------
# Compiler shapes: key marker, DELETE, form, empty success, download, generated secret
# ---------------------------------------------------------------------------

class _FundBody(BaseModel):
    name: str
    request_key: str


class _Row(BaseModel):
    sku: str
    idempotency_key: str | None = None


class _Rows(BaseModel):
    records: list[_Row]


class _NewUser(BaseModel):
    email: str
    password: str


def _shape_app() -> FastAPI:
    app = FastAPI()
    seen: list = []
    app.state.seen = seen
    keyed = {"x-celerp-agent": True, "x-celerp-agent-idempotent": True}
    check = {"x-celerp-agent": True, "x-celerp-agent-check": "/settings/inventory"}

    @app.post("/funds", summary="Create a fund for restricted gifts.",
              openapi_extra={**keyed, "x-celerp-agent-key": "request_key"})
    async def create_fund(body: _FundBody):
        return body.model_dump()

    @app.post("/rows", summary="Import several stock rows at once.",
              openapi_extra={**keyed, "x-celerp-agent-key": "records[].idempotency_key"})
    async def import_rows(body: _Rows):
        return body.model_dump()

    @app.delete("/things/{item_id}", summary="Delete one thing from the list.", openapi_extra=check)
    async def remove_thing(item_id: str):
        return {"deleted": item_id}

    @app.post("/form", summary="Rename a thing from a form post.", openapi_extra=check)
    async def form_post(name: str = Form(...)):
        return {"name": name}

    @app.post("/ping", summary="Mark the thing as checked today.", status_code=204,
              response_class=Response, openapi_extra=check)
    async def ping():
        seen.append("ping")
        return Response(status_code=204)

    @app.get("/export.csv", summary="Download every thing as a CSV file.", response_class=PlainTextResponse,
             openapi_extra={"x-celerp-agent": True, "x-celerp-agent-download": "/things/export.csv"})
    async def export_things():
        seen.append("export")
        return "a,b"

    @app.post("/users", summary="Add a member to the company by email.",
              openapi_extra={**check, "x-celerp-agent-generated-secret": "password"})
    async def add_user(body: _NewUser):
        return {"email": body.email, "password_length": len(body.password)}

    @app.post("/both", summary="Change a thing with two retry markers.",
              openapi_extra={**keyed, "x-celerp-agent-check": "/settings/inventory"})
    async def both_markers():
        return {"ok": True}

    @app.post("/neither", summary="Change a thing with no retry marker.", openapi_extra={"x-celerp-agent": True})
    async def no_marker():
        return {"ok": True}

    for route in app.routes:
        if isinstance(route, APIRoute):
            route.endpoint.__module__ = "celerp.routers.agent_test"
    app.openapi_schema = None
    return app


def _shape(compiled, endpoint_name):
    return next(cap for name, cap in compiled.items() if name.startswith(endpoint_name + "_"))


def _digest(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]


async def test_key_marker_injects_into_the_named_field():
    app = _shape_app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    fund, rows = _shape(compiled, "create_fund"), _shape(compiled, "import_rows")
    fund_body = fund["tool"]["function"]["parameters"]["properties"]["body"]
    assert "request_key" not in fund_body["properties"]
    assert "request_key" not in fund_body.get("required", [])
    rows_items = rows["tool"]["function"]["parameters"]["properties"]["body"]["properties"]["records"]["items"]
    assert "idempotency_key" not in _schema_fields(rows_items)

    got = await ai_tools.execute_agent_capability(app, "Bearer t", fund, {"body": {"name": "Roof"}}, "call-7")
    assert got["data"]["request_key"] == f"agent:{_digest('call-7')}"
    got = await ai_tools.execute_agent_capability(
        app, "Bearer t", rows, {"body": {"records": [{"sku": "a"}, {"sku": "b"}]}}, "call-8"
    )
    assert [row["idempotency_key"] for row in got["data"]["records"]] == [
        f"agent:{_digest('call-8')}:0", f"agent:{_digest('call-8')}:1",
    ]


def test_a_change_compiles_only_with_exactly_one_retry_marker():
    compiled = ai_tools.compile_agent_capabilities(_shape_app(), {})
    assert not any(name.startswith(("both_markers_", "no_marker_")) for name in compiled)
    assert _shape(compiled, "remove_thing")["keyed"] is False
    assert _shape(compiled, "create_fund")["keyed"] is True


async def test_form_and_empty_response_mutations_compile():
    app = _shape_app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    remove, form, ping = (_shape(compiled, n) for n in ("remove_thing", "form_post", "ping"))
    assert remove["method"] == "DELETE" and remove["requires_confirmation"] is True
    assert form["requires_confirmation"] is True and ping["requires_confirmation"] is True
    assert set(form["tool"]["function"]["parameters"]["properties"]["body"]["properties"]) == {"name"}

    got = await ai_tools.execute_agent_capability(app, "Bearer t", form, {"body": {"name": "Blue"}}, "call-f")
    assert got == {"ok": True, "status": 200, "data": {"name": "Blue"}}
    got = await ai_tools.execute_agent_capability(app, "Bearer t", ping, {}, "call-p")
    assert got == {"ok": True, "status": 204} and app.state.seen == ["ping"]


async def test_download_tool_answers_a_link_never_bytes():
    app = _shape_app()
    export = _shape(ai_tools.compile_agent_capabilities(app, {}), "export_things")
    assert export["requires_confirmation"] is False
    got = await ai_tools.execute_agent_capability(app, "Bearer t", export, {}, "call-d")
    assert got == {"ok": True, "status": 200, "data": {"download": "/things/export.csv"}}
    assert app.state.seen == []


async def test_generated_secret_is_filled_by_the_server_and_hidden_from_the_model():
    app = _shape_app()
    add_user = _shape(ai_tools.compile_agent_capabilities(app, {}), "add_user")
    body = add_user["tool"]["function"]["parameters"]["properties"]["body"]
    assert "password" not in body["properties"] and "password" not in body.get("required", [])
    got = await ai_tools.execute_agent_capability(
        app, "Bearer t", add_user, {"body": {"email": "new@example.com"}}, "call-u"
    )
    assert got["ok"] is True and got["data"]["password_length"] >= 16


# ---------------------------------------------------------------------------
# Gates the compiler can see, the way back, downloads, newly opened areas
# ---------------------------------------------------------------------------

_GATE_MARKERS = ("x-celerp-agent-any-permission", "x-celerp-agent-any-role")


def test_every_tool_gate_is_visible_to_compile():
    """A role check the compiler cannot see would show a tool to a role the route then
    refuses. Every tool carries require_permission, an agent_gate dependency, or a marker
    that says any permission or any role is enough; a route whose body refuses a role
    (a 403 or a permission call) needs one of the first three and never any-role."""
    app = real_agent_app()
    routes = _route_index(app)
    problems = []
    for name, cap in sorted(ai_tools.compile_agent_capabilities(app, {}).items()):
        route = routes[(cap["method"], cap["path"])]
        extra = route.openapi_extra or {}
        fixed = bool(ai_tools._route_permissions(route)) or bool(ai_tools.route_gates(route))
        refusing = ai_tools.refusing_checks(route)
        if not (fixed or any(extra.get(m) for m in _GATE_MARKERS)):
            problems.append(f"{name}: no gate the compiler can see")
        if refusing and extra.get("x-celerp-agent-any-role"):
            problems.append(f"{name}: any-role, but its body refuses: {refusing[0]}")
        if refusing and not (fixed or extra.get("x-celerp-agent-any-permission")):
            problems.append(f"{name}: its body refuses with no gate the compiler can see: {refusing[0]}")
    assert problems == []


def test_the_gate_scan_finds_a_refusal_in_a_helper_the_handler_calls():
    """The scan behind the gate guard reads the handler and the helpers it calls."""
    from fastapi import HTTPException

    def _deny(role: str) -> None:
        if role != "owner":
            raise HTTPException(status_code=403, detail="no")

    app = FastAPI()

    @app.post("/guarded", openapi_extra={"x-celerp-agent": True})
    def guarded(role: str = Query("x")) -> dict:
        _deny(role)
        return {}

    route = _route_index(app)[("POST", "/guarded")]
    assert ai_tools.refusing_checks(route) != []


def test_price_tool_hidden_without_price_permission():
    """The price tool is shown to a role that may set prices, and to a role that may edit
    items (a draft's creator authors its cost); a role with neither never sees it."""
    app = real_agent_app()
    route = ("POST", "/items/{entity_id}/price")

    def paths(role):
        return {(c["method"], c["path"]) for c in ai_tools.compile_agent_capabilities(app, {}, role=role).values()}

    assert route not in paths("viewer")
    assert route in paths("operator")
    assert route in paths("manager")


def test_staff_password_tools_owner_only():
    """Whether a member's password may be reset is the owner's question: the check is a
    tool for the owner only, and the reset itself, which takes a password, stays off."""
    app = real_agent_app()
    check = ("GET", "/companies/me/users/{user_id}/password")
    reset = ("POST", "/companies/me/users/{user_id}/password")

    def paths(role, gates):
        compiled = ai_tools.compile_agent_capabilities(app, {}, role=role, caller_gates=gates)
        return {(c["method"], c["path"]) for c in compiled.values()}

    assert check in paths("owner", frozenset({"owner"}))
    assert check not in paths("admin", frozenset())
    assert reset not in paths("owner", frozenset({"owner"}))
    extra = _route_index(app)[reset].openapi_extra or {}
    assert extra.get("x-celerp-agent-excluded") == "secret"


def test_every_write_tool_has_a_way_back():
    """Everything the assistant changes can be undone. Every write tool names its way
    back: a reverse that is itself a tool, a screen the card links to, or a reason a
    user would accept. A reason that only points at the screen is not a reason."""
    from celerp.ai import reverse
    from celerp.help import checks
    from celerp.help.search import covers

    app = real_agent_app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    tools = {(c["method"], c["path"]) for c in compiled.values()}
    screens = checks.ui_route_paths()
    problems = []
    for name, cap in sorted(compiled.items()):
        if not cap["requires_confirmation"]:
            continue
        back = reverse.WAY_BACK.get((cap["method"], cap["path"]))
        kind, _, target = (back or "").partition(":")
        if back is None:
            problems.append(f"{name}: no way back")
        elif kind == "tool" and tuple(target.split(" ", 1)) not in tools:
            problems.append(f"{name}: reverse {target!r} is not a tool")
        elif kind == "screen" and not covers(screens, re.sub(r"\{[^}]+\}", "sample", target)):
            problems.append(f"{name}: screen {target!r} is not a signed-in screen")
        elif kind == "reason" and target not in reverse.REASONS:
            problems.append(f"{name}: unknown reason {target!r}")
        elif kind not in {"tool", "screen", "reason"}:
            problems.append(f"{name}: unreadable way back {back!r}")
    for key, text in reverse.REASONS.items():
        if reverse.SCREEN_ONLY.search(text):
            problems.append(f"reason {key}: only points at the screen")
    assert problems == []
    assert reverse.SCREEN_ONLY.search("Same as the screen.")


def test_every_download_tool_names_a_ui_route():
    """A file is never sent through the chat. A download tool answers a link to the
    signed-in screen that serves the file, so that link must be a live UI route."""
    from celerp.help import checks

    app = real_agent_app()
    screens = set(checks.ui_route_paths())
    downloads = []
    for path, methods, route in ai_tools._api_routes(app):
        link = (route.openapi_extra or {}).get("x-celerp-agent-download")
        if link is None:
            continue
        downloads.append(path)
        if link not in screens:
            raise AssertionError(f"{path}: download link {link!r} is not a UI route")
        assert set(re.findall(r"\{([^}]+)\}", link)) <= set(re.findall(r"\{([^}]+)\}", path)), path
    compiled = {c["path"] for c in ai_tools.compile_agent_capabilities(app, {}).values()}
    assert len(downloads) == 21
    assert set(downloads) <= compiled


def test_funds_tools_compile():
    """Fund balances move through the assistant like any other confirmed change; the
    fund request key is supplied by the system, never shown to the model."""
    caps = ai_tools.compile_agent_capabilities(real_agent_app(), {})
    funds = {(c["method"], c["path"]): c for c in caps.values() if c["path"].startswith("/accounting/funds")}
    assert ("GET", "/accounting/funds") in funds
    assert ("POST", "/accounting/funds") in funds
    for cap in funds.values():
        assert "request_key" not in _schema_fields(cap["tool"]["function"]["parameters"])
        if cap["method"] != "GET":
            assert cap["requires_confirmation"] is True and cap["keyed"] is True


_LIFECYCLE = {
    ("POST", "/docs/{entity_id}/finalize"): "finalize_documents",
    ("POST", "/docs/{entity_id}/void"): "finalize_documents",
    ("POST", "/docs/{entity_id}/send"): "edit_documents",
    ("POST", "/items/bulk/expire"): "adjust_inventory",
    ("POST", "/items/import/batch"): "import_export_data",
    ("DELETE", "/crm/contacts/{contact_id}/notes/{note_id}"): "edit_contacts",
}


def test_lifecycle_bulk_import_and_delete_tools_are_confirm_first_and_role_filtered():
    """Finalize, void, send, a bulk change, a batch import and a delete are confirmed
    tools, and a role without the permission never sees them."""
    app = real_agent_app()
    caps = {(c["method"], c["path"]): c for c in ai_tools.compile_agent_capabilities(app, {}).values()}
    viewer = {(c["method"], c["path"]) for c in ai_tools.compile_agent_capabilities(app, {}, role="viewer").values()}
    for route, permission in _LIFECYCLE.items():
        assert route in caps, route
        assert caps[route]["requires_confirmation"] is True, route
        assert permission in ai_tools._route_permissions(_route_index(app)[route]), route
        assert route not in viewer, route

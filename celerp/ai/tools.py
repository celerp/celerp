# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Agent capabilities derived from the live FastAPI app: OpenAPI compiler and in-process executor."""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import quote
from typing import Any

import fastapi.routing
import httpx
from fastapi.routing import APIRoute

from celerp.modules.loader import loaded_modules
from celerp.modules.registry import get_enabled
from celerp.services.permissions import role_has_permission


# ---------------------------------------------------------------------------
# Generic agent capability adapter
# ---------------------------------------------------------------------------

_AGENT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
_AGENT_MUTATIONS = frozenset({"POST", "PUT", "PATCH"})
AGENT_RESULT_MAX_BYTES = 64 * 1024
# The in-process ASGI transport ignores httpx timeouts, so the executor bounds
# each capability call itself.
CAPABILITY_TIMEOUT_S = 30.0
_LOCAL_REF_PREFIX = "#/components/schemas/"


def _agent_error(code: str, message: str, *, status: int = 0) -> dict[str, Any]:
    return {"ok": False, "status": status, "error": {"code": code, "message": message}}


def _inline_local_refs(value: Any, schemas: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """Inline local OpenAPI component refs; reject cycles/external refs fail-closed."""
    if isinstance(value, list):
        return [_inline_local_refs(v, schemas, seen) for v in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith(_LOCAL_REF_PREFIX):
            raise ValueError("unsupported OpenAPI reference")
        name = ref[len(_LOCAL_REF_PREFIX):]
        if not name or name in seen or name not in schemas:
            raise ValueError("cyclic or missing OpenAPI reference")
        base = _inline_local_refs(schemas[name], schemas, seen + (name,))
        if not isinstance(base, dict):
            raise ValueError("OpenAPI component schema must be an object")
        merged = dict(base)
        for key, child in value.items():
            if key != "$ref":
                merged[key] = _inline_local_refs(child, schemas, seen)
        return merged
    return {key: _inline_local_refs(child, schemas, seen) for key, child in value.items()}


def _api_routes(app: Any) -> list[tuple[str, set[str], APIRoute]]:
    """``(path_format, methods, route)`` for every APIRoute the app serves.

    FastAPI 0.141 stopped flattening included routers into ``app.routes``; the
    prefixed path then lives on a route context. Earlier releases expose the
    flattened APIRoute objects directly.
    """
    iter_route_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if iter_route_contexts is not None:
        return [
            (str(context.path_format or context.path or ""), set(context.methods or ()), context.original_route)
            for context in iter_route_contexts(app.routes)
            if isinstance(context.original_route, APIRoute)
        ]
    return [
        (str(route.path_format or route.path), set(route.methods or ()), route)
        for route in app.routes
        if isinstance(route, APIRoute)
    ]


def _route_permissions(route: APIRoute) -> tuple[str, ...]:
    """Permission keys already enforced by this FastAPI route, recursively.

    ``require_permission`` annotates its dependency guard with ``required_permission``;
    reading that same dependency graph keeps the agent tool surface DRY with the API.
    """
    found: set[str] = set()
    stack = list(getattr(route.dependant, "dependencies", ()) or ())
    while stack:
        dep = stack.pop()
        key = getattr(getattr(dep, "call", None), "required_permission", None)
        if isinstance(key, str) and key:
            found.add(key)
        stack.extend(getattr(dep, "dependencies", ()) or ())
    return tuple(sorted(found))


def _agent_route_owner(endpoint: Any, company_settings: dict[str, Any]) -> str | None:
    """Return core/module owner when direct agent invocation is allowed, else None."""
    endpoint_module = str(getattr(endpoint, "__module__", "") or "")
    if endpoint_module == "celerp" or endpoint_module.startswith("celerp."):
        return "celerp"

    enabled_key_present = "enabled_modules" in (company_settings or {})
    enabled = get_enabled(company_settings)
    matches: list[dict] = []
    for manifest in loaded_modules():
        api_routes = manifest.get("api_routes")
        if not isinstance(api_routes, str) or not api_routes:
            continue
        root = api_routes.split(".", 1)[0]
        if endpoint_module == root or endpoint_module.startswith(root + "."):
            matches.append(manifest)
    if len(matches) != 1:
        return None
    manifest = matches[0]
    if manifest.get("first_party") is not True:
        return None
    name = str(manifest.get("name") or "")
    if not name:
        return None
    if enabled_key_present and name not in enabled:
        return None
    return name


def _json_response(operation: dict[str, Any]) -> bool:
    for status_code, response in (operation.get("responses") or {}).items():
        if not str(status_code).startswith("2") or not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        if "application/json" in content:
            return True
    return False


def compile_agent_capabilities(
    app: Any,
    company_settings: dict[str, Any] | None = None,
    role: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Compile explicitly marked live FastAPI operations into model tools.

    FastAPI/OpenAPI remains the schema source of truth. Direct module operations
    are limited to first-party loaded modules; core celerp.* routes may opt in.

    A mutation compiles only when the canonical route explicitly declares
    ``x-celerp-agent-idempotent``. Merely exposing an ``idempotency_key`` field
    is not proof that secondary effects are replay-safe. When the proven route
    has such a field, the compiler hides it and injects the immutable tool-call
    identity server-side.

    When ``role`` is supplied, tools whose existing FastAPI permission
    dependencies deny that role are omitted before the model sees them. The
    route still re-checks authorization on execution.
    """
    settings = company_settings or {}
    openapi = app.openapi()
    schemas = ((openapi.get("components") or {}).get("schemas") or {})
    routes = _api_routes(app)
    compiled: dict[str, dict[str, Any]] = {}

    for path, path_item in (openapi.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method_lower, operation in path_item.items():
            method = str(method_lower).upper()
            if method not in _AGENT_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("x-celerp-agent") is not True:
                continue
            if not _json_response(operation):
                continue

            matching = [
                route for route_path, methods, route in routes
                if route_path == path and method in methods
            ]
            if len(matching) != 1:
                continue
            route = matching[0]
            owner = _agent_route_owner(route.endpoint, settings)
            if owner is None:
                continue
            required_permissions = _route_permissions(route)
            if role is not None and any(
                not role_has_permission(settings, role, permission)
                for permission in required_permissions
            ):
                continue

            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id or operation_id in compiled:
                raise ValueError(f"duplicate or missing agent operationId: {operation_id!r}")

            path_properties: dict[str, Any] = {}
            path_required: list[str] = []
            query_properties: dict[str, Any] = {}
            query_required: list[str] = []
            unsupported_parameter = False
            for parameter in operation.get("parameters") or []:
                if not isinstance(parameter, dict):
                    unsupported_parameter = True
                    break
                location = parameter.get("in")
                name = parameter.get("name")
                if not isinstance(name, str):
                    unsupported_parameter = True
                    break
                if location not in ("path", "query"):
                    unsupported_parameter = True
                    break
                schema = _inline_local_refs(parameter.get("schema") or {}, schemas)
                if parameter.get("description") and isinstance(schema, dict) and "description" not in schema:
                    schema = dict(schema)
                    schema["description"] = parameter["description"]
                if location == "path":
                    path_properties[name] = schema
                    if parameter.get("required", True):
                        path_required.append(name)
                else:
                    query_properties[name] = schema
                    if parameter.get("required"):
                        query_required.append(name)
            if unsupported_parameter:
                continue

            body_schema: dict[str, Any] | None = None
            body_required = False
            request_body = operation.get("requestBody")
            if request_body is not None:
                if not isinstance(request_body, dict):
                    continue
                media = request_body.get("content") or {}
                if set(media) != {"application/json"}:
                    continue
                raw_schema = (media.get("application/json") or {}).get("schema") or {}
                body_schema = _inline_local_refs(raw_schema, schemas)
                if not isinstance(body_schema, dict):
                    continue
                body_required = bool(request_body.get("required"))

            requires_confirmation = (
                method in _AGENT_MUTATIONS
                and operation.get("x-celerp-agent-confirm") is not False
            )
            inject_idempotency = False
            if requires_confirmation:
                if operation.get("x-celerp-agent-idempotent") is not True:
                    continue
                if body_schema is not None:
                    props = body_schema.get("properties")
                    if isinstance(props, dict) and "idempotency_key" in props:
                        inject_idempotency = True
                        body_schema = dict(body_schema)
                        new_props = dict(props)
                        new_props.pop("idempotency_key", None)
                        body_schema["properties"] = new_props
                        if isinstance(body_schema.get("required"), list):
                            body_schema["required"] = [
                                item for item in body_schema["required"] if item != "idempotency_key"
                            ]

            tool_properties: dict[str, Any] = {}
            tool_required: list[str] = []
            if path_properties:
                section: dict[str, Any] = {"type": "object", "properties": path_properties}
                if path_required:
                    section["required"] = path_required
                    tool_required.append("path")
                tool_properties["path"] = section
            if query_properties:
                section = {"type": "object", "properties": query_properties}
                if query_required:
                    section["required"] = query_required
                    tool_required.append("query")
                tool_properties["query"] = section
            if body_schema is not None:
                tool_properties["body"] = body_schema
                if body_required:
                    tool_required.append("body")

            params: dict[str, Any] = {
                "type": "object",
                "properties": tool_properties,
                "additionalProperties": False,
            }
            if tool_required:
                params["required"] = tool_required

            description = operation.get("summary") or operation.get("description") or operation_id
            compiled[operation_id] = {
                "name": operation_id,
                "method": method,
                "path": path,
                "module": owner,
                "path_names": tuple(path_properties),
                "query_names": tuple(query_properties),
                "expects_body": body_schema is not None,
                "inject_idempotency": inject_idempotency,
                "requires_confirmation": requires_confirmation,
                "required_permissions": required_permissions,
                "tool": {
                    "type": "function",
                    "function": {
                        "name": operation_id,
                        "description": str(description),
                        "parameters": params,
                    },
                },
            }

    return compiled


def agent_tool_specs(capabilities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the model tool specs for compiled capabilities, in stable name order."""
    return [capabilities[name]["tool"] for name in sorted(capabilities)]


async def execute_agent_capability(
    app: Any,
    authorization: str,
    capability: dict[str, Any],
    arguments: dict[str, Any],
    tool_call_id: str,
    *,
    result_max_bytes: int = AGENT_RESULT_MAX_BYTES,
) -> dict[str, Any]:
    """Invoke one compiled capability through the same FastAPI app as the user."""
    if not authorization:
        return _agent_error("missing_authorization", "The user authorization context is unavailable.")
    if not isinstance(arguments, dict):
        return _agent_error("invalid_arguments", "Tool arguments must be a JSON object.")

    allowed_sections = {
        section for section, present in (
            ("path", bool(capability.get("path_names"))),
            ("query", bool(capability.get("query_names"))),
            ("body", bool(capability.get("expects_body"))),
        ) if present
    }
    unknown_sections = set(arguments) - allowed_sections
    if unknown_sections:
        return _agent_error("invalid_arguments", "Tool arguments contain unsupported sections.")

    path_values = arguments.get("path") or {}
    query_values = arguments.get("query") or {}
    body = arguments.get("body") if capability.get("expects_body") else None
    if not isinstance(path_values, dict) or not isinstance(query_values, dict):
        return _agent_error("invalid_arguments", "Path/query arguments must be JSON objects.")
    if capability.get("expects_body") and not isinstance(body, dict):
        return _agent_error("invalid_arguments", "Request body must be a JSON object.")

    path_names = set(capability.get("path_names") or ())
    query_names = set(capability.get("query_names") or ())
    if set(path_values) - path_names or set(query_values) - query_names:
        return _agent_error("invalid_arguments", "Tool arguments contain unknown path/query parameters.")
    if path_names - set(path_values):
        return _agent_error("invalid_arguments", "A required path parameter is missing.")

    url_path = str(capability["path"])
    for name in capability.get("path_names") or ():
        raw = str(path_values[name])
        if "/" in raw or "\\" in raw:
            return _agent_error("invalid_path_parameter", "Path parameters cannot contain path separators.")
        url_path = url_path.replace("{" + name + "}", quote(raw, safe=""))
    if "{" in url_path or "}" in url_path:
        return _agent_error("invalid_path_parameter", "A path parameter could not be resolved.")

    params: list[tuple[str, str]] = []
    for name, value in query_values.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is not None:
                params.append((name, str(item)))

    if body is not None:
        body = dict(body)
        if capability.get("inject_idempotency"):
            digest = hashlib.sha256(str(tool_call_id).encode("utf-8")).hexdigest()[:32]
            body["idempotency_key"] = f"agent:{digest}"

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://celerp.internal") as client:
        try:
            response = await asyncio.wait_for(
                client.request(
                    str(capability["method"]),
                    url_path,
                    params=params,
                    json=body,
                    headers={"Authorization": authorization},
                    follow_redirects=False,
                ),
                timeout=CAPABILITY_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return _agent_error(
                "capability_timeout",
                f"The operation did not finish within {int(CAPABILITY_TIMEOUT_S)} seconds.",
            )

    try:
        data = response.json()
    except Exception:
        return _agent_error(
            "non_json_response",
            "The capability returned a non-JSON response.",
            status=response.status_code,
        )

    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > result_max_bytes:
        if response.is_success and capability.get("requires_confirmation"):
            # The write already returned success. Never turn that definite success into
            # an apparent failure merely because its response is too large to retain;
            # doing so would invite a needless retry of a mutation that already landed.
            return {
                "ok": True, "status": response.status_code,
                "data": {"result_omitted": True, "detail": "Operation completed successfully."},
            }
        return _agent_error(
            "result_too_large",
            "The capability result is too large; narrow the query or use pagination.",
            status=response.status_code,
        )
    return {"ok": response.is_success, "status": response.status_code, "data": data}

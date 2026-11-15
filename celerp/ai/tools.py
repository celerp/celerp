# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Agent capabilities derived from the live FastAPI app: OpenAPI compiler and in-process executor."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import re
import secrets
import textwrap
import types
from collections.abc import Callable
from urllib.parse import quote
from typing import Any

import fastapi.routing
import httpx
from fastapi.routing import APIRoute

from celerp.accounting_roles import refusal
from celerp import error_body
from celerp.ai import deadlines
from celerp.ai.gates import AGENT_GATES
from celerp.modules.loader import loaded_modules
from celerp.modules.registry import uses_module
from celerp.services.permissions import PERMISSIONS, is_permission_key, role_has_permission
from ui.i18n import current_lang, t


# ---------------------------------------------------------------------------
# Generic agent capability adapter
# ---------------------------------------------------------------------------

_AGENT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_AGENT_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AGENT_RESULT_MAX_BYTES = 64 * 1024
_LOCAL_REF_PREFIX = "#/components/schemas/"
_FORM = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data"
# What the model and the stored action keep in place of a secret the system generated.
SHOWN_ONCE = "(shown once to you)"

# Why a route is not an assistant tool. Every route is a tool unless its own decorator
# carries ``openapi_extra=agent_excluded(<key>)`` (or its router calls exclude_router).
AGENT_EXCLUSIONS: dict[str, str] = {
    # Not a user action.
    "session": "Sign-in, first-boot and session plumbing: runs before or around a signed-in user, "
               "never as an action inside a company.",
    "internal": "An endpoint the app calls by itself (health probe, live-update stream, image source, "
                "call-to-action fragment), not an action a user takes.",
    "public": "A page served to an outside party who opens a shared link, with no signed-in user.",
    "machine": "Called by another program (a linked company or a connector), never by a person.",
    # User actions kept off the assistant.
    "secret": "Takes a password, one-time code, claim token or service key; typed into the chat it "
              "would go to the AI provider and stay in the conversation log.",
    "company": "Creates, restores, replaces or erases a whole company, including the conversation and "
               "confirmation the assistant runs in.",
    "installation": "Restarts the server or installs code, which ends the running conversation mid-step.",
    "assistant": "The assistant's own controls: a tool that confirms, dismisses, reruns or reconfigures "
                 "the assistant would let it approve its own proposals.",
}

# Keyed changes that also reach outside the database (an email, the cloud service). A
# retry is safe only because the route commits its keyed entry before the outside step;
# each entry says where the handler does so.
_SUBSCRIPTION_BILL_ORDER = (
    "the bill commits under the key first (claim_period answers a repeat with the earlier bill); "
    "collect.ask_after_bill then asks the cloud service only for a collection still waiting"
)
KEYED_BEFORE_OUTSIDE: dict[tuple[str, str], str] = {
    ("POST", "/docs/{entity_id}/send"):
        "send_doc returns mark_doc_sent's earlier result before any email; the email goes after the commit",
    ("POST", "/subscriptions/generate-due"): _SUBSCRIPTION_BILL_ORDER,
    ("POST", "/subscriptions/catch-up/run"): _SUBSCRIPTION_BILL_ORDER,
    ("POST", "/subscriptions/{entity_id}/generate-due"): _SUBSCRIPTION_BILL_ORDER,
    ("POST", "/subscriptions/{entity_id}/amend"): _SUBSCRIPTION_BILL_ORDER,
    ("POST", "/subscriptions/{entity_id}/bill-early"): _SUBSCRIPTION_BILL_ORDER,
}


def agent_excluded(reason: str) -> dict[str, str]:
    """The ``openapi_extra`` that keeps a route off the assistant, with the reason why."""
    if reason not in AGENT_EXCLUSIONS:
        raise ValueError(f"unknown agent exclusion reason: {reason!r}")
    return {"x-celerp-agent-excluded": reason}


def exclude_router(router: Any, reason: str) -> None:
    """Mark every route of a router whose routes all stay off for one reason."""
    marker = agent_excluded(reason)
    for route in router.routes:
        if isinstance(route, APIRoute):
            route.openapi_extra = {**(route.openapi_extra or {}), **marker}


def keyed_error(code: str, key: str, message: str | None = None, /, **params) -> dict[str, Any]:
    """An AI error a person may read: ``code`` for the program, and the keyed refusal
    (celerp.accounting_roles.refusal) so every reader sees it in their own language.
    ``message`` is the English text, the en catalog's when not given."""
    return {"code": code, **refusal(key, t(key, "en", **params) if message is None else message, **params)}


def _agent_error(code: str, key: str, message: str | None = None, /, *, http_status: int = 0,
                 **params) -> dict[str, Any]:
    """A capability call that produced no route result. The English ``message`` also
    tells the model what to change."""
    return {"ok": False, "status": http_status, "error": keyed_error(code, key, message, **params)}


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


def _result_permissions(operation: dict[str, Any], route_permissions: tuple[str, ...]) -> tuple[str, ...]:
    """Every permission the route's result depends on: its dependencies' and those its
    handler consults on what it returns (``x-celerp-agent-result-permissions``: a list
    of keys, or ``"*"`` when a registry names them, which is every key)."""
    declared = operation.get("x-celerp-agent-result-permissions") or []
    if declared == "*":
        return tuple(sorted(p.key for p in PERMISSIONS))
    if not isinstance(declared, list) or not all(is_permission_key(key) for key in declared):
        raise ValueError(f"x-celerp-agent-result-permissions must list permission keys: {declared!r}")
    return tuple(sorted({*route_permissions, *declared}))


def _agent_route_owner(endpoint: Any, company_settings: dict[str, Any]) -> str | None:
    """Return core/module owner when direct agent invocation is allowed, else None."""
    endpoint_module = str(getattr(endpoint, "__module__", "") or "")
    if endpoint_module == "celerp" or endpoint_module.startswith("celerp."):
        return "celerp"

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
    if not uses_module(company_settings, name):
        return None
    return name


def route_gates(route: APIRoute) -> tuple[str, ...]:
    """``agent_gate`` rules on the route's dependency tree (owner, install_owner)."""
    found: set[str] = set()
    stack = list(getattr(route.dependant, "dependencies", ()) or ())
    while stack:
        dep = stack.pop()
        rule = getattr(getattr(dep, "call", None), "agent_gate", None)
        if rule in AGENT_GATES:
            found.add(rule)
        stack.extend(getattr(dep, "dependencies", ()) or ())
    return tuple(sorted(found))


async def gates_of_caller(session: Any, user_id: Any, role: str) -> frozenset[str]:
    """The ``agent_gate`` rules this caller passes, for ``compile_agent_capabilities``."""
    from celerp.services.auth import is_install_owner

    gates = {"owner"} if role == "owner" else set()
    if await is_install_owner(session, user_id):
        gates.add("install_owner")
    return frozenset(gates)


def _any_permission(extra: dict[str, Any], settings: dict[str, Any]) -> tuple[str, ...] | None:
    """``x-celerp-agent-any-permission``: permissions any one of which admits a role. A list
    names them; a string names a slot whose entries (of modules the company uses) carry
    ``permission``, for a gate that depends on which entry the call names."""
    declared = extra.get("x-celerp-agent-any-permission")
    if declared is None:
        return None
    if isinstance(declared, str):
        from celerp.modules.slots import get as get_slot

        return tuple(sorted({
            entry["permission"] for entry in get_slot(declared)
            if entry.get("permission") and uses_module(settings, entry.get("_module"))
        }))
    if not isinstance(declared, list) or not all(is_permission_key(key) for key in declared):
        raise ValueError(f"x-celerp-agent-any-permission must list permission keys or name a slot: {declared!r}")
    return tuple(declared)


# ---------------------------------------------------------------------------
# Source scans behind the guards: what a handler refuses, whether its key is looked up
# ---------------------------------------------------------------------------

_ROLE_CHECK = re.compile(
    r'status_code\s*=\s*403|HTTPException\(\s*403|role_has_permission\(|assert_role_permission\(|'
    r'reject_price_change\(|missing_permission\(|require_role\(|is_install_owner\(|role\s*[!=]=\s*["\']owner'
)
_REFUSAL = re.compile(
    r'status_code\s*=\s*403|HTTPException\(\s*403|raise\s+missing_permission\(|assert_role_permission\(|'
    r'reject_price_change\('
)
_KEY_LOOKUP = re.compile(r"\b(keyed_event|find_event_by_idempotency|_earlier_run)\(|LedgerEntry\.idempotency_key")
_EMIT = re.compile(r"\bemit_event\(")
_OTHER_WRITE = re.compile(r"session\.(add|delete)\(|\.execute\(\s*(insert|update|delete)\(")


def _source(fn: Any) -> str:
    try:
        return textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return ""


def _callees(fn: Any) -> list[Any]:
    """Functions of this codebase ``fn`` calls by name, through a closure, a local import or
    an attribute of an imported module or class."""
    tree = None
    try:
        tree = ast.parse(_source(fn))
    except SyntaxError:
        pass
    if tree is None:
        return []
    names = dict(getattr(fn, "__globals__", {}) or {})
    try:
        names.update(inspect.getclosurevars(fn).nonlocals)
    except (TypeError, ValueError):
        pass
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                try:
                    module = __import__(node.module, fromlist=["_"])
                except Exception:
                    continue
                names.setdefault(alias.asname or alias.name, getattr(module, alias.name, None))
    own = str(getattr(fn, "__module__", "") or "")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = None
        if isinstance(node.func, ast.Name):
            target = names.get(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base = names.get(node.func.value.id)
            if isinstance(base, (types.ModuleType, type)):
                target = getattr(base, node.func.attr, None)
        if isinstance(target, staticmethod):
            target = target.__func__
        if not inspect.isfunction(target):
            continue
        module = str(target.__module__ or "")
        if module == own or module.startswith(("celerp", "ui.")):
            found.append(target)
    return found


def _reached(fn: Any, depth: int, skip: Callable[[Any], bool] = lambda _fn: False) -> list[Any]:
    """``fn`` and every function of this codebase it reaches within ``depth`` calls."""
    seen, frontier = [fn], [fn]
    for _ in range(depth):
        frontier = list(dict.fromkeys(c for f in frontier for c in _callees(f) if c not in seen and not skip(c)))
        seen.extend(frontier)
    return seen


def refusing_checks(route: APIRoute) -> list[str]:
    """Lines in the handler's body, or a helper it calls (two levels), that refuse the caller
    by role: a 403, a missing permission, a price rule. Signature defaults are not read:
    a Depends(require_permission(...)) there is already visible to compile."""
    found = []
    for fn in _reached(route.endpoint, 2, lambda f: f.__module__ == "celerp.services.permissions"):
        tree = None
        source = _source(fn)
        try:
            tree = ast.parse(source).body[0]
        except (SyntaxError, IndexError):
            pass
        if not isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)) or not tree.body:
            continue
        lines = source.splitlines()
        for index in range(tree.body[0].lineno - 1, len(lines)):
            line = lines[index]
            window = "\n".join(lines[index:index + 4])
            role_check = _ROLE_CHECK.search(line)
            if (role_check or _REFUSAL.search(line)) and (
                _REFUSAL.search(window) or (role_check and re.search(r"raise\b", window))
            ):
                found.append(f"{fn.__module__}.{fn.__qualname__}: {line.strip()[:100]}")
    return found


def key_store(route: APIRoute) -> str:
    """Why a retry under the route's key cannot repeat its write, or "" when nothing looks
    the key up: the handler or a function it reaches finds an earlier run by the key, keeps
    the key on its import batch, or passes it to its one domain event; or the route declares
    itself idempotent with no key field (its state machine refuses a repeat)."""
    extra = route.openapi_extra or {}
    field = str(extra.get("x-celerp-agent-key") or "").split(".")[-1].replace("[]", "")
    if not field:
        return "state machine" if extra.get("x-celerp-agent-idempotent") is True else ""
    sources = [_source(fn) for fn in _reached(route.endpoint, 6)
               if fn.__module__ != "celerp.events.engine"]
    if field in "".join(sources) and any(_KEY_LOOKUP.search(src) for src in sources):
        return "key lookup"
    if any("idempotency_keys" in src for src in sources):
        return "import batch"
    emits = sum(len(_EMIT.findall(src)) for src in sources)
    others = sum(len(_OTHER_WRITE.findall(src)) for src in sources)
    if emits == 1 and others == 0 and any(_EMIT.search(src) and "idempotency_key=" in src and field in src
                                          for src in sources):
        return "event key"
    return ""


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------

def _binary(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("format") == "binary" or schema.get("contentMediaType") == "application/octet-stream":
        return True
    return _binary(schema.get("items")) or any(_binary(sub) for sub in schema.get("anyOf") or ())


def _drop_field(schema: dict[str, Any], dotted: str) -> dict[str, Any]:
    """``schema`` without the field ``dotted`` names (``records[].idempotency_key`` reaches
    into each item of a list), removed from ``properties`` and ``required``."""
    head, _, rest = dotted.partition(".")
    name = head.removesuffix("[]")
    props = schema.get("properties")
    if not isinstance(props, dict) or name not in props:
        raise ValueError(f"agent marker names a field the body does not have: {dotted!r}")
    out = dict(schema)
    if not rest:
        out["properties"] = {k: v for k, v in props.items() if k != name}
        if isinstance(out.get("required"), list):
            out["required"] = [item for item in out["required"] if item != name]
        return out
    child = props[name]
    if head.endswith("[]"):
        child = {**child, "items": _drop_field(child.get("items") or {}, rest)}
    else:
        child = _drop_field(child, rest)
    out["properties"] = {**props, name: child}
    return out


def _fill_field(body: Any, dotted: str, value: str) -> Any:
    """``body`` with ``value`` written into the field ``dotted`` names; each item of a list
    gets ``<value>:<index>``."""
    if not isinstance(body, dict):
        return body
    head, _, rest = dotted.partition(".")
    name = head.removesuffix("[]")
    out = dict(body)
    if not rest:
        out[name] = value
    elif head.endswith("[]"):
        out[name] = [_fill_field(row, rest, f"{value}:{index}") for index, row in enumerate(body.get(name) or [])]
    else:
        out[name] = _fill_field(body.get(name), rest, value)
    return out


def _json_response(operation: dict[str, Any]) -> bool:
    for status_code, response in (operation.get("responses") or {}).items():
        if not str(status_code).startswith("2") or not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        if "application/json" in content:
            return True
    return False


_AFTER_PREVIOUS = {
    "type": "boolean",
    "description": "True when this change needs the result of the change proposed just before it in this reply.",
}
_FILE_IDS = {
    "type": "array", "items": {"type": "string"}, "minItems": 1,
    "description": "Ids of files the user attached to this conversation.",
}


def compile_agent_capabilities(
    app: Any,
    company_settings: dict[str, Any] | None = None,
    role: str | None = None,
    *,
    caller_gates: frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compile explicitly marked live FastAPI operations into model tools.

    FastAPI/OpenAPI remains the schema source of truth. Direct module operations
    are limited to first-party loaded modules; core celerp.* routes may opt in.

    A confirm-first change compiles only with exactly one retry marker:
    ``x-celerp-agent-idempotent`` (a retry under the card's key answers the first
    result; ``keyed``) or ``x-celerp-agent-check`` naming the screen where the user
    looks when the outcome is unknown. ``x-celerp-agent-key`` names the route's key
    field: compile hides it and execute fills it from the tool call identity.

    When ``role`` is supplied, tools whose FastAPI permission dependencies (or
    ``x-celerp-agent-any-permission``) deny that role are omitted before the model
    sees them; ``caller_gates`` does the same for ``agent_gate`` rules (None: no
    filter). The route still re-checks authorization on execution.

    ``result_permissions`` and ``field_visibility`` say what of its asker's authority
    a capability's result depends on, so a kept read is replayed only while that
    still holds (``celerp.ai.service``).
    """
    settings = company_settings or {}
    openapi = app.openapi()
    schemas = ((openapi.get("components") or {}).get("schemas") or {})
    routes = _api_routes(app)
    compiled: dict[str, dict[str, Any]] = {}
    bulk_targets: dict[str, str] = {}

    for path, path_item in (openapi.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method_lower, operation in path_item.items():
            method = str(method_lower).upper()
            if method not in _AGENT_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("x-celerp-agent") is not True:
                continue
            download = operation.get("x-celerp-agent-download")
            mutation = method in _AGENT_MUTATIONS
            requires_confirmation = mutation and operation.get("x-celerp-agent-confirm") is not False
            if download is not None and method != "GET":
                raise ValueError(f"x-celerp-agent-download on a {method} route: {path}")
            if not requires_confirmation and download is None and not _json_response(operation):
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
            any_permission = _any_permission(operation, settings)
            if role is not None and (
                any(not role_has_permission(settings, role, permission) for permission in required_permissions)
                or (any_permission is not None
                    and not any(role_has_permission(settings, role, p) for p in any_permission))
            ):
                continue
            if caller_gates is not None and not set(route_gates(route)) <= caller_gates:
                continue

            keyed = operation.get("x-celerp-agent-idempotent") is True
            check = operation.get("x-celerp-agent-check")
            if requires_confirmation and keyed == bool(check):
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
            encoding = "json"
            uploads: tuple[str, ...] = ()
            request_body = operation.get("requestBody")
            if request_body is not None:
                if not isinstance(request_body, dict):
                    continue
                media = request_body.get("content") or {}
                if len(media) != 1 or next(iter(media)) not in ("application/json", _FORM, _MULTIPART):
                    continue
                encoding = {"application/json": "json", _FORM: "form", _MULTIPART: "multipart"}[next(iter(media))]
                raw_schema = (next(iter(media.values())) or {}).get("schema") or {}
                body_schema = _inline_local_refs(raw_schema, schemas)
                if not isinstance(body_schema, dict):
                    continue
                body_required = bool(request_body.get("required"))
                if encoding != "json":
                    props = body_schema.get("properties") or {}
                    uploads = tuple(name for name, prop in props.items() if _binary(prop))
                    if encoding == "form" and uploads:
                        continue
                    for name in uploads:
                        body_schema = _drop_field(body_schema, name)

            key_field = operation.get("x-celerp-agent-key") if requires_confirmation else None
            generated_secret = operation.get("x-celerp-agent-generated-secret") if requires_confirmation else None
            for field in (key_field, generated_secret):
                if field:
                    if body_schema is None:
                        raise ValueError(f"agent marker names a body field on a route with no body: {path}")
                    body_schema = _drop_field(body_schema, field)
            if body_schema is not None and uploads and not body_schema.get("properties"):
                body_schema = None  # the upload parts were the whole body

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
            if uploads:
                tool_properties["file_ids"] = _FILE_IDS
                tool_required.append("file_ids")
            if requires_confirmation:
                tool_properties["after_previous"] = _AFTER_PREVIOUS

            params: dict[str, Any] = {
                "type": "object",
                "properties": tool_properties,
                "additionalProperties": False,
            }
            if tool_required:
                params["required"] = tool_required

            if operation.get("x-celerp-agent-bulk-of"):
                bulk_targets[operation_id] = str(operation["x-celerp-agent-bulk-of"])
            compiled[operation_id] = {
                "name": operation_id,
                "method": method,
                "path": path,
                "module": owner,
                "path_names": tuple(path_properties),
                "query_names": tuple(query_properties),
                "expects_body": body_schema is not None,
                "encoding": encoding,
                "uploads": uploads,
                "key_field": key_field or None,
                "keyed": keyed,
                "check_screen": check or None,
                "generated_secret": generated_secret or None,
                "download": download,
                "selection": operation.get("x-celerp-agent-selection"),
                "outside": operation.get("x-celerp-agent-outside") is True,
                "credits": operation.get("x-celerp-agent-credits") is True,
                "bulk_of": None,
                "requires_confirmation": requires_confirmation,
                "required_permissions": required_permissions,
                "result_permissions": _result_permissions(operation, required_permissions),
                "field_visibility": operation.get("x-celerp-agent-field-visibility") is True,
                "tool": {
                    "type": "function",
                    "function": {
                        "name": operation_id,
                        # The route's own one-sentence summary: a docstring carries internal
                        # notes and costs context on every turn.
                        "description": str(route.summary or ""),
                        "parameters": params,
                    },
                },
            }

    # A bulk route names the single-record change it does for many records at once
    # ("METHOD path"); the tool for that change, when this caller has it.
    by_route = {f"{cap['method']} {cap['path']}": name for name, cap in compiled.items()}
    for name, target in bulk_targets.items():
        compiled[name]["bulk_of"] = by_route.get(target)
    return compiled


def agent_tool_specs(capabilities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the model tool specs for compiled capabilities, in stable name order."""
    return [capabilities[name]["tool"] for name in sorted(capabilities)]


def tool_call_key(tool_call_id: str) -> str:
    """The key a change's route receives, bound to the card's tool call: a retry of one
    card reuses it and two cards never share one."""
    return "agent:" + hashlib.sha256(str(tool_call_id).encode("utf-8")).hexdigest()[:32]


def _filled_path(template: str, names: Any, values: dict[str, Any]) -> str | dict[str, Any]:
    """``template`` with each ``{name}`` replaced by its quoted value, or an agent error."""
    url_path = template
    for name in names:
        raw = str(values[name])
        if "/" in raw or "\\" in raw:
            return _agent_error("invalid_path_parameter", "ai.err_invalid_arguments", "Path parameters cannot contain path separators.")
        url_path = url_path.replace("{" + name + "}", quote(raw, safe=""))
    if "{" in url_path or "}" in url_path:
        return _agent_error("invalid_path_parameter", "ai.err_invalid_arguments", "A path parameter could not be resolved.")
    return url_path


async def execute_agent_capability(
    app: Any,
    authorization: str,
    capability: dict[str, Any],
    arguments: dict[str, Any],
    tool_call_id: str,
    *,
    result_max_bytes: int = AGENT_RESULT_MAX_BYTES,
    load_file: Callable[[str], tuple[bytes, dict]] | None = None,
) -> dict[str, Any]:
    """Invoke one compiled capability through the same FastAPI app as the user.

    ``load_file`` reads a file the user attached (ownership checked by the loader);
    an upload tool needs it. A generated secret is returned once under ``shown_once``
    for the person who confirmed, unless the route answers ``linked: true`` (it kept
    an existing account and ignored the secret)."""
    if not authorization:
        return _agent_error("missing_authorization", "ai.err_no_authorization",
                            "The user authorization context is unavailable.")
    if not isinstance(arguments, dict):
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "Tool arguments must be a JSON object.")

    allowed_sections = {
        section for section, present in (
            ("path", bool(capability.get("path_names"))),
            ("query", bool(capability.get("query_names"))),
            ("body", bool(capability.get("expects_body"))),
            ("file_ids", bool(capability.get("uploads"))),
            ("after_previous", bool(capability.get("requires_confirmation"))),
        ) if present
    }
    unknown_sections = set(arguments) - allowed_sections
    if unknown_sections:
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "Tool arguments contain unsupported sections.")

    path_values = arguments.get("path") or {}
    query_values = arguments.get("query") or {}
    body = arguments.get("body") if capability.get("expects_body") else None
    if not isinstance(path_values, dict) or not isinstance(query_values, dict):
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "Path/query arguments must be JSON objects.")
    if capability.get("expects_body") and not isinstance(body, dict):
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "Request body must be a JSON object.")

    path_names = set(capability.get("path_names") or ())
    query_names = set(capability.get("query_names") or ())
    if set(path_values) - path_names or set(query_values) - query_names:
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "Tool arguments contain unknown path/query parameters.")
    if path_names - set(path_values):
        return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "A required path parameter is missing.")

    url_path = _filled_path(str(capability["path"]), capability.get("path_names") or (), path_values)
    if isinstance(url_path, dict):
        return url_path
    if capability.get("download"):
        # A file never travels through the chat: the answer is the screen's own link,
        # which serves it under the user's session.
        names = [n for n in capability.get("path_names") or () if "{" + n + "}" in capability["download"]]
        link = _filled_path(str(capability["download"]), names, path_values)
        return link if isinstance(link, dict) else {"ok": True, "status": 200, "data": {"download": link}}

    params: list[tuple[str, str]] = []
    for name, value in query_values.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is not None:
                params.append((name, str(item)))

    if capability.get("key_field") or capability.get("generated_secret"):
        body = dict(body or {})
    if capability.get("key_field"):
        body = _fill_field(body, capability["key_field"], tool_call_key(tool_call_id))
    secret = None
    if capability.get("generated_secret"):
        secret = secrets.token_urlsafe(16)
        body[capability["generated_secret"]] = secret

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    uploads = tuple(capability.get("uploads") or ())
    if uploads:
        file_ids = arguments.get("file_ids")
        if not isinstance(file_ids, list) or not file_ids or not all(isinstance(f, str) for f in file_ids):
            return _agent_error("invalid_arguments", "ai.err_invalid_arguments", "file_ids must list attached file ids.")
        if len(uploads) > 1 and len(file_ids) != len(uploads):
            return _agent_error("invalid_arguments", "ai.err_invalid_arguments",
                                f"This tool takes {len(uploads)} files, one per part, in order.")
        for index, file_id in enumerate(file_ids):
            try:
                if load_file is None:
                    raise FileNotFoundError(file_id)
                content, meta = load_file(file_id)
            except (FileNotFoundError, PermissionError, ValueError):
                return _agent_error("file_not_found", "error.file_not_found",
                                    "That file is not attached to this conversation.")
            part = uploads[0] if len(uploads) == 1 else uploads[index]
            files.append((part, (str(meta.get("filename") or file_id), content,
                                 str(meta.get("content_type") or "application/octet-stream"))))

    if capability.get("encoding", "json") == "json":
        sent: dict[str, Any] = {"json": body}
    else:
        form = {k: v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                for k, v in (body or {}).items() if v is not None}
        sent = {"data": form, "files": files} if files else {"data": form}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://celerp.internal") as client:
        try:
            response = await asyncio.wait_for(
                client.request(
                    str(capability["method"]),
                    url_path,
                    params=params,
                    headers={"Authorization": authorization, "Accept-Language": current_lang()},
                    follow_redirects=False,
                    **sent,
                ),
                timeout=deadlines.TOOL_STEP_S,
            )
        except asyncio.TimeoutError:
            return _agent_error(
                "capability_timeout", "ai.err_capability_timeout",
                f"The operation did not finish within {int(deadlines.TOOL_STEP_S)} seconds.",
                seconds=int(deadlines.TOOL_STEP_S),
            )

    confirmed = bool(capability.get("requires_confirmation"))
    try:
        data = response.json()
    except Exception:
        if response.is_success and confirmed:
            # A change that succeeded with no JSON body (204, a redirect target's text):
            # never turn that success into an apparent failure.
            return {"ok": True, "status": response.status_code}
        return _agent_error(
            "non_json_response", "ai.action_request_failed",
            "The capability returned a non-JSON response.",
            http_status=response.status_code, status=response.status_code,
        )

    # A refusal the route answered with directly, not through an exception handler, meets
    # the same check here, where every answer the AI keeps arrives.
    error_body.check(f"{capability['method']} {url_path}", response.status_code,
                     data.get("detail") if isinstance(data, dict) else data, agent=True)
    shown_once = (
        {capability["generated_secret"]: secret}
        if secret is not None and response.is_success and not (isinstance(data, dict) and data.get("linked") is True)
        else None
    )
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > result_max_bytes:
        if response.is_success and confirmed:
            # The write already returned success. Never turn that definite success into
            # an apparent failure merely because its response is too large to retain;
            # doing so would invite a needless retry of a mutation that already landed.
            result: dict[str, Any] = {
                "ok": True, "status": response.status_code,
                "data": {"result_omitted": True, "detail": "Operation completed successfully."},
            }
        else:
            return _agent_error(
                "result_too_large", "ai.err_too_much_data",
                "The capability result is too large; narrow the query or use pagination.",
                http_status=response.status_code,
            )
    else:
        result = {"ok": response.is_success, "status": response.status_code, "data": data}
    if shown_once:
        result["shown_once"] = shown_once
    return result

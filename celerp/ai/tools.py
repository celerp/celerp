# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""AI tools - ERP-aware callable functions for the AI assistant.

Tools are pure functions that query the database and return structured data.
The AI service calls these to answer queries about inventory, sales, etc.
Each tool takes (session, company_id) plus optional parameters.

Tool registry: TOOLS dict maps tool_name -> ToolDef.
Callers invoke execute_tool(name, params, session, company_id).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import quote

import httpx
from fastapi.routing import APIRoute
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection
from celerp.modules.loader import loaded_modules
from celerp.modules.registry import get_enabled
from celerp.services.reorder import is_below_reorder


@dataclass
class ToolDef:
    name: str
    description: str
    params: list[str]
    fn: Callable[..., Awaitable[dict[str, Any]]]


# -- Shared query helper ----------------------------------------------------

async def _projections(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_type: str,
) -> list[Projection]:
    """Fetch all projections for a company + entity type."""
    return list(
        (await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == entity_type,
            )
        )).scalars().all()
    )


# -- Tool implementations --------------------------------------------------

async def _dashboard_kpis(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return high-level business KPIs."""
    items = await _projections(session, company_id, "item")
    docs = await _projections(session, company_id, "doc")
    deals = await _projections(session, company_id, "deal")

    return {
        "total_items": len(items),
        "inventory_value": round(sum(float(i.state.get("total_cost", 0) or 0) for i in items), 2),
        "low_stock_items": sum(1 for i in items if is_below_reorder(i.state)),
        "ar_outstanding": round(
            sum(
                float(d.state.get("amount_outstanding", 0) or 0)
                for d in docs if d.state.get("doc_type") == "invoice"
            ), 2
        ),
        "active_deals": sum(1 for d in deals if d.state.get("status") not in {"won", "lost"}),
    }


async def _low_stock_items(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List items at or below their reorder point."""
    items = await _projections(session, company_id, "item")
    low = sorted(
        [
            {"sku": r.state.get("sku"), "name": r.state.get("name"), "quantity": r.state.get("quantity", 0)}
            for r in items if is_below_reorder(r.state)
        ],
        key=lambda x: float(x["quantity"] or 0),
    )
    return {"items": low[:int(limit)], "total_count": len(low)}


async def _outstanding_invoices(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List unpaid invoices with outstanding balances."""
    docs = await _projections(session, company_id, "doc")
    invoices = sorted(
        [
            {
                "doc_number": r.state.get("doc_number"),
                "contact_name": r.state.get("contact_name"),
                "amount_outstanding": r.state.get("amount_outstanding", 0),
                "due_date": r.state.get("due_date"),
                "status": r.state.get("status"),
            }
            for r in docs
            if r.state.get("doc_type") == "invoice"
            and float(r.state.get("amount_outstanding", 0) or 0) > 0
        ],
        key=lambda x: float(x["amount_outstanding"] or 0),
        reverse=True,
    )
    return {"invoices": invoices[:int(limit)], "total_count": len(invoices)}


async def _top_items_by_value(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 10, **_,
) -> dict:
    """List items ranked by total inventory value (qty x cost)."""
    items = await _projections(session, company_id, "item")
    ranked = sorted(
        [
            {
                "sku": r.state.get("sku"),
                "name": r.state.get("name"),
                "quantity": r.state.get("quantity", 0),
                "total_cost": r.state.get("total_cost", 0),
            }
            for r in items
        ],
        key=lambda x: float(x["total_cost"] or 0),
        reverse=True,
    )
    return {"items": ranked[:int(limit)]}


async def _active_deals_summary(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Summarise CRM pipeline - open deals by stage."""
    deals = await _projections(session, company_id, "deal")
    pipeline: dict[str, dict] = {}
    for r in deals:
        status = r.state.get("status", "unknown")
        if status in {"won", "lost"}:
            continue
        stage = r.state.get("stage", status)
        if stage not in pipeline:
            pipeline[stage] = {"count": 0, "value": 0.0}
        pipeline[stage]["count"] += 1
        pipeline[stage]["value"] += float(r.state.get("value", 0) or 0)
    return {
        "stages": [
            {"stage": k, "count": v["count"], "value": round(v["value"], 2)}
            for k, v in pipeline.items()
        ]
    }


async def _active_contacts_list(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return a list of all contacts for entity mapping."""
    contacts = await _projections(session, company_id, "contact")
    return {
        "contacts": [
            {"id": str(r.entity_id), "name": r.state.get("name"), "type": r.state.get("contact_type")}
            for r in contacts
        ]
    }


async def _active_items_list(session: AsyncSession, company_id: uuid.UUID, **_) -> dict:
    """Return a list of all active items for entity mapping."""
    items = await _projections(session, company_id, "item")
    return {
        "items": [
            {"id": str(r.entity_id), "name": r.state.get("name"), "sku": r.state.get("sku")}
            for r in items
        ]
    }


async def _dormant_contacts(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List contacts with no invoice/bill activity in the past 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    contacts = await _projections(session, company_id, "contact")
    docs = await _projections(session, company_id, "doc")

    # Build map: contact_id -> latest doc updated_at
    latest_activity: dict[str, datetime] = {}
    for doc in docs:
        if doc.state.get("doc_type") not in ("invoice", "bill"):
            continue
        contact_id = doc.state.get("contact_id")
        if not contact_id:
            continue
        ts = doc.updated_at
        if ts and (contact_id not in latest_activity or ts > latest_activity[contact_id]):
            latest_activity[contact_id] = ts

    dormant = []
    for r in contacts:
        cid = str(r.entity_id)
        last = latest_activity.get(cid)
        ts = last.replace(tzinfo=timezone.utc) if last and last.tzinfo is None else last
        if ts is None or ts < cutoff:
            dormant.append({
                "id": cid,
                "name": r.state.get("name"),
                "type": r.state.get("contact_type"),
                "last_activity": last.isoformat() if last else None,
            })
    return {"dormant_contacts": dormant[:int(limit)], "total_count": len(dormant)}


async def _top_sellers(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 10, **_,
) -> dict:
    """List best selling items by aggregating invoice line items."""
    docs = await _projections(session, company_id, "doc")

    sales: dict[str, dict] = {}
    for doc in docs:
        if doc.state.get("doc_type") != "invoice":
            continue
        for line in doc.state.get("line_items", []):
            key = line.get("item_id") or line.get("sku") or line.get("description", "unknown")
            qty = float(line.get("quantity", 0) or 0)
            revenue = float(line.get("line_total", 0) or 0)
            if key not in sales:
                sales[key] = {
                    "sku": line.get("sku") or key,
                    "name": line.get("description") or line.get("name") or key,
                    "qty_sold": 0.0,
                    "revenue": 0.0,
                }
            sales[key]["qty_sold"] += qty
            sales[key]["revenue"] += revenue

    ranked = sorted(sales.values(), key=lambda x: x["qty_sold"], reverse=True)
    return {"top_sellers": ranked[:int(limit)], "total_count": len(ranked)}


async def _pending_pos(
    session: AsyncSession, company_id: uuid.UUID, limit: int = 20, **_,
) -> dict:
    """List pending purchase orders."""
    docs = await _projections(session, company_id, "doc")
    pos = [
        {
            "doc_number": r.state.get("doc_number"),
            "contact_name": r.state.get("contact_name"),
            "total": r.state.get("total", 0),
            "status": r.state.get("status"),
        }
        for r in docs
        if r.state.get("doc_type") == "po"
        and r.state.get("status") not in ("received", "billed", "void")
    ]
    return {"pending_pos": pos[:int(limit)], "total_count": len(pos)}


# -- Registry ---------------------------------------------------------------

TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef("dashboard_kpis", "Return high-level KPIs: inventory value, AR outstanding, low stock count, active deals.", [], _dashboard_kpis),
        ToolDef("low_stock_items", "List items at or below their reorder point (defaults to zero/negative quantity for items without a reorder point set). Param: limit (default 20).", ["limit"], _low_stock_items),
        ToolDef("outstanding_invoices", "List unpaid invoices. Param: limit (default 20).", ["limit"], _outstanding_invoices),
        ToolDef("top_items_by_value", "List items ranked by total inventory value. Param: limit (default 10).", ["limit"], _top_items_by_value),
        ToolDef("active_deals_summary", "Summarise the CRM pipeline - open deals by stage with count and total value.", [], _active_deals_summary),
        ToolDef("active_contacts_list", "Return a list of all contacts for entity mapping.", [], _active_contacts_list),
        ToolDef("active_items_list", "Return a list of all active items for entity mapping.", [], _active_items_list),
        ToolDef("dormant_contacts", "List contacts with no recent activity. Param: limit (default 20).", ["limit"], _dormant_contacts),
        ToolDef("top_sellers", "List best selling items. Param: limit (default 10).", ["limit"], _top_sellers),
        ToolDef("pending_pos", "List pending purchase orders. Param: limit (default 20).", ["limit"], _pending_pos),
    ]
}


async def execute_tool(
    name: str,
    params: dict[str, Any],
    session: AsyncSession,
    company_id: uuid.UUID,
) -> dict[str, Any]:
    """Run a tool by name. Raises KeyError if name is unknown."""
    return await TOOLS[name].fn(session=session, company_id=company_id, **params)


# ---------------------------------------------------------------------------
# Generic agent capability adapter
# ---------------------------------------------------------------------------

_AGENT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
_AGENT_MUTATIONS = frozenset({"POST", "PUT", "PATCH"})
_AGENT_RESULT_MAX_BYTES = 64 * 1024
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


def _agent_route_owner(route: APIRoute, company_settings: dict[str, Any]) -> str | None:
    """Return core/module owner when direct agent invocation is allowed, else None."""
    endpoint_module = str(getattr(route.endpoint, "__module__", "") or "")
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


def compile_agent_capabilities(app: Any, company_settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Compile explicitly marked live FastAPI operations into model tools.

    FastAPI/OpenAPI remains the schema source of truth. Direct module operations
    are limited to first-party loaded modules; core celerp.* routes may opt in.
    """
    settings = company_settings or {}
    openapi = app.openapi()
    schemas = ((openapi.get("components") or {}).get("schemas") or {})
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
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
                route for route in routes
                if (getattr(route, "path_format", None) or route.path) == path
                and method in (route.methods or set())
            ]
            if len(matching) != 1:
                continue
            route = matching[0]
            owner = _agent_route_owner(route, settings)
            if owner is None:
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

            inject_idempotency = False
            if method in _AGENT_MUTATIONS:
                if body_schema is None:
                    continue
                props = body_schema.get("properties")
                if not isinstance(props, dict) or "idempotency_key" not in props:
                    continue
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


async def execute_agent_capability(
    app: Any,
    authorization: str,
    capability: dict[str, Any],
    arguments: dict[str, Any],
    tool_call_id: str,
    *,
    result_max_bytes: int = _AGENT_RESULT_MAX_BYTES,
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
        response = await client.request(
            str(capability["method"]),
            url_path,
            params=params,
            json=body,
            headers={"Authorization": authorization},
            follow_redirects=False,
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
        return _agent_error(
            "result_too_large",
            "The capability result is too large; narrow the query or use pagination.",
            status=response.status_code,
        )
    return {"ok": response.is_success, "status": response.status_code, "data": data}

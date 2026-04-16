# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

import httpx

from ui.config import API_BASE

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"API {status}: {detail}")


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
        follow_redirects=True,
    )


def _raise(r: httpx.Response) -> httpx.Response:
    if r.is_redirect:
        raise APIError(r.status_code, f"Unexpected redirect to {r.headers.get('location', '?')}")
    if r.is_error:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        logger.warning("API %s: %s", r.status_code, detail)
        raise APIError(r.status_code, detail)
    return r


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

async def batch_import(token: str, path: str, records: list[dict], upsert: bool = False) -> dict:
    """POST a CIF batch import payload to an API path.

    This is intentionally generic so UI routes can reuse it for items/docs/lists/crm/etc.
    """
    async with _client(token) as c:
        r = _raise(await c.post(path, json={"records": records, "upsert": upsert}))
        return r.json()


# ---------------------------------------------------------------------------
# Auth (no token needed)
# ---------------------------------------------------------------------------

async def bootstrap_status() -> bool:
    """Returns True if the system has been bootstrapped (any user exists).

    Raises APIError(503) if the API is unreachable — callers should catch this
    and render a friendly "API not running" page rather than a 500.
    """
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=5.0) as c:
            r = await c.get("/auth/bootstrap-status")
            if r.is_error:
                return False
            return r.json().get("bootstrapped", False)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise APIError(503, f"Cannot reach API at {API_BASE} — is the API server running?") from exc


async def has_data(token: str) -> bool:
    """Returns True if the company has any inventory/docs/contacts loaded."""
    async with _client(token) as c:
        try:
            val = _raise(await c.get("/items/valuation")).json()
            return (val.get("item_count", 0) or 0) > 0
        except APIError:
            return False


async def login(email: str, password: str) -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
        r = _raise(await c.post("/auth/login", json={"email": email, "password": password}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def login_force(email: str, password: str) -> tuple[str, str]:
    """Like login() but evicts other active sessions first."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
        r = _raise(await c.post("/auth/login-force", json={"email": email, "password": password}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def change_password(token: str, current_password: str, new_password: str) -> str:
    """Change password for the authenticated user. Returns detail message."""
    async with _client(token) as c:
        r = _raise(await c.post("/auth/change-password", json={
            "current_password": current_password, "new_password": new_password,
        }))
        return r.json()["detail"]


async def register(company_name: str, email: str, name: str, password: str) -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
        r = _raise(await c.post("/auth/register", json={
            "company_name": company_name, "email": email, "name": name, "password": password,
        }))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Exchange refresh token for new (access_token, refresh_token). Raises APIError on failure."""
    async with httpx.AsyncClient(base_url=API_BASE, timeout=10.0) as c:
        r = _raise(await c.post("/auth/token/refresh", json={"refresh_token": refresh_token}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def my_companies(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/auth/my-companies")).json()


async def switch_company(token: str, company_id: str) -> str:
    async with _client(token) as c:
        r = _raise(await c.post(f"/auth/switch-company/{company_id}"))
        return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Company / settings
# ---------------------------------------------------------------------------

def _flatten_company(data: dict) -> dict:
    """Flatten settings sub-fields into top-level for UI convenience."""
    settings = data.get("settings") or {}
    for k in ("currency", "timezone", "fiscal_year_start", "tax_id", "phone", "address", "vertical", "email"):
        if k not in data:
            data[k] = settings.get(k)
    # Expose dashboard preferences at top level
    dashboard = settings.get("dashboard") or {}
    data["docs_default_preset"] = dashboard.get("docs_default_preset", "last_12m")
    data["default_per_page"] = dashboard.get("per_page", 50)
    return data


async def get_company(token: str) -> dict:
    async with _client(token) as c:
        data = _raise(await c.get("/companies/me")).json()
        return _flatten_company(data)


async def patch_company(token: str, data: dict) -> dict:
    """Patch company. Settings sub-fields and dashboard preferences are merged into
    the settings dict; top-level fields (name, slug) are patched directly."""
    _SETTINGS_FIELDS = {"currency", "timezone", "fiscal_year_start", "tax_id", "phone", "address", "email"}
    _DASHBOARD_FIELDS = {"docs_default_preset", "default_per_page"}
    settings_patch = {k: v for k, v in data.items() if k in _SETTINGS_FIELDS}
    dashboard_patch = {}
    # Map default_per_page to per_page for storage
    for k in _DASHBOARD_FIELDS:
        if k in data:
            storage_key = "per_page" if k == "default_per_page" else k
            dashboard_patch[storage_key] = data[k]
    direct_patch = {k: v for k, v in data.items()
                    if k not in _SETTINGS_FIELDS and k not in _DASHBOARD_FIELDS}
    async with _client(token) as c:
        if settings_patch or dashboard_patch:
            current = _raise(await c.get("/companies/me")).json()
            merged = {**(current.get("settings") or {}), **settings_patch}
            if dashboard_patch:
                merged["dashboard"] = {**(merged.get("dashboard") or {}), **dashboard_patch}
            _raise(await c.patch("/companies/me", json={"settings": merged}))
        if direct_patch:
            _raise(await c.patch("/companies/me", json=direct_patch))
        raw = _raise(await c.get("/companies/me")).json()
        return _flatten_company(raw)


async def create_company(token: str, company_name: str) -> str:
    """Create a new company linked to the current user. Returns new JWT scoped to it."""
    async with _client(token) as c:
        r = _raise(await c.post("/companies", json={"name": company_name}))
        return r.json()["access_token"]


async def get_item_schema(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/item-schema")).json()


async def patch_item_schema(token: str, fields: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/item-schema", json={"fields": fields})).json()


async def get_all_category_schemas(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/category-schemas")).json()


async def get_category_schema(token: str, category: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get(f"/companies/me/category-schema/{category}")).json()


async def patch_category_schema(token: str, category: str, fields: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/companies/me/category-schema/{category}", json={"fields": fields})).json()


async def merge_category_schemas(token: str, schemas: dict[str, list[dict]]) -> dict:
    """Auto-merge attribute keys from import into category schemas."""
    async with _client(token) as c:
        return _raise(await c.post("/companies/me/category-schemas/merge", json={"schemas": schemas})).json()


async def get_column_prefs(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/column-prefs")).json()


async def patch_column_prefs(token: str, prefs: dict[str, list[str]]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/column-prefs", json={"prefs": prefs})).json()


async def get_locations(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/locations")).json()


async def create_location(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/companies/me/locations", json=data)).json()


async def delete_location(token: str, location_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/companies/me/locations/{location_id}")).json()


async def get_users(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/users")).json()


async def create_user(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/companies/me/users", json=data)).json()


async def patch_user(token: str, user_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/companies/me/users/{user_id}", json=data)).json()


async def get_taxes(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/taxes")).json()


async def patch_taxes(token: str, taxes: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/taxes", json={"taxes": taxes})).json()


async def get_payment_terms(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/payment-terms")).json()


async def patch_payment_terms(token: str, terms: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/payment-terms", json={"terms": terms})).json()


async def get_purchasing_taxes(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/purchasing-taxes")).json()


async def patch_purchasing_taxes(token: str, taxes: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/purchasing-taxes", json={"taxes": taxes})).json()


async def get_purchasing_payment_terms(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/purchasing-payment-terms")).json()


async def patch_purchasing_payment_terms(token: str, terms: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/purchasing-payment-terms", json={"terms": terms})).json()


async def get_terms_conditions(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/terms-conditions")).json()


async def patch_terms_conditions(token: str, templates: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/terms-conditions", json={"templates": templates})).json()


async def get_price_lists(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/price-lists")).json()


async def patch_price_lists(token: str, price_lists: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/price-lists", json={"price_lists": price_lists})).json()


async def get_default_price_list(token: str) -> str:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/default-price-list")).json()


async def patch_default_price_list(token: str, name: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/default-price-list", json={"name": name})).json()


async def get_units(token: str) -> list[dict]:
    """GET /companies/me/units → list of unit dicts."""
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/units")).json()


async def patch_units(token: str, units: list[dict]) -> list[dict]:
    """PUT /companies/me/units → replace units list."""
    async with _client(token) as c:
        return _raise(await c.put("/companies/me/units", json={"units": units})).json()


# ---------------------------------------------------------------------------
# Global search
# ---------------------------------------------------------------------------

async def global_search(token: str, q: str) -> dict:
    """Search across items, contacts, docs."""
    async with _client(token) as c:
        return _raise(await c.get("/search", params={"q": q})).json()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

async def list_items(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/items", params=params or {})).json()


async def get_item(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/items/{entity_id}")).json()


async def patch_item(token: str, entity_id: str, fields_changed: dict) -> dict:
    """Patch item fields. Pass a flat {field: value} dict; wraps into {field: {old, new}} format."""
    wrapped = {k: (v if isinstance(v, dict) and "new" in v else {"old": None, "new": v}) for k, v in fields_changed.items()}
    async with _client(token) as c:
        return _raise(await c.patch(f"/items/{entity_id}", json={"fields_changed": wrapped})).json()


async def upload_attachment(token: str, entity_id: str, file) -> dict:
    async with _client(token) as c:
        content = await file.read() if hasattr(file, "read") else file.file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        return _raise(await c.post(
            f"/items/{entity_id}/attachments",
            files={"file": (filename, content, content_type)},
        )).json()


async def delete_attachment(token: str, entity_id: str, att_id: str) -> None:
    async with _client(token) as c:
        _raise(await c.delete(f"/items/{entity_id}/attachments/{att_id}"))


async def bulk_attach(token: str, file) -> dict:
    async with _client(token) as c:
        content = await file.read() if hasattr(file, "read") else file.file.read()
        filename = getattr(file, "filename", "attachments.zip")
        return _raise(await c.post(
            "/items/attachments/bulk",
            files={"file": (filename, content, "application/zip")},
        )).json()


async def get_valuation(token: str, category: str | None = None, status: str | None = None) -> dict:
    params: dict = {}
    if category:
        params["category"] = category
    if status:
        params["status"] = status
    async with _client(token) as c:
        return _raise(await c.get("/items/valuation", params=params)).json()


async def get_item_field_values(token: str, field: str) -> list[str]:
    async with _client(token) as c:
        return _raise(await c.get("/items/field-values", params={"field": field})).json().get("values", [])


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

async def list_docs(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/docs", params=params or {})).json()


async def list_contact_docs(token: str, contact_id: str, params: dict | None = None) -> dict:
    p = {"contact_id": contact_id, **(params or {})}
    async with _client(token) as c:
        return _raise(await c.get("/docs", params=p)).json()


async def get_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}")).json()


async def get_doc_summary(token: str, doc_type: str = "") -> dict:
    params = {}
    if doc_type:
        params["doc_type"] = doc_type
    async with _client(token) as c:
        return _raise(await c.get("/docs/summary", params=params)).json()


async def patch_doc(token: str, entity_id: str, data: dict) -> dict:
    """data is a flat dict of field->value; wraps into fields_changed format."""
    fields_changed = {k: {"old": None, "new": v} for k, v in data.items()}
    async with _client(token) as c:
        return _raise(await c.patch(f"/docs/{entity_id}", json={"fields_changed": fields_changed})).json()


async def create_doc(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/docs", json=data)).json()


async def get_doc_sequences(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/docs/sequences")).json()


async def patch_doc_sequence(token: str, doc_type: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/docs/sequences/{doc_type}", json=data)).json()


async def finalize_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/finalize")).json()


async def send_doc(token: str, entity_id: str, data: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/send", json=data or {})).json()


async def void_doc(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/void", json={"reason": reason})).json()


async def revert_doc_to_draft(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/revert-to-draft", json={"reason": reason})).json()


async def unvoid_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/unvoid", json={})).json()


async def fulfill_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/fulfill", json={})).json()


async def unfulfill_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/unfulfill", json={})).json()


async def delete_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}")).json()


async def record_payment(token: str, entity_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/payment", json=data)).json()


# ---------------------------------------------------------------------------
# CRM / contacts
# ---------------------------------------------------------------------------

async def list_contacts(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        data = _raise(await c.get("/crm/contacts", params=params or {})).json()
        # Normalise: backend now returns {items, total}; keep backward compat for callers
        if isinstance(data, list):
            return {"items": data, "total": len(data)}
        return data


async def get_contact(token: str, contact_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/crm/contacts/{contact_id}")).json()


async def patch_contact(token: str, contact_id: str, data: dict) -> dict:
    """data is a flat dict of field->value; wraps into fields_changed format."""
    fields_changed = {k: {"old": None, "new": v} for k, v in data.items()}
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}", json={"fields_changed": fields_changed})).json()


async def create_contact(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/crm/contacts", json=data)).json()


async def list_memos(token: str, params: dict | None = None) -> dict:
    """Returns {items: [...], total: N}."""
    async with _client(token) as c:
        return _raise(await c.get("/crm/memos", params=params or {})).json()


async def get_memo_summary(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/crm/memos/summary")).json()


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

async def get_chart(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/accounting/chart")).json()


async def seed_chart(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/chart/seed")).json()


async def create_account(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/accounts", json=data)).json()


async def patch_account(token: str, code: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/accounting/accounts/{code}", json=data)).json()


async def get_trial_balance(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/accounting/trial-balance", params=params or {})).json()


async def get_pnl(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/accounting/pnl", params=params or {})).json()


async def get_balance_sheet(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/accounting/balance-sheet", params=params or {})).json()


async def get_bank_accounts(token: str, include_inactive: bool = False) -> dict:
    async with _client(token) as c:
        params = {"include_inactive": "true"} if include_inactive else {}
        return _raise(await c.get("/accounting/bank-accounts", params=params)).json()


async def get_bank_account(token: str, bank_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/accounting/bank-accounts/{bank_id}")).json()


async def create_bank_account(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/bank-accounts", json=data)).json()


async def patch_bank_account(token: str, bank_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/accounting/bank-accounts/{bank_id}", json=data)).json()


async def create_transfer(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/transfers", json=data)).json()


async def start_reconciliation(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/reconciliation/start", json=data)).json()


async def get_reconciliation(token: str, session_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/accounting/reconciliation/{session_id}")).json()


async def match_reconciliation(token: str, session_id: str, je_ids: list[str]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/match", json={"je_ids": je_ids})).json()


async def complete_reconciliation(token: str, session_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/complete")).json()


async def import_recon_csv(token: str, session_id: str, content: bytes, filename: str, column_map: dict | None = None) -> dict:
    import json as _json
    async with _client(token) as c:
        files = {"file": (filename, content, "text/csv")}
        data = {"column_map": _json.dumps(column_map)} if column_map else {}
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/import-csv", files=files, data=data)).json()


async def get_statement_lines(token: str, session_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/accounting/reconciliation/{session_id}/statement-lines")).json()


async def auto_match_recon(token: str, session_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/auto-match")).json()


async def match_recon_line(token: str, session_id: str, line_id: str, je_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/match",
            json={"je_id": je_id},
        )).json()


async def unmatch_recon_line(token: str, session_id: str, line_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch")).json()


async def create_recon_expense(token: str, session_id: str, line_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/create",
            json=data,
        )).json()


async def split_recon_line(token: str, session_id: str, line_id: str, splits: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/split",
            json={"splits": splits},
        )).json()


async def skip_recon_line(token: str, session_id: str, line_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}",
            json={"status": "skipped"},
        )).json()


async def attach_recon_line(token: str, session_id: str, line_id: str, content: bytes, filename: str) -> dict:
    async with _client(token) as c:
        files = {"file": (filename, content, "application/octet-stream")}
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/attach",
            files=files,
        )).json()


async def bulk_confirm_recon(token: str, session_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/bulk-confirm")).json()


async def write_off_recon(token: str, session_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/write-off", json=data)).json()


async def get_recon_rules(token: str, bank_account_id: str | None = None) -> dict:
    async with _client(token) as c:
        params = {"bank_account_id": bank_account_id} if bank_account_id else {}
        return _raise(await c.get("/accounting/rules", params=params)).json()


async def create_recon_rule(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/rules", json=data)).json()


async def patch_recon_rule(token: str, rule_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/accounting/rules/{rule_id}", json=data)).json()


async def delete_recon_rule(token: str, rule_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/accounting/rules/{rule_id}")).json()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

async def get_ar_aging(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/reports/ar-aging", params=params or {})).json()


async def get_ap_aging(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/reports/ap-aging", params=params or {})).json()


async def get_sales_report(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/reports/sales", params=params or {})).json()


async def get_purchases_report(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/reports/purchases", params=params or {})).json()


async def get_expiring(token: str, days: int = 30) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/reports/expiring", params={"days": days})).json()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

async def list_subscriptions(token: str, params: dict | None = None) -> dict:
    """Returns {items: [...], total: N}."""
    async with _client(token) as c:
        return _raise(await c.get("/subscriptions", params=params or {})).json()


async def get_subscription(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/subscriptions/{entity_id}")).json()


async def list_ledger(token: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p.setdefault("resolve", "true")
    async with _client(token) as c:
        return _raise(await c.get("/ledger", params=p)).json()


async def create_subscription(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/subscriptions", json=data)).json()


async def pause_subscription(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/pause")).json()


async def resume_subscription(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/resume")).json()


async def generate_subscription(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/generate")).json()


# ---------------------------------------------------------------------------
# Manufacturing
# ---------------------------------------------------------------------------

async def list_mfg_orders(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/manufacturing")).json()


async def get_mfg_order(token: str, order_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/manufacturing/{order_id}")).json()


async def create_mfg_order(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/manufacturing", json=data)).json()


async def start_mfg_order(token: str, order_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/start")).json()


async def complete_mfg_step(token: str, order_id: str, step_id: str, notes: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/step", json={"step_id": step_id, "notes": notes})).json()


async def consume_mfg_input(token: str, order_id: str, item_id: str, quantity: float) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/consume", json={"item_id": item_id, "quantity": quantity})).json()


async def complete_mfg_order(token: str, order_id: str, data: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/complete", json=data or {})).json()


async def cancel_mfg_order(token: str, order_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/cancel", json={"reason": reason})).json()


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

async def list_boms(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/manufacturing/boms")).json()


async def get_bom(token: str, bom_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/manufacturing/boms/{bom_id}")).json()


async def create_bom(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/manufacturing/boms", json=data)).json()


async def update_bom(token: str, bom_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.put(f"/manufacturing/boms/{bom_id}", json=data)).json()


async def delete_bom(token: str, bom_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/manufacturing/boms/{bom_id}")).json()


# ---------------------------------------------------------------------------
# Scanning disabled — module not yet complete
# ---------------------------------------------------------------------------

# async def scan_once(token: str, code: str, location_id: str | None = None) -> dict:
#     async with _client(token) as c:
#         payload: dict = {"code": code}
#         if location_id:
#             payload["location_id"] = location_id
#         return _raise(await c.post("/scanning/scan", json=payload)).json()
#
#
# async def resolve_scan(token: str, code: str) -> dict:
#     async with _client(token) as c:
#         return _raise(await c.get(f"/scanning/resolve/{code}")).json()
#
#
# async def start_batch(token: str, location_id: str | None = None) -> dict:
#     async with _client(token) as c:
#         return _raise(await c.post("/scanning/batch", json={"location_id": location_id})).json()
#
#
# async def complete_batch(token: str, batch_id: str) -> dict:
#     async with _client(token) as c:
#         return _raise(await c.post(f"/scanning/batch/{batch_id}/complete")).json()
#
#
# async def scan_batch(token: str, scans: list[dict]) -> dict:
#     async with _client(token) as c:
#         return _raise(await c.post("/scanning/scan/batch", json={"scans": scans})).json()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

async def export_items_csv(token: str, params: dict | None = None) -> bytes:
    async with _client(token) as c:
        r = _raise(await c.get("/items/export/csv", params=params or {}))
        return r.content


async def export_docs_csv(token: str, params: dict | None = None) -> bytes:
    async with _client(token) as c:
        r = _raise(await c.get("/docs/export/csv", params=params or {}))
        return r.content


async def export_contacts_csv(token: str, params: dict | None = None) -> bytes:
    async with _client(token) as c:
        r = _raise(await c.get("/crm/contacts/export/csv", params=params or {}))
        return r.content


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

async def list_lists(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/lists", params=params or {})).json()


async def get_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/lists/{entity_id}")).json()


async def get_list_summary(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/lists/summary")).json()


async def create_list(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/lists", json=data)).json()


async def patch_list(token: str, entity_id: str, data: dict) -> dict:
    fields_changed = {k: {"old": None, "new": v} for k, v in data.items()}
    async with _client(token) as c:
        return _raise(await c.patch(f"/lists/{entity_id}", json={"fields_changed": fields_changed})).json()


async def send_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/send", json={})).json()


async def accept_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/accept", json={})).json()


async def complete_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/complete", json={})).json()


async def void_list(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/void", json={"reason": reason} if reason else {})).json()


async def delete_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/lists/{entity_id}")).json()


async def convert_list(token: str, entity_id: str, target_type: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/convert", json={"target_type": target_type})).json()


async def duplicate_list(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/duplicate", json={})).json()


async def add_doc_note(token: str, entity_id: str, text: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/notes", json={"text": text})).json()


async def add_list_note(token: str, entity_id: str, text: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/notes", json={"text": text})).json()


async def export_lists_csv(token: str, params: dict | None = None) -> bytes:
    async with _client(token) as c:
        r = _raise(await c.get("/lists/export/csv", params=params or {}))
        return r.content


# ---------------------------------------------------------------------------
# T1: Document conversion (quotation → invoice)
# ---------------------------------------------------------------------------

async def convert_doc(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/convert")).json()


# ---------------------------------------------------------------------------
# T2: PO receive
# ---------------------------------------------------------------------------

async def receive_po(token: str, entity_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/receive", json=data)).json()


async def return_consignment_items(token: str, entity_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/return-items", json=data)).json()


# ---------------------------------------------------------------------------
# T3: Item actions
# ---------------------------------------------------------------------------

async def adjust_item(token: str, entity_id: str, new_qty: float) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/adjust", json={"new_qty": new_qty})).json()


async def transfer_item(token: str, entity_id: str, location_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/transfer", json={"location_id": location_id})).json()


async def set_item_price(token: str, entity_id: str, price_type: str, new_price: float) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/price", json={"price_type": price_type, "new_price": new_price})).json()


async def set_item_status(token: str, entity_id: str, status: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/status", json={"status": status})).json()


async def reserve_item(token: str, entity_id: str, quantity: float, reference: str | None = None) -> dict:
    async with _client(token) as c:
        payload: dict = {"quantity": quantity}
        if reference:
            payload["reference"] = reference
        return _raise(await c.post(f"/items/{entity_id}/reserve", json=payload)).json()


async def unreserve_item(token: str, entity_id: str, quantity: float) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/unreserve", json={"quantity": quantity})).json()


async def expire_item(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/expire", json={"reason": reason})).json()


async def dispose_item(token: str, entity_id: str, reason: str | None = None, notes: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/dispose", json={"reason": reason, "notes": notes})).json()


async def create_item(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/items", json=data)).json()


async def split_item(token: str, entity_id: str, children: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/split", json={"children": children})).json()


async def merge_items(
    token: str,
    source_entity_ids: list[str],
    target_sku_from: str,
    resulting_quantity: float | None = None,
    resulting_cost_price: float | None = None,
    resulting_name: str | None = None,
    resolved_attributes: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    body: dict = {"source_entity_ids": source_entity_ids, "target_sku_from": target_sku_from}
    if resulting_quantity is not None:
        body["resulting_quantity"] = resulting_quantity
    if resulting_cost_price is not None:
        body["resulting_cost_price"] = resulting_cost_price
    if resulting_name is not None:
        body["resulting_name"] = resulting_name
    if resolved_attributes:
        body["resolved_attributes"] = resolved_attributes
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    async with _client(token) as c:
        return _raise(await c.post("/items/merge", json=body)).json()


async def bulk_set_status(token: str, entity_ids: list[str], status: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/items/bulk/status", json={"entity_ids": entity_ids, "status": status})).json()


async def bulk_transfer(token: str, entity_ids: list[str], to_location_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/items/bulk/transfer", json={"entity_ids": entity_ids, "to_location_id": to_location_id})).json()


async def bulk_delete(token: str, entity_ids: list[str]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/items/bulk/delete", json={"entity_ids": entity_ids})).json()


async def bulk_expire(token: str, entity_ids: list[str]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/items/bulk/expire", json={"entity_ids": entity_ids})).json()


async def bulk_dispose(token: str, entity_ids: list[str], reason: str | None = None) -> dict:
    async with _client(token) as c:
        body: dict = {"entity_ids": entity_ids}
        if reason:
            body["reason"] = reason
        return _raise(await c.post("/items/bulk/dispose", json=body)).json()


# ---------------------------------------------------------------------------
# T5: Deals pipeline
# ---------------------------------------------------------------------------

async def list_deals(token: str, params: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/crm/deals", params=params or {})).json()


async def create_deal(token: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/crm/deals", json=data)).json()


async def move_deal_stage(token: str, deal_id: str, stage: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/deals/{deal_id}/stage", json={"new_stage": stage})).json()


async def mark_deal_won(token: str, deal_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/won")).json()


async def mark_deal_lost(token: str, deal_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/lost", json={"reason": reason})).json()


async def get_deal(token: str, deal_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get(f"/crm/deals/{deal_id}")).json()


async def patch_deal(token: str, deal_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/deals/{deal_id}", json=data)).json()


async def delete_deal(token: str, deal_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/deals/{deal_id}")).json()


async def reopen_deal(token: str, deal_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/reopen")).json()


# ---------------------------------------------------------------------------
# T6: Memo actions
# ---------------------------------------------------------------------------

async def get_memo(token: str, memo_id: str) -> dict:
    """Get a single memo by ID. Falls back to listing and filtering since
    the backend may not have a dedicated GET /memos/{id} endpoint."""
    async with _client(token) as c:
        # Try direct get first
        r = await c.get(f"/crm/memos/{memo_id}")
        if not r.is_error:
            return r.json()
        # Fall back to list and filter
        resp = _raise(await c.get("/crm/memos", params={"limit": 500})).json()
        all_memos = resp.get("items", []) if isinstance(resp, dict) else resp
        for m in all_memos:
            mid = m.get("entity_id") or m.get("id")
            if mid == memo_id:
                return m
        raise APIError(404, f"Memo not found: {memo_id}")


async def approve_memo(token: str, memo_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/memos/{memo_id}/approve")).json()


async def cancel_memo(token: str, memo_id: str, reason: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/memos/{memo_id}/cancel", json={"reason": reason})).json()


async def return_memo(token: str, memo_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/memos/{memo_id}/return", json=data)).json()


async def add_memo_item(token: str, memo_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/memos/{memo_id}/items", json=data)).json()


async def remove_memo_item(token: str, memo_id: str, item_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/memos/{memo_id}/items/{item_id}")).json()


async def create_memo(token: str, data: dict | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/crm/memos", json=data or {})).json()


async def add_contact_tags(token: str, contact_id: str, tags: list[str]) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/tags", json={"tags": tags})).json()


# ---------------------------------------------------------------------------
# Contact people / addresses / notes
# ---------------------------------------------------------------------------

async def add_contact_person(token: str, contact_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/people", json=data)).json()


async def update_contact_person(token: str, contact_id: str, person_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/people/{person_id}", json=data)).json()


async def remove_contact_person(token: str, contact_id: str, person_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/people/{person_id}")).json()


async def add_contact_address(token: str, contact_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/addresses", json=data)).json()


async def update_contact_address(token: str, contact_id: str, address_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/addresses/{address_id}", json=data)).json()


async def remove_contact_address(token: str, contact_id: str, address_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/addresses/{address_id}")).json()


async def add_contact_note(token: str, contact_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/notes", json=data)).json()


async def list_contact_notes(token: str, contact_id: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get(f"/crm/contacts/{contact_id}/notes")).json()


async def update_contact_note(token: str, contact_id: str, note_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/notes/{note_id}", json=data)).json()


async def delete_contact_note(token: str, contact_id: str, note_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/notes/{note_id}")).json()


async def get_contact_tags_vocabulary(token: str) -> list[dict]:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/contact-tags")).json()


async def patch_contact_tags_vocabulary(token: str, tags: list[dict]) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/contact-tags", json={"tags": tags})).json()


async def get_contact_defaults(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/contact-defaults")).json()


async def patch_contact_defaults(token: str, defaults: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch("/companies/me/contact-defaults", json={"defaults": defaults})).json()


async def upload_contact_file(token: str, contact_id: str, file_data: bytes, filename: str, content_type: str, description: str = "") -> dict:
    async with _client(token) as c:
        files = {"file": (filename, file_data, content_type)}
        data = {"description": description}
        return _raise(await c.post(f"/crm/contacts/{contact_id}/files", files=files, data=data)).json()


async def delete_contact_file(token: str, contact_id: str, file_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/files/{file_id}")).json()


async def download_contact_file(token: str, contact_id: str, file_id: str) -> httpx.Response:
    async with _client(token) as c:
        r = _raise(await c.get(f"/crm/contacts/{contact_id}/files/{file_id}"))
        return r


async def patch_location(token: str, location_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.patch(f"/companies/me/locations/{location_id}", json=data)).json()


async def patch_subscription(token: str, entity_id: str, data: dict) -> dict:
    fields_changed = {k: {"old": None, "new": v} for k, v in data.items()}
    async with _client(token) as c:
        return _raise(await c.patch(f"/subscriptions/{entity_id}", json={"fields_changed": fields_changed})).json()


async def convert_memo_to_invoice(token: str, memo_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/crm/memos/{memo_id}/convert-to-invoice")).json()


# ---------------------------------------------------------------------------
# T7: Payment refund
# ---------------------------------------------------------------------------

async def refund_payment(token: str, entity_id: str, data: dict) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/refund", json=data)).json()


async def void_payment(token: str, entity_id: str, payment_index: int, void_reason: str = "") -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/void-payment", json={
            "payment_index": payment_index, "void_reason": void_reason,
        })).json()


async def apply_credit_note(token: str, cn_id: str, target_doc_id: str, amount: float, date: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{cn_id}/apply-to-invoice", json={
            "target_doc_id": target_doc_id, "amount": amount, "date": date,
        })).json()


async def refund_credit_note(token: str, cn_id: str, amount: float, date: str | None = None,
                             method: str | None = None, bank_account: str | None = None,
                             reference: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{cn_id}/cn-refund", json={
            "amount": amount, "date": date, "method": method,
            "bank_account": bank_account, "reference": reference,
        })).json()


async def bulk_payment(token: str, doc_ids: list[str], amount: float, payment_date: str | None = None,
                       method: str | None = None, bank_account: str | None = None,
                       reference: str | None = None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/docs/bulk-payment", json={
            "doc_ids": doc_ids, "amount": amount, "payment_date": payment_date,
            "method": method, "bank_account": bank_account, "reference": reference,
        })).json()


# ---------------------------------------------------------------------------
# Sprint 6a: Activity feed
# ---------------------------------------------------------------------------

async def get_activity(token: str, limit: int = 15) -> list[dict]:
    async with _client(token) as c:
        r = await c.get("/dashboard/activity", params={"limit": limit})
        if r.is_error:
            return []
        return r.json().get("activities", [])


async def get_dashboard_kpis(token: str) -> dict:
    """GET /dashboard/kpis - full KPI payload for vertical-aware dashboard rendering."""
    async with _client(token) as c:
        r = await c.get("/dashboard/kpis")
        if r.is_error:
            return {}
        return r.json()


# ---------------------------------------------------------------------------
# Sprint S8: Document share links
# ---------------------------------------------------------------------------

async def create_share_link(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/share")).json()


async def revoke_share_link(token: str, entity_id: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/share")).json()


# ---------------------------------------------------------------------------
# AI assistant
# ---------------------------------------------------------------------------

async def ai_query(token: str, session_token: str, query: str, file_ids: list[str] | None = None) -> dict:
    """POST /ai/query — run an AI query against ERP data.

    session_token must be the gateway-issued X-Session-Token.
    Returns {"answer": str, "model_used": str, "tools_called": list}.
    """
    payload = {"query": query}
    if file_ids:
        payload["file_ids"] = file_ids

    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=60.0,
    ) as c:
        return _raise(await c.post("/ai/query", json=payload)).json()


async def ai_conversations_list(token: str, session_token: str) -> list[dict]:
    """GET /ai/conversations - list conversations for sidebar."""
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=10.0,
    ) as c:
        return _raise(await c.get("/ai/conversations?limit=20")).json()


async def ai_memory_get(token: str, session_token: str) -> dict:
    """GET /ai/memory - get AI memory for current company."""
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=10.0,
    ) as c:
        return _raise(await c.get("/ai/memory")).json()


async def ai_memory_clear(token: str, session_token: str) -> None:
    """DELETE /ai/memory - clear AI memory for current company."""
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=10.0,
    ) as c:
        _raise(await c.delete("/ai/memory"))


async def ai_upload(token: str, session_token: str, files: list[tuple[str, bytes, str]]) -> dict:
    """POST /ai/upload - upload files for AI processing.

    files: list of (filename, content_bytes, content_type).
    Returns {"file_ids": [...]}.
    """
    multipart = [("files", (name, data, ct)) for name, data, ct in files]
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=60.0,
    ) as c:
        return _raise(await c.post("/ai/upload", files=multipart)).json()


async def ai_confirm_bills(token: str, session_token: str, bills: list[dict]) -> dict:
    """POST /ai/confirm-bills - confirm and create draft bills proposed by AI."""
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}", "X-Session-Token": session_token},
        timeout=60.0,
    ) as c:
        return _raise(await c.post("/ai/confirm-bills", json={"bills": bills})).json()


async def ai_usage_stats(token: str, session_token: str = "") -> dict:
    """GET /ai/usage-stats - per-user query/credit usage for current month."""
    headers = {"Authorization": f"Bearer {token}"}
    if session_token:
        headers["X-Session-Token"] = session_token
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=headers,
        timeout=10.0,
    ) as c:
        return _raise(await c.get("/ai/usage-stats")).json()


async def ai_quota_status(token: str, session_token: str = "") -> dict:
    """GET /ai/quota-status - get current quota usage for UI badge."""
    headers = {"Authorization": f"Bearer {token}"}
    if session_token:
        headers["X-Session-Token"] = session_token
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=headers,
        timeout=10.0,
    ) as c:
        return _raise(await c.get("/ai/quota-status")).json()


# ---------------------------------------------------------------------------
# Import history
# ---------------------------------------------------------------------------

async def list_import_batches(token: str) -> dict:
    """GET /items/import/batches — list all import batches for the company."""
    async with _client(token) as c:
        return _raise(await c.get("/items/import/batches")).json()


async def undo_import_batch(token: str, batch_id: str) -> dict:
    """POST /items/import/batches/{batch_id}/undo — undo an import batch."""
    async with _client(token) as c:
        return _raise(await c.post(f"/items/import/batches/{batch_id}/undo")).json()


# ---------------------------------------------------------------------------
# Module management
# ---------------------------------------------------------------------------

async def get_modules(token: str) -> list[dict]:
    """GET /companies/me/modules — list installed modules with enabled state."""
    async with _client(token) as c:
        return _raise(await c.get("/companies/me/modules")).json()


async def enable_module(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/enable — enable a module (admin only)."""
    async with _client(token) as c:
        return _raise(await c.post(f"/companies/me/modules/{module_name}/enable")).json()


async def disable_module(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/disable — disable a module (admin only)."""
    async with _client(token) as c:
        return _raise(await c.post(f"/companies/me/modules/{module_name}/disable")).json()


# ---------------------------------------------------------------------------
# Verticals / Category Library
# ---------------------------------------------------------------------------

async def list_verticals_categories(token: str) -> list[dict]:
    """GET /companies/verticals/categories — list all category definitions."""
    async with _client(token) as c:
        return _raise(await c.get("/companies/verticals/categories")).json()


async def list_verticals_presets(token: str) -> list[dict]:
    """GET /companies/verticals/presets — list all vertical presets."""
    async with _client(token) as c:
        return _raise(await c.get("/companies/verticals/presets")).json()


async def apply_vertical_preset(token: str, vertical: str) -> dict:
    """POST /companies/me/apply-preset?vertical=X — seed category schemas from a preset."""
    async with _client(token) as c:
        return _raise(await c.post("/companies/me/apply-preset", params={"vertical": vertical})).json()


async def apply_vertical_category(token: str, name: str) -> dict:
    """POST /companies/me/apply-category?name=X — seed a single category schema."""
    async with _client(token) as c:
        return _raise(await c.post("/companies/me/apply-category", params={"name": name})).json()


# ── Period Lock + Fiscal Year Close ──────────────────────────────────────────

async def get_period_lock(token: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.get("/accounting/period-lock")).json()


async def set_period_lock(token: str, lock_date: str | None) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/period-lock", json={"lock_date": lock_date})).json()


async def close_fiscal_year(token: str, fiscal_year_end: str) -> dict:
    async with _client(token) as c:
        return _raise(await c.post("/accounting/close-year", json={"fiscal_year_end": fiscal_year_end})).json()

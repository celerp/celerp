# Celerp Connectors - Final Implementation Plan (v4)
Date: 2026-04-18

## Status

PR #6 merged. v1.0.0 binaries live. Xero redirect URI registered. Website deployed.
This plan covers everything remaining to ship connectors as a working feature.

---

## 1. Code Review Findings

### Critical bugs found in existing code

| # | File | Issue | Severity |
|---|------|-------|----------|
| B1 | `connectors/shopify.py:156` | Pagination URL bug: `_paginate` appends `?limit=250` but `sync_orders` passes `/orders.json?status=any` - double `?` breaks URL | **high** |
| B2 | `connectors/shopify.py:176` | Error accumulation bug: `(result.errors or []).append(msg)` creates a new list on each call (the `or []` returns a fresh list, not result.errors). Errors after the first are silently dropped | **high** |
| B3 | `relay/routers/tokens.py:get_access_token` | No token refresh: returns decrypted token without checking `expires_at`. QB tokens (60min) and Xero tokens (30min) will silently fail after expiry | **critical** |
| B4 | `relay/routers/tokens.py:revoke_token` | No upstream revocation: deletes token from DB but never calls Shopify/QB/Xero revocation endpoints. Tokens remain valid at the provider | **medium** |
| B5 | `tests/test_connectors.py` | License header: `LicenseRef-Proprietary` instead of `BSL-1.1` | **low** |
| B6 | All connectors | No incremental sync: every sync pulls ALL records. Will not scale beyond ~1000 items | **high** |
| B7 | All connectors | No rate limit handling: 429 responses cause immediate failure instead of backoff | **high** |
| B8 | All connectors | No audit trail: sync runs not logged to DB. No way to show "last sync" in UI | **medium** |

### Architectural issues (DRY violations)

| # | Issue | Fix |
|---|-------|-----|
| A1 | `_upsert_item`, `_upsert_contact`, `_upsert_invoice` duplicated across shopify.py, quickbooks.py, xero.py (3 copies each) | Move to `connectors/upsert.py` - single module, imported by all connectors |
| A2 | OAuth callback in `relay/routers/oauth.py` has 3 near-identical branches (one per platform) doing the same upsert pattern | Extract `_store_token(session, instance_id, platform, token_data)` helper |
| A3 | Base class missing: `ConnectorCategory` enum, `conflict_strategy`, `last_sync_at` support | Add to `connectors/base.py` |

---

## 2. Implementation Plan

### Phase 1: Fix critical bugs + base class (prerequisite for everything else)

**1a. Fix base class**

```python
# connectors/base.py additions

class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"

class ConnectorBase(ABC):
    name: str
    display_name: str
    category: ConnectorCategory
    supported_entities: list[SyncEntity]
    direction: SyncDirection
    conflict_strategy: dict[str, str]  # entity -> "celerp" | "platform" | "newest"

    @abstractmethod
    async def sync_products(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult: ...

    @abstractmethod
    async def sync_orders(self, ctx: ConnectorContext, since: datetime | None = None) -> SyncResult: ...
```

Add `since` parameter to all sync methods for incremental support.

**1b. Fix Shopify pagination bug (B1)**

Change `_paginate` to accept full URL or use `params` dict instead of string concatenation.

**1c. Fix error accumulation bug (B2)**

Initialize `errors: list[str] = []` at top, assign `result.errors = errors or None` at end (already done in sync_products but NOT in sync_orders/sync_contacts).

**1d. Extract shared upsert module (A1)**

Create `connectors/upsert.py`:
```python
async def upsert_item(company_id: str, item) -> bool: ...
async def upsert_order_from_shopify(company_id: str, order: dict) -> bool: ...
async def upsert_invoice_from_xero(company_id: str, invoice: dict) -> bool: ...
async def upsert_invoice_from_quickbooks(company_id: str, invoice: dict) -> bool: ...
async def upsert_contact_from_shopify(company_id: str, customer: dict) -> bool: ...
async def upsert_contact_from_xero(company_id: str, contact: dict) -> bool: ...
async def upsert_contact_from_quickbooks(company_id: str, customer: dict) -> bool: ...
```

All connectors import from here. Delete per-connector `_upsert_*` functions.

**1e. Fix license headers (B5)**

Change `LicenseRef-Proprietary` to `BSL-1.1` in all test files.

### Phase 2: Token refresh + rate limiting (make existing connectors production-ready)

**2a. Token auto-refresh in relay (B3)**

In `relay/routers/tokens.py:get_access_token`:
```python
# After fetching token_row:
if token_row.expires_at and token_row.expires_at < now + timedelta(minutes=5):
    if platform == "quickbooks":
        new = await quickbooks_oauth.refresh_access_token(crypto.decrypt(token_row.refresh_token_enc))
    elif platform == "xero":
        new = await xero_oauth.refresh_access_token(crypto.decrypt(token_row.refresh_token_enc))
    else:
        pass  # Shopify offline tokens don't expire
    # Update DB with new tokens
    token_row.access_token_enc = crypto.encrypt(new["access_token"])
    token_row.refresh_token_enc = crypto.encrypt(new["refresh_token"])
    token_row.expires_at = new["expires_at"]
    await session.commit()
    access_token = new["access_token"]
```

**2b. Upstream token revocation (B4)**

Add revocation endpoints:
- Shopify: `DELETE https://{shop}.myshopify.com/admin/api_permissions/current.json` (with access token header)
- QuickBooks: `POST https://developer.api.intuit.com/v2/oauth2/tokens/revoke`
- Xero: `POST https://identity.xero.com/connect/revocation`

Call before deleting from DB.

**2c. Rate limit middleware (B7)**

Add `connectors/http.py`:
```python
class RateLimitedClient:
    """httpx.AsyncClient wrapper with 429 backoff and per-platform rate tracking."""
    async def get(self, url, **kwargs) -> httpx.Response:
        for attempt in range(max_retries):
            resp = await self._client.get(url, **kwargs)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                await asyncio.sleep(retry_after)
                continue
            return resp
```

Replace raw `httpx.AsyncClient` in all connectors.

**2d. Incremental sync (B6)**

Each platform has different filtering:
- Shopify: `?updated_at_min=2026-01-01T00:00:00Z`
- QuickBooks: `WHERE MetaData.LastUpdatedTime > '2026-01-01'`
- Xero: `If-Modified-Since` header

Pass `since` from stored `last_sync_at` per (company, connector, entity).

### Phase 3: Audit trail + sync run model (B8)

New DB model:
```python
class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int]             # PK
    company_id: Mapped[str]
    connector: Mapped[str]      # "shopify", "xero", etc.
    entity: Mapped[str]         # "products", "orders", etc.
    direction: Mapped[str]      # "inbound" | "outbound"
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    created: Mapped[int]
    updated: Mapped[int]
    skipped: Mapped[int]
    errors: Mapped[str | None]  # JSON array of error strings
    status: Mapped[str]         # "success" | "partial" | "failed"
```

Write a `SyncRun` record at end of every sync call. This is what the UI queries for "Last sync: 2 hours ago - 47 items".

### Phase 4: Relay `GET /api/connectors` endpoint

```python
@router.get("/api/connectors")
async def list_connectors(instance_id: str = Depends(require_instance), session = Depends(get_session)):
    """Return available connectors + connection status for this instance."""
    # Static connector catalog (or from DB if we want it dynamic)
    catalog = [
        {"id": "shopify", "name": "Shopify", "category": "website", ...},
        {"id": "quickbooks", "name": "QuickBooks", "category": "accounting", ...},
        {"id": "xero", "name": "Xero", "category": "accounting", ...},
        {"id": "woocommerce", "name": "WooCommerce", "category": "website", "status": "coming-soon", ...},
    ]
    # Enrich with connection status
    tokens = await session.execute(select(OAuthToken).where(OAuthToken.instance_id == uuid.UUID(instance_id)))
    connected = {t.platform for t in tokens.scalars()}
    for c in catalog:
        c["connected"] = c["id"] in connected
    return {"connectors": catalog}
```

### Phase 5: App UI - Settings > Web Access > Connectors

Move from `ui/routes/settings.py:3654` to `ui/routes/settings_cloud.py`.

Key implementation notes:
- Fetch connector list from relay `GET /api/connectors` (not hardcoded)
- Group by `category` dynamically
- "Connect" button opens relay OAuth URL in system browser (Electron `shell.openExternal`)
- "Disconnect" calls `DELETE /tokens/{platform}` on relay
- "Sync Now" calls local sync service
- Show `SyncRun` data for last sync status per entity

### Phase 6: Outbound sync

**Website connectors (Shopify):**
- `sync_inventory_out(ctx)` - push Celerp stock levels to Shopify inventory
- `sync_products_out(ctx)` - push Celerp item updates (name, price, description) to Shopify

**Accounting connectors (QB, Xero):**
- `sync_invoices_out(ctx)` - push Celerp invoices to QB/Xero
- `sync_payments_out(ctx)` - push Celerp payment records to QB/Xero

### Phase 7: WooCommerce connector (full implementation)

#### Why WooCommerce is different

WooCommerce uses REST API keys (consumer_key + consumer_secret), NOT OAuth. This means:
- **No relay needed for auth** - the desktop app talks directly to the WC store
- **No token refresh** - API keys don't expire
- **No OAuth callback flow** - user manually enters credentials
- **HTTPS required** - WC sends consumer_secret as query param over HTTPS (or HTTP Basic Auth)

#### Authentication flow (UI)

1. User clicks "Connect WooCommerce" in Settings > Web Access > Connectors
2. UI shows a different form than Shopify/QB/Xero (no "Connect" OAuth button):
   - **Store URL** field (e.g. `https://mystore.com`)
   - **Consumer Key** field
   - **Consumer Secret** field
   - Link: "How to generate API keys" -> opens WP Admin guide
3. On submit, Celerp validates credentials by calling `GET {store_url}/wp-json/wc/v3/system_status`
4. If valid: store encrypted credentials in `connector_tokens` table (same encryption as OAuth tokens)
5. If invalid: show error with guidance (wrong URL? wrong permissions? HTTPS required?)

#### WooCommerce REST API v3 - entity mapping

**Products** (`GET /wp-json/wc/v3/products`)
```python
def _map_product(self, wc_product: dict) -> ItemUpsertData:
    return ItemUpsertData(
        external_id=str(wc_product["id"]),
        sku=wc_product.get("sku") or f"WC-{wc_product['id']}",
        name=wc_product["name"],
        description=wc_product.get("short_description", ""),
        retail_price=Decimal(wc_product.get("regular_price") or "0"),
        cost_price=None,  # WC doesn't track cost
        category=self._resolve_category(wc_product.get("categories", [])),
        quantity=wc_product.get("stock_quantity"),
        track_inventory=wc_product.get("manage_stock", False),
        weight=Decimal(wc_product["weight"]) if wc_product.get("weight") else None,
        barcode=None,  # WC doesn't have native barcode field
        attributes={
            k: v for k, v in {
                "wc_type": wc_product["type"],  # simple, variable, grouped, external
                "wc_status": wc_product["status"],
                "wc_tax_class": wc_product.get("tax_class"),
            }.items() if v
        },
    )
```

**Variable products**: WC "variable" products have child "variations" - each variation is a separate API call (`GET /products/{id}/variations`). Each variation becomes a separate Celerp item with the parent SKU as prefix (e.g. `TSHIRT-RED-L`). The parent "variable" product is NOT imported as an item - only its variations are.

**Orders** (`GET /wp-json/wc/v3/orders`)
```python
def _map_order(self, wc_order: dict) -> DocumentUpsertData:
    return DocumentUpsertData(
        external_id=str(wc_order["id"]),
        doc_type="invoice",
        doc_number=f"WC-{wc_order['number']}",
        contact_external_id=str(wc_order.get("customer_id", 0)) or None,
        date=wc_order["date_created"],
        currency=wc_order["currency"],
        line_items=[
            LineItemData(
                external_id=str(li["id"]),
                item_sku=li.get("sku"),
                description=li["name"],
                quantity=Decimal(str(li["quantity"])),
                unit_price=Decimal(li["price"]),
                tax_amount=Decimal(li["total_tax"]),
            )
            for li in wc_order["line_items"]
        ],
        shipping=sum(Decimal(s["total"]) for s in wc_order.get("shipping_lines", [])),
        tax_total=Decimal(wc_order["total_tax"]),
        status=self._map_order_status(wc_order["status"]),
    )

def _map_order_status(self, wc_status: str) -> str:
    return {
        "pending": "draft",
        "processing": "confirmed",
        "on-hold": "draft",
        "completed": "confirmed",
        "cancelled": "cancelled",
        "refunded": "cancelled",
        "failed": "cancelled",
    }.get(wc_status, "draft")
```

**Customers** (`GET /wp-json/wc/v3/customers`)
```python
def _map_customer(self, wc_customer: dict) -> ContactUpsertData:
    billing = wc_customer.get("billing", {})
    return ContactUpsertData(
        external_id=str(wc_customer["id"]),
        name=f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
              or wc_customer.get("username", f"WC-Customer-{wc_customer['id']}"),
        email=billing.get("email"),
        phone=billing.get("phone"),
        address_line1=billing.get("address_1"),
        address_line2=billing.get("address_2"),
        city=billing.get("city"),
        state=billing.get("state"),
        postal_code=billing.get("postcode"),
        country=billing.get("country"),
        is_customer=True,
        is_supplier=False,
        tax_id=None,
    )
```

**Inventory push** (outbound, Phase 6 prerequisite)
```
PUT /wp-json/wc/v3/products/{id}
Body: {"stock_quantity": 42, "manage_stock": true}
```

#### Pagination

WC API uses `page` + `per_page` (max 100). Response headers include:
- `X-WP-Total`: total record count
- `X-WP-TotalPages`: total pages

```python
async def _paginate(self, endpoint: str, params: dict = None) -> list[dict]:
    all_items = []
    page = 1
    while True:
        resp = await self.client.get(
            f"{self.store_url}/wp-json/wc/v3/{endpoint}",
            params={**(params or {}), "page": page, "per_page": 100},
        )
        resp.raise_for_status()
        items = resp.json()
        all_items.extend(items)
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1
    return all_items
```

#### Incremental sync

WC supports `modified_after` param (ISO 8601) on products, orders, and customers endpoints. Uses the same `last_sync_at` pattern from Phase 2.

#### Rate limiting

WooCommerce rate limits vary by hosting provider (not enforced by WC itself). Common limits:
- Shared hosting: 60-120 req/min
- WP Engine: 600 req/min
- Self-hosted: unlimited

Strategy: use the same `RateLimitedClient` from Phase 2, but with conservative defaults (60 req/min). If `429` or `503` is returned, use exponential backoff. Unlike Shopify/QB/Xero, WC doesn't send `Retry-After` headers consistently, so default to 10s initial backoff.

#### Error handling

WC REST API returns structured errors:
```json
{"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources.", "data": {"status": 403}}
```

Common errors to handle:
- `401`: Invalid credentials (consumer_key/secret wrong)
- `403`: Insufficient permissions (API key needs read/write)
- `404`: Store URL wrong or WC REST API not enabled
- `503`: Store temporarily unavailable

#### Test matrix (Phase 7)

| Test | Description | Mocking |
|------|-------------|---------|
| `test_validate_credentials_success` | Valid key/secret passes validation | respx |
| `test_validate_credentials_401` | Wrong key returns clear error | respx |
| `test_validate_credentials_404` | Wrong URL returns clear error | respx |
| `test_sync_products_simple` | Simple products map correctly | respx |
| `test_sync_products_variable` | Variable products expand to variations | respx |
| `test_sync_products_no_sku` | Products without SKU get `WC-{id}` fallback | respx |
| `test_sync_orders_creates_docs` | WC orders become invoices | respx |
| `test_sync_orders_status_mapping` | WC statuses map to Celerp statuses | respx |
| `test_sync_orders_with_shipping` | Shipping lines included in total | respx |
| `test_sync_contacts_creates_contacts` | WC customers become CRM contacts | respx |
| `test_sync_contacts_guest_orders` | Guest orders (customer_id=0) handled | respx |
| `test_pagination` | Multi-page results collected correctly | respx |
| `test_incremental_sync` | `modified_after` param sent when `last_sync_at` exists | respx |
| `test_inventory_push` | Stock quantity pushed to WC product | respx |
| `test_rate_limit_backoff` | 429 triggers exponential backoff | respx |
| `test_compliance_base_class` | Passes all base class compliance checks | - |

#### File structure

```
celerp/connectors/woocommerce.py   (~250 lines)
tests/test_connectors/test_woocommerce.py  (~200 lines)
```

No relay changes needed. No new relay endpoints. WooCommerce is entirely desktop-side.

---

## 3. Comprehensive Test Matrix

### Desktop app tests (`tests/`)

#### `tests/test_connectors/conftest.py` - Shared fixtures

```python
# Fixtures needed by all connector tests:

@pytest.fixture(params=["shopify", "quickbooks", "xero"])
def connector(request): ...

@pytest.fixture
def ctx_shopify():
    return ConnectorContext(company_id="co-1", access_token="tok", store_handle="test-store.myshopify.com")

@pytest.fixture
def ctx_quickbooks():
    return ConnectorContext(company_id="co-1", access_token="tok", store_handle="realm-123")

@pytest.fixture
def ctx_xero():
    return ConnectorContext(company_id="co-1", access_token="tok", store_handle="tenant-abc", extra={"tenant_id": "tenant-abc"})

@pytest.fixture
def mock_upsert_item():
    with patch("celerp.connectors.upsert.upsert_item", new=AsyncMock(return_value=True)) as m:
        yield m
```

#### `tests/test_connectors/test_compliance.py` - Parametrized compliance

| Test | Description | Applies to |
|------|-------------|------------|
| `test_has_name` | Connector has non-empty `name` | all |
| `test_has_category` | Category is valid `ConnectorCategory` | all |
| `test_has_display_name` | Has human-readable display name | all |
| `test_has_bidirectional_direction` | Released connectors must be bidirectional | all |
| `test_has_supported_entities` | At least one entity declared | all |
| `test_has_conflict_strategy` | `conflict_strategy` dict is non-empty | all |
| `test_conflict_strategy_covers_all_entities` | Every supported entity has a conflict strategy | all |
| `test_sync_products_returns_sync_result` | `sync_products()` returns `SyncResult` with correct entity/direction | all |
| `test_sync_orders_returns_sync_result` | `sync_orders()` returns `SyncResult` | all |
| `test_sync_contacts_returns_sync_result` | `sync_contacts()` returns `SyncResult` | all |

#### `tests/test_connectors/test_shopify.py` - Shopify-specific

| Test | Description | Mocking |
|------|-------------|---------|
| `test_sync_products_creates_items` | Products with SKUs create items via upsert | respx mock `/products.json` |
| `test_sync_products_skips_no_sku` | Variants without SKU are skipped | respx |
| `test_sync_products_variant_naming` | "Default Title" variant uses product title only | respx |
| `test_sync_products_pagination` | Multiple pages via Link header traversed correctly | respx + Link header |
| `test_sync_products_api_error` | HTTP error returns SyncResult with errors, no crash | respx 500 |
| `test_sync_orders_creates_docs` | Orders create documents | respx mock `/orders.json` |
| `test_sync_orders_idempotent` | Same order twice - second is skipped | respx + mock returning False |
| `test_sync_orders_error_accumulation` | Multiple order errors all captured (not dropped) | respx + mock raising |
| `test_sync_contacts_creates_contacts` | Customers create CRM contacts | respx mock `/customers.json` |
| `test_sync_contacts_api_error` | HTTP error handled gracefully | respx 500 |
| `test_pagination_url_with_query_params` | Path like `/orders.json?status=any` doesn't break pagination | unit test `_paginate` |
| `test_next_page_url_parsing` | `_next_page_url` correctly parses Link headers | unit test |
| `test_next_page_url_no_next` | Returns None when no next link | unit test |
| `test_incremental_sync_passes_since` | `since` parameter becomes `?updated_at_min=` | respx |
| `test_inventory_push` | Outbound inventory push sends correct Shopify API call | respx PUT |
| `test_product_update_push` | Outbound product update sends correct API call | respx PUT |

#### `tests/test_connectors/test_quickbooks.py` - QuickBooks-specific

| Test | Description | Mocking |
|------|-------------|---------|
| `test_sync_products_filters_by_type` | Only Service/Inventory/NonInventory items synced | respx mock `/query` |
| `test_sync_products_uses_sku_fallback` | Falls back to Name when Sku is empty | respx |
| `test_sync_products_pagination` | STARTPOSITION increments correctly | respx |
| `test_sync_products_api_error` | HTTP error returns SyncResult with errors | respx 500 |
| `test_sync_orders_only_invoices` | Only Invoice entities synced (not Estimate, etc.) | respx |
| `test_sync_contacts_active_only` | Query includes `WHERE Active = true` | respx |
| `test_query_pagination` | `_query` helper paginates correctly with STARTPOSITION | respx |
| `test_missing_realm_id` | Raises ValueError when `store_handle` is empty | no mock |
| `test_incremental_sync_passes_since` | `since` becomes `MetaData.LastUpdatedTime >` filter | respx |
| `test_invoice_push` | Outbound invoice creates Invoice entity via QB API | respx POST |
| `test_payment_push` | Outbound payment creates Payment entity | respx POST |

#### `tests/test_connectors/test_xero.py` - Xero-specific

| Test | Description | Mocking |
|------|-------------|---------|
| `test_sync_products_maps_fields` | Xero Item fields map to Celerp item correctly | respx mock `/Items` |
| `test_sync_products_skips_no_code` | Items without Code are skipped | respx |
| `test_sync_products_pagination` | Page-based pagination works (page=1, page=2, ...) | respx |
| `test_sync_orders_filters_accrec` | Only ACCREC (sales) invoices synced, ACCPAY skipped | respx mock `/Invoices` |
| `test_sync_contacts_creates_contacts` | Xero Contacts create CRM contacts | respx mock `/Contacts` |
| `test_tenant_id_in_headers` | `Xero-Tenant-Id` header is set from `store_handle` | respx |
| `test_incremental_sync_passes_since` | `since` becomes `If-Modified-Since` header | respx |
| `test_invoice_push_not_implemented` | `sync_invoices()` raises NotImplementedError (until built) | no mock |
| `test_invoice_push` | (Phase 6) Outbound invoice creates Xero Invoice via API | respx POST |

#### `tests/test_connectors/test_woocommerce.py` - WooCommerce (Phase 7)

| Test | Description | Mocking |
|------|-------------|---------|
| `test_validate_credentials_success` | Valid key/secret passes validation | respx |
| `test_validate_credentials_401` | Wrong key returns clear error | respx |
| `test_validate_credentials_404` | Wrong URL returns clear error | respx |
| `test_sync_products_simple` | Simple WC products create items with correct mapping | respx |
| `test_sync_products_variable` | Variable products expand to individual variation items | respx |
| `test_sync_products_no_sku` | Products without SKU get `WC-{id}` fallback SKU | respx |
| `test_sync_orders_creates_docs` | WC orders create invoice documents | respx |
| `test_sync_orders_status_mapping` | WC order statuses map correctly to Celerp statuses | respx |
| `test_sync_orders_with_shipping` | Shipping lines included in document total | respx |
| `test_sync_contacts_creates_contacts` | WC customers create CRM contacts | respx |
| `test_sync_contacts_guest_orders` | Guest orders (customer_id=0) handled gracefully | respx |
| `test_pagination` | Multi-page results collected via X-WP-TotalPages | respx |
| `test_incremental_sync` | `modified_after` param sent when `last_sync_at` exists | respx |
| `test_inventory_push` | Stock quantity pushed to WC product via PUT | respx |
| `test_rate_limit_backoff` | 429/503 triggers exponential backoff | respx |
| `test_compliance_base_class` | Passes all ConnectorBase compliance checks | - |
| `test_auth_uses_consumer_key` | Requests use consumer_key/consumer_secret, not Bearer | respx |
| `test_inventory_push` | Outbound stock update via WC REST API | respx PUT |
| `test_rate_limit_backoff` | 429 response triggers backoff | respx |

#### `tests/test_connectors/test_rate_limiting.py` - Rate limit handling

| Test | Description |
|------|-------------|
| `test_429_triggers_backoff` | `RateLimitedClient` retries on 429 with exponential backoff |
| `test_retry_after_header_respected` | Uses `Retry-After` header value when present |
| `test_max_retries_exceeded` | Raises after max retries instead of infinite loop |
| `test_non_429_errors_not_retried` | 500, 403, etc. raise immediately |

#### `tests/test_connectors/test_upsert.py` - Shared upsert module

| Test | Description |
|------|-------------|
| `test_upsert_item_creates_new` | New idempotency_key creates item |
| `test_upsert_item_skips_existing` | Existing idempotency_key returns False |
| `test_upsert_order_from_shopify` | Shopify order dict creates document |
| `test_upsert_invoice_from_xero` | Xero invoice dict creates document |
| `test_upsert_invoice_from_quickbooks` | QB invoice dict creates document |
| `test_upsert_contact_from_shopify` | Shopify customer creates CRM contact |
| `test_upsert_contact_from_xero` | Xero contact creates CRM contact |
| `test_upsert_contact_from_quickbooks` | QB customer creates CRM contact |

#### `tests/test_connectors/test_sync_run.py` - Audit trail

| Test | Description |
|------|-------------|
| `test_sync_run_created_on_success` | Successful sync writes SyncRun record |
| `test_sync_run_created_on_partial_failure` | Partial failure writes SyncRun with status="partial" |
| `test_sync_run_created_on_total_failure` | API error writes SyncRun with status="failed" |
| `test_sync_run_records_counts` | created/updated/skipped counts match SyncResult |
| `test_sync_run_stores_errors_json` | Error strings stored as JSON array |
| `test_last_sync_query` | Can query most recent SyncRun per (company, connector, entity) |

### Relay tests (`celerp-cloud/tests/`)

#### `tests/test_oauth_compliance.py` - OAuth flow tests (parametrized)

| Test | Applies to | Description |
|------|-----------|-------------|
| `test_authorize_returns_url` | shopify, quickbooks, xero | `/oauth/{platform}/authorize` returns valid URL |
| `test_authorize_requires_subscription` | all | Returns 402 without active subscription |
| `test_authorize_shopify_requires_shop` | shopify | Returns 400 without shop parameter |
| `test_callback_exchanges_token` | all | Valid code + state stores encrypted token |
| `test_callback_invalid_state` | all | Returns 400 for unknown state |
| `test_callback_expired_state` | all | Returns 400 after TTL (10 min) |
| `test_callback_upserts_existing_token` | all | Re-auth updates existing token row |
| `test_callback_renders_success_page` | all | Returns HTML with deep link |

#### `tests/test_token_refresh.py` - Token lifecycle

| Test | Description |
|------|-------------|
| `test_get_token_returns_decrypted` | GET `/tokens/{platform}/access-token` returns plaintext token |
| `test_get_token_refreshes_expired_qb` | Expired QB token auto-refreshes before returning |
| `test_get_token_refreshes_expired_xero` | Expired Xero token auto-refreshes before returning |
| `test_get_token_shopify_no_refresh` | Shopify token returned as-is (no expiry) |
| `test_refresh_updates_db` | After refresh, new tokens written to DB |
| `test_refresh_failure_returns_401` | Failed refresh returns 401 (re-auth required) |
| `test_revoke_deletes_token` | DELETE `/tokens/{platform}` removes token from DB |
| `test_revoke_calls_upstream` | DELETE also calls platform revocation endpoint |
| `test_revoke_nonexistent_returns_404` | DELETE for disconnected platform returns 404 |

#### `tests/test_connectors_api.py` - Connector catalog endpoint

| Test | Description |
|------|-------------|
| `test_list_connectors_returns_catalog` | GET `/api/connectors` returns all connectors |
| `test_list_connectors_shows_connection_status` | Connected platforms have `connected: true` |
| `test_list_connectors_includes_categories` | Each connector has a `category` field |
| `test_list_connectors_requires_auth` | Returns 401 without instance auth |

#### `tests/test_crypto.py` - Encryption

| Test | Description |
|------|-------------|
| `test_encrypt_decrypt_roundtrip` | encrypt(x) then decrypt returns x |
| `test_decrypt_wrong_key_fails` | Decryption with wrong key raises |
| `test_encrypted_value_not_plaintext` | Encrypted output != input |

### Test file count summary

| Location | File | Tests (approx) |
|----------|------|----------------|
| `tests/test_connectors/conftest.py` | Shared fixtures | - |
| `tests/test_connectors/test_compliance.py` | Parametrized compliance | ~30 (10 x 3 connectors) |
| `tests/test_connectors/test_shopify.py` | Shopify-specific | ~16 |
| `tests/test_connectors/test_quickbooks.py` | QuickBooks-specific | ~11 |
| `tests/test_connectors/test_xero.py` | Xero-specific | ~9 |
| `tests/test_connectors/test_woocommerce.py` | WooCommerce (Phase 7) | ~7 |
| `tests/test_connectors/test_rate_limiting.py` | Rate limit handling | ~4 |
| `tests/test_connectors/test_upsert.py` | Shared upsert module | ~8 |
| `tests/test_connectors/test_sync_run.py` | Audit trail | ~6 |
| `celerp-cloud/tests/test_oauth_compliance.py` | OAuth flows | ~24 (8 x 3 platforms) |
| `celerp-cloud/tests/test_token_refresh.py` | Token lifecycle | ~9 |
| `celerp-cloud/tests/test_connectors_api.py` | Connector catalog | ~4 |
| `celerp-cloud/tests/test_crypto.py` | Encryption | ~3 |
| **Total** | | **~131 new tests** |

---

## 4. Things to Watch Out For

### Security

1. **Token refresh race condition**: Two concurrent sync requests could both try to refresh the same expired token. Solution: use DB-level locking (`SELECT ... FOR UPDATE`) or an in-memory mutex per (instance_id, platform).

2. **Xero refresh token rotation**: Xero rotates refresh tokens on every use. If a refresh fails mid-way (network error after Xero invalidates old token but before we store the new one), the connection is permanently broken. Solution: wrap refresh in a transaction; if storing the new token fails, log a critical alert. The user must re-authorize.

3. **Shopify HMAC validation**: Shopify sends an `hmac` parameter in OAuth callbacks. We should validate it (currently don't). Low risk since we validate `state`, but defense-in-depth.

4. **WooCommerce credentials in transit**: Consumer keys are entered by the user and sent to the desktop app. Since the desktop app connects to relay over TLS, this is fine. But never log these values.

### Data integrity

5. **Idempotency key format**: Currently `shopify:{product_id}:{variant_id}`, `xero:item:{ItemID}`, `quickbooks:item:{Id}`. These MUST be stable across syncs. If we ever change the format, existing items will duplicate.

6. **Conflict resolution per entity**: Must document and implement clearly:
   - **Orders/invoices**: Platform-authoritative (they're the source of truth for what was sold)
   - **Inventory levels**: Celerp-authoritative (Celerp is the warehouse system)
   - **Products/items**: Last-write-wins with timestamp comparison
   - **Contacts**: Merge strategy (platform fields fill gaps in Celerp record, never overwrite)

7. **Partial sync recovery**: If sync crashes on page 3 of 5, the `since` parameter for next run must be the timestamp of the LAST SUCCESSFULLY PROCESSED record, not the start of the failed run. Otherwise page 1-2 items won't be re-checked for updates.

### Performance

8. **Shopify rate limit**: 2 requests/second for REST API. With 250 items/page, a 10,000 item store needs 40 pages = 20 seconds minimum. Add progress reporting to UI.

9. **QuickBooks query API limit**: 500 requests/minute. Our STARTPOSITION pagination is fine but batch queries where possible.

10. **Xero rate limit**: 60 calls/minute per tenant. With 100 items/page, pagination is fine but concurrent entity syncs (products + contacts + invoices) could hit this. Serialize entity syncs per connector.

### UI/UX

11. **OAuth popup flow**: Electron must use `shell.openExternal()` to open the OAuth URL in the system browser, NOT an in-app webview. Webviews are blocked by Google (and some Shopify stores). The callback page uses `celerp://oauth-success` deep link to signal the app.

12. **"Coming soon" vs hidden**: WooCommerce should show as "Coming soon" in the UI (generates interest). Marketplace connectors (Lazada, Shopee) should NOT appear at all.

13. **Sync schedule must persist across restarts**: Store schedule preference in local DB per (company, connector). On app launch, re-register any active schedules.

### Testing

14. **Respx vs real API**: All tests use respx mocks. We need a manual integration test checklist (not automated) for the OAuth flow since it requires browser interaction. Document in `docs/testing/connector-integration-checklist.md`.

15. **Existing test refactor**: Current `tests/test_connectors.py` (579 lines) and `tests/test_services/test_connector_services.py` (310 lines) overlap with new compliance suite. Consolidate - move reusable tests into compliance, delete duplicates.

16. **Marketplace tests**: `tests/test_connectors_shopee_lazada.py` (493 lines) should remain but NOT be part of the compliance suite. These are out of scope for release.

---

## 5. Implementation Order (dependency-aware)

```
Phase 1 (prerequisite)
  ├── 1a. ConnectorCategory + conflict_strategy + since param in base.py
  ├── 1b. Fix Shopify pagination URL bug
  ├── 1c. Fix error accumulation bug
  ├── 1d. Extract connectors/upsert.py (DRY)
  └── 1e. Fix license headers
  
Phase 2 (production-ready existing connectors)
  ├── 2a. Token auto-refresh in relay  ← depends on nothing
  ├── 2b. Upstream token revocation    ← depends on nothing
  ├── 2c. RateLimitedClient            ← depends on nothing
  └── 2d. Incremental sync (since)     ← depends on 1a
  
Phase 3 (audit trail)
  └── 3a. SyncRun model + recording    ← depends on 1a

Phase 4 (relay API)
  └── 4a. GET /api/connectors          ← depends on nothing

Phase 5 (UI)
  └── 5a. Settings > Web Access > Connectors  ← depends on 4a, 3a

Phase 6 (outbound sync)
  ├── 6a. Shopify inventory + product push  ← depends on 2c, 2d
  ├── 6b. QB invoice + payment push         ← depends on 2c, 2d
  └── 6c. Xero invoice + payment push       ← depends on 2c, 2d

Phase 7 (WooCommerce)
  └── 7a. Full WooCommerce connector        ← depends on 1a, 2c, 3a
```

Phases 1-3 can be one PR. Phase 4 is a separate relay PR. Phase 5 is a UI PR. Phases 6-7 are per-connector PRs.

---

## Appendix: Existing code paths

| Component | Path | Lines |
|-----------|------|-------|
| Connector base | `celerp/connectors/base.py` | 74 |
| Connector registry | `celerp/connectors/registry.py` | 27 |
| Shopify connector | `celerp/connectors/shopify.py` | 238 |
| QuickBooks connector | `celerp/connectors/quickbooks.py` | 236 |
| Xero connector | `celerp/connectors/xero.py` | 226 |
| Lazada connector (marketplace) | `celerp/connectors/lazada.py` | 506 |
| Shopee connector (marketplace) | `celerp/connectors/shopee.py` | 490 |
| Shopify OAuth relay | `relay/services/shopify_oauth.py` | 57 |
| QuickBooks OAuth relay | `relay/services/quickbooks_oauth.py` | 99 |
| Xero OAuth relay | `relay/services/xero_oauth.py` | 115 |
| OAuth router | `relay/routers/oauth.py` | 168 |
| Token router | `relay/routers/tokens.py` | 91 |
| Relay models | `relay/models.py` | ~120 |
| Existing connector tests | `tests/test_connectors.py` | 579 |
| Existing service tests | `tests/test_services/test_connector_services.py` | 310 |
| Marketplace tests (keep, out of scope) | `tests/test_connectors_shopee_lazada.py` | 493 |

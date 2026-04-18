# Celerp Connectors - Rectification and Finishing Plan
Date: 2026-04-18 (v3)

## Connector Taxonomy

Celerp has three categories of connectors, each with different business models and release timelines:

### 1. Website Connectors
Connect to your own e-commerce website. Sync products, orders, and customers between your store and Celerp so you manage inventory in one place.

- **Shopify** - your own Shopify store
- **WooCommerce** - your own WordPress/WooCommerce site
- Future: Magento, BigCommerce, etc.

Included in Cloud plan ($29/mo). Sold as a core feature of Celerp Cloud.

### 2. Accounting Connectors
Connect to external accounting systems. Sync invoices, contacts, chart of accounts, and reconciliation data.

- **QuickBooks** - QuickBooks Online
- **Xero** - Xero accounting
- Future: MYOB, Sage, FreshBooks, etc.

Included in Cloud plan ($29/mo). Sold as a core feature of Celerp Cloud.

### 3. Marketplace Connectors (SEPARATE SERVICE - NOT IN SCOPE)
Connect to third-party marketplaces where you list products for discovery and sales. This is an aggregator model - Celerp becomes the central hub managing listings across multiple marketplaces.

- Amazon, Lazada, Shopee, Etsy, eBay, etc.
- **Different business model:** percentage-based or per-item fees, not subscription
- **Completely separate service**, not included in Cloud connectors
- **Not mentioned on the website until built**
- Lazada/Shopee backend code exists in `celerp/connectors/` but has no OAuth relay and should not be marketed

**This plan covers website connectors and accounting connectors only.**

---

## 1. Current State Audit

### Connector Backend Code (`celerp/connectors/`)

**Website Connectors:**

| Connector | Class | Direction | Entities | Status |
|-----------|-------|-----------|----------|--------|
| Shopify | `ShopifyConnector` | Declared BIDIRECTIONAL, actually inbound only | Products, Orders, Customers | Inbound sync done. Zero outbound methods. |
| WooCommerce | - | - | - | Not started |

**Accounting Connectors:**

| Connector | Class | Direction | Entities | Status |
|-----------|-------|-----------|----------|--------|
| QuickBooks | `QuickBooksConnector` | Declared BIDIRECTIONAL, actually inbound only | Products, Orders, Contacts, Invoices | Inbound sync done. Zero outbound methods. |
| Xero | `XeroConnector` | Declared BIDIRECTIONAL, actually inbound only | Products, Orders, Contacts, Invoices | Inbound sync done. Invoice push stub (`NotImplementedError`). |

**Marketplace Connectors (out of scope for this plan):**

| Connector | Notes |
|-----------|-------|
| Lazada | Backend sync exists. No OAuth relay. Marketplace tier. |
| Shopee | Backend sync exists. No OAuth relay. Marketplace tier. |

### OAuth Relay (`celerp-cloud/relay/services/`)

| Service | File | Status |
|---------|------|--------|
| Shopify | `shopify_oauth.py` | Built |
| QuickBooks | `quickbooks_oauth.py` | Built |
| Xero | `xero_oauth.py` | Built |
| WooCommerce | - | Not built |

### App UI (`ui/routes/settings.py` line 3654)

Current `_connectors_tab()`:
- Hard-codes 6 connectors including Lazada, Shopee, and WooCommerce
- Shows "Coming soon" for ALL connectors (even the three with working OAuth)
- Lives in Settings > Sales (wrong location - should be Web Access)
- No connect/disconnect buttons, no OAuth flow, no sync controls

### Existing Tests

- `tests/test_connectors.py` (579 lines) - Shopify + registry
- `tests/test_services/test_connector_services.py` (310 lines) - service layer
- `tests/test_connectors_shopee_lazada.py` (493 lines) - Shopee + Lazada
- `celerp-cloud/tests/test_xero_connector.py`, `test_shopify_connector.py`, `test_quickbooks_connector.py`

Tests are per-connector with inconsistent coverage. No unified compliance suite.

---

## 2. Connector Release Gate: Compliance Test Suite

A connector cannot be released until it passes all applicable tests in CI. The tests ARE the gate - no manual sign-off.

### Category-Specific Requirements

Website connectors and accounting connectors sync different things:

**Website connectors** (Shopify, WooCommerce, etc.):
- Inbound: Products, Orders, Customers
- Outbound: Inventory levels, Product updates, Pricing
- Key concern: keeping stock levels accurate across channels

**Accounting connectors** (QuickBooks, Xero, etc.):
- Inbound: Contacts, Invoices/Bills, Chart of Accounts, Items
- Outbound: Invoices, Bills, Payments
- Key concern: keeping financial records in sync, no duplicate entries

### Global Requirements (all connectors)

These apply to every connector regardless of category:

1. **Bidirectional sync** - both inbound and outbound for all declared entities
2. **Idempotent** - re-running sync doesn't create duplicates
3. **Incremental** - only fetches changes since last sync
4. **Partial failure safe** - if page 3 of 5 fails, pages 1-2 are committed
5. **Rate limit compliant** - respects platform API limits with backoff
6. **Audit trail** - every sync run logs entity counts, errors, timestamps
7. **Conflict resolution** - documented strategy per entity (e.g. Celerp-authoritative for inventory, platform-authoritative for orders)
8. **OAuth token lifecycle** - authorization, refresh, revocation all handled
9. **Encrypted credential storage** - tokens encrypted at rest, per-company

### Compliance Test Suite (`tests/test_connectors/test_compliance.py`)

```python
"""
Parametrized compliance tests. Every connector must pass all applicable tests.
"""
import pytest
from celerp.connectors.base import ConnectorBase, SyncDirection, ConnectorCategory

# Register connectors as they become ready
WEBSITE_CONNECTORS = ["shopify"]      # add woocommerce when ready
ACCOUNTING_CONNECTORS = ["quickbooks", "xero"]
ALL_CONNECTORS = WEBSITE_CONNECTORS + ACCOUNTING_CONNECTORS


@pytest.fixture(params=ALL_CONNECTORS)
def connector(request) -> ConnectorBase:
    from celerp.connectors import get
    return get(request.param)


class TestStructure:
    """Every connector must declare its metadata correctly."""

    def test_has_name(self, connector):
        assert connector.name and isinstance(connector.name, str)

    def test_has_category(self, connector):
        assert connector.category in ConnectorCategory

    def test_has_bidirectional_direction(self, connector):
        """All released connectors must support bidirectional sync."""
        assert connector.direction == SyncDirection.BIDIRECTIONAL

    def test_has_supported_entities(self, connector):
        assert len(connector.supported_entities) > 0


class TestInboundSync:
    def test_products_sync(self, connector, mock_api, mock_db): ...
    def test_orders_sync(self, connector, mock_api, mock_db): ...
    def test_contacts_sync(self, connector, mock_api, mock_db): ...
    def test_idempotent(self, connector, mock_api, mock_db): ...
    def test_incremental(self, connector, mock_api, mock_db): ...
    def test_partial_failure_safe(self, connector, mock_api, mock_db): ...
    def test_audit_trail_created(self, connector, mock_api, mock_db): ...


class TestOutboundSync:
    def test_inventory_push(self, connector, mock_api, mock_db):
        if connector.category != ConnectorCategory.WEBSITE:
            pytest.skip("Website connectors only")
        ...

    def test_invoice_push(self, connector, mock_api, mock_db):
        if connector.category != ConnectorCategory.ACCOUNTING:
            pytest.skip("Accounting connectors only")
        ...

    def test_product_update_push(self, connector, mock_api, mock_db):
        if connector.category != ConnectorCategory.WEBSITE:
            pytest.skip("Website connectors only")
        ...


class TestRateLimiting:
    def test_respects_rate_limit_headers(self, connector, mock_api): ...
    def test_backs_off_on_429(self, connector, mock_api): ...


class TestConflictResolution:
    def test_conflict_strategy_documented(self, connector):
        assert hasattr(connector, 'conflict_strategy')
        assert connector.conflict_strategy is not None
```

### OAuth Compliance (`celerp-cloud/tests/test_oauth_compliance.py`)

```python
OAUTH_CONNECTORS = ["shopify", "quickbooks", "xero"]

class TestOAuthCompliance:
    def test_generates_auth_url(self, oauth_service): ...
    def test_callback_exchanges_token(self, oauth_service, mock_provider): ...
    def test_token_refresh(self, oauth_service, mock_provider): ...
    def test_token_revocation(self, oauth_service, mock_provider): ...
    def test_tokens_encrypted_at_rest(self, oauth_service, mock_db): ...
    def test_minimal_scopes(self, oauth_service): ...
```

### CI Integration

```yaml
- name: Connector compliance
  run: pytest tests/test_connectors/test_compliance.py -v --tb=short
```

---

## 3. Connector State Matrix

| Requirement | Shopify (website) | QuickBooks (accounting) | Xero (accounting) | WooCommerce (website) |
|-------------|----------|------------|------|-------------|
| **Inbound: Products** | DONE | DONE | DONE | - |
| **Inbound: Orders** | DONE | DONE | DONE | - |
| **Inbound: Contacts** | DONE | DONE | DONE | - |
| **Outbound: Inventory** | TODO | N/A | N/A | - |
| **Outbound: Products** | TODO | N/A | N/A | - |
| **Outbound: Invoices** | N/A | TODO | STUB | - |
| **Outbound: Payments** | N/A | TODO | TODO | - |
| **Idempotent sync** | ? | ? | ? | - |
| **Incremental sync** | ? | ? | ? | - |
| **Rate limiting** | ? | ? | ? | - |
| **Audit trail** | ? | ? | ? | - |
| **Conflict resolution** | TODO | TODO | TODO | - |
| **OAuth relay** | DONE | DONE | DONE | TODO |
| **Token refresh** | ? | ? | ? | - |
| **Compliance tests** | TODO | TODO | TODO | - |

Legend: DONE = implemented, TODO = needs building, STUB = skeleton, ? = needs verification, - = not started, N/A = not applicable for category

---

## 4. Dynamic Connector Architecture

### Relay-Driven Registry

The relay is the single source of truth. The app never hard-codes connector names.

#### Relay API: `GET /api/connectors`

```json
{
  "connectors": [
    {
      "id": "shopify",
      "name": "Shopify",
      "category": "website",
      "description": "Sync your Shopify store",
      "logo_url": "/assets/connectors/shopify.svg",
      "status": "available",
      "sync_entities": [
        {"entity": "products", "inbound": true, "outbound": true},
        {"entity": "orders", "inbound": true, "outbound": false},
        {"entity": "customers", "inbound": true, "outbound": false},
        {"entity": "inventory", "inbound": false, "outbound": true}
      ]
    },
    {
      "id": "quickbooks",
      "name": "QuickBooks",
      "category": "accounting",
      "description": "Sync invoices, contacts, and chart of accounts",
      "logo_url": "/assets/connectors/quickbooks.svg",
      "status": "available",
      "sync_entities": [
        {"entity": "contacts", "inbound": true, "outbound": false},
        {"entity": "invoices", "inbound": true, "outbound": true},
        {"entity": "items", "inbound": true, "outbound": false},
        {"entity": "chart_of_accounts", "inbound": true, "outbound": false}
      ]
    }
  ]
}
```

Adding a new connector to the relay automatically makes it available in every connected Celerp instance. No app code changes needed.

### Base Class Update

Add category to `ConnectorBase`:

```python
class ConnectorCategory(str, Enum):
    WEBSITE = "website"
    ACCOUNTING = "accounting"

class ConnectorBase(ABC):
    name: str
    category: ConnectorCategory
    direction: SyncDirection
    supported_entities: list[SyncEntity]
    conflict_strategy: dict[SyncEntity, str]  # e.g. {"inventory": "celerp-authoritative"}
```

---

## 5. App UI Design: Web Access > Connectors

### Location

**Settings > Web Access**, visible only when connected to Celerp Cloud. Add a "Connectors" tab to the existing cloud tabs.

### Layout: "Add Connector" with Category Headers

```
Settings > Web Access > Connectors

  Your connected services:

  +------------------------------------------+
  | [Shopify logo]  Shopify                  |
  | Products, orders, customers, inventory   |
  | Last sync: 2 hours ago - 47 items        |
  | [Sync Now]  [Settings]  [Disconnect]     |
  +------------------------------------------+

  [+ Add Connector]
```

Clicking "Add Connector" expands inline with category sections:

```
  ┌─ Add a connector ─────────────────────────┐
  │                                            │
  │  Website                                   │
  │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │
  │  [WC logo]  WooCommerce     [Connect]     │
  │  Sync products, orders, customers          │
  │                                            │
  │  Accounting                                │
  │  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │
  │  [QB logo]  QuickBooks      [Connect]     │
  │  Sync invoices, contacts, chart of accts   │
  │                                            │
  │  [Xero logo]  Xero         [Connect]      │
  │  Sync invoices, contacts, products         │
  │                                            │
  └────────────────────────────────────────────┘
```

Categories come from the relay response. If a new category appears (e.g. when marketplace launches someday), the UI renders it automatically.

### Interaction Flow

1. **Add:** "Add Connector" button expands panel with available connectors grouped by category
2. **Connect:** Click "Connect" - OAuth flow via relay - redirects back - connector moves to connected section
3. **Settings:** Expand inline per connected connector:
   - Entity toggles (which to sync)
   - Sync schedule (manual / hourly / 6h / daily)
   - Last sync details (timestamp, counts, errors)
4. **Sync Now:** Manual trigger with inline progress
5. **Disconnect:** Confirmation - "Your synced data stays in Celerp" - revokes token

### Dynamic Rendering

```python
async def _connectors_section(token: str) -> FT:
    gw = get_client()
    available = await gw.get_connectors()        # from relay
    connections = await gw.get_connections(token)  # this company's state

    # Group available-but-not-connected by category
    by_category = defaultdict(list)
    for c in available:
        if c["id"] not in connections:
            by_category[c["category"]].append(c)

    connected_cards = [_connected_card(c, connections[c["id"]])
                       for c in available if c["id"] in connections]

    return Div(
        *connected_cards,
        _add_connector_panel(by_category),
    )
```

---

## 6. Website Connector Language

### Current Status: Already Correct

The website already uses open-ended language that naturally covers website + accounting connectors without mentioning marketplace:

> "Connector sync - sync to external services like your own website, Shopify, QuickBooks, and more."

This is used on pricing.html and in FAQ answers. The connectors page (connectors.html) lists Shopify, QuickBooks, Xero as available and WooCommerce as coming soon. No Lazada/Shopee references remain anywhere.

**No website changes needed for this restructuring.**

### Future Enhancement: Category Sections on Connectors Page

When we have more connectors, the connectors page should group them:

```
Website Connectors
  Shopify - Available
  WooCommerce - Available

Accounting Connectors
  QuickBooks - Available
  Xero - Available
```

Not needed yet with only 3-4 connectors, but the `connectors.json` already supports a `category` field for when we need it.

### connectors.json

```json
[
  {
    "id": "shopify",
    "name": "Shopify",
    "status": "available",
    "category": "website",
    "tagline": "Sync your Shopify store"
  },
  {
    "id": "quickbooks",
    "name": "QuickBooks",
    "status": "available",
    "category": "accounting",
    "tagline": "Sync invoices, contacts, and chart of accounts"
  },
  {
    "id": "xero",
    "name": "Xero",
    "status": "available",
    "category": "accounting",
    "tagline": "Sync invoices, contacts, and products"
  },
  {
    "id": "woocommerce",
    "name": "WooCommerce",
    "status": "coming-soon",
    "category": "website",
    "tagline": "Sync your WooCommerce store"
  }
]
```

---

## 7. Implementation Roadmap

### Phase 1: Website Accuracy (DONE)
- [x] Removed Lazada/Shopee from all pages
- [x] Marked QB/Xero as available, WooCommerce as coming soon
- [x] Open-ended language: "sync to external services like your own website, Shopify, QuickBooks, and more"

### Phase 2: Compliance Test Suite
- Build `tests/test_connectors/test_compliance.py` with parametrized, category-aware tests
- Build `celerp-cloud/tests/test_oauth_compliance.py`
- Add `ConnectorCategory` to base class
- Run Shopify, QB, Xero through compliance to identify gaps
- Add compliance step to CI
- Fix failures (idempotency, incremental sync, audit trail, conflict resolution)

### Phase 3: App UI - Web Access > Connectors
- Add "Connectors" tab to `settings_cloud.py`
- Remove old `_connectors_tab()` from Settings > Sales
- Implement relay `GET /api/connectors` endpoint
- Build "Add Connector" panel with category grouping
- OAuth connect/disconnect flow
- Connection status display

### Phase 4: Sync Controls
- "Sync Now" button per connector
- Entity selection toggles
- Sync schedule (manual / hourly / 6h / daily)
- Sync status display (last sync, counts, errors)

### Phase 5: Outbound Sync
**Website connectors:**
- Shopify: inventory levels push, product updates push
- WooCommerce: full build (inbound + outbound + OAuth)

**Accounting connectors:**
- QuickBooks: invoice push, payment push
- Xero: invoice push (complete stub), payment push

### Phase 6: Additional Connectors (incremental)
- Each new connector follows the same pattern: build, pass compliance, deploy to relay
- Automatically available in all Celerp instances
- Future website: Magento, BigCommerce, Squarespace
- Future accounting: MYOB, Sage, FreshBooks, Wave

### Marketplace (SEPARATE - future)
- Different business model (percentage/per-item fees)
- Different service architecture
- Not part of the Cloud plan
- Not mentioned on website until ready
- Existing Lazada/Shopee backend code can be repurposed when the time comes

---

## Appendix: Code Paths

| Component | Path |
|-----------|------|
| Connector base | `celerp/connectors/base.py` |
| Connector registry | `celerp/connectors/registry.py` |
| Shopify connector | `celerp/connectors/shopify.py` |
| QuickBooks connector | `celerp/connectors/quickbooks.py` |
| Xero connector | `celerp/connectors/xero.py` |
| Lazada connector (marketplace - future) | `celerp/connectors/lazada.py` |
| Shopee connector (marketplace - future) | `celerp/connectors/shopee.py` |
| Shopify OAuth relay | `celerp-cloud/relay/services/shopify_oauth.py` |
| QuickBooks OAuth relay | `celerp-cloud/relay/services/quickbooks_oauth.py` |
| Xero OAuth relay | `celerp-cloud/relay/services/xero_oauth.py` |
| Existing connector tests | `tests/test_connectors.py` (579 lines) |
| Existing service tests | `tests/test_services/test_connector_services.py` (310 lines) |
| Current connector UI (to be replaced) | `ui/routes/settings.py:3654` |
| Cloud settings (new home) | `ui/routes/settings_cloud.py` |
| Website connectors page | `celerp-cloud/website/src/docs/connectors.html` |
| Website strings | `celerp-cloud/website/strings/en.json` |

# Celerp Website Rectification Plan
Date: 2026-04-18

## Executive Summary

A full code audit of the Celerp website against the Python codebase reveals **six categories of inaccuracies** across five pages, plus one navigation omission. The most critical issues are:

1. **Connectors page is fundamentally wrong**: it lists Shopify/Shopee/Lazada and marks QB/Xero as "Coming soon" - but QB and Xero are fully built with OAuth relays, while Lazada and Shopee have *no* cloud OAuth relay and cannot be marketed as cloud connectors.
2. **Features page under-documents reality**: six document types are listed but the code has eight base types plus three list subtypes. Several major features are missing entirely.
3. **Backup page is doubly wrong**: claims attached files are NOT backed up (they are), and references non-existent "Business" or "Pro" plans.
4. **FAQ misdirects potential customers**: declares Celerp "not designed for service businesses" - incorrect for a modular system. Also buries the Web Access tunnel innovation.
5. **Pricing page mentions Lazada/Shopee** in feature lists and FAQ answers.
6. **Price comparison strip** on homepage should be removed.

See separate document **"Celerp Connectors - Rectification and Finishing Plan"** for detailed connector architecture, UI design, and implementation roadmap.

---

## Page-by-Page Audit

---

### a. Navigation (nav.html)
**File:** `website/src/_includes/nav.html`

**Current:**
```html
<a href="{{lang_prefix}}/docs/">{{nav.docs}}</a>
<a href="{{lang_prefix}}/pricing.html">{{nav.pricing}}</a>
<a href="{{lang_prefix}}/blog/">{{nav.blog}}</a>
```
No "Home" link exists. The logo links to `/` but there is no explicit text link.

**Issue:** Users on doc pages have no clear "Home" text link in the nav bar.

**Fix:** Add a Home link before Docs:
```html
<a href="{{lang_prefix}}/">{{nav.home}}</a>
<a href="{{lang_prefix}}/docs/">{{nav.docs}}</a>
```
Add `"nav.home": "Home"` (and translations) to `strings/en.json`.

---

### 1. Home Page (index.html)
**File:** `website/src/index.html`

**Current:** Contains a `price-anchor-strip` section comparing QuickBooks $99 vs Xero $55 vs Zoho $79 vs Celerp $0.

**Issue:** Noah has requested this section be removed.

**Fix:** Remove the entire `<section class="price-anchor-strip">...</section>` block.

---

### 2. Features (features.html)
**File:** `website/src/docs/features.html`

#### Document Types - Corrected Full List

**Website currently claims** (6 types): Invoice, Purchase Order, Quote, Credit Note, Customer Memo, Delivery Note.

**Code reality** - `ui/routes/documents.py` line 33 and 28:
```python
_DOC_TYPES = ["invoice", "purchase_order", "bill", "receipt", "credit_note", "memo", "consignment_in", "list"]
_LIST_TYPES = ["quotation", "transfer", "audit"]
```

The "list" type is a parent category with three subtypes. Quote and Delivery Note are NOT standalone doc types - Quote is a list subtype ("quotation"), and Delivery Note does not exist in code.

**Fix:** Replace the features.html document types table with the corrected list:

**Core Document Types (8):**

| Type | Label in UI | Use | Auto journal entry? |
|------|-------------|-----|---------------------|
| Invoice | Invoices | Bill a customer | Yes - AR + Revenue |
| Purchase Order | Draft Bills & POs | Order from supplier | Yes - on receipt |
| Vendor Bill | Vendor Bills | Supplier invoice received | Yes - AP + Expense |
| Receipt | Receipts | Payment receipt | Yes |
| Credit Note | Credit Notes | Reverse or partially refund an invoice | Yes |
| Consignment Out | Consignment Out (Memo) | Items sent to customer on approval | No (on conversion) |
| Consignment In | Consignment In | Goods received on consignment from supplier | No |
| List | Lists | Flexible document type with subtypes (see below) | No |

**List Subtypes (3):**

| Subtype | Use |
|---------|-----|
| Quotation | Price quote for a customer (convertible to invoice) |
| Transfer | Internal stock transfer between locations |
| Audit | Stock audit / count sheet |

Additionally, draft invoices display as "Pro Forma" invoices in the UI (using proforma numbering).

#### Deal Pipeline

**Current:** Listed under CRM features as "Track deals through a customizable pipeline."

**Reality:** Deal Pipeline is a premium module for the marketplace, still under development. It will likely be free but is not yet released. Marketplace modules are marketed separately.

**Fix:** Remove Deal Pipeline from the features page entirely. It will be listed in the marketplace section when that is built.

#### Warehousing / Locations

**Current:** Mentioned on features page but could be clearer.

**Reality:** Celerp natively supports warehouses and locations in core. There is a *separate* premium marketplace module for full multi-warehouse workflows (pick instructions, warehouse receipts) aimed at large companies. The core feature should be documented; the premium module belongs in the marketplace.

**Fix:** Keep the Warehousing/Locations section on features.html but clarify it covers the native built-in support. Do not mention the premium module here.

#### Missing Features - Not Documented At All

The following are fully implemented in code but have zero mention on features.html:

| Feature | Code evidence |
|---------|--------------|
| **Vendor Bills** | `_DOC_TYPES` includes `"bill"`, label "Vendor Bills" |
| **Receipts** | `_DOC_TYPES` includes `"receipt"`, label "Receipts" |
| **Consignment In** | `_DOC_TYPES` includes `"consignment_in"` |
| **Labels / Label Printing** | `ui/routes/inventory.py` lines 829-870: label template dropdown, `celerpPrintLabel()` function, settings at `/settings/labels`. Module: `default_modules/celerp-labels/` |
| **Bank Reconciliation** | `ui/routes/reconciliation.py` - full reconciliation workspace |
| **Notifications** | `ui/routes/notifications.py` - notification proxy with unread count, SSE stream |
| **List Subtypes** | Quotation, Transfer, Audit - full workflow for each |

**Fix:** Add sections for all missing features to features.html.

---

### 3. Connectors (connectors.html)
**File:** `website/src/docs/connectors.html`

**This page requires a major rewrite.** See the separate **"Celerp Connectors - Rectification and Finishing Plan"** document for the full connector audit, architecture plan, and UI design.

**Summary of website-specific fixes:**

1. **Remove "Coming soon" from QB and Xero** - both are fully built with OAuth relay
2. **Remove Lazada and Shopee** from connectors.html entirely - no OAuth relay exists
3. **Add WooCommerce as "Coming soon"** - it's already in the app UI connector list
4. **Remove all `#waitlist` links** - the anchor doesn't exist
5. **Correct sync direction claims** - all connectors currently only have inbound sync implemented, despite some declaring BIDIRECTIONAL
6. **Fix plan name references** - no "Business" or "Pro" plans exist

---

### 4. Backup (backup.html)
**File:** `website/src/docs/backup.html`

#### Claim: "Requires Business or Pro plan"

**Website says:**
> "Cloud backup requires a Business or Pro plan. Business includes 10 GB; Pro includes 50 GB."

**Code reality (`strings/en.json`):** Plan names are **Free**, **Cloud** ($29), **Cloud + AI** ($49), **Team** ($99). There is no "Business" or "Pro" plan.

**Fix:** Replace the callout with:
> "Cloud backup requires a **Cloud plan** or higher ($29/mo). Includes 7 daily backups and 20 GB storage."

Remove all references to "Business" and "Pro" plan names.

#### Claim: "Attached files are NOT included"

**Website says:**
> "Attached files (images, PDFs uploaded to item records) are not included in the database backup."

**Code reality (`celerp/services/backup_export.py`):**
```python
attachment_dirs=[Path("static/attachments"), Path("data/ai_uploads")]
```
The backup archive includes the full `static/attachments` and `data/ai_uploads` directories. `backup_import.py` also extracts files on restore.

**Fix:** Replace with:
> "A Celerp backup includes both the database and all attached files (images, PDFs attached to records, and AI uploads). Your entire data set is captured in a single `.celerp-backup` archive."

#### Backup How It Works

The page implies only the database is backed up. Update to reflect it's a full archive (database + attachments).

#### Retention Table

Verify weekly/monthly retention limits against `celerp/services/backup_scheduler.py` before publishing. `strings/en.json` mentions "7 daily backups".

---

### 5. Pricing (pricing.html)
**File:** `website/src/pricing.html` + `strings/en.json`

#### Cloud Plan Feature List - Remove Lazada/Shopee

**Current (`pricing.cloud_f2`):**
> "All connectors (Shopify, QB, Xero, Lazada, Shopee)"

**Fix:**
> "Connector sync - sync to your website and other external services like Shopify, QuickBooks, and more. See our [connectors](/docs/connectors.html) page for the full list of supported integrations."

This keeps the language open-ended so we only maintain the specific list in one place (the connectors page).

#### "What are the cloud features exactly?" FAQ - Needs Rewrite

**Current (`pricing.faq2_a`):**
> "Three things: connector sync (OAuth relay for Shopify, QuickBooks, Xero, Lazada, Shopee), encrypted cloud backup (pg_dump - AES-256-GCM - Cloudflare R2), and the AI assistant."

**Issues:**
- Lists Lazada/Shopee (no relay exists)
- Doesn't explain the subdomain/tunnel value proposition
- Lists AI as a primary cloud feature

**Recommended rewrite:**
> "Four things: (1) **Your own subdomain** - `yourcompany.celerp.com` gives your Celerp a stable, public URL. (2) **Unlimited-user tunnel** - the Cloud relay creates a secure encrypted tunnel from your subdomain to your machine. Your whole team connects from anywhere, no VPN, no IT configuration. One Mac Mini + Cloud plan = enterprise-grade access for your entire office. (3) **Connector sync** - sync to external services like Shopify, QuickBooks, and more. See our [connectors](/docs/connectors.html) section for details. (4) **Encrypted cloud backup** - your full database and files, AES-256-GCM encrypted, stored on Cloudflare R2. The Cloud plan also includes 100 lifetime AI queries to try."

#### AI Tier Positioning

The current pricing page has a prominent "Cloud + AI" tier at $49/mo. Noah's guidance: don't market AI prominently.

**Recommended restructure:**
- Move "100 AI queries included (lifetime)" into Cloud plan description as a bonus line
- In Cloud + AI tier, emphasize automation: "Auto-draft POs, reconcile invoices, scheduled automations" not "AI assistant"

#### Connector References - Use Open-Ended Language

Anywhere on pricing.html that lists specific connectors, replace with open-ended language pointing to the connectors page. Examples:
- Instead of: "Shopify, QuickBooks, Xero sync"
- Use: "Connector sync with your e-commerce and accounting platforms. [See supported integrations](/docs/connectors.html)"

This way we update specific connector lists in one place only.

---

### 6. FAQ (faq.html)
**File:** `website/src/docs/faq.html`

#### Q: "Is Celerp suitable for my industry?"

**Current answer:**
> "Celerp is designed for businesses that manage physical inventory..."

**Issue:** Incorrect and anti-conversion. Celerp is modular.

**Recommended answer:**
> "Celerp works for any business that needs to track clients, issue invoices, and manage finances - retail, wholesale, distribution, manufacturing, e-commerce, gems and jewelry, and service businesses. Modules are independent: a consultancy uses CRM + Accounting + Documents. A manufacturer uses Inventory + Bills of Materials + Documents. A retailer uses everything. Install only what you need."

#### Q: "Can multiple people use Celerp at the same time?"

**Current answer:** Only covers local network case.

**Recommended answer:**
> "Yes - unlimited users, always. On your local network, anyone can open a browser to your machine's IP and log in with their own account. With a **Cloud subscription**, your Celerp gets a secure public URL (`yourcompany.celerp.com`). Your whole team - including remote staff and accountants - connects from anywhere, no VPN needed. Buy a Mac Mini for around $600, plug it in, download Celerp, subscribe to Cloud ($29/mo), and you have enterprise-grade ERP infrastructure accessible from anywhere in the world. No server admin, no IT department."

#### Q: "What happens if my Celerp is behind a firewall?"

**Current answer:** Sends users to Cloudflare Tunnel or ngrok.

**Recommended answer:**
> "For internal sharing, P2P works on your local network. For external sharing, the easiest solution is the Celerp **Cloud relay** ($29/mo) - it gives your instance a permanent public URL (`yourcompany.celerp.com`) so share links work for anyone, anywhere. Alternatively, you can set any public URL in Settings > Company > Public URL if you prefer your own infrastructure."

#### Q: "Does Celerp work offline?"

**Fix:** Replace specific connector names with "connector sync" to keep it non-specific:
> "You only need internet for connector sync and cloud backup uploads."

#### Q: "What's included in the free plan?"

**Fix:** Add "Bank reconciliation" and "Label printing" to the list.

#### Connector FAQ answers

Remove all Lazada/Shopee references. Use open-ended language pointing to the connectors page wherever specific connectors are listed.

---

## Implementation Priority

### P0 - Factually Wrong (fix before any traffic)

1. **connectors.html** - Remove "Coming soon" from QB and Xero. Remove Lazada/Shopee. Correct to: Shopify, QuickBooks, Xero available now.
2. **connectors.html** - Remove all `#waitlist` links.
3. **backup.html** - Fix plan names (no "Business" or "Pro"). Fix files claim (files ARE backed up).
4. **pricing.html** + `strings/en.json` - Remove Lazada/Shopee. Use open-ended connector language.

### P1 - Misleading / Anti-Conversion (fix within one sprint)

5. **faq.html** - Fix "Is Celerp suitable for my industry?" - modular, works for service businesses.
6. **faq.html** - Enhance multi-user answer with Web Access tunnel narrative.
7. **faq.html** - Fix firewall question to promote Cloud relay instead of ngrok.
8. **pricing.html** - Rewrite "What are the cloud features exactly?" FAQ.

### P2 - Incomplete Documentation (fill gaps)

9. **features.html** - Replace document types with corrected 8 types + 3 list subtypes.
10. **features.html** - Add missing feature sections: Labels, Bank Reconciliation, Notifications.
11. **features.html** - Remove Deal Pipeline (marketplace module, not released).
12. **features.html** - Clarify native warehousing/locations support.
13. **nav.html** - Add Home link.

### P3 - Structural / DRY

14. **index.html** - Remove `price-anchor-strip` section.
15. **Implement DRY connector language** - all pages use open-ended references pointing to the connectors page as the single source of truth.
16. **AI positioning** - Consider reframing "Cloud + AI" tier.
17. **Backup retention table** - Verify weekly/monthly retention limits in `backup_scheduler.py`.

# Celerp Website Rectification Plan v3
Date: 2026-04-18

## Background

### What happened
The celerp-cloud repo had **two parallel sets of HTML files**:

1. **Root-level files** (`website/*.html`) - hand-crafted, single-language HTML files. These were the original website and what the server was actually serving.
2. **Build system** (`website/src/` templates + `website/strings/` i18n JSON + `build.py` -> `website/dist/`) - a multilingual build pipeline developed later whose output was **never deployed** because the deploy workflow rsynced the raw `website/` directory instead of `website/dist/`.

When we cleaned this up (removed stale root files, fixed deploy to use `dist/`), we discovered that all previous rectification plan changes had been correctly applied to `src/` templates but were **already live in `dist/`** - they just weren't reaching production before. The old root-level files had some separate manual fixes that were never ported to `src/`.

Additionally, the **Privacy Policy and Terms of Service** were incorrectly rewritten as simplified website templates, when the canonical approved versions live in `relay/routers/legal.py` and are served at `relay.celerp.com/privacy` and `relay.celerp.com/terms`.

### Current state
- Deploy workflow now correctly runs `build.py` then deploys `website/dist/`
- Stale root-level HTML files removed from git
- The dist-based multilingual site is live

---

## Status of Original Rectification Plan Items

All items from the original plan (v1) were correctly implemented in the `src/` templates and `strings/en.json`. Now that `dist/` is being deployed, they are live.

| # | Item | Status |
|---|------|--------|
| a | Navigation - add Home link | ✅ Done (`nav.home` string + template) |
| 1 | Home - remove price-anchor-strip | ✅ Done (section removed from `src/index.html`) |
| 2a | Features - correct document types (8 base + 3 list subtypes) | ✅ Done |
| 2b | Features - remove Deal Pipeline | ✅ Done |
| 2c | Features - clarify native warehousing | ✅ Done |
| 2d | Features - add Labels, Bank Reconciliation, Notifications | ✅ Done |
| 3a | Connectors - remove "Coming soon" from QB/Xero | ✅ Done |
| 3b | Connectors - remove Lazada/Shopee entirely | ✅ Done |
| 3c | Connectors - add WooCommerce as coming soon | ✅ Done |
| 3d | Connectors - remove #waitlist links | ✅ Done |
| 4a | Backup - fix plan names (no "Business"/"Pro") | ✅ Done |
| 4b | Backup - correct file inclusion claim | ✅ Done |
| 5a | Pricing - remove Lazada/Shopee references | ✅ Done (open-ended connector language) |
| 5b | Pricing - rewrite cloud features FAQ answer | ✅ Done (4-part answer with subdomain/tunnel/connectors/backup) |
| 6a | FAQ - fix industry suitability answer | ✅ Done (modular, inclusive of service businesses) |
| 6b | FAQ - enhance multi-user answer with Cloud tunnel | ✅ Done (Mac Mini narrative) |
| 6c | FAQ - fix firewall answer to promote Cloud relay | ✅ Done |
| 6d | FAQ - remove specific connector names from offline answer | ✅ Done |

---

## New Issues Found (Current Live Site)

### CRITICAL

#### C1. Privacy Policy - unauthorized rewrite
**Current state:** `src/privacy.html` renders simplified privacy content from `strings/en.json` that was written without authorization.

**Correct source:** `relay/routers/legal.py` contains the final, approved Privacy Policy (last updated April 14, 2026). It has 10 sections including: proper legal entity (Data Universal Limited, Office 3906, The CTR, 99 Queens Road, Hong Kong), service analytics collection, AI query data handling, data sharing with partners, GDPR-style rights, cookie policy, and 30-day retention/7-year billing record retention.

**Fix:** Revert `src/privacy.html` to a redirect to `https://relay.celerp.com/privacy`. The relay is the single source of truth for legal documents. This avoids DRY violations (maintaining two copies of legal text).

#### C2. Terms of Service - unauthorized rewrite
**Current state:** I wrote a simplified 8-section ToS and deployed it as `src/terms.html`.

**Correct source:** `relay/routers/legal.py` has the approved ToS with 15 sections: acceptance, service description, accounts/access, subscriptions/billing, acceptable use, third-party integrations, AI query processing, data ownership, service data/analytics, disclaimer of warranties, limitation of liability, termination, governing law (Hong Kong), changes, and contact info.

**Fix:** Revert `src/terms.html` to a redirect to `https://relay.celerp.com/terms`.

### HIGH

#### H1. Unresolved `{{lang_prefix}}` in 4 string values
**Pages affected:** Homepage (2 links), Pricing (1 link), Download (1 link)

The build system substitutes `{{lang_prefix}}` in template files but NOT inside JSON string values. Four links render as literal `{{lang_prefix}}/pricing.html` in the browser.

**Affected strings:**
- `home.nudge` - pricing link
- `home.cta_sub` - pricing link  
- `pricing.nudge` - download link
- `download.step4` - pricing link

**Fix:** Replace `{{lang_prefix}}/pricing.html` with `/pricing.html` and `{{lang_prefix}}/download.html` with `/download.html` in these string values. Root-relative URLs work correctly for all language versions.

#### H2. "What's the catch?" section - breaking out of borders
**Root cause:** The `src/index.html` template is missing `<div class="container">` wrapper inside the `.trust-section`. The old hand-crafted site had this wrapper. Without it, the content stretches full-width and overflows.

**Fix:** Add `<div class="container">` inside the `.trust-section` in `src/index.html`, with matching `</div>` before closing the section. The CSS is identical between old and new sites - only the HTML wrapper is missing.

### CLEANUP

#### CL1. Remove unauthorized privacy/terms strings from en.json
After switching privacy/terms back to redirects, remove all `terms.*` strings (23 strings) from en.json. Also remove `privacy.*` content strings (keep `footer.privacy` and `footer.terms` which are just link labels).

#### CL2. Ensure dist/ is gitignored
`website/dist/` is a build artifact and should be in `.gitignore`. Currently it may be tracked.

#### CL3. Old root-level HTML files already removed
✅ Done in commit `302084c`.

---

## Action Plan

### Phase 1: Critical Legal Fix (immediate)
1. Revert `src/privacy.html` to redirect stub -> `https://relay.celerp.com/privacy`
2. Revert `src/terms.html` to redirect stub -> `https://relay.celerp.com/terms`
3. Remove unauthorized `terms.*` and `privacy.*` content strings from en.json
4. Commit, push, verify redirects work on live site

### Phase 2: Content/Layout Fixes (immediate)
1. Fix 4 `{{lang_prefix}}` broken links in en.json string values
2. Add missing `<div class="container">` in trust-section
3. Rebuild, push, verify

### Phase 3: Verify & Clean
1. Add `website/dist/` to `.gitignore`
2. Final visual check of all pages
3. Confirm no other content regressions

---

## Root Cause & Prevention

**Root cause:** Two parallel website systems coexisted in the same repo. The build system output (`dist/`) was never deployed because the workflow pointed at the wrong directory. Manual fixes accumulated in the stale root-level files. Legal pages were rewritten without checking where the approved versions lived.

**Prevention:**
1. Deploy workflow now runs build + deploys `dist/` (fixed)
2. Stale root-level HTML files removed (fixed)
3. `website/dist/` should be gitignored (build artifact)
4. Legal pages owned by relay only - website uses redirects, never local copies
5. Any future legal text changes must go through `relay/routers/legal.py` only

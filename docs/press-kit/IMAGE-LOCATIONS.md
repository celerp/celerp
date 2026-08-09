# Image locations — for the website & external listings

All marketing/press images live in **`docs/press-kit/screenshots/`** in the `celerp/celerp` repo.
They are the single DRY source: re-running the capture pipeline
(`serve.py → seed.py → capture.py → capture_apidocs.py → press_kit.py`) rewrites these exact files in
place, so anything pointing at the URLs below updates automatically on the next push.

> These URLs use `@main`, so they resolve **after this branch is merged to `main`**.

## Two ways to reference each image

- **jsDelivr CDN (recommended for the website / DigitalOcean):** fast, cached, real CDN.
  `https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/<file>`
- **Raw GitHub (fine for quick embeds, not a CDN):**
  `https://raw.githubusercontent.com/celerp/celerp/main/docs/press-kit/screenshots/<file>`

## Files

<!-- PRESS_KIT:files-table start -->
| Purpose | File | jsDelivr URL |
|---|---|---|
| **Hero** - manufacturing cost worksheet | `manufacturing-worksheet.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/manufacturing-worksheet.png |
| Trial balance | `trial-balance.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/trial-balance.png |
| Balance sheet | `balance-sheet.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/balance-sheet.png |
| General ledger | `general-ledger.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/general-ledger.png |
| Inventory | `inventory.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/inventory.png |
| Production planning | `production-planning.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/production-planning.png |
| REST API | `rest-api.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/rest-api.png |
| Dashboard | `dashboard.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/dashboard.png |
| International shipping document | `international-shipping-document.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/international-shipping-document.png |
| Statement of account | `statement-of-account.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/statement-of-account.png |
| Extended journal | `extended-journal.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/extended-journal.png |
| Work centers | `work-centers.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/work-centers.png |
<!-- PRESS_KIT:files-table end -->

All are optimized PNG, ≤1600 px wide, ~170–390 KB each (≈2 MB total).

## Industry folders (full set per business type)

For press covering a specific vertical, the complete screen set for each demo company is published
under `docs/press-kit/screenshots/<industry>/` (per-folder screen counts in the table below). These are **WebP** (q82, ≤1280 px)
— a browse-and-pick archive, not the social-preview/CMS surface, so WebP's ~80% size win applies with
no visible loss. Each folder has a generated `README.md` gallery that GitHub renders on open.

<!-- PRESS_KIT:industry-table start -->
| Industry | Folder | Screens | CDN base |
|---|---|---|---|
| Apparel & clothing (Northbound Apparel) | `apparel/` | 17 | .../screenshots/apparel/`<name>.webp` |
| Coffee roasting (Meridian Coffee Roasters) | `coffee/` | 15 | .../screenshots/coffee/`<name>.webp` |
| Cosmetics & skincare (Lumera Skincare) | `cosmetics/` | 13 | .../screenshots/cosmetics/`<name>.webp` |
| Jewelry (Aurelia Atelier) | `jewelry/` | 15 | .../screenshots/jewelry/`<name>.webp` |
<!-- PRESS_KIT:industry-table end -->

<!-- PRESS_KIT:screen-names start -->
Screen names per folder:
- Apparel & clothing (17): `dashboard`, `inventory`, `manufacturing-worksheet`, `worksheet-print`, `invoices`, `production-planning`, `chart-of-accounts`, `trial-balance`, `general-ledger`, `balance-sheet`, `audit-log`, `permissions`, `rest-api`, `work-centers`, `invoice-pay`, `label-designer`, `modules`
- Coffee roasting (15): `dashboard`, `inventory`, `manufacturing-worksheet`, `worksheet-print`, `invoices`, `production-planning`, `chart-of-accounts`, `trial-balance`, `general-ledger`, `balance-sheet`, `audit-log`, `permissions`, `rest-api`, `international-shipping-document`, `extended-journal`
- Cosmetics & skincare (13): `dashboard`, `inventory`, `manufacturing-worksheet`, `worksheet-print`, `invoices`, `production-planning`, `chart-of-accounts`, `trial-balance`, `general-ledger`, `balance-sheet`, `audit-log`, `permissions`, `rest-api`
- Jewelry (15): `dashboard`, `inventory`, `manufacturing-worksheet`, `worksheet-print`, `invoices`, `production-planning`, `chart-of-accounts`, `trial-balance`, `general-ledger`, `balance-sheet`, `audit-log`, `permissions`, `rest-api`, `statement-of-account`, `memo-holdings`
<!-- PRESS_KIT:screen-names end -->

## Artwork (hand-illustrated brand graphics)

Creative brand art (not screenshots) lives under **`docs/press-kit/artwork/`** — for blog posts,
social, and launch coverage. WebP (q90, ≤1600 px), ~280–450 KB each (~0.7 MB total).

| Purpose | File | jsDelivr URL |
|---|---|---|
| Celerp vs. legacy ERPs | `celerp-vs-erps.webp` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/artwork/celerp-vs-erps.webp |
| Celerp vs. SaaS | `celerp-vs-saas.webp` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/artwork/celerp-vs-saas.webp |

## Keeping the CDN fresh after a regen
jsDelivr caches `@main` aggressively. After pushing regenerated images, purge each so the website
picks them up immediately:
`https://purge.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/<file>`
(or pin a tagged version like `@v1.2.0` for immutable, never-stale URLs on a release page).

## Social preview (highest-leverage image)
Set the repo's social-preview card (GitHub → Settings → General → Social preview) to
`artwork/celerp-social-preview.png` - the dedicated branded card (logo + tagline + URLs). This is the
image shown when the repo is shared on Twitter/LinkedIn/Slack and in Google results. It must be set in
the GitHub UI (not via git).

Format constraints: GitHub accepts **PNG/JPG/GIF only (not WebP)**, max **1 MB**, recommended
**1280×640** (2:1). The committed card is 1280×640 PNG, ~740 KB. Keep new exports under 1 MB or GitHub
rejects the upload.

## Copyright
See `docs/press-kit/README.md` — screenshots depict the proprietary Celerp UI (© 2026 Noah Severs);
embedded product imagery is CC0; brand names shown are fictional demo data.

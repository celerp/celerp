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

| Purpose | File | jsDelivr URL |
|---|---|---|
| **Hero** — manufacturing cost worksheet | `manufacturing-worksheet.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/manufacturing-worksheet.png |
| Trial balance | `trial-balance.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/trial-balance.png |
| Balance sheet | `balance-sheet.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/balance-sheet.png |
| General ledger | `general-ledger.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/general-ledger.png |
| Inventory | `inventory.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/inventory.png |
| Production planning | `production-planning.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/production-planning.png |
| REST API | `rest-api.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/rest-api.png |
| Dashboard | `dashboard.png` | https://cdn.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/dashboard.png |

All are optimized PNG, ≤1600 px wide, ~170–390 KB each (≈2 MB total).

## Keeping the CDN fresh after a regen
jsDelivr caches `@main` aggressively. After pushing regenerated images, purge each so the website
picks them up immediately:
`https://purge.jsdelivr.net/gh/celerp/celerp@main/docs/press-kit/screenshots/<file>`
(or pin a tagged version like `@v1.2.0` for immutable, never-stale URLs on a release page).

## Social preview (highest-leverage image)
Set the repo's social-preview card (GitHub → Settings → General → Social preview) to
`manufacturing-worksheet.png`. This is the image shown when the repo is shared on
Twitter/LinkedIn/Slack and in Google results. It must be set in the GitHub UI (not via git).

## Demo data backup
The full demo instance backup (~48 MB) is intentionally **not** in the git tree (it would bloat every
clone forever and is excluded from PyPI/binaries). Publish it as a GitHub **Release** asset instead:
`gh release create demo-data context/marketing/backups/celerp-demo-full.celerp-backup -t "Demo data" -n "Importable demo dataset"`
Then link it from the website / README. This keeps it on GitHub, out of clones, PyPI, and binaries.

## Copyright
See `docs/press-kit/README.md` — screenshots depict the proprietary Celerp UI (© 2026 Noah Severs);
embedded product imagery is CC0; brand names shown are fictional demo data.

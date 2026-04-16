# Celerp

**Free ERP for businesses that move physical goods.**

Inventory, invoicing, purchasing, manufacturing, accounting, and CRM — in one place, on your machine.

[![Tests](https://github.com/celerp/celerp/actions/workflows/ci.yml/badge.svg)](https://github.com/celerp/celerp/actions)
[![License](https://img.shields.io/badge/license-BSL--1.1-blue)](LICENSE)

---

## Download

| Platform | Link |
|----------|------|
| Windows (.exe) | [Latest release →](https://github.com/celerp/celerp/releases/latest) |
| Linux (.AppImage) | [Latest release →](https://github.com/celerp/celerp/releases/latest) |
| macOS (.dmg) | Coming soon |

**Double-click. That's it.** Postgres runs embedded. No Docker, no terminal, no server required.

Or install via pip:

```bash
pip install celerp
celerp init    # first-time setup
celerp start   # launches API + UI
```

---

## What it does

- **Invoicing & documents** — invoices, purchase orders, quotations, credit notes, receipts with auto-numbering sequences
- **Inventory** — multi-location stock tracking, barcode scanning, FIFO/average cost valuation
- **Purchasing** — PO workflow, goods receipt, supplier management
- **Manufacturing** — bills of materials, production orders, merge/split/transform patterns
- **Accounting** — double-entry ledger, chart of accounts, P&L, balance sheet, trial balance, fiscal year reporting
- **CRM** — contacts, pipeline, memos, activity feed
- **Subscriptions** — recurring billing with automatic invoice generation
- **Reports** — AR/AP aging, sales, purchases, inventory valuation
- **Multi-company** — manage multiple business entities from a single install
- **CSV import/export** — every section, idempotent, with full audit report

---

## How it works

Celerp runs entirely on your machine. Your data never leaves your computer unless you explicitly share it.

- No subscription required to use the core product
- No internet connection required
- No vendor lock-in — your data stays in a standard Postgres database you control
- The desktop app bundles Postgres, runs migrations on launch, and opens in your browser automatically

For teams, Celerp runs as a local server and teammates connect over the LAN.

---

## Modules

Every business domain is a self-contained module. The full set ships with the download:

| Module | What it does |
|--------|-------------|
| `celerp-inventory` | Items, stock levels, locations, barcode scanning, valuation |
| `celerp-contacts` | Contacts, addresses, tags, notes, file attachments |
| `celerp-docs` | Invoices, POs, quotations, credit notes, receipts |
| `celerp-accounting` | Chart of accounts, journal entries, P&L, balance sheet |
| `celerp-reports` | AR/AP aging, sales, purchases, inventory valuation |
| `celerp-subscriptions` | Recurring billing, auto-invoice generation |
| `celerp-manufacturing` | BOMs, production orders, merge/split/transform |
| `celerp-labels` | Label printing, barcode generation |
| `celerp-verticals` | Industry presets — configure for your business type on first run |

The onboarding wizard lets you pick your industry and use-case. Modules can be toggled any time at **Settings → Modules**.

---

## Data import

Every section supports CSV import with column mapping, preview, validation, and idempotent re-runs:

```
Settings → Import → Upload CSV → Map columns → Preview → Confirm
```

An audit report is generated after every import showing what was created, skipped, or errored. Re-running the same file is safe.

---

## Server install (pip)

For headless servers, VMs, or self-hosted team deployments.

### Prerequisites

- Python 3.11+
- PostgreSQL 14+

### Quickstart

```bash
pip install celerp
celerp init
celerp start
```

`celerp init` connects to the default local Postgres instance, runs migrations, and writes a config file to `~/.config/celerp/config.toml`. Open **http://localhost:8080** after `celerp start`.

### Non-default Postgres

```bash
celerp init --db-url postgresql+asyncpg://user:pass@host:5432/mydb
```

### All init flags

| Flag | Default | Notes |
|------|---------|-------|
| `--db-url` | `postgresql+asyncpg://celerp:celerp@localhost:5432/celerp` | PostgreSQL connection URL |
| `--api-port` | `8000` | API server port |
| `--ui-port` | `8080` | UI server port |
| `--cloud-token` | _(empty)_ | Celerp Cloud token (optional) |
| `--force` | off | Overwrite existing config |

### Other commands

```bash
celerp migrate    # apply pending migrations after an upgrade
celerp status     # show config, DB connection, migration state
celerp demo       # seed demo data
celerp upgrade    # pip install --upgrade celerp + migrate
```

To change ports or connect to Celerp Cloud after init, edit `~/.config/celerp/config.toml` directly.

---

## Development

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Node.js 18+ _(only for Electron desktop build)_

### Install

```bash
git clone git@github.com:celerp/celerp.git
cd celerp/core
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Database setup

```bash
# Ubuntu/Debian
sudo apt install postgresql && sudo systemctl start postgresql
sudo -u postgres psql -c "CREATE USER celerp WITH PASSWORD 'devpass';"
sudo -u postgres psql -c "CREATE DATABASE celerp OWNER celerp;"

# macOS
brew install postgresql@15 && brew services start postgresql@15
psql postgres -c "CREATE USER celerp WITH PASSWORD 'devpass';"
psql postgres -c "CREATE DATABASE celerp OWNER celerp;"
```

### Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
DATABASE_URL=postgresql+asyncpg://celerp:devpass@localhost:5432/celerp
JWT_SECRET=<openssl rand -hex 32>
ALLOW_INSECURE_JWT=true
```

### Run migrations

```bash
set -a && source .env && set +a && alembic upgrade head
```

### Start servers

```bash
# Terminal 1 — API (port 8000)
uvicorn celerp.main:app --reload

# Terminal 2 — UI (port 8080)
PYTHONPATH=. uvicorn ui.app:app --port 8080 --reload
```

Open **http://localhost:8080**.

### Tests

```bash
pytest tests/ --ignore=tests/test_visual.py
```

Tests use SQLite in-memory — no Postgres required for the test suite.

### Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DATABASE_URL` | _(required)_ | `postgresql+asyncpg://user:pass@localhost:5432/dbname` |
| `JWT_SECRET` | _(required)_ | Fatal on startup if unset or default in production |
| `ALLOW_INSECURE_JWT` | `false` | Set `true` in dev/CI |
| `CELERP_PUBLIC_URL` | _(optional)_ | Base URL for share links |
| `MODULE_DIR` | `default_modules` | Module package directory |
| `ENABLED_MODULES` | _(all)_ | Comma-separated list to load |

### Troubleshooting

| Error | Fix |
|-------|-----|
| `fe_sendauth: no password supplied` | Run `set -a && source .env && set +a` before alembic |
| `password authentication failed` | Run `ALTER USER celerp WITH PASSWORD 'devpass';` via psql |
| `role "celerp" does not exist` | Run the `CREATE USER` command above |
| `FATAL: JWT_SECRET is set to the default` | Generate with `openssl rand -hex 32` or set `ALLOW_INSECURE_JWT=true` |
| `Directory 'static' does not exist` | Run uvicorn from the repo root, not a subdirectory |
| `System already bootstrapped` | Drop and recreate the DB, or `DELETE FROM users;` via psql |

---

## Contributing

Issues and PRs welcome. See [LICENSE](LICENSE).

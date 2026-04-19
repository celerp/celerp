# Contributing to Celerp

Issues and PRs welcome. This guide covers the development workflow.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DATABASE_URL` | _(required)_ | `postgresql+asyncpg://user:pass@localhost:5432/dbname` |
| `JWT_SECRET` | _(required)_ | Fatal on startup if unset or default in production |
| `ALLOW_INSECURE_JWT` | `false` | Set `true` in dev/CI |
| `CELERP_PUBLIC_URL` | _(optional)_ | Base URL for share links |
| `MODULE_DIR` | `default_modules` | Module package directory |
| `ENABLED_MODULES` | _(all)_ | Comma-separated list to load |

## CLI commands (pip install)

```bash
celerp init       # first-time setup (connects DB, runs migrations, writes config)
celerp start      # launches API + UI
celerp migrate    # apply pending migrations after an upgrade
celerp status     # show config, DB connection, migration state
celerp demo       # seed demo data
celerp upgrade    # pip install --upgrade celerp + migrate
```

### Init flags

| Flag | Default | Notes |
|------|---------|-------|
| `--db-url` | `postgresql+asyncpg://celerp:celerp@localhost:5432/celerp` | PostgreSQL connection URL |
| `--api-port` | `8000` | API server port |
| `--ui-port` | `8080` | UI server port |
| `--cloud-token` | _(empty)_ | Celerp Cloud token (optional) |
| `--force` | off | Overwrite existing config |

To change ports after init, edit `~/.config/celerp/config.toml` directly.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `fe_sendauth: no password supplied` | Run `set -a && source .env && set +a` before alembic |
| `password authentication failed` | Run `ALTER USER celerp WITH PASSWORD 'devpass';` via psql |
| `role "celerp" does not exist` | Run the `CREATE USER` command from README |
| `FATAL: JWT_SECRET is set to the default` | Generate with `openssl rand -hex 32` or set `ALLOW_INSECURE_JWT=true` |
| `Directory 'static' does not exist` | Run uvicorn from the repo root, not a subdirectory |
| `System already bootstrapped` | Drop and recreate the DB, or `DELETE FROM users;` via psql |

## Module system

Each module is a self-contained Python package under `default_modules/`. A module registers:

- **API routes** via a FastAPI router
- **Projections** that materialize ledger events into queryable state
- **UI routes** via FastHTML
- **Nav slots** for sidebar entries

To add a new module, follow the pattern in any existing module (e.g. `celerp-inventory`). Modules are discovered and loaded at startup based on `ENABLED_MODULES`.

## Coding style

- DRY, SOLID, KISS
- Small pure functions, explicit contracts, deterministic behavior
- Tests use SQLite in-memory - no Postgres required
- Name test files after the module they test
- Use `conftest.py` for shared fixtures

## License headers

Every `.py` file must include a license header. See [LICENSE](LICENSE) for details.

- `celerp/` - BSL-1.1
- `default_modules/celerp-manufacturing/`, `default_modules/celerp-labels/` - MIT
- All other modules, UI, tests - LicenseRef-Proprietary

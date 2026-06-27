# Contributing to Celerp

Issues and PRs welcome. This guide covers the development workflow.

## Contribution terms

Celerp is multi-licensed (see `LICENSING.md`). What the public *receives* depends on the component
(BUSL-1.1 core, MIT modules, proprietary layer). What you *grant us* when contributing is governed as
follows:

- **Anything accepted into an official Celerp repository** - including the MIT modules - requires a signed
  **Contributor License Agreement** (`CLA.md`). The module stays MIT for everyone downstream; the CLA only
  governs the rights you grant Celerp (so we can keep multi-licensing, move code between layers, and offer
  commercial terms). Open an issue before substantial work and we will arrange signing.
- **Third-party modules you do not submit upstream** (e.g. your own marketplace modules) need no CLA - a
  Developer Certificate of Origin sign-off (`git commit -s`) is sufficient for those. The MIT modules are
  the recommended starting point: fork one, rename it (see `legal/TRADEMARK.md`), and license your derivative
  however you wish.

**How CLA acceptance works.** Contributions to official Celerp repositories require a one-time CLA
acceptance per user. If you haven't accepted yet, you'll be linked to a page that shows the agreement and
records your acceptance so that we know that you've read our rules and expectations. After that you can
contribute freely.

Trademarks are not licensed by any code license; see `legal/TRADEMARK.md`.

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
celerp init             # first-time setup: DB + migrations + config, then launches the servers
celerp init --no-start  # same setup, but exit WITHOUT launching (service-managed/headless)
celerp start            # launch API + UI (e.g. from systemd, after `init --no-start`)
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
| `--yes` / `-y` | off | Skip the `--force` wipe confirmation (non-interactive) |
| `--no-start` | off | Set up, then exit WITHOUT launching servers (service-managed/headless) |

To change ports after init, edit `~/.config/celerp/config.toml` directly.

### Run as a service (systemd)

`celerp init` launches the servers and blocks (good for the one-command desktop
flow). For a headless box where a process manager owns the lifecycle, set up with
`--no-start` and let systemd run `celerp start`:

```bash
# First boot: provision DB + migrations + config, then exit (do not block).
sudo celerp init --no-start \
  --db-url "postgresql+asyncpg://celerp:<password>@localhost:5432/celerp" \
  --api-port 8000 --ui-port 8080
```

```ini
# /etc/systemd/system/celerp.service
[Unit]
Description=Celerp
After=network.target postgresql.service

[Service]
ExecStart=/usr/local/bin/celerp start
Restart=on-failure
User=celerp

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now celerp
```

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

## License

Celerp is multi-licensed (see [LICENSING.md](LICENSING.md)). Your contribution is governed by the
**Contribution terms** at the top of this file (a signed CLA for anything accepted upstream, including
the MIT modules; DCO for third-party modules you keep). License headers follow the per-path policy in
`LICENSING.md` and are enforced by `scripts/licenses.py`.

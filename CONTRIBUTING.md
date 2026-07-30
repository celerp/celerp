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
| `--cloud-token` | _(empty)_ | Celerp Connect token (optional) |
| `--force` | off | Overwrite existing config |
| `--yes` / `-y` | off | Skip the `--force` wipe confirmation (non-interactive) |
| `--no-start` | off | Set up, then exit WITHOUT launching servers (service-managed/headless) |
| `--embedded` | off | Force the bundled PostgreSQL even if a server is reachable (cannot combine with `--db-url`) |
| `--no-embedded` | off | Never fall back to the bundled PostgreSQL; require an external server |

To change ports after init, edit `~/.config/celerp/config.toml` directly.

### External vs embedded PostgreSQL

`celerp init` picks the database automatically, and **an existing server always wins**:

1. `--db-url` given → that external server (no detection).
2. else a PostgreSQL server reachable on `localhost:5432` → use it (as root, init
   provisions the role/database via `sudo -u postgres psql`; otherwise it prints the
   `CREATE USER`/`CREATE DATABASE` to run).
3. else the **bundled** PostgreSQL 17 — a self-contained cluster under
   `~/.config/celerp/pgdata`, started from binaries shipped in the
   [`celerp-postgres`](https://github.com/celerp/celerp-postgres) wheel. No `sudo`, no
   system service. Available on Linux x86_64/arm64 (glibc and musl/Alpine) and Windows
   x64, any CPython; on macOS 26+ opt in with `pip install celerp-postgres`. Elsewhere
   init asks you to install PostgreSQL.

Override the detection with `--embedded` / `--no-embedded`. The chosen mode is written to
`config.toml` as `[database] embedded = true|<absent>` and preserved across `--force`
re-inits, so an external install never silently switches to the bundled cluster.

**Moving bundled data to an external server** (e.g. outgrowing single-machine): dump from
the bundled cluster and restore into the target, using the bundled tools so versions match.

```bash
BIN=$(celerp status >/dev/null; python -c "from celerp import embedded_pg; print(embedded_pg.bin_dir())")
"$BIN/pg_dump" "$(python -c "from celerp import embedded_pg,pathlib; from celerp.config import config_path; print(embedded_pg.ensure_cluster(config_path().parent).replace('+asyncpg',''))")" \
  | psql "postgresql://celerp:celerp@newhost:5432/celerp"
celerp init --force --db-url "postgresql+asyncpg://celerp:celerp@newhost:5432/celerp"
```

### Run as a service (systemd)

`celerp init` launches the servers and blocks (good for the one-command desktop
flow). For a headless box where a process manager owns the lifecycle, set up with
`--no-start` and let systemd run `celerp start`:

```bash
# First boot against a system PostgreSQL: provision DB + migrations + config, then
# exit (do not block). Run as root so init can create the role/database.
sudo celerp init --no-start \
  --db-url "postgresql+asyncpg://celerp:<password>@localhost:5432/celerp" \
  --api-port 8000 --ui-port 8080
```

Prefer the bundled database? Drop `--db-url` (and the `sudo` — the bundled cluster needs
neither) and run `celerp init --no-start` as the service user. The cluster then lives under
that user's `~/.config/celerp/pgdata`, and the systemd unit needs no `postgresql.service`
dependency.

```ini
# /etc/systemd/system/celerp.service
[Unit]
Description=Celerp
# Drop the postgresql.service dependency line when using the bundled database.
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

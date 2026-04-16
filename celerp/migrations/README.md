# Celerp Migrations

Alembic-managed DDL migrations for the Celerp ERP system.

## Dev vs Production

| Environment | Database | Schema management |
|-------------|----------|-------------------|
| Development | SQLite   | `create_all()` — fast iteration, no migrations |
| Production  | PostgreSQL | Alembic — controlled, versioned DDL |

**Do not run Alembic migrations against SQLite.** SQLite is dev-only.

## Running migrations

```bash
# Apply all pending migrations
DATABASE_URL="postgresql://user:pass@host/dbname" alembic upgrade head

# Check current migration state
DATABASE_URL="..." alembic current

# View migration history
DATABASE_URL="..." alembic history

# Rollback one step
DATABASE_URL="..." alembic downgrade -1
```

`DATABASE_URL` must be a synchronous PostgreSQL URL (e.g. `postgresql://...`).
Async prefixes (`postgresql+asyncpg://`) are stripped automatically by env.py.

## Generating new migrations

After changing a SQLAlchemy model:

```bash
DATABASE_URL="postgresql://..." alembic revision --autogenerate -m "describe change"
```

Review the generated file in `versions/` before committing.

## Files

- `env.py` — Alembic environment; reads `DATABASE_URL` from env
- `script.py.mako` — template for new migration files
- `versions/` — individual migration scripts

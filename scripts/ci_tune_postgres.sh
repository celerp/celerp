#!/bin/sh
# Turn off durability on a CI job's throwaway Postgres service: its data is discarded, and the
# per-commit disk syncs dominate a commit-heavy suite. Server-wide, so it covers every xdist
# worker's database; all three settings take effect on reload. Fails the job if it cannot.
# Usage: ci_tune_postgres.sh <postgresql://user:password@host:port/db>
set -eu
psql -v ON_ERROR_STOP=1 "$1" <<'SQL'
ALTER SYSTEM SET fsync = off;
ALTER SYSTEM SET synchronous_commit = off;
ALTER SYSTEM SET full_page_writes = off;
SELECT pg_reload_conf();
SQL

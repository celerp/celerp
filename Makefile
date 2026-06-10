# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

# ── Postgres test database ──────────────────────────────────────────────────
# The test suite runs on Postgres (both prod targets do). For a fast *parallel*
# local run, share one throwaway container across xdist workers (each worker gets
# its own database). `make test` does the whole dance. Without a preset
# DATABASE_URL, a plain `pytest` instead auto-starts a per-process container via
# testcontainers (fine for sequential runs).

PG_CONTAINER := celerp-test-pg
PG_PORT      := 5440
TEST_DATABASE_URL := postgresql+asyncpg://celerp:celerp@localhost:$(PG_PORT)/celerp_test
PYTEST := .venv/bin/python -m pytest

.PHONY: test test-up test-down

test-up:
	-docker rm -f $(PG_CONTAINER) >/dev/null 2>&1
	docker run -d --name $(PG_CONTAINER) \
		-e POSTGRES_USER=celerp -e POSTGRES_PASSWORD=celerp -e POSTGRES_DB=celerp_test \
		-p $(PG_PORT):5432 postgres:16-alpine \
		-c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=300 >/dev/null
	@echo "waiting for postgres..."
	@for i in $$(seq 1 30); do \
		docker exec $(PG_CONTAINER) pg_isready -U celerp -d celerp_test >/dev/null 2>&1 && break; \
		sleep 1; done

test-down:
	-docker rm -f $(PG_CONTAINER) >/dev/null 2>&1

# Full unit suite on a shared Postgres, in parallel. Browser/integration excluded.
test: test-up
	-DATABASE_URL=$(TEST_DATABASE_URL) $(PYTEST) tests/ \
		--ignore=tests/test_browser --ignore=tests/integration \
		-n auto -p no:cacheprovider -q
	$(MAKE) test-down

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Performance regression tests for projection query hot paths.

These tests verify that list and summary endpoints do NOT load all projections
and then filter in Python - they must push filters into SQL.

Test strategy:
  SQL statement capture via SQLAlchemy engine event hooks on the shared test engine.
  The `_db_engine` fixture is session-scoped; tests attach/detach listeners around
  each API call and inspect the captured SQL for WHERE clauses and LIMIT.

  Symptom tests: FAIL today (SQL has no doc_type/status filter, no LIMIT).
  Correctness tests: pass before and after (regression guards).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.projections import Projection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


def _make_doc_projection(
    company_id: uuid.UUID,
    doc_type: str,
    status: str = "final",
    issue_date: str = "2026-01-15",
) -> Projection:
    eid = str(uuid.uuid4())
    return Projection(
        entity_id=eid,
        company_id=company_id,
        entity_type="doc",
        state={
            "doc_type": doc_type,
            "status": status,
            "issue_date": issue_date,
            "doc_number": f"{doc_type[:3].upper()}-{eid[:6]}",
            "total": 100.0,
            "amount_outstanding": 100.0,
            "amount_paid": 0.0,
            "contact_name": "Test Co",
            "contact_id": str(uuid.uuid4()),
        },
        version=1,
        updated_at=_NOW,
    )


async def _seed_mixed_docs(session: AsyncSession, company_id: uuid.UUID) -> dict[str, int]:
    """Seed 250 doc projections (50 each of 5 types)."""
    counts = {"invoice": 50, "bill": 50, "memo": 50, "purchase_order": 50, "credit_note": 50}
    rows = []
    for doc_type, n in counts.items():
        for _ in range(n):
            rows.append(_make_doc_projection(company_id, doc_type))
    session.add_all(rows)
    await session.commit()
    return counts


@contextmanager
def _capture_projection_sql(db_engine):
    """Context manager: capture SQL statements that touch the projections table."""
    statements: list[str] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if "projections" in statement.lower():
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _listener)
    try:
        yield statements
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _listener)


async def _register_and_seed(client, session) -> tuple[str, uuid.UUID]:
    """Register a new company, seed mixed docs, return (token, company_id)."""
    import base64, json as _j
    email = f"perf-{uuid.uuid4().hex[:8]}@test.com"
    r = await client.post("/auth/register", json={
        "company_name": f"PerfCo-{email[:8]}",
        "email": email, "name": "Admin", "password": "pw123456",
    })
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    company_id = uuid.UUID(_j.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
    await _seed_mixed_docs(session, company_id)
    return token, company_id


# ---------------------------------------------------------------------------
# Symptom tests - DESIGNED TO FAIL before fix, PASS after
# ---------------------------------------------------------------------------

class TestListDocsSqlFiltering:

    @pytest.mark.asyncio
    async def test_list_docs_with_doc_type_filter_uses_sql_where_clause(
        self, client, session, _db_engine
    ):
        """GET /docs?doc_type=memo must issue SQL that includes doc_type in WHERE.

        Before fix:
          SQL: SELECT * FROM projections WHERE company_id=? AND entity_type='doc'
          No doc_type clause - all 250 rows loaded, filtered in Python.

        After fix:
          SQL contains: state->>'doc_type' = 'memo'  (or json_extract equivalent)
        """
        token, _ = await _register_and_seed(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        with _capture_projection_sql(_db_engine) as stmts:
            resp = await client.get("/docs?doc_type=memo", headers=headers)

        assert resp.status_code == 200

        select_stmts = [s for s in stmts if s.strip().upper().startswith("SELECT")]
        assert select_stmts, "Expected SELECT on projections table - got nothing"

        has_doc_type_filter = any(
            "doc_type" in s.lower() or "json_extract" in s.lower()
            for s in select_stmts
        )
        assert has_doc_type_filter, (
            "list_docs?doc_type=memo issued SELECT on projections with NO doc_type "
            "WHERE condition. All rows are loaded into Python and filtered there.\n"
            "This is O(N) in total docs. Fix: push doc_type into SQL WHERE clause.\n"
            f"Actual SQL (first query): {select_stmts[0][:300]}"
        )

    @pytest.mark.asyncio
    async def test_list_docs_with_status_filter_uses_sql_where_clause(
        self, client, session, _db_engine
    ):
        """GET /docs?status=final must issue SQL with status in WHERE clause."""
        token, _ = await _register_and_seed(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        with _capture_projection_sql(_db_engine) as stmts:
            resp = await client.get("/docs?status=final", headers=headers)

        assert resp.status_code == 200

        select_stmts = [s for s in stmts if s.strip().upper().startswith("SELECT")]
        has_status_filter = any(
            "json_extract" in s.lower() or "status" in s.lower()
            for s in select_stmts
        )
        assert has_status_filter, (
            "list_docs?status=final issued SELECT with NO status WHERE condition.\n"
            "Fix: push status filter into SQL WHERE clause.\n"
            f"SQL: {select_stmts[0][:300] if select_stmts else 'none'}"
        )

    @pytest.mark.asyncio
    async def test_list_docs_limit_is_applied_in_sql(
        self, client, session, _db_engine
    ):
        """GET /docs?limit=10 must issue SQL with LIMIT 10.

        Before fix: SELECT all rows, then out[:10] in Python.
        After fix:  SELECT ... LIMIT 10 in SQL.
        """
        token, _ = await _register_and_seed(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        with _capture_projection_sql(_db_engine) as stmts:
            resp = await client.get("/docs?limit=10", headers=headers)

        assert resp.status_code == 200
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        assert len(items) == 10, f"Expected 10 items, got {len(items)}"

        select_stmts = [s for s in stmts if s.strip().upper().startswith("SELECT")]
        # The paginated SELECT (not the COUNT query) must have LIMIT
        paginated_stmts = [s for s in select_stmts if "count(" not in s.lower()]
        main_query = paginated_stmts[0] if paginated_stmts else ""
        assert "LIMIT" in main_query.upper(), (
            "list_docs?limit=10 did NOT include LIMIT in the paginated SQL.\n"
            "This means all rows are loaded into Python before slicing.\n"
            f"SQL: {main_query[:300]}"
        )

    @pytest.mark.asyncio
    async def test_get_doc_summary_with_doc_type_filter_uses_sql_where_clause(
        self, client, session, _db_engine
    ):
        """GET /docs/summary?doc_type=memo must filter by doc_type in SQL.

        Before fix: loads all 250 rows, iterates checking doc_type in Python.
        After fix:  SQL WHERE state->>'doc_type' = 'memo'.
        """
        token, _ = await _register_and_seed(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        with _capture_projection_sql(_db_engine) as stmts:
            resp = await client.get("/docs/summary?doc_type=memo", headers=headers)

        assert resp.status_code == 200

        select_stmts = [s for s in stmts if s.strip().upper().startswith("SELECT")]
        has_doc_type_filter = any(
            "doc_type" in s.lower() or "json_extract" in s.lower()
            for s in select_stmts
        )
        assert has_doc_type_filter, (
            "get_doc_summary?doc_type=memo issued SELECT with NO doc_type WHERE.\n"
            "The summary loads ALL docs and filters in Python even when doc_type is given.\n"
            f"SQL: {select_stmts[0][:300] if select_stmts else 'none'}"
        )

    @pytest.mark.asyncio
    async def test_list_docs_issues_at_most_two_projection_queries_per_page(
        self, client, session, _db_engine
    ):
        """A single docs page load must issue ≤2 SELECT queries on projections.

        Before fix: 3 separate full-scan queries (list + draft_count + summary).
        After fix:  2 (list + summary); draft count uses the list result's total
                    or a lightweight COUNT(*).
        """
        token, _ = await _register_and_seed(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        with _capture_projection_sql(_db_engine) as stmts:
            resp = await client.get("/docs?doc_type=memo&type=memo", headers=headers)

        assert resp.status_code == 200

        select_count = sum(
            1 for s in stmts
            if s.strip().upper().startswith("SELECT") and "entity_type" in s
        )
        assert select_count <= 2, (
            f"Docs page issued {select_count} SELECT queries on projections "
            f"(entity_type filter). Expected ≤2.\n"
            f"Extra queries = redundant full scans loading all docs into memory.\n"
            f"Fix: merge draft-count into main query total, or use COUNT(*) subquery."
        )


# ---------------------------------------------------------------------------
# Correctness tests - must pass BEFORE and AFTER fix (regression guards)
# ---------------------------------------------------------------------------

class TestListDocsCorrectness:

    @pytest.mark.asyncio
    async def test_list_docs_doc_type_filter_returns_correct_subset(self, client, session):
        r = await client.post("/auth/register", json={
            "company_name": "CorrectFilterCo", "email": "correctfilter@test.com",
            "name": "Admin", "password": "pw123456",
        })
        assert r.status_code == 200
        token = r.json()["access_token"]
        import base64, json as _j
        company_id = uuid.UUID(_j.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
        counts = await _seed_mixed_docs(session, company_id)
        headers = {"Authorization": f"Bearer {token}"}

        for doc_type, expected_count in counts.items():
            resp = await client.get(f"/docs?doc_type={doc_type}", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            wrong = [x for x in items if x.get("doc_type") != doc_type]
            assert not wrong, f"doc_type={doc_type}: got wrong types {set(x.get('doc_type') for x in wrong)}"
            assert len(items) == expected_count, f"Expected {expected_count} {doc_type}, got {len(items)}"

    @pytest.mark.asyncio
    async def test_list_docs_status_filter_correct(self, client, session):
        r = await client.post("/auth/register", json={
            "company_name": "StatusFilterCo", "email": "statusfilter@test.com",
            "name": "Admin", "password": "pw123456",
        })
        assert r.status_code == 200
        token = r.json()["access_token"]
        import base64, json as _j
        company_id = uuid.UUID(_j.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
        headers = {"Authorization": f"Bearer {token}"}

        for status in ["draft", "final"]:
            for _ in range(10):
                session.add(_make_doc_projection(company_id, "invoice", status=status))
        await session.commit()

        resp = await client.get("/docs?doc_type=invoice&status=draft", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        assert len(items) == 10
        assert all(x.get("status") == "draft" for x in items)

    @pytest.mark.asyncio
    async def test_list_docs_returns_total_for_pagination(self, client, session):
        r = await client.post("/auth/register", json={
            "company_name": "TotalCo", "email": "totalco@test.com",
            "name": "Admin", "password": "pw123456",
        })
        assert r.status_code == 200
        token = r.json()["access_token"]
        import base64, json as _j
        company_id = uuid.UUID(_j.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
        counts = await _seed_mixed_docs(session, company_id)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/docs?doc_type=invoice&limit=10", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("total") == counts["invoice"]
        assert len(data.get("items", [])) == 10

    @pytest.mark.asyncio
    async def test_list_docs_offset_pagination_no_duplicates(self, client, session):
        r = await client.post("/auth/register", json={
            "company_name": "PaginateCo", "email": "paginate@test.com",
            "name": "Admin", "password": "pw123456",
        })
        assert r.status_code == 200
        token = r.json()["access_token"]
        import base64, json as _j
        company_id = uuid.UUID(_j.loads(base64.b64decode(token.split(".")[1] + "=="))["company_id"])
        headers = {"Authorization": f"Bearer {token}"}

        for _ in range(30):
            session.add(_make_doc_projection(company_id, "invoice"))
        await session.commit()

        p1 = (await client.get("/docs?doc_type=invoice&limit=10&offset=0", headers=headers)).json()
        p2 = (await client.get("/docs?doc_type=invoice&limit=10&offset=10", headers=headers)).json()
        p3 = (await client.get("/docs?doc_type=invoice&limit=10&offset=20", headers=headers)).json()

        ids = [
            {x["id"] for x in pg.get("items", [])}
            for pg in [p1, p2, p3]
        ]
        for i, pg_ids in enumerate(ids):
            assert len(pg_ids) == 10, f"Page {i+1}: expected 10 items"
        assert not (ids[0] & ids[1]), "Pages 1-2 overlap"
        assert not (ids[1] & ids[2]), "Pages 2-3 overlap"

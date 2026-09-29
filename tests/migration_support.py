# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared harness for the company migration lifecycle tests.

A small in-test source adapter and sink stand in for real sources and domain
modules, so the runner, scan store and routes are exercised without depending
on any one source format. The fake source file is a magic header followed by a
JSON description of the source it pretends to be.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from celerp.importers.adapters.base import (
    ArtifactSpec,
    DetectionResult,
    MappingQuestion,
    MigrationDecisions,
    ScanError,
    SourceScan,
)
from celerp.importers.schema import (
    CIFCompanyProfile,
    CIFCoverageEntry,
    CIFImportBundle,
    CIFImportManifest,
    CIFJournalEntry,
    CIFJournalLine,
    CIFLocation,
    CIFMode,
    CIFTolerance,
    ReconciliationExpectation,
    ReconciliationExpectations,
    ReconciliationMeasure,
)
from celerp.importers.sinks import DestinationMeasurement, SinkBatchResult, SinkEntityMapping
from test_helpers import DATABASE_URL

FAKE_MAGIC = b"FAKESRC1\n"
FAKE_KEY = "fake_source"
OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "ownerpw123"


def fake_spec(**overrides) -> dict:
    """A clean, migratable fake source: a company, three locations and one journal."""
    spec = {
        "company": "Fake Co",
        "currency": "USD",
        "schema": "7",
        "period": ["2026-01-01", "2026-03-31"],
        "first_transaction": "2026-01-05",
        "locations": 3,
        "journal": True,
        "coverage": [
            {"source_type": "Company", "count": 1, "coverage_class": "mapped", "target": "company"},
            {"source_type": "Location", "count": 3, "coverage_class": "mapped", "target": "location"},
            {"source_type": "Payment", "count": 1, "coverage_class": "mapped", "target": "settlement"},
        ],
        "questions": [],
        "expected_locations": None,
    }
    spec.update(overrides)
    return spec


def fake_bytes(spec: dict | None = None) -> bytes:
    return FAKE_MAGIC + json.dumps(spec or fake_spec()).encode()


def _read(artifacts) -> dict:
    return json.loads(artifacts[0].path.read_bytes()[len(FAKE_MAGIC):])


class FakeAdapter:
    key = FAKE_KEY
    display_name = "Fake source"
    adapter_version = "1"

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.artifact_specs = (ArtifactSpec("source_file", "Source file", (".fake",), max_bytes),)
        self.inspect_calls = 0

    def detect(self, artifacts) -> DetectionResult:
        if len(artifacts) == 1 and artifacts[0].path.read_bytes()[: len(FAKE_MAGIC)] == FAKE_MAGIC:
            return DetectionResult(True)
        return DetectionResult(False, "Not a fake source file.")

    def inspect(self, artifacts) -> SourceScan:
        self.inspect_calls += 1
        spec = _read(artifacts)
        if spec.get("unreadable"):
            raise ScanError("This file is damaged.")
        counts = {"Location": spec["locations"], "Payment": 1 if spec["journal"] else 0}
        counts = {k: v for k, v in counts.items() if v}
        return SourceScan(
            source_system=FAKE_KEY,
            source_schema_version=spec["schema"],
            company_name=spec["company"],
            base_currency=spec["currency"],
            period_start=date.fromisoformat(spec["period"][0]),
            period_end=date.fromisoformat(spec["period"][1]),
            currencies=(spec["currency"],),
            object_counts=counts,
            features=("locations",),
            coverage=tuple(CIFCoverageEntry(**c) for c in spec["coverage"]),
            questions=tuple(MappingQuestion(**{**q, "options": tuple(q["options"])}) for q in spec["questions"]),
        )

    def build_manifest(self, artifacts, decisions: MigrationDecisions) -> CIFImportManifest:
        spec = _read(artifacts)
        if decisions.mode == CIFMode.CUTOVER:
            if decisions.cutover_date < date.fromisoformat(spec["first_transaction"]):
                raise ScanError("The cutover date is before the first transaction in this business file.")
        common = {"source_system": FAKE_KEY}
        journals = []
        if spec["journal"]:
            journals.append(CIFJournalEntry(
                **common, source_type="Payment", source_external_id="pay-1:journal",
                entry_date=date.fromisoformat(spec["first_transaction"]),
                lines=[CIFJournalLine(account_external_id="1000", debit=Decimal("5")),
                       CIFJournalLine(account_external_id="2000", credit=Decimal("5"))],
            ))
        bundle = CIFImportBundle(
            company=CIFCompanyProfile(**common, source_type="Company", source_external_id="company",
                                      name=spec["company"], base_currency=spec["currency"]),
            locations=[CIFLocation(**common, source_type="Location", source_external_id=f"loc-{i}",
                                   name=f"Store {i}") for i in range(1, spec["locations"] + 1)],
            journals=journals,
        )
        return CIFImportManifest(
            source=spec["company"], source_system=FAKE_KEY, source_schema_version=spec["schema"],
            adapter_version=self.adapter_version, mode=decisions.mode, cutover_date=decisions.cutover_date,
            exported_at=datetime(2026, 4, 1, tzinfo=timezone.utc), bundle=bundle,
            coverage=[CIFCoverageEntry(**c) for c in spec["coverage"]],
        )

    def source_expectations(self, artifacts, decisions) -> ReconciliationExpectations:
        spec = _read(artifacts)
        expected = spec["expected_locations"]
        rows = [ReconciliationExpectation(
            measure=ReconciliationMeasure.DOCUMENT_COUNT, key="Location",
            expected=Decimal(spec["locations"] if expected is None else expected),
            tolerance=CIFTolerance(kind="exact"),
        )]
        for extra in spec.get("extra_expectations", []):
            rows.append(ReconciliationExpectation(
                measure=extra["measure"], key=extra.get("key", ""), expected=Decimal(extra["expected"]),
                tolerance=CIFTolerance(kind="exact"),
            ))
        return ReconciliationExpectations(expectations=rows)


class FakeSink:
    """Writes locations with deterministic ids, so a replayed batch is skipped, not duplicated."""

    key = "fake"
    groups = frozenset({"company", "locations", "journals"})

    def __init__(self, batch_size: int = 2) -> None:
        self.batch_size = batch_size
        self.fail_on: dict[str, int] = {}        # source_external_id -> remaining failures
        self.on_batch = None                      # async callable(context, records), run before writing
        self.reconcile_error: Exception | None = None
        self.events: list[tuple] = []
        self.keys: list[str] = []

    async def import_batch(self, context, records: Sequence) -> SinkBatchResult:
        self.events.append(("import_batch", [r.source_external_id for r in records]))
        if self.on_batch is not None:
            await self.on_batch(context, records)
        result = SinkBatchResult()
        for record in records:
            if self.fail_on.get(record.source_external_id, 0) > 0:
                self.fail_on[record.source_external_id] -= 1
                raise RuntimeError(f"Sink failed on {record.source_external_id}.")
            self.keys.append(context.idempotency_key(record, "create"))
            if isinstance(record, CIFCompanyProfile):
                target, entity_type, created = str(context.company_id), "company", False
            elif isinstance(record, CIFLocation):
                target = str(uuid.uuid5(context.run_id, record.source_external_id))
                entity_type = "location"
                inserted = await context.session.execute(text(
                    "INSERT INTO locations (id, company_id, name, type, is_default, created_at) "
                    "VALUES (:id, :cid, :name, 'store', false, now()) ON CONFLICT (id) DO NOTHING RETURNING id"
                ), {"id": target, "cid": context.company_id, "name": record.name})
                created = inserted.first() is not None
            else:
                target, entity_type, created = str(uuid.uuid5(context.run_id, record.source_external_id)), "journal_entry", True
            if created:
                result.created += 1
            else:
                result.skipped += 1
            result.mappings.append(SinkEntityMapping(
                record.source_type, record.source_external_id, entity_type, target,
                "created" if created else "skipped",
            ))
        return result

    async def reconcile(self, context, expectations) -> list[DestinationMeasurement]:
        if self.reconcile_error is not None:
            raise self.reconcile_error
        count = (await context.session.execute(
            text("SELECT count(*) FROM locations WHERE company_id = :cid"), {"cid": context.company_id}
        )).scalar_one()
        return [DestinationMeasurement(ReconciliationMeasure.DOCUMENT_COUNT, "Location", None, Decimal(count))]


async def _chunks(data: bytes, size: int = 1024) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i:i + size]


async def upload_parts(*files: tuple[str, bytes], source: str | None = None):
    from celerp.services.migration_scan_store import UploadPart as StoreUploadPart
    if source is not None:
        yield StoreUploadPart("source", None, _chunks(source.encode()))
    for name, data in files:
        yield StoreUploadPart("files", name, _chunks(data))


def multipart(*files: tuple[str, bytes]) -> list:
    return [("files", (name, data, "application/octet-stream")) for name, data in files]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def migration_env(tmp_path, monkeypatch):
    """Mount the migrations router, point the data dir at tmp, register the fake
    source and Manager adapter, register only the fake sink, and record scheduled
    runner tasks instead of starting them."""
    from celerp.config import settings
    from celerp.importers import sinks
    from celerp.importers.adapters import registry
    from celerp.importers.adapters.manager_io.adapter import ManagerIOAdapter
    from celerp.main import app
    from celerp.routers.migrations import router
    from celerp.services import migrations

    if not any(getattr(r, "path", "").startswith("/migrations") for r in app.routes):
        app.include_router(router)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    adapter = FakeAdapter()
    monkeypatch.setattr(registry, "_ADAPTERS", (adapter, ManagerIOAdapter()))
    sink = FakeSink()
    monkeypatch.setattr(sinks, "_SINKS", {sink.key: sink})
    scheduled: list[uuid.UUID] = []
    monkeypatch.setattr(migrations, "schedule_run", lambda run_id: scheduled.append(run_id))
    return {"adapter": adapter, "sink": sink, "scheduled": scheduled, "data_dir": tmp_path}


@pytest.fixture
def code_config(tmp_path, monkeypatch):
    """Point config at a temp file that requires a known setup code."""
    from celerp.config import write_config
    cf = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cf))
    code = "abcd1234"
    write_config({
        "database": {"url": "postgresql+asyncpg://x/y"},
        "auth": {"jwt_secret": "s" * 64, "setup_code_hash": hashlib.sha256(code.encode()).hexdigest()},
        "server": {"api_port": 8000, "ui_port": 8080, "headless": True},
        "cloud": {"token": ""},
    })
    return code


@pytest_asyncio.fixture
async def real_engine(_db_engine, monkeypatch):
    """A NullPool engine with production request timeouts on the worker DB, used as
    celerp.db.engine so the runner's own connections see committed test data."""
    import celerp.db

    engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "3000", "statement_timeout": "30000"}},
    )

    async def _truncate():
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))

    await _truncate()
    monkeypatch.setattr(celerp.db, "engine", engine)
    try:
        yield engine
    finally:
        await _truncate()
        await engine.dispose()


def maker(engine):
    return lambda: AsyncSession(bind=engine, expire_on_commit=False)


async def count(engine, table: str, where: str = "", **params) -> int:
    async with engine.connect() as conn:
        sql = f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")
        return (await conn.execute(text(sql), params)).scalar_one()


async def staged_run(engine, *, spec: dict | None = None, decisions: dict | None = None,
                     start: bool = True, email: str = OWNER_EMAIL):
    """Commit an owner, a staged inactive company and a run built through the real
    scan store and service calls. Returns (run_id, company_id, user_id)."""
    from celerp.models.company import User
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations, provisioning

    async with maker(engine)() as s:
        first = await s.scalar(select(User.id).limit(1)) is None
        user = User(email=email, name="Owner", is_install_owner=first)
        s.add(user)
        await s.flush()
        owner = ("user", user.id)
        scan = await store.create_scan(upload_parts(("books.fake", fake_bytes(spec))), owner=owner)
        chosen = migrations.validate_decisions(scan, decisions or {"mode": "full_history"})
        scan = store.save_decisions(scan.token, owner=owner, decisions=chosen)
        company = await provisioning.provision_migration_company(s, owner=user, company_name="Fake Co")
        run = await migrations.create_run(s, company=company, user=user, scan=scan, decisions=chosen)
        if start:
            await migrations.request_start(s, run)
        await s.commit()
        return run.id, company.id, user.id


async def load_run(engine, run_id):
    from celerp.models.migration import MigrationRun
    async with maker(engine)() as s:
        return await s.get(MigrationRun, run_id)


@pytest_asyncio.fixture
async def real_client(real_engine):
    """An API client whose request sessions commit for real on `real_engine`, so
    routes, the runner and concurrent requests all see the same committed state."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock, patch

    from httpx import ASGITransport, AsyncClient

    from celerp.db import get_session
    from celerp.main import app

    async def _session():
        async with maker(real_engine)() as s:
            yield s

    @asynccontextmanager
    async def _session_ctx():
        async with maker(real_engine)() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    try:
        with patch("celerp.gateway.client._client", MagicMock()), \
             patch("celerp.gateway.state.get_session_token", return_value="test-session-token"), \
             patch("celerp.middleware.get_session_ctx", _session_ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def scan_upload(client, data: bytes, *, token: str | None = None, source: str | None = None,
                      name: str = "books.fake", headers: dict | None = None):
    path = "/migrations/scan" if token else "/migrations/bootstrap/scan"
    return await client.post(path, files=multipart((name, data)),
                             data={"source": source} if source else None,
                             headers={**(auth(token) if token else {}), **(headers or {})})


async def save_decisions(client, scan_token: str, *, token: str | None = None, **decisions):
    path = "/migrations/scan/decisions" if token else "/migrations/bootstrap/decisions"
    body = {"scan_token": scan_token, "mode": "full_history", "cutover_date": None,
            "mappings": {}, "prepared_by": None, **decisions}
    return await client.post(path, json=body, headers=auth(token) if token else {})


async def migrate_as_owner(client, owner_token: str, spec: dict | None = None,
                           company_name: str = "Moved Co", **decisions):
    """Scan, decide and start from an owner's session. Returns (new company token, run id)."""
    r = await scan_upload(client, fake_bytes(spec), token=owner_token)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    r = await save_decisions(client, scan_token, token=owner_token, **decisions)
    assert r.status_code == 200, r.text
    r = await client.post("/migrations/start-from-scan", headers=auth(owner_token),
                          json={"scan_token": scan_token, "company_name": company_name})
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["run_id"]

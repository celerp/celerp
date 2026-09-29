# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end migrations of the Manager fixtures through the real runner and sinks."""

from __future__ import annotations

import ipaddress
import logging
import socket

import httpx
import pytest
from sqlalchemy import select

from migration_support import OWNER_EMAIL, count, load_run, maker, real_engine, upload_parts  # noqa: F401

pytestmark = pytest.mark.asyncio


async def migrate(engine, data: bytes, name: str, decisions: dict, monkeypatch, tmp_path):
    """Scan, decide and run one source file into a staged company named as the source
    names itself, as the wizard proposes.

    Returns the finished run and every record a sink rejected, as
    (source_type, source_external_id, message).
    """
    from celerp.config import settings
    from celerp.importers import sinks
    from celerp.models.company import User
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations, provisioning

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    rejected: list[tuple[str, str, str]] = []
    for sink in set(sinks._SINKS.values()):
        async def recording(context, records, _import=sink.import_batch):
            result = await _import(context, records)
            rejected.extend((e.source_type, e.source_external_id, e.message) for e in result.errors)
            return result
        monkeypatch.setattr(sink, "import_batch", recording)
    async with maker(engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        owner = ("user", user.id)
        scan = await store.create_scan(upload_parts((name, data)), owner=owner)
        chosen = migrations.validate_decisions(scan, decisions)
        scan = store.save_decisions(scan.token, owner=owner, decisions=chosen)
        company = await provisioning.provision_migration_company(s, owner=user, company_name=scan.scan.company_name)
        run = await migrations.create_run(s, company=company, user=user, scan=scan, decisions=chosen)
        run_id = run.id
        await s.commit()
        await migrations.claim_source(s, run_id, token=scan.token, start=True)
    await migrations.run_migration(run_id)
    return await load_run(engine, run_id), rejected


# ── Lifecycle hooks and staged companies ──────────────────────────────────────

async def test_chart_backfill_skips_staged_migration_company(real_engine):
    """The boot-time chart backfill seeds active companies only: a staged migration
    company's chart comes from the imported books."""
    from celerp.models.company import Company, User
    from celerp.services import provisioning
    from celerp_accounting.models import Account
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    async with maker(real_engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        staged = await provisioning.provision_migration_company(s, owner=user, company_name="Staged Co")
        active = Company(name="Active Co", slug="active-co", settings={}, is_active=True)
        s.add(active)
        await s.flush()
        await backfill_chart_of_accounts_hook(session=s)
        await s.commit()
        seeded = set((await s.execute(select(Account.company_id).distinct())).scalars())
    assert active.id in seeded
    assert staged.id not in seeded


async def test_default_location_is_settled_once_and_deterministically(real_engine):
    """A company without a default gets exactly one: the oldest location, or the
    standard one when it has none, and asking again changes nothing."""
    from celerp.models.company import Location, User
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        empty = await provisioning.provision_migration_company(s, owner=user, company_name="Empty Co")
        created = await provisioning.ensure_default_location(s, empty.id)
        assert (await provisioning.ensure_default_location(s, empty.id)).id == created.id
        assert created.name == provisioning.DEFAULT_LOCATION_NAME

        stocked = await provisioning.provision_migration_company(s, owner=user, company_name="Stocked Co")
        first = Location(company_id=stocked.id, name="Warehouse A", type="warehouse", is_default=False)
        s.add(first)
        await s.flush()
        s.add(Location(company_id=stocked.id, name="Warehouse B", type="warehouse", is_default=False))
        await s.flush()
        assert (await provisioning.ensure_default_location(s, stocked.id)).id == first.id
        await provisioning.add_missing_required_defaults(s, stocked.id)
        defaults = (await s.execute(
            select(Location).where(Location.company_id == stocked.id, Location.is_default.is_(True))
        )).scalars().all()
        assert [loc.id for loc in defaults] == [first.id]
        await s.commit()


# ── Manager fixtures end to end ───────────────────────────────────────────────

# Contact names, emails and memo text of the synthetic books: none may reach a log line.
_PRIVATE_TEXT = ("Acme Trading", "accounts@acme.example.com", "2 Example Road", "Owner funding", "Walk-in sale",
                 "Consulting hours", "Price adjustment")
_APP_LOGGERS = ("celerp", "celerp_accounting", "celerp_contacts", "celerp_docs", "celerp_inventory")
_FINANCIAL_GROUPS = {"documents", "settlements", "journals", "bank_transfers"}


def _loopback(host) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@pytest.fixture
def outbound(monkeypatch) -> list[str]:
    """Refuse every connection that leaves the machine and record the attempt."""
    attempts: list[str] = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _guard(address) -> None:
        if isinstance(address, tuple) and not _loopback(address[0]):
            attempts.append(str(address[0]))
            raise OSError("Outbound network access is blocked in this test.")

    def guarded_connect(sock, address):
        _guard(address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        _guard(address)
        return connect_ex(sock, address)

    def refuse(request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("Outbound network access is blocked in this test.", request=request)

    async def guarded_async_send(client, request, *args, **kwargs):
        refuse(request)

    def guarded_send(client, request, *args, **kwargs):
        refuse(request)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)
    monkeypatch.setattr(httpx.Client, "send", guarded_send)
    return attempts


async def _maps(engine, run) -> list:
    from celerp.models.migration import MigrationEntityMap

    async with maker(engine)() as s:
        return list((await s.execute(
            select(MigrationEntityMap).where(MigrationEntityMap.migration_run_id == run.id)
        )).scalars())


async def _projections(engine, run, entity_type: str) -> dict:
    from celerp.models.projections import Projection

    async with maker(engine)() as s:
        return {p.entity_id: p.state for p in (await s.execute(select(Projection).where(
            Projection.company_id == run.company_id, Projection.entity_type == entity_type,
        ))).scalars()}


def _passing(run) -> None:
    assert run.status == "ready_to_finalize", run.error_summary
    assert run.reconciliation["blockers"] == 0
    assert [r for r in run.reconciliation["rows"] if r["result"] not in ("pass", "rounding")] == []


@pytest.mark.parametrize("fixture, decisions", [
    ("basic", {"mode": "full_history"}),
    ("basic", {"mode": "cutover", "cutover_date": "2026-02-28"}),
    ("sample", {"mode": "full_history"}),
])
async def test_source_financial_effect_has_exactly_one_representation(
    real_engine, monkeypatch, tmp_path, fixture, decisions,
):
    """Every source financial effect lands once, as a native Celerp record or as a
    journal fallback, never both; every fallback is listed as mapped with loss in
    the run's coverage report."""
    from celerp.importers.sample import SAMPLE_ARTIFACT
    from celerp.services import migrations
    from fixtures.manager_io.support import BASIC

    source = BASIC if fixture == "basic" else SAMPLE_ARTIFACT
    run, rejected = await migrate(real_engine, source.read_bytes(), source.name, decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    maps = await _maps(real_engine, run)
    async with maker(real_engine)() as s:
        report = await migrations.run_view(s, run)
    coverage = {(e["source_type"], e["coverage_class"]) for e in report["coverage"]}

    financial = [m for m in maps if m.meta["group"] in _FINANCIAL_GROUPS]
    assert {m.meta["representation"] for m in maps} <= {"native", "journal_fallback"}
    fallbacks = [m for m in financial if m.meta["representation"] == "journal_fallback"]
    # One Celerp record per effect: no journal carries two source effects.
    journals = [m.target_entity_id for m in financial if m.target_entity_type == "journal_entry"]
    assert len(journals) == len(set(journals))
    # A fallback effect is never also a document, and a document effect is never also a journal.
    docs = await _projections(real_engine, run, "doc")
    assert set(docs) == {m.target_entity_id for m in maps if m.target_entity_type == "doc"}
    doc_sources = {m.source_external_id for m in financial if m.target_entity_type == "doc"}
    journal_sources = {m.source_external_id.removesuffix(":journal")
                       for m in financial if m.target_entity_type == "journal_entry"}
    assert doc_sources.isdisjoint(journal_sources)
    # The split lines of a receipt or payment never touch the balances its settlement posts.
    control_accounts = {m.target_entity_id for m in maps
                        if m.source_type in ("BalanceSheetAccountsReceivableAccount",
                                             "BalanceSheetAccountsPayableAccount")}
    entries = await _projections(real_engine, run, "journal_entry")
    for m in fallbacks:
        if m.source_external_id.endswith(":journal"):
            assert {e["account"] for e in entries[m.target_entity_id]["entries"]}.isdisjoint(control_accounts)
    for m in fallbacks:
        assert ((m.source_type, "mapped_with_loss") in coverage
                or (f"{m.source_type} (journal fallback)", "mapped_with_loss") in coverage), m.source_type
    if fixture == "basic" and decisions["mode"] == "full_history":
        assert {m.source_type for m in fallbacks} == {"DebitNote", "Receipt"}


async def test_manager_full_history_reconciles_synthetic_fixture(real_engine, monkeypatch, tmp_path, caplog, outbound):
    """The synthetic Manager books migrate in full with nothing blocking, no outbound
    connection, and no contact or memo text in the logs."""
    from fixtures.manager_io.support import BASIC, ref

    for name in _APP_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    run, rejected = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                                  monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    assert outbound == []
    for text in _PRIVATE_TEXT:
        assert text not in caplog.text

    # The source attachment is stored and attached to the invoice it belongs to.
    maps = {(m.source_type, m.source_external_id): m for m in await _maps(real_engine, run)}
    invoice = maps[("SalesInvoice", ref("INV1"))].target_entity_id
    stored = maps[("Attachment", ref("ATT1"))].target_entity_id
    files = (await _projections(real_engine, run, "doc"))[invoice]["files"]
    assert [(f["id"], f["filename"]) for f in files] == [(stored, "receipt-scan.png")]


async def test_manager_cutover_reconciles_synthetic_fixture(real_engine, monkeypatch, tmp_path):
    """A cutover migration carries one opening journal at the cutover date, the documents
    still open at it, and every record after it natively; the migrated company ends at the
    same position as the source's full history."""
    from decimal import Decimal

    from fixtures.manager_io import specs
    from fixtures.manager_io.support import CHECKPOINTS, expected_rows, ref

    fixture = CHECKPOINTS["cutover_fixture"]
    source = specs.build_cutover(tmp_path / "cutover.manager")
    run, rejected = await migrate(real_engine, source.read_bytes(), source.name,
                                  {"mode": "cutover", "cutover_date": fixture["cutover_date"]}, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    maps = await _maps(real_engine, run)
    mapped = {m.source_external_id for m in maps}

    # Every record after the cutover, and the invoice still open at it, is imported as itself.
    assert {ref(label) for label in fixture["native"]} <= mapped
    # Records closed before the cutover are carried only by the opening position.
    assert not {ref(label) for label in ("INVA", "BILLA", "PA", "RA", "JE1")} & mapped
    source_account = {m.target_entity_id: m.source_external_id for m in maps if m.target_entity_type == "account"}
    entries = await _projections(real_engine, run, "journal_entry")
    journals = {m.source_external_id: entries[m.target_entity_id] for m in maps
                if m.target_entity_type == "journal_entry"}
    assert set(journals) == {f"opening:{fixture['cutover_date']}", ref("IATB"), ref("JEB")}

    opening = journals[f"opening:{fixture['cutover_date']}"]
    assert str(opening["ts"])[:10] == fixture["cutover_date"]
    lines: dict[str, Decimal] = {}
    for line in opening["entries"]:
        account = source_account[line["account"]]
        lines[account] = lines.get(account, Decimal(0)) + Decimal(str(line["debit"])) - Decimal(str(line["credit"]))
    assert lines == {ref(label): Decimal(amount) for label, amount in fixture["opening_journal"].items()}
    stock = [m for m in maps if m.source_type == "OpeningBalances" and m.meta["group"] == "inventory_adjustments"]
    assert [m.source_external_id for m in stock] == [
        f"opening:{fixture['cutover_date']}:{ref(label)}" for label in fixture["opening_inventory"]]

    # The figures Celerp holds, not just the pass verdicts: trial balance, AR/AP, bank and cash,
    # stock, and document counts and totals equal the source's full-history position.
    held = {(r["check"], r["key"], r["currency"]): r["celerp"] for r in run.reconciliation["rows"]}
    expected = {(check, key, currency): figure for check, key, currency, figure in expected_rows(fixture, "USD")}
    assert {row: Decimal(held[row]) if held.get(row) is not None else None for row in expected} == expected


async def test_sample_migration_finalizes_and_reconciles(real_engine, monkeypatch, tmp_path):
    """The shipped sample migrates and finalizes into a company named as the sample,
    and its completion page says so."""
    from fasthtml.common import to_xml
    from starlette.requests import Request

    from celerp.importers.sample import SAMPLE_ARTIFACT, SAMPLE_COMPANY_NAME
    from celerp.models.company import Company
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations
    from ui.routes.migrations import _complete_page

    run, rejected = await migrate(real_engine, SAMPLE_ARTIFACT.read_bytes(), SAMPLE_ARTIFACT.name,
                                  {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    async with maker(real_engine)() as s:
        run = await s.get(MigrationRun, run.id)
        await migrations.finalize(s, run)
    async with maker(real_engine)() as s:
        run = await s.get(MigrationRun, run.id)
        company = await s.get(Company, run.company_id)
        view = await migrations.run_view(s, run)
    assert run.status == "completed"
    assert run.reconciliation["blockers"] == 0
    assert company.is_active and company.name == SAMPLE_COMPANY_NAME
    assert view["is_sample"] and view["company_name"] == SAMPLE_COMPANY_NAME

    request = Request({"type": "http", "method": "GET", "path": f"/migrations/{run.id}/complete",
                       "query_string": b"", "headers": []})
    page = to_xml(await _complete_page(request, view))
    assert "That&#x27;s the whole migration." in page or "That's the whole migration." in page
    assert "Move your first company" in page


@pytest.mark.parametrize("source, decisions", [
    ("basic", {"mode": "full_history"}),
    ("basic", {"mode": "cutover", "cutover_date": "2026-02-28"}),
    ("sample", {"mode": "full_history"}),
])
async def test_discard_after_real_migration_removes_everything(real_engine, monkeypatch, tmp_path, source, decisions):
    """Discarding a staged company after a real migration through the domain sinks
    removes every company-scoped row the migration wrote, the company itself, and
    the attachment files stored for it."""
    from sqlalchemy import text

    from celerp.importers.sample import SAMPLE_ARTIFACT
    from celerp.models.migration import MigrationRun
    from celerp.services import migrations
    from fixtures.manager_io.support import BASIC

    path = {"basic": BASIC, "sample": SAMPLE_ARTIFACT}[source]
    run, rejected = await migrate(real_engine, path.read_bytes(), path.name, decisions, monkeypatch, tmp_path)
    assert rejected == []
    _passing(run)
    company = str(run.company_id)
    stored = tmp_path / "static" / "attachments" / company
    if decisions["mode"] == "full_history":  # both full-history sources carry an attachment
        assert [p.name for p in stored.iterdir()]
    async with maker(real_engine)() as s:
        tables = await migrations._company_tables(s)
        written = [t for t in tables if await s.scalar(
            text(f'SELECT count(*) FROM "{t}" WHERE company_id = :c'), {"c": company})]
        assert "import_batches" in written
        assert await migrations.discard(s, await s.get(MigrationRun, run.id)) == "/"
    async with maker(real_engine)() as s:
        for table in tables:
            assert await s.scalar(text(f'SELECT count(*) FROM "{table}" WHERE company_id = :c'),
                                  {"c": company}) == 0, table
        assert await s.scalar(text("SELECT count(*) FROM companies WHERE id = :c"), {"c": company}) == 0
        assert await s.scalar(text("SELECT count(*) FROM migration_entity_maps "
                                   "WHERE migration_run_id = :r"), {"r": str(run.id)}) == 0
    assert not stored.exists()


async def test_discard_keeps_attachment_files_until_commit_and_survives_a_storage_failure(
        real_engine, monkeypatch, tmp_path, caplog):
    """A discard that fails closed leaves the stored files in place; a storage backend
    that cannot delete after the commit never fails the discard and leaves a cleanup
    task for the startup sweep, logged by task id only."""
    from sqlalchemy import text

    from celerp.models.migration import MigrationRun
    from celerp.services import attachments, migrations
    from celerp.services.migrations import MigrationError
    from fixtures.manager_io.support import BASIC

    run, _ = await migrate(real_engine, BASIC.read_bytes(), "basic.manager", {"mode": "full_history"},
                           monkeypatch, tmp_path)
    company = str(run.company_id)
    stored = tmp_path / "static" / "attachments" / company
    files = sorted(p.name for p in stored.iterdir())
    assert files

    async with real_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE discard_probe_rows (company_id uuid)"))
        await conn.execute(text("INSERT INTO discard_probe_rows VALUES (:c)"), {"c": company})
    try:
        async with maker(real_engine)() as s:
            with pytest.raises(MigrationError):
                await migrations.discard(s, await s.get(MigrationRun, run.id))
    finally:
        async with real_engine.begin() as conn:
            await conn.execute(text("DROP TABLE discard_probe_rows"))
    assert sorted(p.name for p in stored.iterdir()) == files

    async def refuse(self, company_id):
        raise OSError("device busy")

    monkeypatch.setattr(attachments.LocalBackend, "delete_company", refuse)
    caplog.set_level(logging.WARNING, logger="celerp.services.migrations")
    async with maker(real_engine)() as s:
        assert await migrations.discard(s, await s.get(MigrationRun, run.id)) == "/"
    assert await count(real_engine, "companies", "id = :c", c=company) == 0
    async with maker(real_engine)() as s:
        task_id = await s.scalar(text("SELECT id FROM migration_cleanup_tasks WHERE company_id = :c"), {"c": company})
    assert task_id is not None
    warnings = [r.getMessage() for r in caplog.records if r.name == "celerp.services.migrations"]
    assert any(str(task_id) in m and "kept for a retry" in m for m in warnings), warnings
    assert all(company not in m for m in warnings)

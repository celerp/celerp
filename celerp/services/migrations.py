# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company migrations: decisions, durable runs, the resumable runner, verification,
finish and discard.

The ``migration_runs`` row is the authoritative state. A run moves only along
``LEGAL_TRANSITIONS``; the runner holds a Postgres session advisory lock keyed to
the run for as long as it works, and every import batch is one transaction that
takes ``lock_company`` before the sink writes, records the entity mappings the
sink returned, and checkpoints the phase cursor and heartbeat. A failed batch
rolls back whole, so a resume continues from the last committed cursor and the
sinks' deterministic ids and idempotency keys make a replayed batch a no-op.

Financial write invariant: each source transaction reaches Celerp through exactly
one CIF group, so it is written once, either as a native document or settlement
or as a journal fallback. The runner records which representation each mapped
record took, from the coverage plan, in the entity map metadata.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.importers.adapters.base import Artifact, MigrationDecisions, ScanError, SourceAdapter
from celerp.importers.adapters.registry import get_adapter
from celerp.importers.schema import (
    CIF_VERSION,
    CIFImportManifest,
    CIFMode,
    CoverageClass,
    ReconciliationExpectation,
    ToleranceKind,
)
from celerp.importers.sinks import SINK_MODULES, MigrationSink, MissingSinkError, SinkContext, sink_for
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.migration import (
    PHASE_ORDER,
    MigrationEntityMap,
    MigrationPhase,
    MigrationRun,
    MigrationStatus,
    can_transition,
)
from celerp.services import migration_scan_store as store
from celerp.services.company_lock import lock_company
from celerp.services.csv_export import csv_safe
from celerp.services.provisioning import add_missing_required_defaults

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
STALE_AFTER = timedelta(seconds=120)

NOT_FOUND = "Migration not found."
ALREADY_RUNNING = "Migration is already running."
NO_UNFINISHED = "This company has no unfinished migration to discard."
NOTHING_TO_MIGRATE = "The source file contains no records to migrate."

_S = MigrationStatus
_P = MigrationPhase

PHASE_LABELS: dict[MigrationPhase, str] = {
    _P.COMPANY_SETTINGS: "Company and settings",
    _P.CURRENCIES_TAXES_ACCOUNTS: "Accounting masters",
    _P.CONTACTS_LOCATIONS: "Contacts and locations",
    _P.INVENTORY_MASTERS: "Inventory",
    _P.OPERATIONAL_DOCUMENTS: "Documents",
    _P.SETTLEMENTS: "Settlements",
    _P.INVENTORY_OPENING_ADJUSTMENTS: "Inventory openings",
    _P.RESIDUAL_JOURNALS_OR_CUTOVER_OPENING: "Residual journals and opening balances",
    _P.ATTACHMENTS: "Attachments",
    _P.RECONCILIATION: "Reconciliation",
    _P.READY_TO_FINALIZE: "Ready to finish",
}

# The CIF bundle groups each import phase writes, in write order.
PHASE_GROUPS: dict[MigrationPhase, tuple[str, ...]] = {
    _P.COMPANY_SETTINGS: ("company",),
    _P.CURRENCIES_TAXES_ACCOUNTS: ("currencies", "exchange_rates", "accounts", "tax_codes"),
    _P.CONTACTS_LOCATIONS: ("locations", "contacts"),
    _P.INVENTORY_MASTERS: ("items",),
    _P.OPERATIONAL_DOCUMENTS: ("documents",),
    _P.SETTLEMENTS: ("settlements", "bank_transfers"),
    _P.INVENTORY_OPENING_ADJUSTMENTS: ("inventory_adjustments",),
    _P.RESIDUAL_JOURNALS_OR_CUTOVER_OPENING: ("journals",),
    _P.ATTACHMENTS: ("attachments",),
}
IMPORT_PHASES: tuple[MigrationPhase, ...] = tuple(PHASE_GROUPS)

# Tables a staged migration company may hold rows in, in safe delete order. A
# company row in any other table means discard cannot prove the graph complete.
_DISCARD_ORDER = ("ledger", "projections", "bank_accounts", "accounts", "locations",
                  "migration_runs", "user_companies")

_BLOCKS_FULL_HISTORY = (CoverageClass.UNCLASSIFIED, CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER)
_BLOCKS_CUTOVER = (CoverageClass.UNCLASSIFIED,)
_BLOCKER_REASONS = {
    CoverageClass.UNCLASSIFIED: "Celerp does not recognise this record type yet.",
    CoverageClass.UNSUPPORTED_FINANCIAL_BLOCKER: "Celerp cannot import these records yet.",
}
_MODE_LABELS = {CIFMode.FULL_HISTORY: "Full history", CIFMode.CUTOVER: "Cutover"}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PREPARED_BY_MAX = 200


class MigrationError(HTTPException):
    """A migration request that cannot be served; the detail is shown to the user."""


# ── Decisions ────────────────────────────────────────────────────────────────

def _blockers(coverage, mode: CIFMode | None = None) -> list:
    classes = _BLOCKS_CUTOVER if mode == CIFMode.CUTOVER else _BLOCKS_FULL_HISTORY
    return [c for c in coverage if c.coverage_class in classes]


def validate_prepared_by(value) -> str | None:
    """Prepared by is optional free text for the reconciliation pack: one line, at most 200 characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MigrationError(422, {"prepared_by": "Enter a name for Prepared by."})
    value = value.strip()
    if not value:
        return None
    if len(value) > _PREPARED_BY_MAX:
        raise MigrationError(422, {"prepared_by": f"Prepared by must be at most {_PREPARED_BY_MAX} characters."})
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise MigrationError(422, {"prepared_by": "Prepared by must be a single line of text."})
    return value


def _cutover_date(scan: store.ScanSession, value) -> date:
    if value is None or value == "":
        raise ValueError("Choose a cutover date.")
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("Enter the cutover date as YYYY-MM-DD.")
    try:
        chosen = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter the cutover date as YYYY-MM-DD.") from None
    start, end = scan.scan.period_start, scan.scan.period_end
    if start is not None and chosen < start:
        raise ValueError(f"The cutover date must be on or after {start.isoformat()}, where the source's records begin.")
    if end is not None and chosen > end:
        raise ValueError(f"The cutover date must be on or before {end.isoformat()}, where the source's records end.")
    return chosen


def _mappings(scan: store.ScanSession, value, errors: dict) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        errors["mappings"] = "Mappings must be a set of answers keyed by question."
        return {}
    questions = {q.key: q for q in scan.scan.questions}
    for key in value:
        if key not in questions:
            errors[str(key)] = "This question is not part of this source."
    chosen: dict[str, str] = {}
    by_record: dict[tuple[str, str], str] = {}
    for key, question in questions.items():
        answer = value.get(key, question.suggested)
        if answer is None:
            errors[key] = f"Choose an answer for {question.label}."
            continue
        if not isinstance(answer, str) or answer not in question.options:
            errors[key] = f"Choose one of the listed answers for {question.label}."
            continue
        record = (question.source_type, question.source_external_id)
        if by_record.setdefault(record, answer) != answer:
            errors[key] = f"{question.label} needs the same answer as the other question about this record."
            continue
        chosen[key] = answer
    return chosen


def validate_decisions(scan: store.ScanSession, body: dict) -> MigrationDecisions:
    """Validate a decision set against its scan; every problem is returned at once, keyed by field."""
    errors: dict[str, str] = {}
    try:
        mode = CIFMode(body.get("mode"))
    except ValueError:
        mode = None
        errors["mode"] = "Choose Full history or Cutover."
    if mode is not None:
        blocked = _blockers(scan.scan.coverage, mode)
        if blocked:
            listed = ", ".join(f"{c.source_type} ({c.count})" for c in blocked)
            errors["mode"] = f"{_MODE_LABELS[mode]} cannot run while the source has records Celerp cannot import: {listed}."
    cutover = None
    if mode == CIFMode.CUTOVER:
        try:
            cutover = _cutover_date(scan, body.get("cutover_date"))
        except ValueError as exc:
            errors["cutover_date"] = str(exc)
    mappings = _mappings(scan, body.get("mappings"), errors)
    prepared_by = None
    try:
        prepared_by = validate_prepared_by(body.get("prepared_by"))
    except MigrationError as exc:
        errors.update(exc.detail)
    decisions = MigrationDecisions(mode=mode, cutover_date=cutover, mappings=mappings, prepared_by=prepared_by)
    if mode == CIFMode.CUTOVER and not errors:
        # Dry run: the source decides whether it can carry its books at this date.
        adapter = _adapter(scan.adapter_key)
        try:
            adapter.build_manifest(scan.artifacts, decisions)
        except ScanError as exc:
            errors["cutover_date"] = str(exc)
    if errors:
        raise MigrationError(422, errors)
    return decisions


def prepare_start(scan: store.ScanSession) -> MigrationDecisions:
    """Re-validate the saved decisions and refuse an empty source, before anything is created."""
    if scan.decisions is None:
        raise MigrationError(422, {"mode": "Choose how to move the books before starting."})
    decisions = validate_decisions(scan, store.decisions_json(scan.decisions))
    if sum(scan.scan.object_counts.values()) == 0:
        raise MigrationError(422, NOTHING_TO_MIGRATE)
    return decisions


def scan_view(scan: store.ScanSession) -> dict:
    s = scan.scan
    adapter = get_adapter(scan.adapter_key)
    return {
        "source_system": s.source_system,
        "display_name": adapter.display_name if adapter else s.source_system,
        "file_name": ", ".join(a.original_name for a in scan.artifacts),
        "size_bytes": sum(a.size_bytes for a in scan.artifacts),
        "sha256_short": scan.artifacts[0].sha256[:12],
        "source_schema_version": s.source_schema_version,
        "company_name": s.company_name,
        "base_currency": s.base_currency,
        "period_start": s.period_start.isoformat() if s.period_start else None,
        "period_end": s.period_end.isoformat() if s.period_end else None,
        "currencies": list(s.currencies),
        "object_counts": dict(s.object_counts),
        "features": list(s.features),
        "coverage": [c.model_dump(mode="json") for c in s.coverage],
        "blockers": [{"source_type": c.source_type, "count": c.count,
                      "reason": c.note or _BLOCKER_REASONS[c.coverage_class]} for c in _blockers(s.coverage)],
        "warnings": [f"{c.source_type} ({c.count}): {c.note or 'not imported'}" for c in s.coverage
                     if c.coverage_class == CoverageClass.UNSUPPORTED_NONFINANCIAL],
        "questions": [{"key": q.key, "source_type": q.source_type, "source_external_id": q.source_external_id,
                       "label": q.label, "options": list(q.options), "suggested": q.suggested}
                      for q in s.questions],
        "decisions": store.decisions_json(scan.decisions) if scan.decisions else None,
        "expires_at": scan.expires_at.isoformat(),
    }


# ── Runs ─────────────────────────────────────────────────────────────────────

def _adapter(key: str) -> SourceAdapter:
    adapter = get_adapter(key)
    if adapter is None:
        raise ScanError("This source is not available.")
    return adapter


@lru_cache(maxsize=1)
def _sample_sha256() -> str | None:
    from celerp.importers.sample import SAMPLE_ARTIFACT
    try:
        return hashlib.sha256(SAMPLE_ARTIFACT.read_bytes()).hexdigest()
    except OSError:
        return None


def _lock_key(run_id: uuid.UUID) -> str:
    return f"migration:{run_id}"


async def _try_xact_lock(session: AsyncSession, run_id: uuid.UUID) -> bool:
    return bool(await session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": _lock_key(run_id)}))


def _illegal(action: str, run: MigrationRun) -> MigrationError:
    return MigrationError(409, f"Cannot {action} a migration that is {run.status}.")


async def create_run(session: AsyncSession, *, company: Company, user: User, scan: store.ScanSession,
                     decisions: MigrationDecisions) -> MigrationRun:
    """Claim the scan's files for a new run of *company*. Does not commit or start it."""
    adapter = _adapter(scan.adapter_key)
    run_id = uuid.uuid4()
    artifacts = store.claim_for_run(scan.token, owner=scan.owner, run_id=run_id)
    try:
        first = artifacts[0]
        run = MigrationRun(
            id=run_id, company_id=company.id, created_by_user_id=user.id,
            source_system=adapter.key, source_artifact_name=first.original_name,
            prepared_by=decisions.prepared_by, source_artifact_sha256=first.sha256,
            source_schema_version=scan.scan.source_schema_version, adapter_version=adapter.adapter_version,
            cif_version=CIF_VERSION, mode=str(decisions.mode), status=_S.READY.value, phase_state={},
            coverage={"entries": [c.model_dump(mode="json") for c in scan.scan.coverage]},
            mapping_decisions=store.decisions_json(decisions),
            source_summary={
                "artifacts": [{"name": a.path.name, "original_name": a.original_name,
                               "size_bytes": a.size_bytes, "sha256": a.sha256} for a in artifacts],
                "bootstrap": scan.owner[0] == "bootstrap",
                "sample": len(artifacts) == 1 and first.sha256 == _sample_sha256(),
            },
            reconciliation={}, error_summary={},
        )
        session.add(run)
        await session.flush()
    except BaseException:
        remove_source(run_id)
        raise
    return run


def remove_source(run_id: uuid.UUID) -> None:
    """Best-effort removal of a run's source files; a failure is logged."""
    try:
        store.remove_run_dir(run_id)
    except OSError:
        logger.warning("Migration source for run %s could not be removed", run_id, exc_info=True)


async def get_run_for_company(session: AsyncSession, run_id: uuid.UUID, company_id: uuid.UUID) -> MigrationRun:
    run = await session.scalar(
        select(MigrationRun).where(MigrationRun.id == run_id, MigrationRun.company_id == company_id))
    if run is None:
        raise MigrationError(404, NOT_FOUND)
    return run


async def _lock_run(session: AsyncSession, run: MigrationRun, *, company: bool = True) -> None:
    """Canonical lock order: the company first, then the run row."""
    if company:
        await lock_company(session, run.company_id)
    await session.refresh(run, with_for_update=True)


async def request_start(session: AsyncSession, run: MigrationRun) -> None:
    """Persist the intent to start or resume. The caller commits, then schedules the runner."""
    await _lock_run(session, run)
    if not can_transition(_S(run.status), _S.RUNNING):
        raise _illegal("start", run)
    if run.status != _S.READY and not store.run_dir(run.id).exists():
        raise MigrationError(409, "The source file for this migration has been deleted. Discard it and start again.")
    if not await _try_xact_lock(session, run.id):
        raise MigrationError(409, ALREADY_RUNNING)
    now = datetime.now(timezone.utc)
    if run.status == _S.READY_TO_FINALIZE:
        run.phase_state = {}  # a re-run replays every phase; replayed records are skipped
    run.status = _S.RUNNING.value
    run.started_at = now
    run.heartbeat_at = now
    run.cancel_requested_at = None
    run.error_summary = {}
    await session.flush()


async def request_cancel(session: AsyncSession, run: MigrationRun) -> None:
    """Persist a cancel request; the runner stops between batches. Commits.

    Only the run row is locked: the runner holds the company lock during a batch."""
    await _lock_run(session, run, company=False)
    if not can_transition(_S(run.status), _S.CANCEL_REQUESTED):
        raise _illegal("cancel", run)
    run.status = _S.CANCEL_REQUESTED.value
    run.cancel_requested_at = datetime.now(timezone.utc)
    await session.commit()


_TASKS: set[asyncio.Task] = set()


def schedule_run(run_id: uuid.UUID) -> None:
    """Start the runner in this process. The run row, not the task, is authoritative."""
    task = asyncio.get_running_loop().create_task(run_migration(run_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _source(run: MigrationRun) -> tuple[SourceAdapter, list[Artifact], MigrationDecisions]:
    directory = store.run_dir(run.id)
    artifacts = [Artifact(directory / a["name"], a["original_name"], a["size_bytes"], a["sha256"])
                 for a in run.source_summary.get("artifacts", [])]
    if not artifacts or not all(a.path.is_file() for a in artifacts):
        raise ScanError("The source file for this migration has been deleted.")
    return _adapter(run.source_system), artifacts, store.decisions_from_json(run.mapping_decisions)


# ── Runner ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Step:
    group: str
    sink: MigrationSink
    record: object


def _phase_steps(manifest: CIFImportManifest) -> dict[MigrationPhase, list[_Step]]:
    """Every record per phase, in write order; raises MissingSinkError before any write."""
    bundle = manifest.bundle
    steps: dict[MigrationPhase, list[_Step]] = {}
    for phase, groups in PHASE_GROUPS.items():
        steps[phase] = []
        for group in groups:
            records = ([bundle.company] if bundle.company else []) if group == "company" else getattr(bundle, group)
            if records:
                sink = sink_for(group)
                steps[phase].extend(_Step(group, sink, r) for r in records)
    return steps


def _next_batch(steps: list[_Step], cursor: int) -> list[_Step]:
    first = steps[cursor]
    batch = [first]
    for step in steps[cursor + 1:]:
        if len(batch) >= first.sink.batch_size or step.group != first.group:
            break
        batch.append(step)
    return batch


def _representation(group: str, source_type: str, targets: dict[str, str | None]) -> str:
    """A journal carrying a transaction whose coverage target is not a journal is a fallback."""
    if group == "journals" and targets.get(source_type) != "journal":
        return "journal_fallback"
    return "native"


def _maker():
    import celerp.db  # resolved per call: the engine is replaced in tests and on reconfigure
    return lambda: AsyncSession(bind=celerp.db.engine, expire_on_commit=False)


def _phase_entry(state: dict, phase: MigrationPhase) -> dict:
    return {"status": "pending", "cursor": 0, "created": 0, "skipped": 0, "errors": 0, **state.get(phase.value, {})}


async def _checkpoint(session: AsyncSession, run_id: uuid.UUID, phase: MigrationPhase, state: dict) -> str:
    """Persist progress (phase state, current phase, heartbeat only) and return the live status."""
    return (await session.execute(
        update(MigrationRun).where(MigrationRun.id == run_id)
        .values(phase_state=state, current_phase=phase.value, heartbeat_at=datetime.now(timezone.utc))
        .returning(MigrationRun.status)
    )).scalar_one()


async def _set_status(session: AsyncSession, run: MigrationRun, new: MigrationStatus) -> bool:
    if not can_transition(_S(run.status), new):
        return False
    run.status = new.value
    run.heartbeat_at = datetime.now(timezone.utc)
    return True


async def _stop_if_cancelled(maker, run_id: uuid.UUID, status: str) -> bool:
    """True when the runner must stop: cancel requested (now cancelled) or no longer running."""
    if status == _S.RUNNING:
        return False
    if status == _S.CANCEL_REQUESTED:
        async with maker() as s:
            run = await s.get(MigrationRun, run_id, with_for_update=True)
            await _set_status(s, run, _S.CANCELLED)
            await s.commit()
    return True


async def _fail(maker, run_id: uuid.UUID, phase: MigrationPhase, cursor: int, exc: Exception) -> None:
    async with maker() as s:
        run = await s.get(MigrationRun, run_id, with_for_update=True)
        state = dict(run.phase_state)
        state[phase.value] = {**_phase_entry(state, phase), "status": "failed", "cursor": cursor}
        run.phase_state = state
        run.current_phase = phase.value
        run.error_summary = {"phase": phase.value, "batch_cursor": cursor,
                             "error_class": type(exc).__name__, "message": str(exc)}
        await _set_status(s, run, _S.FAILED)
        await s.commit()


async def run_migration(run_id: uuid.UUID) -> None:
    """Run or resume one migration to ready_to_finalize, failed or cancelled.

    A second runner for the same run cannot take the session lock and returns at once
    without writing anything."""
    import celerp.db

    async with celerp.db.engine.connect() as holder:
        locked = await holder.scalar(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": _lock_key(run_id)})
        await holder.commit()
        if not locked:
            return
        try:
            await _run_locked(run_id)
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": _lock_key(run_id)})
            await holder.commit()


async def _run_locked(run_id: uuid.UUID) -> None:
    maker = _maker()
    async with maker() as s:
        run = await s.get(MigrationRun, run_id)
        if run is None or await _stop_if_cancelled(maker, run_id, run.status):
            return
        company_id, user_id, state = run.company_id, run.created_by_user_id, dict(run.phase_state)
        first_pending = next((p for p in IMPORT_PHASES if _phase_entry(state, p)["status"] != "done"), None)
        try:
            adapter, artifacts, decisions = _source(run)
            manifest = await asyncio.to_thread(adapter.build_manifest, artifacts, decisions)
            steps = _phase_steps(manifest)
        except Exception as exc:  # a missing source, adapter or module sink: nothing was written
            await _fail(maker, run_id, first_pending or _P.RECONCILIATION, 0, exc)
            return
    targets: dict[str, str | None] = {}
    for entry in manifest.coverage:
        targets.setdefault(entry.source_type, entry.target)

    for phase in IMPORT_PHASES:
        entry = _phase_entry(state, phase)
        if entry["status"] == "done":
            continue
        phase_steps = steps[phase]
        cursor = entry["cursor"]
        if cursor >= len(phase_steps):
            state[phase.value] = {**entry, "status": "done"}
            async with maker() as s:
                status = await _checkpoint(s, run_id, phase, state)
                await s.commit()
            if await _stop_if_cancelled(maker, run_id, status):
                return
            continue
        while cursor < len(phase_steps):
            batch = _next_batch(phase_steps, cursor)
            try:
                async with maker() as s:
                    await lock_company(s, company_id)
                    context = SinkContext(session=s, company_id=company_id, user_id=user_id, run_id=run_id)
                    result = await batch[0].sink.import_batch(context, [b.record for b in batch])
                    await _record_mappings(s, run_id, batch[0].group, result.mappings, targets)
                    for error in result.errors:
                        logger.warning("Migration %s rejected %s %s", run_id, error.source_type,
                                       error.source_external_id)
                    cursor += len(batch)
                    entry = {**entry, "cursor": cursor, "created": entry["created"] + result.created,
                             "skipped": entry["skipped"] + result.skipped,
                             "errors": entry["errors"] + len(result.errors),
                             "status": "done" if cursor >= len(phase_steps) else "running"}
                    state[phase.value] = entry
                    status = await _checkpoint(s, run_id, phase, state)
                    await s.commit()
            except Exception as exc:
                await _fail(maker, run_id, phase, entry["cursor"], exc)
                return
            if await _stop_if_cancelled(maker, run_id, status):
                return

    await _reconcile_run(maker, run_id, state)


async def _record_mappings(session: AsyncSession, run_id: uuid.UUID, group: str, mappings,
                           targets: dict[str, str | None]) -> None:
    """Persist every mapping a batch returned, created or skipped; a replay adds nothing."""
    if not mappings:
        return
    table = MigrationEntityMap.__table__
    await session.execute(pg_insert(table).values([{
        "id": uuid.uuid4(), "migration_run_id": run_id, "source_type": m.source_type,
        "source_external_id": m.source_external_id, "target_entity_type": m.target_entity_type,
        "target_entity_id": m.target_entity_id, "status": m.status,
        "metadata": {"group": group, "representation": _representation(group, m.source_type, targets)},
    } for m in mappings]).on_conflict_do_nothing(constraint="uq_migration_entity_map_source"))


# ── Reconciliation ───────────────────────────────────────────────────────────

def _registered_sinks() -> list[MigrationSink]:
    sinks: dict[str, MigrationSink] = {}
    for group in SINK_MODULES:
        try:
            sink = sink_for(group)
        except MissingSinkError:
            continue
        sinks.setdefault(sink.key, sink)
    return list(sinks.values())


def _rule(expectation: ReconciliationExpectation) -> tuple[str, Decimal]:
    tol = expectation.tolerance
    if tol.kind == ToleranceKind.EXACT:
        return "exact", Decimal(0)
    allowance = Decimal(tol.max_units).scaleb(-tol.precision)
    return f"within {allowance} {tol.currency}", allowance


def _row(expectation: ReconciliationExpectation, actual: Decimal | None) -> dict:
    rule, allowance = _rule(expectation)
    row = {"check": str(expectation.measure), "key": expectation.key, "currency": expectation.currency,
           "source": str(expectation.expected), "celerp": None, "difference": None, "rule": rule, "result": "fail"}
    if actual is None:
        if expectation.expected == 0:
            row.update(celerp="0", difference="0", result="n-a",
                       rule="No figure in the source and none in Celerp")
        else:
            row["rule"] = f"{rule}; Celerp cannot measure this figure"
        return row
    difference = actual - expectation.expected
    row.update(celerp=str(actual), difference=str(difference))
    if difference == 0:
        row["result"] = "pass"
    elif abs(difference) <= allowance:
        row["result"] = "rounding"
    return row


async def _verification(session: AsyncSession, run: MigrationRun) -> dict:
    """Compare the source's own figures with what Celerp now holds. Raises on a provider failure."""
    adapter, artifacts, decisions = _source(run)
    expectations = await asyncio.to_thread(adapter.source_expectations, artifacts, decisions)
    context = SinkContext(session=session, company_id=run.company_id, user_id=run.created_by_user_id, run_id=run.id)
    measured: dict[tuple, Decimal] = {}
    for sink in _registered_sinks():
        for m in await sink.reconcile(context, expectations):
            measured[(str(m.measure), m.key, m.currency)] = m.actual
    rows = [_row(e, measured.get((str(e.measure), e.key, e.currency))) for e in expectations.expectations]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
            "blockers": sum(1 for r in rows if r["result"] == "fail")}


async def _reconcile_run(maker, run_id: uuid.UUID, state: dict) -> None:
    async with maker() as s:
        run = await s.get(MigrationRun, run_id, with_for_update=True)
        if run.status != _S.RUNNING:
            if run.status == _S.CANCEL_REQUESTED:
                await _set_status(s, run, _S.CANCELLED)
                await s.commit()
            return
        state[_P.RECONCILIATION.value] = {**_phase_entry(state, _P.RECONCILIATION), "status": "running"}
        run.phase_state = dict(state)
        run.current_phase = _P.RECONCILIATION.value
        await _set_status(s, run, _S.RECONCILING)
        await s.commit()
    async with maker() as s:
        run = await s.get(MigrationRun, run_id)
        try:
            report = await _verification(s, run)
        except Exception as exc:
            await s.rollback()
            run = await s.get(MigrationRun, run_id, with_for_update=True, populate_existing=True)
            run.reconciliation = {}
            await s.commit()
            await _fail(maker, run_id, _P.RECONCILIATION, 0, exc)
            return
        await s.rollback()  # measurement only; nothing it read is kept locked
        run = await s.get(MigrationRun, run_id, with_for_update=True, populate_existing=True)
        passed = report["blockers"] == 0
        state[_P.RECONCILIATION.value] = {**_phase_entry(state, _P.RECONCILIATION),
                                          "status": "done" if passed else "failed"}
        if passed:
            state[_P.READY_TO_FINALIZE.value] = {**_phase_entry(state, _P.READY_TO_FINALIZE), "status": "done"}
        run.phase_state = dict(state)
        run.reconciliation = report
        run.current_phase = (_P.READY_TO_FINALIZE if passed else _P.RECONCILIATION).value
        if not passed:
            run.error_summary = {"phase": _P.RECONCILIATION.value, "batch_cursor": 0, "error_class": "Mismatch",
                                 "message": f"{report['blockers']} verification check(s) do not match the source."}
        await _set_status(s, run, _S.READY_TO_FINALIZE if passed else _S.FAILED)
        await s.commit()


# ── Finish, discard and housekeeping ─────────────────────────────────────────

async def finalize(session: AsyncSession, run: MigrationRun) -> MigrationRun:
    """Re-check verification under the company lock, then activate the company in one commit."""
    await _lock_run(session, run)
    if run.status != _S.READY_TO_FINALIZE:
        raise _illegal("finalize", run)
    run_id = run.id  # read before a rollback expires the row
    try:
        report = await _verification(session, run)
        if report["blockers"]:
            run.reconciliation = report
            await session.commit()
            raise MigrationError(409, "Verification no longer matches the source. Resume the migration to re-run it.")
        company = await session.get(Company, run.company_id)
        await add_missing_required_defaults(session, run.company_id)
        company.is_active = True
        run.reconciliation = report
        run.status = _S.COMPLETED.value
        run.current_phase = _P.READY_TO_FINALIZE.value
        run.completed_at = datetime.now(timezone.utc)
        await session.commit()
    except MigrationError:
        raise
    except Exception:
        await session.rollback()
        logger.exception("Migration %s could not be finalized", run_id)
        raise MigrationError(500, "Could not finish the migration. Nothing was changed.") from None
    await _cleanup_source(session, run)
    return run


async def _cleanup_source(session: AsyncSession, run: MigrationRun) -> bool:
    """Delete a run's source files; a failure is recorded on the run for a later retry."""
    summary = dict(run.source_summary)
    try:
        store.remove_run_dir(run.id)
    except OSError as exc:
        summary["source_cleanup"] = f"The source file could not be deleted: {exc}"
        removed = False
    else:
        summary.pop("source_cleanup", None)
        removed = True
    if summary != run.source_summary:
        run.source_summary = summary
        await session.commit()
    return removed


async def _company_tables(session: AsyncSession) -> list[str]:
    return list((await session.scalars(text(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = current_schema() AND c.column_name = 'company_id' AND t.table_type = 'BASE TABLE' "
        "ORDER BY c.table_name"))).all())


async def discard(session: AsyncSession, run: MigrationRun) -> str:
    """Delete a staged company, its runs and their files; returns where the user goes next."""
    await _lock_run(session, run)
    company = await session.get(Company, run.company_id)
    if run.status == _S.COMPLETED or company.is_active:
        raise MigrationError(409, NO_UNFINISHED)
    if not await _try_xact_lock(session, run.id):
        raise MigrationError(409, ALREADY_RUNNING)
    for table in await _company_tables(session):
        if table in _DISCARD_ORDER:
            continue
        held = await session.scalar(text(f'SELECT 1 FROM "{table}" WHERE company_id = :c LIMIT 1'),
                                    {"c": str(company.id)})
        if held:
            raise MigrationError(409, f"This company has data in {table} that discard cannot remove safely. "
                                      "Nothing was deleted.")
    run_ids = list((await session.scalars(
        select(MigrationRun.id).where(MigrationRun.company_id == company.id))).all())
    owner_id = run.created_by_user_id
    bootstrap = bool(run.source_summary.get("bootstrap"))
    for table in _DISCARD_ORDER:
        await session.execute(text(f'DELETE FROM "{table}" WHERE company_id = :c'), {"c": str(company.id)})
    await session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": str(company.id)})
    redirect = "/"
    if bootstrap:
        others = await session.scalar(select(UserCompany.id).where(UserCompany.user_id == owner_id).limit(1))
        if others is None:
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": str(owner_id)})
            redirect = "/setup"
    await session.commit()
    session.expunge_all()
    for run_id in run_ids:
        remove_source(run_id)
    return redirect


def _retention_start(run: MigrationRun) -> datetime:
    return run.heartbeat_at or run.created_at


async def run_view(session: AsyncSession, run: MigrationRun) -> dict:
    company = await session.get(Company, run.company_id)
    state = run.phase_state or {}
    retained = run.status in (_S.FAILED, _S.CANCELLED, _S.INTERRUPTED)
    return {
        "id": str(run.id),
        "company_id": str(run.company_id),
        "company_name": company.name,
        "source_system": run.source_system,
        "mode": run.mode,
        "cutover_date": (run.mapping_decisions or {}).get("cutover_date"),
        "status": run.status,
        "current_phase": run.current_phase,
        "phases": [{"phase": p.value, "label": PHASE_LABELS[p],
                    **{k: _phase_entry(state, p)[k] for k in ("status", "created", "skipped", "errors")}}
                   for p in PHASE_ORDER],
        "coverage": (run.coverage or {}).get("entries", []),
        "error_summary": run.error_summary or {},
        "retention_until": (_retention_start(run) + timedelta(days=RETENTION_DAYS)).isoformat() if retained else None,
        "source_deleted": not store.run_dir(run.id).exists(),
        "prepared_by": run.prepared_by,
        "is_bootstrap_run": bool(run.source_summary.get("bootstrap")),
        "is_sample": bool(run.source_summary.get("sample")),
    }


async def purge_run_sources(session: AsyncSession) -> int:
    """Delete source files of finished runs and of stopped runs past retention. Returns how many."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    runs = (await session.scalars(select(MigrationRun).where(MigrationRun.status.in_(
        [_S.COMPLETED.value, _S.FAILED.value, _S.CANCELLED.value, _S.INTERRUPTED.value])))).all()
    removed = 0
    for run in runs:
        due = run.status == _S.COMPLETED or _retention_start(run) < cutoff
        if due and (store.run_dir(run.id).exists() or "source_cleanup" in run.source_summary):
            removed += await _cleanup_source(session, run)
    return removed


async def mark_stale_runs_interrupted(session: AsyncSession, company_id: uuid.UUID | None = None) -> int:
    """Persist as interrupted every active run whose heartbeat is stale and whose runner is gone."""
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    query = select(MigrationRun).where(
        MigrationRun.status.in_([_S.RUNNING.value, _S.CANCEL_REQUESTED.value, _S.RECONCILING.value]),
        MigrationRun.heartbeat_at < cutoff,
    ).with_for_update(skip_locked=True)
    if company_id is not None:
        query = query.where(MigrationRun.company_id == company_id)
    marked = 0
    for run in (await session.scalars(query)).all():
        if await _try_xact_lock(session, run.id):
            run.status = _S.INTERRUPTED.value
            marked += 1
    await session.commit()
    return marked


def reconciliation_pack_csv(run: MigrationRun) -> str:
    """The stored verification report as CSV, with the run's identity above the rows."""
    adapter = get_adapter(run.source_system)
    decisions = run.mapping_decisions or {}
    report = run.reconciliation
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Only the preparer's name is user-authored; "--" is Celerp's own empty marker, not a formula.
    for label, value in (
        ("Run id", str(run.id)),
        ("Source", adapter.display_name if adapter else run.source_system),
        ("Mode", _MODE_LABELS[CIFMode(run.mode)]),
        ("Cutover date", decisions.get("cutover_date") or "--"),
        ("Source hash", run.source_artifact_sha256),
        ("Prepared by", csv_safe(run.prepared_by) if run.prepared_by else "--"),
        ("Generated at", report["generated_at"]),
    ):
        writer.writerow([label, value])
    writer.writerow([])
    writer.writerow(["Check", "Key", "Currency", "Source", "Celerp", "Difference", "Rule", "Result"])
    for row in report["rows"]:
        # Figures are Celerp-formatted decimals; only the text columns can carry a formula.
        writer.writerow([csv_safe(row["check"]), csv_safe(row["key"]), csv_safe(row["currency"] or ""),
                         row["source"], row["celerp"] if row["celerp"] is not None else "--",
                         row["difference"] if row["difference"] is not None else "--",
                         csv_safe(row["rule"]), csv_safe(row["result"])])
    return buf.getvalue()

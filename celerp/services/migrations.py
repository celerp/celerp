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
from functools import lru_cache, partial

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.importers.adapters.base import Artifact, MigrationDecisions, ScanError, SourceAdapter
from celerp.importers.adapters.registry import get_adapter
from celerp.importers.schema import (
    MIGRATION_CIF_VERSION,
    CIFImportManifest,
    CIFMode,
    CoverageClass,
    ReconciliationExpectation,
    ToleranceKind,
)
from celerp.importers.sinks import (
    SINK_MODULES,
    MigrationBatchError,
    MigrationSink,
    MissingSinkError,
    SinkContext,
    record_identities,
    sink_for,
    validate_batch_result,
)
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.models.migration import (
    PHASE_ORDER,
    MigrationCleanupTask,
    MigrationEntityMap,
    MigrationPhase,
    MigrationRun,
    MigrationStatus,
    can_transition,
)
from celerp.services import attachments
from celerp.services import migration_scan_store as store
from celerp.services.auth import normalize_role
from celerp.services.company_lock import lock_company
from celerp.services.csv_export import csv_safe
from celerp.services.permissions import role_has_permission
from celerp.services.provisioning import add_missing_required_defaults

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
STALE_AFTER = timedelta(seconds=120)

NOT_FOUND = "Migration not found."
ALREADY_RUNNING = "Migration is already running."
SCAN_ALREADY_STARTED = "This upload was already used to start a migration, or it was replaced."
NO_UNFINISHED = "This company has no unfinished migration to discard."
NOTHING_TO_MIGRATE = "The source file contains no records to migrate."
OLDER_IMPORTER = "This migration was created by an older importer version and must be restarted."

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
_DISCARD_ORDER = ("import_batches", "ledger", "projections", "bank_accounts", "accounts", "locations",
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
    return bool(await session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                                     {"k": _lock_key(run_id)}))


async def lock_scan_claim(session: AsyncSession, claim: str) -> MigrationRun | None:
    """Serialize starts from one scan until the caller's transaction ends; returns the run
    already holding the claim, if any. Take it before anything is provisioned."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"migration-scan:{claim}"})
    return await session.scalar(select(MigrationRun).where(MigrationRun.scan_claim_sha256 == claim))


def _illegal(action: str, run: MigrationRun) -> MigrationError:
    return MigrationError(409, f"Cannot {action} a migration that is {run.status}.")


async def create_run(session: AsyncSession, *, company: Company, user: User, scan: store.ScanSession,
                     decisions: MigrationDecisions) -> MigrationRun:
    """A ``preparing`` run of *company* holding the scan's claim; the files stay in the scan
    until ``claim_source``. The caller holds ``lock_scan_claim`` and commits."""
    adapter = _adapter(scan.adapter_key)
    await asyncio.to_thread(store.verify_unchanged, scan.token, owner=scan.owner)
    first = scan.artifacts[0]
    run = MigrationRun(
        company_id=company.id, created_by_user_id=user.id, scan_claim_sha256=store.scan_claim(scan.token),
        source_system=adapter.key, source_artifact_name=first.original_name,
        prepared_by=decisions.prepared_by, source_artifact_sha256=first.sha256,
        source_schema_version=scan.scan.source_schema_version, adapter_version=adapter.adapter_version,
        cif_version=MIGRATION_CIF_VERSION, mode=str(decisions.mode), status=_S.PREPARING.value, phase_state={},
        coverage={"entries": [c.model_dump(mode="json") for c in scan.scan.coverage]},
        mapping_decisions=store.decisions_json(decisions),
        source_summary={
            "artifacts": [{"name": a.path.name, "original_name": a.original_name,
                           "size_bytes": a.size_bytes, "sha256": a.sha256} for a in scan.artifacts],
            "bootstrap": scan.owner[0] == "bootstrap",
            "sample": len(scan.artifacts) == 1 and first.sha256 == _sample_sha256(),
        },
        reconciliation={}, error_summary={},
    )
    session.add(run)
    await session.flush()
    return run


async def claim_source(session: AsyncSession, run_id: uuid.UUID, *, token: str | None, start: bool) -> bool:
    """Finish a start: move the claimed scan's files into run storage, then start the run
    (*start*) or leave it ready. Commits.

    Idempotent, so a repeated start and startup recovery both finish a start that died
    part way; only a ``preparing`` run is changed. Returns whether this call started the
    run, so exactly one caller schedules the runner. A source found in neither place
    fails the run."""
    run = await session.get(MigrationRun, run_id)
    if run is None:
        return False
    await lock_scan_claim(session, run.scan_claim_sha256)
    await _lock_run(session, run)
    if run.status != _S.PREPARING:
        await session.commit()
        return False
    try:
        await asyncio.to_thread(store.claim_for_run, token, run_id=run.id,
                                stored_names=[a["name"] for a in run.source_summary["artifacts"]])
    except store.SourceMissingError as exc:
        run.status = _S.FAILED.value
        run.error_summary = {"message": exc.detail}
        await session.commit()
        return False
    run.status = _S.READY.value
    if start:
        await request_start(session, run)
    await session.commit()
    return start


async def recover_preparing_runs(session: AsyncSession) -> int:
    """Finish every start that died after its run was committed; the owner starts each
    recovered run from its progress page. Returns how many were examined."""
    rows = (await session.execute(select(MigrationRun.id, MigrationRun.scan_claim_sha256)
                                  .where(MigrationRun.status == _S.PREPARING.value))).all()
    for run_id, claim in rows:
        try:
            await claim_source(session, run_id, token=store.find_scan_token(claim), start=False)
        except Exception as exc:
            await session.rollback()
            logger.warning("Migration %s could not be recovered: %s", run_id, type(exc).__name__)
    return len(rows)


async def get_owned_migration_run(session: AsyncSession, run_id: uuid.UUID, user_id: uuid.UUID) -> MigrationRun:
    """The run, if *user_id* started it and still owns its company; otherwise not found.

    Control follows the user's own membership, never the company their current token is
    scoped to, so the owner watches and acts on a staged company from their working session."""
    row = (await session.execute(
        select(MigrationRun, UserCompany.role, Company.settings)
        .join(UserCompany, (UserCompany.company_id == MigrationRun.company_id)
              & (UserCompany.user_id == user_id) & UserCompany.is_active.is_(True))
        .join(Company, Company.id == MigrationRun.company_id)
        .where(MigrationRun.id == run_id, MigrationRun.created_by_user_id == user_id)
    )).first()
    if row is None or not role_has_permission(row.settings, normalize_role(row.role), "manage_company_lifecycle"):
        raise MigrationError(404, NOT_FOUND)
    return row[0]


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


# Coverage targets Celerp stores as journal entries natively.
_JOURNAL_TARGETS = {"journal", "bank_transfer"}


def _representation(mapping, targets: dict[str, str | None]) -> str:
    """A journal written for a record whose coverage target is another entity is a
    fallback: a settlement's other lines, or a debit note posted on its bill. A record
    with no coverage row of its own, such as the cutover opening balances, is native."""
    target = targets.get(mapping.source_type)
    if mapping.target_entity_type == "journal_entry" and target is not None and target not in _JOURNAL_TARGETS:
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


class IncompleteMigrationError(Exception):
    """Source records the manifest holds that have no durable entity mapping after import."""

    def __init__(self, missing: list[tuple[str, str]]) -> None:
        self.missing = missing
        by_type: dict[str, int] = {}
        for source_type, _ in missing:
            by_type[source_type] = by_type.get(source_type, 0) + 1
        counts = ", ".join(f"{t} ({n})" for t, n in by_type.items())
        subject = "1 source record has" if len(missing) == 1 else f"{len(missing)} source records have"
        super().__init__(f"{subject} no imported Celerp record: {counts}.")


def _failure_details(exc: Exception) -> tuple[int, dict]:
    """The failed batch's rejected-record count and the extra error summary a failure carries."""
    if isinstance(exc, MigrationBatchError):
        return len(exc.errors), {}
    if isinstance(exc, IncompleteMigrationError):
        return 0, {"missing": record_identities(exc.missing)}
    return 0, {}


async def _fail(maker, run_id: uuid.UUID, phase: MigrationPhase, cursor: int, exc: Exception) -> None:
    errors, details = _failure_details(exc)
    async with maker() as s:
        run = await s.get(MigrationRun, run_id, with_for_update=True)
        state = dict(run.phase_state)
        state[phase.value] = {**_phase_entry(state, phase), "status": "failed", "cursor": cursor, "errors": errors}
        run.phase_state = state
        run.current_phase = phase.value
        run.error_summary = {"phase": phase.value, "batch_cursor": cursor,
                             "error_class": type(exc).__name__, "message": str(exc), **details}
        await _set_status(s, run, _S.FAILED)
        await s.commit()


class IncompatibleImporterVersion(Exception):
    """The run's cursors and mappings were made by an importer whose output may differ now."""

    def __init__(self) -> None:
        super().__init__(OLDER_IMPORTER)


def _require_same_importer(run: MigrationRun, adapter: SourceAdapter) -> None:
    """A run resumes only under the adapter and migration CIF versions it started with:
    a changed importer can include, identify or order records differently."""
    if run.adapter_version != adapter.adapter_version or run.cif_version != MIGRATION_CIF_VERSION:
        raise IncompatibleImporterVersion()


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
        stopped_at = first_pending or _P.RECONCILIATION
        try:
            adapter, artifacts, decisions = _source(run)
            _require_same_importer(run, adapter)
            manifest = await asyncio.to_thread(adapter.build_manifest, artifacts, decisions)
            steps = _phase_steps(manifest)
            read_attachment = partial(adapter.read_attachment, artifacts)
        except Exception as exc:  # a missing source, adapter or module sink, or a changed importer: nothing written
            await _fail(maker, run_id, stopped_at, _phase_entry(state, stopped_at)["cursor"], exc)
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
                    context = SinkContext(session=s, company_id=company_id, user_id=user_id, run_id=run_id,
                                          read_attachment=read_attachment)
                    records = [b.record for b in batch]
                    result = await batch[0].sink.import_batch(context, records)
                    validate_batch_result(records, result)  # any rejected record rolls the batch back
                    await _record_mappings(s, run_id, batch[0].group, result.mappings, targets)
                    cursor += len(batch)
                    entry = {**entry, "cursor": cursor, "created": entry["created"] + result.created,
                             "skipped": entry["skipped"] + result.skipped, "errors": 0,
                             "status": "done" if cursor >= len(phase_steps) else "running"}
                    state[phase.value] = entry
                    status = await _checkpoint(s, run_id, phase, state)
                    await s.commit()
            except Exception as exc:
                await _fail(maker, run_id, phase, entry["cursor"], exc)
                return
            if await _stop_if_cancelled(maker, run_id, status):
                return

    await _reconcile_run(maker, run_id, state,
                         [(r.source_type, r.source_external_id) for r in manifest.bundle.source_records()])


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
        "metadata": {"group": group, "representation": _representation(m, targets)},
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
    context = SinkContext(session=session, company_id=run.company_id, user_id=run.created_by_user_id, run_id=run.id,
                          read_attachment=partial(adapter.read_attachment, artifacts))
    measured: dict[tuple, Decimal] = {}
    for sink in _registered_sinks():
        for m in await sink.reconcile(context, expectations):
            measured[(str(m.measure), m.key, m.currency)] = m.actual
    rows = [_row(e, measured.get((str(e.measure), e.key, e.currency))) for e in expectations.expectations]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
            "blockers": sum(1 for r in rows if r["result"] == "fail")}


async def _unmapped(session: AsyncSession, run_id: uuid.UUID,
                    expected: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Manifest source identities with no durable entity mapping for this run."""
    mapped = set((await session.execute(
        select(MigrationEntityMap.source_type, MigrationEntityMap.source_external_id)
        .where(MigrationEntityMap.migration_run_id == run_id)
    )).tuples())
    return [key for key in expected if key not in mapped]


async def _reconcile_run(maker, run_id: uuid.UUID, state: dict, expected: list[tuple[str, str]]) -> None:
    """Verify every manifest record has a durable mapping, then the source's own figures.

    Completeness is checked first: matching totals cannot show a lost record."""
    async with maker() as s:
        run = await s.get(MigrationRun, run_id, with_for_update=True)
        if run.status != _S.RUNNING:
            if run.status == _S.CANCEL_REQUESTED:
                await _set_status(s, run, _S.CANCELLED)
                await s.commit()
            return
        missing = await _unmapped(s, run_id, expected)
        if missing:
            run.reconciliation = {}
        else:
            state[_P.RECONCILIATION.value] = {**_phase_entry(state, _P.RECONCILIATION), "status": "running"}
            run.phase_state = dict(state)
            run.current_phase = _P.RECONCILIATION.value
            await _set_status(s, run, _S.RECONCILING)
        await s.commit()
    if missing:
        await _fail(maker, run_id, _P.RECONCILIATION, 0, IncompleteMigrationError(missing))
        return
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
        company.is_migration_staged = False
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
    """Delete a staged company and its runs, then their files; returns where the user goes next.

    The files are deleted after the commit through a cleanup task committed with the
    deletes, so a storage failure never blocks the user and is retried at startup."""
    await _lock_run(session, run)
    company = await session.get(Company, run.company_id)
    if run.status == _S.COMPLETED or not company.is_migration_staged:
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
    task = MigrationCleanupTask(company_id=company.id, run_ids=[str(r) for r in run_ids])
    session.add(task)
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
    task_id = task.id
    session.expunge_all()
    await run_cleanup_task(session, task_id)
    return redirect


async def run_cleanup_task(session: AsyncSession, task_id: uuid.UUID) -> bool:
    """Delete a discarded company's run sources and attachment files, then its cleanup
    task. Files already gone count as deleted. A failure keeps the task for the startup
    sweep and is logged by task id only; never raises. Returns whether the task is done."""
    try:
        task = await session.scalar(select(MigrationCleanupTask).where(MigrationCleanupTask.id == task_id)
                                    .with_for_update(skip_locked=True))
        if task is None:  # done, or another sweep holds it
            return True
        for run_id in task.run_ids:
            await asyncio.to_thread(store.remove_run_dir, uuid.UUID(run_id))
        await attachments.delete_company_files(str(task.company_id))
        await session.delete(task)
        await session.commit()
        return True
    except Exception as exc:
        await session.rollback()
        logger.warning("Migration cleanup task %s is kept for a retry at startup: %s", task_id, type(exc).__name__)
        return False


async def sweep_cleanup_tasks(session: AsyncSession) -> int:
    """Retry every pending cleanup task. Returns how many remain."""
    task_ids = (await session.scalars(select(MigrationCleanupTask.id))).all()
    return sum([not await run_cleanup_task(session, task_id) for task_id in task_ids])


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
        "source_deleted": run.status != _S.PREPARING and not store.run_dir(run.id).exists(),
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


async def housekeeping(session: AsyncSession) -> None:
    """Startup maintenance. Preparing runs claim their scans before expired scans are
    purged, so a start that died just before its files moved never loses them."""
    await recover_preparing_runs(session)
    store.purge_expired()
    await mark_stale_runs_interrupted(session)
    await purge_run_sources(session)
    await sweep_cleanup_tasks(session)


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

# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Fetch the files attached to a Xero migration's documents and attach them in Celerp.

A Xero export set carries no attachments. After the import, the owner signs in to Xero
once through Celerp's read-only Xero app on the relay, sees which organisation the sign-in
chose and starts the fetch. Celerp then lists the invoices, bills and credit notes that
have files, finds each one among the documents the migration imported by the key the
export set gave it, and attaches its files only when Xero's date, currency, total and
contact are the document's imported values. Documents that were not imported or differ
are listed with their file count and reason, never downloaded. Once files came from one
organisation, a sign-in to another is refused.

The fetch is resumable: the listing cursor is saved after each page and every file is
attached under an idempotency key from its Xero id, so a retry, a restart or fetching
again never attaches a file twice. A Xero limit pauses the fetch until it resets.
Finishing or stopping closes the Xero access at the relay."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import find_event_by_idempotency
from celerp.models.ledger import LedgerEntry
from celerp.models.migration import (
    MigrationAttachmentFetch,
    MigrationCleanupTask,
    MigrationEntityMap,
    MigrationRun,
    MigrationStatus,
)
from celerp.models.projections import Projection
from celerp.notifications import service as notification_service
from celerp.services import cloud_entitlement
from celerp.services.attachments import FILE_ATTACHED_EVENTS, attach_file, store_file
from celerp.services.company_handoff_sender import cloud_json
from celerp.services.company_lock import lock_company
from celerp.services.migration_core_sink import stage_file_cleanup
from celerp.services.money import round_money
from celerp.services.migrations import _try_xact_lock
from ui.i18n import t

logger = logging.getLogger(__name__)

KINDS = ("Invoices", "CreditNotes")
PAGE_SIZE = 100  # Xero's page size for invoices and credit notes
LIVE = ("signing_in", "fetching", "waiting")
OPEN = (*LIVE, "confirming")  # holds Xero access at the relay; "confirming" waits for the owner
LOOP_S = 60
RETRY_S = 300
DOWNLOAD_S = 120
SALES_TYPES = ("ACCREC", "ACCRECCREDIT")


def document_keys(kind: str, doc: dict) -> list[str]:
    """The keys the Xero export set gave this document, most specific first: ``S:`` for
    sales and ``P:`` for purchases, then its number, with the contact appended when the
    number was not unique. A document without a number was exported without one and
    cannot be matched."""
    number = str(doc.get("CreditNoteNumber" if kind == "CreditNotes" else "InvoiceNumber") or "").strip()
    if not number:
        return []
    key = f"{'S' if doc.get('Type') in SALES_TYPES else 'P'}:{number}"
    contact = str((doc.get("Contact") or {}).get("Name") or "").strip()
    return [f"{key}|{contact}", key] if contact else [key]


def _document_id(kind: str, doc: dict) -> str:
    return str(doc.get("CreditNoteID" if kind == "CreditNotes" else "InvoiceID") or "")


def _idempotency_key(run_id: uuid.UUID, attachment_id: str) -> str:
    return f"xero-attachment:{run_id}:{attachment_id}"


def _next_operation(run_id: uuid.UUID, current: uuid.UUID | None) -> uuid.UUID:
    """A fetch's operation id follows from the one before it, so a start whose answer was
    lost is retried under the same id and the relay continues the same sign-in."""
    return uuid.uuid5(current or run_id, "xero-attachments")


def _maker():
    import celerp.db  # resolved per call: the engine is replaced in tests and on reconfigure
    return lambda: AsyncSession(bind=celerp.db.engine, expire_on_commit=False)


async def _cloud(method: str, path: str, **kw) -> dict:
    return await cloud_json(method, path, unconfirmed=t("migration.xero_att_unconfirmed"),
                            account_needed=t("migration.xero_att_account_needed"), **kw)


async def close_at_relay(cloud_id: str | None) -> None:
    """Close the Xero access at the relay. Best effort: the relay closes it itself when it
    expires, so a failure here never keeps the owner from stopping or discarding."""
    if not cloud_id:
        return
    try:
        await cloud_entitlement.authenticated_request("DELETE", f"/xero-attachments/{cloud_id}")
    except (httpx.HTTPError, TimeoutError):
        logger.warning("Closing Xero attachment fetch %s at the relay failed; it expires there", cloud_id)


def _require_xero(run: MigrationRun) -> None:
    if run.source_system != "xero":
        raise HTTPException(status_code=409, detail=t("migration.xero_att_only_xero"))
    if run.status not in (MigrationStatus.READY_TO_FINALIZE.value, MigrationStatus.COMPLETED.value):
        raise HTTPException(status_code=409, detail=t("migration.xero_att_not_imported"))


async def _row(session: AsyncSession, run_id: uuid.UUID) -> MigrationAttachmentFetch | None:
    return await session.scalar(
        select(MigrationAttachmentFetch).where(MigrationAttachmentFetch.migration_run_id == run_id))


async def _locked_row(session: AsyncSession, run: MigrationRun) -> MigrationAttachmentFetch | None:
    """The fetch as committed, held under the run company's lock, which judges the request again first."""
    await lock_company(session, run.company_id)
    return await session.scalar(
        select(MigrationAttachmentFetch).where(MigrationAttachmentFetch.migration_run_id == run.id)
        .with_for_update().execution_options(populate_existing=True))


async def view(session: AsyncSession, run: MigrationRun) -> dict:
    """The fetch as the owner sees it: its status, the organisation, the files attached of
    those found, the documents not imported, and while signing in the Xero sign-in link."""
    row = await _row(session, run.id)
    if row is None:
        return {"status": "none", "organisation": "", "error": "", "attached": 0, "total": 0, "unmatched": [],
                "resume_at": None}
    progress = row.progress or {}
    out = {"status": row.status, "organisation": row.organisation, "error": row.error,
           "attached": int(progress.get("attached", 0)), "total": int(progress.get("total", 0)),
           "unmatched": list(progress.get("unmatched", [])),
           "resume_at": row.next_attempt_at.isoformat() if row.status == "waiting" and row.next_attempt_at else None}
    if row.status == "signing_in" and row.cloud_id:
        try:
            out["authorize_url"] = (await _cloud("GET", f"/xero-attachments/{row.cloud_id}")).get("authorize_url")
        except Exception:  # the link is shown again on the next poll
            logger.info("Xero sign-in link for fetch %s not available right now", row.cloud_id)
    return out


async def start(session: AsyncSession, run: MigrationRun, user_id: uuid.UUID) -> dict:
    """Start fetching, or return the fetch already under way. Starting while signing in
    returns the same sign-in link; starting after a fetch ended starts a new one. The
    caller turns cloud refusals into HTTP errors (company_handoff_sender.as_http).

    The relay is asked with no lock held. The fetch is then written under the company lock
    only if no other start, confirm, stop or fetch round changed it meanwhile; otherwise it
    is shown as it is now."""
    _require_xero(run)
    row = await _row(session, run.id)
    if row is not None and row.status in ("fetching", "waiting"):
        return await view(session, run)
    seen = None if row is None else (row.operation_id, row.status)
    if row is None or row.status != "signing_in":
        operation = _next_operation(run.id, row.operation_id if row else None)
    else:
        operation = row.operation_id
    if row is not None and row.status == "confirming":  # signing in to another organisation
        await close_at_relay(row.cloud_id)
    created = await _cloud("POST", "/xero-attachments", payload={"operation_id": str(operation)}, expected=201)
    row = await _locked_row(session, run)
    if (None if row is None else (row.operation_id, row.status)) != seen:
        await session.commit()  # nothing written: the locks go before the relay is read again
        return await view(session, run)
    if row is None:
        row = MigrationAttachmentFetch(migration_run_id=run.id, started_by_user_id=user_id)
        session.add(row)
    if row.operation_id != operation:
        row.progress, row.organisation, row.error, row.next_attempt_at = {}, "", "", None
        row.tenant_id = None
    row.operation_id, row.status, row.started_by_user_id = operation, "signing_in", user_id
    row.cloud_id = str(created.get("id") or "") or None
    await session.commit()
    out = await view(session, run)
    out["authorize_url"] = created.get("authorize_url") or out.get("authorize_url")
    return out


async def confirm(session: AsyncSession, run: MigrationRun) -> dict:
    """The owner saw the organisation the sign-in chose and starts fetching from it, which
    binds these books to it. Refused when no sign-in is waiting for it."""
    _require_xero(run)
    row = await _locked_row(session, run)
    if row is None or row.status != "confirming" or not row.tenant_id:
        raise HTTPException(status_code=409, detail=t("migration.xero_att_confirm_gone"))
    row.bound_tenant_id, row.bound_organisation = row.tenant_id, row.organisation
    row.status, row.error, row.next_attempt_at = "fetching", "", None
    await session.commit()
    return await view(session, run)


async def stop(session: AsyncSession, run: MigrationRun) -> dict:
    """End a fetch in progress and close its Xero access. Files already attached stay."""
    row = await _locked_row(session, run)
    if row is not None and row.status in OPEN:
        row.status, row.next_attempt_at = "stopped", None
        cloud_id = row.cloud_id
        await session.commit()
        await close_at_relay(cloud_id)
    return await view(session, run)


async def discarded_cloud_id(session: AsyncSession, run_id: uuid.UUID) -> str | None:
    """The relay fetch to close when a migration is discarded with its fetch still open."""
    row = await _row(session, run_id)
    return row.cloud_id if row is not None and row.status in OPEN else None


async def _attached_any(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Whether a file fetched for this migration was attached."""
    return await session.scalar(select(LedgerEntry.id).where(
        LedgerEntry.idempotency_key.startswith(_idempotency_key(run_id, ""), autoescape=True)).limit(1)) is not None


def _xero_date(doc: dict) -> str | None:
    """The document's date as YYYY-MM-DD, from DateString or Xero's /Date(ms)/ form."""
    if isinstance(doc.get("DateString"), str) and len(doc["DateString"]) >= 10:
        return doc["DateString"][:10]
    ms = re.match(r"/Date\((-?\d+)", str(doc.get("Date") or ""))
    return datetime.fromtimestamp(int(ms[1]) / 1000, timezone.utc).date().isoformat() if ms else None


def _total(value, currency: str) -> Decimal | None:
    try:
        return round_money(Decimal(str(value)), currency) if value is not None and currency else None
    except (InvalidOperation, ValueError):
        return None


def differences(doc: dict, imported: dict) -> list[str]:
    """The fields of Xero's *doc* that differ from the *imported* document's source values:
    date, currency, total and contact. A value missing on either side differs."""
    currency = str(imported.get("currency") or "").upper()
    same = {
        "date": _xero_date(doc) is not None and _xero_date(doc) == imported.get("issue_date"),
        "currency": bool(currency) and str(doc.get("CurrencyCode") or "").upper() == currency,
        "total": _total(doc.get("Total"), currency) is not None
        and _total(doc.get("Total"), currency) == _total(imported.get("total"), currency),
        "contact": bool(imported.get("contact_name")) and str((doc.get("Contact") or {}).get("Name") or "")
        .strip().casefold() == str(imported["contact_name"]).strip().casefold(),
    }
    return [field for field, ok in same.items() if not ok]


# ── Fetching ─────────────────────────────────────────────────────────────────

class _Pause(Exception):
    """Stop this round: the fetch was saved with its new status and when to try again."""


class _Fetch:
    """One round of one fetch. Every write is conditional on the fetch still being the
    same operation in a live status, so a stop or a new fetch wins over a round in flight."""

    def __init__(self, run: MigrationRun, row: MigrationAttachmentFetch, now: datetime) -> None:
        self.run_id, self.company_id, self.user_id = run.id, run.company_id, row.started_by_user_id
        self.row_id, self.operation, self.cloud_id = row.id, row.operation_id, row.cloud_id
        self.status, self.progress, self.now = row.status, dict(row.progress or {}), now
        self.organisation = row.organisation
        self.tenant_id, self.bound_tenant_id = row.tenant_id, row.bound_tenant_id
        self.bound_organisation = row.bound_organisation

    async def save(self, **values) -> bool:
        async with _maker()() as s:
            result = await s.execute(update(MigrationAttachmentFetch).where(
                MigrationAttachmentFetch.id == self.row_id, MigrationAttachmentFetch.operation_id == self.operation,
                MigrationAttachmentFetch.status.in_(LIVE)).values(**values))
            await s.commit()
            return result.rowcount == 1

    async def still_live(self) -> bool:
        async with _maker()() as s:
            status = await s.scalar(select(MigrationAttachmentFetch.status).where(
                MigrationAttachmentFetch.id == self.row_id, MigrationAttachmentFetch.operation_id == self.operation))
        return status in LIVE

    async def fail(self, message: str) -> None:
        await self.save(status="failed", error=message, next_attempt_at=None)
        await close_at_relay(self.cloud_id)
        raise _Pause

    async def relay(self, path: str, params: dict | None = None, *, total_s: float = 35) -> httpx.Response:
        """One read through the relay. A Xero limit pauses the fetch until it resets; a
        refusal the relay explains ends it with that reason; anything else retries later."""
        try:
            response = await cloud_entitlement.authenticated_request("GET", path, params=params, total_s=total_s)
        except (httpx.HTTPError, TimeoutError):
            await self.retry_later()
        if response is None:
            await self.fail(t("migration.xero_att_account_needed"))
        if response.status_code == 429:
            wait = _retry_after(response)
            if response.headers.get("x-rate-limit-problem", "").lower() == "day":
                await self.save(status="waiting", next_attempt_at=self.now + timedelta(seconds=wait),
                                error=t("migration.xero_att_daily_limit"))
            else:
                await self.save(next_attempt_at=self.now + timedelta(seconds=wait))
            raise _Pause
        if response.status_code in (404, 409):
            await self.fail(_detail(response) or t("migration.xero_att_ended"))
        if response.status_code != 200:
            await self.retry_later()
        return response

    async def retry_later(self) -> None:
        await self.save(next_attempt_at=self.now + timedelta(seconds=RETRY_S))
        raise _Pause

    async def signing_in(self) -> bool:
        """Whether the owner has signed in; a sign-in that failed or ran out ends the fetch."""
        if not self.cloud_id:
            await self.fail(t("migration.xero_att_ended"))
        fetch = (await self.relay(f"/xero-attachments/{self.cloud_id}")).json()
        status = fetch.get("status")
        if status == "connected":
            organisation, tenant = str(fetch.get("organisation") or "")[:200], str(fetch.get("tenant_id") or "")[:64]
            if not tenant:
                await self.fail(t("migration.xero_att_ended"))
            if self.bound_tenant_id and tenant != self.bound_tenant_id:
                async with _maker()() as s:
                    attached = await _attached_any(s, self.run_id)
                if attached:
                    await self.fail(t("migration.xero_att_other_org", organisation=organisation or "Xero",
                                      bound=self.bound_organisation or "Xero"))
            # The owner sees the organisation and starts the fetch (confirm).
            await self.save(status="confirming", organisation=organisation, tenant_id=tenant, error="",
                            next_attempt_at=None)
            return False
        if status == "signing_in":
            return False
        await self.fail(str(fetch.get("error") or "") or t("migration.xero_att_ended"))
        return False

    async def run(self) -> None:
        if self.status == "signing_in":
            await self.signing_in()
            return
        if not self.tenant_id or self.tenant_id != self.bound_tenant_id:
            await self.fail(t("migration.xero_att_other_org", organisation=self.organisation or "Xero",
                              bound=self.bound_organisation or "Xero"))
        if self.status == "waiting":
            await self.save(status="fetching", error="", next_attempt_at=None)
        kind_index, page = int(self.progress.get("kind", 0)), int(self.progress.get("page", 1))
        while kind_index < len(KINDS):
            kind = KINDS[kind_index]
            rows = (await self.relay(f"/xero-attachments/{self.cloud_id}/api/{kind}",
                                     {"page": page})).json().get(kind) or []
            if not rows:
                kind_index, page = kind_index + 1, 1
            else:
                counts = await self.page(kind, rows)
                page += 1
                self.progress = {**self.progress,
                                 "attached": self.progress.get("attached", 0) + counts["attached"],
                                 "total": self.progress.get("total", 0) + counts["total"],
                                 "unmatched": self.progress.get("unmatched", []) + counts["unmatched"]}
            self.progress.update(kind=kind_index, page=page)
            if not await self.save(progress=self.progress, next_attempt_at=None):
                return
        if await self.save(status="done", error="", next_attempt_at=None):
            await self.finished()

    async def page(self, kind: str, rows: list[dict]) -> dict:
        """Attach the files of one page of documents. A page interrupted part way is read
        again from its start: files already attached are recognised and counted once."""
        counts = {"attached": 0, "total": 0, "unmatched": []}
        for doc in rows:
            if not isinstance(doc, dict) or not doc.get("HasAttachments", True):
                continue
            if not await self.still_live():
                raise _Pause
            doc_id = _document_id(kind, doc)
            listed = (await self.relay(f"/xero-attachments/{self.cloud_id}/api/{kind}/{doc_id}/Attachments")
                      ).json().get("Attachments") or []
            files = [f for f in listed if isinstance(f, dict) and f.get("AttachmentID")]
            if not files:
                continue
            number = str(doc.get("CreditNoteNumber" if kind == "CreditNotes" else "InvoiceNumber") or "")
            contact = str((doc.get("Contact") or {}).get("Name") or "")
            target = await self.target(document_keys(kind, doc))
            if target is None:
                counts["unmatched"].append({"document": number, "contact": contact, "files": len(files),
                                            "reason": t("migration.xero_att_not_imported_doc")})
                continue
            differ = differences(doc, await self.imported(target))
            if differ:
                counts["unmatched"].append({"document": number, "contact": contact, "files": len(files),
                                            "reason": t("migration.xero_att_mismatch", fields=", ".join(
                                                t(f"migration.xero_att_field_{field}") for field in differ))})
                continue
            for meta in files:
                counts["total"] += 1
                problem = await self.attach(kind, doc_id, target, meta)
                if problem is None:
                    counts["attached"] += 1
                else:
                    counts["unmatched"].append({"document": number, "contact": contact, "files": 1,
                                                "reason": problem})
        return counts

    async def target(self, keys: list[str]) -> tuple[str, str] | None:
        if not keys:
            return None
        async with _maker()() as s:
            found = (await s.execute(select(
                MigrationEntityMap.source_external_id, MigrationEntityMap.target_entity_type,
                MigrationEntityMap.target_entity_id).where(
                MigrationEntityMap.migration_run_id == self.run_id,
                MigrationEntityMap.source_external_id.in_(keys)))).all()
        by_key = {k: (entity_type, str(entity_id)) for k, entity_type, entity_id in found
                  if entity_type in FILE_ATTACHED_EVENTS}
        return next((by_key[k] for k in keys if k in by_key), None)

    async def imported(self, target: tuple[str, str]) -> dict:
        """The source values this migration imported the *target* document with."""
        async with _maker()() as s:
            data = await s.scalar(select(LedgerEntry.data).where(
                LedgerEntry.company_id == self.company_id, LedgerEntry.entity_id == target[1],
                LedgerEntry.event_type == "doc.created", LedgerEntry.source == "migration").limit(1))
        return data or {}

    async def attach(self, kind: str, doc_id: str, target: tuple[str, str], meta: dict) -> str | None:
        """Download one file and attach it to its document. Returns why it was not attached."""
        attachment_id = str(meta["AttachmentID"])
        name = str(meta.get("FileName") or attachment_id)
        key = _idempotency_key(self.run_id, attachment_id)
        async with _maker()() as s:
            if await find_event_by_idempotency(s, self.company_id, key) is not None:
                return None
        response = await self.relay(
            f"/xero-attachments/{self.cloud_id}/api/{kind}/{doc_id}/Attachments/{attachment_id}", total_s=DOWNLOAD_S)
        content = response.content
        mime = str(meta.get("MimeType") or "") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        entity_type, entity_id = target
        file_id = str(uuid.uuid5(self.run_id, f"xero-attachment:{attachment_id}"))
        async with _maker()() as s:
            # The run's lock keeps a re-run, a discard and the cleanup sweep away from the
            # file between storing and linking it; a busy run is tried again next round.
            if not await _try_xact_lock(s, self.run_id):
                await s.rollback()
                await self.retry_later()
            run_status = await s.scalar(select(MigrationRun.status).where(MigrationRun.id == self.run_id))
            if run_status not in (MigrationStatus.READY_TO_FINALIZE.value, MigrationStatus.COMPLETED.value):
                await s.rollback()
                await self.retry_later()
            if await find_event_by_idempotency(s, self.company_id, key) is not None:
                return None
            if await s.get(Projection, {"company_id": self.company_id, "entity_id": entity_id}) is None:
                return t("migration.xero_att_doc_gone", file=name)
            task_id = await stage_file_cleanup(s, self.company_id, self.run_id, file_id, mime, key)
            try:
                stored = await store_file(str(self.company_id), content, name, mime, att_id=file_id)
            except ValueError as exc:
                await s.execute(delete(MigrationCleanupTask).where(MigrationCleanupTask.id == task_id))
                await s.commit()
                return f"{name}: {exc}"
            await attach_file(s, self.company_id, entity_type, entity_id, stored, self.user_id,
                              source="migration", idempotency_key=key)
            await s.execute(delete(MigrationCleanupTask).where(MigrationCleanupTask.id == task_id))
            await s.commit()
        return None

    async def finished(self) -> None:
        await close_at_relay(self.cloud_id)
        async with _maker()() as s:
            await notification_service.create_keyed(
                s, self.company_id, "system", "notice.xero_attachments_fetched",
                {"attached": self.progress.get("attached", 0), "total": self.progress.get("total", 0),
                 "organisation": self.organisation},
                action_url=f"/migrations/{self.run_id}")
            await s.commit()


def _retry_after(response: httpx.Response) -> int:
    try:
        return max(int(response.headers.get("retry-after", "60")), 1)
    except ValueError:
        return 60


def _detail(response: httpx.Response) -> str | None:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, str) and detail else None


async def advance(run_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Take one fetch as far as it can go now: check the sign-in, then read and attach
    until done, paused by a Xero limit, or stopped. A second call for the same run while
    one is running returns at once."""
    import celerp.db

    now = now or datetime.now(timezone.utc)
    async with celerp.db.engine.connect() as holder:
        key = {"k": f"xero-attachments-round:{run_id}"}
        locked = await holder.scalar(text("SELECT pg_try_advisory_lock(hashtext(:k))"), key)
        await holder.commit()
        if not locked:
            return
        try:
            async with _maker()() as s:
                row = await _row(s, run_id)
                run = await s.get(MigrationRun, run_id)
            if row is None or run is None or row.status not in LIVE:
                return
            if row.next_attempt_at is not None and row.next_attempt_at > now:
                return
            try:
                await _Fetch(run, row, now).run()
            except _Pause:
                pass
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), key)
            await holder.commit()


async def due_runs(session: AsyncSession, now: datetime) -> list[uuid.UUID]:
    return list((await session.scalars(select(MigrationAttachmentFetch.migration_run_id).where(
        MigrationAttachmentFetch.status.in_(LIVE),
        (MigrationAttachmentFetch.next_attempt_at.is_(None)) | (MigrationAttachmentFetch.next_attempt_at <= now),
    ))).all())


async def fetch_loop() -> None:
    """Advance every live fetch once a minute. One failing fetch never stops the others."""
    while True:
        await asyncio.sleep(LOOP_S)
        try:
            async with _maker()() as s:
                run_ids = await due_runs(s, datetime.now(timezone.utc))
            for run_id in run_ids:
                try:
                    await advance(run_id)
                except Exception:
                    logger.exception("Fetching Xero attachments for migration %s failed; retried next round", run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Xero attachment fetch loop round failed")

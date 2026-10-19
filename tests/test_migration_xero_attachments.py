# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""After a Xero export set is imported, the files attached to its invoices, bills and credit
notes in Xero are fetched through the relay and attached to the imported documents.

An export set carries no attachments, so the owner signs in to Xero once through Celerp's
read-only Xero app; Celerp reads each document that has files and attaches them. These
tests import the demo Xero company through the real runner and stand in for the relay with
its route contract."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from company_backup_support import token
from fixtures.xero_export import build
from migration_support import auth, load_run, maker, real_client, real_engine  # noqa: F401
from test_export_set_adapters import _xero
from test_migration_e2e import migrate_set

pytestmark = pytest.mark.timeout(240)  # each test imports three years of the demo company

PDF = b"%PDF-1.4\n% receipt\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _first(kind: str):
    return next(d for d in build.simulate().documents if d.kind == kind)


def _in_xero(doc) -> dict:
    """The values Xero's API gives the document the export set was built from."""
    return {"DateString": f"{doc.issued.isoformat()}T00:00:00", "CurrencyCode": doc.currency,
            "Total": float(doc.total)}


DEMO_TENANT, OTHER_TENANT = "6f1c0d2e-0000-4000-8000-00000000d3e0", "6f1c0d2e-0000-4000-8000-0000000000a7"


class FakeRelay:
    """The relay's Xero attachment routes, with one organisation's documents behind them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fetches: dict[str, dict] = {}
        self.invoices: list[dict] = []
        self.credit_notes: list[dict] = []
        self.files: dict[str, list[tuple[str, str, str, bytes]]] = {}
        self.limited: httpx.Response | None = None
        self.no_account = False

    def document(self, kind: str, number: str, xero_type: str, contact: str,
                 *files: tuple[str, str, bytes], **values) -> str:
        doc_id = str(uuid.uuid4())
        key = "CreditNoteID" if kind == "CreditNotes" else "InvoiceID"
        number_key = "CreditNoteNumber" if kind == "CreditNotes" else "InvoiceNumber"
        (self.credit_notes if kind == "CreditNotes" else self.invoices).append(
            {key: doc_id, number_key: number, "Type": xero_type, "Contact": {"Name": contact},
             "HasAttachments": True, **values})
        self.files[doc_id] = [(str(uuid.uuid4()), name, mime, data) for name, mime, data in files]
        return doc_id

    def connect(self, fetch_id: str, organisation: str = "Demo Trading Ltd", tenant_id: str = DEMO_TENANT) -> None:
        self.fetches[fetch_id].update(status="connected", organisation=organisation, tenant_id=tenant_id)

    async def request(self, method, path, *, json=None, params=None, total_s=35, content=None):
        if self.no_account:
            return None
        self.calls.append((method, path, params))
        if (method, path) == ("POST", "/xero-attachments"):
            same = next((f for f in self.fetches.values() if f["operation_id"] == json["operation_id"]), None)
            if same is None:
                same = {"id": str(uuid.uuid4()), "operation_id": json["operation_id"], "status": "signing_in",
                        "organisation": "", "tenant_id": None, "error": ""}
                self.fetches[same["id"]] = same
            return httpx.Response(201, json=self._view(same))
        parts = path.strip("/").split("/")
        fetch = self.fetches.get(parts[1])
        if fetch is None:
            return httpx.Response(404, json={"detail": "Xero attachment fetch not found"})
        if method == "DELETE":
            fetch["status"] = "closed"
            return httpx.Response(200, json=self._view(fetch))
        if len(parts) == 2:
            return httpx.Response(200, json=self._view(fetch))
        if fetch["status"] != "connected":
            return httpx.Response(409, json={"detail": "Sign in to Xero first, from the link Celerp gave you."})
        if self.limited is not None:
            return self.limited
        resource = parts[3:]
        if len(resource) == 1:
            rows = self.credit_notes if resource[0] == "CreditNotes" else self.invoices
            page = int((params or {}).get("page", 1))
            return httpx.Response(200, json={resource[0]: rows[(page - 1) * 100:page * 100]})
        files = self.files[resource[1]]
        if len(resource) == 3:
            return httpx.Response(200, json={"Attachments": [
                {"AttachmentID": a, "FileName": n, "MimeType": m, "ContentLength": len(d)} for a, n, m, d in files]})
        a, n, m, d = next(f for f in files if f[0] == resource[3])
        return httpx.Response(200, content=d, headers={"content-type": m})

    @staticmethod
    def _view(fetch: dict) -> dict:
        out = {k: fetch[k] for k in ("id", "status", "organisation", "tenant_id", "error")}
        if fetch["status"] == "signing_in":
            out["authorize_url"] = f"https://login.xero.com/identity/connect/authorize?state={fetch['id']}"
        return out

    def reads(self) -> list[str]:
        return [p for m, p, _ in self.calls if "/api/" in p]


@pytest.fixture
def relay(monkeypatch):
    from celerp.services import cloud_entitlement
    fake = FakeRelay()
    monkeypatch.setattr(cloud_entitlement, "authenticated_request", fake.request)
    return fake


async def _imported(engine, monkeypatch, tmp_path):
    run, rejected = await migrate_set(engine, list(_xero()), {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == [] and run.status == "ready_to_finalize"
    return run, auth(await token(engine, run.created_by_user_id, run.company_id))


async def _doc_files(engine, run, number_key: str) -> list[dict]:
    from celerp.models.migration import MigrationEntityMap
    from celerp.models.projections import Projection
    async with maker(engine)() as s:
        entity_id = await s.scalar(select(MigrationEntityMap.target_entity_id).where(
            MigrationEntityMap.migration_run_id == run.id, MigrationEntityMap.source_external_id == number_key))
        row = await s.get(Projection, {"company_id": run.company_id, "entity_id": entity_id})
        return row.state.get("files") or []


async def _advance(run, now: datetime | None = None) -> None:
    from celerp.services import migration_xero_attachments as fetching
    await fetching.advance(run.id, now=now)


def test_a_xero_document_is_found_by_the_key_the_export_set_gave_it():
    from celerp.services.migration_xero_attachments import document_keys
    assert document_keys("Invoices", {"Type": "ACCREC", "InvoiceNumber": "INV-1", "Contact": {"Name": "A Ltd"}}) \
        == ["S:INV-1|A Ltd", "S:INV-1"]
    assert document_keys("Invoices", {"Type": "ACCPAY", "InvoiceNumber": "B-7", "Contact": {"Name": "B"}}) \
        == ["P:B-7|B", "P:B-7"]
    assert document_keys("CreditNotes", {"Type": "ACCRECCREDIT", "CreditNoteNumber": "CN-2",
                                         "Contact": {"Name": "A Ltd"}}) == ["S:CN-2|A Ltd", "S:CN-2"]
    assert document_keys("CreditNotes", {"Type": "ACCPAYCREDIT", "CreditNoteNumber": "9",
                                         "Contact": {"Name": "B"}}) == ["P:9|B", "P:9"]
    # A bill entered without a number was exported without one: nothing can match it.
    assert document_keys("Invoices", {"Type": "ACCPAY", "InvoiceNumber": "", "Contact": {"Name": "B"}}) == []


async def test_files_in_xero_are_attached_to_the_imported_documents(real_engine, real_client, relay,
                                                                    monkeypatch, tmp_path):
    from celerp.models.notification import Notification
    run, headers = await _imported(real_engine, monkeypatch, tmp_path)
    base = f"/migrations/{run.id}/xero-attachments"
    invoice, bill, note = _first("Sales invoice"), _first("Bill"), _first("Sales credit note")
    relay.document("Invoices", invoice.number, "ACCREC", invoice.contact, ("receipt.pdf", "application/pdf", PDF),
                   **_in_xero(invoice))
    relay.document("Invoices", bill.number, "ACCPAY", bill.contact,
                   ("bill.pdf", "application/pdf", PDF), ("delivery.png", "image/png", PNG), **_in_xero(bill))
    relay.document("CreditNotes", note.number, "ACCRECCREDIT", note.contact, ("note.pdf", "application/pdf", PDF),
                   **_in_xero(note))
    gone = relay.document("Invoices", "INV-9999", "ACCREC", "Gone Customer", ("old.pdf", "application/pdf", PDF))

    assert (await real_client.get(base, headers=headers)).json()["status"] == "none"

    started = await real_client.post(base, headers=headers)
    assert started.status_code == 200, started.text
    view = started.json()
    assert view["status"] == "signing_in" and view["authorize_url"].startswith("https://login.xero.com/")
    # Starting again while signing in keeps the same sign-in link.
    again = await real_client.post(base, headers=headers)
    assert again.json()["authorize_url"] == view["authorize_url"] and len(relay.fetches) == 1

    await _advance(run)
    assert (await real_client.get(base, headers=headers)).json()["status"] == "signing_in"
    assert relay.reads() == []

    relay.connect(next(iter(relay.fetches)))
    await _advance(run)
    # Signed in: the owner sees which organisation and starts the fetch themselves.
    confirming = (await real_client.get(base, headers=headers)).json()
    assert (confirming["status"], confirming["organisation"]) == ("confirming", "Demo Trading Ltd")
    await _advance(run)
    assert relay.reads() == []
    assert (await real_client.post(f"{base}/confirm", headers=headers)).json()["status"] == "fetching"
    await _advance(run)
    done = (await real_client.get(base, headers=headers)).json()
    assert done["status"] == "done", done
    assert done["organisation"] == "Demo Trading Ltd"
    assert (done["attached"], done["total"]) == (4, 4)
    assert done["unmatched"] == [{"document": "INV-9999", "contact": "Gone Customer", "files": 1,
                                  "reason": "This document was not imported into Celerp."}]
    assert [f["filename"] for f in await _doc_files(real_engine, run, f"S:{invoice.number}")] == ["receipt.pdf"]
    assert sorted(f["filename"] for f in await _doc_files(real_engine, run, f"P:{bill.number}")) \
        == ["bill.pdf", "delivery.png"]
    assert [f["filename"] for f in await _doc_files(real_engine, run, f"S:{note.number}")] == ["note.pdf"]
    # The files of a document Celerp never imported are not downloaded.
    assert not any(p.endswith(relay.files[gone][0][0]) for p in relay.reads())
    # Finishing closes the Xero access and leaves a notice.
    assert relay.calls[-1][0] == "DELETE"
    async with maker(real_engine)() as s:
        notices = (await s.scalars(select(Notification.title).where(
            Notification.company_id == run.company_id))).all()
    assert "Xero attachments fetched" in notices

    # Fetching again reads Xero again but never attaches a file twice.
    reads = len(relay.reads())
    again = await real_client.post(base, headers=headers)
    assert again.json()["status"] == "signing_in" and len(relay.fetches) == 2
    relay.connect(again.json()["authorize_url"].rsplit("=", 1)[1])
    await _advance(run)
    await real_client.post(f"{base}/confirm", headers=headers)
    await _advance(run)
    assert len(relay.reads()) > reads
    assert (await real_client.get(base, headers=headers)).json()["attached"] == 4
    assert len(await _doc_files(real_engine, run, f"P:{bill.number}")) == 2


async def test_xero_limits_pause_the_fetch_and_stop_ends_it(real_engine, real_client, relay, monkeypatch, tmp_path):
    run, headers = await _imported(real_engine, monkeypatch, tmp_path)
    base = f"/migrations/{run.id}/xero-attachments"
    invoice = _first("Sales invoice")
    relay.document("Invoices", invoice.number, "ACCREC", invoice.contact, ("receipt.pdf", "application/pdf", PDF),
                   **_in_xero(invoice))

    # Only the owner of the migration sees or starts it.
    from company_backup_support import company, owner
    other = await owner(real_engine, "other@example.com", "Other")
    stranger = auth(await token(real_engine, other, await company(real_engine, other, "Other Co", "other")))
    assert (await real_client.post(base, headers=stranger)).status_code == 404
    assert (await real_client.get(base, headers=stranger)).status_code == 404

    relay.no_account = True
    refused = await real_client.post(base, headers=headers)
    assert refused.status_code == 428 and "free Celerp account" in refused.json()["detail"]
    relay.no_account = False

    started = (await real_client.post(base, headers=headers)).json()
    relay.connect(next(iter(relay.fetches)))
    await _advance(run)
    await real_client.post(f"{base}/confirm", headers=headers)
    relay.limited = httpx.Response(429, headers={"retry-after": "7200", "x-rate-limit-problem": "day"},
                                   json={"Title": "Rate limit exceeded"})
    now = datetime.now(timezone.utc)
    await _advance(run, now)
    waiting = (await real_client.get(base, headers=headers)).json()
    assert waiting["status"] == "waiting", waiting
    assert "daily limit" in waiting["error"]
    resume = datetime.fromisoformat(waiting["resume_at"])
    assert now + timedelta(seconds=7190) <= resume <= now + timedelta(seconds=7300)
    # Nothing is asked of Xero again before the limit resets.
    reads = len(relay.reads())
    await _advance(run, now + timedelta(minutes=30))
    assert len(relay.reads()) == reads

    relay.limited = None
    await _advance(run, now + timedelta(hours=3))
    assert (await real_client.get(base, headers=headers)).json()["status"] == "done"
    assert started["status"] == "signing_in"

    # Stop ends a fetch in progress and closes its Xero access; nothing more is read.
    relay.document("Invoices", "INV-0002", "ACCREC", invoice.contact, ("second.pdf", "application/pdf", PDF))
    second = (await real_client.post(base, headers=headers)).json()
    stopped = await real_client.post(f"{base}/stop", headers=headers)
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
    assert relay.calls[-1][0] == "DELETE" and second["status"] == "signing_in"
    reads = len(relay.reads())
    await _advance(run, now + timedelta(hours=4))
    assert len(relay.reads()) == reads

    # A sign-in Xero refused or that ran out ends the fetch with the relay's reason.
    third = (await real_client.post(base, headers=headers)).json()
    fetch = relay.fetches[third["authorize_url"].rsplit("=", 1)[1]]
    fetch.update(status="failed", error="Choose exactly one organisation when you sign in to Xero.")
    await _advance(run, now + timedelta(hours=5))
    failed = (await real_client.get(base, headers=headers)).json()
    assert failed["status"] == "failed" and "exactly one organisation" in failed["error"]


async def test_only_a_xero_migration_fetches_from_xero(real_engine, real_client, relay, monkeypatch, tmp_path):
    from celerp.models.migration import MigrationRun
    run, headers = await _imported(real_engine, monkeypatch, tmp_path)
    async with maker(real_engine)() as s:
        row = await s.get(MigrationRun, run.id)
        row.source_system = "quickbooks_online"
        await s.commit()
    refused = await real_client.post(f"/migrations/{run.id}/xero-attachments", headers=headers)
    assert refused.status_code == 409 and "Xero" in refused.json()["detail"]
    assert relay.calls == []
    assert (await load_run(real_engine, run.id)).status == "ready_to_finalize"


async def _fetch_from(client, run, headers, relay, organisation: str, tenant_id: str) -> dict:
    """Sign in to one organisation, confirm it and fetch; the fetch as the owner then sees it."""
    base = f"/migrations/{run.id}/xero-attachments"
    started = (await client.post(base, headers=headers)).json()
    relay.connect(started["authorize_url"].rsplit("=", 1)[1], organisation, tenant_id)
    await _advance(run)
    confirm = await client.post(f"{base}/confirm", headers=headers)
    if confirm.status_code == 200:
        await _advance(run)
    return (await client.get(base, headers=headers)).json()


async def test_files_of_another_organisation_with_the_same_numbers_are_never_attached(
        real_engine, real_client, relay, monkeypatch, tmp_path):
    """Two Xero organisations can both have an INV-0001 for a customer of the same name. The
    files are attached only when Xero's date, currency, total and contact match the document
    imported; the owner can then sign in to the right organisation and fetch again."""
    run, headers = await _imported(real_engine, monkeypatch, tmp_path)
    invoice, note = _first("Sales invoice"), _first("Sales credit note")
    other = relay.document("Invoices", invoice.number, "ACCREC", invoice.contact,
                           ("theirs.pdf", "application/pdf", PDF),
                           **{**_in_xero(invoice), "DateString": "2019-02-03T00:00:00", "Total": 12.5})
    relay.document("CreditNotes", note.number, "ACCRECCREDIT", note.contact, ("their-note.pdf", "application/pdf", PDF),
                   **{**_in_xero(note), "Total": float(note.total) + 1})

    wrong = await _fetch_from(real_client, run, headers, relay, "Other Trading Ltd", OTHER_TENANT)
    assert wrong["status"] == "done" and wrong["organisation"] == "Other Trading Ltd"
    assert (wrong["attached"], wrong["total"]) == (0, 0)
    reasons = {row["document"]: row["reason"] for row in wrong["unmatched"]}
    assert set(reasons) == {invoice.number, note.number}
    assert "date" in reasons[invoice.number] and "total" in reasons[invoice.number]
    assert "total" in reasons[note.number] and "date" not in reasons[note.number]
    assert "fetch again" in reasons[invoice.number]
    assert await _doc_files(real_engine, run, f"S:{invoice.number}") == []
    assert await _doc_files(real_engine, run, f"S:{note.number}") == []
    # Nothing of the other organisation's files was downloaded.
    assert not any(p.endswith(relay.files[other][0][0]) for p in relay.reads())

    # Signed in to the right organisation, the true matches are attached and the rest still are not.
    relay.invoices.clear()
    relay.credit_notes.clear()
    relay.document("Invoices", invoice.number, "ACCREC", invoice.contact, ("receipt.pdf", "application/pdf", PDF),
                   **_in_xero(invoice))
    relay.document("CreditNotes", note.number, "ACCRECCREDIT", note.contact, ("note.pdf", "application/pdf", PDF),
                   **_in_xero(note))
    bill = _first("Bill")
    relay.document("Invoices", bill.number, "ACCPAY", bill.contact, ("bill.pdf", "application/pdf", PDF),
                   **{**_in_xero(bill), "CurrencyCode": "USD" if bill.currency != "USD" else "EUR"})
    right = await _fetch_from(real_client, run, headers, relay, "Demo Trading Ltd", DEMO_TENANT)
    assert right["status"] == "done", right
    assert (right["attached"], right["total"]) == (2, 2)
    assert [row["document"] for row in right["unmatched"]] == [bill.number]
    assert "currency" in right["unmatched"][0]["reason"]
    assert [f["filename"] for f in await _doc_files(real_engine, run, f"S:{invoice.number}")] == ["receipt.pdf"]
    assert [f["filename"] for f in await _doc_files(real_engine, run, f"S:{note.number}")] == ["note.pdf"]
    assert await _doc_files(real_engine, run, f"P:{bill.number}") == []


async def test_once_files_are_attached_a_fetch_from_another_organisation_is_refused(
        real_engine, real_client, relay, monkeypatch, tmp_path):
    run, headers = await _imported(real_engine, monkeypatch, tmp_path)
    base = f"/migrations/{run.id}/xero-attachments"
    invoice = _first("Sales invoice")
    relay.document("Invoices", invoice.number, "ACCREC", invoice.contact, ("receipt.pdf", "application/pdf", PDF),
                   **_in_xero(invoice))
    first = await _fetch_from(real_client, run, headers, relay, "Demo Trading Ltd", DEMO_TENANT)
    assert (first["status"], first["attached"]) == ("done", 1)

    # Another organisation whose document matches in every value is still refused: these
    # books' files come from the organisation they were first fetched from.
    reads = len(relay.reads())
    refused = await _fetch_from(real_client, run, headers, relay, "Other Trading Ltd", OTHER_TENANT)
    assert refused["status"] == "failed", refused
    assert "Other Trading Ltd" in refused["error"] and "Demo Trading Ltd" in refused["error"]
    assert len(relay.reads()) == reads
    assert relay.calls[-1][0] == "DELETE"
    # Confirming a sign-in that is no longer waiting for it is refused too.
    assert (await real_client.post(f"{base}/confirm", headers=headers)).status_code == 409

    again = await _fetch_from(real_client, run, headers, relay, "Demo Trading Ltd", DEMO_TENANT)
    assert (again["status"], again["attached"]) == ("done", 1)

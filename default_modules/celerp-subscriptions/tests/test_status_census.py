# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What the app shows after a subscription action is read after the action from what is
stored, over every door status_census finds.

Each UI route that answers an action has a scenario here: the route's answer is checked
against a fresh read of the agreement, and run again with every read after the action
failing, where a done action is never reported as refused. A route the census finds with no
scenario fails, as does a read after an action outside ``_redrawn`` and a document badge
that decides from the document alone how its agreement bills it. Readers of a send record are
test_send_census's."""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status_census  # noqa: E402
from agreement_kit import agreement, company_id, contact, freeze_day, schedule, tick  # noqa: E402
from ui.i18n import sentence  # noqa: E402

_ROWS = [r for r in status_census.ui_actions() if r.actions]
_FACT = re.compile(r"\b(?:[A-Z]{2,5}-\d{4}-\d{4}|\d{4}-\d{2}-\d{2})\b")


def _toast(r) -> dict:
    return json.loads(r.headers.get("HX-Trigger") or "{}").get("celerpToast") or {}


def _answer_text(r) -> str:
    return f"{_toast(r).get('message', '')} {r.text}"


async def _fresh(api, *eids: str) -> str:
    """The agreements as stored: their records, their period lists and the numbers of the
    documents their quantity changes issued, documents named."""
    parts = []
    for eid in eids:
        record = (await api.get(f"/docs/{eid}")).json()
        parts.append(json.dumps(record))
        for change in record.get("amendments") or []:
            for doc_id in filter(None, (change.get("credit_note_id"), change.get("invoice_id"))):
                parts.append(str((await api.get(f"/docs/{doc_id}")).json().get("ref_id")))
        parts.append(json.dumps((await api.get(f"/subscriptions/{eid}/periods")).json()))
    return " ".join(parts)


def _facts_hold(answer: str, fresh: str) -> None:
    """Every document number and date the answer names is in the stored agreement."""
    named = set(_FACT.findall(answer))
    assert named <= set(_FACT.findall(fresh)), named - set(_FACT.findall(fresh))


def _not_refused(r) -> None:
    """A done action reads as done: no refusal toast, no swap suppressed as for a refusal."""
    assert r.status_code < 400, r.text
    assert _toast(r).get("type") in (None, "success", "warning"), _toast(r)
    assert r.headers.get("HX-Reswap") != "none", r.headers


@pytest.fixture
def reads_fail_after_action(monkeypatch):
    """Every ui.api_client read raises a transient 503 once an action has returned."""
    import ui.api_client as api

    verbs = status_census.api_verbs()
    acted: list[str] = []

    def arm():
        for name in verbs:
            fn = getattr(api, name, None)
            if fn is None:
                continue
            if status_census.is_action(verbs, name):
                async def act(*a, _fn=fn, _name=name, **k):
                    out = await _fn(*a, **k)
                    acted.append(_name)
                    return out
                monkeypatch.setattr(api, name, act)
            elif status_census.is_read(verbs, name):
                async def read(*a, _fn=fn, **k):
                    if acted:
                        raise api.APIError(503, "Service temporarily unavailable")
                    return await _fn(*a, **k)
                monkeypatch.setattr(api, name, read)
        return acted
    return arm


def _no_email(monkeypatch) -> None:
    monkeypatch.setattr("celerp.config.settings.gateway_token", "")
    monkeypatch.setattr("celerp.config.settings.smtp_host", "")


async def _due(ui, monkeypatch) -> str:
    freeze_day(monkeypatch, "2026-07-01")
    return await agreement(ui.api, {}, start="2026-07-01")


# --- one scenario per census row: (ui, session, monkeypatch, arm) -> None -------------------

async def _generate_all_due(ui, session, monkeypatch, arm):
    eid = await _due(ui, monkeypatch)
    failing = arm()
    r = await ui.post("/subscriptions/generate-due", data={"item": json.dumps([f"{eid}|0"]), "idempotency_key": "k1"},
                      headers={"HX-Current-URL": "http://test/subscriptions?direction=sales"})
    _not_refused(r)
    if failing is not None:
        failing.clear()
    _facts_hold(_answer_text(r), await _fresh(ui.api, eid))
    assert re.search(r"INV-\d{4}-\d{4}", r.text), "the answer names the invoice it billed"


async def _catch_up(ui, session, monkeypatch, arm):
    freeze_day(monkeypatch, "2026-09-01")
    eids = []
    for _ in range(2):
        eid = await agreement(ui.api, {}, start="2026-07-01")
        assert (await schedule(ui.api, {}, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
        eids.append(eid)
    failing = arm()
    run = await ui.post("/subscriptions/catch-up/run", data={"item": json.dumps([f"{eids[0]}|0"]), "idempotency_key": "k2"})
    _not_refused(run)
    skip = await ui.post("/subscriptions/catch-up/skip", data={"item": json.dumps([f"{eids[1]}|0"]), "reason": "Closed"})
    _not_refused(skip)
    if failing is not None:
        failing.clear()
    _facts_hold(_answer_text(run) + _answer_text(skip), await _fresh(ui.api, *eids))
    assert re.search(r"INV-\d{4}-\d{4}", run.text), "the answer names the invoice it billed"
    period = next(p for p in (await ui.api.get(f"/subscriptions/{eids[1]}/periods")).json()["rows"]
                  if p["period_index"] == 0)
    assert period["state"] == "skipped"
    assert sentence("subscriptions.done_skipped", start=period["service_start"], end=period["service_end"]) in skip.text


async def _summary_ack(ui, session, monkeypatch, arm):
    _no_email(monkeypatch)
    freeze_day(monkeypatch, "2026-07-01")
    eid = await agreement(ui.api, {}, start="2026-07-01", contact_id=await contact(ui.api, {}, "m@example.test"))
    await schedule(ui.api, {}, eid, generation_mode="auto_invoice", auto_send=True)
    await tick(session, await company_id(ui.api, {}), "2026-07-01")
    keys = [row["key"] for row in (await ui.api.get("/subscriptions/summary")).json()["rows"] if row["key"]]
    assert keys
    arm()
    r = await ui.post("/subscriptions/summary/ack", data={"keys": json.dumps(keys)})
    _not_refused(r)
    assert r.headers["HX-Redirect"] == "/subscriptions/summary" and _toast(r)["type"] == "success"
    assert (await ui.api.get("/subscriptions/summary")).json()["rows"] == []


async def _new(ui, session, monkeypatch, arm):
    arm()
    r = await ui.post("/subscriptions/new?direction=sales")
    _not_refused(r)
    eid = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    assert (await ui.api.get(f"/docs/{eid}")).json()["doc_type"] == "subscription_invoice"
    assert _toast(r)["type"] == "success"


async def _lifecycle(ui, session, monkeypatch, arm):
    eid = await _due(ui, monkeypatch)
    arm()
    r = await ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "k3"})
    _not_refused(r)
    _facts_hold(_answer_text(r), await _fresh(ui.api, eid))
    paused = await ui.post(f"/subscriptions/{eid}/pause")
    _not_refused(paused)
    assert (await ui.api.get(f"/docs/{eid}")).json()["status"] == "paused"


async def _amend(ui, session, monkeypatch, arm):
    """A change inside a billed period names its date and the two documents that settle it."""
    eid = await _due(ui, monkeypatch)
    assert (await ui.post(f"/subscriptions/{eid}/generate-due", data={"idempotency_key": "k4"})).status_code < 400
    line = (await ui.api.get(f"/docs/{eid}")).json()["line_items"][0]
    arm()
    r = await ui.post(f"/subscriptions/{eid}/amend", data={
        "effective": "2026-07-01",
        "quantities": json.dumps({line["line_id"]: float(line.get("quantity") or 1) + 1}),
        "idempotency_key": "k5"})
    _not_refused(r)
    _facts_hold(_answer_text(r), await _fresh(ui.api, eid))


async def _period(ui, session, monkeypatch, arm):
    """Skip names the dates it skipped; a send says what happened to the email, as stored."""
    _no_email(monkeypatch)
    freeze_day(monkeypatch, "2026-07-01")
    eid = await agreement(ui.api, {}, start="2026-07-01", contact_id=await contact(ui.api, {}, "m@example.test"))
    await schedule(ui.api, {}, eid, generation_mode="auto_invoice", auto_send=True)
    await tick(session, await company_id(ui.api, {}), "2026-07-01")
    arm()
    skipped = await ui.post(f"/subscriptions/{eid}/periods/1/skip", data={"reason": "Closed"})
    _not_refused(skipped)
    shown = next(p for p in (await ui.api.get(f"/subscriptions/{eid}/periods")).json()["rows"] if p["period_index"] == 0)
    sent = await ui.post(f"/subscriptions/{eid}/periods/0/send", data={"back": "summary", "attempt": shown["attempt"]})
    assert sent.status_code == 204, sent.text
    fresh = (await ui.api.get(f"/subscriptions/{eid}/periods")).json()
    status = next(p for p in fresh["rows"] if p["period_index"] == 0)["send"]
    assert status == "failed"
    toast = _toast(sent)
    assert toast.get("type") == "error" and toast.get("message"), "a send that failed again says so"
    _facts_hold(_answer_text(skipped) + _answer_text(sent), await _fresh(ui.api, eid))


async def _history(ui, session, monkeypatch, arm):
    from celerp.events.engine import emit_event

    eid = await _due(ui, monkeypatch)
    await emit_event(session, company_id=uuid.UUID(await company_id(ui.api, {})), entity_id=eid,
                     entity_type="doc", event_type="doc.updated",
                     data={"fields_changed": {"history_review": {"new": [{"doc_id": "doc:gone", "candidates": []}]}}},
                     actor_id=None, location_id=None, source="test", idempotency_key=str(uuid.uuid4()))
    await session.commit()
    arm()
    r = await ui.post(f"/subscriptions/{eid}/history/doc:gone/set-aside")
    _not_refused(r)
    assert any(row["doc_id"] == "doc:gone" for row in (await ui.api.get(f"/docs/{eid}")).json()["history_set_aside"])


async def _schedule(ui, session, monkeypatch, arm):
    eid = await _due(ui, monkeypatch)
    failing = arm()
    r = await ui.patch(f"/subscriptions/{eid}/schedule/frequency", data={"value": "weekly"})
    _not_refused(r)
    if failing is not None:
        failing.clear()
    assert (await ui.api.get(f"/docs/{eid}")).json()["frequency"] == "weekly"
    _facts_hold(_answer_text(r), await _fresh(ui.api, eid))


async def _import(ui, session, monkeypatch, arm):
    freeze_day(monkeypatch, "2026-07-01")
    csv = "name,doc_type,frequency,start_date\nCensus Gym,sales,monthly,2026-07-01\n"
    import tempfile
    monkeypatch.setattr("celerp.config.settings.data_dir", Path(tempfile.mkdtemp()))
    preview = await ui.post("/subscriptions/import/preview", files={"csv_file": ("s.csv", csv, "text/csv")})
    ref = re.search(r'name="csv_ref" value="([^"]+)"', preview.text).group(1)
    arm()
    r = await ui.post("/subscriptions/import/confirm", data={"csv_ref": ref})
    _not_refused(r)
    listed = (await ui.api.get("/subscriptions")).json()["items"]
    assert [i.get("name") for i in listed] == ["Census Gym"]


async def _donor_statements(ui, session, monkeypatch, arm):
    from income_books import PERIOD, _mailbox, _statement_donors

    donors = await _statement_donors(ui.api, {"headers": {}})
    sent = _mailbox(monkeypatch)
    arm()
    r = await ui.post("/donors/statements/send", data={**PERIOD, "confirm": "on"})
    _not_refused(r)
    assert _toast(r)["type"] == "success" and [m["to"] for m in sent] == ["cy@test.test"]
    rows = {row["contact_id"]: row for row in (await ui.api.get("/donors/statements/preview", params=PERIOD)).json()["donors"]}
    assert rows[donors["email"]]["statement_sent"] is True and rows[donors["none"]]["statement_sent"] is False


async def _donor_wording(ui, session, monkeypatch, arm):
    arm()
    r = await ui.put("/donors/statements/wording", data={"text": "With thanks, {name}."})
    _not_refused(r)
    assert (await ui.api.get("/donors/statements/wording")).json() == {"text": "With thanks, {name}.", "version": 1}


async def _collection(ui, session, monkeypatch, arm):
    from test_collection import LINK, _bind, _payer, stand_in

    cloud = stand_in(monkeypatch, session)
    freeze_day(monkeypatch, "2026-06-15")
    payer = await _payer(ui.api, {})
    eid = await agreement(ui.api, {}, start="2026-07-01", contact_id=payer)
    assert await _bind(await company_id(ui.api, {}), payer)
    failing = arm()
    r = await ui.post(f"/subscriptions/{eid}/collection/setup-link", data={"delivery": "copy"})
    _not_refused(r)
    assert LINK in r.text and [p["delivery"] for p in cloud.sent("setup-link")] == ["copy"]
    r = await ui.post(f"/subscriptions/{eid}/collection/revoke")
    _not_refused(r)
    if failing is not None:
        failing.clear()
    assert (await ui.api.get(f"/subscriptions/{eid}/collection")).json()["binding"]["state"] == "revoked"


SCENARIOS = {
    "POST /subscriptions/generate-due": _generate_all_due,
    "POST /subscriptions/catch-up/{action}": _catch_up,
    "POST /subscriptions/summary/ack": _summary_ack,
    "POST /subscriptions/new": _new,
    "POST /subscriptions/{entity_id}/amend": _amend,
    "POST /subscriptions/{entity_id}/{action}": _lifecycle,
    "POST /subscriptions/{entity_id}/periods/{k}/{action}": _period,
    "POST /subscriptions/{entity_id}/history/{doc_id}/{action}": _history,
    "PATCH /subscriptions/{entity_id}/schedule/{field}": _schedule,
    "POST /subscriptions/import/confirm": _import,
    "POST /donors/statements/send": _donor_statements,
    "PUT /donors/statements/wording": _donor_wording,
    "POST /subscriptions/{entity_id}/collection/{action}": _collection,
}


@pytest.mark.parametrize("route", [r.key for r in _ROWS])
async def test_every_action_answer_matches_what_is_stored(owner_ui, session, monkeypatch, route):
    """Census row by census row: a route with no scenario fails here."""
    assert route in SCENARIOS, f"{route} answers an action and has no scenario"
    await SCENARIOS[route](owner_ui, session, monkeypatch, lambda: None)


@pytest.mark.parametrize("route", [r.key for r in _ROWS if r.reads or r.shared or r.bare])
async def test_a_read_failing_after_an_action_never_reports_it_refused(
        owner_ui, session, monkeypatch, reads_fail_after_action, route):
    """The action worked; the redraw read that follows fails (503). The answer still says what
    the action did (P2c-3), with a warning to reload rather than a refusal."""
    await SCENARIOS[route](owner_ui, session, monkeypatch, reads_fail_after_action)


def test_no_read_after_an_action_sits_outside_redrawn():
    """Static half of the rule, over the census: a read after the action inside the action's
    refusal handler reports a done action as refused; outside any handler it fails the request."""
    assert [(r.key, r.shared, r.bare) for r in _ROWS if r.shared or r.bare] == []


def test_every_ui_action_answers_by_loading_its_page_redrawing_or_refusing():
    """Every return of every UI action route the census finds answers through _done (the page
    loads again with a toast saying what happened), _redrawn, a refusal, a whole page or the
    shared import wizard; never a redirect the browser does not follow, or none (P4g-1)."""
    routes = status_census.ui_actions()
    assert routes and all(r.answers for r in routes)
    assert [(r.key, a) for r in routes for a in r.answers
            if a.split("@")[0] not in status_census.ALLOWED_ANSWERS] == []


@pytest.mark.parametrize("source, answer", [
    ("return _done(page, 'subscriptions.done_created')", "_done"),
    ("return _done(f'/subscriptions/{eid}#periods', 'subscriptions.done_created')", "fragment"),
    ("return await _redrawn(request, eid)", status_census.REDRAW),
    ("return await _refused_redrawn(e, stored)", status_census.REFUSED_REDRAW),
    ("return _refused(exc)", "_refused"),
    ("return base_shell(title, body)", "page"),
    ("return wizard_step(request)", "wizard"),
    ("return RedirectResponse('/login', status_code=302)", "signed_out"),
    ("return HTMLResponse('', status_code=404)", "other"),
    ("return HTMLResponse(status_code=404)", "not_found"),
    ("return HTMLResponse('', headers={'HX-Redirect': page})", "other"),
    ("return None", "other"),
])
def test_the_census_names_each_way_a_route_can_answer(source, answer):
    """The census tells a full page load from a fragment the page does not reload for, a page
    from a wizard step, and anything else (a redirect the browser does not follow, no answer)
    from the answers it allows."""
    import ast
    assert status_census._answer(ast.parse(source).body[0], {"wizard_step"}) == answer


def test_every_api_action_is_shown_through_a_ui_route():
    """Each API action's answer reaches the user through a UI route the scenarios cover."""
    assert [a.path for a in status_census.api_actions() if not a.reached_by] == []


def test_no_document_badge_decides_alone_how_its_agreement_bills_it():
    """A document's own status cannot say whether its period is billed again (P4c-1): a
    badge render reads no document status; what the agreement holds is read on the page."""
    assert [b.render for b in status_census.badge_renders() if b.reads_doc_status] == []


def test_no_action_call_shares_a_refusal_with_an_earlier_one():
    """An action called again in one request (a later call, or a later turn of a loop) under the
    refusal of an earlier one reports the done work as refused when it fails (P2e-2): each such
    call answers for itself, beside what the earlier ones did."""
    assert [(r.key, r.chained) for r in _ROWS if r.chained] == []


def test_one_writer_records_each_send():
    """A period's send record is written in one place, which stores who pressed Send (P4d-1)."""
    assert {w.function for w in status_census.send_writers()} == {status_census.SEND_WRITER}


# Each writer of an agreement's billing failure, and the test that runs it: run_due writes it
# and drops one that no longer holds (paused), write_agreement drops it with the change that ends
# it (its period skipped or billed, the agreement cancelled). The summary is its one reader
# outside billing, and the UI's _to_check the one reader of the kinds counted to check.
FAILURE_WRITERS = {
    ("billing.run_due", "writes"): "test_a_second_different_reason_replaces_the_first",
    ("billing._drop_failure", "drops"): "test_a_failure_ended_another_way_leaves_no_row",
    ("billing.write_agreement", "drops"): "test_a_failure_ended_another_way_leaves_no_row",
}
FAILURE_READERS = {"summary._rows": "test_a_failure_still_happening_stays_on_the_summary_and_counted_past_the_window"}
TO_CHECK_READERS = {"subscriptions._to_check": "test_a_failed_bill_keeps_its_reason_is_counted_to_check_and_offers_generate_due"}


def test_the_billing_failure_has_the_writers_and_readers_its_tests_walk():
    """The failure is one current fact per agreement (SEND-STATE): a writer or reader the census
    finds that this table does not name fails here until a test runs it."""
    import test_unattended_billing

    uses = status_census.failure_uses()
    assert {(fn, how) for _w, fn, how in uses if how != "reads"} == set(FAILURE_WRITERS)
    assert {fn for _w, fn, how in uses if how == "reads" and not fn.startswith("billing.")} == set(FAILURE_READERS)
    assert {fn for _w, fn in status_census.to_check_readers()} == set(TO_CHECK_READERS)
    walked = {*FAILURE_WRITERS.values(), *FAILURE_READERS.values(), *TO_CHECK_READERS.values()}
    assert [name for name in walked if not callable(getattr(test_unattended_billing, name, None))] == []


def test_every_send_on_a_persons_behalf_names_the_attempt():
    """A person's Send names the attempt they saw, so a repeat of the same click (a second tab,
    a double click, a retried request) is answered as stored, never sent again (P2e-4)."""
    doors = status_census.deliver_doors()
    assert [d for d in doors if d.on_behalf]
    assert [d.caller for d in doors if d.on_behalf and not d.names_attempt] == []


def test_one_reader_says_whether_a_document_was_emailed():
    """Whether a document was emailed is read in one place, from what the doc.sent projection
    stores for every caller (P4d-2)."""
    assert {r.function for r in status_census.emailed_readers()} == {status_census.EMAILED}

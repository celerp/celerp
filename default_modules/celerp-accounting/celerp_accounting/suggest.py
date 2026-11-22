# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The suggestion ladder for bank statement lines. Pure, no database.

Each rung answers from its own evidence and the first rung that answers a line wins:
0 a bank reversal pair, 1 a book entry on the bank (one, or a few of one counterparty that
add up), then a client's answer that named a contact, 2 an open invoice or bill of the same amount, 3 a rule, 4 a transfer from another
of the company's banks, 5 a read receipt, 6 history on the same counterparty, 7 the model
for the groups nothing else answered, 8 a question. A rung only proposes: it never posts or
matches. Every suggestion passes ``statement_lines.suggestion_value``, which keeps a contact
only when a record names it.

``model_rung(groups, accounts)`` is the model's rung, ``None`` when the model cannot run;
it returns ``{group key: {account_code, reason}}`` (or an awaitable of it).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from celerp_accounting import matcher
from celerp_accounting.statement_lines import suggestion_value
from ui.i18n import t

NO_CONTACT_REASON = str(t("acct.recon_no_contact_named", "en"))

# Sources whose suggestion a group may start ticked with, each with the evidence key naming the one
# record it rests on: the entry ``identified`` proved, the document, the rule, the slip, the history
# key. A reversal or a transfer pair takes two statement lines to be one transaction, which only the
# user confirms, and an open document of the same amount ("document_amount") names no payment:
# equal money is no identity.
TICKED_EVIDENCE = {"match": "identifier", "document": "doc_id", "rule": "rule_id", "slip": "file_id",
                   "history": "history"}

MODEL_BATCH = 100
_PAIR_DAYS = 3
_COMBO_DAYS = 7
_HISTORY_MIN = 2


@dataclass
class LadderContext:
    """What the rungs read: the bank's unheld book entries, open documents, active rules,
    open lines on the company's other banks, the company's statement lines that carry a reference
    (``References``, what ``identified`` checks against), read receipts, history per
    counterparty key or account, the posting accounts, and the model (or None)."""
    bank_id: str
    entries: list = field(default_factory=list)
    open_docs: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    pair_lines: list = field(default_factory=list)
    references: "References" = field(default_factory=lambda: References([]))
    slips: list = field(default_factory=list)
    history: dict = field(default_factory=dict)
    accounts: list = field(default_factory=list)
    model: Callable | None = None
    general_expense: str | None = None


def _d(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _days(a, b) -> int:
    da, db = _d(a), _d(b)
    return 9999 if da is None or db is None else abs((da - db).days)


def _amt(value) -> Decimal:
    return Decimal(str(value))


def _key(line: dict) -> str:
    return line.get("counterparty_key") or line.get("key") or ""


def direction(amount) -> str:
    return "in" if _amt(amount) > 0 else "out"


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _reversals(lines: list[dict]) -> dict:
    out: dict = {}
    for a in lines:
        if a["id"] in out:
            continue
        for b in lines:
            if b["id"] == a["id"] or b["id"] in out:
                continue
            if _key(a) == _key(b) and _amt(a["amount"]) == -_amt(b["amount"]) and \
                    _days(a["line_date"], b["line_date"]) <= _PAIR_DAYS:
                for x, y in ((a, b), (b, a)):
                    out[x["id"]] = {"kind": "no_entry", "source": "reversal", "paired_line_id": y["id"],
                                    "reason": t("acct.recon_reason_reversal")}
                break
    return out


def _matches(lines: list[dict], ctx: LadderContext) -> dict:
    out: dict = {}
    found = matcher.auto_match([{**l, "amount": _amt(l["amount"])} for l in lines], ctx.entries)
    by_id = {e["je_id"]: e for e in ctx.entries}
    used = set()
    by_line = {l["id"]: l for l in lines}
    # The entry a line's identifier proves is that line's match, whichever the matcher ranked first;
    # an entry so proved is no other line's ranked match.
    owned = {}
    for line_id, _je, _c in found:
        line = by_line[line_id]
        near = twins(line["amount"], line["line_date"], ctx.entries)
        pick = own_twin(line_id, line.get("reference"), near, ctx.references)
        if pick is not None:
            owned[line_id] = near[pick]["je_id"]
    for line_id, je_id, confidence in found:
        if line_id in owned:
            je_id = owned[line_id]
        elif je_id in owned.values():
            continue
        e = by_id[je_id]
        used.add(je_id)
        out[line_id] = {"kind": "match", "source": "match", "je_ids": [je_id], "confidence": confidence,
                        "reason": t("acct.recon_reason_match", date=str(e.get("date") or e.get("ts") or "")[:10])}
        if line_id in owned:
            out[line_id]["evidence"] = {"identifier": True}
    # One line paid by up to three entries of one counterparty (or three lines of one
    # counterparty for one entry), within a week: a low suggestion, never a match.
    free = [e for e in ctx.entries if e["je_id"] not in used]
    for line in lines:
        if line["id"] in out:
            continue
        near = [e for e in free if e.get("contact") and _days(e.get("date"), line["line_date"]) <= _COMBO_DAYS
                and direction(e["amount"]) == direction(line["amount"])]
        by_contact: dict = {}
        for e in near:
            by_contact.setdefault(e["contact"], []).append(e)
        for group in by_contact.values():
            if 2 <= len(group) <= 3 and sum(_amt(e["amount"]) for e in group) == _amt(line["amount"]):
                ids = [e["je_id"] for e in group]
                used.update(ids)
                free = [e for e in free if e["je_id"] not in ids]
                out[line["id"]] = {"kind": "match", "source": "match", "je_ids": ids, "confidence": "low",
                                   "reason": t("acct.recon_reason_combo")}
                break
    by_group: dict = {}
    for line in lines:
        if line["id"] not in out:
            by_group.setdefault((_key(line), direction(line["amount"])), []).append(line)
    for group in by_group.values():
        if not 2 <= len(group) <= 3 or _key(group[0]).startswith("SPARSE:"):
            continue
        total = sum(_amt(l["amount"]) for l in group)
        for e in free:
            if _amt(e["amount"]) == total and all(_days(e.get("date"), l["line_date"]) <= _COMBO_DAYS for l in group):
                free = [x for x in free if x["je_id"] != e["je_id"]]
                for l in group:
                    out[l["id"]] = {"kind": "match", "source": "match", "je_ids": [e["je_id"]], "confidence": "low",
                                    "reason": t("acct.recon_reason_combo")}
                break
    return out


def _document(line: dict, ctx: LadderContext) -> dict | None:
    """The open invoice or bill a line pays in full. It is the line's own ("document") only when
    ``identified`` proves it: the line's reference is the document's identifier. Otherwise the
    first one open for the same amount is offered ("document_amount"), never ticked."""
    want = "invoice" if _amt(line["amount"]) > 0 else "bill"
    money = abs(_amt(line["amount"]))
    docs = [d for d in ctx.open_docs if d.get("doc_type") == want and _amt(d["open_balance"]) == money]
    if not docs:
        return None
    pick = identified(line.get("reference"), [d.get("identifier") for d in docs],
                      ctx.references.elsewhere({str(line["id"])}))
    doc = docs[0] if pick is None else docs[pick]
    return {"kind": "pay_doc", "source": "document_amount" if pick is None else "document",
            "contact_id": doc.get("contact_id"), "docs": [{"doc_id": doc["doc_id"], "amount": _money(money)}],
            "evidence": {"names_contact": True, "doc_id": doc["doc_id"]},
            "reason": t("acct.recon_reason_document", ref=doc.get("ref") or doc["doc_id"])}


def _rule(line: dict, ctx: LadderContext) -> dict | None:
    probe = {**line, "counterparty_key": _key(line), "status": "unmatched"}
    hit = matcher.apply_rules([probe], ctx.rules, ctx.bank_id)
    if not hit:
        return None
    rule = next(r for r in ctx.rules if r["id"] == hit[0][1])
    s = {"source": "rule", "rule_id": str(rule["id"]), "contact_id": rule.get("contact_id"),
         "evidence": {"names_contact": True, "rule_id": str(rule["id"])},
         "reason": t("acct.recon_reason_rule", pattern=rule.get("match_pattern") or "")}
    if rule.get("target_bank_id"):
        return {**s, "kind": "transfer", "target_bank_id": str(rule["target_bank_id"])}
    return {**s, "kind": "post", "account_code": rule["target_account_code"]}


def transfer_pairs(want: Decimal, line_date, candidates: list[dict]) -> list[dict]:
    """The other banks' open lines a transfer of ``want`` dated ``line_date`` could be: exactly that
    money within the pairing window, nearest first. Which one, if any, only the user says."""
    near = [c for c in candidates if _amt(c["amount"]) == want and _days(c["line_date"], line_date) <= _PAIR_DAYS]
    return sorted(near, key=lambda c: (_days(c["line_date"], line_date), str(c["id"])))


def normalised(reference: str | None) -> str | None:
    """``reference`` as identity compares it: its whole text, spaces collapsed and casefolded, or
    None when empty. Its shape proves nothing; ``identified`` alone decides."""
    return " ".join((reference or "").split()).casefold() or None


def identified(reference: str | None, numbers: list, elsewhere: Callable[[str], bool]) -> int | None:
    """The one rule for transaction identity, a statement line to a book entry: the index of the
    one of ``numbers`` the line's whole reference equals, normalised, or None. ``numbers`` are the
    candidate entries' document numbers (the number of the one document each pays, carried by no
    other document: see bank_statements._entry_identifiers), and it holds only while no other
    statement line of the company, at any date, carries that number (``elsewhere``, see
    ``References.elsewhere``). A document number counts whatever its shape; a reference that is
    no such number never proves identity, and two statement lines are never one transaction on
    their own (a reversal or a transfer pair is offered for the user to confirm). Equal money, a
    near date, a shared account, a shared contact or a word a memo shares never prove it (one
    supplier is paid many times); they only rank the candidates the user is shown."""
    ident = normalised(reference)
    if ident is None:
        return None
    hits = [i for i, n in enumerate(numbers) if normalised(n) == ident]
    return hits[0] if len(hits) == 1 and not elsewhere(ident) else None


def own_twin(line_id, reference: str | None, near: list[dict], references: "References") -> int | None:
    """The index of the one of ``near`` (a line's candidate entries from ``twins``, each with its
    ``identifier``) that ``identified`` proves is the line's own, or None: the one composition the
    ladder and a post's keyed-instead check share. A statement line already matched to one of the
    candidates is that entry's own reading (a transfer counted on the other bank), never another
    line carrying its number."""
    owners = {str(l["id"]) for l in references.matched_to({e["je_id"] for e in near})}
    return identified(reference, [e.get("identifier") for e in near], references.elsewhere({str(line_id)} | owners))


class References:
    """The company's statement lines that carry a reference (``{id, bank_account_id, line_date,
    reference, matched_je_ids}``), indexed by normalised reference and by the entries they are
    matched to."""

    def __init__(self, lines: list[dict]):
        self.lines = lines
        self._by_ident: dict[str, list[dict]] = {}
        self._by_je: dict[str, list[dict]] = {}
        for line in lines:
            if (ident := normalised(line.get("reference"))) is not None:
                self._by_ident.setdefault(ident, []).append(line)
            for je_id in line.get("matched_je_ids") or ():
                self._by_je.setdefault(je_id, []).append(line)

    def matched_to(self, je_ids: set) -> list[dict]:
        """The lines matched to any of ``je_ids``."""
        return list({id(l): l for j in je_ids for l in self._by_je.get(j, ())}.values())

    def carrying(self, idents) -> list[dict]:
        """The lines whose reference is one of ``idents``."""
        return [l for i in set(idents) - {None} for l in self._by_ident.get(i, ())]

    def elsewhere(self, skip: set) -> Callable[[str], bool]:
        """Whether a line not in ``skip`` (ids) carries a normalised reference: the ``elsewhere``
        of ``identified``."""
        return lambda ident: any(str(l["id"]) not in skip for l in self._by_ident.get(ident, ()))


def rank(want: dict, entries: list[dict]) -> list[dict]:
    """``entries`` (each with ``evidence``) in the order the user is shown them: those sharing more of
    ``want``'s contacts and accounts first, then the nearest (their order in)."""
    def shared(e: dict) -> int:
        ev = e.get("evidence") or {}
        return -(bool(set(want.get("contacts") or ()) & set(ev.get("contacts") or ())) +
                 bool(set(want.get("accounts") or ()) & set(ev.get("accounts") or ())))
    return sorted(entries, key=shared)


def twins(amount, line_date, entries: list[dict]) -> list[dict]:
    """The book entries a line's money could already be: exactly its amount, the same way, dated
    within the matcher's exact-amount window, nearest first. Only ``identified`` decides which of
    them, if any, is the line's own entry."""
    near = [e for e in entries if _amt(e["amount"]) == _amt(amount)
            and _days(e.get("date") or e.get("ts"), line_date) <= matcher.EXACT_DAYS]
    return sorted(near, key=lambda e: (_days(e.get("date") or e.get("ts"), line_date), e["je_id"]))


def _transfer(line: dict, ctx: LadderContext) -> dict | None:
    pairs = transfer_pairs(-_amt(line["amount"]), line["line_date"],
                           [p for p in ctx.pair_lines if str(p.get("bank_id")) != str(ctx.bank_id)])
    banks = {str(p["bank_id"]) for p in pairs}
    if len(banks) != 1:
        return None
    s = {"kind": "transfer", "source": "transfer", "target_bank_id": banks.pop(),
         "reason": t("acct.recon_reason_transfer", date=str(pairs[0]["line_date"])[:10])}
    return s


def _slip(line: dict, ctx: LadderContext) -> dict | None:
    if _amt(line["amount"]) >= 0:
        return None
    amount = abs(_amt(line["amount"]))
    for slip in ctx.slips:
        if slip.get("total") is None or _amt(slip["total"]) != amount or _days(slip.get("date"), line["line_date"]) > _PAIR_DAYS:
            continue
        hist = _history(line, ctx)
        s = {"kind": "post", "source": "slip", "file_id": slip.get("file_id"),
             "account_code": (hist or {}).get("account_code") or ctx.general_expense,
             "contact_id": slip.get("party_contact_id"),
             "evidence": {"names_contact": True, "file_id": slip.get("file_id")},
             "reason": t("acct.recon_reason_slip", date=str(slip.get("date") or "")[:10])}
        tax = _amt(slip["tax"]) if slip.get("tax") not in (None, "") else Decimal(0)
        if slip.get("tax_id") and slip.get("is_tax_invoice") and Decimal(0) < tax < amount:
            s["vat"] = {"tax": _money(tax), "net": _money(amount - tax)}
        else:
            s["reason"] = f"{s['reason']} {t('acct.recon_reason_no_vat')}"
        return s
    return None


def _history(line: dict, ctx: LadderContext) -> dict | None:
    key = _key(line)
    rows = ctx.history.get(key) or (ctx.history.get(line.get("counterparty_account")) if line.get("counterparty_account") else None) or []
    sparse = key.startswith("SPARSE:")
    if sparse:
        rows = [r for r in rows if r.get("amount") is not None and _amt(r["amount"]) == _amt(line["amount"])]
    counts = Counter(r["account_code"] for r in rows if r.get("account_code"))
    if not counts:
        return None
    account, n = counts.most_common(1)[0]
    if n < _HISTORY_MIN:
        return None
    s = {"kind": "post", "source": "history", "account_code": account,
         "reason": t("acct.recon_reason_history", account=account, count=n)}
    if not sparse:
        contacts = Counter(r.get("contact_id") for r in rows if r.get("account_code") == account and r.get("contact_id"))
        if contacts:
            s["contact_id"] = contacts.most_common(1)[0][0]
            s["evidence"] = {"names_contact": True, "history": key}
    return s


def client_said(answer: str) -> str:
    return t("acct.recon_reason_client", answer=answer.strip().rstrip(".!? "))


def answer_suggestion(client: dict) -> dict:
    """What a client's answer naming a contact suggests: that contact and the account its
    last posted line went to."""
    return {"kind": "post", "source": "answer", "account_code": client["account_code"],
            "contact_id": client["contact_id"], "evidence": {"names_contact": True, "answer": True},
            "reason": client_said(client["answer"])}


def _answer(line: dict, ctx: LadderContext) -> dict | None:
    client = line.get("client") or {}
    return answer_suggestion(client) if client.get("account_code") else None


def ladder_rungs(lines: list[dict], ctx: LadderContext) -> tuple[dict, list[dict]]:
    """Rungs 0 to 6: ({line id: suggestion}, residue groups for the model). A residue group
    is ``{key, direction, count, total, sample}`` for lines no rung answered."""
    out = _reversals(lines)
    rest = [l for l in lines if l["id"] not in out]
    out.update(_matches(rest, ctx))
    for line in lines:
        if line["id"] in out:
            continue
        for rung in (_answer, _document, _rule, _transfer, _slip, _history):
            s = rung(line, ctx)
            if s:
                out[line["id"]] = s
                break
    residue: dict = {}
    for line in lines:
        if line["id"] in out:
            continue
        answer = (line.get("client") or {}).get("answer")
        key = f"ANSWER:{line['id']}" if answer else _key(line)  # an answer speaks for its own line
        g = residue.setdefault(key, {"key": key, "direction": direction(line["amount"]), "count": 0,
                                     "total": Decimal(0), "sample": line["description"], "line_ids": [],
                                     "answer": answer})
        g["count"] += 1
        g["total"] += _amt(line["amount"])
        g["line_ids"].append(line["id"])
    return out, list(residue.values())


def model_batches(residue: list[dict]) -> list[list[dict]]:
    """The residue groups the model sees, at most ``MODEL_BATCH`` per call."""
    shown = [{"key": g["key"], "direction": g["direction"], "count": g["count"], "total": _money(g["total"]),
              "sample": g["sample"], **({"client_said": g["answer"]} if g.get("answer") else {})} for g in residue]
    return [shown[i:i + MODEL_BATCH] for i in range(0, len(shown), MODEL_BATCH)]


def apply_model(out: dict, residue: list[dict], replies: dict, accounts: list[str]) -> None:
    """Rung 7 from the model's replies (an account from the list or nothing, never a
    contact), then rung 8 for every line still unanswered."""
    allowed = {str(a) for a in accounts}
    for g in residue:
        reply = replies.get(g["key"]) or {}
        account = str(reply.get("account_code") or "")
        said = client_said(g["answer"]) if g.get("answer") else ""
        for line_id in g["line_ids"]:
            if account in allowed:
                out[line_id] = {"kind": "post", "source": "model", "account_code": account,
                                "reason": f"{said} {str(reply.get('reason') or '').strip()[:300]}".strip()}
            else:
                out[line_id] = {"kind": None, "source": "question",
                                "reason": said or t("acct.recon_reason_question")}


def finish(out: dict) -> dict:
    """Every suggestion through the one cleaning rule."""
    return {line_id: suggestion_value(s) for line_id, s in out.items()}


def run_ladder(lines: list[dict], ctx: LadderContext) -> dict:
    """The whole ladder with a synchronous model (or none)."""
    out, residue = ladder_rungs(lines, ctx)
    replies: dict = {}
    if ctx.model is not None and residue:
        for batch in model_batches(residue):
            replies.update(ctx.model(batch, ctx.accounts) or {})
    apply_model(out, residue, replies, ctx.accounts)
    return finish(out)


# ── The model's rung ────────────────────────────────────────────────────────

@dataclass
class ModelCall:
    """Who the model rung asks for: the request's company, user and key."""
    company_id: uuid.UUID
    user_id: uuid.UUID
    key: str


current_call: contextvars.ContextVar[ModelCall | None] = contextvars.ContextVar("bank_model_call", default=None)

_MODEL_PROMPT = (
    "You classify groups of bank statement lines for bookkeeping. For each group, pick the one "
    "posting account from the list that fits best, or none when nothing fits. Never name a "
    "customer or vendor. A group with client_said is one line the client explained; go by what "
    "they said. Reply with JSON only: {\"<group key>\": {\"account_code\": \"<code or null>\", "
    "\"reason\": \"<one short sentence>\"}}."
)


_MODEL_HINTS = {"query": "bank suggestions", "file_count": 0, "is_batch": False}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def model_step(call: ModelCall, groups: list[dict], accounts: list[str]) -> tuple[uuid.UUID, list[dict]]:
    """(identity, messages) of one model step for ``groups``. The groups and accounts are
    put in one order and serialised one way, so the same input in any order is sent byte
    for byte the same; the identity is the request (company and key) plus a hash of all
    that is sent, so a retry of the same input is the same step (its kept reply answers it)
    and any change to what the model sees is a new step. The service can never be sent two
    contents under one identity."""
    shown = {"accounts": sorted(str(a) for a in accounts), "groups": sorted(groups, key=_canonical)}
    messages = [{"role": "system", "content": _MODEL_PROMPT}, {"role": "user", "content": _canonical(shown)}]
    digest = hashlib.sha256(_canonical({"messages": messages, "hints": _MODEL_HINTS}).encode()).hexdigest()
    return uuid.uuid5(uuid.NAMESPACE_URL, f"celerp:bank-suggest:{call.company_id}:{call.key}:{digest}"), messages


class ModelStepRunning(Exception):
    """The same model step, sent first by another run of the request, was still running
    when this run's synchronous wait (``deadlines.SYNC_WAIT_S``) ran out."""


async def ask_model(groups: list[dict], accounts: list[str]) -> dict:
    """One model step for ``groups`` under its identity (``model_step``: a replay is not
    charged again), sent the way the assistant sends one: inside an ``interactive`` slot,
    through ``send_step``, with its usage row committed pending before each send
    (``llm.complete``'s ``before_send``) and settled with what the service reports once
    it answers. A reply lost after the service charged leaves the row pending for the
    background reconcile pass. While the same step sent by another run of the request
    still runs, it is asked again after the service's ``retry_after`` and answered the
    first run's reply; past the synchronous wait it raises ModelStepRunning. Raises
    RelayError operation_mismatch as it comes (``model_step`` makes it impossible; the
    caller refuses it rather than counting it a model failure), and whatever else stops
    the model."""
    import time

    from celerp.ai import deadlines, jobs, llm, usage

    call = current_call.get()
    if call is None:
        return {}
    op, messages = model_step(call, groups, accounts)
    since: list[float] = []

    def _running(exc) -> None:
        if exc.code == "operation_in_progress":
            since.append(time.monotonic())
            if since[-1] - since[0] >= deadlines.SYNC_WAIT_S:
                raise ModelStepRunning from exc

    async def _record(**reported) -> None:
        async with jobs.SessionLocal() as session:
            await usage.record_usage(session, company_id=call.company_id, user_id=call.user_id, operation_id=op,
                                     **reported)
            await session.commit()

    async def _before_send(nbytes: int) -> None:
        await _record(send_bytes=nbytes)

    async with jobs.gate.slot("interactive"):
        result = await jobs.send_step(
            lambda: llm.complete(messages, operation_id=op, hints=_MODEL_HINTS, before_send=_before_send),
            resend=False, on_refusal=_running)
    known = result.operation_credits is not None
    await _record(credits=result.operation_credits if known else int((result.usage or {}).get("credits") or 0),
                  known=known)
    try:
        reply = json.loads((result.message.get("content") or "").strip().strip("`").removeprefix("json"))
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


model_rung: Callable | None = ask_model

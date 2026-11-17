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
import json
import re
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

# Sources whose suggestion a group may start ticked with: each names its evidence.
TICKED_SOURCES = frozenset({"reversal", "match", "document", "rule", "transfer", "slip", "history"})

MODEL_BATCH = 100
_PAIR_DAYS = 3
_COMBO_DAYS = 7
_HISTORY_MIN = 2
_REFERENCE_MIN = 4


@dataclass
class LadderContext:
    """What the rungs read: the bank's unheld book entries, open documents, active rules,
    open lines on the company's other banks, read receipts, history per counterparty key or
    account, the posting accounts, and the model (or None)."""
    bank_id: str
    entries: list = field(default_factory=list)
    open_docs: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    pair_lines: list = field(default_factory=list)
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
    for line_id, je_id, confidence in found:
        e = by_id[je_id]
        used.add(je_id)
        out[line_id] = {"kind": "match", "source": "match", "je_ids": [je_id], "confidence": confidence,
                        "reason": t("acct.recon_reason_match", date=str(e.get("date") or e.get("ts") or "")[:10])}
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
    want = "invoice" if _amt(line["amount"]) > 0 else "bill"
    for doc in ctx.open_docs:
        if doc.get("doc_type") == want and _amt(doc["open_balance"]) == abs(_amt(line["amount"])):
            return {"kind": "pay_doc", "source": "document", "contact_id": doc.get("contact_id"),
                    "docs": [{"doc_id": doc["doc_id"], "amount": _money(abs(_amt(line["amount"])))}],
                    "evidence": {"names_contact": True, "doc_id": doc["doc_id"]},
                    "reason": t("acct.recon_reason_document", ref=doc.get("ref") or doc["doc_id"])}
    return None


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
    money within the pairing window, nearest first. A transfer pairs itself only with the one of
    them ``same_transaction`` proves, never on amount and date alone."""
    near = [c for c in candidates if _amt(c["amount"]) == want and _days(c["line_date"], line_date) <= _PAIR_DAYS]
    return sorted(near, key=lambda c: (_days(c["line_date"], line_date), str(c["id"])))


def _reference_in(reference: str | None, text: str | None) -> bool:
    ref = (reference or "").strip().casefold()
    return len(ref) >= _REFERENCE_MIN and \
        re.search(rf"(?<!\w){re.escape(ref)}(?!\w)", (text or "").casefold()) is not None


def same_transaction(want: dict, have: dict) -> bool:
    """Whether ``have`` (a book entry, or another bank's line) is the very money ``want`` (a
    requested posting or transfer) names. Equal amount and a near date never prove it: two
    suppliers paid the same sum the same week are two payments. The proof is ``want``'s reference
    found in ``have``'s text; otherwise, when both name a counterparty, a shared one; otherwise a
    shared account. Each side reads ``reference``/``text`` (str), ``contacts`` and ``accounts``."""
    if _reference_in(want.get("reference"), have.get("text")):
        return True
    mine, theirs = set(want.get("contacts") or ()), set(have.get("contacts") or ())
    if mine and theirs:
        return bool(mine & theirs)
    return bool(set(want.get("accounts") or ()) & set(have.get("accounts") or ()))


def twins(amount, line_date, entries: list[dict]) -> list[dict]:
    """The book entries a line's money could already be: exactly its amount, the same way, dated
    within the matcher's exact-amount window, nearest first. Only ``same_transaction`` decides
    which of them, if any, is the line's own entry."""
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
    proven = [p for p in pairs if same_transaction({"reference": line.get("reference")}, {"text": p.get("reference")})]
    return {**s, "paired_line_id": str(proven[0]["id"])} if len(proven) == 1 else s


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
    """Who the model rung asks for: the request's session, company, user and key."""
    session: object
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


async def ask_model(groups: list[dict], accounts: list[str]) -> dict:
    """One model call for ``groups`` under the request's key (so a replay is not charged
    again), its usage recorded for the company. Raises when the model cannot be reached."""
    from celerp.ai import jobs, llm
    from celerp.ai.usage import record_usage

    call = current_call.get()
    if call is None:
        return {}
    op = uuid.uuid5(uuid.NAMESPACE_URL, f"celerp:bank-suggest:{call.company_id}:{call.key}:{groups[0]['key']}")
    messages = [{"role": "system", "content": _MODEL_PROMPT},
                {"role": "user", "content": json.dumps({"accounts": accounts, "groups": groups}, ensure_ascii=False)}]
    async with jobs.gate.slot("interactive"):
        result = await llm.complete(messages, operation_id=op, hints={"query": "bank suggestions", "file_count": 0,
                                                                        "is_batch": False})
    known = result.operation_credits is not None
    credits = result.operation_credits if known else int((result.usage or {}).get("credits") or 0)
    await record_usage(call.session, company_id=call.company_id, user_id=call.user_id, operation_id=op,
                       credits=credits, known=known)
    try:
        reply = json.loads((result.message.get("content") or "").strip().strip("`").removeprefix("json"))
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


model_rung: Callable | None = ask_model

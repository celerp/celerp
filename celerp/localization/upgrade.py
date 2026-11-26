# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The one-time upgrade of a company from before tax codes.

Its tax lists gain ids, rate versions and a category; a purchase row an earlier version saved
with a negative rate becomes a withholding code taken at the document. Documents whose lines
withheld through a negative tax keep what they owe: the withholding moves out of ``tax`` into
``withheld`` (amount_due is unchanged), and an issued supplier document is flagged
(``legacy_wht_entry``) until its posted entry is corrected. An item tax code name matching
exactly one code becomes that code's id. Each document and item change is a keyed ledger event
written beside its projection, so a replay builds the same state. What needs the company's
attention is stored as ``settings.upgrade_notice``; a company that holds one has been upgraded
and is left as it is, so running the upgrade again writes nothing.

The core is synchronous on a SQLAlchemy connection, so the alembic revision and a session
(through ``run_sync``) share it. JSON is read and written as text in Python, never cast on the
server, so it works on SQL_ASCII and UTF8 databases alike.
"""
from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa

from celerp.json_text import dump_existing_json
from celerp.migrations._json_compat import decode_json_value

SOURCE = "migration"
_LIST_KEYS = {"sales": "taxes", "purchase": "purchasing_taxes"}
_TEMPLATES = frozenset({"subscription_invoice", "subscription_po"})
_FLAGGED = frozenset({"bill", "debit_note"})


def _code_id(company_id, side: str, name: str) -> str:
    from celerp.tax_regimes import tax_name_key
    return "m-" + hashlib.sha256(f"{company_id}:{side}:{tax_name_key(name)}".encode()).hexdigest()[:12]


def _pack_category(pack: dict | None, name: str, rate: float):
    from celerp.tax_regimes import tax_name_key
    for code in (pack or {}).get("taxes") or ():
        if tax_name_key(code["name"]) == tax_name_key(name) and any(v["rate"] == rate for v in code["versions"]):
            return code["category"]
    return None


def _code_row(company_id, side: str, raw: dict, pack: dict | None) -> dict:
    """One list row of the earlier shape as a TaxCode row. A row already holding versions is
    kept as it is."""
    from celerp.localization.taxcodes import TaxCode
    if "versions" in raw:
        return dict(raw)
    row = {k: v for k, v in raw.items() if k in TaxCode.model_fields}
    rate = float(raw.get("rate") or 0)
    row["id"] = raw.get("id") or _code_id(company_id, side, raw["name"])
    if rate < 0:
        row.update(category="withholding", withholding_point="document")
        rate = -rate
    else:
        row["category"] = _pack_category(pack, raw["name"], rate)
    row["versions"] = [{"rate": rate}]
    return row


def _tax_list(company_id, side: str, rows: list[dict], pack: dict | None) -> list[dict]:
    """The stored list as normalized_list stores it. A blank name, or a name an earlier row
    already holds, was never reachable by name in the earlier version (every lookup found the
    first): such rows are left out."""
    from celerp.localization.taxcodes import normalized_list
    from celerp.tax_regimes import tax_name_key
    seen, kept = set(), []
    for raw in rows or ():
        key = tax_name_key(raw.get("name"))
        if key and key not in seen:
            seen.add(key)
            kept.append(_code_row(company_id, side, raw, pack))
    return normalized_list(kept)


def _chart(conn, company_id) -> dict[str, dict] | None:
    """The company's accounts as account_roles reads them, or None without a chart table."""
    if not sa.inspect(conn).has_table("accounts"):
        return None
    rows = conn.execute(sa.text(
        "SELECT a.code, a.account_type, a.is_active, EXISTS (SELECT 1 FROM accounts c WHERE "
        "c.company_id = a.company_id AND c.parent_code = a.code) AS has_children "
        "FROM accounts a WHERE a.company_id = CAST(:c AS uuid)"), {"c": str(company_id)}).mappings().all()
    return {r["code"]: dict(r) for r in rows}


def _default_country(conn, company_id) -> str:
    from celerp.tax_regimes import tax_country
    address = decode_json_value(conn.execute(sa.text(
        "SELECT CAST(address AS text) FROM locations WHERE company_id = CAST(:c AS uuid) AND is_default"),
        {"c": str(company_id)}).scalar())
    return tax_country(address.get("country")) if isinstance(address, dict) else ""


def _settings(conn, company_id, settings: dict) -> dict:
    """The company's settings in the new shape (lists, localization, country, roles)."""
    from celerp.accounting_roles import chart_from_elsewhere
    from celerp.localization import packs
    from celerp.services.account_roles import reconciled_settings
    out = dict(settings)
    seeded = out.pop("taxes_seeded_for", None)
    pack = packs().get(seeded) if seeded else None
    for side, key in _LIST_KEYS.items():
        out[key] = _tax_list(company_id, side, out.get(key) or [], pack)
    if seeded and not out.get("localization"):
        out["localization"] = {"country": seeded}
    if not out.get("country") and (country := _default_country(conn, company_id)):
        out["country"] = country
    accounts = None if chart_from_elsewhere(out) else _chart(conn, company_id)
    return out if accounts is None else reconciled_settings(out, accounts)


def _doc_change(state: dict, settings: dict) -> dict:
    """``{field: new}`` moving each negative line tax of ``state`` to a withholding taken at
    the document; empty when it has none."""
    from celerp.services.money import round_money, to_decimal, to_stored_float
    from celerp.tax_regimes import doc_side, tax_name_key
    side = doc_side(state.get("doc_type"))
    ids = {tax_name_key(c["name"]): c["id"] for c in settings.get(_LIST_KEYS[side]) or ()}
    currency = str(state.get("currency") or "USD")
    held, lines = to_decimal(0), []
    for line in state.get("line_items") or ():
        taxes = []
        for tax in line.get("taxes") or () if isinstance(line, dict) else ():
            if isinstance(tax, dict) and float(tax.get("rate") or 0) < 0:
                amount = abs(to_decimal(tax.get("amount") or 0))
                held += amount
                tax = {**tax, "rate": -float(tax["rate"]), "amount": to_stored_float(amount),
                       "category": "withholding", "withholding_point": "document",
                       "code_id": ids.get(tax_name_key(tax.get("code")))}
            taxes.append(tax)
        lines.append({**line, "taxes": taxes} if isinstance(line, dict) and "taxes" in line else line)
    if not held:
        return {}
    held = round_money(held, currency)
    change = {"line_items": lines,
              "tax": to_stored_float(round_money(to_decimal(state.get("tax") or 0) + held, currency)),
              "total": to_stored_float(round_money(to_decimal(state.get("total") or 0) + held, currency)),
              "withheld": to_stored_float(round_money(to_decimal(state.get("withheld") or 0) + held, currency))}
    if (state.get("doc_type") in _FLAGGED and state.get("status") not in ("draft", "void")
            and state.get("doc_type") not in _TEMPLATES):
        change["legacy_wht_entry"] = True
    return change


def _item_change(state: dict, settings: dict) -> dict:
    """``{"tax_codes": new}`` where a value names exactly one code (by tax_name_key); empty
    when nothing changes."""
    from celerp.tax_regimes import tax_name_key
    by_name: dict[str, list[str]] = {}
    for code in (*(settings.get("taxes") or ()), *(settings.get("purchasing_taxes") or ())):
        by_name.setdefault(tax_name_key(code["name"]), []).append(code["id"])
    old = state.get("tax_codes")
    if not isinstance(old, list):
        return {}
    new = [ids[0] if len(ids := by_name.get(tax_name_key(v), [])) == 1 else v for v in old]
    return {"tax_codes": new} if new != old else {}


def _unmatched(state: dict, settings: dict) -> int:
    known = {c["id"] for c in (*(settings.get("taxes") or ()), *(settings.get("purchasing_taxes") or ()))}
    return sum(1 for v in state.get("tax_codes") or () if v not in known)


def _write_event(conn, company_id, entity_id: str, entity_type: str, event_type: str, state: dict,
                 change: dict) -> None:
    """The keyed ``event_type`` row moving ``change`` onto ``state``, and the projection as its
    handler builds it: fields set and, for a document, amount_outstanding recomputed from what
    it owes (amount_due, unchanged by the move) less what was paid or credited."""
    new_state = {**state, **change}
    if entity_type == "doc":
        from celerp_docs.doc_projections import _payment_balances
        from celerp.services.money import to_stored_float
        new_state["amount_outstanding"] = to_stored_float(
            _payment_balances(new_state, new_state.get("amount_paid", 0))[1])
    fields = {k: {"old": state.get(k), "new": v} for k, v in change.items()}
    conn.execute(sa.text(
        "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key, "
        "metadata, ts) VALUES (CAST(:c AS uuid), :e, :t, :ev, CAST(:d AS json), :s, :k, CAST(:m AS json), now()) "
        "ON CONFLICT (company_id, idempotency_key) DO NOTHING"),
        {"c": str(company_id), "e": entity_id, "t": entity_type, "ev": event_type,
         "d": dump_existing_json({"fields_changed": fields}), "s": SOURCE, "k": f"tax-upgrade:{entity_id}",
         "m": json.dumps({"reason": "tax_upgrade"})})
    conn.execute(sa.text(
        "UPDATE projections SET state = CAST(:s AS json), version = version + 1, updated_at = now() "
        "WHERE company_id = CAST(:c AS uuid) AND entity_id = :e"),
        {"s": dump_existing_json(new_state), "c": str(company_id), "e": entity_id})


def _projections(conn, company_id, entity_type: str):
    rows = conn.execute(sa.text(
        "SELECT entity_id, CAST(state AS text) AS state FROM projections "
        "WHERE company_id = CAST(:c AS uuid) AND entity_type = :t ORDER BY entity_id"),
        {"c": str(company_id), "t": entity_type}).mappings().all()
    return [(r["entity_id"], decode_json_value(r["state"])) for r in rows]


def upgrade_connection(conn, company_id) -> dict:
    """Upgrade one company on ``conn`` and return its upgrade notice (the stored one, writing
    nothing, when the company was upgraded before)."""
    from celerp.services.account_roles import unmapped_roles
    raw = decode_json_value(conn.execute(sa.text(
        "SELECT CAST(settings AS text) FROM companies WHERE id = CAST(:c AS uuid) FOR UPDATE"),
        {"c": str(company_id)}).scalar()) or {}
    if "upgrade_notice" in raw:
        return raw["upgrade_notice"]
    settings = _settings(conn, company_id, raw)
    awaiting = 0
    for entity_id, state in _projections(conn, company_id, "doc"):
        if change := _doc_change(state, settings):
            _write_event(conn, company_id, entity_id, "doc", "doc.updated", state, change)
            awaiting += bool(change.get("legacy_wht_entry"))
    unmatched = 0
    for entity_id, state in _projections(conn, company_id, "item"):
        if change := _item_change(state, settings):
            _write_event(conn, company_id, entity_id, "item", "item.updated", state, change)
        unmatched += _unmatched({**state, **change}, settings)
    lists = (*settings["taxes"], *settings["purchasing_taxes"])
    settings["upgrade_notice"] = notice = {
        "country_missing": not settings.get("country"),
        "codes_without_category": sum(1 for c in lists if c["category"] is None),
        "unmapped_roles": unmapped_roles(settings),
        "bills_awaiting_correction": awaiting,
        "unmatched_item_tax_codes": unmatched}
    conn.execute(sa.text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = CAST(:c AS uuid)"),
                 {"s": dump_existing_json(settings), "c": str(company_id)})
    return notice


async def upgrade_company(session, company_id) -> dict:
    """upgrade_connection on the session's connection; the caller commits."""
    return await session.run_sync(lambda s: upgrade_connection(s.connection(), company_id))

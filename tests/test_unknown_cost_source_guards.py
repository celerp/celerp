# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Source guards for a lot's goods cost and a bill's lineage.

Each rule names the one place a job is done; a new reader, writer or walker elsewhere fails
here until it is added to the list on purpose."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from celerp.events.schemas import EVENT_SCHEMA_MAP
from celerp.services.goods_cost import GOODS_COST_KEYS, GOODS_COST_UNKNOWN, UNKNOWN_FLAG_EVENTS, unknown_flag_error
from celerp_inventory.projections import is_core_item_key

ROOT = Path(__file__).resolve().parents[1]
INV = "default_modules/celerp-inventory/celerp_inventory"
DOCS = "default_modules/celerp-docs/celerp_docs"
MFG = "default_modules/celerp-manufacturing/celerp_manufacturing"


def _sources():
    for top in ("celerp", "default_modules", "ui"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" not in rel and "/migrations/" not in rel:
                yield rel, path.read_text()


def _functions(tree: ast.AST):
    """(function name, node) for every function, nested ones included."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _enclosing(tree: ast.AST, line: int) -> str | None:
    """The innermost function holding ``line``."""
    best = None
    for name, node in _functions(tree):
        if node.lineno <= line <= node.end_lineno and (best is None or node.lineno > best[1]):
            best = (name, node.lineno)
    return best and best[0]


def _hits(pattern: re.Pattern) -> set[tuple[str, str | None]]:
    out = set()
    for rel, text in _sources():
        if pattern.search(text):
            tree = ast.parse(text)
            out |= {(rel, _enclosing(tree, text[:m.start()].count("\n") + 1)) for m in pattern.finditer(text)}
    return out


def _called(name: str) -> set[tuple[str, str | None]]:
    """(file, function) of every call to ``name``."""
    out = set()
    for rel, text in _sources():
        if name not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == name:
                out.add((rel, _enclosing(tree, node.lineno)))
    return out


def _body(rel: str, function: str) -> str:
    text = (ROOT / rel).read_text()
    [node] = [n for name, n in _functions(ast.parse(text)) if name == function]
    return ast.get_source_segment(text, node)


# ── goods cost ──────────────────────────────────────────────────────────────

def test_only_goods_basis_falls_back_from_cost_base_to_cost_total():
    """A reader that falls back by itself would read a cleared goods cost as the whole value.
    The projection's quantity handler keeps its own unrounded fallback: it replays old events
    and must scale a basis back exactly, which the rounded goods_basis would not."""
    fallback = re.compile(
        r"""get\(\s*["']cost_base["']\s*\)\s*(?:or|if)\b[^\n]*cost_total"""
        r"""|["']cost_base["']\s*\)\s*\n\s*if\s+\w+\s+is\s+None:\s*\n\s*\w+\s*=\s*\w+\.get\(\s*["']cost_total""")
    assert _hits(fallback) == {(f"{INV}/services.py", "goods_basis"), (f"{INV}/projections.py", "_set_quantity")}


def test_goods_added_to_a_lot_are_summed_only_by_topped_up_cost():
    added = re.compile(r"""(?:goods_basis\([^()]*\)|get\(\s*["']cost_base["']\s*\))\s*or\s*0(?:\.0)?\s*\)?\s*\+""")
    assert _hits(added) == set()
    assert _called("topped_up_cost") == {
        (f"{DOCS}/routes.py", "record_receipt"), (f"{INV}/migration_sink.py", "_adjust"), (f"{MFG}/movements.py", "_return")}


def test_only_goods_cost_empty_tells_an_empty_lot():
    empty = re.compile(r"recorded_value\([^\n]*\)\)?\s*-\s*landed")
    assert _hits(empty) == {(f"{INV}/services.py", "goods_cost_empty")}


def test_every_key_the_mark_writes_is_a_core_key():
    for key in (*GOODS_COST_KEYS, GOODS_COST_UNKNOWN):
        assert is_core_item_key(key), key


def test_the_event_boundary_refuses_the_mark_where_it_may_not_be_carried():
    item_events = {e for e in EVENT_SCHEMA_MAP if e.startswith("item.")}
    assert UNKNOWN_FLAG_EVENTS <= item_events
    for event in item_events - UNKNOWN_FLAG_EVENTS:
        assert unknown_flag_error(event, {GOODS_COST_UNKNOWN: True}), event
    for event in UNKNOWN_FLAG_EVENTS:
        assert unknown_flag_error(event, {GOODS_COST_UNKNOWN: True}) is None, event
    for key in GOODS_COST_KEYS:
        assert unknown_flag_error("item.patched", {key: 1.0}), key
    assert ("celerp/events/engine.py", "emit_event") in _called("unknown_flag_error")


def test_the_projection_reads_the_mark_only_from_event_data():
    """State never decides the mark: an event sets or clears it, so a replay rebuilds it."""
    tree = ast.parse((ROOT / f"{INV}/projections.py").read_text())
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            holder, key = node.value, node.slice
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get" and node.args:
            holder, key = node.func.value, node.args[0]
        elif isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            holder, key = node.comparators[0], node.left
        else:
            continue
        if (isinstance(key, ast.Name) and key.id == "GOODS_COST_UNKNOWN") or (
                isinstance(key, ast.Constant) and key.value == GOODS_COST_UNKNOWN):
            reads.append(getattr(holder, "id", ast.unparse(holder)))
    assert reads and set(reads) == {"data"}, reads


def test_the_merge_and_the_negative_cost_check_share_one_reading_of_a_cost_write():
    assert _called("written_goods_costs") >= {
        ("celerp/services/goods_cost.py", "writes_goods_cost"), ("celerp/services/goods_cost.py", "event_goods_costs")}
    assert (f"{INV}/services.py", "_merge_parts_of") in _called("writes_goods_cost")
    assert ("celerp/events/engine.py", "emit_event") in _called("event_goods_costs")


# Every place that writes a lot's birth. A lot that copies another lot's cost carries whether
# that goods cost is known (the function, or the helper named, reads it); the others start
# from a cost they are given.
_COPIES_COST = {
    (f"{INV}/routes.py", "split_item"): "split_item",
    (f"{INV}/routes.py", "split_off_child"): "split_off_child",
    (f"{INV}/routes.py", "transform_item"): "transform_item",
    (f"{INV}/routes.py", "merge_items"): "_plan_merge",
    (f"{DOCS}/routes.py", "receive_return"): "receive_return",
    (f"{DOCS}/routes.py", "record_historical_delivery"): "record_historical_delivery",
    (f"{DOCS}/routes.py", "record_receipt"): "record_receipt",
    (f"{MFG}/movements.py", "_receive"): "_receive",
}
_GIVEN_COST = {
    (f"{INV}/routes.py", "post_item"),
    (f"{INV}/services.py", "build_import_records"),
    (f"{INV}/services.py", "upsert_external_product"),
    (f"{INV}/services.py", "upsert_from_connector"),
    (f"{INV}/migration_sink.py", "_item_record"),
    ("celerp/services/demo.py", "_seed_items"),
}
_KNOWS = re.compile(r"GOODS_COST_UNKNOWN|goods_cost_known|carve_cost")


def _births() -> set[tuple[str, str | None]]:
    out = set()
    for rel, text in _sources():
        if '"item.created"' not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                named = any(k.arg == "event_type" and isinstance(k.value, ast.Constant)
                            and k.value.value == "item.created" for k in node.keywords)
                positional = getattr(node.func, "attr", None) == "emit" and any(
                    isinstance(a, ast.Constant) and a.value == "item.created" for a in node.args)
                if named or positional:
                    out.add((rel, _enclosing(tree, node.lineno)))
            elif isinstance(node, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "event_type"
                    and isinstance(v, ast.Constant) and v.value == "item.created"
                    for k, v in zip(node.keys, node.values)):
                out.add((rel, _enclosing(tree, node.lineno)))
    return out


def test_every_lot_birth_is_listed_and_a_copied_cost_carries_whether_it_is_known():
    assert _births() == set(_COPIES_COST) | _GIVEN_COST
    for (rel, _), reader in _COPIES_COST.items():
        assert _KNOWS.search(_body(rel, reader)), (rel, reader)


def test_a_duplicated_lot_copies_no_cost_it_does_not_know():
    assert _KNOWS.search(_body("ui/routes/inventory.py", "_duplicate_payload"))


# ── a bill's lineage ────────────────────────────────────────────────────────

def test_lot_lineage_walkers_have_named_callers_only():
    assert _called("lineage") == {
        ("celerp/services/auto_je.py", "_held_where"),
        (f"{DOCS}/consignment_buy.py", "_bought_lineage"),
        (f"{DOCS}/consignment_buy.py", "_plan"),
        (f"{DOCS}/routes.py", "_refuse_fixed_consignor"),
        (f"{DOCS}/routes.py", "return_consignment_items"),
        (f"{DOCS}/routes.py", "_bill_lineage"),
    }
    assert _called("_bill_lineage") == {
        (f"{DOCS}/routes.py", "revert_doc_to_draft"),  # the revert and its fold check
        (f"{DOCS}/routes.py", "_capitalise_landed_received"),  # the freight spread and its skip
    }
    assert _called("_split_descendants") == {(f"{DOCS}/routes.py", "_line_return_lots"),
                                             (f"{DOCS}/routes.py", "_split_since")}
    assert _called("_split_since") == {
        (f"{DOCS}/routes.py", "_family_entries"), (f"{DOCS}/routes.py", "_freight_sent_back"),
        (f"{DOCS}/routes.py", "_receipt_family"), (f"{DOCS}/routes.py", "return_consignment_items")}

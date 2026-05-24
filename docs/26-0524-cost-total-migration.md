# Cost Total Migration: `cost_price` → `cost_total` as Primitive

**Date:** 2026-05-24
**Status:** APPROVED - Ready to implement
**Author:** Jin
**Decisions:** Q1=A, Q2=A, Q3=C (all resolved, doc is deterministic)

---

## HOLY Commandments (apply to all implementation)

1. **DRY** - Every piece of cost logic exists once. No duplicated fallback patterns.
2. **SOLID** - Each function/method has one reason to change.
3. **No backward compatibility hacks** - The legacy `cost_price` path in `_flatten_item` and the projection handler is intentional dual-read for the event ledger. Everything else is clean.
4. **Deterministic** - No implicit fallbacks that change behavior depending on environment.
5. **No cruft** - Remove all dead code touched during this migration. If a variable, branch, or helper is rendered obsolete, delete it.
6. **KISS** - The fallback pattern `state.get("cost_total") or (state.get("cost_price") * qty)` is the ONLY fallback. Use it exactly, not variations.

---

## 1. Problem Statement

The system stores `cost_price` (per-unit) as the canonical primitive. `cost_total` is derived as `cost_price × quantity`.

This is semantically wrong for lot-based inventory:

- 10ct stone purchased for $1,000 → stored as `cost_price = $100/ct`
- User corrects qty to 9.8ct → `cost_total` silently becomes $980 (wrong; paid $1,000)
- Split form correctly uses `child_cost_total` but derives and stores `cost_price = total / qty`
- Valuation has a typo bug: `state.get("total_cost")` (never exists) instead of `state.get("cost_total")`

**The correct primitive for lot-based goods is `cost_total` (what was paid for the parcel).**
`cost_price` (per-unit) is derived as `cost_total / quantity` and is read-only everywhere.

---

## 2. Design Decision

**Store `cost_total`; derive `cost_price = cost_total / quantity`.**

| | Current | New |
|--|---------|-----|
| Stored primitive | `cost_price` (unit) | `cost_total` (parcel) |
| Derived | `cost_total = cost_price × qty` | `cost_price = cost_total / qty` |
| Editable | both | `cost_total` only; `cost_price` read-only |
| Qty edit effect | `cost_total` changes silently (bug) | `cost_total` unchanged (correct) |

**Event ledger backward compatibility:**
- Historical events have `price_type: "cost_price"` - projection keeps this path unchanged
- New events emit `price_type: "cost_total"` - projection stores `cost_total`, pops `cost_price`
- `_flatten_item` always derives both fields regardless of which is stored (dual read path)

**No DB migration. No backfill.** `_flatten_item` derivation handles legacy items at read time.

---

## 3. Implementation Spec

### Canonical fallback pattern (use exactly this, everywhere)

```python
cost_total = float(item.state.get("cost_total") or 0) or (
    float(item.state.get("cost_price") or 0) * float(item.state.get("quantity") or 0)
)
```

---

### 3.1 `projections.py` - `item.pricing.set` handler

Add `cost_total` branch. Keep existing `cost_price` branch unchanged (legacy read path).

```python
elif event_type == "item.pricing.set":
    pt = data["price_type"]
    price = data["new_price"]
    if pt == "cost_total":
        current["cost_total"] = price
        current.pop("cost_price", None)  # cost_price is now derived
    elif pt == "cost_price":
        current["cost_price"] = price   # legacy path - do NOT pop cost_total here
    else:
        current[pt] = price
```

---

### 3.2 `_flatten_item` in `routes.py`

Add cost derivation after existing flatten logic:

```python
qty = float(flat.get("quantity") or 0)
if flat.get("cost_total") is not None:
    flat["cost_price"] = round(float(flat["cost_total"]) / qty, 10) if qty else 0.0
elif flat.get("cost_price") is not None:
    flat["cost_total"] = round(float(flat["cost_price"]) * qty, 2)
# else: both remain absent (item has no cost set)
```

---

### 3.3 Valuation endpoint (`routes.py` ~line 412)

Fix typo. Change `state.get("total_cost")` to `state.get("cost_total")`. Keep `elif` fallback for legacy items.

```python
tc = state.get("cost_total")
if tc is not None:
    price_totals[pl_name] += Decimal(str(tc))
elif state.get(key) is not None:
    price_totals[pl_name] += Decimal(str(state[key])) * Decimal(str(qty))
```

---

### 3.4 `pick.py`

Line ~102: change `cost = float(item.get("cost_price") or 0)` to derive from `cost_total`:

```python
cost_total = float(item.get("cost_total") or 0)
item_qty = float(item.get("quantity") or 1)
cost = cost_total / item_qty if item_qty else 0.0
```

`PickLine.cost_price` field stays - it's a per-unit value used in COGS math downstream. Only the derivation changes.

---

### 3.5 Item creation API (`routes.py`)

- Add `cost_total: float | None = None` to `ItemCreateBody` (and CIF equivalent)
- If `cost_total` provided: emit `item.pricing.set` with `price_type="cost_total"`
- If only `cost_price` provided: emit with `price_type="cost_price"` (legacy path)
- RBAC role guard covers both `cost_price` and `cost_total`

---

### 3.6 Split route (`routes.py`)

**Parent cost:** use canonical fallback. After split, emit `item.pricing.set` for parent with remaining cost:

```python
parent_cost_total = float(parent.state.get("cost_total") or 0) or (
    float(parent.state.get("cost_price") or 0) * parent_qty
)
parent_remaining_cost = parent_cost_total - payload.child_cost_total
# emit item.pricing.set: price_type="cost_total", new_price=parent_remaining_cost for parent
```

**Child item:** store `cost_total` directly (not derived unit price):

```python
child_data["cost_total"] = payload.child_cost_total
# Remove: child_data["cost_price"] = payload.child_cost_total / payload.child_quantity  ← DELETE THIS
```

---

### 3.7 Transform route (`routes.py`)

Same pattern as split:

```python
parent_cost_total = float(parent.state.get("cost_total") or 0) or (
    float(parent.state.get("cost_price") or 0) * parent_qty
)
```

Child items: store `cost_total`, not `cost_price`. Remove any `cost_price` derivation for children.

---

### 3.8 Merge route (`routes.py`)

**Resulting cost = sum of source `cost_totals` (Q2=Option A).**

```python
merged_cost_total = sum(
    float(s.get("cost_total") or 0) or (float(s.get("cost_price") or 0) * float(s.get("quantity") or 0))
    for s in source_states
)
resulting_cost = payload.resulting_cost_total if payload.resulting_cost_total is not None else merged_cost_total
```

- Rename `MergeBody.resulting_cost_price` → `resulting_cost_total`
- Line ~1542: change `price_fields["cost_price"] = resulting_cost` → emit `item.pricing.set` with `price_type="cost_total"`
- Remove `weighted_cost` computation if it is now dead code (DELETE it)

---

### 3.9 `auto_je.py` (~line 624)

```python
cost_total = float(s.get("cost_total") or 0) or (
    float(s.get("cost_price") or s.get("cost price") or 0) * float(s.get("quantity") or 0)
)
# Use cost_total directly as the gap JE amount (do NOT multiply by qty again)
```

Remove any downstream `cost * qty` multiplication where `cost` was previously per-unit.

---

### 3.10 `celerp_docs/routes.py`

- Item lookup to populate doc line `cost_price`: derive `cost_price = item["cost_total"] / max(item["quantity"], 0.0001)` with fallback to stored `cost_price`
- Doc line `LineItemCreate.cost_price` stays as-is (frozen point-in-time unit cost - semantically correct)
- Reconciliation calc (~line 3137): prefer `r.get("cost_total")` with fallback to `cost_price * qty`

---

### 3.11 `celerp_reports/routes.py`

`_cogs_for_invoice()` already checks `cost_total` first. No change needed.

---

### 3.12 Doctor / Admin (`doctor.py`, `admin/routes.py`)

No backfill (Q3=Option C). Remove or do not add any cost backfill logic. The doctor should NOT emit `item.pricing.set` corrections for `cost_price` → `cost_total`. `_flatten_item` handles legacy items at read time.

If the doctor currently has any `cost_price` repair logic, leave it unchanged (it writes a `cost_price` event, which the legacy projection path handles correctly).

---

### 3.13 `warehousing/stock_receipts.py`

Line ~146: emit `item.pricing.set` for `cost_total = unit_price * quantity` instead of `cost_price = unit_price`, when quantity is known at receipt time. If quantity is unknown, keep `cost_price` legacy path.

---

### 3.14 Connectors (QB, Xero)

No change. They write `cost_price` events (legacy path). Document as known limitation. `_flatten_item` derives `cost_total = cost_price * qty` at read time.

---

### 3.15 `ui/api_client.py`

- Line 699: derive `cost_price` for doc line from item's `cost_total / qty` (fallback to stored `cost_price`)
- Lines 1363-1372: rename `resulting_cost_price` → `resulting_cost_total` in `merge_items()` call

---

### 3.16 `ui/routes/inventory.py`

#### A. Import confirm (~line 894)
`_price_total` strips `_total` suffix to get field name (`cost_price_total` → `cost_price`). For cost specifically, instead of storing derived unit price, store `cost_total` directly. Add a special case: if `unit_key == "cost_price"`, store as `cost_total` with the total value.

#### B. Virtual total cell render
If item has `cost_total`, display it directly. Fallback: `cost_price * qty` (legacy).

#### C. Virtual total cell edit (~line 1276)
When user edits `cost_price_total` cell, patch `cost_total` field (not `cost_price`). Add special case here.

#### D. Transform preview (~line 2014)
```python
parent_cost_total = float(item.get("cost_total") or 0) or round(float(item.get("cost_price") or 0) * parent_qty, 2)
```
Remove old `parent_cost_price` variable if no longer used (DELETE it).

#### E. Merge form (lines ~2601, 2608, 2629)
Rename `resulting_cost_price` → `resulting_cost_total` in form field name and API call.

#### F. `VIRTUAL_FOLLOWERS` JS (~line 3562)
Currently `cost_price_total` follows `cost_price` (total = derived follower).
New: `cost_price` follows `cost_price_total` (unit = derived read-only follower).
Invert only for cost. All other price list totals remain followers of their unit price.

---

### 3.17 `ui/components/table.py` - `VIRTUAL_FOLLOWERS`

Remove `cost_price_total` from the set of columns that follow `cost_price`.
Add `cost_price` as a column that follows `cost_price_total`.

---

### 3.18 `ui/routes/documents.py` (~lines 1093, 1145)

Item lookup for doc line: derive `cost_price = item["cost_total"] / max(item["quantity"] or 1, 0.0001)` with fallback to stored `cost_price`.

---

### 3.19 Seed data (`scripts/seed_demo.py`)

Change all `cost_price=X` seeded items to `cost_total = cost_price * quantity`.

---

## 4. Tests to Update

All test item creations using `cost_price=X, qty=N` → change to `cost_total = X * N`.

Key files:
- `tests/test_routers/test_items.py` (lines 71, 866-876)
- `tests/test_routers/test_dashboard.py` (lines 66, 71, 89-115, 102, 107)
- `tests/test_inventory_transform.py` (lines 44, 71, 89, 107, 162 in `_seed_item`)
- `tests/test_routers/test_items_gaps.py` (all item creations)
- `tests/test_routers/test_new_features.py` (review)
- `tests/test_routers/test_doc_workflows.py` (COGS items)
- `tests/test_routers/test_user_journeys.py` (lines 42, 983)
- `tests/test_user_journeys.py` (lines 633, 1136)
- `tests/test_fulfillment.py`, `tests/test_fulfillment_module.py`
- `tests/test_inventory_table_helpers.py`
- `tests/test_permissions.py` (add `cost_total` role guard coverage)
- `tests/test_import_history.py` (add `cost_total` import column test)
- `tests/integration/test_items_journeys.py`
- `premium-modules/warehousing/.../test_fulfill_toggle.py` (lines 38-40, 132, 148)
- `premium-modules/warehousing/.../test_pick_instructions.py` (lines 42-44, 228, 257, 276, 291)
- `premium-modules/warehousing/.../test_reservations.py` (lines 50, 56, 94, 100, 130)
- `default_modules/celerp-accounting/tests/test_accounting_gaps.py` (review)

Add new assertions:
- After split: parent `cost_total = original - child_cost_total`; child `cost_total = payload.child_cost_total`
- After transform: parent `cost_total` preserved (archived); child `cost_total = payload.child_cost_total`
- After merge: merged item `cost_total = sum(source cost_totals)`
- Valuation: items with `cost_total` stored are correctly summed (typo fix test)

---

## 5. What Does NOT Change

- `retail_price`, `wholesale_price`, all other price list fields: stored as unit prices, correct semantics
- Doc line `cost_price`: frozen per-unit cost at fulfillment time, correct and unchanged
- Reports COGS: `cost_price * quantity` on doc lines, correct and unchanged
- `item.pricing.set` event structure: unchanged (`cost_total` is a new valid `price_type` value)
- Connectors: keep `cost_price` legacy path, document as limitation

---

## 6. Implementation Order

1. `projections.py`: add `cost_total` branch
2. `_flatten_item` (`routes.py`): dual-derive both fields
3. Valuation typo fix (`routes.py`): `total_cost` → `cost_total`
4. `pick.py`: derive `cost_price = cost_total / qty`
5. Item creation API (`routes.py`, `ItemCreateBody`): accept `cost_total`
6. Split route: parent cost reduction + child `cost_total` storage
7. Transform route: child `cost_total` storage
8. Merge route: `merged_cost_total` sum + rename `resulting_cost_price` → `resulting_cost_total`
9. `auto_je.py`: use `cost_total` directly
10. All tests: update item creations + add new assertions
11. UI inventory routes: virtual cell render/edit, transform preview, merge form, import
12. UI `VIRTUAL_FOLLOWERS` (`table.py` + `inventory.py` JS): invert cost follower relationship
13. UI `api_client.py`: derive `cost_price` for doc lines; rename merge field
14. `celerp_docs/routes.py`: item lookup derivation
15. `warehousing/stock_receipts.py`: emit `cost_total` event
16. `seed_demo.py`: use `cost_total`

**Cruft to delete during implementation:**
- `child_data["cost_price"] = payload.child_cost_total / payload.child_quantity` in split + transform routes
- `weighted_cost` variable in merge route if unused after refactor
- `parent_cost_price` variable in transform preview if unused after refactor
- Any `cost_price` direct-patch UI path that is replaced by `cost_total`

---

## 7. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Legacy items with only `cost_price` show wrong `cost_total` if qty was edited after pricing | Medium | Medium | Accepted (Q3=C); `_flatten_item` derives best-effort value |
| Doc COGS calculations change | Low | High | Doc lines store frozen `cost_price` at fulfillment; unaffected |
| Valuation wrong until deploy | Already broken | Medium | Step 3 (one-line fix) |
| Virtual column JS follower inversion breaks drag | Medium | Low | `VIRTUAL_FOLLOWERS` update in steps 11-12 |
| Tests fail on cost assertions | High | Low | Enumerated in §4; mechanical changes |

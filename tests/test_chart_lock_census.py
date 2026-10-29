# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Every lock on an account row is taken to change it, every account row is added or
changed, and every account a posting reads is read, under the company lock.

The company lock is the chart's lock: a posting reads the accounts it posts to under it and
locks no account row; a change to the chart takes it before the rows it edits. Rows locked
any other way (FOR SHARE, or FOR UPDATE without the company lock first), or a posting that
reads its accounts before the company lock, meet a change in opposite orders, and
PostgreSQL aborts one as a deadlock (tests/test_chart_lock_concurrency_pg.py)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _chain_names(node: ast.AST, assigned: dict[str, list[ast.AST]]) -> set[str]:
    """The model names a ``select(...)`` in this call chain selects, following a statement
    built in a local variable to what the function assigns it."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in assigned:
            for value in assigned.pop(sub.id):
                names |= _chain_names(value, assigned)
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", getattr(sub.func, "attr", "")) == "select":
            names |= {a.id for a in sub.args if isinstance(a, ast.Name)}
            names |= {a.value.id for a in sub.args if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)}
    return names


# The calls that take the company lock (celerp/services/company_lock.py).
COMPANY_LOCKS = frozenset({"lock_company", "lock_companies", "locked_company", "lock_projections"})

# The reads a posting makes of the accounts it posts to (ChartAccess.lock_accounts and the
# money account check), each of which must take the company lock before it reads.
POSTING_READS = {
    "default_modules/celerp-accounting/celerp_accounting/chart_rules.py": "lock_accounts",
    "default_modules/celerp-accounting/celerp_accounting/ledger_accounts.py": "require_money_account",
}


def _company_lock(call: ast.Call) -> bool:
    return getattr(call.func, "id", getattr(call.func, "attr", "")) in COMPANY_LOCKS


def account_row_locks_outside_the_chart_lock(root: Path) -> list[str]:
    """Each ``with_for_update`` on an Account select that is FOR SHARE, or that its function
    does not precede with the company lock."""
    wrong = []
    for path in sorted([*(root / "celerp").rglob("*.py"), *(root / "default_modules").rglob("*.py")]):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or "/migrations/" in rel:
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            charted = [c.lineno for c in calls if _company_lock(c)]
            assignments: dict[str, list[ast.AST]] = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for target in n.targets:
                        if isinstance(target, ast.Name):
                            assignments.setdefault(target.id, []).append(n.value)
            for c in calls:
                if not (isinstance(c.func, ast.Attribute) and c.func.attr == "with_for_update"):
                    continue
                if "Account" not in _chain_names(c.func.value, {k: list(v) for k, v in assignments.items()}):
                    continue
                if any(kw.arg in ("read", "key_share") for kw in c.keywords):
                    wrong.append(f"{rel}:{c.lineno} {fn.name}: an account row FOR SHARE")
                elif not any(line <= c.lineno for line in charted):
                    wrong.append(f"{rel}:{c.lineno} {fn.name}: an account row FOR UPDATE without the company lock first")
    return wrong


def _account_builders(tree: ast.AST) -> set[str]:
    """The chart's ``Account`` model, where the module imports it, and the module's functions
    annotated to return one without taking the company lock (a builder: its caller writes what
    it builds). Importers have account records of their own, never imported."""
    model = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
             and (n.module or "").startswith("celerp_accounting") for a in n.names if a.name == "Account"}
    if not model:
        return set()
    return model | {f.name for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and isinstance(f.returns, ast.Name) and f.returns.id in model
                    and not any(isinstance(c, ast.Call) and _company_lock(c) for c in ast.walk(f))}


def _name(node: ast.AST) -> str:
    return getattr(node, "id", getattr(node, "attr", ""))


def account_writes_outside_the_chart_lock(root: Path) -> list[str]:
    """Each new account row (an ``Account`` built, directly or by a builder annotated
    ``-> Account``, outside such a builder) and each ``insert``/``update``/``delete`` of
    ``Account`` that its function does not precede with the company lock."""
    wrong = []
    for path in sorted([*(root / "celerp").rglob("*.py"), *(root / "default_modules").rglob("*.py")]):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or "/migrations/" in rel:
            continue
        tree = ast.parse(path.read_text())
        builders = _account_builders(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in builders:
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            charted = [c.lineno for c in calls if _company_lock(c)]
            for c in calls:
                name = _name(c.func)
                built = isinstance(c.func, ast.Name) and name in builders
                statement = name in ("insert", "update", "delete", "pg_insert") and any(
                    isinstance(a, ast.Name) and a.id == "Account" for a in c.args)
                if (built or statement) and not any(line <= c.lineno for line in charted):
                    wrong.append(f"{rel}:{c.lineno} {fn.name}: an account row written without the company lock first")
    return wrong


def test_every_account_row_write_sits_under_the_company_lock():
    assert account_writes_outside_the_chart_lock(ROOT) == []


def test_the_census_finds_an_account_written_without_the_company_lock(tmp_path):
    module = tmp_path / "default_modules" / "m"
    module.mkdir(parents=True)
    (tmp_path / "celerp").mkdir()
    (module / "x.py").write_text(
        "from celerp_accounting.models import Account\n"
        "def build(c) -> Account:\n"
        "    return Account(company_id=c)\n"
        "async def seed(s, c):\n"
        "    s.add(build(c))\n"
        "async def direct(s, c):\n"
        "    acc = Account(company_id=c)\n"
        "    s.add(acc)\n"
        "async def statement(s, c):\n"
        "    await s.execute(update(Account).values(name='x'))\n"
        "async def guarded(s, c):\n"
        "    await lock_company(s, c)\n"
        "    s.add(build(c))\n"
        "async def other_lock(s, c):\n"
        "    await lock_chart(s, c)\n"
        "    s.add_all([Account(company_id=c)])\n"
        "async def other(s, c):\n"
        "    s.add(BankAccount(company_id=c))\n"
        "async def create(s, c) -> Account:\n"
        "    await locked_company(s, c)\n"
        "    return Account(company_id=c)\n"
        "async def caller(s, c):\n"
        "    s.add(await create(s, c))\n")
    (module / "importer.py").write_text("def read(c):\n    return Account(c)\n")
    assert account_writes_outside_the_chart_lock(tmp_path) == [
        "default_modules/m/x.py:5 seed: an account row written without the company lock first",
        "default_modules/m/x.py:7 direct: an account row written without the company lock first",
        "default_modules/m/x.py:10 statement: an account row written without the company lock first",
        "default_modules/m/x.py:16 other_lock: an account row written without the company lock first",
    ]


def test_every_account_row_lock_sits_under_the_company_lock():
    assert account_row_locks_outside_the_chart_lock(ROOT) == []


def test_the_census_finds_a_share_lock_and_an_unguarded_update(tmp_path):
    module = tmp_path / "default_modules" / "m"
    module.mkdir(parents=True)
    (tmp_path / "celerp").mkdir()
    (module / "x.py").write_text(
        "async def share(s, c):\n"
        "    return await s.execute(select(Account).where(Account.code == c).with_for_update(read=True))\n"
        "async def update(s, c):\n"
        "    return await s.execute(select(Account.id).with_for_update())\n"
        "async def guarded(s, c):\n"
        "    await lock_projections(s, c, [])\n"
        "    return await s.execute(select(Account).with_for_update())\n"
        "async def other_lock(s, c):\n"
        "    await lock_chart(s, c)\n"
        "    return await s.execute(select(Account).with_for_update())\n"
        "async def built(s, c):\n"
        "    stmt = select(Account).where(Account.code == c)\n"
        "    stmt = stmt.with_for_update()\n"
        "    return await s.execute(stmt)\n"
        "async def other(s, c):\n"
        "    return await s.execute(select(BankAccount).with_for_update())\n")
    assert account_row_locks_outside_the_chart_lock(tmp_path) == [
        "default_modules/m/x.py:2 share: an account row FOR SHARE",
        "default_modules/m/x.py:4 update: an account row FOR UPDATE without the company lock first",
        "default_modules/m/x.py:10 other_lock: an account row FOR UPDATE without the company lock first",
        "default_modules/m/x.py:13 built: an account row FOR UPDATE without the company lock first",
    ]


def posting_reads_before_the_company_lock(root: Path) -> list[str]:
    """Each posting read (POSTING_READS) that selects ``Account`` before it takes the
    company lock, or that is missing."""
    wrong = []
    for rel, name in sorted(POSTING_READS.items()):
        fns = [f for f in ast.walk(ast.parse((root / rel).read_text()))
               if isinstance(f, ast.AsyncFunctionDef) and f.name == name]
        if not fns:
            wrong.append(f"{rel} {name}: missing")
            continue
        calls = [n for n in ast.walk(fns[0]) if isinstance(n, ast.Call)]
        locked = [c.lineno for c in calls if _company_lock(c)]
        reads = [c.lineno for c in calls if _name(c.func) == "select" and _chain_names(c, {}) & {"Account"}]
        if not locked or not reads or min(reads) < min(locked):
            wrong.append(f"{rel}:{fns[0].lineno} {name}: reads accounts before the company lock")
    return wrong


def test_every_posting_reads_its_accounts_under_the_company_lock():
    assert posting_reads_before_the_company_lock(ROOT) == []


def test_the_census_finds_a_posting_read_before_the_company_lock(tmp_path):
    module = tmp_path / "default_modules" / "celerp-accounting" / "celerp_accounting"
    module.mkdir(parents=True)
    (module / "chart_rules.py").write_text(
        "async def lock_accounts(s, c, codes):\n"
        "    await lock_chart(s, c, shared=True)\n"
        "    return await s.execute(select(Account))\n")
    (module / "ledger_accounts.py").write_text(
        "async def require_money_account(s, c, code):\n"
        "    await lock_company(s, c)\n"
        "    return await s.scalar(select(Account))\n")
    assert posting_reads_before_the_company_lock(tmp_path) == [
        "default_modules/celerp-accounting/celerp_accounting/chart_rules.py:1 lock_accounts: "
        "reads accounts before the company lock",
    ]

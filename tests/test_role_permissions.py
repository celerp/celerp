# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Dynamic role-permission registry and resolver tests.

Covers:
- ROLES derived from auth.ROLE_LEVELS with i18n label keys
- PERMISSIONS catalogue: keys, defaults, floors, fixed rows
- Resolver: default membership, per-role grants, stale grants, fail-closed roles
- assert_role_permission 403 shape
- Per-role grant independence (a grant to one role never cascades to another)
"""
from __future__ import annotations

import pytest
import pytest_asyncio


def _roles_from(role: str) -> list[str]:
    """The role keys at or above *role* in the hierarchy: the per-role grant set a
    threshold expands to under the role_grants model. A grant recorded down to
    *role* holds for that role and every higher one, and no lower one."""
    from celerp.services.auth import ROLE_LEVELS

    floor = ROLE_LEVELS[role]
    return [r for r, lvl in ROLE_LEVELS.items() if lvl >= floor]


# ── Registry shape ────────────────────────────────────────────────────────────

def test_roles_derived_from_role_levels():
    """ROLES keys and order match auth.ROLE_LEVELS; each carries a label_key."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import ROLES

    assert [r.key for r in ROLES] == sorted(ROLE_LEVELS, key=ROLE_LEVELS.get)
    for r in ROLES:
        assert r.level == ROLE_LEVELS[r.key]
        assert r.label_key == f"settings.{r.key}"


# The full 2.1 catalogue: key -> (default_role, grantable, floor_role).
# Defaults reproduce the thresholds the enforcement sites carry today.
EXPECTED_CATALOGUE = {
    # viewer defaults (view keys floor at viewer)
    "view_dashboards": ("viewer", True, "viewer"),
    "view_documents": ("viewer", True, "viewer"),
    "view_contacts": ("viewer", True, "viewer"),
    "view_inventory": ("viewer", True, "viewer"),
    # operator defaults (write-capable keys; floor now viewer, owner may grant down to it)
    "edit_documents": ("operator", True, "viewer"),
    "edit_contacts": ("operator", True, "viewer"),
    "edit_inventory": ("operator", True, "viewer"),
    "edit_inventory_amounts": ("operator", True, "viewer"),
    "finalize_documents": ("operator", True, "viewer"),
    "fulfill_documents": ("operator", True, "viewer"),
    "record_payments": ("operator", True, "viewer"),
    "set_sales_doc_prices": ("operator", True, "viewer"),
    "manage_labels": ("operator", True, "viewer"),
    "manage_manufacturing": ("operator", True, "viewer"),
    "use_ai_assistant": ("operator", True, "viewer"),
    "run_backups": ("operator", True, "viewer"),
    "view_subscriptions": ("operator", True, "viewer"),
    # manager defaults
    "view_inventory_costs": ("manager", True, "viewer"),
    "set_inventory_prices": ("manager", True, "viewer"),
    "delete_documents": ("manager", True, "viewer"),
    "adjust_inventory": ("manager", True, "viewer"),
    "import_export_data": ("manager", True, "viewer"),
    "view_payments": ("manager", True, "viewer"),
    "view_financial_reports": ("manager", True, "viewer"),
    "manage_accounting": ("manager", True, "viewer"),
    "manage_module_settings": ("manager", True, "viewer"),
    # admin defaults
    "manage_users": ("admin", True, "viewer"),
    "manage_company_settings": ("admin", True, "admin"),
    "manage_integrations": ("admin", True, "viewer"),
    # fixed rows: owner-only, never grantable
    "manage_permissions": ("owner", False, "owner"),
    "manage_company_lifecycle": ("owner", False, "owner"),
    "manage_billing": ("owner", False, "owner"),
}


def test_permissions_registry_shape():
    """The registry holds exactly the catalogue keys with their defaults and floors."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert set(by_key) == set(EXPECTED_CATALOGUE)
    for key, (default, grantable, floor) in EXPECTED_CATALOGUE.items():
        p = by_key[key]
        assert p.default_role == default, key
        assert p.grantable is grantable, key
        assert p.floor_role == floor, key
        assert p.label, key


def test_fixed_rows_not_grantable():
    """manage_permissions, manage_company_lifecycle, manage_billing are fixed."""
    from celerp.services.permissions import PERMISSIONS

    fixed = {p.key for p in PERMISSIONS if not p.grantable}
    assert fixed == {"manage_permissions", "manage_company_lifecycle", "manage_billing"}


# ── Resolver ──────────────────────────────────────────────────────────────────

def test_default_resolution_membership():
    """Empty grants resolve each key to its registry default set: the default role
    and every role above it hold it, and the role just below does not."""
    from celerp.services.permissions import role_has_permission

    # set_inventory_prices default manager.
    assert role_has_permission({}, "manager", "set_inventory_prices") is True
    assert role_has_permission({}, "operator", "set_inventory_prices") is False
    # set_sales_doc_prices default operator.
    assert role_has_permission({}, "operator", "set_sales_doc_prices") is True
    assert role_has_permission({}, "viewer", "set_sales_doc_prices") is False
    # view_inventory default viewer: every role holds it.
    assert role_has_permission({}, "viewer", "view_inventory") is True


def test_grant_resolution_membership():
    """A role_grants entry sets exactly which roles hold the key, independent of the
    registry default."""
    from celerp.services.permissions import role_has_permission

    settings = {"role_grants": {"set_inventory_prices": _roles_from("operator")}}
    assert role_has_permission(settings, "operator", "set_inventory_prices") is True
    assert role_has_permission(settings, "viewer", "set_inventory_prices") is False
    settings = {"role_grants": {"set_sales_doc_prices": _roles_from("manager")}}
    assert role_has_permission(settings, "manager", "set_sales_doc_prices") is True
    assert role_has_permission(settings, "operator", "set_sales_doc_prices") is False


def test_manage_company_settings_floor_is_admin():
    """Purge and delete are destructive and share the manage_company_settings gate,
    so its floor is admin: an owner can no longer lower the threshold below admin."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert by_key["manage_company_settings"].floor_role == "admin"


def test_grant_clamped_to_floor_membership():
    """A stored grant below the floor does not admit the sub-floor role: a grant of
    manage_company_settings that names operator still resolves to admin-and-up,
    so the raised floor is a real invariant on read and not only on save."""
    from celerp.services.permissions import role_has_permission

    settings = {"role_grants": {"manage_company_settings":
                                ["operator", "manager", "admin", "owner"]}}
    assert role_has_permission(settings, "operator", "manage_company_settings") is False
    assert role_has_permission(settings, "manager", "manage_company_settings") is False
    assert role_has_permission(settings, "admin", "manage_company_settings") is True


def test_stale_role_in_grant_ignored_membership():
    """A grant naming a role no longer in ROLE_LEVELS drops that role; the remaining
    valid roles still resolve, and an all-stale set admits no one (never a crash,
    never an accidental grant). An unknown key falls back to its default set."""
    from celerp.services.permissions import role_has_permission

    settings = {"role_grants": {"set_inventory_prices": ["archduke", "manager"]}}
    assert role_has_permission(settings, "manager", "set_inventory_prices") is True
    assert role_has_permission(settings, "operator", "set_inventory_prices") is False
    settings = {"role_grants": {"set_inventory_prices": ["archduke"]}}
    assert role_has_permission(settings, "manager", "set_inventory_prices") is False
    settings = {"role_grants": {"no_such_permission": ["operator"]}}
    assert role_has_permission(settings, "manager", "set_inventory_prices") is True
    assert role_has_permission(settings, "operator", "set_inventory_prices") is False


def test_fixed_permission_ignores_role_grants():
    """Fixed rows resolve at their owner default even when role_grants names them:
    the grantable short-circuit prevents both escalation and self-lockout."""
    from celerp.services.permissions import role_has_permission

    settings = {"role_grants": {"manage_permissions":
                                ["viewer", "operator", "manager", "admin", "owner"]}}
    assert role_has_permission(settings, "viewer", "manage_permissions") is False
    assert role_has_permission(settings, "owner", "manage_permissions") is True


def test_role_has_permission_unknown_role_fails_closed():
    """An unmapped role resolves to level 0 and fails closed.

    The resolver receives already-migrated roles from get_current_role;
    legacy-alias migration itself stays covered by TestLegacyRoleMigration
    in tests/test_permissions.py.
    """
    from celerp.services.permissions import role_has_permission

    assert role_has_permission({}, "salesperson", "set_sales_doc_prices") is False
    assert role_has_permission({}, "operator", "set_sales_doc_prices") is True


def test_assert_role_permission_raises_403():
    """Denied caller gets a 403 whose message names the permission."""
    from fastapi import HTTPException
    from celerp.services.permissions import assert_role_permission

    with pytest.raises(HTTPException) as exc:
        assert_role_permission({}, "operator", "set_inventory_prices")
    assert exc.value.status_code == 403
    assert "set_inventory_prices" in exc.value.detail
    # A granted caller passes silently.
    assert_role_permission({}, "manager", "set_inventory_prices")


def test_defaults_are_registry_membership():
    """With empty grants every registry key resolves to a set: exactly the roles at
    or above its default_role hold it, every role below does not. This pins the same
    default thresholds the enforcement sites carried before, expressed as membership
    of role_has_permission rather than a numeric minimum."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import PERMISSIONS, role_has_permission

    for p in PERMISSIONS:
        expected_role, _, _ = EXPECTED_CATALOGUE[p.key]
        want = ROLE_LEVELS[expected_role]
        for role, lvl in ROLE_LEVELS.items():
            assert role_has_permission({}, role, p.key) is (lvl >= want), (p.key, role)


# ── Gate 1: inventory cost visibility and price writes ───────────

from test_helpers import grant_permission, invite_user, perm_setup  # noqa: E402

_GRANTED = {"role_grants": {"view_inventory_costs": _roles_from("operator")}}

_ROLE_PERM_URL = "/companies/me/role-permissions"


async def _company_settings(client, headers: dict) -> dict:
    """Read the caller's company settings fresh from the API."""
    r = await client.get("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    return (r.json().get("settings") or {})


async def _toggle(client, headers: dict, perm_key: str, role_key: str, granted: bool) -> None:
    """Toggle one (permission, role) matrix cell through the owner-gated route."""
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": perm_key, "role_key": role_key, "granted": granted},
        headers=headers,
    )
    assert r.status_code == 200, r.text


# ── Per-role grant independence (grants are a set, not a cascading threshold) ──


async def test_grant_is_independent_per_role(client, session):
    """Granting a perm to viewer alone does not grant it to operator: a grant is
    per-role, not a threshold that fills in every role above the one granted."""
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    # set_inventory_prices defaults to manager, so operator sits above viewer but
    # below the default: the only way operator holds it is if the grant cascaded.
    await _toggle(client, ctx["admin_h"], "set_inventory_prices", "viewer", True)
    settings = await _company_settings(client, ctx["admin_h"])
    assert role_has_permission(settings, "viewer", "set_inventory_prices") is True
    assert role_has_permission(settings, "operator", "set_inventory_prices") is False


async def test_revoke_is_independent_per_role(client, session):
    """Revoking a perm from one role leaves every other role's grant untouched."""
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    await _toggle(client, ctx["admin_h"], "set_inventory_prices", "viewer", True)
    await _toggle(client, ctx["admin_h"], "set_inventory_prices", "operator", True)
    await _toggle(client, ctx["admin_h"], "set_inventory_prices", "operator", False)
    settings = await _company_settings(client, ctx["admin_h"])
    assert role_has_permission(settings, "operator", "set_inventory_prices") is False
    assert role_has_permission(settings, "viewer", "set_inventory_prices") is True


async def test_matrix_toggle_changes_only_that_cell(client, session):
    """Toggling one matrix cell flips exactly that role's membership and no other."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    key = "set_inventory_prices"
    s_before = await _company_settings(client, ctx["admin_h"])
    before = {r: role_has_permission(s_before, r, key) for r in ROLE_LEVELS}
    await _toggle(client, ctx["admin_h"], key, "viewer", True)
    s_after = await _company_settings(client, ctx["admin_h"])
    after = {r: role_has_permission(s_after, r, key) for r in ROLE_LEVELS}
    changed = {r for r in ROLE_LEVELS if before[r] != after[r]}
    assert changed == {"viewer"}


async def test_noncontiguous_grant_independent(client, session):
    """A grant set need not be contiguous: viewer and admin can hold a perm while
    operator and manager between them do not."""
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    key = "set_inventory_prices"  # default manager set: {manager, admin, owner}
    await _toggle(client, ctx["admin_h"], key, "viewer", True)   # add viewer
    await _toggle(client, ctx["admin_h"], key, "manager", False)  # drop manager
    settings = await _company_settings(client, ctx["admin_h"])
    assert role_has_permission(settings, "viewer", key) is True
    assert role_has_permission(settings, "operator", key) is False
    assert role_has_permission(settings, "manager", key) is False
    assert role_has_permission(settings, "admin", key) is True


async def test_admin_cannot_write_role_grants_via_company_settings(client, session):
    """The permissions matrix is owner-only: an admin cannot bypass it by writing
    role_grants (or the retired role_permissions key) through PATCH /companies/me.
    Both blobs are rejected 422 and no effective permission changes."""
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}
    for blob in (
        {"role_grants": {"set_inventory_prices": ["viewer", "operator", "manager", "admin", "owner"]}},
        {"role_permissions": {"set_inventory_prices": "viewer"}},
    ):
        r = await client.patch("/companies/me", json={"settings": blob}, headers=admin_h)
        assert r.status_code == 422, (blob, r.text)
    settings = await _company_settings(client, ctx["admin_h"])
    assert role_has_permission(settings, "operator", "set_inventory_prices") is False


# ── Migration: role_permissions threshold -> role_grants set (bug 2) ──────────

import json as _json  # noqa: E402
import os as _os  # noqa: E402
import uuid as _uuid  # noqa: E402

from sqlalchemy import create_engine as _create_engine  # noqa: E402
from sqlalchemy import text as _sa_text  # noqa: E402

def _load_migration_harness():
    """Load the migration test harness (run_migration_ops, wc_mkcompany) from the
    migration package's conftest. It is loaded by file path because the tests
    directory is a namespace package, so tests.test_migrations is not importable
    as a dotted module from a sibling test file; the migration tests themselves
    reach it through a relative import that only works from inside that package."""
    import importlib.util as _ilu

    path = _os.path.join(_os.path.dirname(__file__), "test_migrations", "conftest.py")
    spec = _ilu.spec_from_file_location("_role_grants_migration_harness", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_migration_ops, mod.wc_mkcompany


run_migration_ops, wc_mkcompany = _load_migration_harness()  # noqa: E402

# The alembic head at merge-base; the role_grants expansion migration descends
# from it. Before that migration is written, discovery returns None and the
# T8-T10 tests fail at merge-base for the right reason: it is not present.
_BASE_HEAD = "f8a9b0c1d2e3"


def _role_grants_migration_module():
    """Module stem of the migration whose down_revision is the pinned base head,
    or None when no such migration exists yet."""
    from alembic.script import ScriptDirectory

    from celerp.alembic_config import build_alembic_config

    script = ScriptDirectory.from_config(build_alembic_config())
    for rev in script.walk_revisions():
        if rev.down_revision == _BASE_HEAD:
            return _os.path.splitext(_os.path.basename(rev.path))[0]
    return None


def _read_settings(engine, cid: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(_sa_text("SELECT settings FROM companies WHERE id = :id"),
                           {"id": cid}).scalar()
    return row if isinstance(row, dict) else _json.loads(row)


@pytest.fixture()
def role_grants_migration_db():
    """Isolated Postgres schema with only a companies table, for the settings-only
    role_permissions->role_grants data migration. Mirrors the repo's other
    migration harnesses (tests/test_migrations/conftest.py): a sync psycopg2 engine
    scoped to a throwaway schema, run through a real alembic Operations context."""
    base_url = _os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    schema = f"rgmig_{_uuid.uuid4().hex[:8]}"

    admin = _create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(_sa_text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()

    engine = _create_engine(base_url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(_sa_text(
            "CREATE TABLE companies (id UUID PRIMARY KEY, name TEXT, settings JSON NOT NULL)"))
    yield engine
    engine.dispose()

    admin = _create_engine(base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(_sa_text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_migration_expands_overrides_preserving_effective_permissions(role_grants_migration_db):
    """The data migration expands each stored threshold override into an explicit
    per-role grant set, deltas-only, with the same effective permissions for every
    role. A sub-floor threshold clamps up to the floor; unoverridden keys stay
    absent from role_grants; the old role_permissions key is dropped."""
    from celerp.services.auth import ROLE_LEVELS
    from celerp.services.permissions import role_has_permission

    module = _role_grants_migration_module()
    assert module is not None, "role_grants expansion migration not present at merge-base"

    with role_grants_migration_db.begin() as conn:
        cid = wc_mkcompany(conn, {
            "role_permissions": {
                "view_payments": "operator",              # manager default, floor viewer
                "manage_company_settings": "operator",    # admin default, floor admin (clamps)
            },
            "currency": "USD",
        })
    run_migration_ops(role_grants_migration_db, module)

    settings = _read_settings(role_grants_migration_db, cid)
    grants = settings.get("role_grants") or {}
    # Only the two overridden keys expand; nothing else is materialized.
    assert set(grants) == {"view_payments", "manage_company_settings"}
    assert set(grants["view_payments"]) == {"operator", "manager", "admin", "owner"}
    # operator threshold clamps up to the admin floor for the destructive key.
    assert set(grants["manage_company_settings"]) == {"admin", "owner"}
    # The retired key is gone; unrelated settings survive.
    assert "role_permissions" not in settings
    assert settings.get("currency") == "USD"
    # Effective permission is unchanged for every role, overridden and default alike.
    for r in ROLE_LEVELS:
        assert role_has_permission(settings, r, "view_payments") is (
            ROLE_LEVELS[r] >= ROLE_LEVELS["operator"])
        assert role_has_permission(settings, r, "manage_company_settings") is (
            ROLE_LEVELS[r] >= ROLE_LEVELS["admin"])
        assert role_has_permission(settings, r, "set_inventory_prices") is (
            ROLE_LEVELS[r] >= ROLE_LEVELS["manager"])


def test_migration_idempotent(role_grants_migration_db):
    """Running the migration twice (alembic apply then a reconcile replay) leaves
    role_grants stable and never re-introduces role_permissions."""
    module = _role_grants_migration_module()
    assert module is not None, "role_grants expansion migration not present at merge-base"

    with role_grants_migration_db.begin() as conn:
        cid = wc_mkcompany(conn, {"role_permissions": {"view_payments": "operator"}})
    run_migration_ops(role_grants_migration_db, module)
    first = _read_settings(role_grants_migration_db, cid)
    run_migration_ops(role_grants_migration_db, module)
    second = _read_settings(role_grants_migration_db, cid)

    assert second == first
    assert "role_permissions" not in second
    assert set(second.get("role_grants", {})) == {"view_payments"}


def test_migration_ignores_stale_override_role(role_grants_migration_db):
    """An override naming a role not in ROLE_LEVELS resolved to the default already,
    so it becomes no delta: absent from role_grants, no crash. A valid override
    alongside it still expands."""
    module = _role_grants_migration_module()
    assert module is not None, "role_grants expansion migration not present at merge-base"

    with role_grants_migration_db.begin() as conn:
        cid = wc_mkcompany(conn, {"role_permissions": {
            "view_payments": "archduke",           # stale role -> dropped
            "set_inventory_prices": "operator",    # real delta -> expands
        }})
    run_migration_ops(role_grants_migration_db, module)

    settings = _read_settings(role_grants_migration_db, cid)
    grants = settings.get("role_grants") or {}
    assert "view_payments" not in grants
    assert set(grants.get("set_inventory_prices", [])) == {"operator", "manager", "admin", "owner"}
    assert "role_permissions" not in settings


def test_migration_downgrade_collapses_grants_to_threshold(role_grants_migration_db):
    """downgrade() is the best-effort inverse of upgrade(): each grant set collapses
    back to a single role_permissions threshold at its lowest granted role, the
    retired key is restored, and role_grants is dropped. An upgrade-then-downgrade
    round trip reconstructs the exact thresholds a contiguous set came from; a
    hand-authored non-contiguous set collapses to its lowest role (documented
    lossy)."""
    module = _role_grants_migration_module()
    assert module is not None, "role_grants expansion migration not present at merge-base"

    with role_grants_migration_db.begin() as conn:
        # (a) threshold -> grants -> threshold, expected to round-trip exactly.
        cid_rt = wc_mkcompany(conn, {
            "role_permissions": {
                "view_payments": "operator",             # manager default, floor viewer
                "manage_company_settings": "operator",   # admin default, floor admin (clamps up)
            },
            "currency": "USD",
        })
        # (b) a non-contiguous set upgrade never produces; downgrade takes the lowest.
        cid_nc = wc_mkcompany(conn, {"role_grants": {"view_payments": ["viewer", "admin"]}})
    run_migration_ops(role_grants_migration_db, module)               # (a) threshold -> grants
    run_migration_ops(role_grants_migration_db, module, "downgrade")  # grants -> threshold

    rt = _read_settings(role_grants_migration_db, cid_rt)
    assert "role_grants" not in rt
    # Lowest granted role restores the original threshold; the admin clamp collapses to admin.
    assert (rt.get("role_permissions") or {}) == {
        "view_payments": "operator", "manage_company_settings": "admin"}
    assert rt.get("currency") == "USD"

    nc = _read_settings(role_grants_migration_db, cid_nc)
    assert "role_grants" not in nc
    assert (nc.get("role_permissions") or {}).get("view_payments") == "viewer"


async def test_operator_granted_sees_cost_fields(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_inventory_costs", "operator")
    r = await client.get("/items", headers=ctx["operator_h"])
    assert r.status_code == 200
    items = r.json()["items"]
    target = next(i for i in items if i["id"] == ctx["item_id"])
    assert "cost_price" in target or "cost_total" in target


async def test_operator_granted_item_schema_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_inventory_costs", "operator")
    r = await client.get("/companies/me/item-schema", headers=ctx["operator_h"])
    assert r.status_code == 200
    keys = {f.get("key") for f in r.json()}
    assert "cost_price" in keys


async def test_operator_granted_sets_price(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_operator_ungranted_price_403(client, session):
    ctx = await perm_setup(client, session)
    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403
    assert "set_inventory_prices" in r.json()["detail"]


async def test_operator_granted_create_with_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.post(
        "/items",
        json={"sku": "OP-COST", "name": "Op Cost Item", "quantity": 1,
              "location_id": ctx["location_id"], "cost_price": 42.0, "sell_by": "piece"},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_operator_granted_patch_with_costs(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.patch(
        f"/items/{ctx['item_id']}",
        json={"fields_changed": {"cost_price": {"old": 100.0, "new": 80.0}}},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_price_grant_does_not_reveal_costs(client, session):
    """The split keys are independent: set_inventory_prices alone allows the
    price write but leaves cost fields stripped from reads."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text
    r = await client.get("/items", headers=ctx["operator_h"])
    target = next(i for i in r.json()["items"] if i["id"] == ctx["item_id"])
    assert "cost_price" not in target and "cost_total" not in target


async def test_manager_default_unchanged(client, session):
    """Confirmatory: with no overrides, manager keeps cost visibility and price
    writes, operator keeps neither."""
    ctx = await perm_setup(client, session)

    r = await client.get("/items", headers=ctx["manager_h"])
    target = next(i for i in r.json()["items"] if i["id"] == ctx["item_id"])
    assert "cost_price" in target or "cost_total" in target

    r = await client.post(
        f"/items/{ctx['item_id']}/price",
        json={"price_type": "retail_price", "new_price": 175.0},
        headers=ctx["manager_h"],
    )
    assert r.status_code == 200, r.text

    r = await client.get("/items", headers=ctx["operator_h"])
    target = next(i for i in r.json()["items"] if i["id"] == ctx["item_id"])
    assert "cost_price" not in target and "cost_total" not in target


# ── Activity redaction follows the permission ─────────────────────────────────

def test_can_see_costs_uses_permission():
    from celerp.services.activity_redaction import can_see_costs

    assert can_see_costs(_GRANTED, "operator") is True
    assert can_see_costs({}, "operator") is False
    assert can_see_costs({}, "manager") is True
    assert can_see_costs({}, None) is False


def test_redact_entries_granted_operator():
    from celerp.services.activity_redaction import redact_entries_for_role

    entries = [{"event_type": "item.created", "data": {"cost_total": 100.0, "name": "X"}}]
    out = redact_entries_for_role(entries, _GRANTED, "operator")
    assert out[0]["data"]["cost_total"] == 100.0
    out2 = redact_entries_for_role(entries, {}, "operator")
    assert "cost_total" not in out2[0]["data"]


async def test_ledger_route_passes_overrides(client, session):
    """The ledger routes resolve cost visibility from company settings."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_inventory_costs", "operator")

    def _cost_pricing(entries):
        return [e for e in entries
                if e.get("event_type") == "item.pricing.set"
                and (e.get("data") or {}).get("price_type") == "cost_price"]

    r = await client.get("/ledger", params={"entity_id": ctx["item_id"]}, headers=ctx["operator_h"])
    assert r.status_code == 200
    items = r.json()["items"]
    cost_events = _cost_pricing(items)
    assert cost_events, "item was created with a cost price"
    # Granted: the cost amount is present and not redacted.
    assert all("new_price" in (e.get("data") or {}) for e in cost_events)
    assert not any((e.get("data") or {}).get("cost_redacted") for e in cost_events)

    entry_id = cost_events[0]["id"]
    r2 = await client.get(f"/ledger/{entry_id}", headers=ctx["operator_h"])
    assert r2.status_code == 200
    assert "new_price" in (r2.json().get("data") or {})


async def test_dashboard_activity_granted_operator(client, session):
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "view_inventory_costs", "operator")

    r = await client.get("/dashboard/activity", headers=ctx["operator_h"])
    assert r.status_code == 200
    acts = r.json()["activities"]
    assert any(any(k in (a.get("data") or {}) for k in ("cost_price", "cost_total"))
               for a in acts), "granted operator should see unredacted activity costs"


# ── Sales document price gate (J5) ────────────────────────────────────────────

async def _make_draft_invoice(client, headers, ctx, unit_price, ref_id):
    """Create a one-line draft invoice referencing the perm item; return its id."""
    r = await client.post(
        "/docs",
        json={
            "doc_type": "invoice",
            "ref_id": ref_id,
            "line_items": [{
                "sku": "SKU-PERM", "item_id": ctx["item_id"],
                "quantity": 2, "unit_price": unit_price,
            }],
            "total": 2 * unit_price,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _line_patch(item_id, quantity, unit_price, old_quantity=2, old_unit_price=50.0):
    """A DocPatch body changing the single line to the given quantity/price."""
    line = {"sku": "SKU-PERM", "item_id": item_id}
    return {"fields_changed": {"line_items": {
        "old": [{**line, "quantity": old_quantity, "unit_price": old_unit_price}],
        "new": [{**line, "quantity": quantity, "unit_price": unit_price}],
    }}}


async def test_operator_revoked_patch_price_403(client, session):
    """With set_sales_doc_prices raised to manager, an operator changing unit_price
    on a draft is rejected naming the permission; a quantity-only edit still saves."""
    ctx = await perm_setup(client, session)
    doc_id = await _make_draft_invoice(client, ctx["admin_h"], ctx, 50.0, "INV-P40")
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")

    r = await client.patch(f"/docs/{doc_id}", json=_line_patch(ctx["item_id"], 2, 75.0),
                           headers=ctx["operator_h"])
    assert r.status_code == 403, r.text
    assert "set_sales_doc_prices" in r.json()["detail"]

    r2 = await client.patch(f"/docs/{doc_id}", json=_line_patch(ctx["item_id"], 3, 50.0),
                            headers=ctx["operator_h"])
    assert r2.status_code == 200, r2.text


async def test_operator_revoked_create_price_403(client, session):
    """Doc create with a line whose unit_price deviates from the catalog price is
    rejected; a catalog-priced line succeeds."""
    ctx = await perm_setup(client, session)
    r = await client.post(f"/items/{ctx['item_id']}/price",
                          json={"price_type": "Retail", "new_price": 100.0},
                          headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")

    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P41A",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 150.0}],
              "total": 150.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403, r.text
    assert "set_sales_doc_prices" in r.json()["detail"]

    r2 = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P41B",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 100.0}],
              "total": 100.0},
        headers=ctx["operator_h"],
    )
    assert r2.status_code == 200, r2.text


async def test_operator_default_sets_doc_price(client, session):
    """Confirmatory: with no override, an operator sets and edits document prices
    exactly as today."""
    ctx = await perm_setup(client, session)
    r = await client.post(
        "/docs",
        json={"doc_type": "invoice", "ref_id": "INV-P42",
              "line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                              "quantity": 1, "unit_price": 999.0}],
              "total": 999.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    r2 = await client.patch(
        f"/docs/{doc_id}",
        json={"fields_changed": {"line_items": {
            "old": [{"sku": "SKU-PERM", "item_id": ctx["item_id"], "quantity": 1, "unit_price": 999.0}],
            "new": [{"sku": "SKU-PERM", "item_id": ctx["item_id"], "quantity": 1, "unit_price": 50.0}],
        }}},
        headers=ctx["operator_h"],
    )
    assert r2.status_code == 200, r2.text


async def test_save_doc_lines_surfaces_permission_error(client, session):
    """The UI save endpoint returns {"error": ...} naming the permission when the
    underlying PATCH is rejected, driven end to end through the real API."""
    from unittest.mock import patch
    from httpx import ASGITransport, AsyncClient
    from celerp.main import app as api_app

    ctx = await perm_setup(client, session)
    doc_id = await _make_draft_invoice(client, ctx["admin_h"], ctx, 50.0, "INV-P43")
    await grant_permission(client, ctx["admin_h"], "set_sales_doc_prices", "manager")
    operator_token = ctx["operator_h"]["Authorization"].split()[1]

    def _bridged_client(token, timeout=10.0):
        return AsyncClient(
            transport=ASGITransport(app=api_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    from ui.app import app as ui_app
    with patch("ui.api_client._client", _bridged_client):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as ui_c:
            r = await ui_c.post(
                f"/docs/{doc_id}/lines",
                cookies={"celerp_token": operator_token},
                json={"line_items": [{"sku": "SKU-PERM", "item_id": ctx["item_id"],
                                      "quantity": 2, "unit_price": 75.0}],
                      "subtotal": 150.0, "tax": 0, "total": 150.0},
            )
    assert r.status_code == 400, r.text
    assert "set_sales_doc_prices" in r.json()["error"]


# ── Matrix save API: PATCH /companies/me/role-permissions (J3) ─────────────────


async def test_patch_role_permissions_owner_only(client, session):
    """Editing permissions is owner-only: an admin is refused, the owner accepts."""
    ctx = await perm_setup(client, session)
    admin_tok = await invite_user(client, session, ctx["admin_h"], "adm@perm.example", "admin")
    admin_h = {"Authorization": f"Bearer {admin_tok}"}
    body = {"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True}

    r = await client.patch(_ROLE_PERM_URL, json=body, headers=admin_h)
    assert r.status_code == 403, r.text

    r2 = await client.patch(_ROLE_PERM_URL, json=body, headers=ctx["admin_h"])
    assert r2.status_code == 200, r2.text


async def test_patch_role_permissions_unknown_perm_422(client, session):
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "not_a_permission", "role_key": "operator", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


async def test_patch_role_permissions_non_grantable_403(client, session):
    """A fixed row (manage_billing) cannot be reassigned even by the owner."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "manage_billing", "role_key": "admin", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 403, r.text


async def test_patch_role_permissions_unknown_role_422(client, session):
    """An unknown role, including a retired legacy alias, is refused."""
    ctx = await perm_setup(client, session)
    for bad in ("wizard", "salesperson"):  # salesperson is a migrated legacy alias, not a role key
        r = await client.patch(
            _ROLE_PERM_URL,
            json={"perm_key": "set_inventory_prices", "role_key": bad, "granted": True},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 422, (bad, r.text)


async def test_patch_role_permissions_persists(client, session):
    """A granted override survives a fresh settings read."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

    r2 = await client.get("/companies/me", headers=ctx["admin_h"])
    assert r2.status_code == 200, r2.text
    grants = (r2.json()["settings"] or {}).get("role_grants") or {}
    assert "operator" in (grants.get("set_inventory_prices") or [])


async def test_patch_role_permissions_malformed_boolean_422(client, session):
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "operator", "granted": "banana"},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


async def test_viewer_granted_write_can_act(client, session):
    """The owner may grant a write to viewer, and that viewer can then act: grant
    edit_inventory to viewer (200), and a viewer POST /items succeeds. Only the
    destructive-key floor is retained, so no write key rejects a viewer grant."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "edit_inventory", "role_key": "viewer", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.example', 'viewer')}"}
    r2 = await client.post(
        "/items",
        json={"sku": "VW-1", "name": "Viewer Item", "quantity": 1,
              "location_id": ctx["location_id"], "sell_by": "piece"},
        headers=viewer_h,
    )
    assert r2.status_code == 200, r2.text


# ── Matrix render + per-toggle UI route (J3) ──────────────────────────────────

def _settings_patches(settings: dict, users: list | None = None):
    from unittest.mock import AsyncMock, patch

    company = {"name": "Perm Co", "settings": settings}
    return [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_users", AsyncMock(return_value={"items": users or []})),
        patch("ui.api_client.get_modules", AsyncMock(return_value=[])),
    ]


async def _render_users_tab(ui, role: str, settings: dict) -> str:
    from contextlib import ExitStack

    from test_helpers import authed_cookies

    with ExitStack() as stack:
        for p in _settings_patches(settings):
            stack.enter_context(p)
        r = await ui.get("/settings/general", params={"tab": "users"},
                         cookies=authed_cookies(role=role))
    assert r.status_code == 200, r.text
    return r.text


def _cell(html: str, perm: str, role: str) -> str:
    import re

    m = re.search(rf'<input[^>]*id="perm-{perm}-{role}"[^>]*>', html)
    assert m, f"no matrix cell rendered for {perm}/{role}"
    return m.group(0)


async def test_matrix_renders_checkboxes_for_owner(ui):
    """The owner sees interactive checkbox cells wired to the per-toggle route."""
    html = await _render_users_tab(ui, "owner", {})
    assert 'type="checkbox"' in html
    # A default-granted, grantable cell is interactive for the owner.
    assert 'hx-patch="/settings/roles/edit_documents/operator"' in html
    assert "checked" in _cell(html, "edit_documents", "operator")


async def test_matrix_disabled_for_admin(ui):
    """An admin sees the same matrix, every cell disabled and none wired to save."""
    html = await _render_users_tab(ui, "admin", {})
    assert 'type="checkbox"' in html
    assert 'hx-patch="/settings/roles/' not in html
    assert "disabled" in _cell(html, "edit_documents", "operator")


async def test_matrix_reflects_overrides(ui):
    """A stored override checks the lower role's column that the default leaves clear."""
    assert "checked" not in _cell(await _render_users_tab(ui, "owner", {}),
                                  "set_inventory_prices", "operator")
    html = await _render_users_tab(ui, "owner",
                                   {"role_grants": {"set_inventory_prices": _roles_from("operator")}})
    assert "checked" in _cell(html, "set_inventory_prices", "operator")


async def test_matrix_role_columns_from_registry(ui):
    """Columns come from the ROLES registry, not hardcoded role literals."""
    from ui.i18n import t
    from celerp.services.permissions import ROLES

    html = await _render_users_tab(ui, "owner", {})
    for role in ROLES:
        assert t(role.label_key) in html, role.key
        # every registry role is a real column: it has a cell in a grantable row
        _cell(html, "edit_documents", role.key)


async def test_matrix_row_swap_reflects_single_cell(client, session):
    """Toggling a cell returns the re-rendered row with exactly that role's box now
    checked; roles above it are not filled in by the toggle (grants are per-role)."""
    from unittest.mock import patch

    from httpx import ASGITransport, AsyncClient

    from celerp.main import app as api_app

    ctx = await perm_setup(client, session)
    owner_token = ctx["admin_h"]["Authorization"].split()[1]

    def _bridged_client(token, timeout=10.0):
        return AsyncClient(
            transport=ASGITransport(app=api_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )

    from ui.app import app as ui_app
    with patch("ui.api_client._client", _bridged_client):
        async with AsyncClient(transport=ASGITransport(app=ui_app),
                               base_url="http://ui", follow_redirects=False) as ui_c:
            r = await ui_c.patch(
                "/settings/roles/set_inventory_prices/operator",
                cookies={"celerp_token": owner_token},
                data={"granted": "true"},
            )
    assert r.status_code == 200, r.text
    # operator is now granted; manager holds it by default already, but viewer -
    # the role below the toggled cell - is not swept in.
    assert "checked" in _cell(r.text, "set_inventory_prices", "operator")
    assert "checked" not in _cell(r.text, "set_inventory_prices", "viewer")


# ── Dashboard UI follows the permission ───────────────────────────────────────

@pytest_asyncio.fixture
async def ui():
    from httpx import ASGITransport, AsyncClient
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app),
                           base_url="http://ui", follow_redirects=False) as c:
        yield c


def _dashboard_patches(settings: dict, vertical: str):
    from unittest.mock import AsyncMock, patch

    company = {"name": "Perm Co", "vertical": vertical,
               "currency": "THB", "settings": settings}
    valuation = {"retail_total": 1000.0, "cost_total": 400.0, "active_item_count": 3}
    return [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_valuation", AsyncMock(return_value=valuation)),
        patch("ui.api_client.get_doc_summary", AsyncMock(return_value={})),
        patch("ui.api_client.get_dashboard_kpis", AsyncMock(return_value={})),
        patch("ui.api_client.my_companies", AsyncMock(return_value={"items": [company], "total": 1})),
        patch("ui.api_client.get_ar_aging", AsyncMock(return_value={"buckets": {}})),
        patch("ui.api_client.get_activity", AsyncMock(return_value=[])),
    ]


async def _render_dashboard(ui, role: str, settings: dict, vertical: str) -> bytes:
    from contextlib import ExitStack

    from test_helpers import authed_cookies

    with ExitStack() as stack:
        for p in _dashboard_patches(settings, vertical):
            stack.enter_context(p)
        r = await ui.get("/dashboard", cookies=authed_cookies(role=role))
    assert r.status_code == 200
    return r.content


async def test_margin_redaction_follows_permission(ui):
    """The margin sub-text renders exactly when view_inventory_costs is held.

    Uses the coins vertical, whose operator-visible Stock Value (Retail) card
    carries the margin sub-label, so the assertion isolates the value strip
    from KPI-card filtering."""
    # The mocked valuation (retail 1000, cost 400) yields a 60.0% margin sub-label;
    # match that exact text so the CSS "margin:" declarations never register a hit.
    margin_label = b"margin: 60.0%"
    vertical = "coins_precious_metals"
    content = await _render_dashboard(ui, "operator", {}, vertical)
    assert margin_label not in content

    content = await _render_dashboard(ui, "operator", _GRANTED, vertical)
    assert margin_label in content

    content = await _render_dashboard(ui, "manager", {}, vertical)
    assert margin_label in content


async def test_cost_basis_kpi_follows_permission(ui):
    """The cost KPI card shows exactly for roles holding view_inventory_costs."""
    vertical = "gemstones"
    content = await _render_dashboard(ui, "operator", {}, vertical)
    assert b"Cost Basis" not in content

    content = await _render_dashboard(ui, "operator", _GRANTED, vertical)
    assert b"Cost Basis" in content

    content = await _render_dashboard(ui, "manager", {}, vertical)
    assert b"Cost Basis" in content


# ── Default parity, override flips, and write floors (2.8) ─────────────────────

async def test_owner_can_grant_write_to_viewer(client, session):
    """The owner may grant a write key to viewer: the matrix save returns 200 and
    the resolver then admits a viewer. Viewers stay read-only by default; the owner
    is free to grant any grantable key to viewer."""
    from celerp.services.permissions import role_has_permission

    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "set_inventory_prices", "role_key": "viewer", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

    settings = await _company_settings(client, ctx["admin_h"])
    assert role_has_permission(settings, "viewer", "set_inventory_prices") is True


async def test_default_parity_matrix(client, session):
    """With empty overrides, each default tier's representative endpoint allows the
    role at its default and denies the role one level below - the exact thresholds
    the enforcement sites carried before the registry. The per-key resolver parity
    is proven exhaustively by test_defaults_match_current_behavior; this pins the
    HTTP behavior at every tier boundary."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}
    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.example', 'viewer')}"}

    def _item(sku):
        return {"sku": sku, "name": "Par", "quantity": 1,
                "location_id": ctx["location_id"], "sell_by": "piece"}

    # operator tier: edit_inventory (POST /items). viewer denied, operator allowed.
    assert (await client.post("/items", json=_item("PAR-V"), headers=viewer_h)).status_code == 403
    assert (await client.post("/items", json=_item("PAR-O"), headers=ctx["operator_h"])).status_code == 200

    # manager tier: set_inventory_prices (POST /items/{id}/price). operator denied, manager allowed.
    price = {"price_type": "retail_price", "new_price": 12.0}
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 403
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["manager_h"])).status_code == 200

    # admin tier: manage_company_settings (PATCH /companies/me). manager denied, admin allowed.
    assert (await client.patch("/companies/me", json={"name": "Par X"}, headers=ctx["manager_h"])).status_code == 403
    assert (await client.patch("/companies/me", json={"name": "Par Y"}, headers=admin_h)).status_code == 200

    # owner tier: manage_permissions (PATCH matrix). admin denied, owner allowed.
    grant = {"perm_key": "set_inventory_prices", "role_key": "operator", "granted": True}
    assert (await client.patch(_ROLE_PERM_URL, json=grant, headers=admin_h)).status_code == 403
    assert (await client.patch(_ROLE_PERM_URL, json=grant, headers=ctx["admin_h"])).status_code == 200


async def test_override_grant_flips_gate(client, session):
    """Granting a key to a role below its default flips that role's representative
    endpoint from 403 to success."""
    ctx = await perm_setup(client, session)
    price = {"price_type": "retail_price", "new_price": 20.0}
    # operator lacks set_inventory_prices (manager default) by default.
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 403
    await grant_permission(client, ctx["admin_h"], "set_inventory_prices", "operator")
    assert (await client.post(f"/items/{ctx['item_id']}/price", json=price, headers=ctx["operator_h"])).status_code == 200


async def test_override_revoke_flips_gate(client, session):
    """Raising a key's threshold above a role flips that role's representative
    endpoint from success to 403."""
    ctx = await perm_setup(client, session)

    def _item(sku):
        return {"sku": sku, "name": "Rev", "quantity": 1,
                "location_id": ctx["location_id"], "sell_by": "piece"}

    # operator holds edit_inventory (operator default) today.
    assert (await client.post("/items", json=_item("REV-1"), headers=ctx["operator_h"])).status_code == 200
    await grant_permission(client, ctx["admin_h"], "edit_inventory", "manager")
    assert (await client.post("/items", json=_item("REV-2"), headers=ctx["operator_h"])).status_code == 403


async def test_fixed_rows_reject_patch(client, session):
    """PATCH targeting any fixed row is refused for everyone, owner included."""
    ctx = await perm_setup(client, session)
    for key in ("manage_permissions", "manage_company_lifecycle", "manage_billing"):
        r = await client.patch(
            _ROLE_PERM_URL,
            json={"perm_key": key, "role_key": "admin", "granted": True},
            headers=ctx["admin_h"],
        )
        assert r.status_code == 403, (key, r.text)


def test_nav_visibility_follows_permissions():
    """Sidebar entries render exactly when the caller's resolved role holds the
    entry's permission: a granted lower role gains an entry, a revoked higher role
    loses one."""
    import os
    from celerp.modules import slots
    from celerp.modules.loader import load_all
    from fasthtml.common import to_xml

    from ui.components.shell import _sidebar

    # Register the docs nav slots deterministically: the global slot registry's
    # contents depend on which tests ran earlier in this worker, so rebuild it
    # from the real manifests. load_all refuses a module whose depends_on chain
    # is not enabled, so celerp-docs needs its dependencies alongside it.
    saved = slots.all_slots()
    slots.clear()
    load_all(
        os.environ.get("MODULE_DIR") or "default_modules",
        {"celerp-inventory", "celerp-contacts", "celerp-docs"},
    )
    try:
        # Payments gates on view_payments (manager default): operator cannot see it...
        assert "/payments" not in to_xml(_sidebar("dashboard", role="operator", settings={}))
        # ...until it is granted down to operator.
        granted = {"role_grants": {"view_payments": _roles_from("operator")}}
        assert "/payments" in to_xml(_sidebar("dashboard", role="operator", settings=granted))

        # Sales Documents gate on view_documents (viewer default): a manager sees them,
        # but not once the key is raised to admin.
        assert "/docs?type=invoice" in to_xml(_sidebar("dashboard", role="manager", settings={}))
        revoked = {"role_grants": {"view_documents": _roles_from("admin")}}
        assert "/docs?type=invoice" not in to_xml(_sidebar("dashboard", role="manager", settings=revoked))
    finally:
        slots.clear()
        for slot_name, contributions in saved.items():
            for contribution in contributions:
                slots.register(slot_name, contribution)


def test_kpi_specs_follow_permissions():
    """Dashboard KPI cards filter on permission membership: a permissioned card
    shows only for roles holding the key; ungated cards always show."""
    from fasthtml.common import to_xml

    from ui.routes.dashboard import _kpi_grid

    cfg = {"kpis": [
        {"label": "Cost Basis", "value_fn": "cost_total", "permission": "view_inventory_costs"},
        {"label": "Item Count", "value_fn": "item_count"},
    ]}
    values = {"cost_total": "400", "item_count": "3"}

    op = to_xml(_kpi_grid(cfg, values, role="operator", settings={}))
    assert "Cost Basis" not in op and "Item Count" in op

    granted = {"role_grants": {"view_inventory_costs": _roles_from("operator")}}
    assert "Cost Basis" in to_xml(_kpi_grid(cfg, values, role="operator", settings=granted))
    assert "Cost Basis" in to_xml(_kpi_grid(cfg, values, role="manager", settings={}))


async def test_owner_column_fixed(ui):
    """The owner column renders checked and disabled in every grantable row: the
    owner holds everything and cannot be unset."""
    html = await _render_users_tab(ui, "owner", {})
    for perm in ("edit_documents", "set_inventory_prices", "manage_users"):
        cell = _cell(html, perm, "owner")
        assert "checked" in cell, perm
        assert "disabled" in cell, perm


async def test_deactivate_owner_only(client, session):
    """Company deactivation is owner-only: an admin is refused, the owner succeeds."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}
    assert (await client.delete("/companies/me", headers=admin_h)).status_code == 403
    assert (await client.delete("/companies/me", headers=ctx["admin_h"])).status_code == 200


async def test_reactivate_and_reseed_owner_only(client, session):
    """Reactivation and demo reseed are owner-only like deactivation."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}

    # Reseed on the active company: the gate refuses the admin, the owner succeeds.
    assert (await client.post("/companies/me/demo/reseed", headers=admin_h)).status_code == 403
    assert (await client.post("/companies/me/demo/reseed", headers=ctx["admin_h"])).status_code == 200

    # Reactivate gate refuses the admin while the company is still active (the gate
    # runs before any state check); the owner then deactivates and reactivates.
    assert (await client.post("/companies/me/reactivate", headers=admin_h)).status_code == 403
    assert (await client.delete("/companies/me", headers=ctx["admin_h"])).status_code == 200
    assert (await client.post("/companies/me/reactivate", headers=ctx["admin_h"])).status_code == 200


def test_web_access_link_requires_integrations():
    """The footer Web Access link renders only for a role holding manage_integrations
    (admin default): absent for a manager, present for an admin."""
    from fasthtml.common import to_xml

    from ui.components.shell import _sidebar

    assert "/settings/cloud" not in to_xml(_sidebar("dashboard", role="manager", settings={}))
    assert "/settings/cloud" in to_xml(_sidebar("dashboard", role="admin", settings={}))


async def test_ai_routes_require_permission(client, session):
    """An AI endpoint returns 403 for a viewer under default permissions; an
    operator (holding use_ai_assistant by default) is admitted. The AI router also
    sits behind the Cloud+AI subscription gate (require_session_token); this test
    isolates the permission gate by satisfying that subscription gate, so a plain
    subscription pass cannot be mistaken for a permission pass."""
    from celerp.main import app
    from celerp.session_gate import require_session_token

    ctx = await perm_setup(client, session)
    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.example', 'viewer')}"}
    app.dependency_overrides[require_session_token] = lambda: None
    try:
        assert (await client.get("/ai/memory", headers=viewer_h)).status_code == 403
        assert (await client.get("/ai/memory", headers=ctx["operator_h"])).status_code == 200
    finally:
        app.dependency_overrides.pop(require_session_token, None)


async def test_accounting_reads_require_permission(client, session):
    """The chart-of-accounts read returns 403 for an operator; a manager (holding
    manage_accounting by default) is admitted."""
    ctx = await perm_setup(client, session)
    assert (await client.get("/accounting/chart", headers=ctx["operator_h"])).status_code == 403
    assert (await client.get("/accounting/chart", headers=ctx["manager_h"])).status_code == 200


def test_bulk_action_filter_enforced():
    """A permission-gated bulk action is dropped from the inventory bulk toolbar for
    a role lacking the key and kept for a role holding it, while an ungated action
    always shows. The gated shape mirrors the connectors module's adjust_inventory
    contribution; injecting it keeps the test independent of which optional modules
    a given deployment enables."""
    from unittest.mock import patch

    from fasthtml.common import to_xml

    from ui.routes.inventory import _bulk_toolbar

    actions = [
        {"label": "Enable Shopify sync", "form_action": "/api/items/bulk/shopify-sync/enable",
         "icon": "🛍", "action_type": "htmx", "permission": "adjust_inventory",
         "_module": "celerp-connectors"},
        {"label": "Print Labels", "form_action": "/labels/print-bulk",
         "icon": "🖨", "action_type": "navigate", "_module": "celerp-labels"},
    ]

    def _fake_get(slot):
        return list(actions) if slot == "bulk_action" else []

    with patch("celerp.modules.slots.get", side_effect=_fake_get):
        operator_html = to_xml(_bulk_toolbar([], settings={}, role="operator"))
        manager_html = to_xml(_bulk_toolbar([], settings={}, role="manager"))

    # adjust_inventory defaults to manager: the operator loses the gated action,
    # keeps the ungated one; the manager sees both.
    assert "Enable Shopify sync" not in operator_html
    assert "Print Labels" in operator_html
    assert "Enable Shopify sync" in manager_html


async def test_payments_page_requires_permission(ui):
    """/payments redirects an operator (lacking view_payments, a manager default) to
    the dashboard rather than rendering the payments list."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB", "settings": {}}
    with patch("ui.api_client.get_company", AsyncMock(return_value=company)):
        r = await ui.get("/payments", cookies={"celerp_token": make_test_token(role="operator")})
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


async def test_raised_view_inventory_redirects_viewer(ui):
    """With view_inventory raised to operator, a viewer requesting /inventory is
    redirected to the dashboard. The page reads settings from the company, so the
    raised override is enough to close the page to the viewer."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB",
               "settings": {"role_grants": {"view_inventory": _roles_from("operator")}}}
    patches = [
        patch("ui.api_client.get_company", AsyncMock(return_value=company)),
        patch("ui.api_client.get_item_schema", AsyncMock(return_value={})),
        patch("ui.api_client.get_all_category_schemas", AsyncMock(return_value={})),
        patch("ui.api_client.get_column_prefs", AsyncMock(return_value={})),
        patch("ui.api_client.get_locations", AsyncMock(return_value={"items": []})),
    ]
    for p in patches:
        p.start()
    try:
        r = await ui.get("/inventory", cookies={"celerp_token": make_test_token(role="viewer")})
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"


async def _assert_page_redirects_when_revoked(ui, path: str, perm_key: str):
    """A viewer requesting *path* with *perm_key* raised to operator is redirected
    to the dashboard by the page-level gate, before the handler loads any data."""
    from unittest.mock import AsyncMock, patch

    from test_helpers import make_test_token

    company = {"currency": "THB",
               "settings": {"role_grants": {perm_key: _roles_from("operator")}}}
    with patch("ui.api_client.get_company", AsyncMock(return_value=company)):
        r = await ui.get(path, cookies={"celerp_token": make_test_token(role="viewer")})
    assert r.status_code == 302, (path, r.status_code)
    assert r.headers["location"] == "/dashboard", path


async def test_dashboard_page_requires_view_dashboards(ui):
    """A viewer whose view_dashboards is revoked gets the not-authorized shell on
    /dashboard, not the KPI grid. The page has nowhere to redirect (it is the
    redirect target), so it degrades in place with a clear message."""
    no_access = b"You do not have access to this page."
    revoked = {"role_grants": {"view_dashboards": _roles_from("operator")}}
    content = await _render_dashboard(ui, "viewer", revoked, "coins_precious_metals")
    assert no_access in content

    allowed = await _render_dashboard(ui, "viewer", {}, "coins_precious_metals")
    assert no_access not in allowed


async def test_history_page_requires_view_dashboards(ui):
    await _assert_page_redirects_when_revoked(ui, "/history", "view_dashboards")


async def test_docs_page_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/docs", "view_documents")


async def test_lists_page_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/lists", "view_documents")


async def test_doc_detail_requires_view_documents(ui):
    await _assert_page_redirects_when_revoked(ui, "/docs/doc_1", "view_documents")


async def test_customers_page_requires_view_contacts(ui):
    await _assert_page_redirects_when_revoked(ui, "/contacts/customers", "view_contacts")


async def test_vendors_page_requires_view_contacts(ui):
    await _assert_page_redirects_when_revoked(ui, "/contacts/vendors", "view_contacts")


async def test_item_detail_requires_view_inventory(ui):
    await _assert_page_redirects_when_revoked(ui, "/inventory/item_1", "view_inventory")


# ── Amount-edit permission (edit_inventory_amounts) ───────────────────────────

# The four hand-editable amount fields the new key gates. Hardcoded (not imported
# from celerp.services.field_schema) so the parametrize list resolves at collection
# time even where that symbol does not yet exist.
_AMOUNT_KEYS = ["quantity", "weight", "pieces", "gross_weight"]


def test_edit_inventory_amounts_in_catalogue():
    """The registry carries edit_inventory_amounts: operator default, grantable,
    viewer floor - a distinct key from edit_inventory."""
    from celerp.services.permissions import PERMISSIONS

    by_key = {p.key: p for p in PERMISSIONS}
    assert "edit_inventory_amounts" in by_key
    p = by_key["edit_inventory_amounts"]
    assert p.default_role == "operator"
    assert p.grantable is True
    assert p.floor_role == "viewer"
    assert p.label
    assert "edit_inventory" in by_key  # the amounts key does not replace the base key


def _amount_patch(field: str) -> dict:
    """An ItemPatch body that hand-edits one amount field."""
    old_new = {"quantity": (5, 3), "weight": (None, 2), "pieces": (None, 5),
               "gross_weight": (None, 2)}[field]
    return {"fields_changed": {field: {"old": old_new[0], "new": old_new[1]}}}


@pytest.mark.parametrize("field", _AMOUNT_KEYS)
async def test_amount_edit_denied_without_permission(client, session, field):
    """With edit_inventory_amounts raised above operator, an operator that still
    holds edit_inventory is blocked from hand-editing any amount field; the 403
    names the blocked field."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.patch(f"/items/{ctx['item_id']}", json=_amount_patch(field),
                           headers=ctx["operator_h"])
    assert r.status_code == 403, r.text
    assert field in r.json()["detail"]


async def test_amount_edit_allowed_with_permission(client, session):
    """An operator holding edit_inventory_amounts (its operator default) hand-edits
    an amount field successfully."""
    ctx = await perm_setup(client, session)
    r = await client.patch(f"/items/{ctx['item_id']}", json=_amount_patch("quantity"),
                           headers=ctx["operator_h"])
    assert r.status_code == 200, r.text


async def test_nonamount_edit_allowed_without_amount_permission(client, session):
    """Editing a non-amount field (name) stays allowed for a role lacking
    edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.patch(
        f"/items/{ctx['item_id']}",
        json={"fields_changed": {"name": {"old": "Perm Item", "new": "Renamed Item"}}},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def _mergeable_pair(client, headers, location_id):
    """Create two same-category, same-unit items; return (id_a, id_b)."""
    def _body(sku, qty):
        return {"sku": sku, "name": sku, "quantity": qty, "category": "Raw",
                "location_id": location_id, "sell_by": "piece"}
    a = (await client.post("/items", json=_body("MRG-A", 5), headers=headers)).json()["id"]
    b = (await client.post("/items", json=_body("MRG-B", 3), headers=headers)).json()["id"]
    return a, b


async def _splittable_item(client, headers, location_id, sku="SPL-1", weight=10.0):
    """Create a splittable weight-bearing item; return its entity_id."""
    r = await client.post(
        "/items",
        json={"sku": sku, "name": sku, "quantity": 10, "location_id": location_id,
              "sell_by": "piece", "weight": weight, "allow_splitting": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_split_still_allowed_without_amount_permission(client, session):
    """A plain split (no mother re-weigh override) is allowed for a role lacking
    edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    item_id = await _splittable_item(client, ctx["admin_h"], ctx["location_id"])
    r = await client.post(f"/items/{item_id}/split",
                          json={"children": [{"sku": "SPL-1.1", "quantity": 3}]},
                          headers=ctx["operator_h"])
    assert r.status_code == 200, r.text


async def test_merge_still_allowed_without_amount_permission(client, session):
    """A natural merge (no resulting_quantity override) is allowed for a role
    lacking edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    a, b = await _mergeable_pair(client, ctx["admin_h"], ctx["location_id"])
    r = await client.post("/items/merge",
                          json={"source_entity_ids": [a, b], "target_sku_from": a},
                          headers=ctx["operator_h"])
    assert r.status_code == 200, r.text


async def test_merge_override_denied_without_amount_permission(client, session):
    """A resulting_quantity that differs from the natural summed total is a hand
    override of an amount, blocked for a role lacking edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    a, b = await _mergeable_pair(client, ctx["admin_h"], ctx["location_id"])
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": [a, b], "target_sku_from": a, "resulting_quantity": 99.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403, r.text
    assert "quantity" in r.json()["detail"].lower()


async def test_merge_negative_override_rejected(client, session):
    """A negative resulting_quantity is rejected on value, independent of role."""
    ctx = await perm_setup(client, session)
    a, b = await _mergeable_pair(client, ctx["admin_h"], ctx["location_id"])
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": [a, b], "target_sku_from": a, "resulting_quantity": -5.0},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


async def test_adjust_still_allowed_without_amount_permission(client, session):
    """adjust_item is gated by adjust_inventory, not by edit_inventory_amounts: a
    role granted adjust_inventory but lacking the amount key can still adjust."""
    ctx = await perm_setup(client, session)
    # Grant adjust_inventory down to operator AND raise edit_inventory_amounts above
    # it, so the operator has the adjust gate but not the amount gate.
    await grant_permission(client, ctx["admin_h"], "adjust_inventory", "operator")
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.post(f"/items/{ctx['item_id']}/adjust", json={"new_qty": 3},
                          headers=ctx["operator_h"])
    assert r.status_code == 200, r.text


async def test_sell_by_change_denied_without_amount_permission(client, session):
    """sell_by is gated under edit_inventory_amounts: a role lacking that key cannot
    change it, and the 403 names the field so the user knows why the edit failed."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    r = await client.patch(
        f"/items/{ctx['item_id']}",
        json={"fields_changed": {"sell_by": {"old": "piece", "new": "gram"}}},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403, r.text
    assert "sell_by" in r.json()["detail"]


async def test_sell_by_change_allowed_with_amount_permission(client, session):
    """A role holding edit_inventory_amounts can change sell_by."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "operator")
    r = await client.patch(
        f"/items/{ctx['item_id']}",
        json={"fields_changed": {"sell_by": {"old": "piece", "new": "gram"}}},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_reweigh_denied_without_amount_permission(client, session):
    """A split mother_weight override that differs from the server-derived remainder
    is a hand re-weigh, blocked for a role lacking edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    item_id = await _splittable_item(client, ctx["admin_h"], ctx["location_id"])
    # derived remainder = 10 - 3 = 7; override 6.0 differs.
    r = await client.post(
        f"/items/{item_id}/split",
        json={"children": [{"sku": "SPL-1.1", "quantity": 3, "weight": 3.0}],
              "mother_weight": 6.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 403, r.text
    assert "weight" in r.json()["detail"].lower()


async def test_split_derived_reweigh_allowed_without_amount_permission(client, session):
    """A mother_weight override equal to the server-derived remainder is not a hand
    re-weigh and stays allowed for a role lacking edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    item_id = await _splittable_item(client, ctx["admin_h"], ctx["location_id"])
    # derived remainder = 10 - 3 = 7; override equals it.
    r = await client.post(
        f"/items/{item_id}/split",
        json={"children": [{"sku": "SPL-1.1", "quantity": 3, "weight": 3.0}],
              "mother_weight": 7.0},
        headers=ctx["operator_h"],
    )
    assert r.status_code == 200, r.text


async def test_split_negative_reweigh_rejected(client, session):
    """A negative mother_weight is rejected on value, independent of role."""
    ctx = await perm_setup(client, session)
    item_id = await _splittable_item(client, ctx["admin_h"], ctx["location_id"])
    r = await client.post(
        f"/items/{item_id}/split",
        json={"children": [{"sku": "SPL-1.1", "quantity": 3, "weight": 3.0}],
              "mother_weight": -5.0},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 422, r.text


def _import_record(entity_id: str, data: dict, key: str, event_type: str = "item.created") -> dict:
    return {"entity_id": entity_id, "event_type": event_type, "data": data,
            "source": "csv", "idempotency_key": key}


async def test_batch_import_amount_denied_without_permission(client, session):
    """A batch upsert whose record data carries an amount key is skipped-with-error
    for a role lacking edit_inventory_amounts; no amount is written."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    eid = "item:bi-amt"
    # Seed via a create import (create carries quantity but is not amount-gated).
    r = await client.post("/items/import/batch", json={"records": [
        _import_record(eid, {"sku": "BI-1", "name": "BI 1", "sell_by": "piece", "quantity": 5}, "bi-amt")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    # Upsert the same key with an amount key (weight) present.
    r = await client.post("/items/import/batch", json={"upsert": True, "records": [
        _import_record(eid, {"sku": "BI-1", "sell_by": "piece", "weight": 5.0}, "bi-amt")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["updated"] == 0
    assert result["errors"]
    # The blocked amount never landed on the item.
    item = (await client.get(f"/items/{eid}", headers=ctx["admin_h"])).json()
    assert item.get("weight") in (None, "")


async def test_batch_import_nonamount_allowed_without_permission(client, session):
    """A batch upsert of a non-amount field (name) is applied for a role lacking
    edit_inventory_amounts."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    eid = "item:bi-name"
    r = await client.post("/items/import/batch", json={"records": [
        _import_record(eid, {"sku": "BI-2", "name": "BI 2", "sell_by": "piece", "quantity": 5}, "bi-name")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200 and r.json()["created"] == 1, r.text


async def test_bulk_import_sell_by_change_denied_without_permission(client, session):
    """A batch upsert whose sell_by differs from the stored value is a hand-edit of
    an amount-gated field: skipped-with-error for a role lacking
    edit_inventory_amounts, and the stored sell_by is left unchanged. The gate is
    diff-based, so re-sending the same sell_by is not blocked (covered separately)."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    eid = "item:bi-sb"
    r = await client.post("/items/import/batch", json={"records": [
        _import_record(eid, {"sku": "BI-SB", "name": "BI SB", "sell_by": "piece", "quantity": 5}, "bi-sb")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200 and r.json()["created"] == 1, r.text

    # Upsert the same key changing sell_by piece -> gram.
    r = await client.post("/items/import/batch", json={"upsert": True, "records": [
        _import_record(eid, {"sku": "BI-SB", "sell_by": "gram"}, "bi-sb")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["updated"] == 0
    assert result["errors"]
    # The blocked sell_by change never landed on the item.
    item = (await client.get(f"/items/{eid}", headers=ctx["admin_h"])).json()
    assert item.get("sell_by") == "piece"


async def test_bulk_import_non_sellby_change_allowed_without_permission(client, session):
    """A batch upsert that leaves sell_by unchanged (only name changes) is applied
    for a role lacking edit_inventory_amounts: the diff carries no gated field."""
    ctx = await perm_setup(client, session)
    await grant_permission(client, ctx["admin_h"], "edit_inventory_amounts", "manager")
    eid = "item:bi-nsb"
    r = await client.post("/items/import/batch", json={"records": [
        _import_record(eid, {"sku": "BI-NSB", "name": "BI NSB", "sell_by": "piece", "quantity": 5}, "bi-nsb")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200 and r.json()["created"] == 1, r.text

    r = await client.post("/items/import/batch", json={"upsert": True, "records": [
        _import_record(eid, {"sku": "BI-NSB", "name": "BI NSB renamed", "sell_by": "piece"}, "bi-nsb")
    ]}, headers=ctx["operator_h"])
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["updated"] == 1
    assert not result["errors"]


async def test_batch_import_negative_amount_rejected(client, session):
    """A batch record carrying a negative amount is skipped-with-error even for a
    role holding edit_inventory_amounts; the negative is never written."""
    ctx = await perm_setup(client, session)
    eid = "item:bi-neg"
    r = await client.post("/items/import/batch", json={"records": [
        _import_record(eid, {"sku": "BI-3", "name": "BI 3", "sell_by": "piece",
                             "quantity": 1, "weight": -5.0}, "bi-neg")
    ]}, headers=ctx["admin_h"])
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 0
    assert result["errors"]


@pytest.mark.parametrize("field", _AMOUNT_KEYS)
async def test_create_negative_amount_rejected(client, session, field):
    """Creating an item with a negative amount value is rejected 422 naming the
    field, for every amount key."""
    ctx = await perm_setup(client, session)
    body = {"sku": f"NEG-{field}", "name": "Neg", "sell_by": "piece",
            "location_id": ctx["location_id"], "quantity": 1}
    body[field] = -5
    r = await client.post("/items", json=body, headers=ctx["admin_h"])
    assert r.status_code == 422, r.text
    assert field in r.json()["detail"]


def test_guard_family_removed():
    """No source file under celerp/, ui/, or default_modules/ still names the deleted
    guard family or the old min_role nav vocabulary."""
    import pathlib
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    banned = ["require_admin", "require_operator", "require_manager", "require_min_role",
              "viewer_read_only", "_check_role", "min_role"]
    pattern = r"\b(" + "|".join(banned) + r")\b"
    r = subprocess.run(
        ["grep", "-rnE", pattern, "celerp", "ui", "default_modules", "--include=*.py"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert r.returncode == 1, f"guard-family stragglers found:\n{r.stdout}"


# ── User-management caller-role ceiling (Fix 1: relaxing the manage_users floor
#    must not become an owner-escalation path) ──────────────────────────────────

async def test_manage_users_holder_cannot_promote_above_own_role(client, session):
    """An admin holds manage_users by default; it must not let them mint or promote
    a user to a role above their own. Creating an owner and patching a user up to
    owner both return 403, closing the escalation the floor relaxation would widen."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}

    r = await client.post(
        "/companies/me/users",
        json={"email": "newowner@perm.example", "name": "NO", "role": "owner", "password": "pw123"},
        headers=admin_h,
    )
    assert r.status_code == 403, r.text

    await invite_user(client, session, ctx["admin_h"], "victim@perm.example", "operator")
    users = (await client.get("/companies/me/users", headers=ctx["admin_h"])).json()["items"]
    victim_id = next(u["id"] for u in users if u["email"] == "victim@perm.example")
    r2 = await client.patch(
        f"/companies/me/users/{victim_id}",
        json={"role": "owner"},
        headers=admin_h,
    )
    assert r2.status_code == 403, r2.text


async def test_manage_users_holder_cannot_modify_higher_user(client, session):
    """An admin cannot modify a user whose current role is above their own: patching
    the owner (deactivating them) returns 403, never a silent success."""
    ctx = await perm_setup(client, session)
    admin_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'adm@perm.example', 'admin')}"}
    users = (await client.get("/companies/me/users", headers=ctx["admin_h"])).json()["items"]
    owner_id = next(u["id"] for u in users if u["role"] == "owner")
    r = await client.patch(
        f"/companies/me/users/{owner_id}",
        json={"is_active": False},
        headers=admin_h,
    )
    assert r.status_code == 403, r.text


async def test_viewer_granted_manage_users_bounded_to_viewer(client, session):
    """Once the owner grants manage_users to viewer, that viewer can add users at or
    below their own level but not above: creating a viewer is 200, creating an
    operator is 403 (the caller-role ceiling), so the grant is safe."""
    ctx = await perm_setup(client, session)
    r = await client.patch(
        _ROLE_PERM_URL,
        json={"perm_key": "manage_users", "role_key": "viewer", "granted": True},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

    viewer_h = {"Authorization": f"Bearer {await invite_user(client, session, ctx['admin_h'], 'vwr@perm.example', 'viewer')}"}
    ok = await client.post(
        "/companies/me/users",
        json={"email": "v2@perm.example", "name": "V2", "role": "viewer", "password": "pw123"},
        headers=viewer_h,
    )
    assert ok.status_code == 200, ok.text
    denied = await client.post(
        "/companies/me/users",
        json={"email": "op2@perm.example", "name": "OP2", "role": "operator", "password": "pw123"},
        headers=viewer_h,
    )
    assert denied.status_code == 403, denied.text


async def test_owner_can_still_create_owner(client, session):
    """The ceiling must not over-tighten: an owner (top level) can still create an
    owner. Green at merge-base too; it guards against a ceiling that wrongly blocks
    owners themselves."""
    ctx = await perm_setup(client, session)
    r = await client.post(
        "/companies/me/users",
        json={"email": "co-owner@perm.example", "name": "CoOwner", "role": "owner", "password": "pw123"},
        headers=ctx["admin_h"],
    )
    assert r.status_code == 200, r.text

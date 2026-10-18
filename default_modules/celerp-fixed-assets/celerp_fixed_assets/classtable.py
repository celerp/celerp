# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A country's class table: the asset classes a localization module suggests, applied
to the company's classes in one previewed change.

Each entry is matched to a class by name. A class the table names and the company has
is updated with what differs; one it does not have is created. Every entry is checked as
the class it would become before anything changes, and one that cannot be a class is
refused by its name. Undoing the table puts back what nobody has changed since, archives
the classes it created, and lists the classes it kept because they were edited after.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException

from celerp_fixed_assets import setup
from celerp_fixed_assets.operations import _effects, _run, conflict
from celerp_fixed_assets.store import _emit, _state


def _named(name: str, exc: HTTPException) -> HTTPException:
    return setup.unprocessable(f"{name}: {exc.detail}")


def _fields(state: dict) -> dict:
    return {k: state.get(k) for k in setup.CLASS_FIELDS}


async def apply_table(session, company_id, user_id, body: dict) -> dict:
    """Preview or confirm the class table in ``body["classes"]``."""
    table = body.get("classes")
    if not isinstance(table, list) or not all(isinstance(e, dict) for e in table):
        raise setup.unprocessable("A class table is a list of classes.")

    async def plan(fa):
        settings = await setup.company_settings(session, company_id)
        current = await setup.classes(session, company_id, include_archived=True)
        by_name = {c["name"]: c for c in current if not c.get("archived")}
        create, update, seen = [], [], set()
        for entry in table:
            name = str(entry.get("name") or "").strip()
            if not name or name in seen:
                raise setup.unprocessable("Every class in the table has its own name.")
            seen.add(name)
            fields = {k: v for k, v in entry.items() if k != "name"}
            try:
                if name in by_name:
                    cls = by_name[name]
                    changes = await setup.class_changes(session, company_id, settings, cls["id"], cls, fields)
                    if changes:
                        update.append({"id": cls["id"], "name": name, "changes": changes})
                else:
                    data = setup.new_class({"name": name, **fields}, settings, len(current) + len(create))
                    await setup.check_class(session, company_id, data, settings)
                    create.append(data)
            except HTTPException as exc:
                if exc.status_code != 422:
                    raise
                raise _named(name, exc) from None
        shown = {"create": create, "update": update}
        return shown, {**shown, "basis": [_fields(by_name[u["name"]]) for u in update]}

    async def apply(context, op_key):
        table_effects = []
        for data in context["create"]:
            class_id = f"fa:class:{uuid.uuid4()}"
            await setup._emit(session, company_id, user_id, class_id, "fa.class.created", data)
            table_effects.append({"id": class_id, "created": True, "before": None, "after": _fields(data)})
        for u in context["update"]:
            before = _fields(await _state(session, company_id, u["id"]) or {})
            await setup._emit(session, company_id, user_id, u["id"], "fa.class.updated", {"changes": u["changes"]})
            table_effects.append({"id": u["id"], "created": False, "before": {k: before[k] for k in u["changes"]},
                                  "after": {**before, **u["changes"]}})
        result = {"created": [d["name"] for d in context["create"]], "updated": [u["name"] for u in context["update"]]}
        return result, _effects(table=table_effects)

    return await _run(session, company_id, user_id, body, op_type="class_table", keys=("manage_accounting",),
                      plan=plan, apply=apply, managed=False)


async def undo_table(session, company_id, user_id, target_key: str, body: dict) -> dict:
    """Preview or confirm undoing the class table applied as ``target_key``."""

    async def plan(fa):
        op = await _state(session, company_id, f"fa:op:{target_key}")
        if op is None or op.get("type") != "class_table":
            raise HTTPException(status_code=404, detail="No class table was applied under this key.")
        if op.get("undone_by"):
            raise conflict("This class table was already undone.")
        restore, archive, kept, basis = [], [], [], []
        for effect in (op.get("effects") or {}).get("table") or []:
            state = await _state(session, company_id, effect["id"]) or {}
            basis.append(_fields(state))
            if _fields(state) != effect["after"]:
                kept.append({"id": effect["id"], "name": state.get("name")})
            elif effect["created"]:
                archive.append({"id": effect["id"], "name": state.get("name")})
            else:
                restore.append({"id": effect["id"], "name": state.get("name"), "changes": effect["before"]})
        shown = {"restore": restore, "archive": archive, "kept": kept}
        return shown, {**shown, "basis": basis}

    async def apply(context, op_key):
        for r in context["restore"]:
            await setup._emit(session, company_id, user_id, r["id"], "fa.class.updated", {"changes": r["changes"]})
        for a in context["archive"]:
            await setup._emit(session, company_id, user_id, a["id"], "fa.class.updated",
                              {"changes": {"archived": True}})
        await _emit(session, company_id, user_id, f"fa:op:{target_key}", "fa_op", "fa.op.undone", {"undo_key": op_key})
        result = {"undone": target_key, "restored": [r["name"] for r in context["restore"]],
                  "archived": [a["name"] for a in context["archive"]], "kept": [k["name"] for k in context["kept"]]}
        return result, _effects()

    return await _run(session, company_id, user_id, body, op_type=f"class_table_undo:{target_key}",
                      keys=("manage_accounting",), plan=plan, apply=apply, managed=False)

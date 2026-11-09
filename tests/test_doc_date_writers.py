# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every writer of a document's or List's issue_date and due_date takes a calendar day only.

The index windows and sorts records by these dates and the overdue rules compare them with
today, all as text, so a value such as "31/12/2026" or "yesterday" would sort above every real
date and fall out of every window. `census()` finds every request model of the docs module that
can carry a state field into a write: one that declares a date field, accepts undeclared fields,
or carries a free-form dict of state fields. Each must refuse a value that is not a day, so a new
door added without the check fails here.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from pydantic import BaseModel, ValidationError

import celerp_docs
from test_tax_acceptance_matrix import _demo_company

_DATE_FIELDS = ("issue_date", "due_date")
_STATE_DICTS = ("fields_changed", "data")


def census() -> dict[str, type[BaseModel]]:
    """Every docs request model that can carry a document or List date into a write."""
    found: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(celerp_docs.__path__):
        module = importlib.import_module(f"celerp_docs.{info.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if not issubclass(cls, BaseModel) or not cls.__module__.startswith("celerp_docs"):
                continue
            fields = cls.model_fields
            if (any(f in fields for f in _DATE_FIELDS) or cls.model_config.get("extra") == "allow"
                    or any(f in fields and "dict" in str(fields[f].annotation) for f in _STATE_DICTS)):
                found[cls.__name__] = cls
    return found


# How each door carries a date, built around a payload the model otherwise accepts.
_CARRY = {
    "DocCreatePayload": lambda dates: {"doc_type": "invoice", **dates},
    "ListCreatePayload": lambda dates: dates,
    "DocPatch": lambda dates: {"fields_changed": {k: {"old": None, "new": v} for k, v in dates.items()}},
    "DocImportRecord": lambda dates: {"entity_id": "doc:imported", "event_type": "doc.created", "data": dates,
                                      "source": "csv", "idempotency_key": "import-1"},
}

_NOT_A_DAY = ["not-a-date", "31/12/2026", "2026-13-45", "yesterday", "20261231", "2026-11-06junk"]
_A_DAY = ["2026-11-06", "2026-11-06T10:00:00+07:00", "", None]


def test_every_door_is_checked():
    assert sorted(census()) == sorted(_CARRY), "a docs request model can write a date: add it to _CARRY"


@pytest.mark.parametrize("model", sorted(_CARRY))
@pytest.mark.parametrize("field", _DATE_FIELDS)
@pytest.mark.parametrize("value", _NOT_A_DAY)
def test_a_value_that_is_not_a_day_is_refused(model, field, value):
    with pytest.raises(ValidationError, match="is not a date"):
        census()[model].model_validate(_CARRY[model]({field: value}))


@pytest.mark.parametrize("model", sorted(_CARRY))
@pytest.mark.parametrize("field", _DATE_FIELDS)
@pytest.mark.parametrize("value", _A_DAY)
def test_a_day_is_taken(model, field, value):
    census()[model].model_validate(_CARRY[model]({field: value}))


async def test_list_and_document_writers_refuse_a_value_that_is_not_a_day(client, session):
    auth, _ = await _demo_company(client, session, "USD")
    h = auth["headers"]
    before = (await client.get("/lists", headers=h)).json()["total"]
    for bad in ("not-a-date", "31/12/2026", "2026-13-45"):
        r = await client.post("/lists", headers=h, json={"list_type": "quotation", "issue_date": bad})
        assert r.status_code == 422, (bad, r.text)
        r = await client.post("/docs", headers=h, json={"doc_type": "invoice", "issue_date": bad})
        assert r.status_code == 422, (bad, r.text)
    assert (await client.get("/lists", headers=h)).json()["total"] == before, "a refused List was stored"

    made = await client.post("/lists", headers=h, json={"list_type": "quotation", "issue_date": "2026-11-02"})
    assert made.status_code == 200, made.text
    lid = made.json()["id"]
    r = await client.patch(f"/lists/{lid}", headers=h,
                           json={"fields_changed": {"issue_date": {"old": "2026-11-02", "new": "yesterday"}}})
    assert r.status_code == 422, r.text
    assert (await client.get(f"/lists/{lid}", headers=h)).json()["issue_date"] == "2026-11-02"

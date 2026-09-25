# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from celerp.models.company import Company
from celerp.services.business_time import business_date_at
from celerp.services.pick import PickLine, PickResult


def test_business_date_uses_business_timezone_and_rejects_ambiguity():
    assert business_date_at(
        datetime(2024, 1, 1, 17, 30, tzinfo=timezone.utc), "Asia/Bangkok"
    ) == "2024-01-02"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), "America/Los_Angeles"
    ) == "2023-12-31"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), None
    ) == "2024-01-01"
    with pytest.raises(ValueError):
        business_date_at(datetime(2024, 1, 1, 2, 30), "UTC")
    with pytest.raises(ValueError):
        business_date_at(
            datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc),
            "Synthetic/Invalid",
        )


@pytest.mark.asyncio
async def test_live_fulfillment_uses_company_business_timezone(monkeypatch):
    import celerp.services.fulfill as fulfill

    fixed = datetime(2024, 1, 1, 17, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Asia/Bangkok"})
            return None

    emitted = []
    je_calls = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    async def fake_je(session, **kwargs):
        je_calls.append(kwargs)

    monkeypatch.setattr(fulfill, "datetime", FixedDateTime)
    monkeypatch.setattr(fulfill, "emit_event", fake_emit)
    monkeypatch.setattr(fulfill.auto_je, "create_for_doc_fulfilled", fake_je)

    await fulfill.execute_fulfill(
        FakeSession(),
        doc_entity_id="doc:SYNTH-LIVE",
        doc_state={"line_items": []},
        pick_result=PickResult(
            picks=[
                PickLine(
                    item_id="item:SYNTH-LIVE",
                    sku="SYNTH",
                    pick_qty=1.0,
                    cost_price=13.0,
                    action="full",
                )
            ]
        ),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        doc_type="invoice",
    )

    fulfilled = next(e for e in emitted if e["event_type"] == "doc.fulfilled")
    assert fulfilled["data"]["fulfilled_at"] == fixed.isoformat()
    assert je_calls[0]["ts"] == "2024-01-02"


@pytest.mark.asyncio
async def test_non_cogs_fulfillment_does_not_require_valid_timezone(monkeypatch):
    import celerp.services.fulfill as fulfill

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Synthetic/Invalid"})
            return None

    emitted = []
    je_calls = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    async def fake_je(session, **kwargs):
        je_calls.append(kwargs)

    monkeypatch.setattr(fulfill, "emit_event", fake_emit)
    monkeypatch.setattr(fulfill.auto_je, "create_for_doc_fulfilled", fake_je)

    await fulfill.execute_fulfill(
        FakeSession(),
        doc_entity_id="doc:SYNTH-MEMO",
        doc_state={"line_items": []},
        pick_result=PickResult(
            picks=[
                PickLine(
                    item_id="item:SYNTH-MEMO",
                    sku="SYNTH",
                    pick_qty=1.0,
                    cost_price=13.0,
                    action="full",
                )
            ]
        ),
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        doc_type="memo",
    )

    assert any(e["event_type"] == "doc.fulfilled" for e in emitted)
    assert je_calls == []


@pytest.mark.asyncio
async def test_cogs_fulfillment_invalid_timezone_fails_before_events(monkeypatch):
    import celerp.services.fulfill as fulfill

    class FakeSession:
        async def get(self, model, key):
            if model is Company:
                return SimpleNamespace(settings={"timezone": "Synthetic/Invalid"})
            return None

    emitted = []

    async def fake_emit(session, **kwargs):
        emitted.append(kwargs)
        return SimpleNamespace(id=len(emitted))

    monkeypatch.setattr(fulfill, "emit_event", fake_emit)

    with pytest.raises(ValueError, match="invalid business timezone"):
        await fulfill.execute_fulfill(
            FakeSession(),
            doc_entity_id="doc:SYNTH-BAD-TZ",
            doc_state={"line_items": []},
            pick_result=PickResult(
                picks=[
                    PickLine(
                        item_id="item:SYNTH-BAD-TZ",
                        sku="SYNTH",
                        pick_qty=1.0,
                        cost_price=13.0,
                        action="full",
                    )
                ]
            ),
            company_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            doc_type="invoice",
        )

    assert emitted == []

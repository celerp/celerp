# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/modules/demotion.py and loader.demoted_first_party.

A bundled default whose content no longer matches the committed first-party lock
is demoted to untrusted. That state is surfaced in the notification bell at boot
(deduped, company-wide) instead of an always-on /modules banner.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.modules.demotion import notify_demoted_modules
from celerp.modules import loader as _loader

# `session` (Postgres, rollback-isolated) comes from the root conftest.


@pytest_asyncio.fixture
async def company(session) -> Company:
    c = Company(name="DemoteCo", slug="demoteco", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


@pytest_asyncio.fixture
async def company_b(session) -> Company:
    c = Company(name="DemoteCoB", slug="demotecob", settings={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c


async def _modules_notifs(session, company_id) -> list[Notification]:
    rows = (await session.execute(
        select(Notification).where(
            Notification.company_id == company_id,
            Notification.category == "modules",
        )
    )).scalars().all()
    return list(rows)


# ── demoted_first_party (loader) ──────────────────────────────────────────────

class TestDemotedFirstParty:
    def test_intact_default_is_not_demoted(self):
        # A shipped default loaded from its own tree matches the lock exactly.
        assert "celerp-labels" not in _loader.demoted_first_party({"celerp-labels"})

    def test_third_party_name_is_never_demoted(self):
        # A name not in the lock is not first-party at all, so never "demoted".
        assert _loader.demoted_first_party({"equipment-maintenance"}) == []

    def test_tampered_default_is_demoted(self, tmp_path, monkeypatch):
        # Point the lock at a name with a digest that cannot match any real tree,
        # and resolve that name to a present package: the content check fails, so
        # it reports demoted.
        monkeypatch.setattr(_loader, "_first_party_lock",
                            lambda: {"celerp-widgets": "deadbeef"})
        pkg = tmp_path / "celerp-widgets"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("PLUGIN_MANIFEST = {'name': 'celerp-widgets'}\n")
        monkeypatch.setenv("MODULE_DIR", str(tmp_path))
        _loader._demotion_warned.discard("celerp-widgets")
        assert _loader.demoted_first_party({"celerp-widgets"}) == ["celerp-widgets"]


# ── notify_demoted_modules (bell) ─────────────────────────────────────────────

class TestNotifyDemotedModules:
    @pytest.mark.asyncio
    async def test_creates_company_wide_notice_per_company(self, session, company, company_b):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            created = await notify_demoted_modules(session, ["celerp-widgets"])
            await session.commit()
        assert created == 2  # one per company
        for cid in (company.id, company_b.id):
            rows = await _modules_notifs(session, cid)
            assert len(rows) == 1
            n = rows[0]
            assert n.user_id is None            # company-wide
            assert n.priority == "high"
            assert n.action_url == "/modules"
            assert "celerp-widgets" in n.title

    @pytest.mark.asyncio
    async def test_dedups_against_unread_notice(self, session, company):
        # A reboot while an unread notice already stands must not pile up a second.
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            first = await notify_demoted_modules(session, ["celerp-widgets"])
            await session.commit()
            second = await notify_demoted_modules(session, ["celerp-widgets"])
            await session.commit()
        assert first == 1
        assert second == 0
        assert len(await _modules_notifs(session, company.id)) == 1

    @pytest.mark.asyncio
    async def test_renotifies_after_dismissal(self, session, company):
        # Persistent-until-fixed: once the prior notice is read (dismissed), a still
        # demoted module notifies again on the next boot.
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            await notify_demoted_modules(session, ["celerp-widgets"])
            await session.commit()
            rows = await _modules_notifs(session, company.id)
            rows[0].read = True
            await session.commit()
            again = await notify_demoted_modules(session, ["celerp-widgets"])
            await session.commit()
        assert again == 1
        assert len(await _modules_notifs(session, company.id)) == 2

    @pytest.mark.asyncio
    async def test_empty_list_creates_nothing(self, session, company):
        with patch("celerp.notifications.service.publish", new_callable=AsyncMock):
            created = await notify_demoted_modules(session, [])
            await session.commit()
        assert created == 0
        assert await _modules_notifs(session, company.id) == []

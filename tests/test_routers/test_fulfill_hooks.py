# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for fire_lifecycle_strict and on_pre_fulfill hook."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# fire_lifecycle_strict
# ---------------------------------------------------------------------------

class TestFireLifecycleStrict:
    def setup_method(self):
        from celerp.modules.slots import clear
        clear()

    def teardown_method(self):
        from celerp.modules.slots import clear
        clear()

    @pytest.mark.asyncio
    async def test_propagates_http_exception(self):
        from fastapi import HTTPException
        from celerp.modules.slots import register, fire_lifecycle_strict

        async def _blocker(**kwargs):
            raise HTTPException(status_code=409, detail="blocked")

        with patch("importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.blocker = _blocker
            mock_import.return_value = mock_mod

            register("test_slot", {"handler": "fake_mod:blocker"})
            with pytest.raises(HTTPException) as exc_info:
                await fire_lifecycle_strict("test_slot")
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_swallows_non_http_exceptions(self):
        from celerp.modules.slots import register, fire_lifecycle_strict

        async def _noisy(**kwargs):
            raise ValueError("boom")

        with patch("importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.noisy = _noisy
            mock_import.return_value = mock_mod

            register("test_slot2", {"handler": "fake_mod:noisy"})
            # Should not raise
            await fire_lifecycle_strict("test_slot2")

    @pytest.mark.asyncio
    async def test_no_handlers_no_error(self):
        from celerp.modules.slots import fire_lifecycle_strict
        await fire_lifecycle_strict("nonexistent_slot")


# ---------------------------------------------------------------------------
# on_pre_fulfill (requires celerp_warehousing)
# ---------------------------------------------------------------------------

class TestOnPreFulfill:
    @pytest.fixture(autouse=True)
    def _require_warehousing(self):
        pytest.importorskip("celerp_warehousing")
    @pytest.mark.asyncio
    async def test_passes_when_no_pick(self):
        """No pick instruction = proceed without error."""
        from celerp_warehousing.hooks import on_pre_fulfill

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=MagicMock(settings={}))
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(__iter__=lambda s: iter([]))))

        await on_pre_fulfill(doc_id="doc:123", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)

    @pytest.mark.asyncio
    async def test_auto_complete_pick_when_setting_true(self):
        """auto_complete_pick=True: open pick gets completed."""
        from celerp_warehousing.hooks import on_pre_fulfill

        mock_session = AsyncMock()
        mock_company = MagicMock()
        mock_company.settings = {"warehousing": {"auto_complete_pick": True}}
        mock_session.get = AsyncMock(return_value=mock_company)

        mock_pick = MagicMock()
        mock_pick.state = {
            "list_type": "pick_instruction",
            "source_doc_id": "doc:123",
            "status": "draft",
        }

        scalars_result = MagicMock()
        scalars_result.__iter__ = lambda s: iter([mock_pick])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars_result))

        await on_pre_fulfill(doc_id="doc:123", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)

        assert mock_pick.state["status"] == "completed"

    @pytest.mark.asyncio
    async def test_blocks_when_auto_complete_false(self):
        """auto_complete_pick=False: 409 raised when pick is open."""
        from fastapi import HTTPException
        from celerp_warehousing.hooks import on_pre_fulfill

        mock_session = AsyncMock()
        mock_company = MagicMock()
        mock_company.settings = {"warehousing": {"auto_complete_pick": False}}
        mock_session.get = AsyncMock(return_value=mock_company)

        mock_pick = MagicMock()
        mock_pick.state = {
            "list_type": "pick_instruction",
            "source_doc_id": "doc:456",
            "status": "draft",
        }

        scalars_result = MagicMock()
        scalars_result.__iter__ = lambda s: iter([mock_pick])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars_result))

        with pytest.raises(HTTPException) as exc_info:
            await on_pre_fulfill(doc_id="doc:456", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)
        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# on_post_unfulfill
# ---------------------------------------------------------------------------

class TestOnPostUnfulfill:
    @pytest.fixture(autouse=True)
    def _require_warehousing(self):
        pytest.importorskip("celerp_warehousing")

    @pytest.mark.asyncio
    async def test_no_pick_no_error(self):
        """No pick instruction linked to doc - function returns without error."""
        from celerp_warehousing.hooks import on_post_unfulfill

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=MagicMock(settings={}))
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(__iter__=lambda s: iter([]))))

        await on_post_unfulfill(doc_id="doc:123", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)

    @pytest.mark.asyncio
    async def test_completed_pick_reopened(self):
        """Completed pick instruction gets reopened and history entry added."""
        from celerp_warehousing.hooks import on_post_unfulfill

        mock_session = AsyncMock()

        mock_pick = MagicMock()
        mock_pick.state = {
            "list_type": "pick_instruction",
            "source_doc_id": "doc:789",
            "status": "completed",
        }

        # First execute (active pick search) returns nothing; second (completed) returns mock_pick
        call_count = {"n": 0}
        def _execute_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                scalars_result = MagicMock()
                scalars_result.__iter__ = lambda s: iter([])
                return MagicMock(scalars=lambda: scalars_result)
            scalars_result = MagicMock()
            scalars_result.__iter__ = lambda s: iter([mock_pick])
            return MagicMock(scalars=lambda: scalars_result)

        mock_session.execute = AsyncMock(side_effect=_execute_side_effect)

        await on_post_unfulfill(doc_id="doc:789", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)

        assert mock_pick.state["status"] == "open"
        assert mock_pick.state["reopened_reason"] == "fulfillment_reverted"
        history = mock_pick.state["history"]
        assert len(history) == 1
        assert history[0]["event"] == "pick_reopened"
        assert history[0]["doc_id"] == "doc:789"

    @pytest.mark.asyncio
    async def test_open_pick_also_reopened(self):
        """Active (open/draft) pick instruction also gets reopened with history."""
        from celerp_warehousing.hooks import on_post_unfulfill

        mock_session = AsyncMock()

        mock_pick = MagicMock()
        mock_pick.state = {
            "list_type": "pick_instruction",
            "source_doc_id": "doc:111",
            "status": "draft",
            "history": [{"event": "pick_created"}],
        }

        scalars_result = MagicMock()
        scalars_result.__iter__ = lambda s: iter([mock_pick])
        mock_session.execute = AsyncMock(return_value=MagicMock(scalars=lambda: scalars_result))

        await on_post_unfulfill(doc_id="doc:111", company_id="00000000-0000-0000-0000-000000000001", session=mock_session)

        assert mock_pick.state["status"] == "open"
        assert len(mock_pick.state["history"]) == 2
        assert mock_pick.state["history"][-1]["event"] == "pick_reopened"

# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""
Browser test for the self-service plan chooser grid layout.

The plan chooser holds exactly two cards (Connect and Connect + AI). On a
desktop-width viewport the .cloud-plans grid must lay them out in a single row
with no empty trailing column, so the computed grid resolves to exactly two
tracks.

Run: pytest tests/test_browser/test_cloud_plans_grid.py -m browser --tb=short
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


class TestCloudPlansGrid:
    """The value-prop plan grid renders two even columns on desktop widths."""

    def _open_plans(self, page: Page, ui_server: str) -> None:
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{ui_server}/settings/cloud", wait_until="domcontentloaded")
        expect(page.locator(".cloud-plans")).to_be_visible()

    def test_two_plan_cards_render(self, page, ui_server):
        """Exactly two plan cards are present (Team card removed)."""
        self._open_plans(page, ui_server)
        assert page.locator(".cloud-plans .cloud-plan-card").count() == 2

    def test_grid_has_two_tracks(self, page, ui_server):
        """The desktop grid resolves to exactly two column tracks, no empty third."""
        self._open_plans(page, ui_server)
        tracks = page.evaluate(
            "() => getComputedStyle(document.querySelector('.cloud-plans'))"
            ".gridTemplateColumns.trim().split(/\\s+/).length"
        )
        assert tracks == 2, f"expected 2 grid tracks, got {tracks}"

    def test_cards_share_one_row(self, page, ui_server):
        """Both cards sit on the same row and span the container (no empty slot)."""
        self._open_plans(page, ui_server)
        tops = page.eval_on_selector_all(
            ".cloud-plans .cloud-plan-card", "els => els.map(e => e.offsetTop)"
        )
        assert len(tops) == 2 and tops[0] == tops[1], f"cards not on one row: {tops}"

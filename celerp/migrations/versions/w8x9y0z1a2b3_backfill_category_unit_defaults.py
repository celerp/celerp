# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Backfill category-specific purchase_unit and weight_unit defaults.

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-05-15

Context
-------
Category JSON files now carry default_purchase_unit and default_weight_unit.
This migration:
  1. Updates sell_by from 'carat' → 'gram' for gem categories where that
     changed (diamond, ruby, sapphire, emerald, colored_stone, rough_gemstone,
     pearl, mineral_specimen).
  2. Backfills purchase_unit with the category-specific default (overrides
     the sell_by fallback set by the previous migration).
  3. Backfills weight_unit where still NULL.
"""

from __future__ import annotations
from alembic import op

# In-Python edits avoid json->jsonb casts that fail on SQL_ASCII clusters.
# See https://github.com/celerp/celerp/issues/189
from celerp.migrations._json_compat import update_projection_state


revision = "w8x9y0z1a2b3"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None


# Hardcoded category mapping (migrations must be self-contained).
# Format: category_name -> {"sell_by": ..., "purchase_unit": ..., "weight_unit": ... or None}
CATEGORY_DEFAULTS: dict[str, dict] = {
    # GEMS & JEWELRY
    "diamond": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "ruby": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "sapphire": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "emerald": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "colored_stone": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "rough_gemstone": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "pearl": {"sell_by": "gram", "purchase_unit": "gram", "weight_unit": "gram"},
    "mineral_specimen": {"sell_by": "piece", "purchase_unit": "gram", "weight_unit": "gram"},
    "jewelry": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # COINS & PRECIOUS METALS
    "gold_bullion": {"sell_by": "piece", "purchase_unit": "gram", "weight_unit": "gram"},
    "silver_bullion": {"sell_by": "piece", "purchase_unit": "gram", "weight_unit": "gram"},
    "platinum_bullion": {"sell_by": "piece", "purchase_unit": "gram", "weight_unit": "gram"},
    "bullion_coin": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "numismatic_coin": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "banknote": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "medal_token": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # AGRICULTURAL
    "fresh_produce": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "grain_cereal": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "seeds": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "livestock_feed": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "fertilizer_chemical": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    # FOOD & BEVERAGE
    "fresh_food": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "ingredient_bulk": {"sell_by": "kg", "purchase_unit": "kg", "weight_unit": "kg"},
    "packaged_food": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "frozen_food": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "confectionery": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "beverage_nonalc": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "beer": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "wine": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "spirit": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # FASHION
    "tops": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "bottoms": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "dress_jumpsuit": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "outerwear": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "footwear": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "bag_handbag": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "accessory_fashion": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "activewear": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "swimwear": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # ELECTRONICS
    "mobile_phone": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "tablet": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "camera": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "audio_equipment": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "component_part": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "laptop": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "gaming_console": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "tv_display": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    # AUTOMOTIVE
    "fastener": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "interior_part": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "engine_part": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "brake_suspension": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "body_part_auto": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "tire_wheel": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "fluid_lubricant": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "electrical_auto": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # HARDWARE
    "measuring_instrument": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "hand_tool": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "power_tool": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "safety_ppe": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "pipe_plumbing": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "electrical_component": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # FURNITURE
    "seating": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "table_desk": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "storage_furniture": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "bed_bedroom": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "lighting": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "soft_furnishing": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "kitchen_dining": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "outdoor_furniture": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    # WATCHES
    "watch": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "watch_strap": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "fine_writing_instrument": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # COSMETICS
    "skincare": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "makeup": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "haircare": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "fragrance": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "nail": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "personal_care": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "beauty_tool": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # ARTWORK
    "painting": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "sculpture": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "print_edition": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "art_photography": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "drawing": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    "decorative_art": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "kg"},
    # BOOKS & MEDIA
    "book": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "music_vinyl": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "music_cd": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "film_video": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "video_game": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "comic": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    "trading_card": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": "gram"},
    # PROPERTY / CONSULTING / SAAS (no weight_unit)
    "residential_unit": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "commercial_space": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "parking_bay": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "consulting_service": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "saas_plan": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "software_addon": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
    "software_license": {"sell_by": "piece", "purchase_unit": "piece", "weight_unit": None},
}

# Categories where sell_by was previously 'carat' and must change to 'gram'
CARAT_TO_GRAM = {
    "diamond", "ruby", "sapphire", "emerald", "colored_stone",
    "rough_gemstone", "pearl", "mineral_specimen",
}


def upgrade() -> None:
    conn = op.get_bind()

    def _backfill(state, row):
        cat = state.get("category")
        defaults = CATEGORY_DEFAULTS.get(cat)
        if defaults is None:
            return False
        changed = False

        # Fix sell_by carat → gram (must run before the purchase_unit check below,
        # which compares against the post-fix sell_by - matching the original order).
        if cat in CARAT_TO_GRAM and state.get("sell_by") == "carat":
            state["sell_by"] = "gram"
            changed = True

        # Backfill purchase_unit (overwrite if absent or equal to sell_by - the
        # old generic fallback).
        purchase_unit = defaults["purchase_unit"]
        cur_pu = state.get("purchase_unit")
        if cur_pu is None or cur_pu == state.get("sell_by"):
            if cur_pu != purchase_unit:
                state["purchase_unit"] = purchase_unit
                changed = True

        # Backfill weight_unit where missing
        weight_unit = defaults.get("weight_unit")
        if weight_unit and state.get("weight_unit") is None:
            state["weight_unit"] = weight_unit
            changed = True

        return changed

    update_projection_state(conn, _backfill, where="entity_type = 'item'")


def downgrade() -> None:
    pass  # Data migration - no safe downgrade

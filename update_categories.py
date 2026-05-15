#!/usr/bin/env python3
"""Update category JSON files with default_purchase_unit and default_weight_unit."""
import json
import os

CATEGORIES_DIR = "default_modules/celerp-verticals/celerp_verticals/categories"

# category_name -> (sell_by, purchase_unit, weight_unit or None)
MAPPING = {
    # GEMS & JEWELRY
    "diamond": ("gram", "gram", "gram"),
    "ruby": ("gram", "gram", "gram"),
    "sapphire": ("gram", "gram", "gram"),
    "emerald": ("gram", "gram", "gram"),
    "colored_stone": ("gram", "gram", "gram"),
    "rough_gemstone": ("gram", "gram", "gram"),
    "pearl": ("gram", "gram", "gram"),
    "mineral_specimen": ("piece", "gram", "gram"),
    "jewelry": ("piece", "piece", "gram"),
    # COINS & PRECIOUS METALS
    "gold_bullion": ("piece", "gram", "gram"),
    "silver_bullion": ("piece", "gram", "gram"),
    "platinum_bullion": ("piece", "gram", "gram"),
    "bullion_coin": ("piece", "piece", "gram"),
    "numismatic_coin": ("piece", "piece", "gram"),
    "banknote": ("piece", "piece", "gram"),
    "medal_token": ("piece", "piece", "gram"),
    # AGRICULTURAL
    "fresh_produce": ("kg", "kg", "kg"),
    "grain_cereal": ("kg", "kg", "kg"),
    "seeds": ("kg", "kg", "kg"),
    "livestock_feed": ("kg", "kg", "kg"),
    "fertilizer_chemical": ("kg", "kg", "kg"),
    # FOOD & BEVERAGE
    "fresh_food": ("kg", "kg", "kg"),
    "ingredient_bulk": ("kg", "kg", "kg"),
    "packaged_food": ("piece", "piece", "gram"),
    "frozen_food": ("piece", "piece", "gram"),
    "confectionery": ("piece", "piece", "gram"),
    "beverage_nonalc": ("piece", "piece", "gram"),
    "beer": ("piece", "piece", "gram"),
    "wine": ("piece", "piece", "gram"),
    "spirit": ("piece", "piece", "gram"),
    # FASHION
    "tops": ("piece", "piece", "gram"),
    "bottoms": ("piece", "piece", "gram"),
    "dress_jumpsuit": ("piece", "piece", "gram"),
    "outerwear": ("piece", "piece", "gram"),
    "footwear": ("piece", "piece", "gram"),
    "bag_handbag": ("piece", "piece", "gram"),
    "accessory_fashion": ("piece", "piece", "gram"),
    "activewear": ("piece", "piece", "gram"),
    "swimwear": ("piece", "piece", "gram"),
    # ELECTRONICS
    "mobile_phone": ("piece", "piece", "gram"),
    "tablet": ("piece", "piece", "gram"),
    "camera": ("piece", "piece", "gram"),
    "audio_equipment": ("piece", "piece", "gram"),
    "component_part": ("piece", "piece", "gram"),
    "laptop": ("piece", "piece", "kg"),
    "gaming_console": ("piece", "piece", "kg"),
    "tv_display": ("piece", "piece", "kg"),
    # AUTOMOTIVE
    "fastener": ("piece", "piece", "gram"),
    "interior_part": ("piece", "piece", "gram"),
    "engine_part": ("piece", "piece", "kg"),
    "brake_suspension": ("piece", "piece", "kg"),
    "body_part_auto": ("piece", "piece", "kg"),
    "tire_wheel": ("piece", "piece", "kg"),
    "fluid_lubricant": ("piece", "piece", "kg"),
    "electrical_auto": ("piece", "piece", "gram"),
    # HARDWARE
    "measuring_instrument": ("piece", "piece", "gram"),
    "hand_tool": ("piece", "piece", "gram"),
    "power_tool": ("piece", "piece", "gram"),
    "safety_ppe": ("piece", "piece", "gram"),
    "pipe_plumbing": ("piece", "piece", "gram"),
    "electrical_component": ("piece", "piece", "gram"),
    # FURNITURE
    "seating": ("piece", "piece", "kg"),
    "table_desk": ("piece", "piece", "kg"),
    "storage_furniture": ("piece", "piece", "kg"),
    "bed_bedroom": ("piece", "piece", "kg"),
    "lighting": ("piece", "piece", "kg"),
    "soft_furnishing": ("piece", "piece", "kg"),
    "kitchen_dining": ("piece", "piece", "kg"),
    "outdoor_furniture": ("piece", "piece", "kg"),
    # WATCHES
    "watch": ("piece", "piece", "gram"),
    "watch_strap": ("piece", "piece", "gram"),
    "fine_writing_instrument": ("piece", "piece", "gram"),
    # COSMETICS
    "skincare": ("piece", "piece", "gram"),
    "makeup": ("piece", "piece", "gram"),
    "haircare": ("piece", "piece", "gram"),
    "fragrance": ("piece", "piece", "gram"),
    "nail": ("piece", "piece", "gram"),
    "personal_care": ("piece", "piece", "gram"),
    "beauty_tool": ("piece", "piece", "gram"),
    # ARTWORK
    "painting": ("piece", "piece", "kg"),
    "sculpture": ("piece", "piece", "kg"),
    "print_edition": ("piece", "piece", "kg"),
    "art_photography": ("piece", "piece", "kg"),
    "drawing": ("piece", "piece", "kg"),
    "decorative_art": ("piece", "piece", "kg"),
    # BOOKS & MEDIA
    "book": ("piece", "piece", "gram"),
    "music_vinyl": ("piece", "piece", "gram"),
    "music_cd": ("piece", "piece", "gram"),
    "film_video": ("piece", "piece", "gram"),
    "video_game": ("piece", "piece", "gram"),
    "comic": ("piece", "piece", "gram"),
    "trading_card": ("piece", "piece", "gram"),
    # PROPERTY / CONSULTING / SAAS (no weight_unit)
    "residential_unit": ("piece", "piece", None),
    "commercial_space": ("piece", "piece", None),
    "parking_bay": ("piece", "piece", None),
    "consulting_service": ("piece", "piece", None),
    "saas_plan": ("piece", "piece", None),
    "software_addon": ("piece", "piece", None),
    "software_license": ("piece", "piece", None),
}

# Note: fastener appears in both automotive and hardware - same values so OK

updated = 0
skipped = []

for fname in os.listdir(CATEGORIES_DIR):
    if not fname.endswith(".json"):
        continue
    name = fname[:-5]
    fpath = os.path.join(CATEGORIES_DIR, fname)
    with open(fpath) as f:
        data = json.load(f)
    
    if name not in MAPPING:
        skipped.append(name)
        continue
    
    sell_by, purchase_unit, weight_unit = MAPPING[name]
    data["default_sell_by"] = sell_by
    data["default_purchase_unit"] = purchase_unit
    if weight_unit is not None:
        data["default_weight_unit"] = weight_unit
    elif "default_weight_unit" in data:
        del data["default_weight_unit"]
    
    with open(fpath, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    updated += 1

print(f"Updated: {updated}")
print(f"Skipped (not in mapping): {skipped}")

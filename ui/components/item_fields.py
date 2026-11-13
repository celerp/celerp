"""Display adapters for historical category fields, without rewriting stored items."""
from ui.i18n import t_or

LEGACY_ITEM_ALIASES = {'weight_g': 'weight', 'origin_country': 'country_of_origin'}

def item_display_values(item: dict) -> dict:
    values = {**(item.get('attributes') or {}), **item}
    legacy = {}
    for old, core in LEGACY_ITEM_ALIASES.items():
        if core not in values and values.get(old) not in (None, ''):
            values[core] = values[old]
            legacy[core] = old
            if core == 'weight':
                values['_legacy_weight_unit'] = values.get('weight_unit')
                values['weight_unit'] = 'gram'
    if legacy:
        values['_legacy_core_fields'] = legacy
    return values


def item_detail_schema(schema: list[dict], category_fields: list[dict], category: str) -> list[dict]:
    """Core fields own their names; old preset aliases never add a second editor."""
    result = [dict(f) for f in schema if f.get('key') not in LEGACY_ITEM_ALIASES]
    existing = {f.get('key') for f in result}
    result += [dict(f) for f in category_fields if f.get('key') not in existing and f.get('key') not in LEGACY_ITEM_ALIASES]
    for field in result:
        key = field.get('key')
        if key == 'type' and category == 'packaged_food':
            field['label_key'] = 'inventory.packaging_type'
        if key == 'description':
            field['help_text'] = t_or('inventory.description_help', 'Full product description, including detailed specifications.')
        elif key == 'short_description':
            field['help_text'] = t_or('inventory.short_description_help', 'A brief product summary for compact lists and labels.')
    return result

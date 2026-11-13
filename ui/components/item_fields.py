"""The item detail field list: the base schema plus the category's own fields."""
from celerp_inventory.projections import LEGACY_CATEGORY_FIELDS
from ui.i18n import t_or

def item_detail_schema(schema: list[dict], category_fields: list[dict], category: str) -> list[dict]:
    """Core fields own their names; old preset aliases never add a second editor."""
    result = [dict(f) for f in schema if f.get('key') not in LEGACY_CATEGORY_FIELDS]
    existing = {f.get('key') for f in result}
    result += [dict(f) for f in category_fields if f.get('key') not in existing and f.get('key') not in LEGACY_CATEGORY_FIELDS]
    for field in result:
        key = field.get('key')
        if key == 'type' and category == 'packaged_food':
            field['label_key'] = 'inventory.packaging_type'
        if key == 'description':
            field['help_text'] = t_or('inventory.description_help', 'Full product description, including detailed specifications.')
        elif key == 'short_description':
            field['help_text'] = t_or('inventory.short_description_help', 'A brief product summary for compact lists and labels.')
    return result

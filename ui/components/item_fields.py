"""The item detail field list: the base schema plus the category's own fields."""
from celerp_inventory.projections import LEGACY_CATEGORY_FIELDS
from ui.i18n import t_or

LEGACY_CORE_KEYS = frozenset(LEGACY_CATEGORY_FIELDS.values())


def core_field_key(key: str) -> str:
    """The field an old preset key's values now live in (the key itself for every other field)."""
    return LEGACY_CATEGORY_FIELDS.get(key, key)


def item_detail_schema(schema: list[dict], category_fields: list[dict], category: str) -> list[dict]:
    """Core fields own their names; an old preset field stands for its core field, never a second editor."""
    result = []
    for f in [*schema, *category_fields]:
        key = core_field_key(f.get('key'))
        if key not in {r.get('key') for r in result}:
            result.append({**f, 'key': key})
    for field in result:
        key = field.get('key')
        if key == 'type' and category == 'packaged_food':
            field['label_key'] = 'inventory.packaging_type'
        if key == 'description':
            field['help_text'] = t_or('inventory.description_help', 'Full product description, including detailed specifications.')
        elif key == 'short_description':
            field['help_text'] = t_or('inventory.short_description_help', 'A brief product summary for compact lists and labels.')
    return result

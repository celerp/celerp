"""Help page schema: one JSON page validates into a HelpPage or a list of readable errors."""
from __future__ import annotations

from celerp.help.schema import validate


def _page(**over) -> dict:
    page = {
        "id": "inventory.add-item",
        "title": "Add an item",
        "summary": "Create a stock item.",
        "keywords": ["item", "stock"],
        "routes": ["/inventory"],
        "permission": "view_inventory",
        "order": 1,
        "steps": [{
            "text": "Click {new} at the top of the list.",
            "labels": {"new": "nav.inventory"},
            "image": None,
            "capture": {"route": "/inventory", "selector": "main", "actions": []},
        }],
        "related": [],
    }
    page.update(over)
    return page


def _step(**over) -> dict:
    step = dict(_page()["steps"][0])
    step.update(over)
    return step


def test_valid_page_passes(tmp_path):
    page, errors = validate(_page(), tmp_path, "inventory")
    assert errors == []
    assert page.id == "inventory.add-item"


def test_label_placeholders_match_labels(tmp_path):
    missing = _page(steps=[_step(text="Click {new} then {save}.")])
    _, errors = validate(missing, tmp_path, "inventory")
    assert any("save" in e for e in errors)
    extra = _page(steps=[_step(labels={"new": "nav.inventory", "gone": "nav.inventory"})])
    _, errors = validate(extra, tmp_path, "inventory")
    assert any("gone" in e for e in errors)
    stray = _page(steps=[_step(text="Click {new} and type { or }.")])
    _, errors = validate(stray, tmp_path, "inventory")
    assert errors


def test_size_caps_enforced(tmp_path):
    for bad in (
        _page(title="x" * 121),
        _page(steps=[_step(text="x" * 501, labels={})]),
        _page(steps=[_step()] * 21),
    ):
        page, errors = validate(bad, tmp_path, "inventory")
        assert page is None and errors
    big = tmp_path / "big.json"
    big.write_text("x" * (64 * 1024 + 1))
    from celerp.help.schema import load_file
    page, errors = load_file(big, tmp_path, "inventory")
    assert page is None and errors


def test_unknown_page_keys_rejected(tmp_path):
    for bad in (_page(title_th="เพิ่มสินค้า"), _page(steps=[_step(text_th="x")])):
        page, errors = validate(bad, tmp_path, "inventory")
        assert page is None
        assert any("th" in e for e in errors)


def test_page_id_prefix_is_owning_module(tmp_path):
    _, errors = validate(_page(id="contacts.add-item"), tmp_path, "inventory")
    assert errors
    _, errors = validate(_page(id="Inventory.Add Item"), tmp_path, "inventory")
    assert errors


def test_image_must_be_webp_inside_dir(tmp_path):
    for image in ("img/a.png", "../img/a.webp", "/etc/a.webp", "a.webp", "img/../../a.webp"):
        page, errors = validate(_page(steps=[_step(image=image)]), tmp_path, "inventory")
        assert page is None and errors, image
    page, errors = validate(_page(steps=[_step(image="img/a.webp")]), tmp_path, "inventory")
    assert errors == []

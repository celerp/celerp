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


# -- Screenshots: read only a real WebP file inside the page's own img folder ---------------

def _webp(size: int = 116) -> bytes:
    """A WebP container of ``size`` bytes whose RIFF header covers exactly those bytes."""
    body = b"WEBPVP8X" + b"\0" * (size - 16)
    return b"RIFF" + (size - 8).to_bytes(4, "little") + body


def _image_page(help_dir, image="img/shot.webp"):
    from pathlib import Path
    page, errors = validate(_page(steps=[_step(image=image)]), Path(help_dir), "inventory")
    assert errors == []
    return page


def test_read_image_serves_a_webp_inside_the_help_folder(tmp_path):
    from celerp.help.schema import read_image
    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "shot.webp").write_bytes(_webp())
    page = _image_page(tmp_path)
    assert read_image(page, 1) == _webp()
    # Steps are numbered from 1; a step past the last, or one without an image, has none.
    assert read_image(page, 0) is None and read_image(page, 2) is None
    assert read_image(_image_page(tmp_path, image=None), 1) is None


def test_read_image_never_follows_a_link(tmp_path):
    from celerp.help.schema import read_image
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shot.webp").write_bytes(_webp())
    linked_file = tmp_path / "file-link"
    (linked_file / "img").mkdir(parents=True)
    (linked_file / "img" / "shot.webp").symlink_to(outside / "shot.webp")
    assert read_image(_image_page(linked_file), 1) is None
    linked_dir = tmp_path / "dir-link"
    linked_dir.mkdir()
    (linked_dir / "img").symlink_to(outside, target_is_directory=True)
    assert read_image(_image_page(linked_dir), 1) is None


def test_read_image_refuses_what_is_not_a_bounded_webp_file(tmp_path):
    import os
    from celerp.help.schema import MAX_IMAGE_BYTES, read_image
    img = tmp_path / "img"
    img.mkdir()
    page = _image_page(tmp_path)
    shot = img / "shot.webp"
    for data in (b"\x89PNG\r\n\x1a\n" + b"\0" * 100,         # another format under a .webp name
                 _webp()[:-4],                               # header longer than the file
                 _webp(MAX_IMAGE_BYTES + 16),                # over the size bound
                 b""):
        shot.write_bytes(data)
        assert read_image(page, 1) is None, data[:12]
    shot.write_bytes(_webp(MAX_IMAGE_BYTES))
    assert read_image(page, 1) == _webp(MAX_IMAGE_BYTES)
    shot.unlink()
    assert read_image(page, 1) is None
    shot.mkdir()
    assert read_image(page, 1) is None
    shot.rmdir()
    if hasattr(os, "mkfifo"):  # a pipe would block a reader that waited for data
        os.mkfifo(shot)
        assert read_image(page, 1) is None

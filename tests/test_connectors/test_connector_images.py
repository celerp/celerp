# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Connector image helpers: which files go to a store, and which a pull already holds."""

from __future__ import annotations

import pytest

from celerp.connectors.images import (
    _already_pulled,
    product_image_files,
    store_media_id,
)


def _img(fid: str, is_hero: bool = False, tag: str = "product_images") -> dict:
    return {"id": fid, "filename": f"{fid}.jpg", "mime": "image/jpeg",
            "url": f"/static/{fid}.jpg", "document_tag": tag, "is_hero": is_hero, "size": 100}


def _doc(fid: str, tag: str = "certificates") -> dict:
    return {"id": fid, "filename": f"{fid}.pdf", "mime": "application/pdf",
            "url": f"/static/{fid}.pdf", "document_tag": tag, "is_hero": False, "size": 200}


class TestProductImageFiles:
    def test_hero_first_then_other_product_images(self):
        files = [_img("extra1"), _img("hero", is_hero=True), _img("extra2")]
        assert [f["id"] for f in product_image_files(files)] == ["hero", "extra1", "extra2"]

    def test_empty_files(self):
        assert product_image_files([]) == []

    def test_documents_are_never_images(self):
        """A certificate or spec sheet never goes to a store as an image, even marked hero."""
        cert = _doc("cert1")
        cert["is_hero"] = True
        files = [_img("img1"), cert, _doc("spec1", tag="spec_sheets")]
        assert [f["id"] for f in product_image_files(files)] == ["img1"]


class TestStoreMediaId:
    STORE = "https://shop.example.com"

    def test_pulled_from_this_store(self):
        f = {**_img("a"), "remote": {"woocommerce": {"id": "11", "store": self.STORE}}}
        assert store_media_id(f, "woocommerce", {}, self.STORE) == "11"

    def test_pulled_from_another_store_is_unknown(self):
        f = {**_img("a"), "remote": {"woocommerce": {"id": "11", "store": "https://other.example.com"}}}
        assert store_media_id(f, "woocommerce", {}, self.STORE) is None

    def test_returned_for_an_earlier_push(self):
        assert store_media_id(_img("a"), "woocommerce", {"media_ids": {"a": "77"}}, self.STORE) == "77"


class TestAlreadyPulled:
    """A pulled file is recognised by its platform image id from the same store, or by
    its address when the platform gives no id; never by the local storage address."""

    STORE = "https://shop.example.com"

    def _file(self, **remote):
        return {"url": "/static/attachments/c/f1.jpg", "id": "f1",
                "remote": {"woocommerce": {"store": self.STORE, **remote}}}

    def test_same_image_id(self):
        files = [self._file(id="11", src="https://cdn.example.com/a.jpg")]
        assert _already_pulled(files, "woocommerce", {"id": "11", "src": "https://cdn.example.com/a-1.jpg",
                                                      "store": self.STORE})

    def test_other_image_id(self):
        files = [self._file(id="11", src="https://cdn.example.com/a.jpg")]
        assert not _already_pulled(files, "woocommerce", {"id": "12", "src": "https://cdn.example.com/a.jpg",
                                                          "store": self.STORE})

    def test_same_id_from_another_store(self):
        files = [self._file(id="11", src="https://cdn.example.com/a.jpg")]
        assert not _already_pulled(files, "woocommerce", {"id": "11", "src": "x", "store": "https://other.example.com"})

    def test_no_id_matches_the_address(self):
        files = [self._file(id=None, src="https://cdn.example.com/c.pdf")]
        assert _already_pulled(files, "woocommerce", {"id": None, "src": "https://cdn.example.com/c.pdf",
                                                      "store": self.STORE})

    def test_local_files_are_not_pulled_files(self):
        assert not _already_pulled([{"url": "https://cdn.example.com/a.jpg", "id": "f1"}], "woocommerce",
                                   {"id": None, "src": "https://cdn.example.com/a.jpg", "store": self.STORE})

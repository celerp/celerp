# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Phase 4 tests: connector image + certificate sync helpers."""

from __future__ import annotations

import pytest

from celerp.connectors.images import (
    build_platform_image_payload,
    build_platform_cert_payload,
    _already_pulled,
)


def _img(fid: str, is_hero: bool = False, tag: str = "product_images") -> dict:
    return {"id": fid, "filename": f"{fid}.jpg", "mime": "image/jpeg",
            "url": f"/static/{fid}.jpg", "document_tag": tag, "is_hero": is_hero, "size": 100}


def _doc(fid: str, tag: str = "certificates") -> dict:
    return {"id": fid, "filename": f"{fid}.pdf", "mime": "application/pdf",
            "url": f"/static/{fid}.pdf", "document_tag": tag, "is_hero": False, "size": 200}


class TestBuildPlatformImagePayload:
    def test_hero_and_additional(self):
        files = [_img("hero", is_hero=True), _img("extra1"), _img("extra2")]
        result = build_platform_image_payload(files)
        assert result["hero_url"] == "/static/hero.jpg"
        assert "/static/extra1.jpg" in result["additional_urls"]
        assert "/static/extra2.jpg" in result["additional_urls"]
        assert "/static/hero.jpg" not in result["additional_urls"]

    def test_no_hero_returns_none(self):
        files = [_img("extra1")]
        result = build_platform_image_payload(files)
        assert result["hero_url"] is None
        assert "/static/extra1.jpg" in result["additional_urls"]

    def test_empty_files(self):
        result = build_platform_image_payload([])
        assert result["hero_url"] is None
        assert result["additional_urls"] == []

    def test_non_image_hero_excluded(self):
        """PDF set as is_hero (should not happen via API but guard it) → excluded from hero."""
        files = [_doc("cert1")]
        files[0]["is_hero"] = True  # impossible normally but test the guard
        result = build_platform_image_payload(files)
        assert result["hero_url"] is None

    def test_docs_not_in_additional_urls(self):
        """Only product_images tagged image files appear in additional_urls."""
        files = [_img("img1"), _doc("cert1"), _doc("spec1", tag="spec_sheets")]
        result = build_platform_image_payload(files)
        assert len(result["additional_urls"]) == 1
        assert result["additional_urls"][0] == "/static/img1.jpg"


class TestBuildPlatformCertPayload:
    def test_certificates_and_spec_sheets(self):
        files = [
            _doc("cert1", tag="certificates"),
            _doc("cert2", tag="certificates"),
            _doc("spec1", tag="spec_sheets"),
            _img("img1"),  # should not appear
        ]
        result = build_platform_cert_payload(files)
        assert "certificates" in result
        assert len(result["certificates"]) == 2
        assert "spec_sheets" in result
        assert len(result["spec_sheets"]) == 1
        assert "product_images" not in result

    def test_empty_files(self):
        assert build_platform_cert_payload([]) == {}

    def test_safety_docs(self):
        files = [_doc("msds1", tag="safety_docs")]
        result = build_platform_cert_payload(files)
        assert "safety_docs" in result
        assert result["safety_docs"][0]["name"] == "msds1.pdf"

    def test_cert_entry_has_name_and_url(self):
        files = [_doc("mygrs", tag="certificates")]
        result = build_platform_cert_payload(files)
        entry = result["certificates"][0]
        assert "name" in entry
        assert "url" in entry


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

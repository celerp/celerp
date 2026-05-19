# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Phase 4 tests: connector image + certificate sync helpers."""

from __future__ import annotations

import pytest

from celerp.connectors.images import (
    build_platform_image_payload,
    build_platform_cert_payload,
    _file_already_stored,
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


class TestFileAlreadyStored:
    def test_url_present(self):
        files = [{"url": "/static/img.jpg", "id": "f1"}]
        assert _file_already_stored(files, "/static/img.jpg") is True

    def test_url_absent(self):
        files = [{"url": "/static/img.jpg", "id": "f1"}]
        assert _file_already_stored(files, "/static/other.jpg") is False

    def test_empty(self):
        assert _file_already_stored([], "/static/img.jpg") is False

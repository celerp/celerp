# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""upload_mime: the type an upload is stored as."""
from __future__ import annotations

import io

from starlette.datastructures import Headers, UploadFile

from celerp.services.attachments import upload_mime


def _upload(name: str, content_type: str | None) -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type else Headers({})
    return UploadFile(io.BytesIO(b"x"), filename=name, headers=headers)


def test_a_sent_type_is_kept():
    assert upload_mime(_upload("a.bin", "image/png")) == "image/png"


def test_generic_binary_takes_the_type_the_name_gives():
    assert upload_mime(_upload("statement.pdf", "application/octet-stream")) == "application/pdf"


def test_no_sent_type_takes_the_type_the_name_gives():
    assert upload_mime(_upload("photo.jpg", None)) == "image/jpeg"


def test_generic_binary_with_an_unknown_name_stays_generic():
    assert upload_mime(_upload("blob.zzz", "application/octet-stream")) == "application/octet-stream"

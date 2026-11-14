# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The Files panel of a list works end to end: a file uploaded on the list page can be tagged,
described and deleted there, and every change is stored on the list itself. Runs the real UI app
against the in-process API on a demo company."""

from __future__ import annotations

from test_list_carried_totals import _quotation
from test_list_old_rule_total_ui import _browser, _token

_HX = {"HX-Request": "true"}


async def _files(client, h, lid) -> list[dict]:
    r = await client.get(f"/lists/{lid}", headers=h)
    assert r.status_code == 200, r.text
    return r.json().get("files") or []


async def test_list_file_upload_tag_description_and_delete_are_stored(client, session):
    auth, lid = await _quotation(client, session, finalize=False)
    h = auth["headers"]
    async with _browser(_token(auth)) as ui:
        r = await ui.post(f"/lists/{lid}/files", headers=_HX,
                          files={"file": ("terms.txt", b"payment terms", "text/plain")})
        assert r.status_code == 200 and "terms.txt" in r.text, r.text
        [f] = await _files(client, h, lid)
        fid = f.get("file_id") or f.get("id")
        assert f.get("filename") == "terms.txt", f

        r = await ui.post(f"/lists/{lid}/files/{fid}/tag", headers=_HX, data={"document_tag": "contract"})
        assert r.status_code == 200, r.text
        assert (await _files(client, h, lid))[0].get("document_tag") == "contract"

        r = await ui.post(f"/lists/{lid}/files/{fid}/description", headers=_HX, data={"description": "signed copy"})
        assert r.status_code == 200, r.text
        assert (await _files(client, h, lid))[0].get("description") == "signed copy"

        r = await ui.delete(f"/lists/{lid}/files/{fid}", headers=_HX)
        assert r.status_code == 200, r.text
        assert await _files(client, h, lid) == []

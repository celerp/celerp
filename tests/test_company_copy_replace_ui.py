# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Replacing a company with a copy of it, in the pages: the preview shows both versions
side by side and what would be lost, the owner types the company's name to replace it,
the result page offers the saved version and Undo, and Saved copies lists, downloads,
puts back and deletes what each replace set aside."""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile

import pytest

from company_backup_support import company, member, owner, token
from migration_support import real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_backup_ui import _cookie, _follow, _hidden, _page, _upload_and_preview, ui  # noqa: F401
from test_company_copy_replace import NAME, _cid, _companies, _copy_then_more_work, _docs

pytestmark = pytest.mark.asyncio

BASE = "/settings/restore-backup"
SAVED = "/settings/saved-copies"


def _forms(page: str) -> dict[str, str]:
    """Each form's action and its markup."""
    return {action: body for action, body in re.findall(r'<form\b[^>]*action="([^"]*)"[^>]*>(.*?)</form>', page,
                                                         flags=re.S)}


async def _preview(ui, tok: str, data: bytes) -> str:
    ui.cookies.set("celerp_token", tok)
    return _page(await _upload_and_preview(ui, BASE, data))


async def test_preview_shows_both_versions_and_what_would_be_lost(ui, real_engine, real_client):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    page = await _preview(ui, tok, data)
    assert f"{NAME} is already here" in page
    table = re.search(r"Here now.*?This copy.*?</table>", page, flags=re.S).group(0)
    docs = re.search(r"<td>Documents</td>\s*<td[^>]*>(\d+)</td>\s*<td[^>]*>(\d+)</td>", table)
    here = await _docs(real_engine, _cid(tok))
    assert docs and (int(docs.group(1)), int(docs.group(2))) == (here, here - 2)
    assert "Replacing loses what was entered since" in page and "the table above shows the difference" in page
    form = _forms(page)[f"{BASE}/replace"]
    assert 'name="company_name"' in form and f"Type {NAME} to replace it" in form
    assert f"Replace {NAME}" in form and "btn--danger" in form
    assert "Or open it as a separate company" in page and f'action="{BASE}/restore"' in page
    # That a restore never overwrites is said only under the choice it is true of.
    separate = page.index("restores this as a separate company")
    assert page.index("Or open it as a separate company") < separate < page.index(f'action="{BASE}/restore"')


async def test_replace_needs_the_typed_name_then_offers_the_saved_version_and_undo(ui, real_engine, real_client):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    before = await _docs(real_engine, _cid(tok))
    page = await _preview(ui, tok, data)
    fields = _hidden(_forms(page)[f"{BASE}/replace"])

    wrong = await ui.post(f"{BASE}/replace", data={**fields, "company_name": "demo trading"})
    assert wrong.status_code == 200, wrong.text
    assert f"The name you typed does not match {NAME}." in _page(wrong)
    assert await _companies(real_engine) == [(_cid(tok), NAME)]

    r = await ui.post(f"{BASE}/replace", data={**fields, "company_name": NAME})
    assert r.status_code == 303, r.text
    done = _page(await _follow(ui, r))
    assert re.search(rf"Replaced {NAME} with the copy made on [^<]+\.", done), done[:3000]
    [(new, _)] = await _companies(real_engine)
    assert new != _cid(tok) and await _docs(real_engine, new) == before - 2
    download = re.search(rf'href="({SAVED}/[0-9a-f]{{32}}/download)"', done).group(1)
    got = await ui.get(download)
    assert got.status_code == 200
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        assert json.loads(zf.read("manifest.json"))["company"]["id"] == _cid(tok)

    undo = next(a for a in _forms(done) if a.endswith("/put-back"))
    assert f"Type {NAME} to undo" in _forms(done)[undo]
    back = await ui.post(undo, data={"company_name": NAME})
    assert back.status_code == 303, back.text
    page = _page(await _follow(ui, back))
    assert f"Put {NAME} back as it was on" in page
    [(restored, _)] = await _companies(real_engine)
    assert await _docs(real_engine, restored) == before

    # The version Undo replaced is saved in turn; the one it put back is gone from the list.
    listing = _page(await ui.get(SAVED))
    assert len(re.findall(r"/put-back", listing)) == 1 and undo not in listing


async def test_only_the_owner_is_offered_the_replace(ui, real_engine, real_client):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    admin = await owner(real_engine, "admin@example.test", "Admin")
    await member(real_engine, admin, uuid.UUID(_cid(tok)), role="admin")
    own = await company(real_engine, admin, "Admin Books", "admin-marker")
    ui.cookies.set("celerp_token", await token(real_engine, admin, own))
    page = _page(await _upload_and_preview(ui, "/setup/new-company/restore-backup", data))
    assert f"Only the owner of {NAME} can replace it." in page
    assert "/replace" not in "".join(_forms(page))
    assert "Or open it as a separate company" in page


async def test_saved_copies_listed_newest_first_and_deleted_after_asking(ui, real_engine, real_client):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    page = await _preview(ui, tok, data)
    r = await ui.post(f"{BASE}/replace", data={**_hidden(_forms(page)[f"{BASE}/replace"]), "company_name": NAME})
    await _follow(ui, r)

    tab = _page(await ui.get("/settings/general?tab=backup"))
    assert f'href="{SAVED}"' in tab and "Saved copies" in tab
    listing = _page(await ui.get(SAVED))
    [kept] = re.findall(rf"{SAVED}/([0-9a-f]{{32}})/delete", listing)
    assert NAME in listing and "cannot be undone: the file itself is removed" in listing
    button = re.search(rf'<button\b[^>]*hx-post="{SAVED}/{kept}/delete"[^>]*>', listing).group(0)
    assert 'hx-confirm="Delete this saved copy? This cannot be undone."' in button

    gone = await ui.post(f"{SAVED}/{kept}/delete", headers={"HX-Request": "true"})
    assert gone.status_code == 200
    swapped = _page(gone)
    assert 'id="saved-copies"' in swapped and "Saved copy deleted." in swapped and "No saved copies." in swapped
    again = await ui.post(f"{SAVED}/{kept}/delete", headers={"HX-Request": "true"})
    assert "This saved copy is no longer here." in _page(again)

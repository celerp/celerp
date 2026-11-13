# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Country tax rates come from the country packs shipped inside the release, each read only when
its bytes match the digest in the release's lock.

A pack that is absent or changed is missing: its country is not offered, a company already set
up for it keeps its stored taxes, typing it is refused with the reason, and one bell notice per
company names the missing packs and the next step. An unreadable lock is the same state for every
country. Every test here runs on a copy of the committed fixture pack set.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import socket
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.notification import Notification
from test_helpers import make_test_token

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "l10n"
TITLE_KEY = "l10n.pack_missing.title"


def _reset() -> None:
    from celerp.localization import lock_codes, packs
    lock_codes.cache_clear()
    packs.cache_clear()


@pytest.fixture()
def l10n_dir(tmp_path):
    """A writable copy of the fixture pack set, read by this process for the test's length."""
    root = tmp_path / "l10n"
    shutil.copytree(FIXTURE, root)
    saved = os.environ.get("CELERP_L10N_DIR")
    os.environ["CELERP_L10N_DIR"] = str(root)
    _reset()
    yield root
    if saved is None:
        os.environ.pop("CELERP_L10N_DIR", None)
    else:
        os.environ["CELERP_L10N_DIR"] = saved
    _reset()


def _drop(root: Path, *codes: str) -> None:
    for code in codes:
        (root / "packs" / f"{code}.json").unlink()
    _reset()


def _restore(root: Path) -> None:
    shutil.rmtree(root)
    shutil.copytree(FIXTURE, root)
    _reset()


def _break_lock(root: Path, how: str = "invalid") -> None:
    lock = root / "packs.lock.json"
    if how == "missing":
        lock.unlink()
    else:
        lock.write_text("{", encoding="utf-8")
    _reset()


def _pin_shaped(code: str) -> dict:
    """A company's settings as the pinned release seeded them for ``code``."""
    pack = json.loads((FIXTURE / "packs" / f"{code}.json").read_text(encoding="utf-8"))
    return {"currency": pack["company_defaults"]["currency"], "taxes": pack["taxes"], "taxes_seeded_for": code}


async def _company(session, settings: dict | None = None):
    import uuid
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="Pack co", slug=f"pack-{company_id.hex[:8]}", settings=settings or {}))
    await session.commit()
    return company_id


async def _set_settings(session, company_id, settings: dict) -> None:
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, company_id)
    company.settings = settings
    await session.commit()


async def _stored(session, company_id) -> dict:
    session.expire_all()
    return (await session.execute(select(Company.settings).where(Company.id == company_id))).scalar_one()


async def _default_location(client, auth) -> dict:
    r = await client.post("/companies/me/locations", headers=auth["headers"],
                          json={"name": "Head office", "type": "office", "is_default": True})
    assert r.status_code == 200, r.text
    return r.json()


async def _patch_address(client, auth, location_id, address: dict) -> dict:
    r = await client.patch(f"/companies/me/locations/{location_id}", headers=auth["headers"],
                           json={"address": address})
    assert r.status_code == 200, r.text
    return r.json()


async def _sync_notice(session) -> None:
    from celerp.localization.notice import sync_pack_notice
    await sync_pack_notice(session)
    await session.commit()


async def _pack_notices(session, company_id=None) -> list[Notification]:
    session.expire_all()
    q = select(Notification).where(Notification.i18n["title"].as_string() == TITLE_KEY)
    if company_id is not None:
        q = q.where(Notification.company_id == company_id)
    return list((await session.execute(q)).scalars().all())


def _ui_client():
    from ui.app import app as ui_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://testserver",
                             follow_redirects=False, cookies={"celerp_token": make_test_token(role="owner")})


def _stub(stack: ExitStack, name: str, **kw) -> AsyncMock:
    mock = AsyncMock(**kw)
    stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
    return mock


_LOC = {"id": "loc1", "name": "Main", "type": "warehouse", "is_default": True,
        "address": {"text": "1 Silom Road", "country": "IN"}}


async def _location_cell_refusal(value: str) -> tuple[str, AsyncMock]:
    """The location country cell's answer to ``value``, and the save it would have made."""
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [_LOC]})
        patch_location = _stub(stack, "patch_location", return_value={**_LOC, "taxes_from": None})
        async with _ui_client() as ui:
            r = await ui.patch("/settings/locations/loc1/country", data={"value": value})
    assert r.status_code == 200, r.text
    return r.text, patch_location


# ── The loader ───────────────────────────────────────────────────────────────

def test_pack_digest_mismatch_is_absent(l10n_dir, caplog):
    from celerp.localization import missing
    from celerp.tax_regimes import tax_countries

    path = l10n_dir / "packs" / "MY.json"
    path.write_bytes(path.read_bytes().replace(b"10.0", b"11.0", 1))
    _reset()
    with caplog.at_level(logging.ERROR):
        assert "MY" not in tax_countries()
    assert missing() == ("MY",)
    assert "MY.json" in caplog.text


def test_pack_missing_is_absent_and_logged(l10n_dir, caplog):
    from celerp.localization import missing
    from celerp.tax_regimes import tax_countries

    _drop(l10n_dir, "AU")
    with caplog.at_level(logging.ERROR):
        assert tax_countries() == ("TH", "MY", "IN")
    assert missing() == ("AU",)
    assert "AU.json" in caplog.text


def test_loader_makes_no_network(l10n_dir, monkeypatch):
    from celerp.localization import packs

    def _refuse(*a, **k):
        raise AssertionError("the pack loader opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    assert set(packs()) == {"TH", "MY", "AU", "IN"}
    assert packs()["TH"]["currency"] == "THB"


@pytest.mark.parametrize("how", ["missing", "invalid"])
async def test_lock_unreadable_gives_default_only(l10n_dir, how, client, auth, session, caplog):
    from celerp.localization import lock_codes, missing
    from ui.i18n import t
    from ui.routes.setup import tax_country_options

    company_id = auth["company_id"]
    await _set_settings(session, company_id, _pin_shaped("TH"))
    before = await _stored(session, company_id)
    with caplog.at_level(logging.ERROR):
        _break_lock(l10n_dir, how)
        assert lock_codes() is None
    assert "packs.lock.json" in caplog.text
    assert missing() == ()
    assert [(code, str(name)) for code, name in tax_country_options()] == [("", t("setup.country_other"))]

    loc = await _default_location(client, auth)
    await _patch_address(client, auth, loc["id"], {"country": "MY"})
    assert await _stored(session, company_id) == before

    await _sync_notice(session)
    [notice] = await _pack_notices(session, company_id)
    assert notice.i18n["body"] == "l10n.pack_lock_missing.body"
    assert notice.body == t("l10n.pack_lock_missing.body", "en")


def _hostile_json(rng, depth: int = 0):
    """A random JSON value: scalars, lists and objects, with lock-like objects common."""
    kinds = ["null", "bool", "int", "str", "list", "obj", "entry"] if depth < 3 else ["null", "bool", "int", "str"]
    kind = rng.choice(kinds)
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.randint(-5, 5)
    if kind == "str":
        return rng.choice(["", "TH", "2026.1", "a" * 64])
    if kind == "list":
        return [_hostile_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if kind == "entry":
        return {"version": "1", "sha256": rng.choice(["0" * 64, _hostile_json(rng, depth + 1)])}
    return {rng.choice(["TH", "MY", "AU", "IN", "sha256", "x"]): _hostile_json(rng, depth + 1)
            for _ in range(rng.randint(0, 4))}


def _lock_is_valid(lock) -> bool:
    """The reference: a lock is an object of code to an object carrying a string sha256."""
    return isinstance(lock, dict) and all(
        isinstance(entry, dict) and isinstance(entry.get("sha256"), str) for entry in lock.values())


def test_any_lock_shape_is_read_or_unreadable_never_raises(l10n_dir, caplog):
    """Seeded oracle over lock contents: a lock is accepted exactly when it has the lock's shape;
    any other JSON is the unreadable-lock state, and no reader raises."""
    import random

    from celerp.localization import lock_codes, missing, packs
    from celerp.tax_regimes import pack_missing, tax_countries

    real = json.loads((FIXTURE / "packs.lock.json").read_text(encoding="utf-8"))
    rng = random.Random(20261114)
    cases = [real, [], "TH", 3, None, {"TH": "2026.1"}, {**real, "TH": "2026.1"}, {**real, "MY": {"version": "1"}}]
    for _ in range(300):
        lock = _hostile_json(rng)
        if rng.random() < 0.4:
            lock = {**real, rng.choice(list(real)): _hostile_json(rng)}
        cases.append(lock)
    accepted = 0
    for lock in cases:
        (l10n_dir / "packs.lock.json").write_text(json.dumps(lock), encoding="utf-8")
        _reset()
        with caplog.at_level(logging.ERROR):
            loaded, codes, gone, offered = packs(), lock_codes(), missing(), tax_countries()
        if _lock_is_valid(lock):
            accepted += 1
            assert codes == tuple(lock), lock
            assert set(loaded) <= set(lock) and set(offered) == set(loaded), lock
            assert gone == tuple(c for c in lock if c not in loaded), lock
        else:
            assert codes is None and loaded == {} and gone == () and not offered, lock
            assert pack_missing("TH") and not pack_missing(""), lock
    assert accepted and accepted < len(cases)
    (l10n_dir / "packs.lock.json").write_text(json.dumps(real), encoding="utf-8")
    _reset()
    assert set(packs()) == set(real)


async def test_lock_entry_not_an_object_is_unreadable_through_the_app(l10n_dir, client, auth, session):
    """A lock still JSON but with an entry that is not an object: the locations page answers, the
    company's taxes stay, and the bell says the lock is unreadable."""
    from ui.i18n import t

    company_id = auth["company_id"]
    await _set_settings(session, company_id, _pin_shaped("TH"))
    before = await _stored(session, company_id)
    lock = json.loads((l10n_dir / "packs.lock.json").read_text(encoding="utf-8"))
    (l10n_dir / "packs.lock.json").write_text(json.dumps({**lock, "TH": "2026.1"}), encoding="utf-8")
    _reset()

    r = await client.get("/companies/me/locations", headers=auth["headers"])
    assert r.status_code == 200, r.text
    loc = await _default_location(client, auth)
    await _patch_address(client, auth, loc["id"], {"country": "MY"})
    assert await _stored(session, company_id) == before
    text, patch_location = await _location_cell_refusal("MY")
    assert str(t("l10n.pack_lock_missing.body")) in text.replace("&#x27;", "'")
    patch_location.assert_not_awaited()

    await _sync_notice(session)
    [notice] = await _pack_notices(session, company_id)
    assert notice.i18n["body"] == "l10n.pack_lock_missing.body"


async def test_other_country_is_accepted_when_the_lock_is_unreadable(l10n_dir):
    from ui.routes.setup import company_setup_error

    _break_lock(l10n_dir)
    text, patch_location = await _location_cell_refusal("")
    patch_location.assert_awaited_once()
    assert company_setup_error("USD", "blank", "") is None


# ── Writers and refusals ─────────────────────────────────────────────────────

async def test_missing_pack_keeps_company_taxes_and_notifies(l10n_dir, client, auth, session):
    company_id = auth["company_id"]
    await _set_settings(session, company_id, _pin_shaped("TH"))
    before = await _stored(session, company_id)
    _drop(l10n_dir, "TH")

    loc = await _default_location(client, auth)
    await _patch_address(client, auth, loc["id"], {"text": "1 Silom Road"})
    saved = await _patch_address(client, auth, loc["id"], {"text": "1 Silom Road", "country": "TH"})
    assert saved.get("taxes_from") is None
    assert await _stored(session, company_id) == before

    await _sync_notice(session)
    [notice] = await _pack_notices(session, company_id)
    assert notice.i18n["params"] == {"countries": ["TH"]}
    assert "Thailand" in notice.body and "TH" not in notice.body.split()


async def test_missing_pack_refusal_names_pack(l10n_dir):
    from fasthtml.common import to_xml

    from ui.i18n import t
    from ui.routes.settings import _location_display_cell
    from ui.routes.setup import company_setup_error

    _drop(l10n_dir, "TH")
    refusal = str(t("l10n.pack_missing.body", countries="Thailand"))

    text, patch_location = await _location_cell_refusal("TH")
    assert refusal in text.replace("&#x27;", "'")
    patch_location.assert_not_awaited()
    assert company_setup_error("THB", "blank", "TH") == ("country", refusal)
    assert "Thailand" in to_xml(_location_display_cell("loc1", "country", {"text": "1 Silom Road", "country": "TH"}))


async def test_country_editor_names_a_stored_country_whose_pack_is_missing(l10n_dir):
    """The editor reads as the cell does: the country by name, never its raw code."""
    from ui.i18n import t

    _drop(l10n_dir, "TH")
    loc = {**_LOC, "address": {"text": "1 Silom Road", "country": "TH"}}
    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [loc]})
        async with _ui_client() as ui:
            r = await ui.get("/settings/locations/loc1/country/edit")
    assert r.status_code == 200, r.text
    assert 'value="Thailand"' in r.text and 'value="TH"' in r.text
    assert ">TH<" not in r.text and str(t("setup.country_other")) in r.text


@pytest.mark.parametrize("address", ["1 Silom Road", None, ["TH"]])
async def test_country_editor_takes_an_address_that_is_not_a_record(l10n_dir, address):
    from ui.i18n import t

    with ExitStack() as stack:
        _stub(stack, "get_locations", return_value={"items": [{**_LOC, "address": address}]})
        async with _ui_client() as ui:
            r = await ui.get("/settings/locations/loc1/country/edit")
    assert r.status_code == 200, r.text
    assert f'value="{t("setup.country_other")}"' in r.text


async def test_pack_notice_names_countries_in_the_readers_language(l10n_dir, session):
    """The notice keeps the codes and is read with each country's name, in the reader's language."""
    from ui import i18n
    from ui.routes.notifications import _in_reader_language

    company_id = await _company(session)
    _drop(l10n_dir, "TH", "MY")
    await _sync_notice(session)
    [notice] = await _pack_notices(session, company_id)
    assert notice.i18n["params"] == {"countries": ["TH", "MY"]}
    assert notice.body == i18n.t("l10n.pack_missing.body", "en", countries="Thailand, Malaysia")
    listed = {"items": [{"id": str(notice.id), "title": notice.title, "body": notice.body, "i18n": notice.i18n}]}
    for lang in ("en", "ja"):
        i18n.set_lang(lang)
        try:
            [shown] = json.loads(_in_reader_language(json.dumps(listed).encode()))["items"]
            names = ", ".join(i18n.t(f"country.{c}") for c in ("TH", "MY"))
            assert shown["body"] == i18n.t("l10n.pack_missing.body", countries=names)
            assert "TH" not in shown["body"].replace(",", " ").split()
        finally:
            i18n.set_lang("en")


async def test_source_checkout_without_packs_offers_other_only(tmp_path, client, auth, session):
    """The package lock names every country, the packs directory is empty (a source checkout
    that never fetched): only Other country is offered and nothing is reseeded."""
    from ui.i18n import t
    from ui.routes.setup import company_setup_error, tax_country_options

    package_lock = REPO / "celerp" / "localization" / "packs.lock.json"
    codes = list(json.loads(package_lock.read_text(encoding="utf-8")))
    assert len(codes) == 22
    root = tmp_path / "checkout"
    (root / "packs").mkdir(parents=True)
    shutil.copy(package_lock, root / "packs.lock.json")
    saved = os.environ.get("CELERP_L10N_DIR")
    os.environ["CELERP_L10N_DIR"] = str(root)
    _reset()
    try:
        assert [(code, str(name)) for code, name in tax_country_options()] == [("", t("setup.country_other"))]
        assert company_setup_error("THB", "blank", "TH") == (
            "country", str(t("l10n.pack_missing.body", countries="Thailand")))

        company_id = auth["company_id"]
        await _set_settings(session, company_id, _pin_shaped("TH"))
        before = await _stored(session, company_id)
        loc = await _default_location(client, auth)
        await _patch_address(client, auth, loc["id"], {"country": "TH"})
        assert await _stored(session, company_id) == before

        await _sync_notice(session)
        companies = (await session.execute(select(Company.id))).scalars().all()
        assert companies
        for cid in companies:
            [notice] = await _pack_notices(session, cid)
            assert notice.i18n["params"] == {"countries": ", ".join(codes)}
    finally:
        if saved is None:
            os.environ.pop("CELERP_L10N_DIR", None)
        else:
            os.environ["CELERP_L10N_DIR"] = saved
        _reset()


async def test_country_outside_lock_refused_as_invalid(l10n_dir):
    from ui.i18n import t
    from ui.routes.setup import company_setup_error

    invalid = str(t("setup.invalid_country", value=repr("SG")))
    assert company_setup_error("SGD", "blank", "SG") == ("country", invalid)
    text, patch_location = await _location_cell_refusal("SG")
    assert invalid in text.replace("&#x27;", "'")
    patch_location.assert_not_awaited()

    _break_lock(l10n_dir)
    lock_body = str(t("l10n.pack_lock_missing.body"))
    assert company_setup_error("SGD", "blank", "SG") == ("country", lock_body)
    text, patch_location = await _location_cell_refusal("SG")
    assert lock_body in text.replace("&#x27;", "'")
    patch_location.assert_not_awaited()


async def test_existing_company_untouched(l10n_dir, client, auth, session, monkeypatch):
    """A company the pinned release seeded keeps its stored settings through a start and its
    reads; naming another country still re-seeds as it always did."""
    from celerp.tax_regimes import get_regime

    company_id = auth["company_id"]
    await _set_settings(session, company_id, _pin_shaped("TH"))
    before = await _stored(session, company_id)
    await _start(monkeypatch)
    for path in ("/companies/me", "/companies/me/taxes", "/companies/me/purchasing-taxes"):
        r = await client.get(path, headers=auth["headers"])
        assert r.status_code == 200, r.text
    assert await _stored(session, company_id) == before

    loc = await _default_location(client, auth)
    saved = await _patch_address(client, auth, loc["id"], {"country": "MY"})
    assert saved["taxes_from"] == "MY"
    after = await _stored(session, company_id)
    assert after["taxes"] == get_regime("MY")["taxes"] and after["taxes_seeded_for"] == "MY"
    assert after["currency"] == before["currency"]


def test_tax_regimes_has_no_country_data():
    """Country tax material lives in the packs; core keeps the fallback and its own history."""
    source = (REPO / "celerp" / "tax_regimes.py").read_text(encoding="utf-8")
    assert "TAX_REGIMES" not in source
    tree = ast.parse(source)
    country_keyed = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Dict)
                     and any(isinstance(k, ast.Constant) and isinstance(k.value, str) and len(k.value) == 2
                             and k.value.isupper() for k in node.keys)]
    assert country_keyed == []
    regimes = [node for node in ast.walk(tree) if isinstance(node, ast.Dict)
               and {k.value for k in node.keys if isinstance(k, ast.Constant)} >= {"currency", "taxes"}]
    assert len(regimes) == 1
    assigned = {t.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
                if isinstance(t, ast.Name) and getattr(node, "value", None) is regimes[0]}
    assert assigned == {"DEFAULT_REGIME"}
    assert any(isinstance(node, (ast.Assign, ast.AnnAssign)) and "RETIRED_SEED_ROWS" in ast.unparse(node).split("=")[0]
               for node in tree.body)


# ── The notice ───────────────────────────────────────────────────────────────

async def test_pack_notice_follows_pack_state(l10n_dir, session):
    from ui.i18n import t

    _drop(l10n_dir, "TH")
    await _sync_notice(session)
    assert await _pack_notices(session) == []  # a fresh install has nobody to tell

    company_id = await _company(session)
    await _sync_notice(session)
    [first] = await _pack_notices(session)
    assert first.company_id == company_id and first.i18n["params"] == {"countries": ["TH"]}
    assert first.category == "settings" and first.title == t(TITLE_KEY, "en")

    await _sync_notice(session)
    [again] = await _pack_notices(session)
    assert again.id == first.id

    _drop(l10n_dir, "MY")
    await _sync_notice(session)
    [both] = await _pack_notices(session)
    assert both.id == first.id and both.i18n["params"] == {"countries": ["TH", "MY"]}

    _break_lock(l10n_dir)
    await _sync_notice(session)
    [lock] = await _pack_notices(session)
    assert lock.id == first.id and lock.i18n["body"] == "l10n.pack_lock_missing.body"

    _restore(l10n_dir)
    await _sync_notice(session)
    assert await _pack_notices(session) == []


async def test_pack_notice_clears_when_packs_return(l10n_dir, session):
    companies = [await _company(session), await _company(session)]
    _drop(l10n_dir, "IN")
    await _sync_notice(session)
    for cid in companies:
        assert len(await _pack_notices(session, cid)) == 1

    _restore(l10n_dir)
    await _sync_notice(session)
    for cid in companies:
        assert await _pack_notices(session, cid) == []


async def _start(monkeypatch) -> None:
    """The start's data steps on the test database with no module enabled (needs the ``client``
    fixture, which routes the start's own sessions to the test's)."""
    from celerp.main import _bring_data_current

    monkeypatch.setattr("celerp.services.dev_release_guard.run_upgrade_guard",
                        AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))
    app = type("App", (), {"state": type("State", (), {})()})()
    assert await _bring_data_current(app, modules_ready=False) is True


async def test_pack_notice_runs_without_enabled_modules(l10n_dir, client, session, monkeypatch):
    company_id = await _company(session)
    _drop(l10n_dir, "TH")
    await _start(monkeypatch)
    [notice] = await _pack_notices(session, company_id)
    assert notice.i18n["params"] == {"countries": "TH"}


async def test_pack_notice_failure_is_logged_and_boot_continues(monkeypatch, caplog):
    from test_main_lifespan import _boot_in_order

    monkeypatch.setattr("celerp.localization.notice.sync_pack_notice",
                        AsyncMock(side_effect=RuntimeError("notice table unreadable")))
    with caplog.at_level(logging.ERROR, logger="celerp.main"):
        order, _notify, app = await _boot_in_order(
            monkeypatch, AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))
    assert app.state.data_current is True
    failed = [r for r in caplog.records if "Country pack notice failed" in r.getMessage()]
    assert len(failed) == 1 and "notice table unreadable" in caplog.text

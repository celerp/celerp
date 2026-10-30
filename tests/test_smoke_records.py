# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The packaged upgrade smoke and the update e2e read module pages only once
Celerp has finished starting: a module page answers 503 "still starting" until
the UI process reports, and that answer is never taken as the page's records."""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_smoke = _load("smoke_records")
_e2e = _load("e2e_update")

_STARTING = (503, {"detail": "Celerp is still starting. Try again in a moment."})


@pytest.fixture
def clock(monkeypatch):
    """A clock that moves one second per sleep, so waits run instantly."""
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def _api(starting_answers: int):
    """A fake API whose module pages answer "still starting" a number of times."""
    asked: dict[str, int] = {}

    def http(method, url, token=None, body=None, timeout=30):
        path = url.removeprefix("http://api")
        asked[path] = asked.get(path, 0) + 1
        if path == "/auth/login-force":
            return 200, {"access_token": "t"}
        if path == "/health":
            return 200, {"version": "1.0.0"}
        if path.startswith("/ledger"):
            return 200, {"items": [{"id": 1, "event_type": "company.created", "entity_id": "c"}]}
        if path == "/companies/me/locations":
            return 200, [{"name": "Smoke Warehouse"}]
        if asked[path] <= starting_answers:
            return _STARTING
        field = "sku" if path == "/items" else "name"
        return 200, {"items": [{field: "seeded"}]}
    return http


def test_snapshot_waits_for_module_pages_still_starting(monkeypatch, clock, capsys):
    monkeypatch.setattr(_smoke, "http", _api(starting_answers=3))

    _smoke.snapshot("http://api")

    served = json.loads(capsys.readouterr().out)["served"]
    assert served == {"/companies/me/locations": ["Smoke Warehouse"],
                      "/crm/contacts": ["seeded"], "/items": ["seeded"]}


def test_snapshot_fails_when_a_module_page_never_finishes_starting(monkeypatch, clock, capsys):
    monkeypatch.setattr(_smoke, "http", _api(starting_answers=10_000))

    with pytest.raises(SystemExit) as exc:
        _smoke.snapshot("http://api")

    assert exc.value.code == 1
    assert "/crm/contacts still answers that Celerp is starting" in capsys.readouterr().err


def test_module_get_returns_other_answers_at_once(monkeypatch, clock):
    monkeypatch.setattr(_smoke, "http", lambda *a, **k: (404, {"detail": "Not Found"}))

    assert _smoke.module_get("http://api", "/items", "t") == (404, {"detail": "Not Found"})
    assert clock[0] == 0.0


def test_update_e2e_waits_for_module_pages_through_the_same_helper():
    assert _e2e.module_get.__module__ == "smoke_records"
    assert _e2e.module_get.__code__.co_filename == _smoke.module_get.__code__.co_filename
    assert _e2e.http.__code__.co_filename == _smoke.http.__code__.co_filename


def _snapshot_file(tmp_path, name, version, ledger, served):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"version": version, "ledger": ledger, "served": served}), "utf-8")
    return str(path)


_LEDGER = [[1, "company.created", "c"]]


def test_carried_accepts_an_upgrade_that_kept_the_records_and_serves_every_page(tmp_path, capsys):
    before = _snapshot_file(tmp_path, "before", "2.5.6", _LEDGER, {"/items": 404, "/crm/contacts": ["seeded"]})
    after = _snapshot_file(tmp_path, "after", "2.5.7", _LEDGER, {"/items": [], "/crm/contacts": ["seeded"]})

    _smoke.carried(before, after)

    assert "carried from 2.5.6 to 2.5.7" in capsys.readouterr().out


@pytest.mark.parametrize("ledger, served, why", [
    ([], {"/items": ["seeded"]}, "the ledger changed"),
    (_LEDGER, {"/items": []}, "records changed"),
    (_LEDGER, {"/items": ["seeded"], "/crm/contacts": 500}, "pages not served"),
])
def test_carried_fails_when_the_upgrade_lost_records_or_a_page(tmp_path, capsys, ledger, served, why):
    before = _snapshot_file(tmp_path, "before", "2.5.6", _LEDGER, {"/items": ["seeded"]})
    after = _snapshot_file(tmp_path, "after", "2.5.7", ledger, served)

    with pytest.raises(SystemExit) as exc:
        _smoke.carried(before, after)

    assert exc.value.code == 1
    assert why in capsys.readouterr().err


@pytest.mark.parametrize("disposition, name", [
    ("attachment; filename*=utf-8''%E0%B9%83%E0%B8%9A.pdf", "ใบ.pdf"),
    ('attachment; filename="celerp-backup-2026-10-17-1200.celerp-backup"', "celerp-backup-2026-10-17-1200.celerp-backup"),
])
def test_sent_name_reads_either_form(disposition, name):
    assert _smoke.sent_name(disposition) == name


def _restored_api(attachment_bytes: bytes, attachment_name: str):
    """A fake API after a restore, serving the records and one attachment."""
    def transfer(method, url, token=None, data=None, content_type="application/json", timeout=30):
        from urllib.parse import quote
        return 200, attachment_bytes, {"content-disposition": f"attachment; filename*=utf-8''{quote(attachment_name)}"}
    return transfer


@pytest.mark.parametrize("served_bytes, served_name, ok", [
    (b"%PDF backed up", _smoke.ATTACHMENT, True),
    (b"%PDF changed", _smoke.ATTACHMENT, False),
    (b"%PDF backed up", "download", False),
])
def test_restored_requires_the_backed_up_records_and_attachment(monkeypatch, clock, tmp_path, capsys,
                                                                 served_bytes, served_name, ok):
    import hashlib
    monkeypatch.setattr(_smoke, "http", _api(starting_answers=0))
    monkeypatch.setattr(_smoke, "transfer", _restored_api(served_bytes, served_name))
    before = {"records": _smoke.records("http://api", "t"), "contact_id": "contact:1", "file_id": "f1",
              "attachment": {"name": _smoke.ATTACHMENT, "sha256": hashlib.sha256(b"%PDF backed up").hexdigest()}}
    (tmp_path / "before.json").write_text(json.dumps(before, ensure_ascii=False), "utf-8")

    if ok:
        _smoke.restored("http://api", str(tmp_path))
        assert "exactly" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit):
            _smoke.restored("http://api", str(tmp_path))
        assert "did not bring back" in capsys.readouterr().err


def test_backup_restore_waits_for_module_pages_before_its_first_write(monkeypatch, clock, tmp_path):
    """Module routes answer "still starting" for a while after /health/ready; the
    contact the backup carries is only posted once they answer."""
    reads = _api(starting_answers=0)
    posted = []

    def http(method, url, token=None, body=None, timeout=30):
        if url.endswith("/crm/contacts") and clock[0] < 3:
            return _STARTING
        if method == "POST" and url.endswith("/crm/contacts"):
            posted.append(body)
            return 200, {"id": "c1"}
        return reads(method, url, token, body, timeout)

    class Stop(Exception):
        pass

    def upload(*a, **k):
        raise Stop
    monkeypatch.setattr(_smoke, "http", http)
    monkeypatch.setattr(_smoke, "upload", upload)

    with pytest.raises(Stop):
        _smoke.backup_restore("http://api", str(tmp_path))

    assert posted == [{"name": _smoke.CUSTOMER}]


def test_stock_roles_are_the_roles_that_carry_stock_value():
    """The smoke runs without the app installed, so it holds the stock roles itself."""
    from celerp.accounting_roles import INVENTORY_VALUE_ROLES
    assert _smoke.STOCK_ROLES == {r.value for r in INVENTORY_VALUE_ROLES}


def test_posting_choices_keep_stock_accounts_for_stock_roles():
    def row(role, codes, current=None):
        return {"role": role, "required": True, "current": current, "preselect": None,
                "candidates": [{"code": c} for c in codes], "proposal": {"code": "9" + role[:3]}}
    readiness = [row("inventory", ["1010"], current="1010"),
                 row("landed_freight", ["1200", "1010"]),
                 row("cash", ["1010", "1000"]),
                 row("sales", ["1010"])]

    assert _smoke.posting_choices(readiness) == {
        "roles": {"landed_freight": "1010", "cash": "1000"},
        "add_accounts": [{"code": "9sal", "role": "sales"}]}


@pytest.mark.parametrize("first", [None, "", "  \n", "starting", "0", "70000"])
def test_serving_waits_for_a_port_file_that_is_missing_or_not_a_port_yet(monkeypatch, tmp_path, first):
    """Celerp rewrites its api-port file as it restarts: until the file names a port
    the API is not taken from it, and the wait ends as soon as it does."""
    port_file = tmp_path / "api-port"
    if first is not None:
        port_file.write_text(first)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 3:
            port_file.write_text("8123\n")

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)

    assert _smoke._serving(str(port_file), deadline=60) == "http://127.0.0.1:8123"
    assert 3 <= now[0] < 4


@pytest.mark.parametrize("value", [None, "", "8080x"])
def test_serving_fails_at_the_callers_deadline_without_making_up_a_port(clock, tmp_path, capsys, value):
    port_file = tmp_path / "api-port"
    if value is not None:
        port_file.write_text(value)

    with pytest.raises(SystemExit) as exc:
        _smoke._serving(str(port_file), deadline=20)

    assert exc.value.code == 1
    assert 20 <= clock[0] < 21
    assert f"no API port in {port_file}" in capsys.readouterr().err


def test_serving_waits_a_bounded_time_when_the_caller_has_no_deadline(clock, tmp_path):
    with pytest.raises(SystemExit):
        _smoke._serving(str(tmp_path / "api-port"))

    assert 0 < clock[0] <= 61


_DEPENDS = {"celerp-inventory": [], "celerp-contacts": ["celerp-inventory"],
            "celerp-docs": ["celerp-inventory", "celerp-contacts"], "celerp-manufacturing": ["celerp-inventory"],
            "celerp-verticals": ["celerp-inventory"]}


class _NewInstall:
    """A fake new install: the starter modules run, a restart loads every module a
    company uses unless it is ``broken``, and the business-type answer either says
    what the server would decide or ``lies`` about it for one type."""

    def __init__(self, tmp_path, lies=None, broken=()):
        self.port_file = tmp_path / "api-port"
        self.port_file.write_text("8001")
        self.running = {"celerp-inventory", "celerp-contacts"}
        self.companies: dict[str, dict] = {}
        self.current = None
        self.lies, self.broken = lies, set(broken)
        self.boot, self.restarts = 1, []

    def _new_company(self, name):
        cid = f"c{len(self.companies)}"
        self.companies[cid] = {"name": name, "modules": {"celerp-inventory", "celerp-contacts"}, "vertical": None}
        self.current = cid
        return {"access_token": cid}

    def http(self, method, url, token=None, body=None, timeout=30):
        path = url.split(":", 2)[2].split("/", 1)[1]
        path = "/" + path
        company = self.companies.get(token)
        if path == "/auth/register":
            return 200, self._new_company(body["company_name"])
        if path == "/auth/login-force":
            return 200, {"access_token": self.current}
        if path.startswith("/auth/switch-company/"):
            self.current = path.rsplit("/", 1)[1]
            return 200, {"access_token": self.current}
        if path == "/companies" and method == "POST":
            return 200, self._new_company(body["name"])
        if path == "/companies/me" and method == "PATCH":
            return 200, {}
        if path == "/companies/me":
            return 200, {"id": token, "settings": {"vertical": company["vertical"]}}
        if path == "/companies/me/modules":
            return 200, [{"name": n, "depends_on": d, "enabled": n in company["modules"],
                          "running": n in self.running} for n, d in _DEPENDS.items()]
        if path == "/companies/me/business-type":
            vertical = body["vertical"]
            wanted = set(_PRESETS[vertical])
            while True:
                more = {d for n in wanted for d in _DEPENDS[n]} - wanted
                if not more:
                    break
                wanted |= more
            company["modules"] |= wanted
            company["vertical"] = vertical
            need = any(n not in self.running for n in company["modules"])
            return 200, {"restart_required": (not need) if vertical == self.lies else need}
        if path == "/system/restart":
            self.restarts.append(self.current)
            self.boot += 1
            self.running = {n for c in self.companies.values() for n in c["modules"]} - self.broken
            self.port_file.write_text(str(8000 + self.boot))
            return 202, {}
        if path == "/health":
            return 200, {"boot": self.boot}
        if path == "/health/ready":
            return 200, {}
        raise AssertionError(f"unexpected {method} {path}")


_PRESETS = {"blank": ["celerp-inventory"], "retail": ["celerp-docs", "celerp-verticals"],
            "furniture": ["celerp-docs", "celerp-manufacturing"],
            "wholesale": ["celerp-docs"], "hidden": ["celerp-docs"]}


def _presets(tmp_path):
    folder = tmp_path / "presets"
    folder.mkdir()
    for name, modules in _PRESETS.items():
        (folder / f"{name}.json").write_text(json.dumps({"name": name, "modules": modules, "hidden": name == "hidden"}))
    return str(folder)


def test_first_run_restarts_only_for_the_types_whose_modules_are_not_running(monkeypatch, clock, tmp_path, capsys):
    install = _NewInstall(tmp_path)
    monkeypatch.setattr(_smoke, "http", install.http)

    _smoke.first_run(str(install.port_file), _presets(tmp_path))

    # blank runs on the starter modules, furniture turns on docs and manufacturing,
    # retail then needs only verticals and wholesale nothing new.
    chosen = {c["vertical"]: cid for cid, c in install.companies.items()}
    assert install.restarts == [chosen["furniture"], chosen["retail"]]
    assert "hidden" not in chosen
    out = capsys.readouterr().out
    assert "blank: set up, its modules were already running" in out
    assert "furniture: set up, restarted and its modules are running" in out


def test_first_run_fails_when_the_server_misjudges_the_restart(monkeypatch, clock, tmp_path, capsys):
    install = _NewInstall(tmp_path, lies="wholesale")
    monkeypatch.setattr(_smoke, "http", install.http)

    with pytest.raises(SystemExit):
        _smoke.first_run(str(install.port_file), _presets(tmp_path))

    assert "wholesale: the server says a restart is needed" in capsys.readouterr().err


def test_first_run_fails_when_the_server_skips_a_restart_the_type_needs(monkeypatch, clock, tmp_path, capsys):
    install = _NewInstall(tmp_path, lies="furniture")
    monkeypatch.setattr(_smoke, "http", install.http)

    with pytest.raises(SystemExit):
        _smoke.first_run(str(install.port_file), _presets(tmp_path))

    err = capsys.readouterr().err
    assert "furniture: the server says no restart is needed" in err
    assert "celerp-docs" in err and "celerp-manufacturing" in err


def test_first_run_fails_when_a_module_the_type_needs_is_not_running_after_the_restart(
        monkeypatch, clock, tmp_path, capsys):
    install = _NewInstall(tmp_path, broken={"celerp-manufacturing"})
    monkeypatch.setattr(_smoke, "http", install.http)

    with pytest.raises(SystemExit):
        _smoke.first_run(str(install.port_file), _presets(tmp_path))

    assert "furniture: after the restart celerp-manufacturing is not running" in capsys.readouterr().err

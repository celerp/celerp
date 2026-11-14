# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""scripts/l10n_packs.py: the one tool that fetches the country packs at build time, checks them
against the lock by digest, validates their fields and writes the lock.

``fetch`` and ``check`` use the standard library only, so they run under a bare runner Python
before anything is installed; ``fetch`` never prints a repository name, URL or tag (they are
private), only the country and the HTTP status. The committed fixture pack set passes both
``check`` and ``validate``, and the test session reads it from any working directory.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(150)  # each session-script run may wait up to 120 s

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "l10n_packs.py"
FIXTURE = REPO / "tests" / "fixtures" / "l10n"
CODES = ("TH", "MY", "AU", "IN")


def _script():
    spec = importlib.util.spec_from_file_location("l10n_packs_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = _script().main(list(argv))
    return code, out.getvalue()


@pytest.fixture()
def root(tmp_path):
    copy = tmp_path / "l10n"
    shutil.copytree(FIXTURE, copy)
    return copy


def _rewrite(root: Path, code: str, change, *, relock: bool = True) -> None:
    """Change one pack's fields, keep it in normalized form and, by default, re-digest it into
    the lock, so only the field defect remains."""
    import hashlib
    path = root / "packs" / f"{code}.json"
    pack = json.loads(path.read_text(encoding="utf-8"))
    change(pack)
    data = (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(data)
    if relock:
        _relock(root, code, hashlib.sha256(data).hexdigest())


def _relock(root: Path, code: str, digest: str) -> None:
    lock_path = root / "packs.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock[code]["sha256"] = digest
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def _not_normalized(root: Path) -> None:
    import hashlib
    path = root / "packs" / "TH.json"
    data = (json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=4) + "\n").encode("utf-8")
    path.write_bytes(data)
    _relock(root, "TH", hashlib.sha256(data).hexdigest())


def _set(key, value):
    return lambda pack: pack.__setitem__(key, value)


def _bad_row(pack):
    pack["taxes"][0]["rate"] = "seven"


def _row(**fields):
    return lambda pack: pack["taxes"][0].update(fields)


def _duplicate_name(pack):
    pack["taxes"].append({**pack["taxes"][1], "name": " " + pack["taxes"][0]["name"].lower()})


def _bad_currency(pack):
    pack["company_defaults"]["currency"] = "ZZZ"


_CHECK_CASES = {
    "bad digest": (lambda r: (r / "packs" / "TH.json").write_bytes(
        (r / "packs" / "TH.json").read_bytes().replace(b"7.0", b"8.0", 1)), "sha256"),
    "extra file": (lambda r: shutil.copy(r / "packs" / "TH.json", r / "packs" / "ZZ.json"), "unexpected file"),
    "missing file": (lambda r: (r / "packs" / "AU.json").unlink(), "missing"),
    "lock entry not an object": (lambda r: (r / "packs.lock.json").write_text(
        json.dumps({"TH": "x"}), encoding="utf-8"), "unreadable"),
}
_VALIDATE_CASES = {
    "not normalized": (_not_normalized, "normalized"),
    "wrong country": (lambda r: _rewrite(r, "TH", _set("country", "MY")), "country field"),
    "wrong version": (lambda r: _rewrite(r, "TH", _set("version", "2099.1")), "version"),
    "schema_version not 1": (lambda r: _rewrite(r, "TH", _set("schema_version", 2)), "schema_version"),
    "unknown currency": (lambda r: _rewrite(r, "TH", _bad_currency), "currency"),
    "invalid tax row": (lambda r: _rewrite(r, "TH", _bad_row), "tax row"),
    "tax side outside the three": (lambda r: _rewrite(r, "TH", _row(tax_type="output")), "tax row 0 is invalid (tax_type)"),
    "default flag not a bool": (lambda r: _rewrite(r, "TH", _row(is_default="yes")), "tax row 0 is invalid (is_default)"),
    "duplicate tax name": (lambda r: _rewrite(r, "TH", _duplicate_name), "duplicate tax name"),
    "company defaults not an object": (lambda r: _rewrite(r, "TH", _set("company_defaults", ["THB"])), "currency"),
}


@pytest.mark.parametrize("case", list(_CHECK_CASES) + ["lock differs"] + list(_VALIDATE_CASES))
def test_l10n_packs_check_refuses(root, tmp_path, case):
    if case == "lock differs":
        other = tmp_path / "committed.lock.json"
        other.write_text(json.dumps({"TH": {"version": "fixture", "schema_version": 1, "sha256": "0" * 64}}),
                         encoding="utf-8")
        code, out = _run("check", str(root), "--lock", str(other))
        assert code != 0 and "differs from" in out, out
        return
    command = "check" if case in _CHECK_CASES else "validate"
    mutate, defect = (_CHECK_CASES | _VALIDATE_CASES)[case]
    assert _run(command, str(root))[0] == 0
    mutate(root)
    code, out = _run(command, str(root))
    assert code != 0, out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1 and defect in lines[0], out


# ── lock: the locked country set changes only by --add and --remove ─────────

def _lock_bytes(root: Path) -> bytes:
    return (root / "packs.lock.json").read_bytes()


def _new_pack(root: Path, code: str) -> None:
    pack = json.loads((root / "packs" / "TH.json").read_text(encoding="utf-8"))
    pack["country"] = code
    (root / "packs" / f"{code}.json").write_bytes((json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode())


def test_lock_rewrites_the_same_set_unchanged(root):
    before = _lock_bytes(root)
    assert _run("lock", str(root)) == (0, "")
    assert _lock_bytes(root) == before


@pytest.mark.parametrize("gone", [CODES, ("AU",)], ids=["unfetched checkout", "partial checkout"])
def test_lock_fails_when_a_locked_pack_file_is_missing(root, gone):
    before = _lock_bytes(root)
    for code in gone:
        (root / "packs" / f"{code}.json").unlink()
    code, out = _run("lock", str(root))
    assert code != 0
    assert [line for line in out.splitlines() if line] == [
        f"country {c}: pack file missing (pass --remove {c} to take it out of the lock)" for c in CODES if c in gone]
    assert _lock_bytes(root) == before


def test_lock_adds_a_country_only_when_asked(root):
    before = _lock_bytes(root)
    _new_pack(root, "SG")
    code, out = _run("lock", str(root))
    assert code != 0 and out.strip() == "packs/SG.json: not in the lock (pass --add SG to lock it)"
    assert _lock_bytes(root) == before
    assert _run("lock", str(root), "--add", "SG") == (0, "")
    lock = json.loads(_lock_bytes(root))
    assert list(lock) == [*json.loads(before), "SG"]
    assert _run("check", str(root)) == (0, "")


def test_lock_removes_a_country_only_when_asked(root):
    before = json.loads(_lock_bytes(root))
    (root / "packs" / "AU.json").unlink()
    assert _run("lock", str(root), "--remove", "AU") == (0, "")
    assert list(json.loads(_lock_bytes(root))) == [c for c in before if c != "AU"]
    assert _run("check", str(root)) == (0, "")


@pytest.mark.parametrize("argv, defect", [
    (("--add", "TH"), "country TH: already in the lock"),
    (("--remove", "SG"), "country SG: not in the lock"),
    (("--remove", "AU"), "packs/AU.json: not in the lock (pass --add AU to lock it)"),
])
def test_lock_refuses_a_change_that_does_not_fit(root, argv, defect):
    before = _lock_bytes(root)
    code, out = _run("lock", str(root), *argv)
    assert code != 0 and out.strip() == defect, out
    assert _lock_bytes(root) == before


def _sources(codes=CODES) -> str:
    return json.dumps({code: f"acme-private/secret-{code.lower()}@v2026.9" for code in codes})


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serving(fail: dict | None = None):
    """An opener that serves each fixture pack from its repository URL, or raises ``fail[code]``."""
    def _open(request, *a, **k):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        code = next(c for c in CODES if f"secret-{c.lower()}/" in url)
        if fail and code in fail:
            raise fail[code](url)
        return _Response((FIXTURE / "packs" / f"{code}.json").read_bytes())
    return _open


def _fetch_root(tmp_path) -> Path:
    target = tmp_path / "fetched"
    (target / "packs").mkdir(parents=True)
    shutil.copy(FIXTURE / "packs.lock.json", target / "packs.lock.json")
    return target


def test_l10n_packs_fetch_writes_and_checks(tmp_path, monkeypatch):
    target = _fetch_root(tmp_path)
    monkeypatch.setenv("CELERP_L10N_SOURCES", _sources())
    monkeypatch.setenv("CELERP_L10N_TOKEN", "token-value")
    monkeypatch.setattr(urllib.request, "urlopen", _serving())
    code, out = _run("fetch", str(target))
    assert code == 0, out
    assert sorted(p.name for p in (target / "packs").iterdir()) == sorted(f"{c}.json" for c in CODES)


def _http_404(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


@pytest.mark.parametrize("case", ["not json", "missing country", "extra country", "no token", "http error"])
def test_l10n_packs_fetch_fails_closed(tmp_path, monkeypatch, case):
    target = _fetch_root(tmp_path)
    sources = {"not json": "{acme-private/secret-th@v2026.9", "missing country": _sources(CODES[:3]),
               "extra country": _sources(CODES + ("SG",))}.get(case, _sources())
    monkeypatch.setenv("CELERP_L10N_SOURCES", sources)
    if case == "no token":
        monkeypatch.delenv("CELERP_L10N_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CELERP_L10N_TOKEN", "token-value")
    monkeypatch.setattr(urllib.request, "urlopen", _serving({"IN": _http_404} if case == "http error" else None))
    code, out = _run("fetch", str(target))
    assert code != 0, out
    assert list((target / "packs").iterdir()) == []
    assert "acme-private" not in out and "secret-" not in out


@pytest.mark.parametrize("error, shown", [
    (_http_404, "country TH: fetch failed (HTTP 404)"),
    (lambda url: urllib.error.URLError(f"no route to {url}"), "country TH: fetch failed (network error)"),
])
def test_l10n_packs_fetch_hides_repo_names(tmp_path, monkeypatch, error, shown):
    target = _fetch_root(tmp_path)
    sources = _sources()
    monkeypatch.setenv("CELERP_L10N_SOURCES", sources)
    monkeypatch.setenv("CELERP_L10N_TOKEN", "token-value")
    monkeypatch.setattr(urllib.request, "urlopen", _serving({"TH": error}))
    code, out = _run("fetch", str(target))
    assert code != 0 and shown in out, out
    for value in json.loads(sources).values():
        repo, tag = value.split("@")
        for secret in (value, repo, tag, *repo.split("/")):
            assert secret not in out


# ── Subprocesses (marked process) ────────────────────────────────────────────

def _python(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True, text=True, timeout=120,
                          env=env if env is not None else os.environ.copy())


@pytest.mark.process
def test_fixture_packs_pass_check(tmp_path):
    for command in ("check", "validate"):
        done = _python(str(SCRIPT), command, str(FIXTURE), cwd=REPO)
        assert done.returncode == 0, done.stdout + done.stderr
    configured = os.environ["CELERP_L10N_DIR"]
    assert Path(configured).is_absolute() and Path(configured) == FIXTURE
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    done = _python("-c", "import json; from celerp.localization import packs; print(json.dumps(sorted(packs())))",
                   cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == sorted(CODES)


@pytest.mark.process
def test_real_packs_session_stops_without_packs(tmp_path):
    root = tmp_path / "unfetched"
    (root / "packs").mkdir(parents=True)
    shutil.copy(REPO / "celerp" / "localization" / "packs.lock.json", root / "packs.lock.json")
    env = os.environ.copy()
    env["CELERP_L10N_DIR"] = str(root)
    done = _python("-m", "pytest", "-m", "real_packs", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
                   "tests/test_l10n_golden.py", cwd=REPO, env=env)
    out = done.stdout + done.stderr
    assert done.returncode == 4, out
    assert "country packs missing: run scripts/l10n_packs.py fetch" in out
    assert " passed" not in out and " failed" not in out


@pytest.mark.process
def test_fixture_session_stops_when_a_fixture_pack_does_not_load(tmp_path):
    """Tests read AU's expectations through the pack reader; a fixture set missing AU stops the
    session instead of letting them meet the generic fallback."""
    root = tmp_path / "l10n"
    shutil.copytree(FIXTURE, root)
    (root / "packs" / "AU.json").unlink()
    env = os.environ.copy()
    env["CELERP_L10N_DIR"] = str(root)
    done = _python("-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
                   "tests/test_recon_default_bank_currency.py", cwd=REPO, env=env)
    out = done.stdout + done.stderr
    assert done.returncode == 4, out
    assert f"fixture country packs do not load from {root}" in out
    assert " passed" not in out and " failed" not in out


_STUB_FETCH = """
import io, runpy, sys, urllib.request
fixture, target, script = sys.argv[1:4]
class R(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *e): return False
def opener(request, *a, **k):
    url = request.full_url
    code = url.split("secret-")[1].split("/")[0].upper()
    return R(open(f"{fixture}/packs/{code}.json", "rb").read())
urllib.request.urlopen = opener
sys.argv = [script, "fetch", target]
runpy.run_path(script, run_name="__main__")
"""


@pytest.mark.process
def test_check_l10n_packs_stdlib_fetch(tmp_path):
    """fetch and check run under a Python with nothing but the standard library."""
    target = _fetch_root(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(CELERP_L10N_SOURCES=_sources(), CELERP_L10N_TOKEN="token-value")
    done = _python("-I", "-S", "-c", _STUB_FETCH, str(FIXTURE), str(target), str(SCRIPT), cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(p.name for p in (target / "packs").iterdir()) == sorted(f"{c}.json" for c in CODES)

    done = _python("-I", "-S", str(SCRIPT), "check", str(FIXTURE), "--lock", str(FIXTURE / "packs.lock.json"),
                   cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stdout + done.stderr

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    top = {alias.name.split(".")[0] for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    top |= {node.module.split(".")[0] for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
            and node.level == 0}
    assert top <= set(sys.stdlib_module_names) | {"__future__"}, top - set(sys.stdlib_module_names)
    app_imports = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                   for node in ast.walk(fn) if isinstance(node, (ast.Import, ast.ImportFrom))
                   and any(name.startswith("celerp") for name in
                           ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]))}
    assert app_imports == {"validate"}

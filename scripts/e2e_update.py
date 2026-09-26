# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end proof that a pip install updates itself from the app.

Everything here is real: wheels built from this tree, fresh virtual
environments, the bundled PostgreSQL, `celerp start` as the supervisor, and the
HTTP API as the install owner. Nothing in the product has a test-only switch;
the updater finds the new version the same way it finds one on PyPI, through
PIP_FIND_LINKS.

Wheels built per run (release versions, so the updater accepts them):
  base         90.0.0  this tree
  good         90.0.1  adds a migration that creates a table
  pip_fail     90.0.2  needs a package that cannot be built
  migrate_fail 90.0.3  its migration creates a table, then fails
  health_fail  90.0.4  migrates, then the API fails at startup
  good_pg      90.0.5  good, plus a newer celerp-postgres package

Scenarios:
  E1 update to good                 E6 2.5.0 from PyPI -> base -> good
  E2 update to pip_fail             E7 update that replaces celerp-postgres
  E3 update to migrate_fail         E8 two requests at once, then "current"
  E4 update to health_fail          E9 member and anonymous requests refused
  E5 supervisor killed mid-update, finished by the next start

Release gate (publish.yml): --release-wheel PATH runs
  R1 a fresh install of that wheel
  R2 the current PyPI release updated to that wheel

Usage:
  python scripts/e2e_update.py [--scenario E1 --scenario E4 ...] [--work DIR] [--keep]
  python scripts/e2e_update.py --release-wheel dist/celerp-X-py3-none-any.whl
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
MARKER_TABLE = "e2e_update_marker"
MARKER_REVISION = "e2e0update0marker"
BROKEN_DEP = "e2e-broken-dep"
PG_PACKAGE = "celerp-postgres"
PASSWORD = "e2e-update-password-1"
UPDATE_TIMEOUT = 900

VERSIONS = {
    "base": "90.0.0",
    "good": "90.0.1",
    "pip_fail": "90.0.2",
    "migrate_fail": "90.0.3",
    "health_fail": "90.0.4",
    "good_pg": "90.0.5",
}


class Failed(AssertionError):
    pass


def check(cond: bool, what: str) -> None:
    if not cond:
        raise Failed(what)
    print(f"    ok  {what}")


def run(cmd: list, timeout: float = 900, **kw) -> subprocess.CompletedProcess:
    """Run a command to completion; a hang fails the scenario, naming the command.

    Output goes through files, not pipes: a server the command leaves running
    (the bundled PostgreSQL) can inherit a pipe on Windows and hold it open."""
    cmd = [str(c) for c in cmd]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out, \
            tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        try:
            code = subprocess.run(cmd, stdout=out, stderr=err, timeout=timeout, **kw).returncode
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{' '.join(cmd)} did not finish within {timeout:.0f}s") from exc
        out.seek(0)
        err.seek(0)
        result = subprocess.CompletedProcess(cmd, code, out.read(), err.read())
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {result.returncode}:\n"
                           f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, token: str | None = None, body: dict | None = None,
         timeout: float = 30) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"raw": raw.decode(errors="replace")}


# ── Wheels ────────────────────────────────────────────────────────────────────


def _alembic_head(tree: Path) -> str:
    revisions, parents = set(), set()
    for f in (tree / "celerp" / "migrations" / "versions").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        revisions.add(re.search(r"^revision\s*(?::[^=]*)?=\s*['\"]([^'\"]+)", text, re.M).group(1))
        down = re.search(r"^down_revision\s*(?::[^=]*)?=\s*(.*)$", text, re.M).group(1)
        parents.update(re.findall(r"['\"]([^'\"]+)['\"]", down))
    heads = revisions - parents
    if len(heads) != 1:
        raise RuntimeError(f"expected one alembic head, found {sorted(heads)}")
    return heads.pop()


def _add_marker_migration(tree: Path, *, fail: bool) -> None:
    body = f"""\"\"\"End-to-end update check: one new table.\"\"\"
import sqlalchemy as sa
from alembic import op

revision = "{MARKER_REVISION}"
down_revision = "{_alembic_head(tree)}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("{MARKER_TABLE}", sa.Column("id", sa.Integer, primary_key=True))
    {'raise RuntimeError("e2e: this migration fails after creating a table")' if fail else ''}


def downgrade() -> None:
    op.drop_table("{MARKER_TABLE}")
"""
    (tree / "celerp" / "migrations" / "versions" / f"{MARKER_REVISION}.py").write_text(body, encoding="utf-8")
    # A real release declares the table in the kernel models as well. The stamp
    # repair in `celerp migrate` only trusts migration DDL the models still own.
    with (tree / "celerp" / "models" / "base.py").open("a", encoding="utf-8") as f:
        f.write(f"""

import sqlalchemy as _sa


class E2EUpdateMarker(Base):
    __tablename__ = "{MARKER_TABLE}"
    id = _sa.Column(_sa.Integer, primary_key=True)
""")


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) < 1:
        raise RuntimeError(f"{path.name}: {old!r} not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _break_startup(tree: Path) -> None:
    _replace_once(tree / "celerp" / "main.py", "async def lifespan(_app: FastAPI):\n",
                  "async def lifespan(_app: FastAPI):\n"
                  "    raise RuntimeError('e2e: this version fails at startup')\n")


def _require_broken_dep(tree: Path) -> None:
    _replace_once(tree / "pyproject.toml", "dependencies = [\n",
                  f'dependencies = [\n    "{BROKEN_DEP}==1.0",\n')


def _require_pg(tree: Path, version: str) -> None:
    path = tree / "pyproject.toml"
    text, n = re.subn(rf'"{PG_PACKAGE}==[^;"]+', f'"{PG_PACKAGE}=={version} ', path.read_text(encoding="utf-8"))
    if n != 1:
        raise RuntimeError(f"expected one {PG_PACKAGE} pin, found {n}")
    path.write_text(text, encoding="utf-8")


def _broken_dep_sdist(out: Path) -> None:
    """An sdist whose metadata builds (so pip's dry run offers the update) but
    whose wheel build fails (so the install itself fails)."""
    name = "e2e_broken_dep-1.0"
    files = {
        "pyproject.toml": '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n',
        "PKG-INFO": "Metadata-Version: 2.1\nName: e2e-broken-dep\nVersion: 1.0\n",
        "backend.py": (
            "import os\n"
            "def get_requires_for_build_wheel(config_settings=None):\n    return []\n"
            "def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):\n"
            "    d = os.path.join(metadata_directory, 'e2e_broken_dep-1.0.dist-info')\n"
            "    os.makedirs(d, exist_ok=True)\n"
            "    with open(os.path.join(d, 'METADATA'), 'w') as f:\n"
            "        f.write('Metadata-Version: 2.1\\nName: e2e-broken-dep\\nVersion: 1.0\\n')\n"
            "    return 'e2e_broken_dep-1.0.dist-info'\n"
            "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
            "    raise RuntimeError('e2e: this package cannot be built')\n"
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / name
        root.mkdir()
        for rel, text in files.items():
            (root / rel).write_text(text, encoding="utf-8")
        with tarfile.open(out / f"{name}.tar.gz", "w:gz") as tar:
            tar.add(root, arcname=name)


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _repack_pg_wheel(src_dir: Path, out: Path, old: str, new: str) -> None:
    """The same celerp-postgres wheel under a newer version number: a package
    replacement the update has to carry out while the cluster uses it."""
    src = next(src_dir.glob("celerp_postgres-*.whl"))
    old_info, new_info = f"celerp_postgres-{old}.dist-info/", f"celerp_postgres-{new}.dist-info/"
    entries = []
    with zipfile.ZipFile(src) as zin:
        for info in zin.infolist():
            name = info.filename.replace(old_info, new_info, 1)
            data = zin.read(info)
            if name == new_info + "METADATA":
                data = data.replace(f"\nVersion: {old}\n".encode(), f"\nVersion: {new}\n".encode(), 1)
            entries.append((info, name, data))
    record = []
    target = out / src.name.replace(f"-{old}-", f"-{new}-")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, name, data in entries:
            if name == new_info + "RECORD":
                continue
            zi = zipfile.ZipInfo(name, info.date_time)
            zi.external_attr = info.external_attr
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)
            record.append(f"{name},{_record_hash(data)},{len(data)}")
        record.append(f"{new_info}RECORD,,")
        zout.writestr(new_info + "RECORD", "\n".join(record) + "\n")


def _pg_pin(tree: Path) -> str | None:
    """The celerp-postgres version constraints.txt pins for this platform, if any."""
    try:
        from packaging.markers import Marker
    except ImportError:  # the harness python may lack packaging; pip vendors it
        from pip._vendor.packaging.markers import Marker
    for line in (tree / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{PG_PACKAGE}=="):
            version, _, marker = line[len(PG_PACKAGE) + 2:].partition(";")
            if not marker.strip() or Marker(marker.strip()).evaluate():
                return version.strip()
    return None


def build_wheels(work: Path, names: set[str]) -> dict[str, Path]:
    """Build the requested variants; returns name -> directory holding its wheel."""
    src = work / "src"
    if not src.exists():
        files = run(["git", "ls-files", "-z"], cwd=REPO).stdout.split("\0")
        for rel in filter(None, files):
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (REPO / rel).is_file():
                shutil.copy2(REPO / rel, dest)
        run([sys.executable, "scripts/pin_release_deps.py"], cwd=src)
    variants = {
        "base": [],
        "good": [lambda t: _add_marker_migration(t, fail=False)],
        "pip_fail": [lambda t: _add_marker_migration(t, fail=False), _require_broken_dep],
        "migrate_fail": [lambda t: _add_marker_migration(t, fail=True)],
        "health_fail": [lambda t: _add_marker_migration(t, fail=False), _break_startup],
        "good_pg": [lambda t: _add_marker_migration(t, fail=False)],
    }
    if _pg_pin(src) is None:  # E7 is skipped where celerp-postgres is not pinned
        names = names - {"good_pg"}
    dirs = {}
    for name in sorted(names):
        out = work / "wheels" / name
        dirs[name] = out
        if out.exists() and any(out.iterdir()):
            continue
        out.mkdir(parents=True, exist_ok=True)
        tree = work / "trees" / name
        shutil.rmtree(tree, ignore_errors=True)
        shutil.copytree(src, tree)
        for change in variants[name]:
            change(tree)
        if name == "pip_fail":
            _broken_dep_sdist(out)
        if name == "good_pg":
            old = _pg_pin(tree)
            new = old.rsplit(".", 1)[0] + "." + str(int(old.rsplit(".", 1)[1]) + 1)
            run([sys.executable, "-m", "pip", "download", "--no-deps", "-d", work / "pg", f"{PG_PACKAGE}=={old}"])
            _repack_pg_wheel(work / "pg", out, old, new)
            _require_pg(tree, new)
        print(f"  building {name} {VERSIONS[name]}")
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", out, tree],
            env={**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": VERSIONS[name]})
    return dirs


# ── One install ───────────────────────────────────────────────────────────────


class Install:
    """A fresh venv, config dir and data dir, driven like a headless install."""

    def __init__(self, work: Path, name: str):
        self.root = work / "runs" / name
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.venv = self.root / "venv"
        self.config = self.root / "config" / "celerp"
        self.api_port, self.ui_port = free_port(), free_port()
        self.api = f"http://127.0.0.1:{self.api_port}"
        self.proc: subprocess.Popen | None = None
        self.log = self.root / "celerp.log"
        self.find_links: list[Path] = []
        self.owner = ""
        self.env = {
            **os.environ,
            "CELERP_CONFIG": str(self.config / "config.toml"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "APPDATA": str(self.root / "config"),
            "CELERP_DATA_DIR": str(self.root / "data"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        for key in ("CELERP_INSTALL_CHANNEL", "CELERP_APP_VERSION", "DATABASE_URL", "PIP_FIND_LINKS",
                    "PIP_NO_INDEX", "VIRTUAL_ENV"):
            self.env.pop(key, None)

    @property
    def python(self) -> Path:
        return self.venv / ("Scripts/python.exe" if WINDOWS else "bin/python")

    def pip(self, *args, index: bool = True) -> str:
        env = dict(self.env)
        if self.find_links:
            env["PIP_FIND_LINKS"] = " ".join(p.as_uri() for p in self.find_links)
        if not index:
            env["PIP_NO_INDEX"] = "1"
        return run([self.python, "-m", "pip", *args], env=env).stdout

    def create(self, requirement: str) -> None:
        run([sys.executable, "-m", "venv", self.venv])
        self.pip("install", "-q", "--upgrade", "pip")
        extra = [PG_PACKAGE] if sys.platform == "darwin" else []
        self.pip("install", "-q", requirement, *extra)

    def create_as_existing(self, requirement: str) -> None:
        """A published release, installed the way its existing users have it.
        Releases up to 2.5 float their dependencies, and their installs predate
        SQLAlchemy 2.1 (a new default PostgreSQL driver they do not ship), so a
        floating release gets the SQLAlchemy of its time."""
        self.create(requirement)
        pinned = self.py("from importlib.metadata import requires; "
                         "print(any(r.lower().startswith('sqlalchemy') and '==' in r for r in requires('celerp')))")
        if pinned.strip() != "True":
            self.pip("install", "-q", "sqlalchemy<2.1")

    def celerp(self, *args) -> str:
        return run([self.python, "-m", "celerp", *args], env=self.env, cwd=self.root).stdout

    def init(self) -> None:
        self.celerp("init", "--embedded", "--no-start",
                    "--api-port", self.api_port, "--ui-port", self.ui_port)

    def start(self, *, expect_version: str | None = None) -> None:
        env = dict(self.env)
        # The updater finds new versions only where these point, like a mirror.
        env["PIP_FIND_LINKS"] = " ".join(p.as_uri() for p in self.find_links)
        env["PIP_NO_INDEX"] = "1"
        log = open(self.log, "a", encoding="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        self.proc = subprocess.Popen([str(self.python), "-m", "celerp", "start"], env=env, cwd=self.root,
                                     stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
                                     start_new_session=not WINDOWS)
        self.wait_healthy(expect_version, timeout=300)

    def version(self) -> str | None:
        try:
            status, body = http("GET", self.api + "/health", timeout=3)
        except OSError:
            return None
        return body.get("version") if status == 200 else None

    def wait_healthy(self, version: str | None, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise Failed(f"celerp start exited {self.proc.returncode}; see {self.log}")
            try:
                ready, _ = http("GET", self.api + "/health/ready", timeout=3)
            except OSError:
                ready = 0
            if ready == 200 and (version is None or self.version() == version):
                return
            time.sleep(1)
        raise Failed(f"not healthy on {version or 'any version'} within {timeout}s; see {self.log}")

    def kill(self) -> None:
        """The supervisor and everything it started, as a crash would."""
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()

    def stop(self, *, group: bool = False) -> None:
        """SIGTERM to the supervisor alone, so the checks see that it stops what
        it started. `group` signals the whole process group instead, as Ctrl+C
        in a terminal or a service manager does."""
        if self.proc is None or self.proc.poll() is not None:
            return
        if WINDOWS:
            # No console signal reaches a detached process group reliably, so
            # stop the tree, then the cluster the way `celerp start` does on exit.
            self.kill()
            self.py("from pathlib import Path; import sys; from celerp import embedded_pg; "
                    "embedded_pg.stop_cluster(Path(sys.argv[1]))", str(self.config))
        else:
            if group:
                os.killpg(self.proc.pid, signal.SIGTERM)
            else:
                self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.kill()
                raise Failed("celerp start did not stop on SIGTERM")

    def py(self, code: str, *args) -> str:
        return run([self.python, "-c", code, *args], env=self.env, cwd=self.root).stdout

    # ── what the checks read ──

    def state(self) -> dict:
        try:
            return json.loads((self.config / "update_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def freeze(self) -> list[str]:
        return sorted(self.pip("freeze").splitlines())

    def db(self) -> dict:
        """Tables, alembic revision and row counts, read straight from Postgres."""
        out = self.py(r"""
import asyncio, json
import asyncpg
from celerp.config import read_config
url = read_config()["database"]["url"].replace("postgresql+asyncpg://", "postgresql://")
async def main():
    c = await asyncpg.connect(url)
    try:
        tables = sorted(r[0] for r in await c.fetch(
            "select table_name from information_schema.tables where table_schema='public'"))
        rev = await c.fetchval("select version_num from alembic_version")
        counts = {t: await c.fetchval(f'select count(*) from "{t}"')
                  for t in ("companies", "users", "user_companies", "locations")}
        print(json.dumps({"tables": tables, "revision": rev, "counts": counts}))
    finally:
        await c.close()
asyncio.run(main())
""")
        return json.loads(out)

    def processes(self) -> list[dict]:
        """Processes whose command line mentions this install's directory."""
        out = self.py(r"""
import json, os, sys, psutil
root = sys.argv[1]
found = []
for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
    if p.info["pid"] == os.getpid():
        continue
    try:
        line = " ".join(p.info["cmdline"] or [])
        if root in line:
            parent = psutil.Process(p.info["ppid"]).name() if p.info["ppid"] else ""
            found.append({"pid": p.info["pid"], "name": p.info["name"], "parent": parent, "cmd": line[:200]})
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
print(json.dumps(found))
""", str(self.root))
        return json.loads(out)

    def postmasters(self) -> int:
        return sum(1 for p in self.processes()
                   if "postgres" in p["name"].lower() and "postgres" not in p["parent"].lower())

    # ── the app, as the owner ──

    def register(self) -> None:
        code_file = self.config / "setup-code"
        code = code_file.read_text().strip() if code_file.exists() else None
        status, body = http("POST", self.api + "/auth/register", body={
            "company_name": "E2E Trading", "email": "owner@example.com", "name": "Owner",
            "password": PASSWORD, "setup_code": code})
        check(status == 200, f"owner registered ({status})")
        self.owner = body["access_token"]

    def login(self, email: str) -> str:
        """A new session for `email`. Without Connect an install serves one
        session at a time, so this takes over from any other."""
        status, body = http("POST", self.api + "/auth/login-force", body={"email": email, "password": PASSWORD})
        if status != 200:
            raise Failed(f"login {email}: {status} {body}")
        return body["access_token"]

    def seed(self) -> None:
        status, _ = http("POST", self.api + "/companies/me/locations", self.owner,
                         {"name": "E2E Warehouse", "type": "warehouse"})
        check(status == 200, "a location was created")

    def seeded_location_present(self) -> bool:
        status, body = http("GET", self.api + "/companies/me/locations", self.owner)
        items = body.get("items", body) if isinstance(body, dict) else body
        return status == 200 and any(loc.get("name") == "E2E Warehouse" for loc in items)

    def system_notices(self) -> list[dict]:
        status, body = http("GET", self.api + "/notifications?limit=100", self.owner)
        if status != 200:
            raise Failed(f"notifications: {status}")
        return [n for n in body["items"] if n["category"] == "system"]


# ── Shared steps and checks ───────────────────────────────────────────────────


def fresh(work: Path, name: str, wheels: dict[str, Path], target: str | None) -> Install:
    inst = Install(work, name)
    inst.find_links = [wheels["base"]] + ([wheels[target]] if target else [])
    inst.create(f"celerp=={VERSIONS['base']}")
    inst.init()
    inst.start(expect_version=VERSIONS["base"])
    inst.register()
    inst.seed()
    return inst


def request_update(inst: Install, expect_target: str) -> float:
    status, body = http("POST", inst.api + "/system/update", inst.owner, timeout=180)
    check(status == 202 and body.get("installing") == expect_target,
          f"update to {expect_target} accepted ({status} {body})")
    return time.time()


def wait_result(inst: Install, healthy_on: str, since: float) -> dict:
    deadline = time.time() + UPDATE_TIMEOUT
    while time.time() < deadline:
        state = inst.state()
        result = state.get("last_result") or {}
        if not state.get("in_progress") and result and inst.version() == healthy_on:
            inst.wait_healthy(healthy_on, timeout=120)
            print(f"    --  click to healthy: {time.time() - since:.0f}s")
            return result
        time.sleep(2)
    raise Failed(f"no update result within {UPDATE_TIMEOUT}s; see {inst.log}")


def one_notice(inst: Install, before: int) -> None:
    deadline = time.time() + 60
    while time.time() < deadline and len(inst.system_notices()) == before:
        time.sleep(2)
    time.sleep(3)
    check(len(inst.system_notices()) == before + 1, "exactly one update notice")
    check(inst.state()["last_result"]["notified"] is True, "the result is marked as reported")


def after_update_checks(inst: Install, outcome: str, target: str) -> None:
    state = inst.state()
    check("in_progress" not in state, "no update left in progress")
    check(state["last_result"]["outcome"] == outcome, f"outcome is {outcome} ({state['last_result']})")
    failed = target in state.get("failed_versions", [])
    check(failed == (outcome != "ok"), "failed versions list is consistent")
    dump = inst.config / "backups" / "pre-update.dump"
    check(dump.exists() and dump.stat().st_size > 0, "pre-update database dump kept")
    if not WINDOWS:
        check((dump.stat().st_mode & 0o777) == 0o600, "dump is readable by its owner only")
    check(inst.postmasters() == 1, "exactly one PostgreSQL server running")


def stop_and_check_clean(inst: Install) -> None:
    inst.stop()
    time.sleep(2)
    left = inst.processes()
    check(not left, f"no processes left behind ({left})")


def failed_update(work: Path, wheels, name: str, variant: str, outcome: str) -> None:
    inst = fresh(work, name, wheels, variant)
    try:
        freeze, db, notices = inst.freeze(), inst.db(), len(inst.system_notices())
        t0 = request_update(inst, VERSIONS[variant])
        wait_result(inst, VERSIONS["base"], t0)
        after_update_checks(inst, outcome, VERSIONS[variant])
        check(inst.freeze() == freeze, "installed packages identical to before")
        after = inst.db()
        check(after == db, "database identical to before (tables, revision, row counts)")
        check(MARKER_TABLE not in after["tables"], "the new version's table is absent")
        check(inst.seeded_location_present(), "seeded data intact")
        one_notice(inst, notices)
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def good_update(inst: Install, target_variant: str) -> None:
    db, notices = inst.db(), len(inst.system_notices())
    t0 = request_update(inst, VERSIONS[target_variant])
    wait_result(inst, VERSIONS[target_variant], t0)
    assert_updated(inst, target_variant, db, notices)


def assert_updated(inst: Install, target_variant: str, db: dict, notices: int) -> None:
    after_update_checks(inst, "ok", VERSIONS[target_variant])
    check(inst.pip("show", "celerp").count(f"Version: {VERSIONS[target_variant]}") == 1,
          f"celerp {VERSIONS[target_variant]} installed")
    after = inst.db()
    check(after["revision"] == MARKER_REVISION, "database at the new release's revision")
    added = set(after["tables"]) ^ set(db["tables"])
    check(added == {MARKER_TABLE}, f"exactly the new table was added (changed: {sorted(added)})")
    check(after["counts"] == db["counts"], "row counts unchanged")
    check(inst.seeded_location_present(), "seeded data intact")
    one_notice(inst, notices)


# ── Scenarios ─────────────────────────────────────────────────────────────────


def e1(work, wheels):
    inst = fresh(work, "E1", wheels, "good")
    try:
        good_update(inst, "good")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def e2(work, wheels):
    failed_update(work, wheels, "E2", "pip_fail", "failed")


def e3(work, wheels):
    failed_update(work, wheels, "E3", "migrate_fail", "rolled_back")


def e4(work, wheels):
    failed_update(work, wheels, "E4", "health_fail", "rolled_back")


def e5(work, wheels):
    inst = fresh(work, "E5", wheels, "good")
    try:
        db, notices = inst.db(), len(inst.system_notices())
        request_update(inst, VERSIONS["good"])
        deadline = time.time() + UPDATE_TIMEOUT
        while (inst.state().get("in_progress") or {}).get("step") != "migrate":
            if time.time() > deadline:
                raise Failed("the update never reached the database step")
            time.sleep(0.1)
        inst.kill()
        check(inst.state()["in_progress"]["to"] == VERSIONS["good"], "killed with the update unfinished")
        t0 = time.time()
        inst.start(expect_version=VERSIONS["good"])
        wait_result(inst, VERSIONS["good"], t0)
        assert_updated(inst, "good", db, notices)
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def e6(work, wheels):
    """The oldest supported pre-updater release: upgraded by pip once, as its
    users do, then updated from the app."""
    inst = Install(work, "E6")
    try:
        inst.create_as_existing("celerp==2.5.0")
        inst.init()
        inst.find_links = [wheels["base"], wheels["good"]]
        inst.start(expect_version="2.5.0")
        inst.register()
        inst.seed()
        # 2.5.0 only handles SIGTERM once its startup banner is out, which it can
        # still be waiting on here; its users stop it with Ctrl+C or a service.
        inst.stop(group=True)
        inst.pip("install", "-q", f"celerp=={VERSIONS['base']}")
        inst.start(expect_version=VERSIONS["base"])
        inst.owner = inst.login("owner@example.com")
        check(inst.seeded_location_present(), "2.5.0 data carried to the new release")
        good_update(inst, "good")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def e7(work, wheels):
    if _pg_pin(REPO) is None:
        print("    --  skipped: celerp-postgres is not a pinned dependency on this platform")
        return
    inst = fresh(work, "E7", wheels, "good_pg")
    try:
        old = inst.pip("show", PG_PACKAGE)
        good_update(inst, "good_pg")
        new = inst.pip("show", PG_PACKAGE)
        check(old != new and "Version: " in new, f"{PG_PACKAGE} replaced")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def e8(work, wheels):
    inst = fresh(work, "E8", wheels, "good")
    try:
        db, notices = inst.db(), len(inst.system_notices())
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(
                lambda _: http("POST", inst.api + "/system/update", inst.owner, timeout=180), range(2)))
        codes = sorted(status for status, _ in replies)
        check(codes == [202, 409], f"two requests at once start one update ({replies})")
        t0 = time.time()
        wait_result(inst, VERSIONS["good"], t0)
        assert_updated(inst, "good", db, notices)
        status, body = http("POST", inst.api + "/system/update", inst.owner, timeout=180)
        check(status == 409 and body.get("detail") == "current", f"nothing newer: refused ({status} {body})")
        check(not (inst.config / ".restart_requested").exists(), "no restart requested")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def e9(work, wheels):
    inst = fresh(work, "E9", wheels, "good")
    try:
        status, _ = http("POST", inst.api + "/companies/me/users", inst.owner,
                         {"email": "member@example.com", "name": "Member", "role": "admin",
                          "password": PASSWORD})
        check(status == 200, "a member was added")
        member = inst.login("member@example.com")
        status, _ = http("POST", inst.api + "/system/update", member)
        check(status == 403, f"a member cannot install ({status})")
        status, _ = http("POST", inst.api + "/system/update")
        check(status == 401, f"an anonymous request is refused ({status})")
        check(not (inst.config / ".restart_requested").exists(), "no restart requested")
        check(inst.version() == VERSIONS["base"], "still on the installed version")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


SCENARIOS = {
    "E1": (e1, {"base", "good"}),
    "E2": (e2, {"base", "pip_fail"}),
    "E3": (e3, {"base", "migrate_fail"}),
    "E4": (e4, {"base", "health_fail"}),
    "E5": (e5, {"base", "good"}),
    "E6": (e6, {"base", "good"}),
    "E7": (e7, {"base", "good_pg"}),
    "E8": (e8, {"base", "good"}),
    "E9": (e9, {"base", "good"}),
}


# ── Release gate ──────────────────────────────────────────────────────────────


def _wheel_version(wheel: Path) -> str:
    return wheel.name.split("-")[1]


def r1(work: Path, wheel: Path) -> None:
    inst = Install(work, "R1")
    try:
        inst.create(str(wheel))
        inst.init()
        inst.start(expect_version=_wheel_version(wheel))
        inst.register()
        inst.seed()
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def r2(work: Path, wheel: Path) -> None:
    """The current PyPI release updated to this wheel. A release that predates
    the updater has no update endpoint; its users upgrade with pip, so it does."""
    version = _wheel_version(wheel)
    inst = Install(work, "R2")
    try:
        inst.create_as_existing("celerp")
        inst.init()
        inst.find_links = [wheel.parent]
        inst.start()
        inst.register()
        inst.seed()
        status, _ = http("GET", inst.api + "/system/update", inst.owner)
        if status == 200:
            t0 = request_update(inst, version)
            wait_result(inst, version, t0)
            check(inst.state()["last_result"]["outcome"] == "ok", "updated from the app")
        else:
            inst.stop()
            inst.pip("install", "-q", str(wheel))
            inst.start(expect_version=version)
        check(inst.version() == version, f"serving {version}")
        check(inst.seeded_location_present(), "data carried to the new release")
        stop_and_check_clean(inst)
    finally:
        inst.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--release-wheel", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    args = parser.parse_args()

    work = (args.work or Path(tempfile.mkdtemp(prefix="celerp-e2e-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    print(f"work directory: {work}")
    if args.release_wheel:
        runs = [("R1", lambda: r1(work, args.release_wheel.resolve())),
                ("R2", lambda: r2(work, args.release_wheel.resolve()))]
    else:
        chosen = args.scenario or sorted(SCENARIOS)
        wheels = build_wheels(work, set().union(*(SCENARIOS[s][1] for s in chosen)))
        runs = [(s, (lambda s=s: SCENARIOS[s][0](work, wheels))) for s in chosen]

    failures = []
    for name, fn in runs:
        print(f"{name}:")
        try:
            fn()
        except Exception as exc:  # report every scenario, then fail the run
            print(f"    FAIL {name}: {exc}")
            failures.append(name)
    if not args.keep and not failures:
        shutil.rmtree(work, ignore_errors=True)
    print("passed" if not failures else f"failed: {', '.join(failures)} (logs under {work / 'runs'})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

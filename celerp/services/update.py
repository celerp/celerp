# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Self-update for pip installs run by `celerp start`.

The API decides whether an update is available and whether this install can
install it; the supervisor (`celerp start`) does the install, because the API
and UI must be stopped while packages change. The two talk through the restart
sentinel: an empty sentinel is a plain restart, `update <version>` is a request
to update. Every step is recorded in `update_state.json` in the config dir first,
so a supervisor killed mid-update finishes or undoes it on the next start.

`run_update` owns the order of the steps and the undo rules; `Steps` does the
work. Tests drive `run_update` with fake steps; the supervisor uses
`SupervisorSteps`.
"""

from __future__ import annotations

import json
import logging
import os
import asyncio
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from importlib import invalidate_caches, metadata
from pathlib import Path
from typing import Callable

from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

STATE_FILE = "update_state.json"
_UPDATE_PREFIX = "update "
_MIN_PIP = Version("22.2")  # first release with `install --dry-run --report`
CHECK_TIMEOUT_SECONDS = 120
VERIFY_TIMEOUT_SECONDS = 120
WINDOW_START_HOUR = 3
WINDOW_END_HOUR = 5

# Outcomes recorded in last_result["outcome"].
OK = "ok"
FAILED = "failed"            # nothing changed, or packages put back before the database changed
ROLLED_BACK = "rolled_back"  # the new version failed after the database changed; database and packages restored
ROLLBACK_FAILED = "rollback_failed"


class UpdateError(RuntimeError):
    """A step failed; the message is shown to the install owner."""


# ── Paths and state ───────────────────────────────────────────────────────────


def config_dir() -> Path:
    from celerp.config import config_path
    return config_path().parent


def sentinel_path() -> Path:
    from celerp.routers.system import _restart_sentinel_path
    return _restart_sentinel_path()


def backup_paths() -> tuple[Path, Path]:
    """(freeze file, database dump) kept from the last update attempt."""
    d = config_dir() / "backups"
    return d / "pre-update-requirements.txt", d / "pre-update.dump"


def read_state() -> dict:
    path = config_dir() / STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    path = config_dir() / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Versions and blockers ─────────────────────────────────────────────────────


def installed_version() -> str:
    """The celerp version on disk now (not the one this process imported)."""
    invalidate_caches()
    try:
        return metadata.version("celerp")
    except metadata.PackageNotFoundError:
        return "0.0.0+dev"


def is_dev_build(version: str) -> bool:
    return ".dev" in version or "+dev" in version or version.startswith("0.0.0")


def validate_target(version: str) -> str:
    """A release version string safe to pass to pip, or UpdateError."""
    try:
        parsed = Version(version.strip())
    except (InvalidVersion, AttributeError) as exc:
        raise UpdateError(f"not a release version: {version!r}") from exc
    if parsed.local or parsed.is_devrelease:
        raise UpdateError(f"not a release version: {version!r}")
    return str(parsed)


def _pip_blocker() -> str | None:
    try:
        pip_version = Version(metadata.version("pip"))
    except metadata.PackageNotFoundError:
        return "pip_missing"
    except InvalidVersion:
        return "pip_old"
    return "pip_old" if pip_version < _MIN_PIP else None


# The blockers that also stop `celerp upgrade` (the rest only concern the in-app update).
PIP_BLOCKERS = ("pip_missing", "pip_old", "not_writable")

# Every reason the update card can show (`shell.update_blocked_<code>`): the
# blockers, "administrator" for everyone but the install owner, and the refusals
# a person can get from asking for an update.
CARD_REASONS = ("channel", "dev_build", "unsupervised", *PIP_BLOCKERS, "container",
                "administrator", "in_progress", "check_failed", "current")


def self_update_blockers() -> list[str]:
    """Reasons this install cannot update itself, as codes the card translates
    (`shell.update_blocked_<code>`). Empty means it can."""
    blockers = []
    if os.environ.get("CELERP_INSTALL_CHANNEL", "pypi") != "pypi":
        blockers.append("channel")
    if is_dev_build(installed_version()):
        blockers.append("dev_build")
    if os.environ.get("CELERP_SUPERVISED") != "1":
        blockers.append("unsupervised")
    pip = _pip_blocker()
    if pip:
        blockers.append(pip)
    import celerp
    if not os.access(Path(celerp.__file__).resolve().parent.parent, os.W_OK):
        blockers.append("not_writable")
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        blockers.append("container")
    return blockers


def available_update(timeout: float = CHECK_TIMEOUT_SECONDS) -> str | None:
    """The celerp version pip would install as an upgrade, or None when current.

    pip resolves the full dependency set, so a release that cannot install on
    this Python or platform is never offered; yanked and pre-releases are
    skipped and PIP_INDEX_URL / PIP_FIND_LINKS are honoured. An error or a
    timeout raises UpdateError, so callers say "could not check", never guess.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--dry-run", "--quiet",
             "--disable-pip-version-check", "--report", "-", "celerp"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("update check failed: %s", exc)
        raise UpdateError("could not reach the package index") from exc
    if result.returncode != 0:
        log.warning("update check failed: %s", result.stderr.strip()[-500:])
        raise UpdateError("could not reach the package index")
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError("the package index gave no answer") from exc
    for item in report.get("install", []):
        meta = item.get("metadata", {})
        if meta.get("name", "").lower() == "celerp":
            try:
                newer = Version(meta.get("version", "")) > Version(installed_version())
            except InvalidVersion as exc:
                raise UpdateError("the package index gave no answer") from exc
            return meta["version"] if newer else None
    return None


# ── Requests (API side) ───────────────────────────────────────────────────────


def requested_target(sentinel_text: str) -> str | None:
    """The version an `update <version>` sentinel asks for; None for a plain restart."""
    text = sentinel_text.strip()
    if not text.startswith(_UPDATE_PREFIX):
        return None
    return validate_target(text[len(_UPDATE_PREFIX):])


def update_in_progress() -> bool:
    if read_state().get("in_progress"):
        return True
    try:
        return requested_target(sentinel_path().read_text(encoding="utf-8")) is not None
    except (OSError, UpdateError):
        return False


def request_update(target: str) -> None:
    """Ask the supervisor to update to `target` at the next API restart.

    The caller then restarts the API (`_send_sigterm`), which keeps the content.
    """
    path = sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_UPDATE_PREFIX + validate_target(target), encoding="utf-8")


def auto_enabled() -> bool:
    from celerp.config import read_config
    return bool(read_config().get("updates", {}).get("auto", True))


def in_install_window(now_utc: datetime, tz_name: str | None) -> bool:
    """True between 03:00 and 05:00 in `tz_name` (UTC when unset or unknown)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return WINDOW_START_HOUR <= now_utc.astimezone(tz).hour < WINDOW_END_HOUR


# ── Status, requests and the nightly loop (API side) ─────────────────────────

CHECK_INTERVAL_SECONDS = 3600  # hourly ticks always land inside the two-hour window
FIRST_CHECK_DELAY_SECONDS = 60  # startup stays light; a quick restart makes no index call

# The last check, kept in memory: one answer for every page view.
_check: dict = {"latest": None, "error": "", "checked_at": None}
_request_lock = threading.Lock()


class UpdateRefused(UpdateError):
    """An update request that cannot go ahead; `code` is shown via
    `shell.update_blocked_<code>` like the blockers."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def refresh_check() -> dict:
    """Ask the package index now and keep the answer. Blocking; run in a thread."""
    try:
        latest, error = available_update(), ""
    except UpdateError as exc:
        latest, error = None, str(exc)
    _check.update(latest=latest, error=error, checked_at=_now())
    return dict(_check)


def status(*, owner: bool) -> dict:
    """What the update card shows. Only the install owner may install; everyone
    else sees reason "administrator"."""
    blockers = self_update_blockers()
    reason = (blockers[0] if blockers else "") if owner else "administrator"
    return {
        "current": installed_version(),
        "latest": _check["latest"],
        "check_error": _check["error"],
        "checked_at": _check["checked_at"],
        "can_install": owner and not blockers,
        "reason": reason,
        "auto": auto_enabled(),
        "installing": update_in_progress(),
        "last_result": read_state().get("last_result"),
    }


def request_available(*, automatic: bool = False) -> str:
    """Check the index, then ask the supervisor to install the newest version.

    The version always comes from this check, never from a caller. Returns the
    requested version; raises UpdateRefused. The caller restarts the API.
    Automatic requests skip a version that already failed here; a person can
    still ask for it.
    """
    with _request_lock:  # two requests at once produce one sentinel
        blockers = self_update_blockers()
        if blockers:
            raise UpdateRefused(blockers[0])
        if update_in_progress():
            raise UpdateRefused("in_progress")
        check = refresh_check()
        if check["error"]:
            raise UpdateRefused("check_failed")
        target = check["latest"]
        if not target:
            raise UpdateRefused("current")
        if automatic and target in read_state().get("failed_versions", []):
            raise UpdateRefused("failed_before")
        request_update(target)
        return target


def set_auto(enabled: bool) -> None:
    from celerp.config import _update_config

    def _set(cfg: dict) -> None:
        cfg["updates"] = {"auto": enabled}
    _update_config(_set)


async def _owner_timezone(session) -> str | None:
    """The time zone of the install owner's first company, if set."""
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company
    from celerp.services.auth import installation_root_user_id

    owner_id = await installation_root_user_id(session)
    if owner_id is None:
        return None
    settings = await session.scalar(
        select(Company.settings)
        .join(UserCompany, UserCompany.company_id == Company.id)
        .where(UserCompany.user_id == owner_id)
        .order_by(Company.created_at)
        .limit(1)
    )
    return (settings or {}).get("timezone")


def result_message(result: dict) -> tuple[str, str]:
    """(title, body) for the notification about an update attempt."""
    if result.get("ok"):
        return (f"Celerp was updated to {result['to']}",
                f"Celerp is now on version {result['to']}.")
    title = f"Celerp could not update to {result['to']}"
    if result.get("outcome") == ROLLBACK_FAILED:
        return title, (f"The update to {result['to']} failed and could not be undone: "
                       f"{result.get('reason', '')}. The backup taken before the update is "
                       "kept; see the update instructions to restore it.")
    return title, (f"Celerp is still on {result['from']}: {result.get('reason', '')}. "
                   "Your data was not changed.")


async def notify_last_result(session) -> int:
    """One high-priority notification per company about the last update attempt,
    once. The state is marked only after the commit, so a crash in between can
    repeat the notice, never lose it. Returns the number created."""
    from sqlalchemy import select

    from celerp.models.company import Company
    from celerp.notifications import service as notif_service

    state = read_state()
    result = state.get("last_result")
    if not result or result.get("notified"):
        return 0
    title, body = result_message(result)
    company_ids = (await session.execute(
        select(Company.id).where(Company.is_active.is_(True)))).scalars().all()
    for company_id in company_ids:
        await notif_service.create(session, company_id, "system", title, body, priority="high")
    await session.commit()
    result["notified"] = True
    write_state(state)
    return len(company_ids)


async def auto_update_tick(session, now_utc: datetime, restart: Callable[[], None]) -> str | None:
    """One loop tick: refresh the check, and inside the install window with
    automatic updates on, request the update and restart. Returns the version
    requested, if any."""
    if not auto_enabled() or not in_install_window(now_utc, await _owner_timezone(session)):
        await asyncio.to_thread(refresh_check)
        return None
    try:
        target = await asyncio.to_thread(request_available, automatic=True)
    except UpdateRefused as exc:
        log.info("automatic update skipped: %s", exc.code)
        return None
    log.info("automatic update to %s requested", target)
    await asyncio.to_thread(restart)
    return target


async def update_loop(restart: Callable[[], None]) -> None:
    """Background loop started from the API lifespan: report the last update,
    then tick hourly."""
    from celerp.db import get_session_ctx

    try:
        async with get_session_ctx() as session:
            await notify_last_result(session)
    except Exception:
        log.exception("update result notification failed")
    await asyncio.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            async with get_session_ctx() as session:
                await auto_update_tick(session, datetime.now(timezone.utc), restart)
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ── The update (supervisor side) ──────────────────────────────────────────────


class Steps:
    """The work behind each update step. Every method raises on failure."""

    def freeze(self, path: Path) -> None: ...
    def dump(self, path: Path) -> None: ...
    def stop_cluster(self) -> None: ...
    def install(self, target: str) -> None: ...
    def start_cluster(self) -> None: ...
    def migrate(self) -> None: ...
    def verify(self, target: str) -> tuple: ...
    def stop_children(self, children: tuple) -> None: ...
    def restore(self, path: Path) -> None: ...
    def reinstall(self, path: Path) -> None: ...


def _mark(state: dict, current: str, target: str, step: str) -> None:
    state["in_progress"] = {"from": current, "to": target, "step": step,
                            "started_at": state.get("in_progress", {}).get("started_at") or _now()}
    write_state(state)


def _finish(state: dict, current: str, target: str, outcome: str, reason: str = "") -> dict:
    result = {"ok": outcome == OK, "outcome": outcome, "from": current, "to": target,
              "reason": reason, "at": _now(), "notified": False}
    state.pop("in_progress", None)
    state["last_result"] = result
    if outcome != OK:
        failed = state.setdefault("failed_versions", [])
        if target not in failed:
            failed.append(target)
    write_state(state)
    level = logging.INFO if outcome == OK else logging.ERROR
    log.log(level, "update %s -> %s: %s %s", current, target, outcome, reason)
    return result


def _undo(steps: Steps, state: dict, current: str, target: str, reason: str,
          *, restore: bool) -> dict:
    """Put the database (when `restore`) and packages back as they were."""
    freeze, dump = backup_paths()
    _mark(state, current, target, "rollback")
    try:
        if restore:
            steps.restore(dump)
        steps.stop_cluster()
        steps.reinstall(freeze)
        steps.start_cluster()
    except Exception as exc:
        log.error(
            "Rollback failed: %s. The pre-update database is at %s (pg_restore --clean -d <url> %s) "
            "and the previous packages at %s (python -m pip install -r %s).",
            exc, dump, dump, freeze, freeze,
        )
        return _finish(state, current, target, ROLLBACK_FAILED, f"{reason}; rollback failed: {exc}")
    return _finish(state, current, target, ROLLED_BACK if restore else FAILED, reason)


def run_update(target: str, steps: Steps, *, verify: bool = True) -> tuple[dict, tuple]:
    """Update to `target`. Returns (last_result, children).

    `children` are the verified new API and UI processes (empty unless the
    update succeeded with `verify`). The database is restored from the dump
    once a migration may have touched it; a failed install leaves it untouched.
    """
    target = validate_target(target)
    current = installed_version()
    state = read_state()
    freeze, dump = backup_paths()
    freeze.parent.mkdir(parents=True, exist_ok=True)

    _mark(state, current, target, "snapshot")
    try:
        steps.freeze(freeze)
        _mark(state, current, target, "backup")
        steps.dump(dump)
    except Exception as exc:
        return _finish(state, current, target, FAILED, f"backup failed: {exc}"), ()

    _mark(state, current, target, "install")
    try:
        steps.stop_cluster()
        steps.install(target)
    except Exception as exc:
        return _undo(steps, state, current, target, f"install failed: {exc}", restore=False), ()

    _mark(state, current, target, "migrate")
    try:
        steps.start_cluster()
        steps.migrate()
    except Exception as exc:
        return _undo(steps, state, current, target, f"database update failed: {exc}", restore=True), ()

    if not verify:
        return _finish(state, current, target, OK), ()
    return _verify(steps, state, current, target)


def _verify(steps: Steps, state: dict, current: str, target: str) -> tuple[dict, tuple]:
    _mark(state, current, target, "verify")
    try:
        children = steps.verify(target)
    except Exception as exc:
        return _undo(steps, state, current, target, f"new version did not start: {exc}", restore=True), ()
    return _finish(state, current, target, OK), children


def reconcile(steps: Steps) -> tuple[dict | None, tuple]:
    """Finish or undo an update the supervisor did not live to finish.

    Runs once at `celerp start`: when the target is installed it migrates and
    verifies; otherwise it puts everything back. Safe to repeat: restore and
    reinstall are exact regardless of how far the last attempt got.
    """
    state = read_state()
    pending = state.get("in_progress")
    if not pending:
        return None, ()
    current, target, step = pending["from"], pending["to"], pending["step"]
    log.warning("Resuming an interrupted update to %s (last step: %s)", target, step)
    if step in ("snapshot", "backup"):
        return _finish(state, current, target, FAILED, "interrupted before anything changed"), ()
    if step != "rollback" and installed_version() == target:
        try:
            steps.start_cluster()
            steps.migrate()
        except Exception as exc:
            return _undo(steps, state, current, target, f"database update failed: {exc}", restore=True), ()
        return _verify(steps, state, current, target)
    return _undo(steps, state, current, target, "interrupted",
                 restore=step in ("migrate", "verify", "rollback")), ()


# ── Real steps ────────────────────────────────────────────────────────────────


def _pip(*args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pip", *args, "--disable-pip-version-check"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise UpdateError((result.stderr or result.stdout).strip()[-800:] or f"pip exit {result.returncode}")


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


class SupervisorSteps(Steps):
    """Real steps, run by `celerp start` with its API and UI stopped.

    Everything used after `install` is imported here, before pip changes the
    installed files; the new version only ever runs in child processes.
    """

    def __init__(self, cfg: dict, env: dict, *, spawn_api: Callable, spawn_ui: Callable,
                 wait_ready: Callable) -> None:
        from celerp.config import settings
        from celerp.services import backup

        self.cfg = cfg
        self.env = env
        self.embedded = bool(cfg.get("database", {}).get("embedded"))
        self.api_port = cfg["server"]["api_port"]
        self.ui_port = cfg["server"]["ui_port"]
        self._spawn_api = spawn_api
        self._spawn_ui = spawn_ui
        self._wait_ready = wait_ready
        self._backup = backup
        if not settings.pg_bin_dir:
            settings.pg_bin_dir = cfg.get("backup", {}).get("pg_bin_dir", "")
        if self.embedded:
            from celerp import embedded_pg
            import celerp_postgres  # noqa: F401  (imported before pip replaces it)
            self._embedded_pg = embedded_pg

    @property
    def db_url(self) -> str:
        return self.cfg["database"]["url"]

    def freeze(self, path: Path) -> None:
        result = subprocess.run([sys.executable, "-m", "pip", "freeze", "--disable-pip-version-check"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError(result.stderr.strip()[-800:])
        path.write_text(result.stdout, encoding="utf-8")

    def dump(self, path: Path) -> None:
        data = self._backup.dump_database(self.db_url)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)

    def stop_cluster(self) -> None:
        if self.embedded:
            self._embedded_pg.stop_cluster(config_dir())

    def start_cluster(self) -> None:
        if self.embedded:
            self.cfg["database"]["url"] = self._embedded_pg.ensure_cluster(config_dir())
            self.env["DATABASE_URL"] = self.db_url

    def install(self, target: str) -> None:
        _pip("install", f"celerp=={target}")

    def reinstall(self, path: Path) -> None:
        _pip("install", "-r", str(path))

    def migrate(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "celerp", "migrate", "--db-url", self.db_url],
            env=self.env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise UpdateError((result.stderr or result.stdout).strip()[-800:])

    def verify(self, target: str) -> tuple:
        """Start the new API alone and require it healthy on the target
        version before the UI starts, so no user reaches it until it passed."""
        api = self._spawn_api(self.env, self.api_port)
        base = f"http://127.0.0.1:{self.api_port}"
        deadline = time.time() + VERIFY_TIMEOUT_SECONDS
        version = None
        while time.time() < deadline and api.poll() is None:
            if get_json(base + "/health/ready") is not None:
                version = (get_json(base + "/health") or {}).get("version")
                if version is not None:
                    break
            time.sleep(0.5)
        if version != target:
            _terminate(api)
            raise UpdateError(f"reported version {version}" if version else "not healthy")
        ui = self._spawn_ui(self.env, self.ui_port)
        if not self._wait_ready((api, self.api_port), (ui, self.ui_port), VERIFY_TIMEOUT_SECONDS):
            self.stop_children((api, ui))
            raise UpdateError("the web interface did not start")
        return api, ui

    def stop_children(self, children: tuple) -> None:
        for proc in children:
            _terminate(proc)

    def restore(self, path: Path) -> None:
        self._backup.restore_database(path.read_bytes(), self.db_url, clean_schema=True)

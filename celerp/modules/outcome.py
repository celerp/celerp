# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Which modules the API process is running, recorded where the UI process reads it.

The API process decides which modules run: it admits them, applies their
migrations, loads them and registers their routes. Once that is done it records,
in the instance_meta table both processes share, the modules it is running and
the reason each other enabled module is not, stamped with a token unique to that
API process. The same token is served on /health, which the API only answers once
its startup has finished.

The UI process loads modules when it is imported, which can be before the API
has finished. So it waits for the API's /health, reads the record, and offers
only the modules that same API process is running; a module the API refused or
failed is skipped with the API's reason, and its dependents with it. A record
from another API process (one that started in recovery and loaded nothing, say)
or none at all means no module is offered. There is no deadline on the wait: the
supervisor stops the UI if the API exits, and the UI is no use before the API.

A module can still fail in the UI process (its UI routes raise, say). Once its
own modules are started, the UI moves each such module, and every module it took
out with it, from running to failed in the same record, and marks the record as
reported. Until that report is in, the API process serves no module route; on the
first module request after it, the API stops each module the record no longer
lists as running - its routes, slots and the rest, as for a failure at its own
startup - records the result again, and only then serves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
import uuid

log = logging.getLogger(__name__)

# Identifies this process to the other one; served on the API's /health.
BOOT_TOKEN = uuid.uuid4().hex

_KEY = "module_outcome"
_POLL_SECONDS = 0.5
_LOG_EVERY_SECONDS = 30.0

NOT_REPORTED = "Not running: the server did not report this module as started."
STARTING = "Celerp is still starting. Try again in a moment."

# API side: whether module routes wait for the UI process's report. Set once the
# API process has recorded its modules, cleared once the report is applied.
_awaiting_ui = False
_confirm_lock: asyncio.Lock | None = None


def _record(*, ui_reported: bool) -> dict:
    from celerp.modules.loader import load_errors, loaded_modules

    return {
        "boot": BOOT_TOKEN,
        "running": sorted(m["name"] for m in loaded_modules()),
        "failed": load_errors(),
        "ui_reported": ui_reported,
    }


def publish(conn) -> None:
    """Record what this (API) process loaded. Called once its modules' routes are
    registered, on a sync connection."""
    from celerp.migrations._data_reconcile import set_meta

    set_meta(conn, _KEY, json.dumps(_record(ui_reported=False)))


def await_ui_report() -> None:
    """API side, after :func:`publish`: serve no module route until the UI
    process has reported (:func:`confirm_ui_report`)."""
    global _awaiting_ui
    _awaiting_ui = True


def ui_report_applied() -> bool:
    return not _awaiting_ui


def read(conn, *, lock: bool = False) -> dict | None:
    """The last recorded outcome, or None when nothing was ever recorded. Reads
    only; never creates the table. ``lock`` holds the row until the transaction
    ends, so a read-change-write cannot lose the other process's write."""
    import sqlalchemy as sa

    from celerp.migrations._data_reconcile import _META_TABLE

    if _META_TABLE not in sa.inspect(conn).get_table_names():
        return None
    raw = conn.execute(sa.text(f"SELECT value FROM {_META_TABLE} WHERE key = :k"
                               + (" FOR UPDATE" if lock else "")),
                       {"k": _KEY}).scalar()
    return json.loads(raw) if raw else None


def report_stopped(fence, record: dict | None) -> dict[str, str]:
    """UI side, once it holds its version *fence* and its modules' routes are
    registered: move every module the API's ``record`` lists as running but this
    process is not running to failed, with this process's reason, and mark the
    record reported, writing through the fence. Returns the modules moved."""
    import sqlalchemy as sa

    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules.loader import is_running, load_errors

    if not record:
        return {}
    errors = load_errors()
    stopped = {n: errors.get(n) or NOT_REPORTED
               for n in record["running"] if not is_running(n)}
    with fence.engine(poolclass=sa.pool.NullPool) as engine, engine.begin() as conn:
        current = read(conn, lock=True)
        if not current or current.get("boot") != record["boot"]:
            return {}  # that API process is gone; the next one records afresh
        current["running"] = [n for n in current["running"] if n not in stopped]
        current["failed"] = {**current.get("failed", {}), **stopped}
        current["ui_reported"] = True
        set_meta(conn, _KEY, json.dumps(current))
    for name, reason in stopped.items():
        log.error("Module %r failed in the UI process and is stopped: %s", name, reason)
    return stopped


def apply_reported_stops(conn, app) -> bool:
    """API side, on a sync connection: once the UI process has reported for this
    process, stop every module this process is running that the record no longer
    lists as running, with the recorded reason, and record the result. Whether
    the report was in."""
    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules.loader import is_running, loaded_modules, stop_module

    record = read(conn, lock=True)
    if not record or record.get("boot") != BOOT_TOKEN or not record.get("ui_reported"):
        return False
    listed = set(record["running"])
    failed = record.get("failed", {})
    stopped: set[str] = set()
    for name in [m["name"] for m in loaded_modules()]:
        if name not in listed and is_running(name):
            stopped |= stop_module(app, name, failed.get(name) or NOT_REPORTED)
    if stopped:
        set_meta(conn, _KEY, json.dumps(_record(ui_reported=True)))
        log.error("Stopped module(s) %s: they failed in the UI process", ", ".join(sorted(stopped)))
    return True


async def confirm_ui_report(app, engine) -> bool:
    """API side, before serving a module route: whether module routes may be
    served yet. True once the UI process's report is in and applied
    (:func:`apply_reported_stops`); from then on without reading the record."""
    global _awaiting_ui, _confirm_lock
    if not _awaiting_ui:
        return True
    if _confirm_lock is None:
        _confirm_lock = asyncio.Lock()
    async with _confirm_lock:
        if _awaiting_ui:
            async with engine.begin() as conn:
                if await conn.run_sync(apply_reported_stops, app):
                    _awaiting_ui = False
    return not _awaiting_ui


def _health_token(api_url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=2) as resp:
            return json.loads(resp.read()).get("boot")
    except (OSError, ValueError):
        return None


def wait_for_api_token(api_url: str) -> str:
    """Block until the API at ``api_url`` answers /health, and return its token."""
    started = last_log = time.monotonic()
    while True:
        token = _health_token(api_url)
        if token:
            return token
        now = time.monotonic()
        if now - last_log >= _LOG_EVERY_SECONDS:
            log.info("Waiting for the API at %s to finish starting (%ds) before loading modules",
                     api_url, int(now - started))
            last_log = now
        time.sleep(_POLL_SECONDS)


def reported_by_api(api_url: str, database_url: str) -> dict | None:
    """The outcome recorded by the API answering at ``api_url``, or None when
    that API process recorded none."""
    import sqlalchemy as sa

    from celerp.db_url import sync_url

    token = wait_for_api_token(api_url)
    engine = sa.create_engine(sync_url(database_url))
    try:
        with engine.connect() as conn:
            record = read(conn)
    finally:
        engine.dispose()
    if not record or record.get("boot") != token:
        log.error("The API at %s recorded no module outcome; no module is offered", api_url)
        return None
    return record


def admission_as_reported(module_dir: str, enabled: set[str], record: dict | None):
    """This process's own admission, narrowed to the modules the API process is
    running: every other one refused with the API's reason (or NOT_REPORTED),
    along with every module depending on it."""
    from celerp.modules.loader import admit_modules

    admission = admit_modules(module_dir, enabled)
    running = set(record["running"]) if record else set()
    failed = record.get("failed", {}) if record else {}
    return admission.without({
        m.name: failed.get(m.name) or NOT_REPORTED
        for m in admission.admitted if m.name not in running})

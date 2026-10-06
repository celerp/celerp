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

A module can still fail in the UI process (its UI routes raise, say). The UI then
moves it, and every module it took out with it, from running to failed in the
same record. The API process watches the record and stops each module its own
record no longer lists as running - its routes, slots and the rest, as for a
failure at its own startup - then records the result again.
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
_WATCH_SECONDS = 2.0

NOT_REPORTED = ("Not running: this module didn't start and gave no reason. Restart Celerp; "
                "if it still doesn't start, ask the module's developer.")


def publish(conn) -> None:
    """Record what this (API) process loaded. Called once its modules' routes are
    registered, on a sync connection."""
    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules.loader import load_errors, loaded_modules

    set_meta(conn, _KEY, json.dumps({
        "boot": BOOT_TOKEN,
        "running": sorted(m["name"] for m in loaded_modules()),
        "failed": load_errors(),
    }))


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


def report_stopped(database_url: str, record: dict | None) -> dict[str, str]:
    """UI side, after its modules' routes are registered: move every module the
    API's ``record`` lists as running but this process is not running to failed,
    with this process's reason. Returns the modules moved."""
    import sqlalchemy as sa

    from celerp.db_url import sync_url
    from celerp.migrations._data_reconcile import set_meta
    from celerp.modules.loader import is_running, load_errors

    if not record:
        return {}
    errors = load_errors()
    stopped = {n: errors.get(n) or NOT_REPORTED
               for n in record["running"] if not is_running(n)}
    if not stopped:
        return {}
    engine = sa.create_engine(sync_url(database_url))
    try:
        with engine.begin() as conn:
            current = read(conn, lock=True)
            if not current or current.get("boot") != record["boot"]:
                return {}  # that API process is gone; the next one records afresh
            current["running"] = [n for n in current["running"] if n not in stopped]
            current["failed"] = {**current.get("failed", {}), **stopped}
            set_meta(conn, _KEY, json.dumps(current))
    finally:
        engine.dispose()
    for name, reason in stopped.items():
        log.error("Module %r failed in the UI process and is stopped: %s", name, reason)
    return stopped


def apply_reported_stops(conn, app) -> set[str]:
    """API side, on a sync connection: stop every module this process is running
    that its own record no longer lists as running, with the recorded reason, and
    record the result. Returns the modules stopped."""
    from celerp.modules.loader import is_running, loaded_modules, stop_module

    record = read(conn, lock=True)
    if not record or record.get("boot") != BOOT_TOKEN:
        return set()
    listed = set(record["running"])
    failed = record.get("failed", {})
    stopped: set[str] = set()
    for name in [m["name"] for m in loaded_modules()]:
        if name not in listed and is_running(name):
            stopped |= stop_module(app, name, failed.get(name) or NOT_REPORTED)
    if stopped:
        publish(conn)
    return stopped


async def watch_reported_stops(app, engine, interval: float = _WATCH_SECONDS) -> None:
    """API side: :func:`apply_reported_stops` every ``interval`` seconds."""
    while True:
        try:
            async with engine.begin() as conn:
                stopped = await conn.run_sync(apply_reported_stops, app)
            if stopped:
                log.error("Stopped module(s) %s: they failed in the UI process",
                          ", ".join(sorted(stopped)))
        except Exception:
            log.exception("Reading the module outcome record failed; retrying")
        await asyncio.sleep(interval)


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

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
"""
from __future__ import annotations

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


def read(conn) -> dict | None:
    """The last recorded outcome, or None when nothing was ever recorded. Reads
    only; never creates the table."""
    import sqlalchemy as sa

    from celerp.migrations._data_reconcile import _META_TABLE

    if _META_TABLE not in sa.inspect(conn).get_table_names():
        return None
    raw = conn.execute(sa.text(f"SELECT value FROM {_META_TABLE} WHERE key = :k"),
                       {"k": _KEY}).scalar()
    return json.loads(raw) if raw else None


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
